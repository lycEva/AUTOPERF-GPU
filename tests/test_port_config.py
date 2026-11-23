"""Service-port contracts: ports.json (service -> port) and the required --service.

Run: python3 -B -m unittest discover -s tests -p 'test_port_config.py' -v
Only synthetic JMX files are used; no JMeter run and no service traffic.

ports.json is a flat mapping of service name -> port. There is no default port
and no container-name mapping: the service name must be passed with --service.
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_run import prepare

PORT_PROP = "HTTPSampler.port"
TEMPLATE_PORT = 58096


def fixture(path):
    """One enabled and one disabled HTTP sampler, both on TEMPLATE_PORT."""
    root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.4.1")
    top = ET.SubElement(root, "hashTree")
    plan = ET.SubElement(top, "TestPlan", guiclass="TestPlanGui", testclass="TestPlan",
                         testname="plan", enabled="true")
    ET.SubElement(plan, "boolProp", name="TestPlan.serialize_threadgroups").text = "false"
    plan_tree = ET.SubElement(top, "hashTree")
    group = ET.SubElement(plan_tree, "ThreadGroup", guiclass="ThreadGroupGui",
                          testclass="ThreadGroup", testname="tg", enabled="true")
    main = ET.SubElement(group, "elementProp", name="ThreadGroup.main_controller",
                         elementType="LoopController", guiclass="LoopControlPanel",
                         testclass="LoopController", enabled="true")
    ET.SubElement(main, "boolProp", name="LoopController.continue_forever").text = "false"
    ET.SubElement(main, "intProp", name="LoopController.loops").text = "-1"
    for name, value in (("num_threads", 1), ("duration", 9), ("ramp_time", 0), ("delay", 0)):
        ET.SubElement(group, "stringProp", name="ThreadGroup." + name).text = str(value)
    ET.SubElement(group, "boolProp", name="ThreadGroup.scheduler").text = "false"
    group_tree = ET.SubElement(plan_tree, "hashTree")
    for name, enabled in (("on", "true"), ("off", "false")):
        sampler = ET.SubElement(group_tree, "HTTPSamplerProxy", guiclass="HttpTestSampleGui",
                                testclass="HTTPSamplerProxy", testname=name, enabled=enabled)
        ET.SubElement(sampler, "stringProp", name="HTTPSampler.domain").text = "127.0.0.1"
        ET.SubElement(sampler, "stringProp", name=PORT_PROP).text = str(TEMPLATE_PORT)
        ET.SubElement(sampler, "stringProp", name="HTTPSampler.protocol").text = "http"
        ET.SubElement(group_tree, "hashTree")
    ET.ElementTree(root).write(str(path), encoding="utf-8", xml_declaration=True)
    return path


class PortConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="port-config-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.template = fixture(self.base / "source.jmx")
        self.out = self.base / "run"
        self.config = self.base / "ports.json"
        self.write_config({"fs": 58096, "bsocr": 58028, "tableocr": 56010})

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def generate(self, port=None, config=None, out=None, container="svc-a", service=None):
        with contextlib.redirect_stdout(io.StringIO()):
            prepare(self.template, out or self.out, container, 1, 2,
                    monitor_mode="direct", port=port, ports_config=config or self.config,
                    service=service)
        return ET.parse(str((out or self.out) / "plan.jmx")).getroot()

    def enabled_ports(self, root):
        return [int(s.find('stringProp[@name="' + PORT_PROP + '"]').text)
                for s in root.iter("HTTPSamplerProxy")
                if s.get("enabled", "true").lower() != "false"]

    def disabled_ports(self, root):
        return [int(s.find('stringProp[@name="' + PORT_PROP + '"]').text)
                for s in root.iter("HTTPSamplerProxy")
                if s.get("enabled", "true").lower() == "false"]

    def test_explicit_port_rewrites_enabled_samplers_only(self):
        before = self.template.read_bytes()
        root = self.generate(port=9091)
        self.assertEqual(self.enabled_ports(root), [9091])
        self.assertEqual(self.disabled_ports(root), [TEMPLATE_PORT],
                         "a disabled sampler must not be touched")
        self.assertEqual(self.template.read_bytes(), before, "the template must never be rewritten")

    def test_service_selects_the_port_for_any_container_name(self):
        for index, container in enumerate(("table_v3", "ai-bsocr-serving-7d8f9", "anything")):
            with self.subTest(container=container):
                root = self.generate(container=container, service="tableocr",
                                     out=self.base / ("run-s-" + str(index)))
                self.assertEqual(self.enabled_ports(root), [56010])

    def test_no_service_leaves_the_jmx_port_untouched(self):
        # Library callers that do not care about ports keep the template value;
        # the runner still refuses to start without --service.
        root = self.generate(service=None)
        self.assertEqual(self.enabled_ports(root), [TEMPLATE_PORT])

    def test_unknown_service_is_an_error_not_a_fallback(self):
        with self.assertRaisesRegex(ValueError, "没有服务名"):
            self.generate(service="nosuch")

    def test_explicit_port_wins_over_the_service_lookup(self):
        root = self.generate(port=7003, service="bsocr", out=self.base / "run-p")
        self.assertEqual(self.enabled_ports(root), [7003])

    def test_missing_ports_config_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "端口配置不存在"):
            self.generate(config=self.base / "absent.json", service="fs")

    def test_invalid_ports_in_the_config_are_rejected(self):
        for value in (0, 65536, -1, "abc", ""):
            with self.subTest(value=value):
                self.write_config({"fs": value})
                with self.assertRaisesRegex(ValueError, "端口"):
                    self.generate(service="fs")

    def test_cli_rejects_invalid_port_and_missing_service(self):
        command = [sys.executable, "-B", str(ROOT / "prepare_run.py"),
                   "--template", str(self.template), "--run-dir", str(self.out),
                   "--container", "svc-a", "--threads", "1", "--duration", "2",
                   "--ports-config", str(self.config)]
        bad_port = subprocess.run(command + ["--port", "0"], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertNotEqual(bad_port.returncode, 0)
        self.assertIn("端口", bad_port.stderr)
        no_service = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=20)
        self.assertNotEqual(no_service.returncode, 0)
        self.assertIn("--service", no_service.stderr)
        self.assertFalse((self.out / "plan.jmx").exists())

    def test_runner_requires_service_before_creating_output(self):
        output = self.base / "results"
        result = subprocess.run(["bash", str(ROOT / "run_test.sh"), str(self.template),
                                 str(output), "svc-a", "1", "2"],
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--service 必传", result.stdout)
        self.assertFalse(output.exists(), "a rejected run must not create the result tree")

    def test_runner_rejects_a_bad_port_before_creating_output(self):
        output = self.base / "results-port"
        result = subprocess.run(["bash", str(ROOT / "run_test.sh"), str(self.template),
                                 str(output), "svc-a", "1", "2", "--service", "fs",
                                 "--port", "0"],
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("端口必须是", result.stdout)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
