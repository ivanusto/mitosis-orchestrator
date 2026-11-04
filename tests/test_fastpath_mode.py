"""Fast-path budget modes: advise (default), enforce, off."""

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enforce-orchestrator.py"
spec = importlib.util.spec_from_file_location("enforce_orchestrator_fp", SCRIPT)
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

from mitosis_hook import handlers  # noqa: E402

ADVICE = "[Mitosis Orchestrator] Advice, not a block:"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "project"
        self.ws.mkdir()
        self.art = Path(self.tmp.name) / "artifacts"
        self.art.mkdir()
        self.sid = str(uuid.uuid4())
        self._env = dict(os.environ)
        for k in list(os.environ.keys()):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_")):
                os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        _support.release_state_locks()
        self.tmp.cleanup()

    def claude(self, name, args, **extra):
        p = {"session_id": self.sid, "hook_event_name": "PreToolUse", "cwd": str(self.ws),
             "artifactDirectoryPath": str(self.art), "tool_name": name, "tool_input": args}
        p.update(extra)
        return p

    def codex(self, name, args, **extra):
        p = self.claude(name, args, turn_id="t1")
        p.update(extra)
        return p

    def agy(self, name, args, inv=1):
        return {"conversationId": self.sid, "artifactDirectoryPath": str(self.art),
                "workspacePaths": [str(self.ws)], "modelName": "Gemini 4 Argon (Medium)",
                "invocationNum": inv, "toolCall": {"name": name, "args": args}}

    def edit(self, i):
        return {"file_path": str(self.ws / f"f{i}.py")}

    def agy_edit(self, i):
        return {"TargetFile": str(self.ws / f"f{i}.py")}


def advice_of(res):
    return (res.get("hookSpecificOutput") or {}).get("additionalContext")


