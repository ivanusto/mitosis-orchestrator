#!/usr/bin/env python3
"""Replay sanitized payloads recorded from real CLIs through the hook handlers.

Fake payloads let two edit budgets pass their tests while the real count was 0, and let a PostToolUse
that carries no failure signal go unnoticed. Every fixture under tests/fixtures/payloads/<platform>/<version>/
holds the shapes a real CLI sent (see scripts/sanitize-payloads.py); this module feeds them to the handlers
in process and asserts on the real counters in the state file.

Provenance: agy 1.5.0 and Claude Code 2.1.318 fixtures are recorded. Codex fixtures under source-0.166.0
come from older Codex runs (their _meta says so) until the live capture at the end of the release.
Run: python -m unittest discover tests"""

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "payloads"
ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eo = _load("enforce_orchestrator_real_payloads", "enforce-orchestrator.py")
shape_tool = _load("payload_shape_real_payloads", "payload-shape.py")


def read_fixture(path):
    """(meta, records) of one fixture file, read as UTF-8."""
    with open(str(path), "r", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    return lines[0]["_meta"], lines[1:]


def all_fixtures():
    return sorted(FIXTURES.glob("*/*/*.jsonl"))


class Replay:
    """One fixture, materialised in a temp dir: placeholders become real paths and uuids, then handlers run."""

    def __init__(self, path, tmp, records=None):
        self.meta, loaded = read_fixture(path)
        self.records = loaded if records is None else records
        self.root = Path(tmp)
        self.ws = self.root / "ws"
        self.art = self.root / "art"
        self.home = self.root / "home"
        for directory in (self.ws, self.art, self.home):
            directory.mkdir(parents=True, exist_ok=True)
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")

    def _string(self, text):
        text = re.sub(r"__ID(\d+)__", lambda m: str(uuid.uuid5(uuid.NAMESPACE_URL, "mitosis-fixture-" + m.group(1))), text)
        if text == "__TRANSCRIPT__":
            return str(self.transcript)
        for tag, base in (("__WS__", self.ws), ("__ART__", self.art), ("__HOME__", self.home)):
            if text == tag or text.startswith(tag + "/"):
                parts = [p for p in text[len(tag):].split("/") if p]
                return str(base.joinpath(*parts)) if parts else str(base)
        return text

    def _walk(self, value):
        if isinstance(value, dict):
            return {k: self._walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v) for v in value]
        return self._string(value) if isinstance(value, str) else value

    def payload(self, record):
        payload = self._walk(record["payload"])
        if self.meta.get("platform") == "codex":
            payload["platform"] = "codex"  # what hooks/codex-hooks.json passes as --platform=codex
        return payload

    def run(self, record):
        return eo.HANDLERS[record["mode"]](self.payload(record))

    def state(self, record):
        return eo.load_state(self.payload(record))


def tool_of(payload):
    call = payload.get("toolCall")
    return call.get("name") if isinstance(call, dict) else payload.get("tool_name")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(ENV_PREFIXES):
                os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        _support.set_temp_env(os.environ, self.tmp.name)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        _support.release_state_locks()
        self.tmp.cleanup()

    def replay(self, platform, version, scenario, records=None):
        return Replay(FIXTURES / platform / version / (scenario + ".jsonl"), self.tmp.name, records)


class TestFixtureSet(Base):
    def test_fixtures_exist_for_every_platform(self):
        platforms = {p.parent.parent.name for p in all_fixtures()}
        self.assertEqual(platforms, {"agy", "claude", "codex"})

    def test_meta_matches_location_and_has_no_date_or_host(self):
        for path in all_fixtures():
            with self.subTest(fixture=str(path.relative_to(FIXTURES))):
                meta, records = read_fixture(path)
                self.assertEqual(meta["platform"], path.parent.parent.name)
                self.assertEqual(meta["cli_version"], path.parent.name)
                self.assertEqual(meta["scenario"], path.stem)
                self.assertIsInstance(meta["sanitizer_version"], int)
                self.assertFalse({"date", "time", "timestamp", "host", "hostname"} & set(meta))
                self.assertTrue(records)
                if meta["platform"] == "codex":
                    self.assertIn("not 0.166", meta.get("provenance", ""))

    def test_every_fixture_replays_without_error(self):
        for path in all_fixtures():
            with self.subTest(fixture=str(path.relative_to(FIXTURES))):
                with tempfile.TemporaryDirectory() as tmp:
                    replay = Replay(path, tmp)
                    for record in replay.records:
                        result = replay.run(record)
                        self.assertIsInstance(result, dict, record["mode"])
                        json.dumps(result)
                    _support.release_state_locks()

    def test_committed_shape_files_match_the_fixtures(self):
        for directory in sorted({p.parent for p in all_fixtures()}):
            with self.subTest(version=str(directory.relative_to(FIXTURES))):
                committed = json.loads((directory / "SHAPE.json").read_text(encoding="utf-8"))
                self.assertEqual(committed, shape_tool.build_shape(str(directory)), "run scripts/payload-shape.py shape DIR")


