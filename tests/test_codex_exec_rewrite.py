#!/usr/bin/env python3
"""The Bash rewrite only touches a parsed codex exec invocation.
Run: python -m unittest discover tests"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")

spec = importlib.util.spec_from_file_location("eo_codex_exec_rewrite", SCRIPT)
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

SCOUT = "gpt-5.6-luna"
WORKER = "gpt-5.6-terra"
LOOKUP = "find the parser"
EDIT = "fix bug in a.py"
CE = "codex " + "exec"


def pre(command):
    return {
        "session_id": "rw-1", "transcript_path": "/nonexistent/rollout.jsonl", "cwd": "/tmp",
        "hook_event_name": "PreToolUse", "model": SCOUT, "permission_mode": "bypassPermissions",
        "turn_id": "t-1", "tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "c1",
    }


def updated_command(out):
    upd = (out.get("hookSpecificOutput") or {}).get("updatedInput")
    return None if upd is None else upd.get("command")


class CodexExecRewriteTest(unittest.TestCase):
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

    def run_hook(self, command):
        env = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "pre-tool-use", "--platform=codex"],
            input=json.dumps(pre(command)), capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def rewritten(self, command):
        return updated_command(eo.handle_pre_tool_use(pre(command)))

    # real invocations
    def test_insert_model_after_exec(self):
        self.assertEqual(self.rewritten(CE + ' "' + LOOKUP + '"'),
                         CE + " -m " + SCOUT + ' "' + LOOKUP + '"')

    def test_replace_short_model(self):
        self.assertEqual(self.rewritten(CE + ' -m foo "' + EDIT + '"'),
                         CE + " -m " + WORKER + ' "' + EDIT + '"')

    def test_replace_long_model_after_cd(self):
        self.assertEqual(self.rewritten("cd x && " + CE + ' --model foo "' + LOOKUP + '"'),
                         "cd x && " + CE + " --model " + SCOUT + ' "' + LOOKUP + '"')

    def test_replace_model_equals_and_attached_forms(self):
        self.assertEqual(self.rewritten(CE + ' --model=foo "' + LOOKUP + '"'),
                         CE + " --model=" + SCOUT + ' "' + LOOKUP + '"')
        self.assertEqual(self.rewritten(CE + ' -mfoo "' + LOOKUP + '"'),
                         CE + " -m " + SCOUT + ' "' + LOOKUP + '"')

    def test_env_assignment_and_wrappers(self):
        self.assertEqual(self.rewritten("FOO=1 " + CE + ' "' + LOOKUP + '"'),
                         "FOO=1 " + CE + " -m " + SCOUT + ' "' + LOOKUP + '"')
        self.assertEqual(self.rewritten("nohup env A=b " + CE + ' "' + EDIT + '" > out.txt'),
                         "nohup env A=b " + CE + " -m " + WORKER + ' "' + EDIT + '" > out.txt')

    def test_npx_form(self):
        self.assertEqual(self.rewritten("npx @openai/" + CE + ' "' + LOOKUP + '"'),
                         "npx @openai/" + CE + " -m " + SCOUT + ' "' + LOOKUP + '"')

    def test_other_dash_m_is_left_alone(self):
        cmd = "python3 -m unittest discover -s tests && " + CE + ' "' + LOOKUP + '"'
        self.assertEqual(self.rewritten(cmd),
                         "python3 -m unittest discover -s tests && " + CE + " -m " + SCOUT + ' "' + LOOKUP + '"')

    def test_override_model_validation_still_applies(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "bad model; rm -rf /"
        self.assertIn(" -m " + SCOUT + " ", self.rewritten(CE + ' "' + LOOKUP + '"'))
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        self.assertIn(" -m gpt-5.7-mini ", self.rewritten(CE + ' "' + LOOKUP + '"'))

    # not an invocation
    def assert_untouched(self, command):
        self.assertIsNone(self.rewritten(command), command)

    def test_grep_pattern(self):
        self.assert_untouched('grep -n "' + CE + '" README.md')

    def test_echo_string(self):
        self.assert_untouched("echo 'use " + CE + " -m X'")

    def test_heredoc_body(self):
        self.assert_untouched("cat > notes.md <<'EOF'\nrun " + CE + " here\npython3 -m unittest\nEOF")

    def test_heredoc_then_real_invocation_only_rewrites_the_invocation(self):
        cmd = "cat > n.md <<EOF\n" + CE + " -m x\nEOF\n" + CE + ' "' + LOOKUP + '"'
        self.assertEqual(self.rewritten(cmd),
                         "cat > n.md <<EOF\n" + CE + " -m x\nEOF\n" + CE + " -m " + SCOUT + ' "' + LOOKUP + '"')

    def test_python_dash_m_with_phrase_in_echo(self):
        self.assert_untouched('python3 -m unittest discover -s tests && echo "' + CE + '"')

    def test_comment_and_unbalanced_quote(self):
        self.assert_untouched("ls # " + CE + " -m x")
        self.assert_untouched("echo \"" + CE + " -m x")

    # classification
    def test_classifies_from_codex_prompt_only(self):
        noisy = "git log -- fix edit write implement && " + CE + ' "' + LOOKUP + '"'
        self.assertEqual(self.rewritten(noisy).split(" -m ")[1].split()[0], SCOUT)
        noisy2 = "grep -rn find . ; " + CE + ' "' + EDIT + '"'
        self.assertEqual(self.rewritten(noisy2).split(" -m ")[1].split()[0], WORKER)

    def test_prompt_after_double_dash(self):
        out = self.rewritten(CE + " -- '" + LOOKUP + "'")
        self.assertEqual(out, CE + " -m " + SCOUT + " -- '" + LOOKUP + "'")

    # real path through the hook process
    def test_subprocess_rewrites_invocation(self):
        out = self.run_hook("cd x && " + CE + ' --model foo "' + EDIT + '"')
        self.assertEqual(updated_command(out), "cd x && " + CE + " --model " + WORKER + ' "' + EDIT + '"')

    def test_subprocess_leaves_unrelated_commands(self):
        for cmd in (
            'grep -n "' + CE + '" README.md',
            "cat > notes.md <<'EOF'\nrun " + CE + " here\npython3 -m unittest\nEOF",
            'python3 -m unittest discover -s tests && echo "' + CE + '"',
        ):
            self.assertIsNone(updated_command(self.run_hook(cmd)), cmd)


if __name__ == "__main__":
    unittest.main()
