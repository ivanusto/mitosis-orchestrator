#!/usr/bin/env python3
"""Codex plugin hook tests (payload shapes captured from Codex 0.163).
Run: python -m unittest discover tests"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eo = _load("enforce_orchestrator_codex_hooks", "enforce-orchestrator.py")

ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")
SCOUT = "gpt-5.6-luna"
WORKER = "gpt-5.6-terra"


def codex_payload(event, **extra):
    p = {
        "session_id": "01a15f91-1862-7122-a482-edb5acadc197",
        "transcript_path": "/nonexistent/rollout.jsonl",
        "cwd": "/tmp",
        "hook_event_name": event,
        "model": SCOUT,
        "permission_mode": "bypassPermissions",
        "turn_id": "01a15f91-1b97-7e53-a469-8d0ca70908aa",
    }
    p.update(extra)
    return p


def pre_tool(tool_name, tool_input):
    return codex_payload(
        "PreToolUse", tool_name=tool_name, tool_input=tool_input, tool_use_id="call_1"
    )


class CodexHooksTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(ENV_PREFIXES):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TMPDIR"] = self.tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def run_script(self, mode, payload):
        env = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), mode, "--platform=codex"],
            input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def spawn(self, **tool_input):
        return eo.handle_pre_tool_use(pre_tool("spawn_agent", tool_input))

    def assert_allow(self, out):
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "allow")
        return hso["updatedInput"]

    def test_lookup_spawn_pins_scout_model_and_appends_report_format(self):
        upd = self.assert_allow(self.spawn(
            message="Find where main is defined in this directory and report the file name."))
        self.assertEqual(upd["model"], SCOUT)
        self.assertIn("REPORT FORMAT", upd["message"])
        for h in ("STATUS:", "FILES_CHANGED:", "VERIFICATION:", "NOTES:"):
            self.assertIn(h, upd["message"])

    def test_edit_spawn_pins_worker_model(self):
        upd = self.assert_allow(self.spawn(
            message="Edit app.py and fix the bug in main, then write the changes."))
        self.assertEqual(upd["model"], WORKER)
        self.assertIn("REPORT FORMAT", upd["message"])

    def test_scout_typed_edit_is_rewritten_to_worker(self):
        upd = self.assert_allow(self.spawn(
            message="Edit app.py to rename main", agent_type="flash-lite-scout"))
        self.assertEqual(upd["agent_type"], "worker")
        self.assertEqual(upd["model"], WORKER)

    def test_bash_pre_tool_use_has_no_opinion(self):
        self.assertEqual(eo.handle_pre_tool_use(pre_tool("Bash", {"command": "ls"})), {})

    def test_mitosis_scout_tool_name_is_recognised(self):
        self.assertTrue(eo.is_codex_payload(pre_tool("mcp__mitosis__mitosis_scout", {"prompt": "find main"})))
        out = eo.handle_pre_tool_use(pre_tool("mcp__mitosis__mitosis_scout", {"prompt": "find main"}))
        self.assertNotIn("not supported", json.dumps(out))
        self.assertNotEqual(
            out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", out)

    def test_session_start_subprocess_names_live_model(self):
        p = codex_payload("SessionStart", source="startup", model="gpt-5.6-terra")
        del p["turn_id"]
        out = self.run_script("session-start", p)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("gpt-5.6-terra", hso["additionalContext"])

    def test_subagent_user_prompt_submit_is_empty(self):
        p = codex_payload("UserPromptSubmit", prompt="Reply with exactly the word PONG.",
                          agent_id="01a15f96-42ad", agent_type="default")
        self.assertEqual(eo.handle_pre_invocation(p), {})

    def test_is_codex_payload_turn_id_vs_claude(self):
        with_turn = {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Task",
                     "tool_input": {}, "turn_id": "t1"}
        self.assertTrue(eo.is_codex_payload(with_turn))
        claude = dict(with_turn)
        del claude["turn_id"]
        self.assertFalse(eo.is_codex_payload(claude))
        self.assertTrue(eo.is_claude_code_payload(claude))

    def test_codex_hooks_json_commands(self):
        data = json.loads((ROOT / "hooks" / "codex-hooks.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn("--platform=codex", c)
            self.assertIn("${PLUGIN_ROOT}", c)

    def test_plugin_json_points_at_codex_hooks(self):
        data = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"], "./hooks/codex-hooks.json")
        self.assertTrue((ROOT / "hooks" / "codex-hooks.json").is_file())


if __name__ == "__main__":
    unittest.main()
