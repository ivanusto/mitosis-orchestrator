#!/usr/bin/env python3
"""Runtime pin verification: the subagent transcript must show the model the hook pinned.
Run: python -m unittest tests.test_pin_verify -v"""

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
sys.path.insert(0, str(ROOT / "scripts"))
from mitosis_hook import pin_verify as pv  # noqa: E402

spec = importlib.util.spec_from_file_location("enforce_orchestrator_pin_verify", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

SCOUT_T = "mitosis-orchestrator:flash-lite-scout"
WORKER_T = "mitosis-orchestrator:flash-worker"
LOOKUP = "Locate where parse_token is defined."
WORK = "Run the tests and fix failures."
GOOD = "STATUS: COMPLETED\nFILES_CHANGED: NONE\nVERIFICATION: ran grep, exit 0\nNOTES: found it"
FAILED = ("STATUS: FAILED\nFILES_CHANGED: NONE\nVERIFICATION: ran grep, exit 1\n"
          "NOTES: could not do it, log file:///tmp/scratch/task.log")
OPAQUE = "gAAAAABq2W6q_XALFZgD1zKjI6St8hIilg6diYo5-ULKaJMEhHaMKEcdSxOmRa2b1_16hBGQv9tSsT-yYt4xtowdOA_fE7FIw96PB5VXsqtVMWeVsN8J_9wdsW49rMnfe9yNrXF8a8tipe-jXMH7_OFi1IT9rdtcvnDYiwyoN1jlPhnNo"
AID = "a1a1a1a1a1a1a1a1"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def asst(model):
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "content": []}}


class _EnvBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        _support.set_temp_env(os.environ, self.tmp.name)
        tempfile.tempdir = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        _support.release_state_locks()
        self.tmp.cleanup()

    def jsonl(self, records, name="t.jsonl"):
        path = Path(self.tmp.name) / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return str(path)


class UnitTest(unittest.TestCase):
    def test_model_matches(self):
        self.assertTrue(pv.model_matches("haiku", HAIKU))
        self.assertFalse(pv.model_matches("sonnet", HAIKU))
        self.assertFalse(pv.model_matches("", HAIKU))

    def test_pin_verdict(self):
        self.assertIsNone(pv.pin_verdict("inherit", [HAIKU])[0])
        self.assertIsNone(pv.pin_verdict("haiku", [])[0])
        self.assertTrue(pv.pin_verdict("haiku", [HAIKU])[0])
        self.assertFalse(pv.pin_verdict("haiku", [HAIKU, SONNET])[0])


class _ClaudeBase(_EnvBase):
    def setUp(self):
        super().setUp()
        art = Path(self.tmp.name) / "artifacts"
        art.mkdir()
        self.base = {"session_id": f"pin-{id(self)}", "artifactDirectoryPath": str(art), "cwd": self.tmp.name,
                     "permission_mode": "ask"}

    def tool(self, event, name, args, **extra):
        p = dict(self.base, hook_event_name=event, tool_name=name, tool_input=args)
        p.update(extra)
        return p

    def state(self):
        return eo._read_state_file(eo._state_file(self.base))

    def spawn(self, subagent_type=SCOUT_T, prompt=LOOKUP):
        args = {"prompt": prompt, "description": "x", "model": "inherit"}
        if subagent_type is not None:
            args["subagent_type"] = subagent_type
        out = eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", args))
        return args, out["hookSpecificOutput"]

    def stop(self, path=None, agent_type=SCOUT_T, msg=GOOD, agent_id=AID, **extra):
        p = dict(self.base, hook_event_name="SubagentStop", agent_id=agent_id, agent_type=agent_type,
                 last_assistant_message=msg)
        if path is not None:
            p["agent_transcript_path"] = path
        p.update(extra)
        return eo.handle_subagent_stop(p)

    def rec(self, agent_id=AID):
        return self.state()["subagents"][agent_id]

    def primary_stop(self, **extra):
        return eo.handle_stop(dict(self.base, hook_event_name="Stop", **extra))


