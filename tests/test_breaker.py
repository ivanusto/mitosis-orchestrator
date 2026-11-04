"""Circuit breaker on real agy 1.5.0 shapes.

agy sends error="" and no result for a command that exits non-zero, and writes the step to its
transcript only after PostToolUse, so the exit code is read at the next PreInvocation. These shapes
were recorded from a real run; earlier tests fed toolResult and error, which agy never sends.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enforce-orchestrator.py"
spec = importlib.util.spec_from_file_location("enforce_orchestrator_breaker", SCRIPT)
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "project"
        self.ws.mkdir()
        self.art = Path(self.tmp.name) / "artifacts"
        self.art.mkdir()
        self.transcript = Path(self.tmp.name) / "transcript_full.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.step = 2
        self.inv = 0
        self.conv = str(uuid.uuid4())
        self._env = dict(os.environ)
        for k in list(os.environ.keys()):
            if k.startswith(("AGY_", "CLAUDE_", "CODEX_")):
                os.environ.pop(k, None)
        eo.handle_pre_invocation(self.base(invocationNum=0, initialNumSteps=0))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        _support.release_state_locks()
        self.tmp.cleanup()

    def base(self, **extra):
        p = {
            "artifactDirectoryPath": str(self.art),
            "conversationId": self.conv,
            "lastUserInput": "x",
            "modelName": "Gemini 4 Argon (High)",
            "transcriptPath": str(self.transcript),
            "workspacePaths": [str(self.ws)],
        }
        p.update(extra)
        return p

    def run_command(self, command, code):
        """One command the way agy delivers it: PostToolUse first, then the step, then the next model call."""
        self.step += 1
        call = {"args": {"CommandLine": command, "WaitMsBeforeAsync": 5000}, "name": "run_command"}
        eo.handle_post_tool_use(self.base(error="", stepIdx=self.step, toolCall=call))
        content = f"Created At: x\nCompleted At: x\nCommand exited with code {code}.\n\nCommand output:\nsome text\n"
        self.write_step(self.step, "GENERIC", content)
        self.step += 1
        self.inv += 1
        return eo.handle_pre_invocation(self.base(invocationNum=self.inv, initialNumSteps=0))

    def write_step(self, index, kind, content):
        with open(str(self.transcript), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"step_index": index, "type": kind, "status": "DONE", "content": content}) + "\n")

    def fails(self):
        return eo.load_state(self.base()).get("consecutive_failures", 0)

    def try_command(self):
        return eo.handle_pre_tool_use(self.base(stepIdx=99, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))


class TestCounting(Base):
    def test_failures_count_across_model_calls(self):
        self.run_command("pytest", 1)
        self.run_command("pytest", 2)
        self.assertEqual(self.fails(), 2)

    def test_nothing_counted_before_the_step_is_written(self):
        eo.handle_post_tool_use(self.base(error="", stepIdx=7, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.assertEqual(self.fails(), 0)
        eo.handle_pre_invocation(self.base(invocationNum=1))
        self.assertEqual(self.fails(), 0)
        self.write_step(7, "GENERIC", "Created At: x\nCommand exited with code 1.\n")
        eo.handle_pre_invocation(self.base(invocationNum=2))
        self.assertEqual(self.fails(), 1)

    def test_grep_without_a_match_is_not_a_failure(self):
        self.run_command("grep -c zzz a.txt", 1)
        self.run_command("grep -c zzz a.txt", 1)
        self.assertEqual(self.fails(), 0)

    def test_lookup_between_failures_neither_counts_nor_clears(self):
        self.run_command("pytest", 1)
        self.run_command("grep -c zzz a.txt", 1)
        self.run_command("cat a.txt", 0)
        self.assertEqual(self.fails(), 1)
        self.run_command("pytest", 1)
        self.assertEqual(self.fails(), 2)

    def test_read_only_exit_2_is_a_failure(self):
        self.run_command("ls /nonexistent-a", 2)
        self.assertEqual(self.fails(), 1)

    def test_success_clears_the_streak(self):
        self.run_command("pytest", 1)
        self.run_command("pytest", 0)
        self.assertEqual(self.fails(), 0)

    def test_new_user_turn_resets(self):
        self.run_command("pytest", 1)
        self.run_command("pytest", 1)
        eo.handle_pre_invocation(self.base(invocationNum=0))
        self.assertEqual(self.fails(), 0)

    def test_steps_from_the_last_turn_are_dropped(self):
        eo.handle_post_tool_use(self.base(error="", stepIdx=7, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.write_step(7, "GENERIC", "Command exited with code 1.\n")
        eo.handle_pre_invocation(self.base(invocationNum=0))
        self.assertEqual(self.fails(), 0)
        self.assertNotIn("breaker_steps", eo.load_state(self.base()))

    def test_two_commands_in_one_model_call(self):
        for step in (5, 6):
            eo.handle_post_tool_use(self.base(error="", stepIdx=step, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
            self.write_step(step, "GENERIC", "Command exited with code 1.\n")
        eo.handle_pre_invocation(self.base(invocationNum=1))
        self.assertEqual(self.fails(), 2)

    def test_output_quoting_the_words_is_not_read(self):
        self.step += 1
        eo.handle_post_tool_use(self.base(error="", stepIdx=self.step, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.write_step(self.step, "GENERIC", "Command exited with code 0.\n\nCommand output:\n" + "x" * 400 + "exited with code 9")
        eo.handle_pre_invocation(self.base(invocationNum=1))
        self.assertEqual(self.fails(), 0)

    def test_step_without_an_exit_code_is_neutral(self):
        self.run_command("pytest", 1)
        self.step += 1
        eo.handle_post_tool_use(self.base(error="", stepIdx=self.step, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.write_step(self.step, "GENERIC", "Background command started.\n")
        eo.handle_pre_invocation(self.base(invocationNum=5))
        self.assertEqual(self.fails(), 1)
        self.assertEqual(eo.load_state(self.base()).get("breaker_steps"), {})

    def test_denied_errors_are_neutral(self):
        for text in ("tool call denied by pre-tool hook: [Mitosis Orchestrator] Circuit breaker", "permission check failed for x"):
            eo.handle_post_tool_use(self.base(error=text, stepIdx=3, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.assertEqual(self.fails(), 0)

    def test_real_error_text_counts(self):
        eo.handle_post_tool_use(self.base(error="exit status 3", stepIdx=3, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.assertEqual(self.fails(), 1)

    def test_error_exit_1_on_a_lookup_is_neutral(self):
        eo.handle_post_tool_use(self.base(error="exit status 1", stepIdx=3, toolCall={"args": {"CommandLine": "grep x a.txt"}, "name": "run_command"}))
        self.assertEqual(self.fails(), 0)

    def test_missing_transcript_is_harmless(self):
        eo.handle_post_tool_use(self.base(error="", stepIdx=3, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        eo.handle_pre_invocation(self.base(invocationNum=1, transcriptPath=str(self.transcript) + ".gone"))
        self.assertEqual(self.fails(), 0)


class TestModes(Base):
    def advice(self, result):
        return [s.get("ephemeralMessage", "") for s in (result or {}).get("injectSteps", []) if "Circuit breaker" in s.get("ephemeralMessage", "") or "commands failed in a row" in s.get("ephemeralMessage", "")]

    def test_advise_is_the_default_and_never_denies(self):
        self.run_command("pytest", 1)
        result = self.run_command("pytest", 1)
        self.assertEqual(len(self.advice(result)), 1)
        self.assertNotEqual(self.try_command().get("decision"), "deny")

    def test_advice_once_per_turn(self):
        self.run_command("pytest", 1)
        self.assertEqual(self.advice(self.run_command("pytest", 1)) != [], True)
        self.assertEqual(self.advice(self.run_command("pytest", 1)), [])

    def test_advice_again_in_a_new_turn(self):
        self.run_command("pytest", 1)
        self.run_command("pytest", 1)
        eo.handle_pre_invocation(self.base(invocationNum=0))
        self.inv = 0
        self.run_command("pytest", 1)
        self.assertNotEqual(self.advice(self.run_command("pytest", 1)), [])

    def test_enforce_denies_and_does_not_advise(self):
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER_MODE"] = "enforce"
        self.run_command("pytest", 1)
        self.assertEqual(self.advice(self.run_command("pytest", 1)), [])
        verdict = self.try_command()
        self.assertEqual(verdict.get("decision"), "deny")
        self.assertIn("Circuit breaker tripped", verdict.get("reason", ""))

    def test_off_neither_denies_nor_advises(self):
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER_MODE"] = "off"
        self.run_command("pytest", 1)
        self.assertEqual(self.advice(self.run_command("pytest", 1)), [])
        self.assertNotEqual(self.try_command().get("decision"), "deny")

    def test_zero_wins_over_enforce(self):
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER"] = "0"
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER_MODE"] = "enforce"
        self.run_command("pytest", 1)
        self.run_command("pytest", 1)
        self.assertNotEqual(self.try_command().get("decision"), "deny")

    def test_bad_mode_means_advise(self):
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER_MODE"] = "banana"
        self.assertEqual(eo.handlers._breaker_mode(), "advise")

    def test_breaker_denial_does_not_feed_itself(self):
        os.environ["AGY_MITOSIS_CIRCUIT_BREAKER_MODE"] = "enforce"
        self.run_command("pytest", 1)
        self.run_command("pytest", 1)
        eo.handle_post_tool_use(self.base(error="tool call denied by pre-tool hook: x", stepIdx=50, toolCall={"args": {"CommandLine": "pytest"}, "name": "run_command"}))
        self.assertEqual(self.fails(), 2)


class TestOtherPlatformsUnchanged(unittest.TestCase):
    """Real Claude Code and Codex PostToolUse payloads carry tool_response and no error: the counter stays 0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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

    def test_claude_failed_bash(self):
        p = {"session_id": self.sid, "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": self.tmp.name,
             "tool_input": {"command": "pytest"}, "tool_response": {"stdout": "", "stderr": "boom", "interrupted": False}}
        eo.handle_post_tool_use(p)
        eo.handle_post_tool_use(p)
        self.assertEqual(eo.load_state(p).get("consecutive_failures", 0), 0)

    def test_codex_failed_exec(self):
        p = {"session_id": self.sid, "turn_id": "t1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": self.tmp.name,
             "model": "gpt-6.1-terra", "tool_input": {"command": "pytest"}, "tool_response": "Process exited with code 1\nboom"}
        eo.handle_post_tool_use(p)
        eo.handle_post_tool_use(p)
        self.assertEqual(eo.load_state(p).get("consecutive_failures", 0), 0)


if __name__ == "__main__":
    unittest.main()
