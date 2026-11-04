#!/usr/bin/env python3
"""Opt-in pinning of Claude Code built-in agents (CLAUDE_MITOSIS_PIN_BUILTINS=1).
Run: python -m unittest tests.test_builtin_pin -v"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("enforce_orchestrator_builtin_pin", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)
hb = eo.heartbeat

SCOUT_T = "mitosis-orchestrator:flash-lite-scout"
LOOKUP = "Locate where parse_token is defined."
MUTATE = "Fix the bug in parse_token and edit the file."
FAILED = ("STATUS: FAILED\nFILES_CHANGED: NONE\nVERIFICATION: ran grep, exit 1\n"
          "NOTES: could not do it, log file:///tmp/scratch/task.log")
EXPLORE_ID = "e1e1e1e1e1e1e1e1"
GP_ID = "d4d4d4d4d4d4d4d4"
SCOUT_ID = "a1a1a1a1a1a1a1a1"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
SECRET = "SECRET-PROMPT-TEXT-77"


def asst(model):
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "content": []}}


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        _support.set_temp_env(os.environ, self.tmp.name)
        os.environ["MITOSIS_HEARTBEAT_DIR"] = str(Path(self.tmp.name) / "hb")
        tempfile.tempdir = None
        hb._PENDING.clear()
        hb._EVENTS_PENDING[0] = 0
        art = Path(self.tmp.name) / "artifacts"
        art.mkdir()
        self.base = {"session_id": f"bp-{id(self)}", "artifactDirectoryPath": str(art), "cwd": self.tmp.name,
                     "permission_mode": "ask"}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        hb._PENDING.clear()
        hb._EVENTS_PENDING[0] = 0
        _support.release_state_locks()
        self.tmp.cleanup()

    def tool(self, event, name, args, **extra):
        p = dict(self.base, hook_event_name=event, tool_name=name, tool_input=args)
        p.update(extra)
        return p

    def state(self):
        return eo._read_state_file(eo._state_file(self.base))

    def spawn(self, subagent_type, prompt=LOOKUP, **more):
        args = {"prompt": prompt, "description": "x", "model": "inherit"}
        if subagent_type is not None:
            args["subagent_type"] = subagent_type
        args.update(more)
        return args, eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", args))

    def stop(self, agent_id, agent_type, msg="plain text", path=None, **extra):
        p = dict(self.base, hook_event_name="SubagentStop", agent_id=agent_id, agent_type=agent_type,
                 last_assistant_message=msg, stop_hook_active=False)
        if path is not None:
            p["agent_transcript_path"] = path
        p.update(extra)
        return eo.handle_subagent_stop(p)

    def jsonl(self, records, name="t.jsonl"):
        path = Path(self.tmp.name) / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return str(path)

    def flag(self, on=True):
        os.environ["CLAUDE_MITOSIS_PIN_BUILTINS"] = "1" if on else "0"


class FlagOffTest(Base):
    def test_unset_and_zero_are_identical_and_untouched(self):
        for t in ("Explore", "Plan", "general-purpose"):
            args, a = self.spawn(t)
            os.environ["CLAUDE_MITOSIS_PIN_BUILTINS"] = "0"
            _, b = self.spawn(t)
            del os.environ["CLAUDE_MITOSIS_PIN_BUILTINS"]
            self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
            hso = a["hookSpecificOutput"]
            self.assertNotIn("updatedInput", hso)
            self.assertEqual(hso["permissionDecision"], "allow")
            self.assertEqual(hso["permissionDecisionReason"],
                             "Mitosis Orchestrator: built-in agent type '%s'; not pinned." % t)
        self.assertEqual(self.state().get("pending_spawns", []), [])


class ExploreTest(Base):
    def test_explore_pinned_to_scout_values(self):
        self.flag()
        args, out = self.spawn("Explore", MUTATE)
        upd = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["model"], "haiku")
        self.assertEqual(upd["effort"], "low")
        self.assertEqual(upd["prompt"], MUTATE)  # no REPORT FORMAT appended
        self.assertNotIn("REPORT FORMAT", json.dumps(upd))
        self.assertEqual(upd["subagent_type"], "Explore")
        pend = self.state()["pending_spawns"]
        self.assertEqual((pend[0]["type"], pend[0]["pinned_model"]), ("Explore", "haiku"))

    def test_scout_effort_env_and_pin_effort_off(self):
        self.flag()
        os.environ["CLAUDE_MITOSIS_SCOUT_EFFORT"] = "medium"
        _, out = self.spawn("Explore")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["effort"], "medium")
        os.environ["CLAUDE_MITOSIS_PIN_EFFORT"] = "0"
        _, out = self.spawn("Explore")
        self.assertNotIn("effort", out["hookSpecificOutput"]["updatedInput"])

    def test_explicit_model_honoured(self):
        self.flag()
        _, out = self.spawn("Explore", model="opus")
        self.assertNotIn("updatedInput", out["hookSpecificOutput"])
        self.assertEqual(self.state().get("pending_spawns", []), [])

    def test_plan_and_foreign_agents_untouched(self):
        self.flag()
        for t in ("Plan", "claude-code-guide", "other-plugin:agent"):
            _, out = self.spawn(t)
            self.assertNotIn("updatedInput", out["hookSpecificOutput"], t)

    def test_armed_escalation_survives_explore(self):
        self.flag()
        self.spawn(SCOUT_T)
        self.stop(SCOUT_ID, SCOUT_T, FAILED)
        esc = self.state()["escalation"]
        self.assertEqual(esc["to"], "flash")
        _, out = self.spawn("Explore")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")
        self.assertEqual(self.state()["escalation"], esc)

    def test_pin_verify_true_and_false(self):
        self.flag()
        self.spawn("Explore")
        self.stop(EXPLORE_ID, "Explore", path=self.jsonl([asst(HAIKU)]))
        rec = self.state()["subagents"][EXPLORE_ID]
        self.assertIs(rec["pin_verified"], True)
        self.spawn("Explore")
        out = self.stop("e2e2e2e2e2e2e2e2", "Explore", path=self.jsonl([asst(SONNET)], "u.jsonl"))
        self.assertNotEqual(out.get("decision"), "block")
        rec = self.state()["subagents"]["e2e2e2e2e2e2e2e2"]
        self.assertIs(rec["pin_verified"], False)
        self.assertEqual(rec["actual_model"], SONNET)

    def test_stop_never_blocks_or_escalates_even_failed_or_empty(self):
        self.flag()
        for i, msg in enumerate((FAILED, "no headers", "")):
            aid = "c%d" % i * 8
            self.spawn("Explore")
            payload = dict(self.base, hook_event_name="SubagentStop", agent_id=aid, agent_type="Explore",
                           last_assistant_message=msg, stop_hook_active=False,
                           permission_mode="auto", hook_event="SubagentStop")
            out = eo.handle_subagent_stop(payload)
            self.assertEqual(out, {}, repr(msg))
            self.assertNotIn("escalation", self.state())
            self.assertFalse(self.state()["subagents"][aid]["alive"])


class GeneralPurposeTest(Base):
    def test_routed_as_untyped_and_recorded_untyped(self):
        self.flag()
        _, out = self.spawn("general-purpose", LOOKUP)
        upd = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["model"], "haiku")
        self.assertIn("REPORT FORMAT", upd["prompt"])  # same as an untyped call
        _, ref = self.spawn(None, LOOKUP)
        self.assertEqual(ref["hookSpecificOutput"]["updatedInput"]["model"], upd["model"])
        pend = self.state()["pending_spawns"]
        self.assertEqual(pend[0]["type"], "")
        self.assertEqual(pend[0]["pinned_model"], "haiku")

    def test_worker_prompt_routes_to_worker_model(self):
        self.flag()
        _, out = self.spawn("general-purpose", MUTATE)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")

    def test_explicit_model_passthrough(self):
        self.flag()
        _, out = self.spawn("general-purpose", model="opus")
        self.assertNotIn("updatedInput", out["hookSpecificOutput"])

    def test_from_spawn_pairs_and_stop_escalates(self):
        self.flag()
        args, out = self.spawn("general-purpose", LOOKUP)
        resp = {"isAsync": True, "status": "async_launched", "agentId": GP_ID}
        eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", dict(args, **out["hookSpecificOutput"]["updatedInput"]),
                                          tool_response=resp))
        rec = self.state()["subagents"][GP_ID]
        self.assertEqual(rec["type"], "")
        self.assertEqual(rec["pinned_model"], "haiku")
        self.stop(GP_ID, "general-purpose", FAILED)
        self.assertEqual(self.state()["escalation"]["to"], "flash")

    def test_from_spawn_without_flag_still_unpaired(self):
        args, _ = self.spawn("general-purpose")
        resp = {"isAsync": True, "status": "async_launched", "agentId": GP_ID}
        eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", args, tool_response=resp))
        self.assertNotIn(GP_ID, self.state().get("subagents", {}))


class SendMessageGuardTest(Base):
    def test_guard_applies_to_pinned_explore(self):
        self.flag()
        args, out = self.spawn("Explore")
        resp = {"isAsync": True, "status": "async_launched", "agentId": EXPLORE_ID}
        eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", dict(args, **out["hookSpecificOutput"]["updatedInput"]),
                                          tool_response=resp))
        res = eo.handle_pre_tool_use(self.tool("PreToolUse", "SendMessage",
                                               {"to": EXPLORE_ID, "message": "Now edit parse.py and fix the bug, then commit."}))
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "deny")


class HeartbeatTest(Base):
    def test_counter_has_allowlisted_names_only(self):
        for i, t in enumerate(("Explore", "Explore", "Plan", "general-purpose", "claude-code-guide", "weird-" + SECRET,
                               "other-plugin:" + SECRET, SCOUT_T, "")):
            self.stop(("%02x" % (i + 16)) * 8, t, SECRET)
            eo.heartbeat.record_event(dict(self.base, hook_event_name="SubagentStop"), "subagent-stop")
        path = Path(self.tmp.name) / "hb" / "heartbeat-claude.json"
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["builtin_stops"], {"Explore": 2, "Plan": 1, "general-purpose": 1, "other": 1})
        self.assertNotIn(SECRET, raw)

    def test_note_rejects_unlisted_names(self):
        hb.note(builtin="whatever/" + SECRET)
        self.assertNotIn("builtin_stops", hb._PENDING)


if __name__ == "__main__":
    unittest.main()
