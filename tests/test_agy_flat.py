#!/usr/bin/env python3
"""agy 1.4.6 tests: flat invoke_subagent calls, Role excluded from routing, primary-side report scan.
Run: python -m unittest tests.test_agy_flat -v"""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enforce-orchestrator.py"
spec = importlib.util.spec_from_file_location("enforce_orchestrator_agy_flat", SCRIPT)
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

PARENT = "6c0f93da-2912-400b-9b41-a46eacc1bfa3"
SCOUT = "11111111-2222-4333-8444-555555555555"
WORKER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
GOOD = "STATUS: COMPLETED\nFILES_CHANGED: NONE\nVERIFICATION: grep found 2 hits\nNOTES: see file:///home/u/proj/a.py"
FAILED = ("STATUS: FAILED\nFILES_CHANGED: NONE\nVERIFICATION: grep failed\n"
          "NOTES: file:///home/u/brain/scratch/x.log")
BAD = "I looked around and found nothing."
ENV_KEYS = ("AGY_MITOSIS_ESCALATE", "CLAUDE_MITOSIS_ESCALATE", "CODEX_MITOSIS_ESCALATE",
            "AGY_MITOSIS_APPEND_REPORT_FORMAT", "AGY_MITOSIS_ESCALATE_PRIMARY")


class AgyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = Path(self.tmp.name) / "brain"
        self.brain.mkdir()
        self._env = dict(os.environ)
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def adir(self, cid):
        d = self.brain / cid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def primary(self):
        return {"artifactDirectoryPath": str(self.adir(PARENT)), "conversationId": PARENT,
                "modelName": "gemini-4-argon-high", "invocationNum": 1}

    def tool(self, name, args):
        return dict(self.primary(), toolCall={"name": name, "args": dict(args, toolAction="x", toolSummary="y")})

    def flat(self, prompt, type_name="flash-lite-scout", role="Mul Scout"):
        return self.tool("invoke_subagent", {"Prompt": prompt, "Role": role, "TypeName": type_name})

    def state(self):
        return eo.load_state(self.primary())

    def register(self, cid, type_name, role="r"):
        d = self.adir(PARENT) / ".system_generated" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.json" % cid)).write_text(json.dumps({
            "conversationId": cid, "subagentDescriptor": {"typeName": type_name, "role": role},
            "state": "SUBAGENT_STATE_ALIVE"}), encoding="utf-8")

    def deliver(self, sender, content, mid=None):
        d = self.adir(PARENT) / ".system_generated" / "messages"
        d.mkdir(parents=True, exist_ok=True)
        mid = mid or "msg-%s-%d" % (sender[:4], len(list(d.glob("*.json"))))
        (d / ("%s.json" % mid)).write_text(json.dumps({
            "id": mid, "recipient": PARENT, "sender": sender, "content": content}), encoding="utf-8")


class TestFlatInvokeSubagent(AgyBase):
    def test_scout_lookup_stays_flash_lite_with_model(self):
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        self.assertEqual(res["decision"], "allow")
        self.assertEqual(res["overwrite"]["Model"], "flash_lite")
        self.assertNotIn("TypeName", res["overwrite"])

    def test_scout_edit_task_becomes_flash_worker(self):
        res = eo.handle_pre_tool_use(self.flat("Add a function sub(a, b) to src/calc.py."))
        self.assertEqual(res["overwrite"]["Model"], "flash")
        self.assertEqual(res["overwrite"]["TypeName"], "flash-worker")

    def test_role_label_does_not_upgrade_lookup(self):
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined.", role="Add Scout"))
        self.assertEqual(res["overwrite"]["Model"], "flash_lite")
        self.assertNotIn("TypeName", res["overwrite"])

    def test_caller_key_spelling_kept(self):
        res = eo.handle_pre_tool_use(self.tool("invoke_subagent", {
            "prompt": "Find where foo is defined.", "Role": "r", "typeName": "flash-lite-scout"}))
        self.assertIn("prompt", res["overwrite"])
        self.assertNotIn("Prompt", res["overwrite"])
        self.assertEqual(res["overwrite"]["Model"], "flash_lite")

    def test_report_format_appended_to_prompt(self):
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        self.assertTrue(res["overwrite"]["Prompt"].startswith("Find where foo is defined."))
        self.assertIn("STATUS:", res["overwrite"]["Prompt"])

    def test_empty_prompt_denied(self):
        res = eo.handle_pre_tool_use(self.flat("   "))
        self.assertEqual(res["decision"], "deny")
        self.assertIn("empty Prompt", res["reason"])
        self.assertIn("Mul Scout", res["reason"])

    def test_pending_spawns_recorded(self):
        eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        eo.handle_pre_tool_use(self.flat("Add a function sub(a, b) to src/calc.py.", role="Sub Adder"))
        spawns = self.state()["pending_spawns"]
        self.assertEqual([(s["role"], s["type"], s["model"]) for s in spawns],
                         [("Mul Scout", "flash-lite-scout", "flash_lite"), ("Sub Adder", "flash-worker", "flash")])

    def test_list_form_still_works_and_ignores_role(self):
        res = eo.handle_pre_tool_use(self.tool("invoke_subagent", {"Subagents": [
            {"TypeName": "flash-lite-scout", "Role": "Add Scout", "Model": "inherit",
             "Prompt": "Find where foo is defined."}]}))
        self.assertEqual(res["overwrite"]["Subagents"][0]["Model"], "flash_lite")

    def test_decide_subagent_role_kept_by_default_dropped_for_agy(self):
        item = {"TypeName": "flash-lite-scout", "Role": "Add Scout", "Prompt": "Find where foo is defined."}
        self.assertEqual(eo.decide_subagent(item, use_role=False).tier, "flash_lite")
        self.assertEqual(eo.decide_subagent(dict(item, Role="Fix the parser and edit config.py")).tier, "flash")


