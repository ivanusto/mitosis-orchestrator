"""Codex reports a command's workdir as the hook payload cwd: roots and the worktree test use the turn cwd."""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mitosis_hook import guards, handlers  # noqa: E402
from mitosis_hook.common import is_codex_hook_payload  # noqa: E402


class CodexWorkdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        (self.repo / "sub").mkdir(parents=True)
        (self.repo / "README.md").write_text("x\n")
        self.outside = base / "outside"
        self.outside.mkdir()
        self.wt = base / "worktrees" / "b1"
        self.wt.mkdir(parents=True)
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

    def payload(self, event, cwd, **extra):
        p = {"session_id": self.sid, "turn_id": "t1", "hook_event_name": event, "cwd": str(cwd), "model": "gpt-5.5"}
        p.update(extra)
        return p

    def prompt(self, cwd=None):
        p = self.payload("UserPromptSubmit", cwd or self.repo, prompt="go")
        self.assertTrue(is_codex_hook_payload(p))
        handlers.handle_pre_invocation(p)
        _support.release_state_locks()

    def bash(self, cmd, workdir, **extra):
        p = self.payload("PreToolUse", workdir, tool_name="Bash", tool_input={"command": cmd}, **extra)
        r = handlers.handle_pre_tool_use(p)
        _support.release_state_locks()
        return r

    def denied(self, r):
        return (r.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"

    def test_workdir_outside_workspace_denied(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        self.assertTrue(self.denied(self.bash("ls", self.outside)))

    def test_subdir_workdir_parent_read_allowed(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        self.assertEqual(self.bash("cat ../README.md", self.repo / "sub"), {})

    def test_subdir_workdir_escape_denied(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        self.assertTrue(self.denied(self.bash("cat ../../x", self.repo / "sub")))

    def test_no_stored_turn_cwd_keeps_old_behaviour(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        # No UserPromptSubmit: the payload cwd is the only root, as before.
        self.assertEqual(self.bash("ls", self.outside), {})
        self.assertTrue(self.denied(self.bash("cat ../README.md", self.repo / "sub")))

    def test_worktree_workdir_still_gets_test_runner_check(self):
        os.environ["CODEX_MITOSIS_FASTPATH_MODE"] = "enforce"
        self.prompt()
        edit = self.payload("PreToolUse", self.repo, tool_name="Edit",
                            tool_input={"file_path": str(self.repo / "README.md"), "old_string": "x", "new_string": "y"})
        handlers.handle_pre_tool_use(edit)
        _support.release_state_locks()
        self.assertTrue(self.denied(self.bash("pytest -q", self.wt)))

    def test_subagent_unchanged(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        # A subagent in its own worktree: its cwd stays the root, the stored primary cwd is ignored.
        self.assertEqual(self.bash("ls", self.wt, agent_id="a1", agent_type="worker"), {})
        self.assertTrue(self.denied(self.bash("cat ../../x", self.wt, agent_id="a1", agent_type="worker")))

    def test_auto_allow_unset_no_workspace_denial(self):
        self.prompt()
        self.assertEqual(self.bash("ls", self.outside), {})
        self.assertEqual(self.bash("cat ../../x", self.repo / "sub"), {})


class StepsAfterCd(unittest.TestCase):
    """From a subfolder each .. stays inside, read from cwd, but the shell has already moved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.sub = self.repo / "sub"
        self.sub.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, command):
        return guards.command_stays_in_workspace(command, str(self.sub), [self.repo])[0]

    def test_cd_then_parent_escapes(self):
        self.assertFalse(self.ok("cd .. && rm -rf ../outside"))
        self.assertFalse(self.ok("cd ..; cd ..; cat a"))
        self.assertFalse(self.ok("pushd .. && cat ../a"))
        self.assertFalse(self.ok("(cd .. && cat ../a)"))

    def test_cd_inside_the_workspace_still_passes(self):
        self.assertTrue(self.ok("cd .. && cat README.md"))
        self.assertTrue(self.ok("cd deep && cat ../a"))

    def test_without_cd_each_path_stands_alone(self):
        self.assertTrue(self.ok("cat ../a ../b"))
        self.assertTrue(self.ok("git log v1..v2"))


if __name__ == "__main__":
    unittest.main()
