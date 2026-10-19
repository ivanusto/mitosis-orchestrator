#!/usr/bin/env python3
"""Unit tests for scripts/mitosis-cost-report.py."""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mitosis-cost-report.py"
spec = importlib.util.spec_from_file_location("mitosis_cost_report", SCRIPT)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)


def assistant(model, msg_id, inp, cw, crd, out, thinking=0, ts="2026-10-19T10:00:00.000Z", **extra):
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": crd,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": thinking},
            },
        },
    }
    rec.update(extra)
    return rec


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class CostReportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "-home-me-repo"
        self.project.mkdir()
        self.sid = "11111111-2222-4333-8444-555555555555"
        main = [
            {"type": "user", "timestamp": "2026-10-19T10:00:00.000Z", "message": {"role": "user", "content": "hi"}},
            # same message id twice (one per streamed content block): must count once
            assistant("claude-fable-5-1", "msg_1", 100, 1000, 10000, 1700, thinking=200, ts="2026-10-19T10:00:05.000Z"),
            assistant("claude-fable-5-1", "msg_1", 100, 1000, 10000, 1700, thinking=200, ts="2026-10-19T10:00:06.000Z"),
            assistant("claude-fable-5-1", "msg_2", 50, 0, 20000, 300, ts="2026-10-19T10:05:00.000Z"),
            # inline sidechain record (older layout) counts as subagent
            assistant("claude-haiku-5-5", "msg_3", 10, 0, 1000, 100, ts="2026-10-19T10:06:00.000Z", isSidechain=True),
            {"type": "assistant", "message": {"role": "assistant", "content": "no usage here"}},
            "not json at all",
        ]
        write_jsonl(self.project / f"{self.sid}.jsonl", main)
        subs = self.project / self.sid / "subagents"
        write_jsonl(subs / "agent-aaa.jsonl", [assistant("claude-haiku-5-5", "msg_s1", 20, 0, 2000, 200, ts="2026-10-19T10:07:00.000Z")])
        write_jsonl(subs / "agent-bbb.jsonl", [
            assistant("claude-sonnet-5-5", "msg_s2", 1000, 5000, 50000, 4000, thinking=1000, ts="2026-10-19T10:08:00.000Z"),
            assistant("claude-sonnet-5-5", "msg_s2", 1000, 5000, 50000, 4000, thinking=1000, ts="2026-10-19T10:10:00.000Z"),
            assistant("claude-mystery-9", "msg_s3", 1, 0, 0, 1, ts="2026-10-19T10:11:00.000Z"),
        ])
        self.session = cr.collect_session(self.project, self.sid)
        self.summary = cr.summarize(self.session, cr.DEFAULT_PRICES)

    def tearDown(self):
        self.tmp.cleanup()