class TestMessageScan(AgyBase):
    def setUp(self):
        super().setUp()
        self.register(SCOUT, "flash-lite-scout", role="Probe Scout")
        self.register(WORKER, "flash-worker", role="Builder")
        eo.save_state(self.primary(), {"seen_messages": []})  # not a first scan

    def test_first_scan_seeds_old_reports_then_new_failure_escalates(self):
        st = self.state()
        st.pop("seen_messages")
        eo.save_state(self.primary(), st)
        self.deliver(SCOUT, FAILED, mid="old1")
        eo.handle_pre_invocation(self.primary())
        st = self.state()
        self.assertNotIn("escalation", st)
        self.assertIn("old1", st["seen_messages"])
        self.deliver(SCOUT, FAILED, mid="new1")
        eo.handle_pre_invocation(self.primary())
        self.assertEqual(self.state()["escalation"]["to"], "flash")

    def test_failed_arms_escalation_and_next_flat_scout_escalated_once(self):
        self.deliver(SCOUT, FAILED)
        eo.handle_pre_invocation(self.primary())
        st = self.state()
        self.assertEqual(st["escalation"]["from"], "flash_lite")
        self.assertEqual(st["subagents"][SCOUT]["status"], "FAILED")
        self.assertIs(st["subagents"][SCOUT]["report_ok"], True)
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        self.assertEqual((res["overwrite"]["Model"], res["overwrite"]["TypeName"]), ("flash", "flash-worker"))
        self.assertIn("Probe Scout", res["reason"])
        self.assertNotIn("escalation", self.state())
        self.assertIs(self.state()["pending_spawns"][-1].get("escalated"), True)
        res2 = eo.handle_pre_tool_use(self.flat("Find where bar is defined."))
        self.assertEqual(res2["overwrite"]["Model"], "flash_lite")

    def test_scan_in_invoke_without_preinvocation(self):
        self.deliver(SCOUT, FAILED.replace("FAILED", "BLOCKED"))
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        self.assertEqual(res["overwrite"]["Model"], "flash")

    def test_completed_scout_does_not_escalate(self):
        self.deliver(SCOUT, GOOD)
        eo.handle_pre_invocation(self.primary())
        self.assertNotIn("escalation", self.state())

    def test_expires_after_30_minutes(self):
        self.deliver(SCOUT, FAILED)
        eo.handle_pre_invocation(self.primary())
        st = self.state()
        st["escalation"]["ts"] = time.time() - 31 * 60
        eo.save_state(self.primary(), st)
        res = eo.handle_pre_tool_use(self.flat("Find where foo is defined."))
        self.assertEqual(res["overwrite"]["Model"], "flash_lite")

    def test_switch_off(self):
        os.environ["AGY_MITOSIS_ESCALATE"] = "0"
        self.deliver(SCOUT, FAILED)
        eo.handle_pre_invocation(self.primary())
        self.assertNotIn("escalation", self.state())

    def test_invalid_report_notice_once_per_subagent(self):
        self.deliver(WORKER, BAD, mid="m1")
        res = eo.handle_pre_invocation(self.primary())
        msgs = [s["ephemeralMessage"] for s in res["injectSteps"]]
        self.assertEqual(len(msgs), 1)
        self.assertIn("Builder", msgs[0])
        self.assertIs(self.state()["subagents"][WORKER]["report_ok"], False)
        self.deliver(WORKER, BAD, mid="m2")
        self.assertEqual(eo.handle_pre_invocation(self.primary()), {})

    def test_valid_report_gives_no_notice(self):
        self.deliver(WORKER, GOOD)
        self.assertEqual(eo.handle_pre_invocation(self.primary()), {})


if __name__ == "__main__":
    unittest.main()