class TestEditBudgetOnRealShapes(Base):
    def test_codex_apply_patch_with_command_increments_the_edit_budget(self):
        # Codex sends apply_patch as {"command": "*** Begin Patch ..."}, not file_path; 2.6 counted 0 for it.
        replay = self.replay("codex", "source-0.166.0", "apply-patch")
        record = replay.records[0]
        payload = replay.payload(record)
        self.assertEqual(payload["tool_name"], "apply_patch")
        self.assertTrue(payload["tool_input"]["command"].startswith("*** Begin Patch\n*** Update File: a.txt"))
        self.assertEqual(replay.state(record).get("turn_edited_files", []), [])
        replay.run(record)
        edited = replay.state(record).get("turn_edited_files", [])
        self.assertEqual(len(edited), 1, edited)
        self.assertEqual(os.path.basename(edited[0]), "a.txt")
        replay.run(record)
        self.assertEqual(len(replay.state(record).get("turn_edited_files", [])), 1, "the same file counts once")
        other = json.loads(json.dumps(record))
        other["payload"]["tool_input"]["command"] = "*** Begin Patch\n*** Add File: b.txt\n+x\n*** End Patch"  # derived from the recorded shape
        replay.run(other)
        self.assertEqual(len(replay.state(record).get("turn_edited_files", [])), 2)

    def test_agy_write_file_with_path_increments_the_edit_budget(self):
        # agy 1.5.0 write_file and edit_file carry Path (and Content); the budget read file_path and TargetFile only.
        replay = self.replay("agy", "1.5.0", "write-file")
        record = next(r for r in replay.records if r["mode"] == "pre-tool-use")
        payload = replay.payload(record)
        self.assertEqual(payload["toolCall"]["name"], "write_file")
        self.assertEqual(set(payload["toolCall"]["args"]), {"Content", "Path"})
        for earlier in replay.records:
            if earlier is record:
                break
            replay.run(earlier)
        self.assertEqual(replay.state(record).get("turn_edited_files", []), [])
        replay.run(record)
        edited = replay.state(record).get("turn_edited_files", [])
        self.assertEqual(len(edited), 1, edited)
        self.assertEqual(os.path.basename(edited[0]), "a.txt")
        derived = json.loads(json.dumps(record))  # derived: same recorded shape, another file
        derived["payload"]["toolCall"]["args"]["Path"] = "__WS__/b.txt"
        replay.run(derived)
        self.assertEqual(len(replay.state(record).get("turn_edited_files", [])), 2)


