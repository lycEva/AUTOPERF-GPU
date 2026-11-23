"""`gputest` CLI contracts: dispatch, help, install and caller-relative paths.

Run: python3 -B -m unittest discover -s tests -p 'test_cli.py' -v

Everything here is offline: `--help` / `version` / `services` never touch
docker, nvidia-smi or jmeter, and the run_test.sh path check fails before any
of those are required, so this suite runs on any dev machine.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GPUTEST = ROOT / "bin" / "gputest"
INSTALLER = ROOT / "install.sh"


def run(args, cwd=None, env=None, timeout=60):
    """Run the CLI and return (returncode, stdout, stderr) as text."""
    merged = dict(os.environ)
    merged.pop("GPUTEST_HOME", None)
    if env:
        merged.update(env)
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, env=merged,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


class CliBasics(unittest.TestCase):
    def test_version(self):
        code, out, _err = run([str(GPUTEST), "version"])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^gputest \d+\.\d+\.\d+$")

    def test_help_lists_every_subcommand(self):
        code, out, _err = run([str(GPUTEST), "help"])
        self.assertEqual(code, 0)
        for command in ("run", "config", "monitor", "plot", "services", "doctor", "install"):
            self.assertIn(command, out)

    def test_subcommand_help_exits_zero(self):
        for command in ("run", "config", "monitor", "plot", "services", "install"):
            with self.subTest(command=command):
                code, out, _err = run([str(GPUTEST), command, "--help"])
                self.assertEqual(code, 0)
                self.assertIn(command, out)

    def test_unknown_command_fails(self):
        code, _out, err = run([str(GPUTEST), "definitely-not-a-command"])
        self.assertEqual(code, 1)
        self.assertIn("未知命令", err)

    def test_monitor_without_container_fails(self):
        code, _out, err = run([str(GPUTEST), "monitor"])
        self.assertEqual(code, 1)
        self.assertIn("--container", err)

    def test_plot_rejects_missing_csv(self):
        code, _out, err = run([str(GPUTEST), "plot", "./no-such-metrics.csv"])
        self.assertEqual(code, 1)
        self.assertIn("CSV 不存在", err)


class ServicesCommand(unittest.TestCase):
    def test_lists_bundled_ports(self):
        code, out, _err = run([str(GPUTEST), "services"])
        self.assertEqual(code, 0)
        expected = json.loads((ROOT / "ports.json").read_text(encoding="utf-8"))
        for name, port in expected.items():
            self.assertIn(name, out)
            self.assertIn(str(port), out)

    def test_honours_ports_config_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "ports.json"
            custom.write_text(json.dumps({"only-one": 51234}), encoding="utf-8")
            code, out, _err = run([str(GPUTEST), "services", "--ports-config", str(custom)])
            self.assertEqual(code, 0)
            self.assertIn("only-one", out)
            self.assertIn("51234", out)


class SymlinkResolution(unittest.TestCase):
    """An installed symlink must still find the tool directory."""

    def test_reaches_tool_root_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "gputest"
            link.symlink_to(GPUTEST)
            code, out, _err = run([str(link), "version"])
            self.assertEqual(code, 0)
            self.assertIn("gputest", out)

    def test_gputest_home_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = run([str(GPUTEST), "services"], env={"GPUTEST_HOME": tmp})
            self.assertEqual(code, 1)
            self.assertIn("找不到工具目录", err)


class CallerRelativePaths(unittest.TestCase):
    """Relative paths belong to the caller's cwd, not to the tool directory."""

    def test_missing_jmx_is_reported_with_callers_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            code, out, err = run(
                ["bash", str(ROOT / "run_test.sh"), "./no-such.jmx", "./out",
                 "some-container", "1", "1", "--service", "fs"],
                cwd=workdir,
            )
            self.assertEqual(code, 1)
            self.assertIn(str(workdir / "no-such.jmx"), out + err)

    def test_help_does_not_need_positionals(self):
        code, out, _err = run(["bash", str(ROOT / "run_test.sh"), "--help"], cwd=tempfile.gettempdir())
        self.assertEqual(code, 0)
        self.assertIn("--service", out)

    def test_run_reports_missing_jmx_from_any_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run(
                [str(GPUTEST), "run", "./absent.jmx", "./out",
                 "some-container", "1", "1", "--service", "fs"],
                cwd=Path(tmp),
            )
            self.assertEqual(code, 1)
            self.assertIn(str(Path(tmp) / "absent.jmx"), out + err)


class Installer(unittest.TestCase):
    def test_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "bin"
            code, out, err = run(["bash", str(INSTALLER), "--prefix", str(prefix)])
            self.assertEqual(code, 0, msg=out + err)
            for name in ("gputest", "gpu-test"):
                target = prefix / name
                self.assertTrue(target.is_symlink(), msg="{} missing".format(target))
                self.assertEqual(Path(os.path.realpath(target)), GPUTEST)

            code, out, _err = run([str(prefix / "gputest"), "version"])
            self.assertEqual(code, 0)
            self.assertIn("gputest", out)

            code, out, err = run(["bash", str(INSTALLER), "--prefix", str(prefix), "--uninstall"])
            self.assertEqual(code, 0, msg=out + err)
            self.assertFalse((prefix / "gputest").exists())
            self.assertFalse((prefix / "gpu-test").exists())

    def test_copy_mode_is_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "bin"
            code, out, err = run(["bash", str(INSTALLER), "--prefix", str(prefix), "--copy"])
            self.assertEqual(code, 0, msg=out + err)
            copied = prefix / "gputest.d"
            self.assertTrue((copied / "run_test.sh").is_file())
            self.assertTrue((copied / "ports.json").is_file())
            self.assertTrue((copied / "bin" / "gputest").is_file())
            # 复制的这份不依赖源目录
            code, out, _err = run([str(prefix / "gputest"), "services"],
                                  env={"GPUTEST_HOME": ""})
            self.assertEqual(code, 0)
            self.assertIn("fs", out)


if __name__ == "__main__":
    unittest.main()
