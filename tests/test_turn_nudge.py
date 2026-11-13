#!/usr/bin/env python3
"""agy turn nudge and the agy policy notice. Run: python -m unittest discover tests"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import test_enforce_orchestrator as T  # noqa: E402
from mitosis_hook import handlers, models  # noqa: E402
from mitosis_hook.state import load_state, save_state  # noqa: E402

eo = T.eo
SUB = "11111111-1111-4111-8111-111111111111"
SUB2 = "22222222-2222-4222-8222-222222222222"


class TurnNudgeBase(T.HookBase):
    def setUp(self):
        super().setUp()
        self.payload.update(invocationNum=0, modelName="Gemini 4 Hydrogen (High)", lastUserInput="find the root cause")
        self.now = 1000.0

    def at(self, t, **extra):
        with mock.patch.object(handlers.time, "time", return_value=1000.0 + t):
            return eo.handle_pre_invocation(dict(self.payload, **extra))

    def set_subs(self, stamp=1000.0, **records):
        """Records dispatched at `stamp` (anchor starts at 1000.0 in these tests)."""
        state = load_state(self.payload)
        state["subagents"] = {k: dict(v, dispatched_at=stamp) for k, v in records.items()}
        save_state(self.payload, state)

    def nudges(self, res):
        return [s for s in res.get("injectSteps", []) if "you have delegated work" in s.get("ephemeralMessage", "")]


class TurnNudgeTest(TurnNudgeBase):
    def test_no_nudge_before_threshold(self):
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(self.nudges(self.at(299)), [])

    def test_nudge_once(self):
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        res = self.at(301)
        self.assertEqual(len(self.nudges(res)), 1)
        text = self.nudges(res)[0]["ephemeralMessage"]
        self.assertIn("(1 subagent(s) still pending)", text)
        self.assertIn("Write the deliverable now", text)
        self.assertEqual(self.nudges(self.at(400, invocationNum=0)), [])
        self.assertEqual(self.nudges(self.at(900, invocationNum=5)), [])

    def test_new_user_input_resets_timer(self):
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(len(self.nudges(self.at(301))), 1)
        self.assertEqual(self.nudges(self.at(310, lastUserInput="another request")), [])
        self.assertEqual(self.nudges(self.at(600, lastUserInput="another request")), [])
        self.set_subs(stamp=1320.0, **{SUB: {"role": "x"}})  # dispatched under the new anchor
        self.assertEqual(len(self.nudges(self.at(611, lastUserInput="another request"))), 1)

    def test_invocation_num_is_not_the_anchor(self):
        self.at(0, invocationNum=0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.at(100, invocationNum=0)
        self.assertEqual(len(self.nudges(self.at(301, invocationNum=0))), 1)

    def test_env_zero_disables(self):
        os.environ["AGY_MITOSIS_TURN_NUDGE_SECONDS"] = "0"
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(self.nudges(self.at(5000)), [])

    def test_env_custom_and_invalid(self):
        os.environ["AGY_MITOSIS_TURN_NUDGE_SECONDS"] = "60"
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(len(self.nudges(self.at(61))), 1)
        os.environ["AGY_MITOSIS_TURN_NUDGE_SECONDS"] = "abc"
        self.at(0, lastUserInput="two")
        self.assertEqual(self.nudges(self.at(299, lastUserInput="two")), [])

    def test_all_reported_still_nudges_once(self):
        self.at(0)
        self.set_subs(**{SUB: {"status": "COMPLETED"}, SUB2: {"status": "FAILED"}})
        res = self.at(600)
        self.assertEqual(len(self.nudges(res)), 1)
        self.assertNotIn("still pending", self.nudges(res)[0]["ephemeralMessage"])
        self.assertIn("follow-ups", self.nudges(res)[0]["ephemeralMessage"])
        self.assertEqual(self.nudges(self.at(700)), [])

    def test_subagent_from_previous_anchor_no_nudge(self):
        self.at(0)
        self.set_subs(stamp=900.0, **{SUB: {"role": "x"}})
        self.assertEqual(self.nudges(self.at(600)), [])

    def test_pending_spawn_counts(self):
        self.at(0)
        state = load_state(self.payload)
        state["pending_spawns"] = [{"role": "x", "dispatched_at": 1010.0}]
        save_state(self.payload, state)
        self.assertEqual(len(self.nudges(self.at(400))), 1)

    def test_dispatch_stamped_on_spawn(self):
        with mock.patch.object(handlers.time, "time", return_value=1234.5):
            eo.handle_pre_tool_use(self.tool("invoke_subagent", {"Subagents": [T.sub("Locate where the config loader is defined.")]}))
        self.assertEqual(load_state(self.payload)["pending_spawns"][0]["dispatched_at"], 1234.5)

    def test_no_subagents_no_nudge(self):
        self.at(0)
        self.assertEqual(self.nudges(self.at(600)), [])

    def test_time_backwards_no_nudge(self):
        self.at(100)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(self.nudges(self.at(0)), [])
        self.assertEqual(self.nudges(self.at(250)), [])
        self.assertEqual(len(self.nudges(self.at(301))), 1)

    def test_composes_with_non_frontier_warning(self):
        self.payload["modelName"] = "gemini-3-flash"
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        res = self.at(301, invocationNum=7)
        texts = [s.get("ephemeralMessage", "") for s in res["injectSteps"]]
        self.assertTrue(any("not a frontier model" in t for t in texts), texts)
        self.assertTrue(any("you have delegated work" in t for t in texts), texts)

    def test_subagent_payload_never_nudges(self):
        self.at(0)
        self.set_subs(**{SUB: {"role": "x"}})
        self.assertEqual(self.at(900, agent_id="abc"), {})


class OtherPlatformsTest(unittest.TestCase):
    def test_claude_and_codex_never_nudge(self):
        for name, payload in (
            ("claude", {"session_id": "s-claude", "hook_event_name": "UserPromptSubmit", "model": "opus", "prompt": "x"}),
            ("codex", {"session_id": "s-codex", "hook_event_name": "UserPromptSubmit", "model": "gpt-6.1-sol", "turn_id": "t1", "cwd": "/tmp"}),
        ):
            with self.subTest(platform=name):
                self.assertFalse(handlers._is_agy_payload(payload))
                with mock.patch.object(handlers, "load_state", return_value={"subagents": {SUB: {}}}), \
                        mock.patch.object(handlers, "save_state"), \
                        mock.patch.object(handlers, "_turn_nudge_steps") as nudge:
                    handlers.handle_pre_invocation(payload)
                nudge.assert_not_called()


class PolicyNoticeTest(unittest.TestCase):
    def test_agy_notice_rules(self):
        n = models.POLICY_NOTICE
        self.assertNotIn("web/docs research and edits go to `flash`", n)
        self.assertNotRegex(n, r"web/docs research[^.]*go to `flash`")
        self.assertIn("Do sequential web/docs research and tracing yourself (search_web, read_file); delegate only independent work that can run in parallel", n)
        self.assertIn("do not wait on a single subagent for something a few of your own calls would finish", n)
        self.assertIn("write it as soon as you have an evidence-backed answer, then refine; do not open new verification branches before the deliverable exists", n)

    def test_claude_codex_notices_unchanged(self):
        for n in (models.claude_policy_notice(), models.codex_policy_notice()):
            self.assertIn("web/docs research", n)


if __name__ == "__main__":
    unittest.main()
