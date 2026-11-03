#!/usr/bin/env python3
"""Wire heartbeat: what hooks really saw, persisted per platform. Run: python -m unittest tests.test_heartbeat -v"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
spec = importlib.util.spec_from_file_location("enforce_orchestrator_heartbeat", SCRIPT)
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)
hb = eo.heartbeat

MARKER = "SECRET-MARKER-9f3a"
CLAUDE_BASH = {"hook_event_name": "PreToolUse", "session_id": "hb-test", "tool_name": "Bash",
               "tool_input": {"command": "ls"}}


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "hb"
        os.environ["MITOSIS_HEARTBEAT_DIR"] = str(self.dir)
        os.environ["TMPDIR"] = self.tmp.name
        tempfile.tempdir = None
        hb._PENDING.clear()
        hb._EVENTS_PENDING[0] = 0

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        hb._PENDING.clear()
        hb._EVENTS_PENDING[0] = 0
        self.tmp.cleanup()

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_"))}
        e["MITOSIS_HEARTBEAT_DIR"] = str(self.dir)
        e["TMPDIR"] = self.tmp.name
        e.update(extra)
        return e

    def run_main(self, data, mode="pre-tool-use", **env):
        return subprocess.run([sys.executable, str(SCRIPT), mode], input=data, capture_output=True,
                              env=self.env(**env), timeout=60)

    def read(self, platform="claude"):
        return json.loads((self.dir / ("heartbeat-%s.json" % platform)).read_text(encoding="utf-8"))

    def expire(self, platform="claude"):
        p = self.dir / ("heartbeat-%s.json" % platform)
        old = time.time() - 3600
        os.utime(str(p), (old, old))


class TestEntrypoint(Base):
    def test_writes_file_atomically_with_mode_0600(self):
        r = self.run_main(json.dumps(CLAUDE_BASH).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read("claude")
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["last_event"], "pre-tool-use")
        self.assertEqual(data["events"], 1)
        self.assertEqual(data["plugin_version"], json.loads((ROOT / "plugin.json").read_text())["version"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["heartbeat-claude.json"])  # no temp left
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.dir / "heartbeat-claude.json").stat().st_mode), 0o600)

    def test_disabled_flag_writes_nothing(self):
        r = self.run_main(json.dumps(CLAUDE_BASH).encode(), MITOSIS_HEARTBEAT="0")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.dir.exists())

    def test_invalid_utf8_stdin_is_counted(self):
        raw = b'{"hook_event_name":"PreToolUse","session_id":"hb","tool_name":"Bash","tool_input":{"command":"echo \xff\xfe"}}'
        r = self.run_main(raw)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read("claude")["stdin_replacement_chars"], 2)

    def test_cp950_bytes_are_counted(self):
        text = '{"hook_event_name":"PreToolUse","session_id":"hb","tool_name":"Bash","tool_input":{"command":"echo 測試"}}'
        r = self.run_main(text.encode("cp950"))
        self.assertEqual(r.returncode, 0)
        self.assertGreaterEqual(self.read("claude")["stdin_replacement_chars"], 1)

    def test_replacement_char_already_in_the_payload_is_not_counted(self):
        # Tool output with mojibake carries U+FFFD as valid UTF-8; nothing was lost reading stdin.
        text = '{"hook_event_name":"PreToolUse","session_id":"hb","tool_name":"Bash","tool_input":{"command":"echo ��"}}'
        r = self.run_main(text.encode("utf-8"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read("claude")["stdin_replacement_chars"], 0)

    def test_valid_replacement_char_does_not_hide_a_real_one(self):
        raw = '{"hook_event_name":"PreToolUse","session_id":"hb","tool_name":"Bash","tool_input":{"command":"echo � '.encode("utf-8")
        r = self.run_main(raw + b'\xff"}}')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read("claude")["stdin_replacement_chars"], 1)

    def test_parse_failure_recorded_as_stdin_parse_error(self):
        r = self.run_main(b"{not json " + MARKER.encode())
        self.assertEqual(r.returncode, 0)
        err = self.read("agy")["last_error"]
        self.assertEqual(err["class"], "StdinParseError")
        self.assertEqual(err["event"], "pre-tool-use")
        self.assertNotIn(MARKER, (self.dir / "heartbeat-agy.json").read_text())

    def test_agy_pre_tool_use_stdout_unchanged_by_heartbeat(self):
        payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}, "conversationId": "hb-agy",
                   "artifactDirectoryPath": str(Path(self.tmp.name) / "art")}
        (Path(self.tmp.name) / "art").mkdir()
        data = json.dumps(payload).encode()
        on = self.run_main(data)
        off = self.run_main(data, MITOSIS_HEARTBEAT="0")
        self.assertEqual(on.stdout, off.stdout)
        self.assertTrue((self.dir / "heartbeat-agy.json").exists())


class TestPrivacyAndErrors(Base):
    def test_no_prompt_path_or_exception_text_in_file(self):
        original = eo.HANDLERS["pre-tool-use"]

        def boom(_p):
            raise ValueError(MARKER + " /home/secret/path")

        eo.HANDLERS["pre-tool-use"] = boom
        payload = dict(CLAUDE_BASH, tool_input={"command": "cat /home/secret/" + MARKER}, cwd="/home/secret/" + MARKER)
        stdin = sys.stdin
        try:
            import io
            sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            old_argv = sys.argv
            sys.argv = ["x", "pre-tool-use"]
            out = io.StringIO()
            old_out, sys.stdout = sys.stdout, out
            old_err, sys.stderr = sys.stderr, io.StringIO()
            try:
                eo.main()
            finally:
                sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
        finally:
            sys.stdin = stdin
            eo.HANDLERS["pre-tool-use"] = original
        text = (self.dir / "heartbeat-claude.json").read_text()
        self.assertNotIn(MARKER, text)
        self.assertNotIn("secret", text)
        err = json.loads(text)["last_error"]
        self.assertEqual((err["class"], err["event"]), ("ValueError", "pre-tool-use"))
        # stdout is exactly what the hook printed before the heartbeat existed
        self.assertEqual(json.loads(out.getvalue()), eo._no_opinion(payload))

    def test_handler_exception_subprocess_class_only(self):
        # a Claude Bash payload whose handler raises: use a payload that breaks the real handler
        payload = dict(CLAUDE_BASH, tool_input={"command": ["not", "a", "string", MARKER]})
        r = self.run_main(json.dumps(payload).encode())
        self.assertEqual(r.returncode, 0)
        text = (self.dir / "heartbeat-claude.json").read_text()
        self.assertNotIn(MARKER, text)

    def test_corrupt_existing_file_recovered(self):
        self.dir.mkdir(parents=True)
        (self.dir / "heartbeat-claude.json").write_text("{corrupt" + MARKER)
        self.expire()  # a fresh corrupt file is healed at the next un-throttled write
        hb.record_event(CLAUDE_BASH, "pre-tool-use")
        data = self.read("claude")
        self.assertEqual(data["events"], 1)
        self.assertNotIn(MARKER, json.dumps(data))

    def test_unwritable_dir_does_not_raise(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        os.environ["MITOSIS_HEARTBEAT_DIR"] = str(blocker / "sub")
        hb.record_event(CLAUDE_BASH, "pre-tool-use", error_class="ValueError")
        hb.note(failure="Bash")
        r = self.run_main(json.dumps(CLAUDE_BASH).encode(), MITOSIS_HEARTBEAT_DIR=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, b"")


class TestThrottle(Base):
    def test_plain_events_throttled_informative_ones_not(self):
        hb.record_event(CLAUDE_BASH, "pre-tool-use")
        hb.record_event(CLAUDE_BASH, "pre-tool-use")
        hb.record_event(CLAUDE_BASH, "post-tool-use")
        self.assertEqual(self.read()["events"], 1)
        self.assertEqual(self.read()["last_event"], "pre-tool-use")
        hb.record_event(CLAUDE_BASH, "post-tool-use", error_class="KeyError")
        data = self.read()
        self.assertEqual(data["events"], 4)  # throttled events were held, then merged
        self.assertEqual(data["last_error"]["class"], "KeyError")
        self.expire()
        hb.record_event(CLAUDE_BASH, "stop")
        self.assertEqual(self.read()["last_event"], "stop")

    def test_counters_merge_and_names_are_capped(self):
        for i in range(20):
            hb.record_event(CLAUDE_BASH, "pre-tool-use", unhandled="xspawn_%d" % i)
        data = self.read()
        self.assertEqual(len(data["unhandled_spawn_tools"]), 16)
        hb.note(pin="mismatch")
        hb.note(report=False)
        hb.record_event(CLAUDE_BASH, "subagent-stop")
        data = self.read()
        self.assertEqual(data["pin"], {"verified": 0, "mismatch": 1, "unknown": 0})
        self.assertEqual(data["report"], {"ok": 0, "bad": 1})


class TestUnhandledSpawnTools(Base):
    def name_of(self, platform, raw):
        if platform == "codex":
            p = {"platform": "codex", "hook_event_name": "PreToolUse", "tool_name": raw, "tool_input": {}}
        else:
            p = dict(CLAUDE_BASH, tool_name=raw)
        return eo.handlers.unhandled_spawn_tool(p)

    def test_fake_spawn_tools_count(self):
        self.assertEqual(self.name_of("codex", "xyzspawn_agent"), "xyzspawn_agent")
        self.assertEqual(self.name_of("codex", "collab_invoke_subagent"), "collab_invoke_subagent")
        self.assertEqual(self.name_of("claude", "DelegateTask"), "DelegateTask")

    def test_real_names_do_not(self):
        for raw in ("Bash", "Read", "SendMessage", "send_message", "followup_task", "SubagentHandback", "Agent",
                    "Task", "spawn_agent", "invoke_subagent", "manage_subagents", "collaborationspawn_agent",
                    "collaboration.spawn_agent", "collaboration__wait_agent", "mcp__mitosis__mitosis_scout",
                    "mcp__other__agent_thing", "run_command", "Write"):
            # Namespaced Codex names are normalised for Codex payloads only.
            for platform in (("codex",) if raw.startswith("collaboration") else ("claude", "codex")):
                with self.subTest(raw=raw, platform=platform):
                    self.assertIsNone(self.name_of(platform, raw))

    def test_entrypoint_counts_unhandled_tool(self):
        payload = {"platform": "codex", "hook_event_name": "PreToolUse", "tool_name": "xyzspawn_agent", "tool_input": {}}
        r = self.run_main(json.dumps(payload).encode())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read("codex")["unhandled_spawn_tools"], {"xyzspawn_agent": 1})


class TestFailureObserver(Base):
    def setUp(self):
        super().setUp()
        art = Path(self.tmp.name) / "art"
        art.mkdir()
        self.art = art
        self.base = {"session_id": "hb-fail", "artifactDirectoryPath": str(art), "cwd": self.tmp.name}

    def state(self):
        return eo._read_state_file(eo._state_file(self.base))

    def test_post_tool_use_failure_is_observe_only(self):
        st = eo.load_state(self.base)
        st["consecutive_failures"] = 1
        st["pending_spawns"] = [{"role": "r", "type": "t"}]
        eo.save_state(self.base, st)
        eo._release_state_locks()
        p = dict(self.base, hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "false"},
                 error="boom " + MARKER)
        self.assertEqual(eo.handle_post_tool_use_failure(p), {})
        eo.handle_post_tool_use_failure(p)
        s = self.state()
        self.assertEqual(s["consecutive_failures"], 1)
        self.assertEqual(len(s["pending_spawns"]), 1)
        self.assertEqual(s["tool_failures"], {"Bash": 2})
        hb.record_event(p, "post-tool-use-failure")
        self.assertEqual(self.read()["tool_failures"], {"Bash": 2})
        self.assertNotIn(MARKER, (self.dir / "heartbeat-claude.json").read_text())

    def test_subagent_failure_counts_in_heartbeat_only(self):
        p = dict(self.base, hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={}, agent_id="abc123")
        self.assertEqual(eo.handle_post_tool_use_failure(p), {})
        self.assertNotIn("tool_failures", self.state())
        hb.record_event(p, "post-tool-use-failure")
        self.assertEqual(self.read()["tool_failures"], {"Bash": 1})

    def test_failed_tool_response_observed_without_breaker_change(self):
        p = dict(self.base, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "grep x y"},
                 tool_response={"exitCode": 1, "stdout": ""})
        before = eo._tool_failed("Bash", p)
        eo.handle_post_tool_use(p)
        eo.handle_post_tool_use(p)
        self.assertEqual(eo._tool_failed("Bash", p), before)
        self.assertFalse(before)
        self.assertEqual(self.state()["consecutive_failures"], 0)
        hb.record_event(p, "post-tool-use")
        self.assertEqual(self.read()["tool_failures"], {"Bash": 2})

    def test_mode_registered_and_hook_configured(self):
        self.assertIn("post-tool-use-failure", eo.HANDLERS)
        cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUseFailure"][0]
        self.assertEqual(cfg["matcher"], "Agent|Bash")
        self.assertIn("post-tool-use-failure", cfg["hooks"][0]["command"])


class TestSubagentStopCounts(Base):
    def test_claude_stop_counts_pin_and_report_once(self):
        art = Path(self.tmp.name) / "art"
        art.mkdir()
        base = {"session_id": "hb-stop", "artifactDirectoryPath": str(art), "cwd": self.tmp.name}
        st = eo.load_state(base)
        st["subagents"]["a1a1a1a1a1a1a1a1"] = {"type": "mitosis-orchestrator:flash-worker", "pin_verified": True, "report_ok": True}
        eo.save_state(base, st)
        eo._release_state_locks()
        p = dict(base, hook_event_name="SubagentStop", agent_id="a1a1a1a1a1a1a1a1",
                 agent_type="mitosis-orchestrator:flash-worker", last_assistant_message="not a report")
        eo.handle_subagent_stop(p)
        hb.record_event(p, "subagent-stop")
        data = self.read()
        self.assertEqual(sum(data["report"].values()), 1)
        self.assertEqual(sum(data["pin"].values()), 1)


if __name__ == "__main__":
    unittest.main()
