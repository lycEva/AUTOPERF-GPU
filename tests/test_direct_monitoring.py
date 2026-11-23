"""Direct-monitoring black-box contracts for the NVIDIA (nvidia-smi) tool.

Run: python3 -B -m unittest discover -s tests -p 'test_*.py' -v
All entrypoints run against temporary data and mocked docker/nvidia-smi/jmeter:
no real container, GPU or business traffic is touched.

The contracts are the same as the DCU/ROCm tool; only the GPU probe changes:
  docker top                        -> container PIDs (+ RSS)
  nvidia-smi --query-compute-apps   -> PID, gpu_uuid, used_memory (MiB)
  nvidia-smi --query-gpu            -> gpu_uuid, memory.total (MiB)
VRAM % is derived as used_memory / memory.total * 100 per (pid, card).
"""
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ("timestamp", "elapsed_s", "container", "cpu_pct", "vram_mib",
           "vram_pct", "memory_gib", "status", "error", "sample_duration_s")
GPU_A = "GPU-11111111-1111-1111-1111-111111111111"
GPU_B = "GPU-22222222-2222-2222-2222-222222222222"
GPU_INDEX = {GPU_A: 0, GPU_B: 1}
# pid -> (uuid, used_mib, pmon sm token, rss_kib)
# `-` is pmon's idle marker and must count as a real 0 %, not as missing data.
GPU_PROCS = {111: (GPU_A, 101, "11", 524288),
             112: (GPU_A, 7, "-", 524288),
             222: (GPU_B, 202, "22", 2097152),
             999: (GPU_A, 9999, "99", 0)}
CONTAINER_PIDS = {"worker-a": (111, 112, 333), "worker-b": (222, 333)}
FOREIGN_PID = 333


class DirectMonitoringTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gpu-direct-contract-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        for name in ("run_test.sh", "apply_server_config.sh", "update_server_config.sh",
                     "update_jmx_pid.py", "prepare_run.py", "ports.json"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, self.project / name)
        for name in ("summary_monitor", "tests"):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.project / name,
                                ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.container = self.base / "container"
        self.container.mkdir()
        self.events = self.base / "events.jsonl"
        self.output = self.base / "results"
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        PROC_ROOT=str(self.proc), MOCK_STATE=str(self.state), MOCK_CONTAINER_ROOT=str(self.container),
                        MOCK_EVENTS=str(self.events), TMPDIR=str(self.base),
                        PYTHONDONTWRITEBYTECODE="1", MONITOR_MODE="direct")
        for key in ("MOCK_DOCKER_MODE", "MOCK_GPU_MODE", "MOCK_DELAY", "MOCK_JMETER_FAIL",
                    "MOCK_JMETER_BARRIER", "MOCK_JMETER_SECONDS", "MOCK_ROOT",
                    "MOCK_TREE_CPU", "MOCK_STRAY_CPU",
                    "MOCK_DOCKER_RESTART_FAIL"):
            self.env.pop(key, None)
        for pid in (111, 112, 222):
            self.write_stat(pid, 20)
        # One mock program serves both external tools; per-command counters are
        # locked, including when two collectors run at the same time.
        mock = '''
            import fcntl, json, os, pathlib, shutil, subprocess, sys, time
            PROCS = __GPU_PROCS__
            INDEX = __GPU_INDEX__
            name = pathlib.Path(sys.argv[0]).name
            args = sys.argv[1:]
            kwargs = {}
            for arg in args:
                if arg.startswith("--query-compute-apps"):
                    kwargs["kind"] = "apps"
                elif arg == "pmon":
                    kwargs["kind"] = "pmon"
            state = pathlib.Path(os.environ["MOCK_STATE"])
            def event(**extra):
                value = dict(command=name, argv=args, pid=os.getpid(), time=time.monotonic())
                value.update(extra)
                fd = os.open(os.environ["MOCK_EVENTS"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.write(fd, (json.dumps(value) + "\\n").encode())
                os.close(fd)
            if name == "docker":
                key, mode = "docker", os.environ.get("MOCK_DOCKER_MODE", "ok")
            else:
                key = "nvidia-smi-" + kwargs.get("kind", "?")
                mode = os.environ.get("MOCK_GPU_MODE", "ok")
            with open(state / (key + ".count"), "a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.seek(0)
                count = int(stream.read() or "0") + 1
                stream.seek(0); stream.truncate(); stream.write(str(count)); stream.flush()
            event(stage="start", count=count, kind=kwargs.get("kind"))
            if mode == "hang" or (mode == "hang_after_first" and count > 1):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                event(stage="child", child=child.pid)
                time.sleep(60)
            time.sleep(float(os.environ.get("MOCK_DELAY", "0")))
            if mode == "fail" or (mode == "fail_after_first" and count > 1):
                print("mock service failure", file=sys.stderr)
                sys.exit(7)
            if name == "docker" and args[:1] in (["exec"], ["cp"], ["restart"]):
                root = pathlib.Path(os.environ["MOCK_CONTAINER_ROOT"])
                if args[0] == "exec":
                    if len(args) != 5 or args[2:4] != ["mkdir", "-p"]:
                        sys.exit("unexpected docker exec: " + " ".join(args))
                    (root / args[4].strip("/")).mkdir(parents=True, exist_ok=True)
                elif args[0] == "cp":
                    if len(args) != 3:
                        sys.exit("unexpected docker cp: " + " ".join(args))
                    container, _, remote = args[2].partition(":")
                    if not container:
                        sys.exit("docker cp destination must be CONTAINER:PATH")
                    target = root / remote.strip("/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(args[1], target)
                else:
                    if len(args) != 2:
                        sys.exit("unexpected docker restart: " + " ".join(args))
                    if os.environ.get("MOCK_DOCKER_RESTART_FAIL"):
                        print("mock restart failure", file=sys.stderr)
                        sys.exit(5)
                event(stage="end", count=count)
                sys.exit(0)
            if name == "docker":
                if len(args) != 4 or args[0] != "top" or args[2:] != ["-eo", "pid,ppid,rss"]:
                    sys.exit("expected exactly docker top NAME -eo pid,ppid,rss")
                if args[1] == "worker-b":
                    pids = (222, 333)
                elif args[1] == "worker-empty":
                    # Alive container, but no process of it holds GPU memory.
                    pids = (333,)
                else:
                    pids = (111, 112, 333)
                pids = (100, 900) + pids  # 100 = init/root, 900 = stray docker-exec shell
                print("PID PPID RSS")
                for pid in pids:
                    fields = ["S"] + ["0"] * 49
                    stray_only = os.environ.get("MOCK_STRAY_CPU") == "1"
                    if pid == 900:
                        ticks = 20 + 2000 * count if stray_only else 20
                    elif stray_only:
                        ticks = 20
                    elif os.environ.get("MOCK_TREE_CPU") == "1" and pid not in (333,):
                        ticks = 20
                    else:
                        ticks = 20 + 200 * count
                    fields[11], fields[12], fields[19] = str(ticks), "10", str(pid * 10)
                    folder = pathlib.Path(os.environ["PROC_ROOT"]) / str(pid)
                    if mode == "missing_proc_after_first" and count > 1:
                        try:
                            (folder / "stat").unlink()
                        except OSError:
                            pass
                    else:
                        folder.mkdir(exist_ok=True)
                        (folder / "stat").write_text(
                            str(pid) + " (worker with spaces) " + " ".join(fields) + "\\n")
                    rss = 0 if pid == 100 else (2097152 if pid == 333 else
                                                (2097152 if pid == 222 else 524288))
                    print(pid, 1 if pid in (100, 900) else 100, rss)
            elif kwargs.get("kind") == "apps":
                if mode == "unknown":
                    print("GPU process table schema changed; unsupported driver")
                else:
                    for pid, (uuid, used, sm) in PROCS.items():
                        print("%d, %s, %d" % (pid, uuid, used))
            else:
                if mode == "unknown":
                    print("pmon schema changed; unsupported driver")
                else:
                    print("# gpu   pid  type    sm    mem    enc    dec    jpg    ofa     fb   ccpm  command")
                    print("# Idx     #   C/G     %      %      %      %      %      %     MB     MB  name")
                    for pid, (uuid, used, sm) in PROCS.items():
                        print("%d %7d C %5s - - - - - %5d 0 mock-worker" % (INDEX[uuid], pid, sm, used))
            event(stage="end", count=count)
        '''
        mock = (mock.replace("__GPU_PROCS__", repr({pid: (uuid, used, sm)
                                                    for pid, (uuid, used, sm, _)
                                                    in GPU_PROCS.items()}))
                    .replace("__GPU_INDEX__", repr(dict(GPU_INDEX))))
        self.executable("docker", mock)
        self.executable("nvidia-smi", mock)
        self.executable("jmeter", 'raise SystemExit("JMETER override was ignored")')
        self.env["JMETER"] = str(self.executable("fake-jmeter", '''
            import json, os, pathlib, sys, time
            args = sys.argv[1:]
            def option(flag):
                return pathlib.Path(args[args.index(flag) + 1])
            run = option("-t").parent
            ready_files = list(run.rglob("ready.json"))
            if not ready_files or not all(json.loads(p.read_text())["ok"] for p in ready_files):
                sys.exit("JMeter started before successful monitoring readiness")
            if not option("-t").name == "plan.jmx":
                sys.exit("run copy must be named plan.jmx")
            state = pathlib.Path(os.environ["MOCK_STATE"])
            token = str(os.getpid())
            (state / (token + ".jmeter-entered")).write_text(json.dumps(dict(run=str(run), time=time.monotonic())))
            deadline = time.monotonic() + 6
            while len(list(state.glob("*.jmeter-entered"))) < int(os.environ.get("MOCK_JMETER_BARRIER", "1")):
                if time.monotonic() > deadline:
                    sys.exit("concurrent JMeter barrier timed out")
                time.sleep(.02)
            time.sleep(float(os.environ.get("MOCK_JMETER_SECONDS", ".7")))
            if os.environ.get("MOCK_JMETER_FAIL"):
                sys.exit(9)
            for flag in ("-l", "-j"):
                option(flag).write_text(token + "\\n")
            option("-o").mkdir()
            (option("-o") / "index.html").write_text(token)
        '''))
        self.template = self.base / "source.jmx"
        self.template.write_text(textwrap.dedent('''\
            <?xml version="1.0" encoding="UTF-8"?>
            <jmeterTestPlan version="1.2" properties="5.0" jmeter="5.4.1">
              <hashTree>
                <TestPlan testname="source-plan"/>
                <hashTree>
                  <ThreadGroup testname="tg">
                    <stringProp name="ThreadGroup.num_threads">1</stringProp>
                    <stringProp name="ThreadGroup.duration">1</stringProp>
                  </ThreadGroup>
                  <hashTree/>
                </hashTree>
              </hashTree>
            </jmeterTestPlan>
            '''))

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body).lstrip())
        path.chmod(0o755)
        return path

    def write_stat(self, pid, ticks):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        fields = ["S"] + ["0"] * 49
        fields[11], fields[12], fields[19] = str(ticks), "10", str(pid * 10)
        (folder / "stat").write_text(str(pid) + " (worker with spaces) " + " ".join(fields) + "\n")

    def collector_command(self, output, container="worker-a", duration=".8", timeout="2"):
        script = self.project / "summary_monitor" / "collect_metrics.py"
        self.assertTrue(script.is_file(), "collect_metrics.py implementation is not ready")
        args = [sys.executable, "-B", str(script), "--container", container,
                "--output-dir", str(output), "--interval", ".3", "--timeout", timeout]
        if duration is not None:
            args += ["--duration", duration]
        return args

    def run_collector(self, name="standalone", **kwargs):
        output = self.base / name
        result = subprocess.run(self.collector_command(output, **kwargs), cwd=str(self.base),
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        return result, output

    def rows(self, output):
        with (output / "metrics.csv").open(newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(tuple(reader.fieldnames), COLUMNS)
            rows = list(reader)
        self.assertTrue(rows)
        for row in rows:
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            self.assertGreaterEqual(float(row["elapsed_s"]), 0)
            self.assertGreaterEqual(float(row["sample_duration_s"]), 0)
        return rows

    def chart_svg(self, output, stem="cpu"):
        """The chart the PNG is rasterized from (legend/colours are checkable here)."""
        sys.path.insert(0, str(ROOT / "summary_monitor"))
        try:
            from plot_metrics import chart_svgs, load_rows
        finally:
            sys.path.pop(0)
        rows, containers = load_rows(output / "metrics.csv")
        charts = {name: svg for name, svg, _height in chart_svgs(rows, containers)}
        return charts[stem]

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def status(self, output):
        status = json.loads((output / "monitor_status.json").read_text())
        self.assertIsInstance(status, dict)
        rows = self.rows(output)
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        self.assertEqual(status["samples"], len(rows))
        self.assertEqual(status["statuses"], counts)
        failed = sum(row["status"] in ("error", "timeout", "partial") for row in rows)
        self.assertEqual(status["failed_samples"], failed)
        self.assertIs(status["healthy"], failed == 0)
        self.assertGreaterEqual(status["overruns"], 0)
        self.assertGreaterEqual(status["duration_s"], 0)
        return status

    def wait_for(self, predicate, process, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if process.poll() is not None:
                self.fail("process exited before expected event: " + process.communicate()[0])
            time.sleep(.02)
        self.fail("timed out waiting for mock event")

    def spawn(self, command):
        process = subprocess.Popen(command, cwd=str(self.base), env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def assert_no_mock_processes(self):
        # A killed orphan may briefly be a zombie awaiting reaping; zombies
        # cannot run or retain service resources and count as stopped.
        for event in self.records():
            for key in ("pid", "child"):
                if key not in event:
                    continue
                stat = Path("/proc") / str(event[key]) / "stat"
                if stat.exists():
                    self.assertEqual(stat.read_text().rsplit(")", 1)[1].split()[0], "Z",
                                     "mock subprocess survived cleanup: " + str(event))

    def runner_command(self, container="worker-a", options=()):
        return ["bash", str(self.project / "run_test.sh"), str(self.template),
                str(self.output), container, "2", "2", "--service", "fs", *options]

    def run_runner(self, **kwargs):
        return subprocess.run(self.runner_command(**kwargs), cwd=str(self.base), env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=40)

    def run_dirs(self):
        return sorted(path for path in self.output.iterdir() if path.is_dir())

    # ---------------------------------------------------------------- collector

    def test_collector_units_pid_intersection_cpu_warmup_and_single_calls(self):
        result, output = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["cpu_pct"], "")
        self.assertTrue(any(float(row["cpu_pct"]) > 100 for row in rows[1:] if row["cpu_pct"]))
        for row in rows:
            self.assertEqual(row["container"], "worker-a")
            # 222 and 999 hold GPU memory but are not container PIDs.
            # Memory is the whole service tree (100 + 111 + 112 + 333 = 3 GiB),
            # not only the GPU holders (1 GiB).
            self.assertAlmostEqual(float(row["memory_gib"]), 3)
            self.assertAlmostEqual(float(row["vram_mib"]), 108)
            # 11 (pmon sm) + 0 (pmon `-`, an idle process) = 11, never 13
            # and never a memory share.
            self.assertAlmostEqual(float(row["vram_pct"]), 11)
        ready = json.loads((output / "ready.json").read_text())
        self.assertIs(ready["ok"], True)
        self.assertIsInstance(ready["error"], str)
        self.status(output)
        starts = [event for event in self.records() if event["stage"] == "start"]
        self.assertEqual(sum(e["command"] == "docker" for e in starts), len(rows))
        self.assertEqual(sum(e["command"] == "nvidia-smi" for e in starts), 2 * len(rows))
        self.assert_no_mock_processes()

    def test_stray_exec_shell_outside_the_service_tree_is_not_counted(self):
        # PID 900 is listed by docker top but is its own root (a docker exec
        # shell, parent outside the container). Only it burns CPU here, so any
        # non-zero cpu_pct would mean the service tree was computed wrongly.
        self.env["MOCK_STRAY_CPU"] = "1"
        result, output = self.run_collector(duration=".5")
        self.assertEqual(result.returncode, 0, result.stdout)
        for row in self.rows(output):
            self.assertIn(row["cpu_pct"], ("", "0.0", "0"), row)
            self.assertAlmostEqual(float(row["vram_mib"]), 108)

    def test_cpu_counts_the_whole_process_tree_not_only_gpu_holders(self):
        # Only PID 333 (in the container but NOT holding GPU memory) burns CPU.
        self.env["MOCK_TREE_CPU"] = "1"
        result, output = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(any(float(row["cpu_pct"]) > 0 for row in rows[1:] if row["cpu_pct"]),
                        "CPU of a non-GPU container process must be counted")
        for row in rows:
            # GPU metrics still only cover the GPU-holding processes.
            self.assertAlmostEqual(float(row["vram_mib"]), 108)
            self.assertAlmostEqual(float(row["vram_pct"]), 11)

    def test_one_run_monitors_two_containers_with_shared_probes(self):
        result, output = self.run_collector(name="multi", container="worker-a,worker-b")
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertEqual({row["container"] for row in rows}, {"worker-a", "worker-b"})
        self.assertEqual(len(rows) % 2, 0, "every round writes one row per container")
        expect = {"worker-a": (108, 11, 3), "worker-b": (202, 22, 4)}
        for row in rows:
            vram, pct, mem = expect[row["container"]]
            self.assertAlmostEqual(float(row["vram_mib"]), vram)
            self.assertAlmostEqual(float(row["vram_pct"]), pct)
            self.assertAlmostEqual(float(row["memory_gib"]), mem)
        # Exactly one docker top per container per round: no duplicated GPU probes.
        starts = [r for r in self.records() if r["stage"] == "start"]
        self.assertEqual(sum(r["command"] == "docker" for r in starts), len(rows))
        self.assertEqual(sum(r["command"] == "nvidia-smi" for r in starts), 2 * len(rows) // 2)
        summary = json.loads((output / "plots" / "summary.json").read_text())
        self.assertEqual(sorted(summary["containers"]), ["worker-a", "worker-b"])
        svg = self.chart_svg(output)
        for name in ("worker-a", "worker-b"):
            self.assertIn(name, svg, "each container needs its own legend entry")
        # Two containers must be drawn in two distinct colours.
        self.assertIn('#2563eb', svg)
        self.assertIn('#d97706', svg)
        self.status(output)

    def test_vram_pct_is_pmon_sm_not_memory_share(self):
        result, output = self.run_collector(container="worker-b")
        self.assertEqual(result.returncode, 0, result.stdout)
        for row in self.rows(output):
            self.assertAlmostEqual(float(row["vram_mib"]), 202)
            # The pmon sm% of PID 222, NOT 202 MiB of a 4000 MiB card (5.05).
            self.assertAlmostEqual(float(row["vram_pct"]), 22)
            # Tree = 100 + 222 + 333 = 4 GiB.
            self.assertAlmostEqual(float(row["memory_gib"]), 4)

    def test_external_commands_run_in_parallel(self):
        self.env["MOCK_DELAY"] = ".3"
        result, output = self.run_collector(duration=".1", timeout=".6")
        self.assertEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        self.assertLess(float(row["sample_duration_s"]), .67)
        starts = [event for event in self.records() if event["stage"] == "start"]
        self.assertEqual(len(starts), 3)
        self.assertLess(max(e["time"] for e in starts) - min(e["time"] for e in starts), .2)

    def test_unrecognized_gpu_output_is_error_and_missing_not_zero(self):
        self.env["MOCK_GPU_MODE"] = "unknown"
        result, output = self.run_collector(duration=".1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        self.assertEqual(row["vram_mib"], "")
        self.assertEqual(row["vram_pct"], "")
        self.assertTrue(row["error"])
        ready = json.loads((output / "ready.json").read_text())
        self.assertIs(ready["ok"], False)
        self.assertTrue(ready["error"])
        self.status(output)

    def test_docker_failure_has_no_fabricated_memory_or_cpu(self):
        self.env["MOCK_DOCKER_MODE"] = "fail"
        result, output = self.run_collector(duration=".1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        for column in ("memory_gib", "cpu_pct", "vram_mib", "vram_pct"):
            self.assertEqual(row[column], "", column)
        self.assertTrue(row["error"])
        self.assertIs(json.loads((output / "ready.json").read_text())["ok"], False)
        self.status(output)

    def test_missing_proc_cpu_remains_missing_after_warmup(self):
        self.env["MOCK_DOCKER_MODE"] = "missing_proc_after_first"
        result, output = self.run_collector()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row["cpu_pct"] == "" for row in rows))
        self.assertTrue(any(row["error"] for row in rows[1:]))
        self.assertIs(json.loads((output / "ready.json").read_text())["ok"], True)
        self.status(output)

    def test_no_gpu_processes_is_zero_not_error(self):
        result, output = self.run_collector(container="worker-empty")
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        # worker-empty has no GPU PID -> documented zero reading, not a failure,
        # and CPU still comes from its main process tree.
        for row in rows:
            self.assertEqual(row["status"], "no_gpu_processes")
            self.assertEqual(float(row["vram_mib"]), 0)
            self.assertEqual(float(row["vram_pct"]), 0)
        self.assertTrue(any(row["cpu_pct"] for row in rows[1:]))
        self.status(output)

    def test_shared_deadline_kills_all_command_process_groups(self):
        self.env.update(MOCK_DOCKER_MODE="hang", MOCK_GPU_MODE="hang")
        started = time.monotonic()
        result, output = self.run_collector(duration=".1", timeout=".4")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertLess(time.monotonic() - started, 20)
        row = self.rows(output)[0]
        self.assertLess(float(row["sample_duration_s"]), .9)
        for column in ("cpu_pct", "memory_gib", "vram_mib", "vram_pct"):
            self.assertEqual(row[column], "")
        self.assertTrue(row["error"])
        children = [record for record in self.records() if record["stage"] == "child"]
        self.assertEqual(len(children), 3, "all three mock command trees must really have started")
        self.assert_no_mock_processes()
        self.status(output)

    def test_standalone_term_flushes_csv_and_final_status(self):
        output = self.base / "signal-output"
        process = self.spawn(self.collector_command(output, duration=None))
        self.wait_for(lambda: (output / "ready.json").exists(), process)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout)
        self.rows(output)
        self.status(output)
        self.assert_no_mock_processes()

    def test_term_during_sample_kills_inflight_command_trees(self):
        self.env.update(MOCK_DOCKER_MODE="hang_after_first", MOCK_GPU_MODE="hang_after_first")
        output = self.base / "inflight-term"
        process = self.spawn(self.collector_command(output, duration=None, timeout="10"))
        self.wait_for(lambda: len([r for r in self.records() if r["stage"] == "child"]) == 3, process)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout)
        self.assertEqual(len(self.rows(output)), 1)
        self.status(output)
        self.assert_no_mock_processes()

    # ------------------------------------------------------------------- runner

    def test_default_direct_uses_ready_keeps_template_and_generates_plots(self):
        before = self.template.read_bytes()
        self.env.pop("MONITOR_MODE", None)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.template.read_bytes(), before, "the template must never be rewritten")
        self.assertEqual(len(list(self.state.glob("*.jmeter-entered"))), 1)
        self.assertTrue((runs[0] / "result.jtl").is_file())
        metrics = list(runs[0].rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.rows(metrics[0].parent)
        self.status(metrics[0].parent)
        pngs = {path.name for path in (runs[0] / "plots").glob("*.png")}
        self.assertTrue({"cpu.png", "vram.png", "vram_utilization.png",
                         "memory.png", "overview.png"} <= pngs, pngs)
        self.assertFalse(list(runs[0].rglob("*.svg")), "SVG charts are no longer written")
        self.assert_no_mock_processes()

    def test_failed_preflight_prevents_jmeter(self):
        self.env["MOCK_DOCKER_MODE"] = "fail"
        result = self.run_runner(options=("--monitor-interval", ".3", "--monitor-timeout", ".5"))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(list(self.state.glob("*.jmeter-entered")))
        ready = list(self.output.rglob("ready.json"))
        self.assertEqual(len(ready), 1)
        self.assertIs(json.loads(ready[0].read_text())["ok"], False)
        self.assert_no_mock_processes()

    def test_monitor_failure_after_ready_is_not_a_successful_run(self):
        self.env["MOCK_GPU_MODE"] = "fail_after_first"
        result = self.run_runner(options=("--monitor-interval", ".3", "--monitor-timeout", ".5"))
        self.assertTrue(list(self.state.glob("*.jmeter-entered")), result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertTrue(metrics)
        rows = self.rows(metrics[0].parent)
        self.assertTrue(any(row["error"] for row in rows[1:]))
        self.assert_no_mock_processes()

    def test_two_container_runs_overlap_and_are_isolated(self):
        self.env["MOCK_JMETER_BARRIER"] = "2"
        processes = [self.spawn(self.runner_command(container))
                     for container in ("worker-a", "worker-b")]
        for process in processes:
            stdout, _ = process.communicate(timeout=40)
            self.assertEqual(process.returncode, 0, stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 2)
        contents = []
        for run in runs:
            container = "worker-b" if "worker-b" in run.name else "worker-a"
            metrics = list(run.rglob("metrics.csv"))
            self.assertEqual(len(metrics), 1)
            for row in self.rows(metrics[0].parent):
                self.assertEqual(row["container"], container)
                self.assertEqual(float(row["vram_mib"]), 202 if container == "worker-b" else 108)
                self.assertEqual(float(row["memory_gib"]), 4 if container == "worker-b" else 3)
            contents.append((run / "result.jtl").read_text())
        self.assertNotEqual(*contents)
        self.assert_no_mock_processes()

    def test_repeated_same_container_gets_distinct_runs(self):
        for _ in range(2):
            result = self.run_runner(options=("--monitor-interval", ".3"))
            self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 2)
        self.assertNotEqual((runs[0] / "result.jtl").read_text(), (runs[1] / "result.jtl").read_text())
        self.assert_no_mock_processes()

    def test_scenario1_two_unrelated_services_are_monitored_separately(self):
        """Two unrelated load tests must not share metrics or charts."""
        self.env["MOCK_JMETER_BARRIER"] = "2"
        results = {name: self.base / ("results-" + name) for name in ("worker-a", "worker-b")}
        processes = []
        for name, out in results.items():
            command = ["bash", str(self.project / "run_test.sh"), str(self.template),
                       str(out), name, "2", "2", "--service", "fs",
                       "--monitor-interval", ".3"]
            processes.append(self.spawn(command))
        for process in processes:
            stdout, _ = process.communicate(timeout=40)
            self.assertEqual(process.returncode, 0, stdout)
        for name, out in results.items():
            runs = sorted(path for path in out.iterdir() if path.is_dir())
            self.assertEqual(len(runs), 1, "each load test owns exactly one run directory")
            rows = self.rows(runs[0])
            self.assertEqual({row["container"] for row in rows}, {name},
                             "a run must only contain its own container")
            summary = json.loads((runs[0] / "plots" / "summary.json").read_text())
            self.assertEqual(summary["containers"], [name])
        self.assert_no_mock_processes()

    def test_scenario2_caller_and_callee_appear_in_one_chart_set(self):
        """A calls B: one load test, one run directory, both containers charted."""
        result = self.run_runner(options=("--monitor-containers", "worker-a,worker-b",
                                          "--monitor-interval", ".3"))
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        rows = self.rows(runs[0])
        self.assertEqual({row["container"] for row in rows}, {"worker-a", "worker-b"})
        self.assertEqual(len(rows) % 2, 0)
        summary = json.loads((runs[0] / "plots" / "summary.json").read_text())
        self.assertEqual(sorted(summary["containers"]), ["worker-a", "worker-b"])
        self.assert_no_mock_processes()

    def test_jmeter_failure_still_stops_collector_and_plots(self):
        self.env["MOCK_JMETER_FAIL"] = "1"
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(list(self.state.glob("*.jmeter-entered")), result.stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.status(metrics[0].parent)
        self.assertTrue(list(self.output.rglob("plots")))
        self.assertTrue(list(self.output.rglob("*.png")))
        self.assertFalse(list(self.output.rglob("*.svg")))
        self.assert_no_mock_processes()

    def test_runner_term_stops_children_finalizes_status_and_plots(self):
        self.env["MOCK_JMETER_SECONDS"] = "30"
        process = self.spawn(self.runner_command(options=("--monitor-interval", ".3")))
        self.wait_for(lambda: list(self.state.glob("*.jmeter-entered")), process)
        entered = list(self.state.glob("*.jmeter-entered"))
        jmeter_pid = int(entered[0].name.split(".")[0])
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=40)
        self.assertNotEqual(process.returncode, 0, stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.status(metrics[0].parent)
        self.assertTrue(list(self.output.rglob("*.png")))
        self.assertFalse(list(self.output.rglob("*.svg")))
        self.assertFalse(Path("/proc", str(jmeter_pid)).exists(), "fake JMeter survived runner TERM")
        self.assert_no_mock_processes()


    # ------------------------------------------- 改配置 + 压测：一条命令

    def listener_port(self):
        """A real listening port: the readiness probe must succeed for real."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self.addCleanup(sock.close)
        def accept_loop():
            while True:
                try:
                    connection, _ = sock.accept()
                except OSError:
                    return
                connection.close()
        threading.Thread(target=accept_loop, daemon=True).start()
        return sock.getsockname()[1]

    def free_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def write_server_config(self, workers=1, threads=1):
        path = self.base / "server_config.json"
        path.write_text(json.dumps({"workers": workers, "threads": threads,
                                    "keep": "untouched"}, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def config_options(self, port, config):
        return ("--server-config", str(config),
                "--config-workers", "2", "--config-threads", "4",
                "--port", str(port), "--config-settle", "0")

    def went_live(self):
        return [json.loads(path.read_text()) for path in self.state.glob("*.jmeter-entered")]

    def test_config_then_benchmark_runs_as_one_command(self):
        port = self.listener_port()
        config = self.write_server_config()
        result = self.run_runner(options=("--monitor-interval", ".3", "--config-wait", "20",
                                          *self.config_options(port, config),
                                          "--config-target-path",
                                          "/opt/app/conf/server_config.json"))
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        run = runs[0]

        change = json.loads((run / "config_change.json").read_text())
        self.assertEqual((change["workers"], change["threads"]), (2, 4))
        self.assertEqual(change["readiness"], "ok")
        self.assertEqual(change["readiness_port"], port)
        self.assertEqual(change["target_path"], "/opt/app/conf/server_config.json")

        copied = json.loads((self.container / "opt/app/conf/server_config.json").read_text())
        self.assertEqual((copied["workers"], copied["threads"]), (2, 4))
        snapshot = json.loads((run / "server_config.json").read_text())
        self.assertEqual(snapshot["keep"], "untouched")

        # 施压发生在容器重启之后，而不是并行或之前
        live = self.went_live()
        self.assertEqual(len(live), 1, result.stdout)
        restarts = [record for record in self.records()
                    if record["argv"][:1] == ["restart"] and record["stage"] == "end"]
        self.assertTrue(restarts, "container was never restarted")
        self.assertLess(max(record["time"] for record in restarts), live[0]["time"])
        self.assert_no_mock_processes()

    def test_config_failure_aborts_before_any_load(self):
        self.env["MOCK_DOCKER_RESTART_FAIL"] = "1"
        port = self.listener_port()
        config = self.write_server_config()
        result = self.run_runner(options=(*self.config_options(port, config),
                                          "--config-wait", "5"))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.went_live(), [], "JMeter must not start when the config step failed")
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        self.assertFalse((runs[0] / "plan.jmx").exists(),
                         "no run copy may be generated after a failed config step")
        self.assert_no_mock_processes()

    def test_unready_service_aborts_before_any_load(self):
        port = self.free_port()
        config = self.write_server_config()
        result = self.run_runner(options=(*self.config_options(port, config),
                                          "--config-wait", "1"))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.went_live(), [], "JMeter must not start against an unready service")
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        change = json.loads((runs[0] / "config_change.json").read_text())
        self.assertEqual(change["readiness"], "timeout")
        self.assertIn("未", result.stdout)
        self.assertFalse((runs[0] / "plan.jmx").exists())
        self.assert_no_mock_processes()

    def test_incomplete_config_options_are_rejected_before_anything_runs(self):
        result = self.run_runner(options=("--config-workers", "2"))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("--server-config", result.stdout)
        self.assertFalse(self.output.exists(),
                         "a half-specified config step must not create a result directory")
        self.assertEqual(self.went_live(), [])



if __name__ == "__main__":
    unittest.main()
