#!/usr/bin/env python3
"""Generate an isolated JMeter plan for a direct-monitored GPU stress run.

The generated plan is a copy of the template for one run directory with:

* thread count / duration updated in place;
* CSV and included-plan paths made absolute, so moving the plan cannot change
  which data is read;
* when a warmup is requested, a measurement-window "surgery": the single
  enabled ThreadGroup loops forever and its scheduler stops it exactly after
  ``warmup + duration``, while two JSR223 elements publish the measurement
  window once and drop every sample that started before it.

This tool no longer talks to PerfMon (the GPU plan has no PerfMon listener), so
``monitor_mode`` only accepts ``direct``.
"""
import argparse
import base64
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent

PORTS_CONFIG = "ports.json"

MARKER_NAME = "measurement_window.json"
MARKER_TEMP_PREFIX = ".measurement_window."
SCHEDULER_PROP = "ThreadGroup.scheduler"
DURATION_PROP = "ThreadGroup.duration"
THREADS_PROP = "ThreadGroup.num_threads"
MAIN_CONTROLLER = "ThreadGroup.main_controller"
LOOPS_PROP = "LoopController.loops"

# Element kinds that may appear under a ThreadGroup without changing the linear
# "sampler runs once per iteration" shape the warmup contract relies on.
CONFIG_SUFFIXES = ("Manager", "Defaults", "DataSet", "Configuration", "Extractor")
PROCESSOR_SUFFIXES = ("PreProcessor", "PostProcessor", "Processor", "Assertion",
                      "Timer", "Listener", "Collector")
EXTRA_SAFE_KINDS = {"TestAction", "DebugSampler", "JavaSampler", "JSR223Sampler",
                    "BeanShellSampler", "BeanShellPostProcessor"}


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def non_negative_int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是非负整数")
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


# --------------------------------------------------------------------------
# Thread count / duration (formerly update_data.py, inlined for this tool)
# --------------------------------------------------------------------------
def update_threads(root, threads, target_tg=None):
    for group in root.iter("ThreadGroup"):
        if target_tg and group.get("testname") != target_tg:
            continue
        prop = group.find('stringProp[@name="' + THREADS_PROP + '"]')
        if prop is not None:
            print("线程数: " + str(prop.text) + " → " + str(threads))
            prop.text = str(threads)


def update_duration(root, duration, target_tg=None):
    for group in root.iter("ThreadGroup"):
        if target_tg and group.get("testname") != target_tg:
            continue
        prop = group.find('stringProp[@name="' + DURATION_PROP + '"]')
        if prop is not None:
            print("压测时间: " + str(prop.text) + " → " + str(duration))
            prop.text = str(duration)


# --------------------------------------------------------------------------
# Plan walking helpers
# --------------------------------------------------------------------------
def _enabled(node):
    return str(node.get("enabled", "true")).strip().lower() != "false"


def _is_thread_group(node):
    return node.tag.endswith("ThreadGroup")


def _pairs(hash_tree):
    """Yield (element, its own hashTree) for every JMeter pair in a hashTree."""
    children = list(hash_tree)
    position = 0
    while position < len(children):
        element = children[position]
        if element.tag == "hashTree":
            position += 1
            continue
        own = None
        if position + 1 < len(children) and children[position + 1].tag == "hashTree":
            own = children[position + 1]
            position += 1
        yield element, own
        position += 1


def _plan_tree(root):
    top = root.find("hashTree")
    if top is None:
        raise ValueError("无法识别的执行类型: 缺少 hashTree")
    for element, own in _pairs(top):
        if element.tag == "TestPlan":
            if own is None:
                raise ValueError("无法识别的执行类型: TestPlan 缺少 hashTree")
            return own
    raise ValueError("无法识别的执行类型: 缺少 TestPlan")


def _execution_kind(tag):
    if tag.endswith("Controller"):
        return "controller"
    if "Sampler" in tag:
        return "sampler"
    if tag.endswith(CONFIG_SUFFIXES):
        return "config"
    if tag.endswith(PROCESSOR_SUFFIXES):
        return "processor"
    if tag in EXTRA_SAFE_KINDS:
        return "sampler" if "Sampler" in tag else "config"
    return None


