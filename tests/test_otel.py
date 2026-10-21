#!/usr/bin/env python3
"""OTel GenAI invoke_agent spans and the debug-log effort fix.
Run: python -m unittest discover tests"""

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enforce-orchestrator.py"
ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_", "OTEL_")
SID = "otel-session-1"
PAYLOAD = {
    "hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Agent",
    "tool_input": {"prompt": "Locate parse_token in src/", "description": "lookup"},
}


def attrs(span):
    out = {}
    for a in span["attributes"]:
        (kind, val), = a["value"].items()
        out[a["key"]] = val
    return out


class OtelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
        self.env["TMPDIR"] = self.tmp.name
        self.file = os.path.join(self.tmp.name, "spans.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, extra=None, payload=PAYLOAD):
        env = dict(self.env)
        env.update(extra or {})
        start = time.time()
        proc = subprocess.run([sys.executable, str(SCRIPT), "pre-tool-use"], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)
        self.elapsed = time.time() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def spans(self):
        with open(self.file) as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        out = []
        for body in lines:
            for rs in body["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return lines, out

    def test_file_span(self):
        self.run_hook({"MITOSIS_OTEL": "1", "MITOSIS_OTEL_FILE": self.file})
        lines, spans = self.spans()
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(spans), 1)
        res = lines[0]["resourceSpans"][0]
        self.assertEqual(res["resource"]["attributes"][0]["value"]["stringValue"], "mitosis-orchestrator")
        self.assertEqual(res["scopeSpans"][0]["scope"]["name"], "mitosis-orchestrator")
        span = spans[0]
        self.assertTrue(span["name"].startswith("invoke_agent "))
        self.assertEqual(span["kind"], 1)
        self.assertEqual(span["traceId"], hashlib.sha256(SID.encode()).hexdigest()[:32])
        self.assertEqual(len(span["spanId"]), 16)
        self.assertEqual(span["startTimeUnixNano"], span["endTimeUnixNano"])
        a = attrs(span)
        self.assertEqual(a["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(a["gen_ai.provider.name"], "anthropic")
        self.assertEqual(a["gen_ai.request.model"], "haiku")
        self.assertEqual(a["gen_ai.conversation.id"], SID)
        self.assertEqual(a["mitosis.tier"], "flash_lite")
        self.assertEqual(a["mitosis.effort"], "low")
        self.assertEqual(a["mitosis.platform"], "claude")
        self.assertTrue(a["mitosis.rule"])
        self.assertIsInstance(a["mitosis.confidence"], float)
        self.assertTrue(a["mitosis.source"])

    def test_disabled_by_default(self):
        self.run_hook({"MITOSIS_OTEL_FILE": self.file})
        self.assertFalse(os.path.exists(self.file))

    def test_enabled_without_destination_is_noop(self):
        self.run_hook({"MITOSIS_OTEL": "1"})
        self.assertFalse(os.path.exists(self.file))

    def test_http_collector(self):
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got["path"] = self.path
                got["auth"] = self.headers.get("Authorization")
                got["ctype"] = self.headers.get("Content-Type")
                got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.run_hook({
                "MITOSIS_OTEL": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:%d/" % server.server_address[1],
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20tok,x-other=1",
                "MITOSIS_OTEL_TIMEOUT": "5",
            })
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(got["path"], "/v1/traces")
        self.assertEqual(got["auth"], "Bearer tok")
        self.assertEqual(got["ctype"], "application/json")
        self.assertEqual(len(got["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]), 1)

    def test_unreachable_endpoint_is_harmless(self):
        base = self.run_hook()
        out = self.run_hook({
            "MITOSIS_OTEL": "1",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces",
            "MITOSIS_OTEL_TIMEOUT": "0.2",
        })
        self.assertEqual(out, base)
        self.assertLess(self.elapsed, 1.5)

    def test_debug_log_records_pinned_effort(self):
        self.run_hook({"CLAUDE_MITOSIS_DEBUG": "1", "CLAUDE_MITOSIS_SCOUT_EFFORT": "medium"})
        log = Path(self.tmp.name) / "mitosis-orchestrator" / SID / "mitosis_orchestrator_debug.log"
        entry = json.loads(log.read_text().splitlines()[-1])
        decision = entry["decisions"][0]
        self.assertEqual(len(entry["decisions"]), 1)
        self.assertEqual(decision["tier"], "flash_lite")
        self.assertEqual(decision["effort"], "medium")
        self.assertEqual(decision["model"], "haiku")
        self.assertEqual(decision["platform"], "claude")
        updated = entry["result"]["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