class TestHookPlatforms(Base):
    def _run(self, make):
        for i in range(3):
            self.assertEqual(eo.handle_pre_tool_use(make("Write", self.edit(i))), {})
        res = eo.handle_pre_tool_use(make("Write", self.edit(3)))
        self.assertTrue(advice_of(res).startswith(ADVICE), res)
        self.assertNotIn("deny", str(res.get("hookSpecificOutput")))
        self.assertEqual(res["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", res["hookSpecificOutput"])
        # once per kind per turn
        self.assertEqual(eo.handle_pre_tool_use(make("Write", self.edit(4))), {})
        # edit_test is a separate kind and does not echo the command
        res2 = eo.handle_pre_tool_use(make("Bash", {"command": "pytest -q secret_arg"}))
        self.assertTrue(advice_of(res2).startswith(ADVICE))
        self.assertNotIn("secret_arg", advice_of(res2))
        self.assertEqual(eo.handle_pre_tool_use(make("Bash", {"command": "pytest -q"})), {})
        return make

    def test_claude_default_advise(self):
        self._run(self.claude)

    def test_codex_hook_default_advise(self):
        self._run(self.codex)

    def test_new_turn_resets_hook_platform(self):
        self._run(self.claude)
        eo.handle_pre_invocation({"session_id": self.sid, "hook_event_name": "UserPromptSubmit",
                                  "cwd": str(self.ws), "artifactDirectoryPath": str(self.art)})
        res = eo.handle_pre_tool_use(self.claude("Write", self.edit(9)))
        self.assertEqual(res, {})  # budget reset
        for i in range(2):
            eo.handle_pre_tool_use(self.claude("Write", self.edit(10 + i)))
        res = eo.handle_pre_tool_use(self.claude("Write", self.edit(20)))
        self.assertTrue(advice_of(res))  # advice available again

    def test_unknown_mode_is_advise(self):
        os.environ["CLAUDE_MITOSIS_FASTPATH_MODE"] = "banana"
        os.environ["CLAUDE_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        res = eo.handle_pre_tool_use(self.claude("Write", self.edit(0)))
        self.assertTrue(advice_of(res))

    def test_enforce_denies(self):
        os.environ["CLAUDE_MITOSIS_FASTPATH_MODE"] = "enforce"
        os.environ["CLAUDE_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        res = eo.handle_pre_tool_use(self.claude("Write", self.edit(0)))
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_off_and_legacy_flag_write_no_state(self):
        for var, val in (("CLAUDE_MITOSIS_FASTPATH_MODE", "off"), ("CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH", "1")):
            with self.subTest(var=var):
                os.environ.clear()
                os.environ.update(self._env)
                os.environ[var] = val
                self.sid = str(uuid.uuid4())
                for i in range(6):
                    self.assertEqual(eo.handle_pre_tool_use(self.claude("Write", self.edit(i))), {})
                self.assertEqual(eo.handle_pre_tool_use(self.claude("Bash", {"command": "pytest"})), {})
                state = eo.load_state(self.claude("Write", {}))
                self.assertFalse(state.get("turn_edited_files"))
                self.assertFalse(state.get("fastpath_advised"))

    def test_legacy_flag_wins_over_enforce(self):
        os.environ["CLAUDE_MITOSIS_FASTPATH_MODE"] = "enforce"
        os.environ["CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH"] = "1"
        os.environ["CLAUDE_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        self.assertEqual(eo.handle_pre_tool_use(self.claude("Write", self.edit(0))), {})

    def test_workspace_guard_deny_survives_and_drops_advice(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        os.environ["CODEX_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        outside = {"file_path": "/etc/mitosis_outside_test"}
        res = eo.handle_pre_tool_use(self.codex("Write", outside))
        hso = res["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertNotIn("additionalContext", hso)

    def test_legacy_codex_payload_gets_no_advice(self):
        os.environ["CODEX_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        p = {"thread_id": self.sid, "cwd": str(self.ws), "tool_name": "apply_patch",
             "tool_input": {"files": [str(self.ws / "a.py")]}}
        res = eo.handle_pre_tool_use(p)
        self.assertEqual(res.get("decision"), "allow")
        self.assertNotIn("Advice", str(res))


class TestAgy(Base):
    def test_default_advise_queues_and_emits_once(self):
        for i in range(3):
            eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(i), inv=0))
        res = eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(3), inv=1))
        self.assertNotEqual(res, {})
        self.assertEqual(res.get("decision"), "allow")
        self.assertNotIn("Advice", str(res))
        out = eo.handle_pre_invocation(self.agy("x", {}, inv=2))
        steps = out["injectSteps"]
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0]["ephemeralMessage"].startswith(ADVICE))
        # delivered once
        out2 = eo.handle_pre_invocation(self.agy("x", {}, inv=3))
        self.assertNotIn("injectSteps", out2)

    def test_budget_survives_invocation_1_2_and_resets_on_0(self):
        os.environ["AGY_MITOSIS_MAX_DIRECT_EDITS"] = "1"
        eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(0), inv=0))
        for inv in (1, 2):
            eo.handle_pre_invocation(self.agy("x", {}, inv=inv))
            state = eo.load_state(self.agy("x", {}))
            self.assertIn(str((self.ws / "f0.py").resolve()), state["turn_edited_files"])
            eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(5), inv=inv))
            eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(0), inv=inv))
        os.environ["AGY_MITOSIS_FASTPATH_MODE"] = "enforce"
        res = eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(6), inv=2))
        self.assertEqual(res["decision"], "deny")
        eo.handle_pre_invocation(self.agy("x", {}, inv=0))
        state = eo.load_state(self.agy("x", {}))
        self.assertEqual(state["turn_edited_files"], [])
        self.assertEqual(state["fastpath_advised"], [])

    def test_non_int_invocation_num_resets(self):
        eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(0), inv=0))
        p = self.agy("x", {}, inv="1")
        eo.handle_pre_invocation(p)
        self.assertEqual(eo.load_state(p)["turn_edited_files"], [])

    def test_consecutive_failures_reset_every_call(self):
        p = self.agy("x", {}, inv=2)
        state = eo.load_state(p)
        state["consecutive_failures"] = 2
        eo.save_state(p, state)
        eo.handle_pre_invocation(p)
        self.assertEqual(eo.load_state(p).get("consecutive_failures", 0), 0)

    def test_advice_survives_frontier_warning_path(self):
        os.environ["AGY_MITOSIS_MAX_DIRECT_EDITS"] = "0"
        eo.handle_pre_tool_use(self.agy("write_to_file", self.agy_edit(0), inv=1))
        p = self.agy("x", {}, inv=2)
        p["modelName"] = "tiny-model-7b"
        out = eo.handle_pre_invocation(p)
        msgs = [s["ephemeralMessage"] for s in out["injectSteps"]]
        self.assertTrue(msgs[0].startswith(ADVICE))
        self.assertEqual(len(msgs), 2)