class ClaudePinTest(_ClaudeBase):
    def test_1_match(self):
        self.spawn()
        out = self.stop(self.jsonl([{"type": "user"}, asst(HAIKU), {"type": "attachment"}, asst(HAIKU)]))
        self.assertNotIn("systemMessage", out)
        r = self.rec()
        self.assertEqual(r["pinned_model"], "haiku")
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_model"], HAIKU)

    def test_2_mismatch(self):
        self.spawn()
        out = self.stop(self.jsonl([asst(SONNET)]))
        self.assertIs(self.rec()["pin_verified"], False)
        self.assertNotIn("systemMessage", out)
        msg = self.primary_stop()["systemMessage"]
        self.assertIn("haiku", msg)
        self.assertIn(SONNET, msg)
        self.assertIn("scripts/mitosis-doctor.py", msg)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", msg)
        self.assertNotIn("systemMessage", self.primary_stop())

    def test_3_mixed(self):
        self.spawn()
        out = self.stop(self.jsonl([asst(HAIKU), asst(SONNET)]))
        self.assertIs(self.rec()["pin_verified"], False)
        self.assertNotIn("systemMessage", out)
        self.assertIn("systemMessage", self.primary_stop())

    def test_4_synthetic_ignored(self):
        self.assertEqual(pv.claude_transcript_models(self.jsonl([asst("<synthetic>")])), [])
        self.spawn()
        out = self.stop(self.jsonl([asst("<synthetic>")]))
        self.assertIsNone(self.rec()["pin_verified"])
        self.assertNotIn("systemMessage", out)
        self.spawn()
        self.stop(self.jsonl([asst("<synthetic>"), asst(HAIKU)], "t2.jsonl"), agent_id="b2b2b2b2b2b2b2b2")
        self.assertIs(self.rec("b2b2b2b2b2b2b2b2")["pin_verified"], True)

    def test_5_no_evidence(self):
        empty = Path(self.tmp.name) / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        bad = Path(self.tmp.name) / "bad.jsonl"
        bad.write_text('not json\n{"type": "assistant", "message":\n[1,2]\n\xff\xfe\n', encoding="utf-8")
        cases = [None, str(Path(self.tmp.name) / "missing.jsonl"), str(empty), str(bad)]
        for i, path in enumerate(cases):
            aid = f"c{i}" * 8
            self.spawn()
            out = self.stop(path, agent_id=aid)
            self.assertNotIn("systemMessage", out, path)
            self.assertIsNone(self.rec(aid)["pin_verified"], path)
            self.assertNotIn("actual_model", self.rec(aid))

    def test_6_primary_escalation_on_opus(self):
        os.environ["CLAUDE_MITOSIS_ESCALATE_PRIMARY"] = "1"
        eo.handle_session_start(dict(self.base, hook_event_name="SessionStart", model="claude-opus-5-5"))
        args, _ = self.spawn(WORKER_T, WORK)
        self.stop(self.jsonl([asst(SONNET)]), agent_type=WORKER_T, msg=FAILED)
        self.assertIn("escalation", self.state())
        _, pre = self.spawn(WORKER_T, WORK)
        self.assertEqual(pre["updatedInput"]["model"], "opus")
        pending = self.state()["pending_spawns"][-1]
        self.assertEqual(pending["pinned_model"], "opus")
        aid = "d4d4d4d4d4d4d4d4"
        out = self.stop(self.jsonl([asst("claude-opus-5-5")], "o.jsonl"), agent_type=WORKER_T, agent_id=aid)
        self.assertIs(self.rec(aid)["pin_verified"], True)
        self.assertNotIn("systemMessage", out)

    def test_7_warns_once_and_coexists_with_block(self):
        self.spawn(SCOUT_T)
        path = self.jsonl([asst(SONNET)])
        first = self.stop(path, msg="no report here")
        self.assertEqual(first.get("decision"), "block")
        self.assertNotIn("systemMessage", first)
        second = self.stop(path, msg="no report here", stop_hook_active=True)
        self.assertNotIn("systemMessage", second)
        self.assertNotEqual(second.get("decision"), "block")
        self.assertIs(self.rec()["pin_verified"], False)
        warned = self.primary_stop()
        self.assertIn(SONNET, warned["systemMessage"])
        self.assertEqual(warned["systemMessage"].count("was pinned to"), 1)
        self.assertNotIn("systemMessage", self.primary_stop())

    def test_7b_warning_coexists_with_worktree_block_on_stop(self):
        eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", {
            "prompt": "Fix other bug in branch", "description": "x", "subagent_type": WORKER_T,
            "model": "inherit", "isolation": "worktree"}))
        self.spawn(SCOUT_T)
        aid = "b3b3b3b3b3b3b3b3"
        self.stop(self.jsonl([asst(SONNET)]), agent_id=aid)
        self.assertIs(self.rec(aid)["pin_verified"], False)
        out = self.primary_stop()
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("worktrees are still active", out["reason"])
        self.assertIn("scripts/mitosis-doctor.py", out["systemMessage"])
        self.assertIn(SONNET, out["systemMessage"])
        self.assertNotIn("systemMessage", self.primary_stop())

    def test_7c_stop_hook_active_keeps_warning(self):
        self.spawn()
        self.stop(self.jsonl([asst(SONNET)]))
        self.assertEqual(self.primary_stop(stop_hook_active=True), {})
        self.assertEqual(len(self.state()["pin_warnings"]), 1)
        self.assertIn("systemMessage", self.primary_stop())
        self.assertNotIn("pin_warnings", self.state())

    def test_8_later_no_evidence_keeps_verdict(self):
        for model, want in ((HAIKU, True), (SONNET, False)):
            aid = ("e1" if want else "e2") * 8
            self.spawn()
            self.stop(self.jsonl([asst(model)], f"{want}.jsonl"), agent_id=aid)
            self.stop(None, agent_id=aid, stop_hook_active=True)
            self.stop(str(Path(self.tmp.name) / "gone.jsonl"), agent_id=aid, stop_hook_active=True)
            self.assertIs(self.rec(aid)["pin_verified"], want)
            self.assertEqual(self.rec(aid)["actual_model"], model)

    def test_9_unpinned_records_have_no_verdict(self):
        path = self.jsonl([asst(SONNET)])
        # explorer built-in / fork: no record at all
        for aid, atype in (("f0f0f0f0f0f0f0f0", ""), ("e1e1e1e1e1e1e1e1", "Explore")):
            out = self.stop(path, agent_type=atype, agent_id=aid)
            self.assertNotIn("systemMessage", out)
            self.assertNotIn(aid, self.state().get("subagents", {}))
        # synthetic plugin-type record (no pending spawn): never pinned
        out = self.stop(path)
        self.assertNotIn("systemMessage", out)
        self.assertNotIn("pin_verified", self.rec())
        for r in list(self.state().get("subagents", {}).values()) + self.state().get("pending_spawns", []):
            self.assertNotIn("pin_verified", r)

    def test_10_disabled(self):
        os.environ["CLAUDE_MITOSIS_VERIFY_PIN"] = "0"
        self.spawn()
        out = self.stop(self.jsonl([asst(SONNET)]))
        self.assertNotIn("systemMessage", out)
        self.assertNotIn("pin_verified", self.rec())

    def test_11_tail_window_cut_line(self):
        filler = {"type": "user", "message": {"content": "x" * 4000}}
        lines = [json.dumps(asst(SONNET))] + [json.dumps(filler)] * 300  # ~1.2 MB, old sonnet falls outside
        lines.append(json.dumps(asst(HAIKU)))
        path = Path(self.tmp.name) / "big.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertGreater(path.stat().st_size, pv._TAIL_BYTES)
        self.assertEqual(pv.claude_transcript_models(str(path)), [HAIKU])
        # the cut lands inside the first kept line: it must be dropped, not crash or mis-parse
        cut = Path(self.tmp.name) / "cut.jsonl"
        pad = "y" * (pv._TAIL_BYTES - 200)
        head = json.dumps(dict(asst(SONNET), pad=pad))
        cut.write_text(head + "\n" + json.dumps(asst(HAIKU)) + "\n", encoding="utf-8")
        self.assertEqual(pv.claude_transcript_models(str(cut)), [HAIKU])
        self.spawn()
        out = self.stop(str(path))
        self.assertIs(self.rec()["pin_verified"], True)
        self.assertNotIn("systemMessage", out)


