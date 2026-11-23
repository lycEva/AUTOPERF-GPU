"""Config-stage contracts for apply_server_config.sh (改配置 → 复制 → 重启 → 等就绪).

Run: python3 -B -m unittest discover -s tests -p 'test_config_stage.py' -v

Docker is mocked: `exec mkdir -p`, `cp` and `restart` are recorded and the copy
lands in a temporary "container" directory. Readiness is a real TCP listener on
127.0.0.1, so the port probe is exercised for real; no container, service or GPU
is touched and no benchmark is started.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]

MOCK_DOCKER = '''
import json, os, pathlib, shutil, sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["MOCK_CONTAINER_ROOT"])
root.mkdir(parents=True, exist_ok=True)
events = pathlib.Path(os.environ["MOCK_EVENTS"])

def event(stage, **extra):
    payload = dict(command=name, argv=args, stage=stage, pid=os.getpid())
    payload.update(extra)
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + "\\n")

event("start")
mode = os.environ.get("MOCK_DOCKER_MODE", "ok")
if args[:1] == ["exec"]:
    if args[2:4] != ["mkdir", "-p"] or len(args) != 5:
        sys.exit("unexpected docker exec: " + " ".join(args))
    (root / args[4].strip("/")).mkdir(parents=True, exist_ok=True)
elif args[:1] == ["cp"]:
    if len(args) != 3:
        sys.exit("unexpected docker cp: " + " ".join(args))
    source, dest = args[1], args[2]
    container, _, remote = dest.partition(":")
    if not container:
        sys.exit("docker cp destination must be CONTAINER:PATH")
    if mode == "fail_cp":
        sys.exit(6)
    target = root / remote.strip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
elif args[:1] == ["restart"]:
    if len(args) != 2:
        sys.exit("unexpected docker restart: " + " ".join(args))
    if mode == "fail_restart":
        sys.exit(7)
else:
    sys.exit("unexpected docker call: " + " ".join(args))
event("end")
'''


class Listener:
    """A real listening socket: the readiness probe must succeed for real."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            connection.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def closed_port():
    """A port that was just free: connections must be refused there."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ApplyServerConfigTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="apply-server-config-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        for name in ("run_test.sh", "apply_server_config.sh", "update_server_config.sh",
                     "prepare_run.py", "ports.json"):
            source = ROOT / name
            self.assertTrue(source.is_file(), name + " is missing")
            shutil.copy2(source, self.project / name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.container_root = self.base / "container"
        self.container_root.mkdir()
        self.events = self.base / "events.jsonl"
        docker = self.bin / "docker"
        docker.write_text("#!" + sys.executable + "\n" + textwrap.dedent(MOCK_DOCKER).lstrip())
        docker.chmod(0o755)
        self.ports_config = self.project / "ports.json"
        self.run_dir = self.base / "run-dir"
        self.config = self.base / "server_config.json"
        self.write_config(workers=1, threads=1)
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        MOCK_CONTAINER_ROOT=str(self.container_root),
                        MOCK_EVENTS=str(self.events),
                        PYTHONDONTWRITEBYTECODE="1",
                        CONFIG_WAIT="30", CONFIG_SETTLE="5")
        for key in ("MOCK_DOCKER_MODE", "TARGET_PATH", "PORT", "PORTS_CONFIG"):
            self.env.pop(key, None)

    # ---------------------------------------------------------------- helpers
    def write_config(self, workers, threads, extra=None):
        payload = {"workers": workers, "threads": threads, "keep": "untouched"}
        if extra:
            payload.update(extra)
        self.config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_ports(self, mapping):
        self.ports_config.write_text(json.dumps(mapping), encoding="utf-8")

    def start_listener(self):
        listener = Listener()
        self.addCleanup(listener.close)
        return listener

    def apply(self, *args, **kwargs):
        command = ["bash", str(self.project / "apply_server_config.sh"),
                   str(self.config), "svc-c1", "fs", "2", "4", *args]
        env = dict(self.env)
        env.update(kwargs.pop("env", {}))
        return subprocess.run(command, cwd=str(self.base), env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=kwargs.pop("timeout", 40), **kwargs)

    def records(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def docker_stages(self):
        return [(record["argv"][0], record["stage"]) for record in self.records()]

    def container_copy(self, remote="/hexapp/ai-fs-serving/conf/server_config.json"):
        return self.container_root / remote.strip("/")

    # ------------------------------------------------------------------ tests
    def test_success_edits_copies_restarts_waits_and_archives(self):
        listener = self.start_listener()
        self.write_ports({"fs": listener.port})
        result = self.apply("--run-dir", str(self.run_dir), "--wait", "20", "--settle", "0")
        self.assertEqual(result.returncode, 0, result.stdout)

        # 1. 本地 JSON 只改 workers/threads，其它字段保留
        local = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual((local["workers"], local["threads"]), (2, 4))
        self.assertEqual(local["keep"], "untouched")

        # 2. 复制进容器的就是改后的内容
        copied = json.loads(self.container_copy().read_text(encoding="utf-8"))
        self.assertEqual((copied["workers"], copied["threads"]), (2, 4))

        # 3. 顺序必须是「复制 → 重启」，不能先重启再拷文件
        stages = self.docker_stages()
        self.assertIn(("cp", "end"), stages)
        self.assertIn(("restart", "end"), stages)
        self.assertLess(stages.index(("cp", "end")), stages.index(("restart", "end")))
        self.assertIn(("exec", "end"), stages, "目标目录要先 mkdir -p")

        # 4. 归档：配置快照 + 本次改了什么、就绪结果
        snapshot = json.loads((self.run_dir / "server_config.json").read_text(encoding="utf-8"))
        self.assertEqual((snapshot["workers"], snapshot["threads"]), (2, 4))
        change = json.loads((self.run_dir / "config_change.json").read_text(encoding="utf-8"))
        self.assertEqual(change["workers"], 2)
        self.assertEqual(change["threads"], 4)
        self.assertEqual(change["container"], "svc-c1")
        self.assertEqual(change["service"], "fs")
        self.assertEqual(change["readiness"], "ok")
        self.assertEqual(change["readiness_port"], listener.port)
        self.assertEqual(change["target_path"],
                         "/hexapp/ai-fs-serving/conf/server_config.json")
        self.assertIsInstance(change["applied_at"], str)
        self.assertIn("就绪", result.stdout)

    def test_readiness_timeout_fails_and_records_the_failure(self):
        self.write_ports({"fs": closed_port()})
        result = self.apply("--run-dir", str(self.run_dir), "--wait", "1", "--settle", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("未", result.stdout)
        change = json.loads((self.run_dir / "config_change.json").read_text(encoding="utf-8"))
        self.assertEqual(change["readiness"], "timeout")
        # 配置本身已经改完、也已经重启了：失败的是「服务没起来」，不能伪装成功
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["workers"], 2)
        self.assertIn(("restart", "end"), self.docker_stages())

    def test_wait_zero_skips_the_probe(self):
        result = self.apply("--run-dir", str(self.run_dir), "--wait", "0", "--settle", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        change = json.loads((self.run_dir / "config_change.json").read_text(encoding="utf-8"))
        self.assertEqual(change["readiness"], "skipped")
        self.assertIsNone(change["readiness_port"])

    def test_explicit_port_wins_over_ports_json(self):
        listener = self.start_listener()
        # ports.json 里根本没有 fs，说明 --port 没有回落到查表
        self.write_ports({"bsocr": listener.port + 1})
        result = self.apply("--run-dir", str(self.run_dir),
                            "--port", str(listener.port), "--wait", "20", "--settle", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        change = json.loads((self.run_dir / "config_change.json").read_text(encoding="utf-8"))
        self.assertEqual(change["readiness_port"], listener.port)

    def test_unknown_service_without_port_is_an_error(self):
        self.write_ports({"bsocr": closed_port()})
        result = self.apply("--run-dir", str(self.run_dir), "--wait", "5")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("fs", result.stdout)

    def test_restart_failure_stops_before_the_probe(self):
        listener = self.start_listener()
        self.write_ports({"fs": listener.port})
        result = self.apply("--run-dir", str(self.run_dir),
                            env={"MOCK_DOCKER_MODE": "fail_restart"})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("改服务配置失败", result.stdout)
        self.assertNotIn(("restart", "end"), self.docker_stages())
        self.assertFalse((self.run_dir / "config_change.json").exists())

    def test_cp_failure_is_not_reported_as_success(self):
        listener = self.start_listener()
        self.write_ports({"fs": listener.port})
        result = self.apply("--run-dir", str(self.run_dir),
                            env={"MOCK_DOCKER_MODE": "fail_cp"})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("改服务配置失败", result.stdout)

    def test_custom_target_path_is_honoured(self):
        listener = self.start_listener()
        self.write_ports({"fs": listener.port})
        result = self.apply("--target-path", "/opt/app/conf/server_config.json",
                            "--run-dir", str(self.run_dir), "--wait", "20", "--settle", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        copied = json.loads(self.container_copy("/opt/app/conf/server_config.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual((copied["workers"], copied["threads"]), (2, 4))
        change = json.loads((self.run_dir / "config_change.json").read_text(encoding="utf-8"))
        self.assertEqual(change["target_path"], "/opt/app/conf/server_config.json")

    def test_argument_validation_rejects_bad_input_before_touching_docker(self):
        cases = {
            "relative target path": ["--target-path", "opt/app/conf.json"],
            "colon in target path": ["--target-path", "/opt/app/conf:wrong.json"],
            "negative wait": ["--wait", "-1"],
            "non-numeric settle": ["--settle", "abc"],
            "out of range port": ["--port", "70000"],
            "unknown option": ["--nope", "1"],
        }
        for label, extra in cases.items():
            with self.subTest(label):
                result = self.apply(*extra)
                self.assertNotEqual(result.returncode, 0, label + ": " + result.stdout)
        self.assertEqual(self.records(), [], "no docker call may happen on bad arguments")

    def test_missing_config_file_is_an_error(self):
        self.config.unlink()
        result = self.apply("--wait", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("不存在", result.stdout)

    def test_usage_is_printed_when_arguments_are_missing(self):
        result = subprocess.run(["bash", str(self.project / "apply_server_config.sh"),
                                 str(self.config), "svc-c1"],
                                cwd=str(self.base), env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stdout)


if __name__ == "__main__":
    unittest.main()