def _check_execution_tree(hash_tree):
    for element, own in _pairs(hash_tree):
        kind = _execution_kind(element.tag)
        if kind == "controller":
            raise ValueError("预热只支持线性执行，不支持控制器: " + element.tag)
        if kind is None:
            raise ValueError("无法识别的执行类型: " + element.tag)
        if own is not None:
            _check_execution_tree(own)


def _check_plan_level(plan_tree):
    for element, _own in _pairs(plan_tree):
        if not _is_thread_group(element):
            raise ValueError("无法识别的执行类型: " + element.tag)


def _reject_serial(root):
    for plan in root.iter("TestPlan"):
        flag = plan.find('boolProp[@name="TestPlan.serialize_threadgroups"]')
        if flag is not None and str(flag.text).strip().lower() == "true":
            raise ValueError("预热要求线程组并行执行，不支持串行计划")


def _samplers_under(hash_tree, found):
    for element, own in _pairs(hash_tree):
        if "Sampler" in element.tag and _enabled(element):
            found.append(element)
        if own is not None:
            _samplers_under(own, found)
    return found


def _warmup_target(root):
    """Validate the plan for the warmup surgery and return (group, its tree)."""
    plan_tree = _plan_tree(root)
    _check_plan_level(plan_tree)
    groups = [(element, own) for element, own in _pairs(plan_tree)
              if _is_thread_group(element) and _enabled(element)]
    if len(groups) != 1:
        raise ValueError("预热要求恰好一个启用的线程组（当前 " + str(len(groups)) + " 个）")
    group, group_tree = groups[0]
    if group.tag != "ThreadGroup":
        raise ValueError("预热只支持普通 ThreadGroup，不支持 " + group.tag)
    _reject_serial(root)
    controller = group.find('elementProp[@name="' + MAIN_CONTROLLER + '"]')
    if controller is None or controller.get("elementType") != "LoopController":
        raise ValueError("预热要求线程组主控制器为 LoopController")
    if group_tree is None:
        raise ValueError("预热要求线程组至少包含一个启用的采样器")
    samplers = _samplers_under(group_tree, [])
    if not samplers:
        raise ValueError("预热要求线程组至少包含一个启用的采样器")
    _check_execution_tree(group_tree)
    return group, group_tree, samplers


# --------------------------------------------------------------------------
# Scripts for the injected JSR223 elements
# --------------------------------------------------------------------------
PRE_SCRIPT = """import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.Base64

long warmup_ms = __WARMUP__L * 1000L
long duration_ms = __DURATION__L * 1000L

synchronized (props) {
    if (props.get('measurement_window_published') == null) {
        double load_started_epoch_s = System.currentTimeMillis() / 1000.0d
        double warmup_s = warmup_ms / 1000.0d
        double duration_s = duration_ms / 1000.0d
        double measurement_start_epoch_s = load_started_epoch_s + warmup_s
        double measurement_end_epoch_s = measurement_start_epoch_s + duration_s
        String target_dir = new String(Base64.decoder.decode('__PATH__'), 'UTF-8')
        File target = new File(target_dir, 'measurement_window.json')
        File temp = new File(target_dir, '.measurement_window.' + System.nanoTime() + '.tmp')
        String json = '{"load_started_epoch_s": ' + String.valueOf(load_started_epoch_s) +
            ', "measurement_start_epoch_s": ' + String.valueOf(measurement_start_epoch_s) +
            ', "measurement_end_epoch_s": ' + String.valueOf(measurement_end_epoch_s) +
            ', "warmup_s": ' + String.valueOf(warmup_s) +
            ', "duration_s": ' + String.valueOf(duration_s) + '}'
        try {
            temp.setText(json, 'UTF-8')
            Files.move(temp.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE)
        } catch (Throwable failure) {
            if (temp.exists()) { temp.delete() }
            log.error('Cannot establish measurement window: ' + failure)
            // A run without a window is untrustworthy: stop it right here and
            // skip the current sample instead of publishing business traffic.
            throw new org.apache.jorphan.util.JMeterStopTestNowException()
        }
        props.put('measurement_start_epoch_s', String.valueOf(measurement_start_epoch_s))
        props.put('measurement_window_published', 'true')
    }
}
"""