def handback(text):
    return {"type": "assistant", "message": {"model": SONNET, "role": "assistant", "content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}]}}


class _NoKeyMixin:
    def stop_nokey(self, path=None, agent_type=WORKER_T, agent_id=AID, **extra):
        p = dict(self.base, hook_event_name="SubagentStop", agent_id=agent_id, agent_type=agent_type)
        if path is not None:
            p["agent_transcript_path"] = path
        p.update(extra)
        return eo.handle_subagent_stop(p)


class HandbackTest(_NoKeyMixin, _ClaudeBase):
    def test_h1_valid_handback(self):
        self.spawn(WORKER_T, WORK)
        out = self.stop_nokey(self.jsonl([asst(SONNET), handback(GOOD)]))
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)

    def test_h2_bad_handback(self):
        self.spawn(WORKER_T, WORK)
        out = self.stop_nokey(self.jsonl([handback("all done, nothing to see")]))
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], False)

    def test_h3_failed_handback_escalates(self):
        self.spawn(SCOUT_T, LOOKUP)
        out = self.stop_nokey(self.jsonl([handback(FAILED)]), agent_type=SCOUT_T)
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)
        self.assertIn("escalation", self.state())
        _, pre = self.spawn(SCOUT_T, LOOKUP)
        self.assertEqual(pre["updatedInput"]["subagent_type"], WORKER_T)

    def test_h4_nothing_at_all(self):
        self.spawn(WORKER_T, WORK)
        for i, path in enumerate([None, str(Path(self.tmp.name) / "none.jsonl"), self.jsonl([asst(SONNET)])]):
            aid = f"{i + 1}9" * 8
            out = self.stop_nokey(path, agent_id=aid)
            self.assertNotEqual(out.get("decision"), "block")
            self.assertNotIn("report_ok", self.rec(aid))
        self.assertEqual(self.state().get("report_nagged", []), [])

    def test_h5_empty_key_blocks_once(self):
        self.spawn(WORKER_T, WORK)
        path = self.jsonl([asst(SONNET)])
        self.assertEqual(self.stop(path, agent_type=WORKER_T, msg="").get("decision"), "block")
        self.assertNotEqual(self.stop(path, agent_type=WORKER_T, msg="").get("decision"), "block")

    def test_h6_last_assistant_message_wins(self):
        self.spawn(WORKER_T, WORK)
        out = self.stop(self.jsonl([handback("garbage report")]), agent_type=WORKER_T, msg=GOOD)
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)
        self.spawn(WORKER_T, WORK)
        aid = "c7c7c7c7c7c7c7c7"
        out = self.stop(self.jsonl([handback(GOOD)], "h2.jsonl"), agent_type=WORKER_T, msg="garbage", agent_id=aid)
        self.assertEqual(out.get("decision"), "block")
        self.assertIs(self.rec(aid)["report_ok"], False)

    def test_h7_last_handback_wins_empty_ignored(self):
        path = self.jsonl([handback("old"), handback(GOOD), handback("  "), handback("")])
        self.assertEqual(pv.claude_handback_report(path), GOOD)
        self.assertEqual(pv.claude_handback_report(self.jsonl([handback("first"), handback(GOOD)], "b.jsonl")), GOOD)
        self.assertEqual(pv.claude_handback_report(self.jsonl([handback("")], "c.jsonl")), "")
        self.assertEqual(pv.claude_handback_report(None), "")
        self.spawn(WORKER_T, WORK)
        self.stop_nokey(self.jsonl([handback("bad"), handback(GOOD)], "d.jsonl"))
        self.assertIs(self.rec()["report_ok"], True)