class TestCollect(CostReportBase):
    def test_dedupes_by_message_id_and_splits_roles(self):
        fable = self.session["models"]["claude-fable-5-1"]["primary"]
        self.assertEqual(fable["turns"], 2)
        self.assertEqual(fable["input"], 150)
        self.assertEqual(fable["cache_write"], 1000)
        self.assertEqual(fable["cache_read"], 30000)
        self.assertEqual(fable["output"], 2000)
        self.assertEqual(fable["thinking"], 200)
        haiku = self.session["models"]["claude-haiku-5-5"]
        self.assertNotIn("primary", haiku)
        self.assertEqual(haiku["subagent"]["turns"], 2)  # sidechain + subagent file
        self.assertEqual(haiku["subagent"]["cache_read"], 3000)
        sonnet = self.session["models"]["claude-sonnet-5-5"]["subagent"]
        self.assertEqual(sonnet["turns"], 1)
        self.assertEqual(sonnet["output"], 4000)
        self.assertEqual(self.session["subagent_files"], 2)
        self.assertEqual(self.session["duration_s"], 11 * 60)

    def test_cost_math(self):
        fable_row = next(r for r in self.summary["rows"] if r["model"] == "claude-fable-5-1")
        expected = (150 * 10 + 1000 * 20 + 30000 * 0.25 + 2000 * 50) / 1e6  # primary: 1h cache-write rate (2x input)
        self.assertAlmostEqual(fable_row["usd"], expected, places=9)
        sonnet_row = next(r for r in self.summary["rows"] if r["model"] == "claude-sonnet-5-5")
        self.assertAlmostEqual(sonnet_row["usd"], (1000 * 2 + 5000 * 2.5 + 50000 * 0.2 + 4000 * 10) / 1e6, places=9)  # subagent: 5m rate
        five_min = cr.summarize(self.session, cr.DEFAULT_PRICES, primary_cache_ttl="5m")
        fable_5m = next(r for r in five_min["rows"] if r["model"] == "claude-fable-5-1")
        self.assertAlmostEqual(fable_5m["usd"], (150 * 10 + 1000 * 12.5 + 30000 * 0.25 + 2000 * 50) / 1e6, places=9)
        self.assertEqual(self.summary["cache_ttl"], {"primary": "1h", "subagent": "5m"})
        mystery = next(r for r in self.summary["rows"] if r["model"] == "claude-mystery-9")
        self.assertIsNone(mystery["usd"])
        self.assertEqual(self.summary["unknown_models"], ["claude-mystery-9"])
        self.assertAlmostEqual(self.summary["totals_usd"]["primary"], expected, places=9)
        self.assertGreater(self.summary["primary_usd_share"], 0.5)
        self.assertLess(self.summary["primary_token_share"], 0.5)

    def test_price_lookup_aliases_and_prefixes(self):
        self.assertEqual(cr.price_for("opus", cr.DEFAULT_PRICES), cr.DEFAULT_PRICES["claude-opus-5-5"])
        self.assertEqual(cr.price_for("claude-opus-5-5-20260401", cr.DEFAULT_PRICES), cr.DEFAULT_PRICES["claude-opus-5-5"])
        self.assertEqual(cr.price_for("claude-opus-5", cr.DEFAULT_PRICES), cr.DEFAULT_PRICES["claude-opus-5"])
        self.assertIsNone(cr.price_for("gemini-flash", cr.DEFAULT_PRICES))

    def test_prices_override_file(self):
        override = Path(self.tmp.name) / "prices.json"
        override.write_text(json.dumps({"claude-mystery-9": {"input": 1, "cache_write": 1, "cache_read": 1, "output": 1}, "claude-fable-5-1": [0, 0, 0, 0]}), encoding="utf-8")
        prices = cr.resolve_prices(str(override))
        summary = cr.summarize(self.session, prices)
        self.assertEqual(summary["unknown_models"], [])
        self.assertEqual(summary["totals_usd"]["primary"], 0.0)

    def test_encode_project_dir(self):
        self.assertEqual(cr.encode_project_dir("/home/me/repo"), "-home-me-repo")
        self.assertEqual(cr.encode_project_dir("/tmp/claude-1000/-home-me/x.y"), "-tmp-claude-1000--home-me-x-y")


class TestCli(CostReportBase):
    def test_text_and_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cr.main(["--project", str(self.project)])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("claude-fable-5-1", text)
        self.assertIn("primary share", text)
        self.assertIn("no price for: claude-mystery-9", text)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cr.main(["--project", str(self.project), "--json"])
        data = json.loads(buf.getvalue())
        self.assertEqual(data[0]["session_id"], self.sid)

    def test_last_n_picks_newest_and_prints_comparison(self):
        older = "22222222-2222-4333-8444-555555555555"
        write_jsonl(self.project / f"{older}.jsonl", [assistant("claude-opus-5-5", "m", 10, 0, 0, 10)])
        past = time.time() - 3600
        os.utime(self.project / f"{older}.jsonl", (past, past))
        self.assertEqual(cr.list_sessions(self.project)[0], self.sid)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cr.main(["--project", str(self.project), "--last", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("Comparison", buf.getvalue())
        self.assertIn("claude-opus-5-5", buf.getvalue())

    def test_reproduces_claude_code_cost_accounting(self):
        # Figures reported by `claude -p --output-format json` (modelUsage) on 2026-10-19.
        opus = {"input": 4, "cache_write": 21995, "cache_read": 41365, "output": 519, "thinking": 114}
        haiku = {"input": 4, "cache_write": 3883, "cache_read": 7278, "output": 941, "thinking": 250}
        self.assertAlmostEqual(cr.cost_usd(opus, cr.DEFAULT_PRICES["claude-opus-5-5"], "1h"), 0.194629, places=5)
        self.assertAlmostEqual(cr.cost_usd(haiku, cr.DEFAULT_PRICES["claude-haiku-5-5"], "5m"), 0.001029, places=5)

    def test_missing_project(self):
        rc = cr.main(["--project", str(Path(self.tmp.name) / "nope")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
