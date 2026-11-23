"""Warmup-plan contracts; synthetic JSR223 samplers only, never business traffic.

Default: python3 -B -m unittest discover -s tests -p 'test_warmup_plan.py' -v
Optional local JMeter: XNCS_TEST_REAL_JMETER=1 <same command>
"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_run import prepare


def prop(node, tag, name, value):
    element = ET.SubElement(node, tag, name=name)
    element.text = str(value)
    return element


def fixture(path, sampler_script="Thread.sleep(50); SampleResult.setResponseCode('200')"):
    root = ET.Element('jmeterTestPlan', version='1.2', properties='5.0', jmeter='5.4.1')
    top = ET.SubElement(root, 'hashTree')
    plan = ET.SubElement(top, 'TestPlan', guiclass='TestPlanGui', testclass='TestPlan',
                         testname='synthetic plan', enabled='true')
    prop(plan, 'boolProp', 'TestPlan.serialize_threadgroups', 'false')
    plan_tree = ET.SubElement(top, 'hashTree')
    group = ET.SubElement(plan_tree, 'ThreadGroup', guiclass='ThreadGroupGui',
                          testclass='ThreadGroup', testname='dummy group', enabled='true')
    prop(group, 'stringProp', 'ThreadGroup.on_sample_error', 'continue')
    main = ET.SubElement(group, 'elementProp', name='ThreadGroup.main_controller',
                         elementType='LoopController', guiclass='LoopControlPanel',
                         testclass='LoopController', enabled='true')
    prop(main, 'boolProp', 'LoopController.continue_forever', 'false')
    prop(main, 'intProp', 'LoopController.loops', '1')
    for name, value in (('num_threads', 1), ('duration', 9), ('ramp_time', 0), ('delay', 0)):
        prop(group, 'stringProp', 'ThreadGroup.' + name, value)
    prop(group, 'boolProp', 'ThreadGroup.scheduler', 'false')
    group_tree = ET.SubElement(plan_tree, 'hashTree')
    sampler = ET.SubElement(group_tree, 'JSR223Sampler', guiclass='TestBeanGUI',
                            testclass='JSR223Sampler', testname='dummy', enabled='true')
    for name, value in (('scriptLanguage', 'groovy'), ('cacheKey', 'true'),
                        ('parameters', ''), ('filename', ''), ('script', sampler_script)):
        prop(sampler, 'stringProp', name, value)
    sampler_tree = ET.SubElement(group_tree, 'hashTree')
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return root, plan_tree, group, group_tree, sampler, sampler_tree


class WarmupPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='gpu-warmup-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.template = self.base / 'source.jmx'
        self.parts = fixture(self.template)
        self.out = self.base / 'prepared'

    def generate(self, warmup=120, duration=600, monitor_mode='direct', out=None):
        with contextlib.redirect_stdout(io.StringIO()):
            prepare(self.template, out or self.out, 'dummy', 3, duration,
                    monitor_mode=monitor_mode, warmup=warmup)
        return ET.parse((out or self.out) / 'plan.jmx').getroot()

    def save(self):
        ET.ElementTree(self.parts[0]).write(self.template, encoding='utf-8')

    def test_120_plus_600_is_720_and_template_unchanged(self):
        before = self.template.read_bytes()
        root = self.generate()
        self.assertEqual(self.template.read_bytes(), before)
        group = root.find('.//ThreadGroup')
        self.assertEqual(group.find('stringProp[@name="ThreadGroup.duration"]').text, '720')
        self.assertEqual(group.find('stringProp[@name="ThreadGroup.num_threads"]').text, '3')
        self.assertEqual(group.find('.//intProp[@name="LoopController.loops"]').text, '-1')
        self.assertEqual(group.find('boolProp[@name="ThreadGroup.scheduler"]').text, 'true')
        pre = root.find('.//JSR223PreProcessor/stringProp[@name="script"]').text
        post = root.find('.//JSR223PostProcessor/stringProp[@name="script"]').text
        for token in ('120L * 1000L', '600L * 1000L', 'synchronized (props)',
                      'ATOMIC_MOVE', 'load_started_epoch_s',
                      'measurement_start_epoch_s', 'measurement_end_epoch_s',
                      'warmup_s', 'duration_s'):
            self.assertIn(token, pre)
        self.assertIn('prev.getStartTime()', post)
        self.assertIn('prev.setIgnore()', post)
        self.assertFalse((self.out / 'measurement_window.json').exists(),
                         'Python preparation must not establish load start')

    def test_zero_is_identical_to_old_default_and_allows_multiple_groups(self):
        plan_tree, group = self.parts[1:3]
        plan_tree.extend((ET.fromstring(ET.tostring(group)), ET.Element('hashTree')))
        self.save()
        root = self.generate(warmup=0, duration=600)
        with contextlib.redirect_stdout(io.StringIO()):
            prepare(self.template, self.base / 'default', 'dummy', 3, 600,
                    monitor_mode='direct')
        self.assertEqual((self.out / 'plan.jmx').read_bytes(),
                         (self.base / 'default' / 'plan.jmx').read_bytes())
        self.assertEqual(len(root.findall('.//ThreadGroup')), 2)
        self.assertFalse(root.findall('.//JSR223PreProcessor'))
        self.assertFalse(root.findall('.//JSR223PostProcessor'))
        for group in root.findall('.//ThreadGroup'):
            self.assertEqual(group.find('stringProp[@name="ThreadGroup.duration"]').text, '600')
            self.assertEqual(group.find('.//intProp[@name="LoopController.loops"]').text, '1')

    def test_warmup_rejects_non_direct_mode_and_invalid_api_values(self):
        for value in (-1, 0.5, 1.0, True, '120', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '非负整数'):
                self.generate(warmup=value)
        with self.assertRaisesRegex(ValueError, 'direct'):
            self.generate(monitor_mode='perfmon')
        with self.assertRaisesRegex(ValueError, 'direct'):
            self.generate(monitor_mode='Direct')
        self.assertFalse(self.out.exists())

    def test_cli_rejects_invalid_warmup_and_defaults_to_zero(self):
        ports = self.base / 'ports.json'
        ports.write_text('{"dummy": 58096}', encoding='utf-8')
        command = [sys.executable, '-B', str(ROOT / 'prepare_run.py'), '--template',
                   str(self.template), '--run-dir', str(self.out), '--container', 'dummy',
                   '--threads', '1', '--duration', '2',
                   '--service', 'dummy', '--ports-config', str(ports)]
        for value in ('-1', '1.5', 'abc', '', '1e2'):
            with self.subTest(value=value):
                result = subprocess.run(command + ['--warmup', value], text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('warmup', result.stderr)
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(ET.parse(self.out / 'plan.jmx').findall('.//JSR223PreProcessor'))

    def test_rejects_multiple_enabled_groups_but_ignores_disabled_group(self):
        plan_tree, group = self.parts[1:3]
        other = ET.fromstring(ET.tostring(group))
        plan_tree.extend((other, ET.Element('hashTree')))
        self.save()
        with self.assertRaisesRegex(ValueError, '恰好一个'):
            self.generate()
        other.set('enabled', 'false')
        self.save()
        self.generate()

    def test_rejects_unsafe_controllers_and_custom_groups(self):
        for kind in ('TransactionController', 'IncludeController', 'ModuleController',
                     'com.example.CustomController'):
            with self.subTest(kind=kind):
                self.parts = fixture(self.template)
                node = ET.SubElement(self.parts[3], kind, enabled='true')
                if kind == 'TransactionController':
                    prop(node, 'boolProp', 'TransactionController.parent', 'true')
                ET.SubElement(self.parts[3], 'hashTree')
                self.save()
                with self.assertRaisesRegex(ValueError, '控制器'):
                    self.generate()
        for kind in ('SetupThreadGroup', 'PostThreadGroup',
                     'kg.apc.jmeter.threads.UltimateThreadGroup'):
            with self.subTest(kind=kind):
                self.parts = fixture(self.template)
                self.parts[2].tag = kind
                self.save()
                with self.assertRaisesRegex(ValueError, '普通 ThreadGroup'):
                    self.generate()

    def test_rejects_unrecognized_execution_elements(self):
        for kind in ('com.example.Workers', 'UnrecognizedExecutionElement'):
            with self.subTest(kind=kind):
                self.parts = fixture(self.template)
                ET.SubElement(self.parts[1], kind, enabled='true')
                ET.SubElement(self.parts[1], 'hashTree')
                self.save()
                with self.assertRaisesRegex(ValueError, '执行类型'):
                    self.generate()
        self.parts = fixture(self.template)
        ET.SubElement(self.parts[3], 'com.example.Workers', enabled='true')
        ET.SubElement(self.parts[3], 'hashTree')
        self.save()
        with self.assertRaisesRegex(ValueError, '执行类型'):
            self.generate()

    def test_rejects_serial_plan_no_sampler_and_wrong_main_controller(self):
        self.parts[0].find('.//boolProp[@name="TestPlan.serialize_threadgroups"]').text = 'true'
        self.save()
        with self.assertRaisesRegex(ValueError, '串行'):
            self.generate()
        self.parts = fixture(self.template)
        self.parts[4].set('enabled', 'false')
        self.save()
        with self.assertRaisesRegex(ValueError, '采样器'):
            self.generate()
        self.parts = fixture(self.template)
        self.parts[2].find('elementProp').set('elementType', 'OtherController')
        self.save()
        with self.assertRaisesRegex(ValueError, 'LoopController'):
            self.generate()

    def test_isolated_paths_and_existing_processor_order(self):
        for kind in ('JSR223PreProcessor', 'JSR223PostProcessor'):
            for name in ('first', 'second'):
                ET.SubElement(self.parts[5], kind, testname=name, enabled='true')
                ET.SubElement(self.parts[5], 'hashTree')
        self.save()
        odd = self.base / "space ' dollar $ slash \\ 中文"
        root = self.generate(out=odd)
        existing = [(node.tag, node.get('testname')) for node in root.iter()
                    if node.get('testname') in ('first', 'second')]
        self.assertEqual(existing, [('JSR223PreProcessor', 'first'),
                                    ('JSR223PreProcessor', 'second'),
                                    ('JSR223PostProcessor', 'first'),
                                    ('JSR223PostProcessor', 'second')])
        pre = root.find('.//JSR223PreProcessor/stringProp[@name="script"]').text
        self.assertNotIn(str(odd), pre)
        self.assertIn('Base64.decoder.decode', pre)
        other = self.generate(out=self.base / 'other')
        self.assertNotEqual(pre, other.find('.//JSR223PreProcessor/stringProp[@name="script"]').text)

    def test_does_not_overwrite_existing_marker_or_original_plan(self):
        self.out.mkdir()
        marker = self.out / 'measurement_window.json'
        marker.write_text('existing')
        with self.assertRaisesRegex(ValueError, '已有配置'):
            self.generate()
        self.assertEqual(marker.read_text(), 'existing')
        with self.assertRaisesRegex(ValueError, '原始模板'):
            self._original_plan()

    def _original_plan(self):
        plan = self.base / 'plan.jmx'
        shutil.copyfile(self.template, plan)
        prepare(plan, self.base, 'dummy', 1, 2, monitor_mode='direct', warmup=1)

    @unittest.skipUnless(os.environ.get('XNCS_TEST_REAL_JMETER') == '1',
                         'set XNCS_TEST_REAL_JMETER=1 for local synthetic JMeter verification')
    def test_real_jmeter_marker_failure_does_not_execute_sampler(self):
        jmeter = os.environ.get('JMETER') or shutil.which('jmeter')
        if not jmeter:
            self.skipTest('JMeter not installed')
        import base64
        sentinel = self.base / 'sampler-executed'
        encoded = base64.b64encode(str(sentinel).encode()).decode()
        fixture(self.template, "new File(new String(Base64.decoder.decode('%s'), 'UTF-8')).text = 'BAD'" % encoded)
        self.generate(warmup=1, duration=2)
        # A publication failure AFTER preparation must stop before doSampling.
        (self.out / 'measurement_window.json').mkdir()
        env = os.environ.copy()
        env.pop('JAVA_HOME', None)
        env.pop('JRE_HOME', None)
        result = subprocess.run([jmeter, '-n', '-t', str(self.out / 'plan.jmx'),
                                 '-l', str(self.out / 'result.jtl'),
                                 '-j', str(self.out / 'jmeter.log')],
                                cwd=str(self.base), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=40)
        # JMeter itself may return 0 on script errors. The integration wrapper
        # must additionally require a valid marker/completed measurement window.
        self.assertIn('Cannot establish measurement window',
                      (self.out / 'jmeter.log').read_text(), result.stdout)
        self.assertFalse(sentinel.exists(), 'PreProcessor failure must not run even the current sampler')
        self.assertFalse(list(self.out.glob('.measurement_window.*')))

    @unittest.skipUnless(os.environ.get('XNCS_TEST_REAL_JMETER') == '1',
                         'set XNCS_TEST_REAL_JMETER=1 for local synthetic JMeter verification')
    def test_real_jmeter_shared_window_and_start_time_filter(self):
        jmeter = os.environ.get('JMETER') or shutil.which('jmeter')
        if not jmeter:
            self.skipTest('JMeter not installed')
        # Every sampler writes an audit before filtering. One slow first request
        # straddles warmup end and MUST NOT appear in either JTL listener.
        audit = self.base / 'audit.csv'
        import base64
        path64 = base64.b64encode(str(audit).encode()).decode()
        script = '''
if (vars.get('dummy_seen') == null) {
    vars.put('dummy_seen', 'yes')
    Thread.sleep(1250)
} else { Thread.sleep(80) }
SampleResult.setResponseCode('200')
SampleResult.setSuccessful(true)
'''
        self.parts = fixture(self.template, script)
        existing_post = ET.SubElement(self.parts[5], 'JSR223PostProcessor',
                                     guiclass='TestBeanGUI', testclass='JSR223PostProcessor',
                                     testname='audit all samples', enabled='true')
        for name, value in (('scriptLanguage', 'groovy'), ('cacheKey', 'true'),
                            ('script', '''
synchronized (props) {
    def f = new File(new String(Base64.decoder.decode('%s'), 'UTF-8'))
    f.append(prev.getStartTime().toString() + ',' + prev.getEndTime().toString() + '\\n')
}
''' % path64)):
            prop(existing_post, 'stringProp', name, value)
        ET.SubElement(self.parts[5], 'hashTree')
        # Existing local aggregate listener gets the same filtered event stream.
        listener_path = self.base / 'aggregate.jtl'
        listener = ET.SubElement(self.parts[5], 'ResultCollector',
                                 guiclass='StatVisualizer', testclass='ResultCollector',
                                 testname='aggregate', enabled='true')
        prop(listener, 'stringProp', 'filename', listener_path)
        ET.SubElement(self.parts[5], 'hashTree')
        self.save()
        weird_out = self.base / "run $ ' 中文"
        self.generate(warmup=1, duration=2, out=weird_out)
        env = os.environ.copy()
        env.pop('JAVA_HOME', None)
        env.pop('JRE_HOME', None)
        started = time.monotonic()
        result = subprocess.run([jmeter, '-n', '-t', str(weird_out / 'plan.jmx'),
                                 '-l', str(weird_out / 'result.jtl'),
                                 '-j', str(weird_out / 'jmeter.log'),
                                 '-Jjmeter.save.saveservice.timestamp_format=ms',
                                 '-Jjmeter.save.saveservice.print_field_names=true'],
                                cwd=str(self.base), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout)
        log = (weird_out / 'jmeter.log').read_text()
        self.assertNotIn('ERROR', log, log)
        marker = json.loads((weird_out / 'measurement_window.json').read_text())
        self.assertEqual(set(marker), {'load_started_epoch_s', 'measurement_start_epoch_s',
                                      'measurement_end_epoch_s', 'warmup_s', 'duration_s'})
        self.assertAlmostEqual(marker['measurement_start_epoch_s'] - marker['load_started_epoch_s'], 1,
                               places=5)
        self.assertAlmostEqual(marker['measurement_end_epoch_s'] - marker['measurement_start_epoch_s'], 2,
                               places=5)
        self.assertAlmostEqual(marker['warmup_s'], 1, places=5)
        self.assertAlmostEqual(marker['duration_s'], 2, places=5)
        self.assertGreaterEqual(time.monotonic() - started, 3)
        lower = round(marker['measurement_start_epoch_s'] * 1000)
        upper = round(marker['measurement_end_epoch_s'] * 1000)
        with (weird_out / 'result.jtl').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreater(len(rows), 3)
        stamps = [int(row['timeStamp']) for row in rows]
        self.assertTrue(all(lower <= stamp < upper for stamp in stamps))
        # A finite template loop (loops=1) would leave exactly one sample per
        # thread; more rows prove loops=-1 and the scheduler actually ran the
        # warmup + measurement span. The window tail can stay empty because
        # JSR223 first-run compilation delays the published window.
        self.assertGreater(len(rows), 6, 'finite original loop must not stop early')
        all_samples = [tuple(map(int, line.split(','))) for line in audit.read_text().splitlines()]
        self.assertTrue(any(start < lower < end for start, end in all_samples),
                        'fixture must contain requests straddling warmup boundary')
        self.assertTrue(any(start < lower for start, _ in all_samples))
        self.assertGreater(max(end for _, end in all_samples), lower,
                           'sampling must continue past the warmup boundary')
        with listener_path.open() as stream:
            aggregate = list(csv.DictReader(stream))
        self.assertEqual(sorted(int(row['timeStamp']) for row in aggregate), sorted(stamps))
        self.assertFalse(list(weird_out.glob('.measurement_window.*')))


if __name__ == '__main__':
    unittest.main()