class StoredHandbackTest(_NoKeyMixin, _ClaudeBase):
    def post(self, message, agent_id=AID, with_id=True, tool_name="SubagentHandback"):
        extra = {"agent_id": agent_id} if with_id else {}
        return eo.handle_post_tool_use(self.tool("PostToolUse", tool_name, {"message": message}, **extra))

    def stored(self):
        return self.state().get("handback_reports")

    def test_s1_stored_report_validates_with_empty_transcript(self):
        self.spawn(WORKER_T, WORK)
        self.assertEqual(self.post(GOOD), {})
        self.assertEqual(self.stored(), {AID: GOOD})
        out = self.stop_nokey(self.jsonl([], "empty.jsonl"))
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)
        self.assertNotIn(AID, self.stored() or {})

    def test_s2_stored_failed_escalates_without_transcript(self):
        self.spawn(SCOUT_T, LOOKUP)
        self.post(FAILED)
        out = self.stop_nokey(None, agent_type=SCOUT_T)
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)
        self.assertIn("escalation", self.state())

    def test_s3_cap_and_ignored_payloads(self):
        from mitosis_hook.handlers import HANDBACK_REPORT_CAP
        self.post("x" * (HANDBACK_REPORT_CAP + 500))
        self.assertEqual(len(self.stored()[AID]), HANDBACK_REPORT_CAP)
        bid = "b4b4b4b4b4b4b4b4"
        self.post("", agent_id=bid)
        self.post("  \n\t ", agent_id=bid)
        self.post(GOOD, with_id=False)
        self.post(GOOD, agent_id="  ")
        self.assertEqual(list(self.stored()), [AID])

    def test_s4_stored_beats_transcript(self):
        self.spawn(WORKER_T, WORK)
        self.post(GOOD)
        self.stop_nokey(self.jsonl([handback("garbage report")]))
        self.assertIs(self.rec()["report_ok"], True)

    def test_s5_other_agent_report_not_used(self):
        self.spawn(WORKER_T, WORK)
        other = "d5d5d5d5d5d5d5d5"
        self.post(GOOD, agent_id=other)
        self.stop_nokey(self.jsonl([handback("garbage report")]))
        self.assertIs(self.rec()["report_ok"], False)
        self.assertEqual(self.stored(), {other: GOOD})

    def test_s6_corrupt_handback_reports_does_not_raise(self):
        self.spawn(WORKER_T, WORK)
        path = eo._state_file(self.base)
        state = eo._read_state_file(path)
        state["handback_reports"] = ["not", "a", "dict"]
        Path(path).write_text(json.dumps(state), encoding="utf-8")
        out = self.stop_nokey(self.jsonl([handback(GOOD)]))
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)

    def test_s7_reanswer_corrects_report_ok(self):
        self.spawn(WORKER_T, WORK)
        self.assertEqual(self.stop(agent_type=WORKER_T, msg="garbage").get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], False)
        out = self.stop(agent_type=WORKER_T, msg=GOOD, stop_hook_active=True)
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], True)

    def test_s7b_bad_reanswer_never_blocks_again(self):
        self.spawn(WORKER_T, WORK)
        self.stop(agent_type=WORKER_T, msg="garbage")
        out = self.stop(agent_type=WORKER_T, msg="still garbage", stop_hook_active=True)
        self.assertNotEqual(out.get("decision"), "block")
        self.assertIs(self.rec()["report_ok"], False)

    def test_s7c_failed_reanswer_escalates_once(self):
        self.spawn(SCOUT_T, LOOKUP)
        self.assertEqual(self.stop(msg="garbage").get("decision"), "block")
        self.assertNotIn("escalation", self.state())
        out = self.stop(msg=FAILED, stop_hook_active=True)
        self.assertNotEqual(out.get("decision"), "block")
        esc = self.state()["escalation"]
        self.assertEqual(esc["source"], AID)
        self.assertTrue(self.rec()["escalation_sent"])
        self.stop(msg=FAILED, stop_hook_active=True)
        self.assertEqual(self.state()["escalation"], esc)

    def test_s8_post_handback_leaves_counters(self):
        self.spawn(WORKER_T, WORK)
        before = self.state()
        self.post(FAILED)
        after = self.state()
        self.assertEqual(after.get("consecutive_failures"), before.get("consecutive_failures"))
        self.assertEqual(after.get("pending_spawns"), before.get("pending_spawns"))

    def test_s9_hooks_json_matches_subagent_handback(self):
        hooks = json.loads((ROOT / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))["hooks"]
        matchers = [g.get("matcher", "") for g in hooks["PostToolUse"]]
        self.assertTrue(any("SubagentHandback" in m.split("|") for m in matchers), matchers)


