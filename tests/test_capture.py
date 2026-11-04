"""Payload capture: it must never delay or change the hook's response, and it must stay private."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mitosis_hook import capture  # noqa: E402

SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
POSIX = os.name != "nt"


class CaptureWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def write(self, directory, snap=None):
        os.environ["MITOSIS_CAPTURE_PAYLOADS"] = str(directory)
        capture.write({"a": 1} if snap is None else snap, "pre-tool-use", {})

    def test_writes_one_line(self):
        d = self.base / "cap"
        self.write(d)
        self.write(d)
        lines = (d / "payloads.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["mode"] for x in lines], ["pre-tool-use"] * 2)

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_c2_private_modes_whatever_the_umask(self):
        old = os.umask(0)
        try:
            d = self.base / "cap"
            self.write(d)
            existing = self.base / "loose"
            existing.mkdir()
            (existing / "payloads.jsonl").write_text("", encoding="utf-8")
            os.chmod(str(existing / "payloads.jsonl"), 0o666)
            self.write(existing)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(str(d)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(str(d / "payloads.jsonl")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(existing / "payloads.jsonl")).st_mode), 0o600)

    @unittest.skipUnless(POSIX and hasattr(os, "symlink") and hasattr(os, "O_NOFOLLOW"), "needs symlinks and O_NOFOLLOW")
    def test_c2_symlink_is_not_followed(self):
        d = self.base / "cap"
        d.mkdir()
        target = self.base / "victim"
        target.write_text("keep\n", encoding="utf-8")
        os.symlink(str(target), str(d / "payloads.jsonl"))
        self.write(d)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs")
    def test_c1_fifo_is_refused_without_blocking(self):
        d = self.base / "cap"
        d.mkdir()
        fifo = str(d / "payloads.jsonl")
        os.mkfifo(fifo)
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # a reader makes a blocking-free open succeed
        try:
            self.write(d)
            try:
                data = os.read(reader, 100)
            except BlockingIOError:
                data = b""
            self.assertEqual(data, b"")
        finally:
            os.close(reader)

    def test_c3_relative_path_is_refused(self):
        os.chdir(str(self.base))
        try:
            self.write("rel-cap")
            self.assertFalse((self.base / "rel-cap").exists())
        finally:
            os.chdir(str(ROOT))

    def test_c3_tilde_is_expanded(self):
        home = self.base / "home"
        home.mkdir()
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
        self.write("~/cap")
        self.assertTrue((home / "cap" / "payloads.jsonl").is_file())
        self.assertFalse((Path.cwd() / "~").exists())


class CaptureDoesNotDelayTheResponse(unittest.TestCase):
    def run_hook(self, capture_dir, prepare=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            _support.set_temp_env(env, tmp)
            env["MITOSIS_CAPTURE_PAYLOADS"] = capture_dir(tmp)
            if prepare:
                prepare(capture_dir(tmp))
            payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read",
                       "tool_input": {"file_path": "x"}, "cwd": tmp}
            return subprocess.run([sys.executable, str(SCRIPT), "pre-tool-use"], input=json.dumps(payload).encode(),
                                  capture_output=True, env=env, timeout=30)

    def test_stdout_is_complete_when_capture_cannot_write(self):
        def unwritable(tmp):
            return os.path.join(tmp, "file-not-dir", "cap")

        def prepare(path):
            Path(path).parent.write_text("x", encoding="utf-8")  # the parent is a file: makedirs fails

        proc = self.run_hook(unwritable, prepare)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout.decode()), dict)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs")
    def test_c1_fifo_does_not_hang_the_hook(self):
        def fifo_dir(tmp):
            return os.path.join(tmp, "cap")

        def prepare(path):
            os.makedirs(path)
            os.mkfifo(os.path.join(path, "payloads.jsonl"))

        proc = self.run_hook(fifo_dir, prepare)  # a blocking open waits for a reader and times out
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout.decode()), dict)

    def test_capture_runs_after_stdout_is_flushed(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(source.index("sys.stdout.flush()"), source.index("capture.write("))


if __name__ == "__main__":
    unittest.main()
