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

    def test_g3_heuristic_refusal_is_no_opinion_on_codex(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        self.assertEqual(self.bash("cd; rm x", self.repo / "sub"), {})
        self.assertEqual(self.bash("cd .. && cat ../a", self.repo / "sub"), {})

    def test_g3_a_path_that_escapes_still_denies_on_codex(self):
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        self.prompt()
        self.assertTrue(self.denied(self.bash("cat ../../x", self.repo / "sub")))

    def test_g3_claude_still_asks(self):
        os.environ["CLAUDE_MITOSIS_AUTO_ALLOW"] = "1"
        p = {"session_id": self.sid, "hook_event_name": "PreToolUse", "cwd": str(self.repo / "sub"),
             "tool_name": "Bash", "tool_input": {"command": "cd; rm x"}}
        r = handlers.handle_pre_tool_use(p)
        _support.release_state_locks()
        self.assertEqual((r.get("hookSpecificOutput") or {}).get("permissionDecision"), "ask")


class WindowsDirectoryChanges(unittest.TestCase):
    """Windows mode comes from the roots, so this runs on any host. Claude Code uses Git Bash on Windows."""

    ROOT = "C:\\w"
    CWD = "C:\\w\\sub"

    def ok(self, command):
        return guards.command_stays_in_workspace(command, self.CWD, [Path(self.ROOT)])[0]

    def test_refused(self):
        for command in ("git -C .. -C .. clean -fdx", "make -C .. -C .. clean", "tar -C .. -C .. -xf a.tar", "env -C .. rm ../x",
                        "popd && rm ../x", "cd && rm -rf proj", "cd `pwd` && ls", "cd .. && del ..\\x", "cd /d .. && del ..\\x", "cd ..; cd ..; type a",
                        "python3 -c \"import os;os.chdir('..');os.remove('../x')\""):
            with self.subTest(command=command):
                self.assertFalse(self.ok(command))

    def test_still_passes(self):
        for command in ("cd .. && type README.md", "git -C sub status", "git log v1..v2", "type ..\\a"):
            with self.subTest(command=command):
                self.assertTrue(self.ok(command))


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
        if os.name != "nt":  # Windows refuses any .. after a directory change: it does not add up the steps
            self.assertTrue(self.ok("cd deep && cat ../a"))

    def test_without_cd_each_path_stands_alone(self):
        self.assertTrue(self.ok("cat ../a ../b"))
        self.assertTrue(self.ok("git log v1..v2"))


class DirectoryChangeWords(unittest.TestCase):
    """cwd /w/sub, root /w: a conservative word check, never a shell parser."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve() / "w"
        self.sub = self.repo / "sub"
        self.sub.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, command):
        return guards.command_stays_in_workspace(command, str(self.sub), [self.repo])

    def test_g1_forms_that_change_directory(self):
        for command in (
            'sh -c "cd .. && rm ../x"', "bash -c 'cd ..; rm ../x'", "eval 'cd ..; rm ../x'",
            "echo `cd ..; rm ../x`", "\\cd .. && rm ../x", "'cd' .. && rm ../x", '"cd" .. && rm ../x',
            "chdir .. && rm ../x", "popd && rm ../x", "git -C .. -C .. clean -fdx", "make -C .. -C .. clean",
            "tar -C .. -C .. -xf a.tar", "env -C .. rm ../x",
            "python3 -c \"import os;os.chdir('..');os.remove('../x')\"",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.ok(command)[0])

    def test_g1_cd_without_a_plain_target(self):
        for command in ("cd; rm ../x", "cd - && rm ../x", "cd $OLDPWD && rm ../x",
                        'cd "$(dirname "$PWD")" && rm ../x', "cd && rm -rf proj", "cd `pwd` && ls"):
            with self.subTest(command=command):
                ok, why = self.ok(command)
                self.assertFalse(ok)
                self.assertTrue(why.startswith(guards.HEURISTIC_REFUSAL_PREFIX), why)

    def test_g1_plain_commands_stay_allowed(self):
        for command in ("cd sub && ls", "cd .. && cat README.md", "cat ../a ../b", "git log v1..v2"):
            with self.subTest(command=command):
                self.assertTrue(self.ok(command)[0])


class WindowsDirectoryChange(unittest.TestCase):
    def ok(self, command):
        return guards.command_stays_in_workspace(command, "C:\\repo\\sub", [Path("C:\\repo")])

    def test_g2_chdir_with_a_dotdot_segment(self):
        for command in ("cd sub && type ..\\x", "cd /d sub & type ../x", "CD sub && type ..\\x", "pushd sub && dir ..\\x",
                        "Set-Location sub; Get-Content ..\\x", "sl sub; Remove-Item ../x", "chdir sub && del ..\\x",
                        "cd .. ; del ..\\x", "cd /d .. && type ..\\x"):
            with self.subTest(command=command):
                self.assertFalse(self.ok(command)[0])

    def test_g2_other_commands_unaffected(self):
        self.assertTrue(self.ok("cd sub && dir")[0])
        self.assertTrue(self.ok("cd ..\\b")[0])  # the target alone is judged from the real cwd
        self.assertTrue(self.ok("type C:\\repo\\x")[0])


if __name__ == "__main__":
    unittest.main()
