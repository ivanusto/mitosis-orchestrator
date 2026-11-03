#!/usr/bin/env python3
"""The hook commands must start Python on every platform, through the shell each CLI uses.

agy runs hooks with `cmd /c` on Windows. The Microsoft Store alias `python3.exe` exits 9009 with
no output, and a python.org install has `python` and `py` but no `python3`.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
CONFIGS = ("hooks.json", "hooks/hooks.json", "hooks/codex-hooks.json")
WINDOWS = os.name == "nt"
PAYLOAD = {
    "hook_event_name": "PreToolUse",
    "session_id": "launcher-test",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
}


def commands(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from commands(item)


def config_commands(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    root = ROOT.as_posix()
    return [
        c.replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${PLUGIN_ROOT}", root)
        for c in commands(json.loads(text))
    ]


def run_shell(command, env, stdin):
    if WINDOWS:  # the raw command line, as `cmd /c <command>`
        argv = 'cmd /d /s /c "' + command + '"'
    else:
        argv = ["/bin/sh", "-c", command]
    return subprocess.run(argv, input=stdin, capture_output=True, env=env, cwd=str(ROOT), timeout=60)


class ShimDir:
    """A PATH with only the interpreters a test names; each logs that it ran."""

    def __init__(self, tmp):
        self.dir = Path(tmp) / "bin"
        self.dir.mkdir()
        self.log = Path(tmp) / "ran.log"
        self.tmp = tmp

    def add(self, name, works=True):
        real = sys.executable
        if WINDOWS:
            # `py -3 x` drops the version flag; the others pass everything through.
            args = "%2 %3 %4 %5" if name == "py" else "%*"
            body = f'@echo {name}>>"{self.log}"\r\n'
            body += f'@"{real}" -B {args}\r\n' if works else "@exit /b 9009\r\n"
            (self.dir / (name + ".cmd")).write_text(body, encoding="ascii", newline="")
        else:
            args = '"$@"'
            body = f'#!/bin/sh\necho {name} >> "{self.log}"\n'
            if name == "py":
                body += "shift\n"
            body += f'exec "{real}" -B {args}\n' if works else "exit 127\n"
            path = self.dir / name
            path.write_text(body, encoding="ascii")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return self

    def env(self):
        env = {k: v for k, v in os.environ.items() if "MITOSIS" not in k and not k.startswith("PYTHON")}
        path = [str(self.dir)]
        if WINDOWS:  # cmd.exe itself; System32 ships no python
            path.append(os.path.join(os.environ["SystemRoot"], "System32"))
        env["PATH"] = os.pathsep.join(path)
        env["TMP"] = env["TEMP"] = env["TMPDIR"] = self.tmp
        return env

    def ran(self):
        if not self.log.exists():
            return []
        return [line.strip() for line in self.log.read_text().splitlines() if line.strip()]


class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.shims = ShimDir(self._tmp.name)

    def check(self, rel, command, expected_ran):
        proc = run_shell(command, self.shims.env(), json.dumps(PAYLOAD).encode())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout.decode()), dict, rel)
        self.assertEqual(self.shims.ran(), expected_ran, rel)

    def scenario(self, expected_ran):
        for rel in CONFIGS:
            with self.subTest(config=rel):
                if self.shims.log.exists():
                    self.shims.log.unlink()
                self.check(rel, config_commands(rel)[0], expected_ran)

    def test_python3_is_tried_first_and_runs_once(self):
        self.shims.add("python3").add("python").add("py")
        self.scenario(["python3"])

    def test_only_python(self):
        self.shims.add("python")
        self.scenario(["python"])

    def test_store_alias_falls_through_to_python(self):
        self.shims.add("python3", works=False).add("python")
        self.scenario(["python3", "python"])

    def test_store_aliases_fall_through_to_py(self):
        self.shims.add("python3", works=False).add("python", works=False).add("py")
        self.scenario(["python3", "python", "py"])

    def test_no_python_answers_nothing(self):
        # A failed hook blocks every tool on agy, and so does "{}" from PreToolUse. Empty output
        # with exit 0 is "no objection" on all three CLIs.
        for rel in CONFIGS:
            for command in config_commands(rel):
                with self.subTest(config=rel, command=command[-60:]):
                    proc = run_shell(command, self.shims.env(), json.dumps(PAYLOAD).encode())
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.strip(), b"")

    def test_every_hook_mode_answers_json(self):
        self.shims.add("python")
        for rel in CONFIGS:
            for command in config_commands(rel):
                with self.subTest(config=rel, command=command[-60:]):
                    proc = run_shell(command, self.shims.env(), json.dumps(PAYLOAD).encode())
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIsInstance(json.loads(proc.stdout.decode()), dict)

    @unittest.skipUnless(WINDOWS and shutil.which("python3"), "Windows with python3 on PATH")
    def test_claude_code_git_bash(self):
        bash = r"C:\Program Files\Git\bin\bash.exe"
        if not os.path.exists(bash):
            self.skipTest("no Git Bash")
        command = config_commands("hooks/hooks.json")[0]
        proc = subprocess.run([bash, "-c", command], input=json.dumps(PAYLOAD).encode(), capture_output=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout.decode()), dict)


class TestStdinEncoding(unittest.TestCase):
    """PYTHONIOENCODING reproduces the Windows locale code page on any platform."""

    PROMPTS = (
        ("請修改 src/app.py 的登入流程，並補上單元測試", "sonnet"),
        ("找出 parse_token 定義在哪個檔案", "haiku"),
    )

    def run_hook(self, prompt, encoding, prefix=b""):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "encoding-test",
            "tool_name": "Agent",
            "tool_input": {"prompt": prompt, "description": "x"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if "MITOSIS" not in k and not k.startswith("PYTHON")}
            env["PYTHONIOENCODING"] = encoding
            env["PYTHONUTF8"] = "0"
            env["TMP"] = env["TEMP"] = env["TMPDIR"] = tmp
            proc = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "pre-tool-use"],
                input=prefix + json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True, env=env, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.decode())["hookSpecificOutput"]["updatedInput"]

    def test_utf8_prompt_survives_a_legacy_code_page(self):
        for encoding in ("cp950", "cp1252", "utf-8"):
            for prompt, model in self.PROMPTS:
                with self.subTest(encoding=encoding, prompt=prompt):
                    updated = self.run_hook(prompt, encoding)
                    self.assertEqual(updated["model"], model)
                    self.assertIn(prompt, updated["prompt"])

    def test_byte_order_mark_is_ignored(self):
        updated = self.run_hook(self.PROMPTS[1][0], "cp950", prefix=b"\xef\xbb\xbf")
        self.assertEqual(updated["model"], "haiku")

    def test_statusline_reads_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
            env["PYTHONIOENCODING"] = "cp950"
            env["TMP"] = env["TEMP"] = env["TMPDIR"] = tmp
            state = Path(tmp) / "mitosis-orchestrator" / "s1"
            state.mkdir(parents=True)
            (state / "mitosis_orchestrator_state.json").write_text('{"subagents": {"a": {"tier": "flash_lite"}}}', encoding="utf-8")
            data = json.dumps({"session_id": "s1", "cwd": "專案目錄", "model": {"display_name": "Opus"}}, ensure_ascii=False)
            proc = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "mitosis-statusline.py")],
                input=data.encode("utf-8"), capture_output=True, env=env, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"idle", proc.stdout)

    def test_mcp_server_reads_utf8(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["PYTHONIOENCODING"] = "cp950"
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"note": "繁體中文"}}, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "mitosis-mcp-server.py")],
            input=request.encode("utf-8") + b"\n", capture_output=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(json.loads(proc.stdout.decode().splitlines()[0]).get("error", {}).get("code"), -32700)


if __name__ == "__main__":
    unittest.main()
