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


class TestSavingsCcusageRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "-home-me-proj"
        self.project.mkdir()
        self.sid = "aaaaaaaa-2222-4333-8444-555555555555"
        write_jsonl(self.project / f"{self.sid}.jsonl", [
            assistant("claude-opus-5-5", "p1", 1000, 0, 0, 1000, ts="2026-10-19T10:00:00.000Z"),
        ])
        subs = self.project / self.sid / "subagents"
        write_jsonl(subs / "agent-h.jsonl", [assistant("claude-haiku-5-5", "h1", 1_000_000, 0, 0, 100_000, ts="2026-10-19T10:01:00.000Z")])
        write_jsonl(subs / "agent-s.jsonl", [
            assistant("claude-sonnet-5-5", "s1", 500_000, 100_000, 0, 100_000, ts="2026-10-19T10:02:03.456Z"),
            assistant("claude-mystery-9", "m1", 5, 0, 0, 5, ts="2026-10-19T10:02:04.000Z"),
        ])
        self.summary = cr.summarize(cr.collect_session(self.project, self.sid), cr.DEFAULT_PRICES)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cr.main(["--project", str(self.project), *argv])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_savings_math(self):
        sv = self.summary["savings"]
        self.assertEqual(sv["primary_model"], "claude-opus-5-5")
        self.assertAlmostEqual(sv["actual_usd"], 0.024 + 0.15 + 2.25, places=9)
        self.assertAlmostEqual(sv["counterfactual_usd"], 0.024 + 6.0 + 4.5, places=9)
        self.assertAlmostEqual(sv["savings_usd"], 8.1, places=9)
        self.assertAlmostEqual(sv["savings_pct"], 8.1 / 10.524 * 100, places=6)
        text = self.run_main("--debug-log", str(Path(self.tmp.name) / "none.log"))
        self.assertIn("Savings vs all-frontier (same tokens on claude-opus-5-5): $8.10 (77.0%)", text)
        self.assertIn("different number of tokens", text)
        self.assertIn("no price for: claude-mystery-9", text)
        self.assertNotIn("Routing", text)

    def test_primary_tie_breaks_on_price(self):
        rows = [
            {"model": "claude-haiku-5-5", "role": "primary", "turns": 1, "usd": 1.0},
            {"model": "claude-opus-5-5", "role": "primary", "turns": 1, "usd": 1.0},
        ]
        self.assertEqual(cr.compute_savings(rows, cr.DEFAULT_PRICES)["primary_model"], "claude-opus-5-5")
        rows[0]["turns"] = 2
        self.assertEqual(cr.compute_savings(rows, cr.DEFAULT_PRICES)["primary_model"], "claude-haiku-5-5")
        self.assertIsNone(cr.compute_savings([], cr.DEFAULT_PRICES))

    def test_ccusage_shape(self):
        data = json.loads(self.run_main("--format", "ccusage"))
        self.assertEqual(set(data), {"sessions", "totals"})
        sess = data["sessions"][0]
        self.assertEqual(
            list(sess),
            ["sessionId", "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens",
             "totalCost", "lastActivity", "firstActivity", "modelsUsed", "modelBreakdowns", "projectPath"],
        )
        self.assertEqual(sess["inputTokens"], 1000 + 1_000_000 + 500_000 + 5)
        self.assertEqual(sess["cacheCreationTokens"], 100_000)
        self.assertEqual(sess["totalTokens"], sess["inputTokens"] + sess["outputTokens"] + sess["cacheCreationTokens"] + sess["cacheReadTokens"])
        self.assertEqual(sess["lastActivity"], "2026-10-19T10:02:04.000Z")
        self.assertEqual(sess["firstActivity"], "2026-10-19T10:00:00.000Z")
        self.assertEqual(sess["projectPath"], "-home-me-proj")
        names = {b["modelName"]: b for b in sess["modelBreakdowns"]}
        self.assertEqual(set(names), {"claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-mystery-9"})
        self.assertTrue(names["claude-mystery-9"]["missingPricing"])
        self.assertNotIn("missingPricing", names["claude-haiku-5-5"])
        self.assertEqual(set(names["claude-haiku-5-5"]), {"modelName", "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "cost"})
        self.assertEqual(data["totals"]["unpricedModels"], ["claude-mystery-9"])
        self.assertEqual(data["totals"]["totalTokens"], sess["totalTokens"])
        self.assertAlmostEqual(data["totals"]["totalCost"], sess["totalCost"], places=9)

    def test_ccusage_merges_roles_and_sorts_by_cost(self):
        other = "bbbbbbbb-2222-4333-8444-555555555555"
        write_jsonl(self.project / f"{other}.jsonl", [assistant("claude-haiku-5-5", "x", 10, 0, 0, 10)])
        write_jsonl(self.project / self.sid / "subagents" / "agent-o.jsonl", [assistant("claude-opus-5-5", "o1", 10, 0, 0, 10)])
        data = json.loads(self.run_main("--last", "2", "--format", "ccusage"))
        self.assertEqual([x["sessionId"] for x in data["sessions"]], [self.sid, other])
        opus = [b for b in data["sessions"][0]["modelBreakdowns"] if b["modelName"] == "claude-opus-5-5"]
        self.assertEqual(len(opus), 1)
        self.assertEqual(opus[0]["inputTokens"], 1010)
        self.assertNotIn("unpricedModels", json.loads(self.run_main("--session", other, "--format", "ccusage"))["totals"])

    def test_json_alias_and_savings_key(self):
        data = json.loads(self.run_main("--json"))
        self.assertEqual(data, json.loads(self.run_main("--format", "json")))
        self.assertEqual(data[0]["savings"]["primary_model"], "claude-opus-5-5")
        self.assertEqual(set(data[0]["savings"]), {"primary_model", "actual_usd", "counterfactual_usd", "savings_usd", "savings_pct"})
        self.assertIsNone(data[0]["routing"])

    def test_routing_stats(self):
        log = Path(self.tmp.name) / "debug.log"
        lines = [
            json.dumps({"decisions": [
                {"tier": "flash_lite", "rule": "scout", "source": "rule", "confidence": 0.9},
                {"tier": "flash", "rule": "impl", "source": "judge", "confidence": 0.8},
            ]}),
            "{not json",
            json.dumps({"mode": "x"}),
            json.dumps({"decisions": [
                {"tier": "flash", "rule": "impl", "source": "default", "confidence": 0.4},
                {"tier": "flash", "rule": "impl", "source": "judge", "confidence": 0.6},
            ]}),
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        r = cr.routing_stats([log])
        self.assertEqual(r["decisions"], 4)
        self.assertEqual(r["tiers"]["flash"]["count"], 3)
        self.assertAlmostEqual(r["tiers"]["flash_lite"]["pct"], 25.0)
        self.assertEqual(r["rules"], {"impl": 3, "scout": 1})
        self.assertEqual(r["sources"], {"default": 1, "judge": 2, "rule": 1})
        self.assertAlmostEqual(r["judge_hit_rate"], 2 / 3)
        self.assertAlmostEqual(r["mean_confidence"]["flash"], 0.6)
        self.assertAlmostEqual(r["mean_confidence"]["flash_lite"], 0.9)
        text = self.run_main("--debug-log", str(log))
        self.assertIn("Routing", text)
        self.assertIn("judge decisions / decisions with source judge or default", text)
        data = json.loads(self.run_main("--json", "--debug-log", str(log)))
        self.assertEqual(data[0]["routing"]["decisions"], 4)
        self.assertNotIn("routing", json.loads(self.run_main("--format", "ccusage", "--debug-log", str(log))))

    def test_routing_absent_without_log(self):
        self.assertIsNone(cr.routing_stats([]))
        self.assertIsNone(cr.routing_stats([Path(self.tmp.name) / "missing.log"]))
        self.assertEqual(cr.sanitise_sid("a/b.c" + "x" * 200), ("a_b_c" + "x" * 200)[:128])


if __name__ == "__main__":
    unittest.main()