SCOUT_M = eo.DEFAULT_CODEX_SCOUT_MODEL
WORKER_M = eo.DEFAULT_CODEX_WORKER_MODEL
OLD_M = "gpt-5.6-terra"  # a user-set older primary


class CodexPinTest(_EnvBase):
    UUID = "01a161ae-4d72-74f2-be05-d8fa31145c60"

    def setUp(self):
        super().setUp()
        self.sid = f"01a16158-{os.getpid():04x}-{id(self) & 0xffff:04x}"

    def payload(self, event, **extra):
        p = {"session_id": self.sid, "transcript_path": "/nonexistent/rollout.jsonl", "cwd": "/tmp",
             "hook_event_name": event, "model": SCOUT_M, "permission_mode": "bypassPermissions",
             "turn_id": "01a15f91-1b97-7e53-a469-8d0ca70908aa"}
        p.update(extra)
        return p

    def spawn(self, role="explorer"):
        ti = {"task_name": "probe", "fork_turns": "none", "message": OPAQUE, "agent_type": role}
        eo.handle_pre_tool_use(self.payload("PreToolUse", tool_name="collaborationspawn_agent", tool_input=ti,
                                            tool_use_id="call_1"))

    def rollout(self, last_model, bound=True, parent=(OLD_M, "high"), child_effort="low", role="explorer"):
        """A child rollout: the parent's turn_context copy (None for no copy), then the child's own."""
        meta = {"id": self.UUID, "thread_source": "subagent", "agent_role": role}
        if bound:
            meta["agent_path"] = "/root/probe"

        def ctx(model, effort):
            payload = {"model": model, "cwd": "/tmp", "approval_policy": "never"}
            if effort is not None:
                payload["effort"] = effort
            return {"type": "turn_context", "payload": payload}

        recs = [{"type": "session_meta", "payload": meta}, {"type": "session_meta", "payload": dict(meta)}]
        if parent is not None:
            recs += [ctx(*parent), {"type": "event_msg"}]
        recs.append(ctx(last_model, child_effort))
        return self.jsonl(recs, "rollout.jsonl")

    def stop(self, path, role="explorer"):
        p = self.payload("SubagentStop", stop_hook_active=False, agent_id=self.UUID, agent_type=role,
                         last_assistant_message=GOOD, agent_transcript_path=path)
        p.pop("transcript_path")
        return eo.handle_subagent_stop(p)

    def rec(self):
        return eo._read_state_file(eo._state_file(self.payload("Stop")))["subagents"]["probe"]

    def worker_stop(self, last_model, **kw):
        self.spawn("worker")
        self.stop(self.rollout(last_model, role="worker", **kw), role="worker")
        return self.rec()

    def test_12_match_ignores_parent_context(self):
        self.spawn()
        self.stop(self.rollout(SCOUT_M))
        r = self.rec()
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_model"], SCOUT_M)

    def test_13_mismatch_no_system_message(self):
        self.spawn()
        out = self.stop(self.rollout(OLD_M))
        self.assertIs(self.rec()["pin_verified"], False)
        self.assertNotIn("systemMessage", json.dumps(out))
        self.assertNotIn("systemMessage", out)
        state = eo._read_state_file(eo._state_file(self.payload("Stop")))
        self.assertFalse(state.get("pin_warnings"))
        self.assertNotIn("systemMessage", eo.handle_stop(self.payload("Stop", stop_hook_active=False)))

    def test_14_unbound_and_disabled(self):
        self.spawn()
        # no agent_path and a second same-role record makes the child unbindable
        ti = {"task_name": "two", "fork_turns": "none", "message": OPAQUE, "agent_type": "explorer"}
        eo.handle_pre_tool_use(self.payload("PreToolUse", tool_name="collaborationspawn_agent", tool_input=ti,
                                            tool_use_id="call_2"))
        self.stop(self.rollout(OLD_M, bound=False))
        subs = eo._read_state_file(eo._state_file(self.payload("Stop")))["subagents"]
        for r in subs.values():
            self.assertNotIn("pin_verified", r)

    def test_14b_disabled(self):
        os.environ["CODEX_MITOSIS_VERIFY_PIN"] = "0"
        self.spawn()
        self.stop(self.rollout(OLD_M))
        self.assertNotIn("pin_verified", self.rec())

    def test_15_same_model_working_role(self):
        # primary and worker share a model; the role set a different effort than the parent's
        r = self.worker_stop(WORKER_M, parent=(WORKER_M, "high"), child_effort="medium")
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_effort"], "medium")
        self.assertNotIn("pin_note", r)

    def test_16_same_model_inherited(self):
        # the child carries the parent's model and effort, not the pinned effort
        r = self.worker_stop(WORKER_M, parent=(WORKER_M, "high"), child_effort="high")
        self.assertIs(r["pin_verified"], False)
        self.assertIn("inherited", r["pin_note"])

    def test_17_equal_to_expected_and_parent_is_unknown(self):
        r = self.worker_stop(WORKER_M, parent=(WORKER_M, "medium"), child_effort="medium")
        self.assertIsNone(r["pin_verified"])
        self.assertNotIn("pin_note", r)

    def test_17b_indistinguishable_keeps_earlier_evidence(self):
        rec = {"pin_verified": True, "actual_model": WORKER_M}
        turns = ((WORKER_M, "medium"), (WORKER_M, "medium"), 2)
        self.assertIsNone(pv.record_codex_pin_verdict(rec, WORKER_M, "medium", turns))
        self.assertIs(rec["pin_verified"], True)
        fresh = {}
        pv.record_codex_pin_verdict(fresh, WORKER_M, "medium", turns)
        self.assertIn("pin_verified", fresh)
        self.assertIsNone(fresh["pin_verified"])

    def test_18_older_primary_with_new_worker(self):
        r = self.worker_stop(WORKER_M, parent=(OLD_M, "medium"), child_effort="medium")
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_model"], WORKER_M)

    def test_19_one_turn_context_uses_model_only(self):
        # parent unknown: even an effort that is not the pinned one stays a match
        r = self.worker_stop(WORKER_M, parent=None, child_effort="high")
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_effort"], "high")

    def test_20_no_effort_in_rollout_uses_model_only(self):
        r = self.worker_stop(WORKER_M, parent=(WORKER_M, None), child_effort=None)
        self.assertIs(r["pin_verified"], True)
        self.assertNotIn("actual_effort", r)

    def test_21_effort_difference_alone_is_not_a_mismatch(self):
        # differs from the parent, and from the pinned effort too: info only
        r = self.worker_stop(WORKER_M, parent=(WORKER_M, "high"), child_effort="low")
        self.assertIs(r["pin_verified"], True)
        self.assertEqual(r["actual_effort"], "low")

    def test_22_model_mismatch_stays_false_with_same_efforts(self):
        r = self.worker_stop(OLD_M, parent=(OLD_M, "high"), child_effort="medium")
        self.assertIs(r["pin_verified"], False)

    def test_23_rollout_turns_first_last_and_count(self):
        path = self.jsonl([
            {"type": "turn_context", "payload": {"model": "a", "effort": "High"}}, {"type": "event_msg"},
            {"type": "turn_context", "payload": {"model": "b"}},
            {"type": "turn_context", "payload": {"effort": "low"}}])
        self.assertEqual(pv.codex_rollout_turns(path), (("a", "high"), ("b", ""), 2))
        self.assertEqual(pv.codex_rollout_turns("/nonexistent"), (None, None, 0))
        self.assertEqual(pv.codex_rollout_models(path), ["b"])

    def big_rollout(self, records_before, records_after):
        """parent copy, a line larger than the window, then the child's own turn_contexts."""
        pad = {"type": "event_msg", "pad": "x" * (pv._TAIL_BYTES + 100000)}

        def ctx(model, effort):
            return {"type": "turn_context", "payload": {"model": model, "effort": effort}}

        path = self.jsonl([ctx(*c) for c in records_before] + [pad] + [ctx(*c) for c in records_after], "big-rollout.jsonl")
        self.assertGreater(Path(path).stat().st_size, pv._TAIL_BYTES)
        return path

    def test_25_cut_window_does_not_look_inherited_and_keeps_earlier_true(self):
        path = self.big_rollout([(OLD_M, "high")], [(WORKER_M, "high"), (WORKER_M, "high")])
        turns = pv.codex_rollout_turns(path)
        self.assertEqual((turns[0], turns[1], turns[2]), (None, (WORKER_M, "high"), 2))
        rec = {"pin_verified": True, "actual_model": WORKER_M}
        self.assertIs(pv.record_codex_pin_verdict(rec, WORKER_M, "medium", turns), True)
        self.assertIs(rec["pin_verified"], True)
        self.assertNotIn("pin_note", rec)

    def test_25b_cut_window_first_stop_is_model_only_not_unknown(self):
        path = self.big_rollout([(OLD_M, "high")], [(WORKER_M, "medium"), (WORKER_M, "medium")])
        rec = {}
        self.assertIs(pv.record_codex_pin_verdict(rec, WORKER_M, "medium", pv.codex_rollout_turns(path)), True)
        self.assertIs(rec["pin_verified"], True)

    def test_25c_whole_file_keeps_the_parent_rule(self):
        path = self.jsonl([{"type": "turn_context", "payload": {"model": WORKER_M, "effort": "high"}},
                           {"type": "turn_context", "payload": {"model": WORKER_M, "effort": "high"}}])
        rec = {}
        self.assertIs(pv.record_codex_pin_verdict(rec, WORKER_M, "medium", pv.codex_rollout_turns(path)), False)
        self.assertIn("inherited", rec["pin_note"])

    def test_26_a_later_stop_on_another_model_clears_the_stale_note(self):
        rec = {}
        pv.record_codex_pin_verdict(rec, WORKER_M, "medium", ((WORKER_M, "high"), (WORKER_M, "high"), 2))
        self.assertIn("inherited", rec["pin_note"])
        pv.record_codex_pin_verdict(rec, WORKER_M, "medium", ((OLD_M, "high"), (OLD_M, "high"), 2))
        self.assertIs(rec["pin_verified"], False)
        self.assertEqual(rec["actual_model"], OLD_M)
        self.assertNotIn("pin_note", rec)
        claude = {"pin_verified": False, "pin_note": "looks inherited: stale"}
        pv.record_pin_verdict(claude, "haiku", [HAIKU])
        self.assertIs(claude["pin_verified"], True)
        self.assertNotIn("pin_note", claude)

    def test_24_unknown_is_counted_as_unknown_in_heartbeat(self):
        from mitosis_hook import handlers
        seen = []
        orig = handlers.heartbeat.note
        handlers.heartbeat.note = lambda **kw: seen.append(kw)
        try:
            handlers._observe_stop_record({"pin_verified": None, "pinned_model": WORKER_M})
        finally:
            handlers.heartbeat.note = orig
        self.assertEqual(seen, [{"pin": "unknown"}])


if __name__ == "__main__":
    unittest.main()
