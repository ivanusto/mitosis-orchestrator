#!/usr/bin/env python3
"""Claude agent-id pairing: SubagentStop and async PostToolUse must not steal pending pinned spawns.
Run: python -m unittest tests.test_agent_pairing -v"""

import importlib.util
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("enforce_orchestrator_pairing", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

SCOUT_T = "mitosis-orchestrator:flash-lite-scout"
WORKER_T = "mitosis-orchestrator:flash-worker"
LOOKUP = "Locate where parse_token is defined."
MUTATE = "Fix the bug in parse_token and edit the file."
GOOD = "STATUS: COMPLETED\nFILES_CHANGED: NONE\nVERIFICATION: ran grep, exit 0\nNOTES: found it"
CHANGE_MSG = "Now edit parse.py and fix the bug, then commit the change."
FORK_ID = "f0f0f0f0f0f0f0f0"
EXPLORE_ID = "e1e1e1e1e1e1e1e1"
SCOUT_ID = "a1a1a1a1a1a1a1a1"
WORKER_ID = "b2b2b2b2b2b2b2b2"


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


class _ClaudeBase(_EnvBase):
    def setUp(self):
        super().setUp()
        art = Path(self.tmp.name) / "artifacts"
        art.mkdir()
        self.base = {"session_id": f"pair-{id(self)}", "artifactDirectoryPath": str(art), "cwd": self.tmp.name,
                     "permission_mode": "ask"}

    def tool(self, event, name, args, **extra):
        p = dict(self.base, hook_event_name=event, tool_name=name, tool_input=args)
        p.update(extra)
        return p

    def state(self):
        return eo._read_state_file(eo._state_file(self.base))

    def spawn(self, subagent_type=SCOUT_T, prompt=LOOKUP, **more):
        args = {"prompt": prompt, "description": "x", "model": "inherit"}
        if subagent_type is not None:
            args["subagent_type"] = subagent_type
        args.update(more)
        out = eo.handle_pre_tool_use(self.tool("PreToolUse", "Agent", args))
        return args, out["hookSpecificOutput"]

    def post_async(self, args, agent_id, upd=None):
        resp = {"isAsync": True, "status": "async_launched", "agentId": agent_id}
        return eo.handle_post_tool_use(self.tool("PostToolUse", "Agent", dict(args, **(upd or {})), tool_response=resp))

    def stop(self, agent_id, agent_type, msg=GOOD, **extra):
        p = dict(self.base, hook_event_name="SubagentStop", agent_id=agent_id, agent_type=agent_type,
                 last_assistant_message=msg)
        p.update(extra)
        return eo.handle_subagent_stop(p)

    def send(self, agent_id, message=CHANGE_MSG):
        return eo.handle_pre_tool_use(self.tool("PreToolUse", "SendMessage", {"to": agent_id, "message": message}))

    def assertDenied(self, out):
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class PairingTest(_ClaudeBase):
    def test_1_fork_stop_leaves_pending(self):
        self.spawn()
        for msg in (GOOD, "no headers here at all"):
            out = self.stop(FORK_ID, "", msg)
            self.assertNotEqual(out.get("decision"), "block")
        st = self.state()
        self.assertEqual(len(st["pending_spawns"]), 1)
        self.assertNotIn(FORK_ID, st.get("subagents", {}))

    def test_2_explore_stop_unknown_id_leaves_pending(self):
        self.spawn()
        out = self.stop(EXPLORE_ID, "Explore", "plain text")
        self.assertNotEqual(out.get("decision"), "block")
        st = self.state()
        self.assertEqual(len(st["pending_spawns"]), 1)
        self.assertNotIn(EXPLORE_ID, st.get("subagents", {}))

    def test_3_typed_explore_post_does_not_take_scout_entry(self):
        scout_args, pre = self.spawn()
        explore = {"prompt": "look around", "description": "e", "subagent_type": "Explore"}
        self.post_async(explore, EXPLORE_ID)
        st = self.state()
        self.assertEqual(len(st["pending_spawns"]), 1)
        self.assertNotIn(EXPLORE_ID, st.get("subagents", {}))
        self.post_async(scout_args, SCOUT_ID, pre.get("updatedInput"))
        st = self.state()
        self.assertEqual(st["pending_spawns"], [])
        self.assertEqual(st["subagents"][SCOUT_ID]["tier"], "flash_lite")

    def test_4_unknown_stop_keeps_branch_idless_clears(self):
        self.spawn(WORKER_T, "Fix bug in branch", isolation="worktree")
        # promote the pending spawn to a live record via its PostToolUse
        args = {"prompt": "Fix bug in branch", "description": "x", "subagent_type": WORKER_T}
        self.post_async(args, WORKER_ID)
        self.assertTrue(self.state()["subagents"][WORKER_ID]["active_branch"])
        self.stop(EXPLORE_ID, "Explore")
        self.assertTrue(self.state()["subagents"][WORKER_ID]["active_branch"])
        self.assertEqual(eo.handle_stop(dict(self.base, hook_event_name="Stop")).get("decision"), "block")
        eo.handle_subagent_stop(dict(self.base, hook_event_name="SubagentStop"))
        self.assertFalse(self.state()["subagents"][WORKER_ID]["active_branch"])
        self.assertEqual(eo.handle_stop(dict(self.base, hook_event_name="Stop")), {})

    def test_4b_unknown_stop_keeps_pending_branch(self):
        self.spawn(WORKER_T, "Fix bug in branch", isolation="worktree")
        self.stop(EXPLORE_ID, "Explore")
        self.assertTrue(self.state()["pending_spawns"][0]["active_branch"])
        eo.handle_subagent_stop(dict(self.base, hook_event_name="SubagentStop"))
        self.assertFalse(self.state()["pending_spawns"][0]["active_branch"])

    def test_5a_untyped_scout_paired_at_post_stays_guarded(self):
        args, pre = self.spawn(None)
        self.assertEqual(self.state()["pending_spawns"][0]["tier"], "flash_lite")
        self.post_async(args, SCOUT_ID, pre.get("updatedInput"))
        st = self.state()
        self.assertEqual(st["pending_spawns"], [])
        self.assertEqual(st["subagents"][SCOUT_ID]["tier"], "flash_lite")
        self.assertDenied(self.send(SCOUT_ID))

    def test_5b_untyped_scout_paired_at_stop_general_purpose(self):
        self.spawn(None)
        self.stop(SCOUT_ID, "general-purpose", "plain text")
        st = self.state()
        self.assertEqual(st["pending_spawns"], [])
        self.assertEqual(st["subagents"][SCOUT_ID]["tier"], "flash_lite")
        self.assertDenied(self.send(SCOUT_ID))

    def test_6_upgraded_scout_pairs_with_original_hint(self):
        args, pre = self.spawn(SCOUT_T, MUTATE)
        self.assertEqual(self.state()["pending_spawns"][0]["tier"], "flash")
        self.post_async(args, WORKER_ID, None)  # original scout hint, not updatedInput
        st = self.state()
        self.assertEqual(st["pending_spawns"], [])
        self.assertEqual(st["subagents"][WORKER_ID]["tier"], "flash")

    def test_7_plugin_stop_synthetic_record_builtin_none(self):
        self.stop(SCOUT_ID, SCOUT_T)
        self.stop(WORKER_ID, "flash-worker")
        self.stop(EXPLORE_ID, "Plan")
        subs = self.state()["subagents"]
        self.assertEqual(subs[SCOUT_ID]["tier"], "flash_lite")
        self.assertEqual(subs[WORKER_ID]["tier"], "flash")
        self.assertNotIn(EXPLORE_ID, subs)

    def test_8_report_block_once_for_plugin_type(self):
        first = self.stop(WORKER_ID, WORKER_T, "no report here")
        self.assertEqual(first.get("decision"), "block")
        second = self.stop(WORKER_ID, WORKER_T, "no report here")
        self.assertNotEqual(second.get("decision"), "block")

    def test_9_debug_log_has_agent_type(self):
        os.environ["CLAUDE_MITOSIS_DEBUG"] = "1"
        payload = dict(self.base, hook_event_name="SubagentStop", agent_id=SCOUT_ID, agent_type=SCOUT_T)
        eo._debug_log(payload, "subagent-stop", {})
        log = eo._state_file(self.base).with_name("mitosis_orchestrator_debug.log")
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["agentType"], SCOUT_T)


if __name__ == "__main__":
    unittest.main()
