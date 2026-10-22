#!/usr/bin/env python3
"""Per-session state lock and state correctness fixes.
Run: python -m unittest discover tests"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")
SID = "lock-session-1"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eo = _load("eo_state_lock", "enforce-orchestrator.py")


def claude_agent(i=0):
    return {
        "hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Agent",
        "tool_input": {"prompt": "Locate parse_token in src/ (%d)" % i, "description": "lookup %d" % i},
    }


class StateLockTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(ENV_PREFIXES):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TMPDIR"] = self.tmp.name
        tempfile.tempdir = None  # re-read TMPDIR

    def tearDown(self):
        eo._release_state_locks()
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        self.tmp.cleanup()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
        env.update(extra)
        return env

    def state_dir(self):
        return Path(self.tmp.name) / "mitosis-orchestrator" / SID

    def read_state(self):
        return json.loads((self.state_dir() / eo.STATE_FILENAME).read_text(encoding="utf-8"))

    def popen(self, mode, payload, **env):
        return subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env(**env),
        )

    @unittest.skipIf(fcntl is None, "needs fcntl")
    def test_parallel_agent_spawns_do_not_lose_pending_entries(self):
        procs = []
        for i in range(8):
            p = self.popen("pre-tool-use", None)
            procs.append((p, claude_agent(i)))
        for p, payload in procs:  # all started, then fed back to back
            p.stdin.write(json.dumps(payload))
            p.stdin.close()
        for p, _ in procs:
            self.assertEqual(p.wait(timeout=60), 0, p.stderr.read())
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(len(self.read_state()["pending_spawns"]), 8)

    @unittest.skipIf(fcntl is None, "needs fcntl")
    def test_lock_timeout_fails_open(self):
        d = self.state_dir()
        d.mkdir(parents=True)
        fd = os.open(str(d / eo.LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.monotonic()
            p = self.popen("pre-tool-use", None, CLAUDE_MITOSIS_LOCK_TIMEOUT="0.2")
            out, err = p.communicate(json.dumps(claude_agent()), timeout=30)
            elapsed = time.monotonic() - start
        finally:
            os.close(fd)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("hookSpecificOutput", json.loads(out))
        self.assertLess(elapsed, 5)

    def test_subagent_bash_failure_does_not_touch_primary_state(self):
        payload = {
            "hook_event_name": "PostToolUse", "session_id": SID, "tool_name": "Bash",
            "tool_input": {"command": "pytest"}, "tool_response": {"exit_code": 1},
            "tool_result": {"exitCode": 1}, "agent_id": "abc123", "agent_type": "mitosis-orchestrator:flash-worker",
        }
        self.assertEqual(eo.handle_post_tool_use(payload), {})
        self.assertEqual(eo.load_state(payload)["consecutive_failures"], 0)
        # The primary's own failure still counts.
        primary = dict(payload)
        del primary["agent_id"], primary["agent_type"]
        eo.handle_post_tool_use(primary)
        self.assertEqual(eo.load_state(primary)["consecutive_failures"], 1)

    def _seed(self, record, recipient="agent-x"):
        payload = {"session_id": SID}
        state = eo.load_state(payload)
        state["subagents"][recipient] = record
        eo.save_state(payload, state)
        eo._release_state_locks()

    def _send(self, claude, message="Edit app.py to rename main and write the change."):
        p = {
            "hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "SendMessage" if claude else "send_message",
            "tool_input": {"to": "agent-x", "message": message},
        }
        if not claude:
            p["turn_id"] = "t-1"
        return eo.handle_pre_tool_use(p)

    def _denied(self, out):
        return out.get("decision") == "deny" or out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_send_message_guard_uses_tier_field(self):
        self._seed({"type": "explorer", "model": "gpt-5.6-luna", "tier": "flash_lite"})
        self.assertTrue(self._denied(self._send(claude=True)))
        self.assertTrue(self._denied(self._send(claude=False)))

    def test_send_message_guard_tier_only_record(self):
        self._seed({"type": "explorer", "tier": "flash_lite"})
        self.assertTrue(self._denied(self._send(claude=True)))

    def test_send_message_guard_old_state_with_codex_scout_model(self):
        self._seed({"type": "explorer", "model": "gpt-5.6-luna"})
        self.assertTrue(self._denied(self._send(claude=True)))
        self.assertTrue(self._denied(self._send(claude=False)))

    def test_send_message_to_worker_is_not_denied(self):
        self._seed({"type": "worker", "model": "gpt-5.6-terra", "tier": "flash"})
        self.assertFalse(self._denied(self._send(claude=True)))

    def test_codex_spawn_agent_records_tier(self):
        p = {
            "session_id": SID, "hook_event_name": "PreToolUse", "turn_id": "t", "tool_name": "spawn_agent",
            "tool_input": {"message": "Find where main is defined in this directory and report the file name."},
        }
        eo.handle_pre_tool_use(p)
        self.assertEqual(self.read_state()["pending_spawns"][0]["tier"], "flash_lite")

    def test_record_tier_inference(self):
        self.assertEqual(eo._record_tier({"model": "haiku"}), "flash_lite")
        self.assertEqual(eo._record_tier({"model": "flash_lite"}), "flash_lite")
        self.assertEqual(eo._record_tier({"model": "sonnet"}), "flash")
        self.assertEqual(eo._record_tier({"model": "flash_lite", "tier": "flash"}), "flash")

    def test_subagent_stop_saves_once_and_nags_once(self):
        payload = {
            "hook_event_name": "SubagentStop", "session_id": SID, "agent_id": "a1",
            "agent_type": "mitosis-orchestrator:flash-worker", "last_assistant_message": "done, no format",
        }
        calls = []
        real = eo.save_state
        eo.claude.save_state = lambda pl, st: (calls.append(1), real(pl, st))[1]
        try:
            out = eo.handle_subagent_stop(payload)
        finally:
            eo.claude.save_state = real
        self.assertEqual(out.get("decision"), "block")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.read_state()["report_nagged"], ["a1"])
        self.assertEqual(eo.handle_subagent_stop(payload), {})

    @unittest.skipIf(fcntl is None, "needs fcntl")
    def test_locks_released_after_each_handler(self):
        os.environ["CLAUDE_MITOSIS_LOCK_TIMEOUT"] = "0.5"
        start = time.monotonic()
        for i in range(2):
            eo.handle_pre_tool_use(claude_agent(i))
            self.assertEqual(eo._HELD_LOCKS, {})
        self.assertLess(time.monotonic() - start, 0.4)
        # Another process can take the lock straight away.
        fd = os.open(str(self.state_dir() / eo.LOCK_FILENAME), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    @unittest.skipIf(fcntl is None, "needs fcntl")
    def test_nested_acquisition_is_reentrant(self):
        payload = {"session_id": SID}
        os.environ["CLAUDE_MITOSIS_LOCK_TIMEOUT"] = "0.5"
        start = time.monotonic()
        eo.load_state(payload)
        eo.load_state(payload)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(len(eo._HELD_LOCKS), 1)

    def test_debug_log_entries_are_single_lines(self):
        os.environ["CLAUDE_MITOSIS_DEBUG"] = "1"
        payload = claude_agent()
        for _ in range(3):
            eo._debug_log(payload, "pre-tool-use", {"x": 1})
        lines = (self.state_dir() / "mitosis_orchestrator_debug.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            json.loads(line)


if __name__ == "__main__":
    unittest.main()
