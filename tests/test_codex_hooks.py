#!/usr/bin/env python3
"""Codex plugin hook tests (payload shapes captured from Codex 0.163).
Run: python -m unittest discover tests"""

import importlib.util
import json
import os
import re
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


# Codex 0.163 multi-agent v2: hooks see "collaboration<tool>" names and an encrypted message.
OPAQUE = (
    "gAAAAABq2W6q_XALFZgD1zKjI6St8hIilg6diYo5-ULKaJMEhHaMKEcdSxOmRa2b1_16hBGQv9tSsT-yYt4xtowdOA_"
    "fE7FIw96PB5VXsqtVMWeVsN8J_9wdsW49rMnfe9yNrXF8a8tipe-jXMH7_OFi1IT9rdtcvnDYiwyoN1jlPhnNo"
)
CHILD = {"agent_id": "01a16158-4b8c-7740-8588-dc83af681e30", "agent_type": "explorer"}


class CodexCollaborationTest(unittest.TestCase):
    def setUp(self):
        CodexHooksTest.setUp(self)
        self.sid = f"01a16158-{os.getpid():04x}-{id(self) & 0xffff:04x}"

    def tearDown(self):
        CodexHooksTest.tearDown(self)

    def payload(self, event, **extra):
        p = codex_payload(event, **extra)
        p["session_id"] = self.sid
        return p

    def pre(self, tool, tool_input, **extra):
        return eo.handle_pre_tool_use(self.payload("PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id="call_1", **extra))

    def post(self, tool, tool_input, **extra):
        return eo.handle_post_tool_use(self.payload("PostToolUse", tool_name=tool, tool_input=tool_input, tool_use_id="call_1", **extra))

    def subagents(self):
        return eo._read_state_file(eo._state_file(self.payload("Stop"))).get("subagents", {})

    def denied(self, out):
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def spawn_probe(self, message=OPAQUE, **extra):
        tool_input = {"task_name": "probe", "fork_turns": "none", "message": message}
        tool_input.update(extra)
        return self.pre("collaborationspawn_agent", tool_input)

    def test_collaboration_prefix_is_stripped(self):
        for raw in ("collaborationspawn_agent", "collaboration.followup_task", "collaboration__send_message"):
            name, _ = eo._extract_tool_call(self.payload("PreToolUse", tool_name=raw, tool_input={}))
            self.assertEqual(name, raw.replace("collaboration", "").lstrip("._"))
        name, _ = eo._extract_tool_call(self.payload("PreToolUse", tool_name="collaborationnotes", tool_input={}))
        self.assertEqual(name, "collaborationnotes")

    def test_opaque_spawn_keeps_message_and_role(self):
        upd = self.spawn_probe(agent_type="explorer")["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["message"], OPAQUE)
        self.assertEqual(upd["agent_type"], "explorer")
        self.assertNotIn("model", upd)
        rec = self.subagents()["probe"]
        self.assertEqual((rec["tier"], rec["task_name"]), ("flash_lite", "probe"))

    def test_opaque_spawn_without_role_is_pinned_to_worker(self):
        upd = self.spawn_probe()["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["agent_type"], "worker")
        self.assertEqual(self.subagents()["probe"]["tier"], "flash")

    def test_plaintext_v2_spawn_appends_report_format_without_model(self):
        upd = self.spawn_probe(message="Find where main is defined in this directory and report the file name.",
                               agent_type="explorer")["hookSpecificOutput"]["updatedInput"]
        self.assertIn("REPORT FORMAT", upd["message"])
        self.assertNotIn("model", upd)

    def test_opaque_followups_to_scout_are_denied(self):
        self.spawn_probe(agent_type="explorer")
        for tool in ("collaborationfollowup_task", "collaborationsend_message"):
            for target in ("probe", "/root/probe"):
                out = self.pre(tool, {"target": target, "message": OPAQUE})
                self.assertTrue(self.denied(out), (tool, target))
                self.assertIn("agent_type: \\\"worker\\\"", json.dumps(out))

    def test_opaque_followup_to_worker_or_unknown_is_allowed(self):
        self.spawn_probe(agent_type="worker")
        self.assertFalse(self.denied(self.pre("collaborationsend_message", {"target": "probe", "message": OPAQUE})))
        self.assertFalse(self.denied(self.pre("collaborationfollowup_task", {"target": "nobody", "message": OPAQUE})))

    def test_plaintext_followup_to_scout_is_checked(self):
        self.spawn_probe(agent_type="explorer")
        lookup = "Find where parse_header is defined and report the file path."
        self.assertFalse(self.denied(self.pre("collaborationfollowup_task", {"target": "probe", "message": lookup})))
        edit = "Edit a.txt and append the line epsilon."
        self.assertTrue(self.denied(self.pre("collaborationfollowup_task", {"target": "probe", "message": edit})))
        self.assertTrue(self.denied(self.pre("collaborationsend_message", {"target": "probe", "message": edit})))

    def test_post_tool_use_stores_agent_path(self):
        tool_input = {"task_name": "probe", "agent_type": "explorer", "fork_turns": "none", "message": OPAQUE}
        self.pre("collaborationspawn_agent", tool_input)
        self.post("collaborationspawn_agent", tool_input, tool_response='{"task_name":"/root/probe"}')
        self.assertEqual(self.subagents()["probe"]["agent_path"], "/root/probe")

    def test_failed_spawn_removes_its_record(self):
        tool_input = {"task_name": "probe", "agent_type": "explorer", "fork_turns": "none", "message": OPAQUE}
        self.pre("collaborationspawn_agent", tool_input)
        self.post("collaborationspawn_agent", tool_input, error="spawn failed")
        self.assertNotIn("probe", self.subagents())

    def test_child_hooks_keep_the_record(self):
        self.spawn_probe(agent_type="explorer")
        eo.handle_post_tool_use(self.payload("PostToolUse", tool_name="Bash", tool_input={"command": "ls -la"},
                                             tool_response="a.txt\n", **CHILD))
        eo.handle_subagent_stop(self.payload("SubagentStop", stop_hook_active=False,
                                             last_assistant_message="STATUS: COMPLETED", **CHILD))
        eo.handle_stop(self.payload("Stop", stop_hook_active=False, last_assistant_message="DONE"))
        self.assertEqual(self.subagents()["probe"]["tier"], "flash_lite")

    def test_explorer_is_read_only(self):
        patch = {"command": "*** Begin Patch\n*** Update File: a.txt\n@@\n+delta\n*** End Patch"}
        self.assertTrue(self.denied(self.pre("apply_patch", patch, **CHILD)))
        self.assertTrue(self.denied(self.pre("Bash", {"command": "echo delta >> a.txt"}, **CHILD)))
        self.assertTrue(self.denied(self.pre("Bash", {"command": "sed -i s/a/b/ a.txt"}, **CHILD)))
        self.assertFalse(self.denied(self.pre("Bash", {"command": "ls -la && wc -l a.txt"}, **CHILD)))
        worker = dict(CHILD, agent_type="worker")
        self.assertFalse(self.denied(self.pre("apply_patch", patch, **worker)))

    @staticmethod
    def matches(matcher, name):
        # Codex, like Claude Code: only [A-Za-z0-9_|] means an exact-name list, anything else a regex.
        if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
            return name in matcher.split("|")
        return re.search(matcher, name) is not None

    def test_codex_hooks_json_matcher_covers_collaboration_tools(self):
        data = json.loads((ROOT / "hooks" / "codex-hooks.json").read_text(encoding="utf-8"))
        pre = data["hooks"]["PreToolUse"][0]["matcher"]
        for name in ("collaborationspawn_agent", "collaborationfollowup_task", "collaborationsend_message", "apply_patch", "Bash"):
            self.assertTrue(self.matches(pre, name), name)
        post = data["hooks"]["PostToolUse"][0]["matcher"]
        for name in ("collaborationspawn_agent", "spawn_agent", "Bash"):
            self.assertTrue(self.matches(post, name), name)

    def test_debug_log_records_codex_tool_fields(self):
        os.environ["CODEX_MITOSIS_DEBUG"] = "1"
        p = self.payload("PreToolUse", tool_name="collaborationspawn_agent", tool_input={"task_name": "probe", "message": OPAQUE})
        eo._debug_log(p, "pre-tool-use", {})
        log = eo._state_file(p).with_name("mitosis_orchestrator_debug.log")
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((entry["tool"], entry["rawTool"]), ("spawn_agent", "collaborationspawn_agent"))
        self.assertEqual(entry["toolInputKeys"], ["message", "task_name"])


if __name__ == "__main__":
    unittest.main()