POST_SCRIPT = """if (prev != null && props.get('measurement_start_epoch_s') != null) {
    double measurement_start_epoch_s = Double.parseDouble(
        String.valueOf(props.get('measurement_start_epoch_s')))
    if (prev.getStartTime() < measurement_start_epoch_s * 1000.0d) {
        prev.setIgnore()
    }
}
"""


def _pre_script(warmup, duration, run_dir):
    encoded = base64.b64encode(str(run_dir).encode("utf-8")).decode("ascii")
    script = PRE_SCRIPT.replace("__WARMUP__", str(warmup))
    script = script.replace("__DURATION__", str(duration))
    return script.replace("__PATH__", encoded)


def _jsr223(tag, testname, script):
    element = ET.Element(tag, guiclass="TestBeanGUI", testclass=tag,
                         testname=testname, enabled="true")
    for name, value in (("scriptLanguage", "groovy"), ("parameters", ""),
                        ("filename", ""), ("cacheKey", "true"), ("script", script)):
        prop = ET.SubElement(element, "stringProp", name=name)
        prop.text = value
    return element


def _inject_processors(root, pre_script, post_script):
    """Attach one publisher/filter pair to every enabled sampler."""
    targets = []
    for parent in list(root.iter("hashTree")):
        for element in list(parent):
            if element.tag == "hashTree" or "Sampler" not in element.tag:
                continue
            if _enabled(element):
                targets.append((parent, element))
    for parent, sampler in targets:
        children = list(parent)
        index = children.index(sampler)
        if index + 1 < len(children) and children[index + 1].tag == "hashTree":
            tree = children[index + 1]
        else:
            tree = ET.Element("hashTree")
            parent.insert(index + 1, tree)
        tree.append(_jsr223("JSR223PreProcessor", "测量窗口发布", pre_script))
        ET.SubElement(tree, "hashTree")
        tree.append(_jsr223("JSR223PostProcessor", "测量窗口过滤", post_script))
        ET.SubElement(tree, "hashTree")
    return len(targets)


# --------------------------------------------------------------------------
# Preparation
# --------------------------------------------------------------------------
def _set_string_prop(parent, name, value):
    prop = parent.find('stringProp[@name="' + name + '"]')
    if prop is None:
        prop = ET.SubElement(parent, "stringProp", name=name)
    prop.text = str(value)
    return prop


def _absolutize_paths(root, template):
    # JMeter resolves these paths relative to the plan; moving the plan must not
    # change which CSV data or included plan is read. Expressions remain intact.
    for element in root.iter():
        names = []
        if element.tag == "CSVDataSet":
            names = ["filename"]
        elif element.tag == "IncludeController":
            names = ["IncludeController.includepath"]
        for name in names:
            prop = element.find('stringProp[@name="' + name + '"]')
            if prop is not None and prop.text and "${" not in prop.text:
                path = Path(prop.text)
                if not path.is_absolute():
                    prop.text = str((template.parent / path).resolve())


