#!/usr/bin/env python3
"""Failure escalation tests: scout FAILED/BLOCKED -> next dispatch on the worker.
Run: python -m unittest tests.test_escalation -v"""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("enforce_orchestrator_escalation", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

SCOUT_T = "mitosis-orchestrator:flash-lite-scout"
WORKER_T = "mitosis-orchestrator:flash-worker"
LOOKUP = "Locate where parse_token is defined."
WORK = "Run the tests and fix failures."
OPAQUE = (
    "gAAAAABq2W6q_XALFZgD1zKjI6St8hIilg6diYo5-ULKaJMEhHaMKEcdSxOmRa2b1_16hBGQv9tSsT-yYt4xtowdOA_"
    "fE7FIw96PB5VXsqtVMWeVsN8J_9wdsW49rMnfe9yNrXF8a8tipe-jXMH7_OFi1IT9rdtcvnDYiwyoN1jlPhnNo"
)


def report(status):
    return f"STATUS: {status}\nFILES_CHANGED: NONE\nVERIFICATION: ran grep, exit 1\nNOTES: could not do it, log file:///tmp/scratch/task.log"


class _EnvBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TMPDIR"] = self.tmp.name
        tempfile.tempdir = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        self.tmp.cleanup()


class _ClaudeBase(_EnvBase):
    def setUp(self):
        super().setUp()
        art = Path(self.tmp.name) / "artifacts"
        art.mkdir()
        self.base = {"session_id": f"esc-{id(self)}", "artifactDirectoryPath": str(art), "cwd": self.tmp.name,
                     "permission_mode": "ask"}
        self.n = 0

    def tool(self, event, name, args, **extra):
        p = dict(self.base, hook_event_name=event, tool_name=name, tool_input=args)
        p.update(extra)
        return p

    def state(self):
        return eo._read_state_file(eo._state_file(self.base))

    def spawn(self, subagent_type=SCOUT_T, prompt=LOOKUP):
        args = {"prompt": prompt, "description": "x", "subagent_type": subagent_type, "model": "inherit"}
        out = eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", args))
        return args, out["hookSpecificOutput"]

    def finish(self, args, status, upd=None):
        """PostToolUse for a spawned Agent whose report carries `status`."""
        self.n += 1
        aid = f"a{self.n:016x}"
        args = dict(args, **(upd or {}))
        resp = f"{report(status)}\nagentId: {aid} (for resuming)"
        return eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", args, tool_response=resp))

    def spawn_and_finish(self, status, subagent_type=SCOUT_T, prompt=LOOKUP):
        args, pre = self.spawn(subagent_type, prompt)
        return pre, self.finish(args, status, pre.get("updatedInput"))


class ClaudeEscalationTest(_ClaudeBase):
    def test_scout_blocked_sets_escalation_and_context(self):
        _, post = self.spawn_and_finish("BLOCKED")
        ctx = post["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(post["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("BLOCKED", ctx)
        self.assertIn("flash-worker", ctx)
        self.assertIn("instead of doing it yourself", ctx)
        esc = self.state()["escalation"]
        self.assertEqual((esc["from"], esc["to"], esc["spawns_left"]), ("flash_lite", "flash", 3))
        self.assertTrue(esc["source"])

    def test_completed_scout_does_not_escalate(self):
        _, post = self.spawn_and_finish("COMPLETED")
        self.assertEqual(post, {})
        self.assertNotIn("escalation", self.state())

    def test_next_scout_upgraded_logged_and_consumed(self):
        os.environ["CLAUDE_MITOSIS_DEBUG"] = "1"
        self.spawn_and_finish("FAILED")
        args, pre = self.spawn()
        upd = pre["updatedInput"]
        self.assertEqual(upd["model"], "sonnet")
        self.assertEqual(upd["subagent_type"], WORKER_T)
        self.assertEqual(upd["effort"], "medium")
        self.assertIn("re-dispatched on the worker", pre["permissionDecisionReason"])
        pend = self.state()["pending_spawns"][-1]
        self.assertTrue(pend["escalated"])
        self.assertEqual(pend["tier"], "flash")
        self.assertNotIn("escalation", self.state())
        pre_payload = self.tool("PreToolUse", "Agent", args)
        eo._debug_log(pre_payload, "pre-tool-use", {})
        log = eo._state_file(self.base).with_name("mitosis_orchestrator_debug.log")
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("escalation", [d["rule"] for d in entry["decisions"]])
        # consumed: the following scout stays a scout
        _, pre2 = self.spawn()
        self.assertEqual(pre2["updatedInput"]["model"], "haiku")

    def test_type_namespace_follows_caller(self):
        def upgraded(subagent_type):
            self.spawn_and_finish("FAILED")
            args = {"prompt": LOOKUP, "description": "x", "model": "inherit"}
            if subagent_type is not None:
                args["subagent_type"] = subagent_type
            out = eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", args))["hookSpecificOutput"]
            return out["updatedInput"]
        upd = upgraded(None)
        self.assertEqual((upd["model"], upd["effort"]), ("sonnet", "medium"))
        self.assertNotIn("subagent_type", upd)
        self.assertEqual(upgraded(SCOUT_T)["subagent_type"], WORKER_T)
        upd = upgraded("flash-lite-scout")
        self.assertEqual((upd["subagent_type"], upd["model"]), ("flash-worker", "sonnet"))

    def test_decision_reason_mentions_source(self):
        self.spawn_and_finish("BLOCKED")
        src = self.state()["escalation"]["source"]
        _, pre = self.spawn()
        self.assertIn(src, pre["permissionDecisionReason"])

    def test_failure_of_escalated_worker_does_not_escalate(self):
        self.spawn_and_finish("FAILED")
        args, pre = self.spawn()
        post = self.finish(args, "FAILED", pre["updatedInput"])
        self.assertEqual(post, {})
        self.assertNotIn("escalation", self.state())

    def test_worker_failure_does_nothing_by_default(self):
        _, post = self.spawn_and_finish("FAILED", WORKER_T, WORK)
        self.assertEqual(post, {})
        self.assertNotIn("escalation", self.state())

    def test_primary_escalation_uses_primary_model(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        eo.handle_session_start(dict(self.base, hook_event_name="SessionStart", model="claude-fable-5"))
        _, post = self.spawn_and_finish("FAILED", WORKER_T, WORK)
        self.assertIn("primary", post["hookSpecificOutput"]["additionalContext"])
        esc = self.state()["escalation"]
        self.assertEqual((esc["from"], esc["to"]), ("flash", "primary"))
        args, pre = self.spawn(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "fable")
        self.assertEqual(pre["updatedInput"]["subagent_type"], WORKER_T)
        self.assertTrue(self.state()["pending_spawns"][-1]["escalated"])
        self.assertNotIn("escalation", self.state())
        # the primary-model retry failing does not escalate again
        self.assertEqual(self.finish(args, "FAILED", pre["updatedInput"]), {})
        self.assertNotIn("escalation", self.state())

    def test_primary_unknown_model_defaults_to_opus(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        os.environ["CLAUDE_SETTINGS_PATH"] = str(Path(self.tmp.name) / "none.json")
        self.spawn_and_finish("BLOCKED", WORKER_T, WORK)
        _, pre = self.spawn(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "opus")

    def test_primary_scout_failure_still_goes_to_worker(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        self.spawn_and_finish("FAILED")
        self.assertEqual(self.state()["escalation"]["to"], "flash")

    def test_off_switch(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE"] = "0"
        _, post = self.spawn_and_finish("FAILED")
        self.assertEqual(post, {})
        self.assertNotIn("escalation", self.state())
        # a leftover escalation is also ignored
        os.environ["CLAUDE_MITOSIS_ESCALATE"] = "1"
        self.spawn_and_finish("FAILED")
        os.environ["CLAUDE_MITOSIS_ESCALATE"] = "0"
        _, pre = self.spawn()
        self.assertEqual(pre["updatedInput"]["model"], "haiku")

    def test_agy_style_env_name_disables(self):
        os.environ["AGY_MITOSIS_ESCALATE"] = "0"
        _, post = self.spawn_and_finish("FAILED")
        self.assertEqual(post, {})

    def test_user_prompt_submit_does_not_clear(self):
        self.spawn_and_finish("FAILED")
        eo.handle_pre_invocation(dict(self.base, hook_event_name="UserPromptSubmit", prompt="next"))
        self.assertIn("escalation", self.state())
        _, pre = self.spawn()
        self.assertEqual(pre["updatedInput"]["model"], "sonnet")

    def test_time_expiry(self):
        self.spawn_and_finish("FAILED")
        st = self.state()
        st["escalation"]["ts"] = time.time() - 31 * 60
        eo.save_state(self.base, st)
        _, pre = self.spawn()
        self.assertEqual(pre["updatedInput"]["model"], "haiku")
        self.assertNotIn("escalation", self.state())

    def test_spawns_left_expiry_for_primary(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        self.spawn_and_finish("FAILED", WORKER_T, WORK)
        st = self.state()
        st["escalation"]["spawns_left"] = 2
        eo.save_state(self.base, st)
        self.spawn()  # scout spawn passes through and uses one
        self.assertEqual(self.state()["escalation"]["spawns_left"], 1)
        self.spawn()
        self.assertNotIn("escalation", self.state())
        _, pre = self.spawn(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "sonnet")

    def test_next_spawn_already_flash_clears(self):
        self.spawn_and_finish("FAILED")
        _, pre = self.spawn(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "sonnet")
        self.assertNotIn("escalation", self.state())
        self.assertNotIn("escalated", self.state()["pending_spawns"][-1])
        _, pre2 = self.spawn()
        self.assertEqual(pre2["updatedInput"]["model"], "haiku")

    def test_foreign_agent_type_ignored(self):
        args = {"prompt": LOOKUP, "description": "x", "subagent_type": "Explore", "model": "haiku"}
        post = eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", args,
                                                 tool_response=report("FAILED") + "\nagentId: abcdef0123456789"))
        self.assertEqual(post, {})
        self.assertNotIn("escalation", self.state())

    def test_report_template_text_does_not_trigger(self):
        args, pre = self.spawn()
        resp = "STATUS: COMPLETED | BLOCKED | FAILED\nagentId: abcdef0123456789"
        post = eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", pre["updatedInput"], tool_response=resp))
        self.assertEqual(post, {})


class ClaudeAsyncEscalationTest(_ClaudeBase):
    """Agent calls are async: PostToolUse only carries the agentId, the report arrives in SubagentStop."""

    def launch(self, subagent_type=SCOUT_T, prompt=LOOKUP):
        args, pre = self.spawn(subagent_type, prompt)
        self.n += 1
        aid = f"b{self.n:016x}"
        resp = {"isAsync": True, "status": "async_launched", "agentId": aid, "description": "x"}
        post = eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", pre["updatedInput"], tool_response=resp))
        return aid, pre, post

    def stop(self, aid, status, agent_type=SCOUT_T, **extra):
        p = dict(self.base, hook_event_name="SubagentStop", agent_id=aid, agent_type=agent_type,
                 last_assistant_message=report(status) if status else "", stop_hook_active=False)
        p.update(extra)
        return eo.handle_subagent_stop(p)

    def test_async_post_registers_but_does_not_escalate(self):
        aid, _, post = self.launch()
        self.assertEqual(post, {})
        self.assertEqual(self.state()["subagents"][aid]["tier"], "flash_lite")
        self.assertNotIn("escalation", self.state())

    def test_subagent_stop_blocked_escalates(self):
        aid, _, _ = self.launch()
        self.assertEqual(self.stop(aid, "BLOCKED"), {})
        esc = self.state()["escalation"]
        self.assertEqual((esc["from"], esc["to"], esc["source"]), ("flash_lite", "flash", aid))
        _, pre = self.spawn()
        self.assertEqual(pre["updatedInput"]["model"], "sonnet")
        self.assertIn(aid, pre["permissionDecisionReason"])

    def test_completed_does_not_escalate(self):
        aid, _, _ = self.launch()
        self.stop(aid, "COMPLETED")
        self.assertNotIn("escalation", self.state())

    def test_worker_and_foreign_agents_do_not_escalate(self):
        aid, _, _ = self.launch(WORKER_T, WORK)
        self.stop(aid, "FAILED", WORKER_T)
        self.assertNotIn("escalation", self.state())
        self.stop("c" * 16, "FAILED", "Explore")
        self.assertNotIn("escalation", self.state())

    def test_malformed_report_blocks_and_does_not_escalate(self):
        aid, _, _ = self.launch()
        res = self.stop(aid, None, last_assistant_message="STATUS: FAILED\nRESULT: nope")
        self.assertEqual(res["decision"], "block")
        self.assertNotIn("escalation", self.state())

    def test_double_trigger_escalates_once(self):
        args, pre = self.spawn()
        aid = "d" * 16
        resp = report("FAILED") + f"\nagentId: {aid} (for resuming)"
        post = eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", pre["updatedInput"], tool_response=resp))
        self.assertIn("additionalContext", post["hookSpecificOutput"])
        st = self.state()
        st["escalation"]["spawns_left"] = 2  # a second trigger would reset it to 3
        eo.save_state(self.base, st)
        self.stop(aid, "FAILED")
        self.assertEqual(self.state()["escalation"]["spawns_left"], 2)

    def test_stop_then_sync_post_escalates_once(self):
        aid, _, _ = self.launch()
        self.stop(aid, "FAILED")
        st = self.state()
        st["escalation"]["spawns_left"] = 2
        eo.save_state(self.base, st)
        post = eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", {"subagent_type": SCOUT_T},
                                                 tool_response=report("FAILED") + f"\nagentId: {aid}"))
        self.assertEqual(post, {})
        self.assertEqual(self.state()["escalation"]["spawns_left"], 2)

    def test_escalated_worker_stop_does_not_escalate(self):
        aid, _, _ = self.launch()
        self.stop(aid, "BLOCKED")
        aid2, pre, _ = self.launch()  # upgraded to the worker
        self.assertEqual(pre["updatedInput"]["subagent_type"], WORKER_T)
        self.assertTrue(self.state()["subagents"][aid2]["escalated"])
        self.stop(aid2, "FAILED", WORKER_T)
        self.assertNotIn("escalation", self.state())

    def test_primary_escalated_stop_does_not_escalate_again(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        aid, _, _ = self.launch(WORKER_T, WORK)
        self.stop(aid, "FAILED", WORKER_T)
        self.assertEqual(self.state()["escalation"]["to"], "primary")
        aid2, pre, _ = self.launch(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "opus")
        self.stop(aid2, "FAILED", WORKER_T)
        self.assertNotIn("escalation", self.state())


class CodexEscalationTest(_EnvBase):
    UUID = "01a161ae-4d72-74f2-be05-d8fa31145c60"

    def setUp(self):
        super().setUp()
        self.sid = f"01a16158-{os.getpid():04x}-{id(self) & 0xffff:04x}"

    def payload(self, event, **extra):
        p = {"session_id": self.sid, "transcript_path": "/nonexistent/rollout.jsonl", "cwd": "/tmp",
             "hook_event_name": event, "model": "gpt-5.6-luna", "permission_mode": "bypassPermissions",
             "turn_id": "01a15f91-1b97-7e53-a469-8d0ca70908aa"}
        p.update(extra)
        return p

    def spawn(self, name="probe", role="explorer"):
        out = eo.handle_pre_tool_use(self.payload(
            "PreToolUse", tool_name="collaborationspawn_agent", tool_use_id="c1",
            tool_input={"task_name": name, "fork_turns": "none", "message": OPAQUE, "agent_type": role}))
        return out["hookSpecificOutput"]["updatedInput"]

    def stop(self, msg, uuid=None, agent_type="explorer", active=False):
        p = self.payload("SubagentStop", stop_hook_active=active, agent_id=uuid or self.UUID,
                         agent_type=agent_type, last_assistant_message=msg)
        p.pop("transcript_path")
        return eo.handle_subagent_stop(p)

    def state(self):
        return eo._read_state_file(eo._state_file(self.payload("Stop")))

    def test_failed_explorer_report_rewrites_next_explorer_to_worker(self):
        self.spawn("probe", "explorer")
        self.assertEqual(self.stop(report("FAILED")), {})
        esc = self.state()["escalation"]
        self.assertEqual((esc["from"], esc["to"]), ("flash_lite", "flash"))
        upd = self.spawn("second", "explorer")
        self.assertEqual(upd["agent_type"], "worker")
        rec = self.state()["subagents"]["second"]
        self.assertEqual((rec["tier"], rec["escalated"]), ("flash", True))
        self.assertNotIn("escalation", self.state())
        # consumed: another explorer stays a scout
        self.assertEqual(self.spawn("third", "explorer")["agent_type"], "explorer")

    def test_blocked_report_also_escalates(self):
        self.spawn("probe", "explorer")
        self.stop(report("BLOCKED"))
        self.assertIn("escalation", self.state())

    def test_malformed_report_keeps_block_and_does_not_escalate(self):
        self.spawn("probe", "explorer")
        bad = "STATUS: FAILED\nRESULT: nope"
        res = self.stop(bad)
        self.assertEqual(res["decision"], "block")
        self.assertNotIn("escalation", self.state())
        self.assertEqual(self.stop(bad, active=True), {})
        self.assertNotIn("escalation", self.state())

    def test_completed_does_not_escalate(self):
        self.spawn("probe", "explorer")
        self.stop(report("COMPLETED"))
        self.assertNotIn("escalation", self.state())

    def test_escalated_worker_failure_does_not_escalate(self):
        self.spawn("probe", "explorer")
        self.stop(report("FAILED"))
        self.spawn("second", "explorer")
        self.stop(report("FAILED"), uuid="01a161ae-0000-74f2-be05-d8fa31145c61", agent_type="worker")
        self.assertNotIn("escalation", self.state())

    def test_worker_failure_default_off_and_primary_note_only(self):
        self.spawn("w", "worker")
        self.stop(report("FAILED"), agent_type="worker")
        self.assertNotIn("escalation", self.state())
        os.environ["CODEX_MITOSIS_ESCALATE_PRIMARY"] = "1"
        self.spawn("w2", "worker")
        self.stop(report("FAILED"), uuid="01a161ae-1111-74f2-be05-d8fa31145c62", agent_type="worker")
        self.assertEqual(self.state()["escalation"]["to"], "primary")
        out = eo.handle_pre_tool_use(self.payload(
            "PreToolUse", tool_name="collaborationspawn_agent", tool_use_id="c1",
            tool_input={"task_name": "w3", "fork_turns": "none", "message": OPAQUE, "agent_type": "worker"}))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["updatedInput"]["agent_type"], "worker")
        self.assertNotIn("model", hso["updatedInput"])
        self.assertIn("no per-spawn model", hso["permissionDecisionReason"])
        self.assertNotIn("escalation", self.state())

    def test_off_switch(self):
        os.environ["CODEX_MITOSIS_ESCALATE"] = "0"
        self.spawn("probe", "explorer")
        self.stop(report("FAILED"))
        self.assertNotIn("escalation", self.state())

    def test_user_prompt_submit_does_not_clear(self):
        self.spawn("probe", "explorer")
        self.stop(report("FAILED"))
        eo.handle_pre_invocation(self.payload("UserPromptSubmit", prompt="next"))
        self.assertIn("escalation", self.state())
        self.assertEqual(self.spawn("second", "explorer")["agent_type"], "worker")

    def test_next_worker_spawn_clears(self):
        self.spawn("probe", "explorer")
        self.stop(report("FAILED"))
        self.assertEqual(self.spawn("w", "worker")["agent_type"], "worker")
        self.assertNotIn("escalation", self.state())
        self.assertEqual(self.spawn("again", "explorer")["agent_type"], "explorer")


if __name__ == "__main__":
    unittest.main()