class TestCodexAllowShape(Base):
    def test_codex_allow_without_updated_input_returns_empty(self):
        # Codex marks an allow without updatedInput as a failed hook, so only a deny may be returned there.
        os.environ["CODEX_MITOSIS_AUTO_ALLOW"] = "1"
        seen = 0
        for path in sorted((FIXTURES / "codex").glob("*/*.jsonl")):
            with self.subTest(fixture=path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    replay = Replay(path, tmp)
                    for record in replay.records:
                        result = replay.run(record)
                        if record["mode"] != "pre-tool-use":
                            continue
                        hso = result.get("hookSpecificOutput") or {}
                        if hso.get("permissionDecision") == "allow":
                            self.assertIn("updatedInput", hso, result)
                        if tool_of(record["payload"]) == "Bash" and "agent_id" in record["payload"]:
                            seen += 1
                            self.assertEqual(result, {}, "a scout's ls -la inside the workspace")
                    _support.release_state_locks()
        self.assertGreater(seen, 0, "no recorded Codex scout command was replayed")


class TestBreakerOnRealShapes(Base):
    # What the exit code of each canned agy command is in the transcript, as test_breaker.py writes it.
    EXIT = {"ls /nonexistent-a": 2, "ls /nonexistent-b": 2, "grep -c zzz a.txt": 1, "ls": 0}

    def test_agy_post_tool_use_without_failure_signal_waits_for_the_transcript(self):
        replay = self.replay("agy", "1.5.0", "failing-commands")
        pending = []
        expected = 0
        posts = 0
        history = []
        for record in replay.records:
            payload = replay.payload(record)
            if record["mode"] == "pre-invocation":
                for step, command in pending:  # agy writes the step after PostToolUse, before the next model call
                    code = self.EXIT[command]
                    content = "Created At: x\nCommand exited with code %d.\n\nCommand output:\nx\n" % code
                    with open(str(replay.transcript), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"step_index": step, "type": "GENERIC", "status": "DONE", "content": content}) + "\n")
                    if code >= 2:
                        expected += 1  # read-only and exit 2 or more: a real failure; exit 0 and 1 say nothing
                pending = []
            replay.run(record)
            state = replay.state(record)
            if record["mode"] == "post-tool-use":
                posts += 1
                args = payload["toolCall"]["args"]
                self.assertEqual(payload["error"], "", "real agy 1.5.0 sends error='' for a failed command")
                self.assertEqual(state["breaker_steps"][str(payload["stepIdx"])], args["CommandLine"])
                self.assertEqual(state.get("consecutive_failures", 0), history[-1], "PostToolUse must not count")
                pending.append((payload["stepIdx"], args["CommandLine"]))
            elif record["mode"] == "pre-invocation":
                self.assertEqual(state.get("consecutive_failures", 0), expected)
            history.append(state.get("consecutive_failures", 0))
        self.assertEqual(posts, 4)
        self.assertEqual(expected, 2)
        self.assertEqual(history[-1], 2)

    def test_real_claude_failure_leaves_the_counter_at_zero(self):
        replay = self.replay("claude", "2.1.318", "bash-failure")
        failures = [r for r in replay.records if r["mode"] == "post-tool-use-failure"]
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]["payload"]["error"].startswith("Exit code 2"))
        for record in replay.records + failures + failures:
            replay.run(record)
            self.assertEqual(replay.state(record).get("consecutive_failures", 0), 0, record["mode"])

    def test_codex_failure_leaves_the_counter_at_zero(self):
        replay = self.replay("codex", "source-0.166.0", "failed-exec")
        posts = [r for r in replay.records if r["mode"] == "post-tool-use"]
        self.assertEqual(len(posts), 3)
        for record in replay.records:
            self.assertTrue(not record["mode"] == "post-tool-use" or record["payload"]["tool_response"].startswith("Process exited with code 1"))
            replay.run(record)
            self.assertEqual(replay.state(record).get("consecutive_failures", 0), 0, record["mode"])


class TestPayloadShapeTool(unittest.TestCase):
    def write(self, directory, lines):
        directory.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(line) + "\n" for line in [{"_meta": {"platform": "agy", "cli_version": "9"}}] + lines)
        (directory / "s.jsonl").write_text(text, encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = shape_tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_shape_and_diff_report_added_removed_and_changed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "1", Path(tmp) / "2"
            self.write(old, [
                {"mode": "post-tool-use", "payload": {"error": "", "stepIdx": 3, "toolCall": {"name": "run_command", "args": {"CommandLine": "x"}}}, "result": {}},
            ])
            self.write(new, [
                {"mode": "post-tool-use", "payload": {"error": "", "stepIdx": "3", "toolCall": {"name": "run_command", "args": {"CommandLine": "x", "exitCode": 2}}, "wait": True}, "result": {}},
                {"mode": "stop", "payload": {"terminationReason": "x"}, "result": {}},
            ])
            self.assertEqual(self.run_main("shape", str(old))[0], 0)
            self.assertEqual(self.run_main("shape", str(new))[0], 0)
            shape = json.loads((old / "SHAPE.json").read_text(encoding="utf-8"))
            self.assertEqual(shape["modes"]["post-tool-use"]["run_command"]["payload.toolCall.args.CommandLine"], ["str"])
            code, out, _ = self.run_main("diff", str(old), str(new / "SHAPE.json"))
            self.assertEqual(code, 0)
            self.assertIn("post-tool-use run_command: + payload.toolCall.args.exitCode (int)", out)
            self.assertIn("post-tool-use run_command: + payload.wait (bool)", out)
            self.assertIn("post-tool-use run_command: ~ payload.stepIdx int -> str", out)
            self.assertIn("stop -: + tool new", out)
            code, out, _ = self.run_main("diff", str(new), str(old))
            self.assertEqual(code, 0)
            self.assertIn("post-tool-use run_command: - payload.toolCall.args.exitCode (int)", out)
            self.assertIn("stop -: - tool gone", out)
            code, out, _ = self.run_main("diff", str(old), str(old))
            self.assertEqual((code, out.strip()), (0, "no shape change"))

    def test_bad_input_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.json"
            junk.write_text("[1]", encoding="utf-8")
            self.assertEqual(self.run_main("diff", str(junk), str(junk))[0], 2)
            self.assertEqual(self.run_main("diff", str(Path(tmp) / "missing"), str(junk))[0], 2)
            self.assertEqual(self.run_main("shape", str(Path(tmp) / "missing"))[0], 2)
            self.assertEqual(self.run_main("nonsense")[0], 2)


if __name__ == "__main__":
    unittest.main()