def _valid_port(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("端口必须是 1-65535 之间的整数: " + str(value))
    if not 1 <= number <= 65535:
        raise ValueError("端口必须是 1-65535 之间的整数: " + str(value))
    return number


def port_number(value):
    try:
        return _valid_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def load_ports_config(path):
    """Read the flat service -> port map, e.g. {"fs": 58096, "bsocr": 58028}."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("端口配置不存在: " + str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("端口配置读取失败: {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise ValueError("端口配置必须是 JSON 对象: " + str(path))
    # 兼容旧格式 {"services": {...}}
    if isinstance(data.get("services"), dict):
        data = data["services"]
    ports = {}
    for name, value in data.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("端口配置的服务名必须是非空字符串: " + str(path))
        ports[name.strip()] = _valid_port(value)
    return ports


def resolve_port(container, port, config, service=None):
    """--port > ports[服务名]; 没给服务名就不改端口，查不到则报错。

    The runner makes ``--service`` mandatory, so this only returns None for
    direct library callers that do not care about the port at all.
    """
    if port is not None:
        return _valid_port(port), "--port"
    if not service:
        return None, "未配置"
    if service not in config:
        raise ValueError("ports.json 里没有服务名 '{}' 的端口配置".format(service))
    return config[service], "ports.json " + service


def apply_port(root, port):
    """Rewrite the port of every enabled HTTP sampler; disabled ones stay put."""
    changed = 0
    for sampler in root.iter("HTTPSamplerProxy"):
        if not _enabled(sampler):
            continue
        _set_string_prop(sampler, "HTTPSampler.port", port)
        changed += 1
    return changed


def prepare(template, run_dir, container, threads, duration, monitor_mode='direct',
            warmup=0, port=None, ports_config=None, service=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ValueError("容器名只能包含字母、数字、下划线、点、横线，且以字母或数字开头")
    if threads <= 0 or duration <= 0:
        raise ValueError("线程数和持续时间必须是正整数")
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup 必须是非负整数")
    if monitor_mode not in ('direct',):
        raise ValueError("monitor_mode 只能是 direct（本机 JMX 已移除 PerfMon 监听器）")
    template = Path(template).resolve(strict=True)
    run_dir = Path(run_dir).resolve()
    if template == run_dir / "plan.jmx":
        raise ValueError("运行副本不能覆盖原始模板")

    tree = ET.parse(template)
    root = tree.getroot()

    group = None
    if warmup:
        group, _group_tree, _samplers = _warmup_target(root)

    update_threads(root, threads)
    for each in root.iter("ThreadGroup"):
        scheduler = each.find('boolProp[@name="' + SCHEDULER_PROP + '"]')
        if scheduler is None:
            scheduler = ET.SubElement(each, "boolProp", name=SCHEDULER_PROP)
        scheduler.text = "true"

    if warmup:
        _set_string_prop(group, THREADS_PROP, threads)
        controller = group.find('elementProp[@name="' + MAIN_CONTROLLER + '"]')
        loops = controller.find('intProp[@name="' + LOOPS_PROP + '"]')
        if loops is None:
            loops = ET.SubElement(controller, "intProp", name=LOOPS_PROP)
        loops.text = "-1"
        _set_string_prop(group, DURATION_PROP, warmup + duration)
        _inject_processors(root, _pre_script(warmup, duration, run_dir), POST_SCRIPT)
    else:
        update_duration(root, duration)

    _absolutize_paths(root, template)

    # 服务端口：--port 临时覆盖；否则用 --service 给的服务名查 ports.json；
    # 没给 --service 时按 ai-<服务名>-serving 的容器命名约定取服务名。
    port_value, port_source = resolve_port(
        container, port, load_ports_config(ports_config or (BASE_DIR / PORTS_CONFIG)),
        service=service)
    if port_value is None:
        print("ℹ️ 端口未配置，JMX 端口保持不变")
    else:
        changed = apply_port(root, port_value)
        print("✅ JMX 端口 -> {}（来源: {}，改写 {} 个启用采样器）".format(
            port_value, port_source, changed))

    # Preparation only targets a fresh run directory. Never rewrite an active run.
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "plan.jmx").exists() or (run_dir / MARKER_NAME).exists():
        raise ValueError("运行目录已有配置，请使用新的运行目录")
    tree.write(str(run_dir / "plan.jmx"), encoding="utf-8", xml_declaration=True)
    if warmup:
        print("✅ 独立配置已生成，容器: " + container + "，预热: " + str(warmup)
              + " 秒，测量窗口: " + str(duration) + " 秒")
    else:
        print("✅ 独立配置已生成，容器: " + container + "，预热: 未启用")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--threads", required=True, type=positive_int)
    parser.add_argument("--duration", required=True, type=positive_int)
    parser.add_argument("--warmup", type=non_negative_int, default=0)
    parser.add_argument("--port", type=port_number, default=None,
                        help="覆盖 JMX 中的服务端口（1-65535），用于端口映射与默认端口不一致时")
    parser.add_argument("--service", default=None,
                        help="服务名（ports.json services 的 key）；不传则按 ai-<服务名>-serving 推导")
    parser.add_argument("--ports-config", default=None,
                        help="端口配置文件，默认脚本同目录下的 ports.json")
    args = parser.parse_args()
    if args.port is None and not args.service:
        parser.error("--service 必传（服务名，见 ports.json），或直接给 --port")
    try:
        prepare(args.template, args.run_dir, args.container, args.threads,
                args.duration, 'direct', args.warmup,
                port=args.port, ports_config=args.ports_config, service=args.service)
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.exit(1, "❌ 生成配置失败: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
