#!/usr/bin/env python3
"""Privacy gate for tests/fixtures/payloads and for scripts/sanitize-payloads.py.

Fixtures are committed and published. This module fails when one holds a home path, the local user or
host name, a key, an e-mail address, an unmapped uuid or an opaque Codex token that is not the fake. It
also feeds the sanitizer a payload with secrets in keys it has never heard of, and plants bad lines to
prove the gate itself can fail.
Run: python -m unittest discover tests"""

import getpass
import importlib.util
import io
import json
import re
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "payloads"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


san = _load("sanitize_payloads_privacy", "sanitize-payloads.py")
codex = _load("enforce_orchestrator_privacy", "enforce-orchestrator.py")

PATTERNS = (
    ("home path", re.compile(r"/home/|/Users/", re.I)),
    ("windows path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\|\\|/)")),
    ("sk- key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{3,}")),
    ("e-mail", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("uuid", re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
)
TOKEN = re.compile(r"gAAAA[A-Za-z0-9_=-]{16,}")


def local_names():
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    try:
        host = socket.gethostname()
        names.update((host, host.split(".")[0]))
    except Exception:
        pass
    return sorted(n.lower() for n in names if n and len(n) >= 3)


def scan(text):
    """Findings for one string of fixture text. Empty means clean."""
    found = [label for label, pattern in PATTERNS if pattern.search(text)]
    if any(m.group(0) != san.FAKE_OPAQUE for m in TOKEN.finditer(text)):
        found.append("opaque token")
    low = text.lower()
    for name in local_names():
        # A short name ("run", "dev") would match inside ordinary words, so it must stand alone.
        pattern = re.escape(name) if len(name) >= 6 else r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(name)
        if re.search(pattern, low):
            found.append("local name")
    return found


def scan_tree(directory):
    findings = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for label in scan(line):
                    findings.append("%s:%d: %s" % (path.relative_to(directory), number, label))
    return findings


class TestCommittedFixtures(unittest.TestCase):
    def test_fixtures_are_present(self):
        self.assertTrue(list(FIXTURES.glob("*/*/*.jsonl")))

    def test_no_private_content_in_any_fixture(self):
        self.assertEqual(scan_tree(FIXTURES), [])

    def test_every_fixture_is_utf8_json_lines_with_meta_first(self):
        for path in FIXTURES.glob("*/*/*.jsonl"):
            with self.subTest(fixture=path.name):
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(list(json.loads(lines[0])), ["_meta"])
                for line in lines[1:]:
                    self.assertEqual(sorted(json.loads(line)), ["mode", "payload", "result"])

    def test_values_are_placeholders_or_allowlisted(self):
        """Every string in a fixture is a placeholder, an enum-like name or a canned command. Nothing else."""
        free = re.compile(r"^(__[A-Z]+[0-9]*__(/__NAME__|/[A-Za-z0-9_.-]+)*|Exit code \d+(\n__TEXT__)?|Process exited with code \d+(\n__TEXT__)?)$")

        def strings(node, key=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from strings(v, k)
            elif isinstance(node, list):
                for item in node:
                    yield from strings(item, key)
            elif isinstance(node, str):
                yield key, node

        for path in FIXTURES.glob("*/*/*.jsonl"):
            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[1:]]
            for line in lines:
                for key, text in strings(line):
                    if key in san.NAME_KEYS or key == "name" or key == "mode":
                        ok = bool(san.NAME_RE.match(text))
                    elif key in san.COMMAND_KEYS:
                        ok = text in san.CANNED_COMMANDS or free.match(text) or text.startswith("*** Begin Patch")
                    elif text.startswith("{"):
                        ok = "__TEXT__" in text or "__ID" in text or "timed_out" in text
                    else:
                        ok = text == "" or text == san.FAKE_OPAQUE or free.match(text)
                    with self.subTest(fixture=path.name, key=key):
                        self.assertTrue(ok, "unexpected value under %r" % key)


class TestTheGateCanFail(unittest.TestCase):
    PLANTED = {
        "home path": '{"cwd": "/home/someone/project"}',
        "windows path": '{"cwd": "C:\\\\Users\\\\someone"}',
        "sk- key": '{"env": "sk-live-abcdef123456"}',
        "e-mail": '{"who": "someone@example.com"}',
        "uuid": '{"session_id": "01a15f91-1862-4122-a482-edb5acadc197"}',
        "opaque token": '{"message": "gAAAAABq2ZqgkxWOcFgr1V5_lfPDvBNuuCxjbP0Dy2Ou"}',
    }

    def test_each_planted_line_is_caught(self):
        for label, line in self.PLANTED.items():
            with self.subTest(planted=label):
                self.assertIn(label, scan(line))

    def test_fake_token_and_placeholders_pass(self):
        self.assertEqual(scan('{"message": "%s", "cwd": "__WS__/a.txt", "session_id": "__ID1__"}' % san.FAKE_OPAQUE), [])

    def test_local_user_and_host_are_caught(self):
        names = local_names()
        if not names:
            self.skipTest("no user or host name could be read")
        self.assertIn("local name", scan('{"x": "%s"}' % names[0]))

    def test_planted_line_in_a_tree_is_reported_with_its_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "p" / "1"
            good.mkdir(parents=True)
            (good / "ok.jsonl").write_text('{"_meta": {}}\n{"mode": "stop", "payload": {"cwd": "__WS__"}, "result": {}}\n', encoding="utf-8")
            self.assertEqual(scan_tree(tmp), [])
            with open(str(good / "ok.jsonl"), "a", encoding="utf-8") as fh:
                fh.write('{"mode": "stop", "payload": {"cwd": "/home/x"}, "result": {}}\n')
            self.assertEqual(scan_tree(tmp), ["p/1/ok.jsonl:3: home path"])


class TestSanitizer(unittest.TestCase):
    META = {"platform": "t", "cli_version": "1", "scenario": "s", "sanitizer_version": san.SANITIZER_VERSION}
    SECRETS = (
        "sk-live-abcdef123456", "someone@example.com", "/home/secretuser/.ssh/id_rsa", "C:\\Users\\secretuser\\x",
        "01a15f91-1862-4122-a482-edb5acadc197", "gAAAAABq2ZqgkxWOcFgr1V5_lfPDvBNuuCxjbP0Dy2Ou4GrJPkb", "hunter2-password",
        "ghp_abcdefghijklmnop", "987654321",
    )

    def run_one(self, payload, result=None):
        out = san.sanitize_lines([{"mode": "pre-tool-use", "payload": payload, "result": result or {}}], self.META)
        return out, json.dumps(out)

    def test_secrets_in_unlisted_keys_never_survive(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "brand_new_key": self.SECRETS[0],
            "nested": {"unlisted": [self.SECRETS[1], self.SECRETS[2], {"deeper": self.SECRETS[3]}], "n": 987654321, "f": 3.14159},
            "ids": [self.SECRETS[4]],
            "blob": self.SECRETS[5],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "curl -H 'x: hunter2-password' http://h", "mystery": self.SECRETS[6]}},
            self.SECRETS[7]: "value",
            "model": self.SECRETS[1],
            "tool_name": self.SECRETS[2],
        }
        out, text = self.run_one(payload, {"new_result_key": self.SECRETS[0], "reason": self.SECRETS[6]})
        for secret in self.SECRETS:
            self.assertNotIn(secret, text, secret)
        body = out[1]["payload"]
        self.assertEqual(body["brand_new_key"], "__STR__")
        self.assertEqual(body["nested"]["n"], 0)
        self.assertEqual(body["nested"]["f"], 0.0)
        self.assertEqual(body["toolCall"]["args"]["CommandLine"], "__CMD__")
        self.assertEqual(body["toolCall"]["args"]["mystery"], "__STR__")
        self.assertEqual(body["model"], "__STR__")
        self.assertEqual(body["tool_name"], "__STR__")
        self.assertEqual(out[1]["result"], {"new_result_key": "__STR__", "reason": "__TEXT__"})
        self.assertEqual(scan(text), [])

    def test_structure_and_allowlisted_values_survive(self):
        payload = {
            "hook_event_name": "PostToolUse", "permission_mode": "acceptEdits", "model": "gemini-3.8-flash-low",
            "modelName": "Gemini 4 Argon (High)", "invocationNum": 2, "stepIdx": 5, "stop_hook_active": False, "error": "",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "ls /nonexistent-a", "WaitMsBeforeAsync": 5000}},
            "tool_input": {"command": "pytest -k secret_name"}, "tool_response": {"stdout": "", "stderr": "boom", "interrupted": False},
        }
        out, _ = self.run_one(payload)
        body = out[1]["payload"]
        self.assertEqual(body["hook_event_name"], "PostToolUse")
        self.assertEqual(body["modelName"], "Gemini 4 Argon (High)")
        self.assertEqual((body["invocationNum"], body["stepIdx"], body["stop_hook_active"], body["error"]), (2, 5, False, ""))
        self.assertEqual(body["toolCall"], {"name": "run_command", "args": {"CommandLine": "ls /nonexistent-a", "WaitMsBeforeAsync": 5000}})
        self.assertEqual(body["tool_input"]["command"], "__CMD__")
        self.assertEqual(body["tool_response"], {"stdout": "", "stderr": "__TEXT__", "interrupted": False})

    def test_ids_and_paths_map_to_stable_placeholders(self):
        ws, art = "/srv/work/project", "/srv/state/brain/abc"
        lines = [
            {"mode": "stop", "payload": {"conversationId": "c-1", "artifactDirectoryPath": art, "workspacePaths": [ws],
                                         "transcriptPath": art + "/logs/t.jsonl", "toolCall": {"name": "write_file", "args": {"Path": ws + "/a.txt"}}}, "result": {}},
            {"mode": "stop", "payload": {"conversationId": "c-1", "toolCall": {"name": "write_file", "args": {"Path": ws + "/private-notes.txt"}}}, "result": {}},
            {"mode": "stop", "payload": {"conversationId": "c-2", "toolCall": {"name": "write_file", "args": {"Path": "/etc/passwd"}}}, "result": {}},
        ]
        out = san.sanitize_lines(lines, self.META)
        first, second, third = (o["payload"] for o in out[1:])
        self.assertEqual((first["conversationId"], second["conversationId"], third["conversationId"]), ("__ID1__", "__ID1__", "__ID2__"))
        self.assertEqual((first["artifactDirectoryPath"], first["workspacePaths"], first["transcriptPath"]), ("__ART__", ["__WS__"], "__TRANSCRIPT__"))
        self.assertEqual(first["toolCall"]["args"]["Path"], "__WS__/a.txt")
        self.assertEqual(second["toolCall"]["args"]["Path"], "__WS__/__NAME__")
        self.assertEqual(third["toolCall"]["args"]["Path"], "__PATH__")

    def test_apply_patch_keeps_its_structure_only(self):
        patch = "*** Begin Patch\n*** Update File: src/secret_plan.py\n@@ def hidden():\n-old secret\n+new secret\n*** Add File: a.txt\n+hello\n*** End Patch"
        out, text = self.run_one({"tool_name": "apply_patch", "tool_input": {"command": patch}})
        self.assertEqual(out[1]["payload"]["tool_input"]["command"],
                         "*** Begin Patch\n*** Update File: __NAME__\n@@\n-x\n+x\n*** Add File: a.txt\n+x\n*** End Patch")
        self.assertNotIn("secret", text)

    def test_opaque_codex_token_becomes_the_fake_and_still_matches(self):
        self.assertEqual(san.OPAQUE_MESSAGE_RE.pattern, codex.OPAQUE_MESSAGE_RE.pattern)
        self.assertTrue(codex.OPAQUE_MESSAGE_RE.match(san.FAKE_OPAQUE))
        self.assertTrue(codex._is_opaque_message(san.FAKE_OPAQUE))
        out, text = self.run_one({"tool_input": {"message": self.SECRETS[5], "other": self.SECRETS[5]}})
        self.assertEqual(out[1]["payload"]["tool_input"]["message"], san.FAKE_OPAQUE)
        self.assertEqual(out[1]["payload"]["tool_input"]["other"], san.FAKE_OPAQUE)
        self.assertNotIn(self.SECRETS[5], text)

    def test_exit_code_header_survives_but_not_the_output(self):
        out, text = self.run_one({"error": "Exit code 2\nls: cannot access 'hunter2-password'", "tool_response": "Process exited with code 1\nsecret"})
        self.assertEqual(out[1]["payload"]["error"], "Exit code 2\n__TEXT__")
        self.assertEqual(out[1]["payload"]["tool_response"], "Process exited with code 1\n__TEXT__")
        self.assertNotIn("hunter2", text)

    def test_local_user_and_host_names_are_removed_from_allowlisted_values(self):
        names = local_names()
        if not names:
            self.skipTest("no user or host name could be read")
        out, text = self.run_one({"model": "model-" + names[0], "agent_type": names[0]})
        self.assertEqual((out[1]["payload"]["model"], out[1]["payload"]["agent_type"]), ("__STR__", "__STR__"))
        self.assertNotIn(names[0], text.lower())

    def test_meta_line_is_first_without_date_or_host(self):
        meta = dict(self.META, provenance="older CLI, not 0.166")
        out = san.sanitize_lines([{"mode": "stop", "payload": {}, "result": {}}, {"payload": {"hook_event_name": "Stop"}, "TAIL": "x"}], meta)
        self.assertEqual(list(out[0]), ["_meta"])
        self.assertEqual(out[0]["_meta"], meta)
        self.assertEqual([o["mode"] for o in out[1:]], ["stop", "stop"])
        self.assertTrue(all(set(o) == {"mode", "payload", "result"} for o in out[1:]), "top-level extras are dropped")

    def test_command_line_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, dest = Path(tmp) / "raw.jsonl", Path(tmp) / "out" / "x.jsonl"
            raw.write_text(json.dumps({"mode": "stop", "payload": {"cwd": tmp, "last_assistant_message": "private words", "session_id": "abc"}, "result": {}}) + "\n", encoding="utf-8")
            self.assertEqual(san.main([str(raw), str(dest), "--platform", "claude", "--cli-version", "9.9", "--scenario", "demo"]), 0)
            lines = [json.loads(x) for x in dest.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0], {"_meta": {"platform": "claude", "cli_version": "9.9", "scenario": "demo", "sanitizer_version": san.SANITIZER_VERSION}})
            self.assertEqual(lines[1]["payload"], {"cwd": "__WS__", "last_assistant_message": "__TEXT__", "session_id": "__ID1__"})
            with redirect_stderr(io.StringIO()):
                self.assertEqual(san.main([str(Path(tmp) / "missing"), str(dest), "--platform", "p", "--cli-version", "1", "--scenario", "s"]), 2)


if __name__ == "__main__":
    unittest.main()