class TestRunnerHelper(unittest.TestCase):
    def test_false_positives(self):
        for cmd in ('git commit -m "add pytest"', "grep pytest file", "cat <<EOF\nrun pytest now\nEOF",
                    "echo 'npm test' | tee log", "git log --grep=pytest && ls"):
            with self.subTest(cmd=cmd):
                self.assertFalse(handlers._runs_test_runner(cmd))

    def test_true_positives(self):
        for cmd in ("pytest -q", "npm test && echo ok", "cd x; python -m pytest",
                    "ls | head\npytest", "cat <<EOF\nx\nEOF\npytest"):
            with self.subTest(cmd=cmd):
                self.assertTrue(handlers._runs_test_runner(cmd))


class TestCodexAllow(unittest.TestCase):
    def test_codex_hook_allow_without_updated_input_is_empty(self):
        from mitosis_hook.common import _allow
        p = {"turn_id": "t", "hook_event_name": "PreToolUse", "session_id": "s"}
        self.assertEqual(_allow("r", overrides=["command(ls)"], payload=p), {})
        out = _allow("r", payload=p, updated_input={"a": 1})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"], {"a": 1})


class TestRunnerDetection(unittest.TestCase):
    """Text that names a runner is not a run, but a string given to a shell or a nested command is."""

    RUNS = [
        "pytest", "FOO=1 pytest -x", "(pytest)", "time pytest", "sudo pytest", 'bash -c "pytest"',
        'bash -lc "cd x && pytest -q"', "sh -c 'npm test'", 'eval "pytest"', "echo x && pytest",
        'git commit -m "pytest" && pytest', "cat <<EOF\npytest\nEOF\nnpm test", "python3 -m unittest discover",
        "cargo test", "go test ./...", "xargs pytest", "find . -name x -exec pytest {} \\;", "echo $(pytest)",
        "tail -f log & pytest", "pytest 2>&1 | tail -5", "pytest &> out", "cd x; python -m pytest",
        "npm test && echo ok", '/usr/bin/env bash -c "pytest"',
    ]
    MENTIONS = [
        'git commit -m "add pytest"', "grep pytest file", "cat <<'E' > t.py\nimport pytest\nE",
        "grep pytest a | wc -l", 'echo "a;pytest"', "git log --grep=pytest", "rg pytest; ls",
        "sed -n 1p pytest.ini", "ls tests | grep pytest", 'python -c "import pytest"', "echo bash -c",
        "git commit -F- <<EOF\nrun pytest\nEOF", "cat a 2>&1", 'grep "bash -c" notes.md',
    ]

    def test_runs(self):
        for cmd in self.RUNS:
            with self.subTest(cmd=cmd):
                self.assertTrue(handlers._runs_test_runner(cmd))

    def test_mentions(self):
        for cmd in self.MENTIONS:
            with self.subTest(cmd=cmd):
                self.assertFalse(handlers._runs_test_runner(cmd))


if __name__ == "__main__":
    unittest.main()
