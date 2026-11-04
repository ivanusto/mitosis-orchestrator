#!/usr/bin/env python3
"""Tests for evals/builtin-pin: schedule, arm environment, decision rule, cost parsing, validity, seal, budget.

No real CLI is ever started: a fake `claude` executable on PATH plays the part. Nothing here spends money.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

REPO = Path(__file__).resolve().parent.parent
COMPARE = REPO / "evals" / "compare"
OVERHEAD = REPO / "evals" / "overhead"
PIN = REPO / "evals" / "builtin-pin"
LOOKUP = COMPARE / "tasks" / "lookup"


def snapshot(root):
    """Every file under root (bytecode included) -> sha256."""
    out = {}
    for base, dirs, files in os.walk(str(root)):
        dirs.sort()
        for name in sorted(files):
            full = Path(base) / name
            out[full.relative_to(root).as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


COMPARE_BEFORE = snapshot(COMPARE)    # taken before this module imports anything from evals
OVERHEAD_BEFORE = snapshot(OVERHEAD)
PIN_BEFORE = snapshot(PIN)

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("builtin_pin_run_under_test", str(PIN / "run.py"))
bp = importlib.util.module_from_spec(_spec)
sys.modules["builtin_pin_run_under_test"] = bp
_spec.loader.exec_module(bp)

POSIX_ONLY = unittest.skipIf(os.name == "nt", "the harness and its fake claude are POSIX-only")


def rmtree(path):
    for base, dirs, files in os.walk(str(path)):
        try:
            os.chmod(base, 0o755)
        except OSError:
            pass
        for name in files:
            try:
                os.chmod(os.path.join(base, name), 0o644)
            except OSError:
                pass
    shutil.rmtree(str(path), ignore_errors=True)


def sh(args, cwd=None, env=None, timeout=600):
    return subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)


# ---------------------------------------------------------------------------------------------
# helpers to build run records for the decision rule
# ---------------------------------------------------------------------------------------------

PAIRS = [(b, r) for b in range(1, 7) for r in range(1, 4)]    # 18 (batch, round) pairs


def spread(total):
    """18 per-run correct counts (each 0 to 5) that sum to total."""
    out, left = [], total
    for _ in PAIRS:
        n = min(5, left)
        out.append(n)
        left -= n
    return out


def records(a_total=90, b_total=90, p_costs=None, i_costs=None, invalid=(), comp=()):
    """36 records. invalid: set of (arm, pair index); comp: set of pair indexes whose pinned run has compensation reads."""
    pc = p_costs or [Fraction(1, 2)] * 18
    ic = i_costs or [Fraction(1)] * 18
    out = []
    for arm, correct, costs in (("pinned", spread(a_total), pc), ("inherit", spread(b_total), ic)):
        for k, (b, r) in enumerate(PAIRS):
            out.append({"id": "r%d__b%d__%s" % (r, b, arm), "arm": arm, "batch": b, "round": r, "valid": (arm, k) not in invalid,
                        "correct": correct[k], "cost": costs[k], "comp": arm == "pinned" and k in comp, "reasons": []})
    return out


# ---------------------------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------------------------

class ScheduleTests(unittest.TestCase):
    def test_batches_are_deterministic_and_balanced(self):
        ids = bp.question_ids()
        self.assertEqual(len(ids), 30)
        for seed in (1, 7, 20261105):
            batches = bp.make_batches(seed)
            self.assertEqual(batches, bp.make_batches(seed))
            self.assertEqual(len(batches), 6)
            for b in batches:
                self.assertEqual(len(b), 5)
                self.assertEqual(len(set(b)), 5)
            flat = [q for b in batches for q in b]
            self.assertEqual(sorted(flat), ids)               # all 30 used exactly once
        self.assertTrue(any(bp.make_batches(s) != bp.make_batches(1) for s in range(2, 20)))

    def test_schedule_has_36_cells_two_arms_per_batch_and_round(self):
        for seed in (1, 7, 20261105):
            cells = bp.make_schedule(seed)
            self.assertEqual(cells, bp.make_schedule(seed))
            self.assertEqual(len(cells), 36)
            self.assertEqual(len(set(cells)), 36)
            for r in (1, 2, 3):
                block = [c for c in cells if c[0] == r]
                self.assertEqual(len(block), 12)
                self.assertEqual(cells[(r - 1) * 12:r * 12], block)   # rounds stay contiguous
                for b in range(1, 7):
                    self.assertEqual(sorted(a for rr, bb, a in block if bb == b), sorted(bp.ARMS))
        self.assertTrue(any(bp.make_schedule(s) != bp.make_schedule(1) for s in range(2, 20)))
        self.assertEqual(sorted(bp.warmup_order(3)), sorted(bp.ARMS))
        self.assertEqual(sorted(bp.calibration_order(3)), sorted(bp.ARMS))

    def test_prompt_is_the_fixture_preamble_five_questions_and_the_sealed_instruction(self):
        batch = bp.make_batches(5)[2]
        prompt = bp.build_prompt(batch)
        lines = [ln for ln in prompt.splitlines() if re.match(r"^- q\d\d: ", ln)]
        self.assertEqual([ln[2:5] for ln in lines], batch)
        self.assertIn("Do not answer from memory", prompt)
        self.assertTrue(prompt.endswith(bp.PROMPT_TAIL))
        self.assertIn("exactly one subagent of type `Explore`", prompt)
        self.assertNotIn("pin", prompt.lower())
        self.assertNotIn("cost", prompt.lower())
        with self.assertRaises(bp.Refused):
            bp.build_prompt(["q99"])

    def test_protocol_states_the_sealed_constants(self):
        text = (PIN / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn(bp.PROMPT_TAIL.strip(), text.replace("> ", ""))
        for needle in ("20.00 USD", "1.50 USD", "70%", "14 of the 18", "A <= B - 5", "A >= B - 2", "not a formal non-inferiority test",
                       "stays off in 2.7", "CLAUDE_MITOSIS_PIN_BUILTINS=1", "does not separate model from effort", "tenfold"):
            self.assertIn(needle, text)
        self.assertEqual((bp.BUDGET_LIMIT_USD, bp.RUN_CAP_USD, bp.EXPECTED_RUNS), (20.0, 1.5, 36))


# ---------------------------------------------------------------------------------------------
# arm environment and command
# ---------------------------------------------------------------------------------------------

class EnvTests(unittest.TestCase):
    CALLER = {"PATH": "/bin", "HOME": "/h", "CLAUDE_MITOSIS_PIN_BUILTINS": "0", "CLAUDE_MITOSIS_PIN_EFFORT": "0",
              "CLAUDE_MITOSIS_FASTPATH_MODE": "enforce", "AGY_MITOSIS_DEBUG": "0", "CODEX_MITOSIS_X": "1", "AGY_ARGON_DEBUG": "1",
              "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_SUBAGENT_EFFORT": "high", "ANTHROPIC_API_KEY": "k"}

    def test_the_arms_differ_in_exactly_the_flag(self):
        for base in (self.CALLER, {"PATH": "/bin"}, dict(self.CALLER, CLAUDE_MITOSIS_PIN_BUILTINS="1")):
            pinned, inherit = bp.arm_env("pinned", base), bp.arm_env("inherit", base)
            self.assertEqual(pinned["CLAUDE_MITOSIS_PIN_BUILTINS"], "1")
            self.assertNotIn("CLAUDE_MITOSIS_PIN_BUILTINS", inherit)
            self.assertEqual({k: v for k, v in pinned.items() if k != "CLAUDE_MITOSIS_PIN_BUILTINS"}, inherit)
            self.assertEqual(set(pinned) ^ set(inherit), {"CLAUDE_MITOSIS_PIN_BUILTINS"})
            for env in (pinned, inherit):
                self.assertEqual(env["CLAUDE_MITOSIS_DEBUG"], "1")
                self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(bp.env_parity_problems(), [])
        with self.assertRaises(ValueError):
            bp.arm_env("other", {})

    def test_the_callers_mitosis_variables_and_subagent_overrides_never_reach_the_child(self):
        for arm in bp.ARMS:
            env = bp.arm_env(arm, self.CALLER)
            self.assertEqual(sorted(k for k in env if k.startswith(("CLAUDE_MITOSIS_", "AGY_", "CODEX_MITOSIS_"))),
                             ["CLAUDE_MITOSIS_DEBUG"] + (["CLAUDE_MITOSIS_PIN_BUILTINS"] if arm == "pinned" else []))
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_EFFORT", env)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        self.assertIn("CLAUDE_MITOSIS_PIN_BUILTINS", self.CALLER)      # the caller's mapping is not modified

    def test_commands_are_identical_for_both_arms(self):
        cmd = bp.build_command("claude", "PROMPT\n", "/frozen", bp.RUN_CAP_USD)
        self.assertEqual(cmd[2], "PROMPT\n")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1.5")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], "/frozen")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn('"mitosis-orchestrator@mitosis-orchestrator":false', cmd[cmd.index("--settings") + 1])
        self.assertIn("Agent", cmd[cmd.index("--allowedTools") + 1].split(","))
        self.assertNotIn("--plugin-dir", bp.cmp.build_command("claude", "off", "lookup", "P", None, 1.5))   # compare's own command, extended here

    def test_flag_resolution_records_env_settings_and_result(self):
        r = bp.flag_resolution("pinned", {"CLAUDE_MITOSIS_PIN_BUILTINS": "1"}, {})
        self.assertEqual((r["env_passed"], r["settings_value"], r["resolved"], r["as_intended"]), ("1", None, "1", True))
        r = bp.flag_resolution("inherit", {}, {})
        self.assertEqual((r["resolved"], r["as_intended"]), (None, True))
        self.assertFalse(bp.flag_resolution("pinned", {"CLAUDE_MITOSIS_PIN_BUILTINS": "1"}, {"CLAUDE_MITOSIS_PIN_BUILTINS": "0"})["as_intended"])
        self.assertFalse(bp.flag_resolution("inherit", {}, {"CLAUDE_MITOSIS_PIN_BUILTINS": "1"})["as_intended"])
        self.assertFalse(bp.flag_resolution("pinned", {}, {})["as_intended"])

    def test_settings_env_is_read_from_the_settings_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-settings-"))
        self.addCleanup(rmtree, tmp)
        (tmp / "settings.json").write_text(json.dumps({"env": {"CLAUDE_MITOSIS_DEBUG": "1", "N": 3}}), encoding="utf-8")
        (tmp / "settings.local.json").write_text("not json", encoding="utf-8")
        merged, used = bp.read_settings_env([tmp / "settings.json", tmp / "settings.local.json", tmp / "missing.json"])
        self.assertEqual(merged, {"CLAUDE_MITOSIS_DEBUG": "1", "N": "3"})
        self.assertEqual(used, [str(tmp / "settings.json")])
        self.assertEqual(bp.settings_files({"CLAUDE_CONFIG_DIR": str(tmp)})[0], tmp / "settings.json")


# ---------------------------------------------------------------------------------------------
# decision rule
# ---------------------------------------------------------------------------------------------

class RuleTests(unittest.TestCase):
    def outcome(self, **kw):
        return bp.decide(records(**kw))

    def test_baseline_is_supported(self):
        d = self.outcome()
        self.assertEqual(d["outcome"], "SUPPORTED", d["reason"])
        self.assertEqual((d["A"], d["B"], d["cheaper_pairs"], d["n_pairs_usable"]), (90, 90, 18, 18))

    def test_quality_boundaries(self):
        self.assertEqual(self.outcome(a_total=85)["outcome"], "QUALITY LOSS")            # A = B - 5
        self.assertEqual(self.outcome(a_total=80)["outcome"], "QUALITY LOSS")
        self.assertEqual(self.outcome(a_total=86)["outcome"], "INCONCLUSIVE")            # A = B - 4
        self.assertEqual(self.outcome(a_total=87)["outcome"], "INCONCLUSIVE")            # A = B - 3
        self.assertEqual(self.outcome(a_total=88)["outcome"], "SUPPORTED")               # A = B - 2
        self.assertEqual(self.outcome(a_total=90)["outcome"], "SUPPORTED")
        self.assertEqual(self.outcome(a_total=85, b_total=80)["outcome"], "SUPPORTED")   # compared with B, not 90
        self.assertEqual(self.outcome(a_total=75, b_total=80)["outcome"], "QUALITY LOSS")
        self.assertEqual(self.outcome(a_total=76, b_total=80)["outcome"], "INCONCLUSIVE")

    def test_median_cost_ratio_exactly_seventy_percent(self):
        self.assertEqual(self.outcome(p_costs=[Fraction(7, 10)] * 18)["outcome"], "SUPPORTED")
        self.assertEqual(self.outcome(p_costs=[0.7] * 18)["outcome"], "SUPPORTED")        # floats go through their decimal form
        d = self.outcome(p_costs=[0.701] * 18)
        self.assertEqual(d["outcome"], "INCONCLUSIVE")
        self.assertIn("above 70%", d["reason"])
        self.assertEqual(self.outcome(p_costs=[1.0] * 18)["outcome"], "INCONCLUSIVE")
        self.assertEqual(self.outcome(p_costs=[0.5] * 9 + [3.0] * 9)["outcome"], "INCONCLUSIVE")   # median of 18 is the mean of the middle two: 1.75

    def test_cheaper_in_14_of_18_but_not_13(self):
        cheap14 = [0.5] * 14 + [1.2] * 4
        cheap13 = [0.5] * 13 + [1.2] * 5
        d14, d13 = self.outcome(p_costs=cheap14), self.outcome(p_costs=cheap13)
        self.assertEqual((d14["outcome"], d14["cheaper_pairs"]), ("SUPPORTED", 14))
        self.assertEqual((d13["outcome"], d13["cheaper_pairs"]), ("INCONCLUSIVE", 13))
        self.assertIn("cheaper in 13 pairs", d13["reason"])
        tie = self.outcome(p_costs=[0.5] * 13 + [1.0] * 5)      # a tie is not cheaper
        self.assertEqual((tie["outcome"], tie["cheaper_pairs"]), ("INCONCLUSIVE", 13))

    def test_compensation_reads_above_twenty_percent_are_quality_loss(self):
        self.assertEqual(self.outcome(comp={0, 1, 2})["outcome"], "SUPPORTED")           # 3 of 18 = 16.7%
        d = self.outcome(comp={0, 1, 2, 3})                                              # 4 of 18 = 22.2%
        self.assertEqual(d["outcome"], "QUALITY LOSS")
        self.assertIn("compensation", d["reason"])
        self.assertTrue(bp.comp_exceeds(3, 10))
        self.assertFalse(bp.comp_exceeds(2, 10))       # exactly 20% does not exceed
        self.assertFalse(bp.comp_exceeds(0, 0))

    def test_invalid_share_exactly_twenty_percent_and_just_over(self):
        self.assertFalse(bp.invalid_exceeds(2, 10))
        self.assertTrue(bp.invalid_exceeds(3, 10))
        self.assertFalse(bp.invalid_exceeds(7, 36))
        self.assertTrue(bp.invalid_exceeds(8, 36))
        # 7 invalid runs (19.4%): pairs 0 to 2 lost completely, pair 3 on the pinned side; 14 usable pairs still decide
        seven = {("pinned", 0), ("inherit", 0), ("pinned", 1), ("inherit", 1), ("pinned", 2), ("inherit", 2), ("pinned", 3)}
        d = self.outcome(invalid=seven)
        self.assertEqual((d["n_invalid"], d["n_pairs_usable"], d["cheaper_pairs"], d["outcome"]), (7, 14, 14, "SUPPORTED"))
        d = self.outcome(invalid=seven | {("inherit", 4)})                               # 8 of 36 = 22.2%
        self.assertEqual((d["n_invalid"], d["outcome"]), (8, "INCONCLUSIVE"))
        self.assertIn("invalid", d["reason"])

    def test_dropped_pairs_count_as_not_cheaper(self):
        d = self.outcome(invalid={("pinned", 0), ("pinned", 1), ("pinned", 2), ("pinned", 3), ("pinned", 4)}, p_costs=[0.5] * 13 + [1.2] * 5)
        self.assertEqual((d["n_pairs_usable"], d["cheaper_pairs"], d["outcome"]), (13, 8, "INCONCLUSIVE"))

    def test_invalid_runs_come_before_quality_loss_and_incomplete_data_is_inconclusive(self):
        many = {("pinned", k) for k in range(8)}
        d = self.outcome(a_total=40, invalid=many)
        self.assertEqual(d["outcome"], "INCONCLUSIVE")
        self.assertEqual(self.outcome(a_total=40)["outcome"], "QUALITY LOSS")
        d = bp.decide(records()[:-1])
        self.assertEqual(d["outcome"], "INCONCLUSIVE")
        self.assertIn("incomplete", d["reason"])
        self.assertEqual(bp.decide([])["outcome"], "INCONCLUSIVE")

    def test_quality_loss_is_checked_on_valid_pairs_only(self):
        # the three worst pinned runs are invalid and their pairs drop out, so A and B are compared on the rest
        scores = records(a_total=85)
        bad = [r for r in scores if r["arm"] == "pinned" and r["correct"] == 0]
        self.assertEqual(len(bad), 1)
        bad[0]["valid"] = False
        self.assertEqual(bp.decide(scores)["outcome"], "SUPPORTED")


# ---------------------------------------------------------------------------------------------
# cost parsing, validity, scoring
# ---------------------------------------------------------------------------------------------

RESULT_EVENT = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 91234, "num_turns": 5, "session_id": "sess-1",
    "result": "done", "total_cost_usd": 0.43217, "permission_denials": [],
    "usage": {"input_tokens": 12, "output_tokens": 410, "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 9000},
    "modelUsage": {
        "claude-opus-5-5": {"inputTokens": 12, "outputTokens": 3400, "cacheReadInputTokens": 50000, "cacheCreationInputTokens": 9000,
                            "webSearchRequests": 0, "costUSD": 0.31, "contextWindow": 200000, "maxOutputTokens": 32000},
        "claude-haiku-5-5": {"inputTokens": 40, "outputTokens": 9100, "cacheReadInputTokens": 120000, "cacheCreationInputTokens": 15000,
                             "webSearchRequests": 0, "costUSD": 0.12217, "contextWindow": 200000, "maxOutputTokens": 32000},
    },
}


class CostTests(unittest.TestCase):
    def test_per_model_cost_and_tokens_from_model_usage(self):
        c = bp.parse_cost(RESULT_EVENT)
        self.assertEqual(set(c["models"]), {"claude-opus-5-5", "claude-haiku-5-5"})
        self.assertEqual(c["models"]["claude-haiku-5-5"]["output"], 9100)
        self.assertEqual(c["models"]["claude-opus-5-5"]["cache_write"], 9000)
        self.assertAlmostEqual(c["family_cost"]["opus"], 0.31)
        self.assertAlmostEqual(c["family_cost"]["haiku"], 0.12217)
        self.assertEqual(c["family_output"]["haiku"], 9100)       # modelUsage counts, not the 410 of the top-level usage
        self.assertAlmostEqual(c["usage_cost_sum"], 0.43217)
        self.assertAlmostEqual(c["effective"], 0.43217)
        self.assertFalse(c["disagree"])

    def test_effective_cost_is_the_larger_and_a_disagreement_is_flagged(self):
        e = dict(RESULT_EVENT, total_cost_usd=0.31)             # a CLI total that leaves out the subagent
        c = bp.parse_cost(e)
        self.assertAlmostEqual(c["effective"], 0.43217)
        self.assertTrue(c["disagree"])
        small = dict(RESULT_EVENT, total_cost_usd=0.4322, modelUsage=dict(RESULT_EVENT["modelUsage"]))
        self.assertFalse(bp.parse_cost(small)["disagree"])
        tiny = dict(RESULT_EVENT, total_cost_usd=0.43217 + 0.004)    # under 0.005 USD apart
        self.assertFalse(bp.parse_cost(tiny)["disagree"])
        only_total = bp.parse_cost({"total_cost_usd": 0.2})
        self.assertEqual((only_total["effective"], only_total["usage_cost_sum"], only_total["disagree"]), (0.2, None, False))
        self.assertIsNone(bp.parse_cost({})["effective"])
        self.assertIsNone(bp.parse_cost(None)["effective"])
        self.assertEqual(bp.model_family("claude-haiku-5-5"), "haiku")
        self.assertEqual(bp.model_family("claude-opus-5-5[1m]"), "opus")
        self.assertEqual(bp.model_family("gpt"), "other")

    def test_primary_tool_counts_leave_subagent_calls_out(self):
        def ev(block, parent=None):
            return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": "m", "content": [block]}}
        events = [
            ev({"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "Explore", "prompt": "x"}}),
            ev({"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "Explore", "prompt": "x"}}),   # streamed twice
            ev({"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "stdlib/a.py"}}),
            ev({"type": "tool_use", "id": "t3", "name": "Grep", "input": {}}, parent="t1"),
            ev({"type": "tool_use", "id": "t4", "name": "Write", "input": {}}),
            ev({"type": "tool_use", "id": "t5", "name": "Task", "input": {"subagent_type": "general-purpose"}}),
        ]
        counts, agents = bp.primary_tool_counts(events)
        self.assertEqual(counts, {"Agent": 1, "Read": 1, "Write": 1, "Task": 1})
        self.assertEqual(agents, ["Explore", "general-purpose"])
        self.assertTrue(bp.is_explore("Explore"))
        self.assertTrue(bp.is_explore(" explore "))
        self.assertFalse(bp.is_explore("general-purpose"))


class ValidityTests(unittest.TestCase):
    def sub_dir(self, *agents):
        """agents: (agentType, meta model or None, [transcript models] or None for no transcript)."""
        tmp = Path(tempfile.mkdtemp(prefix="bp-sub-"))
        self.addCleanup(rmtree, tmp)
        for i, (atype, meta_model, models) in enumerate(agents):
            meta = {"agentType": atype, "description": "d", "toolUseId": "toolu_%d" % i, "finished": True}
            if meta_model:
                meta["model"] = meta_model
            (tmp / ("agent-a%d.meta.json" % i)).write_text(json.dumps(meta), encoding="utf-8")
            if models is not None:
                lines = [{"type": "user", "message": {"role": "user", "content": "q"}}]
                lines += [{"type": "assistant", "message": {"id": "m%d" % k, "model": m, "content": [], "usage": {}}} for k, m in enumerate(models)]
                (tmp / ("agent-a%d.jsonl" % i)).write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        return tmp

    def problems(self, arm, *agents, calls=1):
        return bp.model_problems(arm, bp.subagent_facts(self.sub_dir(*agents)), calls)

    def test_pinned_needs_a_haiku_explore_and_inherit_an_opus_one(self):
        self.assertEqual(self.problems("pinned", ("Explore", "haiku", ["claude-haiku-5-5", "claude-haiku-5-5"])), [])
        self.assertEqual(self.problems("pinned", ("Explore", None, ["claude-haiku-5-5"])), [])
        self.assertEqual(self.problems("inherit", ("Explore", "inherit", ["claude-opus-5-5"])), [])
        self.assertEqual(self.problems("inherit", ("Explore", None, ["claude-opus-5-5"])), [])
        self.assertEqual(self.problems("pinned", ("Explore", "haiku", ["claude-opus-5-5"])), ["explore_wrong_model:claude-opus-5-5"])
        self.assertEqual(self.problems("inherit", ("Explore", None, ["claude-haiku-5-5"])), ["explore_wrong_model:claude-haiku-5-5"])
        self.assertEqual(self.problems("pinned", ("Explore", None, ["claude-haiku-5-5", "claude-sonnet-5-5"])), ["explore_wrong_model:claude-sonnet-5-5"])
        self.assertEqual(self.problems("pinned", ("Explore", "sonnet", ["claude-haiku-5-5"])), ["explore_meta_wrong_model:sonnet"])

    def test_no_explore_call_or_record_and_missing_transcripts_are_problems(self):
        self.assertEqual(self.problems("pinned", calls=0), ["no_explore_call", "no_explore_subagent_record"])
        self.assertEqual(self.problems("pinned", ("general-purpose", None, ["claude-haiku-5-5"])), ["no_explore_subagent_record"])
        self.assertEqual(self.problems("inherit", ("Explore", None, None)), ["explore_model_unverifiable"])
        self.assertEqual(self.problems("inherit", ("Explore", None, [])), ["explore_model_unverifiable"])
        self.assertEqual(self.problems("pinned", ("Explore", None, ["<synthetic>"])), ["explore_model_unverifiable"])
        self.assertEqual(self.problems("inherit", ("mitosis-orchestrator:flash-worker", "sonnet", ["claude-sonnet-5-5"]), calls=1),
                         ["no_explore_subagent_record"])
        self.assertEqual(bp.subagent_facts(Path(tempfile.gettempdir()) / "bp-no-such-dir"), [])

    def test_a_bad_meta_file_is_tolerated(self):
        d = self.sub_dir(("Explore", None, ["claude-opus-5-5"]))
        (d / "agent-a9.meta.json").write_text("[1]", encoding="utf-8")
        (d / "agent-a8.meta.json").write_text("{", encoding="utf-8")
        facts = bp.subagent_facts(d)
        self.assertEqual(len(facts), 3)
        self.assertEqual(len([f for f in facts if bp.is_explore(f["agent_type"])]), 1)

    def test_pin_decisions_in_the_hook_log(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-log-"))
        self.addCleanup(rmtree, tmp)
        log = tmp / "mitosis_orchestrator_debug.log"
        pin = {"mode": "pre-tool-use", "tool": "Agent", "decisions": [{"rule": "builtin-pin", "tier": "flash_lite"}],
               "result": {"hookSpecificOutput": {"permissionDecisionReason": "Mitosis Orchestrator pinned built-in Explore: haiku"}}}
        other = {"mode": "pre-tool-use", "tool": "Agent", "decisions": [{"rule": "x"}], "result": {}}
        log.write_text("\n".join(json.dumps(e) for e in (other, pin, {"mode": "subagent-stop", "tool": "Agent", "decisions": [{"rule": "builtin-pin"}]})) + "\nnot json\n", encoding="utf-8")
        self.assertEqual(bp.pin_decisions(log), (1, 3))
        log.write_text(json.dumps(other) + "\n", encoding="utf-8")
        self.assertEqual(bp.pin_decisions(log), (0, 1))
        self.assertEqual(bp.pin_decisions(tmp / "nope.log"), (0, None))
        self.assertTrue(str(bp.hook_debug_path("a/b c", "/t")).endswith(os.path.join("mitosis-orchestrator", "a_b_c", "mitosis_orchestrator_debug.log")))

    def test_validity_reasons(self):
        ok_flag = {"as_intended": True, "resolved": "1"}
        run = {"capped": False, "timed_out": False, "has_result": True, "leak_suspect": False, "stdlib_ok": True}
        facts = [{"agent_type": "Explore", "models": ["claude-haiku-5-5"], "meta_model": "haiku"}]
        self.assertEqual(bp.validity("pinned", facts, 1, 1, 4, ok_flag, run), [])
        self.assertEqual(bp.validity("pinned", facts, 1, 0, 4, ok_flag, run), ["no_pin_decision_in_hook_log"])
        self.assertEqual(bp.validity("pinned", facts, 1, 0, None, ok_flag, run), ["hook_log_missing"])
        self.assertEqual(bp.validity("pinned", facts, 1, 1, 4, dict(ok_flag, as_intended=False, resolved="0"), run), ["flag_not_as_intended:0"])
        ofacts = [{"agent_type": "Explore", "models": ["claude-opus-5-5"], "meta_model": None}]
        self.assertEqual(bp.validity("inherit", ofacts, 1, 0, None, {"as_intended": True, "resolved": None}, run), [])
        self.assertEqual(bp.validity("inherit", ofacts, 1, 1, 3, {"as_intended": True, "resolved": None}, run), ["pin_decision_in_inherit_arm"])
        bad = dict(run, capped=True, timed_out=True, has_result=False, leak_suspect=True, stdlib_ok=False)
        self.assertEqual(bp.validity("inherit", ofacts, 1, 0, 3, {"as_intended": True, "resolved": None}, bad),
                         ["hit_cap", "timed_out", "no_result_event", "leak_suspect", "stdlib_changed"])


class ScoringTests(unittest.TestCase):
    def test_exact_matches_through_graders_normalisation(self):
        key = json.loads((LOOKUP / "key.json").read_text(encoding="utf-8"))
        ids = ["q01", "q02", "q08", "q12", "q20"]
        good = {q: key[q] for q in ids}
        self.assertEqual(bp.score_answers(good, ids, key), (5, []))
        self.assertEqual(bp.score_answers(dict(good, q08="40"), ids, key)[0], 5)      # grade.same accepts a numeric string
        self.assertEqual(bp.score_answers(dict(good, q02=" " + key["q02"] + " "), ids, key)[0], 5)   # and strips strings
        self.assertEqual(bp.score_answers(dict(good, q02="urllib/error.py:36"), ids, key), (4, ["q02"]))
        self.assertEqual(bp.score_answers({q: key[q] for q in ids[:3]}, ids, key), (3, ["q12", "q20"]))
        self.assertEqual(bp.score_answers(None, ids, key), (0, ids))
        self.assertEqual(bp.score_answers(["x"], ids, key), (0, ids))
        extra = dict(good, q30=key["q30"], q99="x")
        self.assertEqual(bp.score_answers(extra, ids, key)[0], 5)     # answers outside the batch are ignored

    def test_load_answers_statuses(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-ans-"))
        self.addCleanup(rmtree, tmp)
        self.assertEqual(bp.load_answers(tmp), (None, "missing"))
        (tmp / "answers.json").write_text("{", encoding="utf-8")
        self.assertEqual(bp.load_answers(tmp), (None, "bad_json"))
        (tmp / "answers.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(bp.load_answers(tmp), (None, "not_object"))
        (tmp / "answers.json").write_text('{"q01": "a"}', encoding="utf-8")
        self.assertEqual(bp.load_answers(tmp), ({"q01": "a"}, "ok"))

    def test_the_full_grader_does_not_accept_a_five_question_subset(self):
        """Why the batch is scored with key.json here: grade.py counts every missing id as wrong. grade.py is not changed."""
        tmp = Path(tempfile.mkdtemp(prefix="bp-grade-"))
        self.addCleanup(rmtree, tmp)
        work = tmp / "w"
        scaffold = sh([sys.executable, "-B", str(LOOKUP / "scaffold.py"), str(work)])
        if scaffold.returncode != 0:
            self.skipTest("local standard library does not match the fixture manifest")
        key = json.loads((LOOKUP / "key.json").read_text(encoding="utf-8"))
        ids = ["q01", "q02", "q03", "q04", "q05"]
        (work / "answers.json").write_text(json.dumps({q: key[q] for q in ids}), encoding="utf-8")
        proc = sh([sys.executable, "-B", str(LOOKUP / "grade.py"), str(work)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("5 of 30 correct", proc.stdout)
        answers, status = bp.load_answers(work)
        self.assertEqual(bp.score_answers(answers, ids, key), (5, []))
        self.assertEqual(bp.grade_module().scaffold.verify_tree(str(work / "stdlib")), [])
        rmtree(work)


# ---------------------------------------------------------------------------------------------
# fake claude and a sandbox copy of the harness
# ---------------------------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys, tempfile

cfg = json.load(open(os.environ["FAKE_CLAUDE_CFG"]))
argv = sys.argv[1:]
sys.stdin.read()
prompt = argv[argv.index("-p") + 1]
cwd = os.path.realpath(os.getcwd())
ids = re.findall(r"^- (q\d\d): ", prompt, re.M)
pinned = os.environ.get("CLAUDE_MITOSIS_PIN_BUILTINS") == "1"
arm = "pinned" if pinned else "inherit"
seen = {k: v for k, v in os.environ.items() if k.startswith(("CLAUDE_MITOSIS_", "AGY_", "CODEX_MITOSIS_", "CLAUDE_CODE_SUBAGENT"))}
with open(cfg["log"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": cwd, "arm": arm, "env": seen, "ids": ids}) + "\n")
idx = len([1 for ln in open(cfg["log"]) if json.loads(ln)["arm"] == arm]) - 1
costs = cfg.get("costs", {}).get(arm, [])
cost = costs[idx] if idx < len(costs) else cfg.get("default_cost", {}).get(arm, 0.3)
if os.path.isdir("stdlib"):
    ref = json.load(open(os.path.join(cfg["lookup_dir"], "reference", "answers.json")))
    out = {q: ref[q] for q in ids}
    for q in ids[:cfg.get("wrong", {}).get(arm, 0)]:
        out[q] = "wrong"
    if not cfg.get("no_answers"):
        json.dump(out, open("answers.json", "w"))
sid = "sess-%s-%d" % (arm, idx)
init = {"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"]}
if not cfg.get("no_plugin"):
    init["plugins"] = [{"name": "mitosis-orchestrator", "path": "/frozen"}]
    init["agents"].append("mitosis-orchestrator:flash-worker")
    init["skills"].append("mitosis-orchestrator:orchestrate")
events = [init]
if not cfg.get("no_explore"):
    block = {"type": "tool_use", "id": "tu1", "name": "Agent", "input": {"subagent_type": "Explore", "prompt": "answer all"}}
    events.append({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m0", "model": "claude-opus-5-5", "content": [block], "usage": {"input_tokens": 5, "output_tokens": 5}}})
    events.append({"type": "assistant", "parent_tool_use_id": "tu1", "message": {"id": "s0", "model": "x", "content": [{"type": "tool_use", "id": "tu9", "name": "Grep", "input": {}}], "usage": {"input_tokens": 1, "output_tokens": 1}}})
for i in range(cfg.get("comp", {}).get(arm, 0)):
    events.append({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "c%d" % i, "model": "claude-opus-5-5",
                   "content": [{"type": "tool_use", "id": "tc%d" % i, "name": "Read", "input": {"file_path": "stdlib/x.py"}}], "usage": {"input_tokens": 1, "output_tokens": 1}}})
sub_cost = cost * 0.25
mu = {"claude-opus-5-5": {"inputTokens": 5, "outputTokens": 900, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 50, "costUSD": cost - (sub_cost if pinned else 0)}}
if pinned:
    mu["claude-haiku-5-5"] = {"inputTokens": 5, "outputTokens": 9000, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 50, "costUSD": sub_cost}
total = cost * cfg.get("total_factor", 1.0)
events.append({"type": "result", "subtype": cfg.get("subtype", "success"), "is_error": False, "session_id": sid, "total_cost_usd": total, "permission_denials": [], "num_turns": 3, "modelUsage": mu})
if not cfg.get("no_explore"):
    model = cfg.get("explore_model", {}).get(arm) or ("claude-haiku-5-5" if pinned else "claude-opus-5-5")
    enc = "".join(c if c.isalnum() else "-" for c in cwd)
    sub = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", enc, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    json.dump({"agentType": "Explore", "description": "d", "toolUseId": "tu1", "finished": True}, open(os.path.join(sub, "agent-a1.meta.json"), "w"))
    with open(os.path.join(sub, "agent-a1.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"id": "x1", "model": model, "content": [], "usage": {}}}) + "\n")
logdir = os.path.join(tempfile.gettempdir(), "mitosis-orchestrator", sid)
os.makedirs(logdir, exist_ok=True)
if not cfg.get("no_log"):
    entry = {"mode": "pre-tool-use", "tool": "Agent", "decisions": [], "result": {}}
    if pinned and not cfg.get("no_pin_decision"):
        entry["decisions"] = [{"rule": "builtin-pin", "tier": "flash_lite"}]
    with open(os.path.join(logdir, "mitosis_orchestrator_debug.log"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
for e in events:
    print(json.dumps(e), flush=True)
'''


class Sandbox:
    """A throw-away copy of the repository layout, a clean git plugin tree and a fake claude on PATH."""

    def __init__(self, case, **cfg):
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-harness-"))
        case.addCleanup(rmtree, self.tmp)
        self.root = self.tmp / "repo"
        ignore = shutil.ignore_patterns("__pycache__", "SEAL.json", "*.pyc")
        shutil.copytree(str(COMPARE), str(self.root / "evals" / "compare"), ignore=ignore)
        for sub in (OVERHEAD, PIN):
            (self.root / "evals" / sub.name).mkdir(parents=True)
            for name in ("PROTOCOL.md", "run.py"):
                shutil.copy(str(sub / name), str(self.root / "evals" / sub.name / name))
        subprocess.run([sys.executable, "-B", str(self.root / "evals" / "compare" / "tasks" / "lookup" / "scaffold.py"), "--make-manifest"], check=True, stdout=subprocess.DEVNULL)
        self.run_py = self.root / "evals" / "builtin-pin" / "run.py"
        self.seal_path = self.root / "evals" / "builtin-pin" / "SEAL.json"
        self.plugin = self.tmp / "plugin"
        (self.plugin / "skills" / "orchestrate").mkdir(parents=True)
        (self.plugin / "skills" / "orchestrate" / "SKILL.md").write_text("---\nname: orchestrate\n---\n\n# Mitosis\n")
        (self.plugin / "evals" / "compare").mkdir(parents=True)
        (self.plugin / "evals" / "compare" / "key.json").write_text("{}\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        for cmd in (git + ["init", "-q"], git + ["add", "-A"], git + ["commit", "-q", "-m", "x"]):
            subprocess.run(cmd, cwd=str(self.plugin), check=True, stdout=subprocess.DEVNULL)
        self.commit = sh(["git", "rev-parse", "HEAD"], cwd=str(self.plugin)).stdout.strip()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        exe = self.bin / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "claude-calls.jsonl"
        self.cfg_path = self.tmp / "fake.json"
        self.config_dir = self.tmp / "claude-config"
        self.config_dir.mkdir()
        self.work = self.tmp / "work"
        self.out = self.tmp / "out"
        self.cfg = {"log": str(self.log), "lookup_dir": str(self.root / "evals" / "compare" / "tasks" / "lookup")}
        self.cfg.update(cfg)
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.extra_env = {}

    def env(self):
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["FAKE_CLAUDE_CFG"] = str(self.cfg_path)
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env.update(self.extra_env)
        return env

    def cli(self, *args, **kw):
        return sh([sys.executable, "-B", str(self.run_py)] + list(args), env=self.env(), **kw)

    def common(self, *extra):
        return ["--plugin-repo", str(self.plugin), "--work-root", str(self.work), "--out", str(self.out)] + list(extra)

    def seal(self, *extra):
        proc = self.cli("seal", "--plugin-repo", str(self.plugin), "--plugin-commit", self.commit, *extra)
        assert proc.returncode == 0, proc.stderr
        return json.loads(self.seal_path.read_text())

    def calls(self):
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def results(self, folder="runs"):
        return [json.loads(p.read_text()) for p in sorted((self.out / folder).glob("*/result.json"))]

    def calibrate(self, *extra):
        return self.cli("calibrate", *self.common(*extra))

    def calibrate_and_run(self, *run_extra):
        proc = self.calibrate()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return self.cli("run", *self.common(*run_extra))


@POSIX_ONLY
class SealTests(unittest.TestCase):
    def test_seal_records_files_commit_batches_and_schedule(self):
        sb = Sandbox(self)
        seal = sb.seal("--seed", "42")
        for name in ("builtin-pin:PROTOCOL.md", "builtin-pin:run.py", "overhead:run.py", "compare:run.py", "compare:PROTOCOL.md", "lookup/prompt.md",
                     "lookup/grade.py", "lookup/scaffold.py", "lookup/stdlib.manifest.json", "lookup/key.json", "lookup/allowed_tools.txt"):
            self.assertIn(name, seal["files"])
            self.assertEqual(len(seal["files"][name]), 64)
        self.assertFalse([f for f in seal["files"] if "__pycache__" in f or f.endswith(".pyc")])
        self.assertEqual(seal["seed"], 42)
        self.assertEqual(len(seal["schedule"]), 36)
        self.assertEqual(seal["batches"], bp.make_batches(42))
        self.assertEqual(seal["plugin_commit"], sb.commit)
        self.assertEqual(len(seal["plugin_tree_sha256"]), 64)
        self.assertEqual((seal["budget_limit_usd"], seal["run_cap_usd"]), (20.0, 1.5))

    def test_the_seal_is_created_only_by_seal_and_never_overwritten(self):
        sb = Sandbox(self)
        self.assertFalse(sb.seal_path.exists())
        sb.seal()
        again = sb.cli("seal", "--plugin-repo", str(sb.plugin), "--plugin-commit", sb.commit)
        self.assertEqual(again.returncode, 2)
        self.assertIn("SEAL.json exists", again.stderr)

    def test_seal_refuses_a_bad_commit(self):
        sb = Sandbox(self)
        proc = sb.cli("seal", "--plugin-repo", str(sb.plugin), "--plugin-commit", "deadbeef")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a commit", proc.stderr)
        self.assertFalse(sb.seal_path.exists())

    def test_calibrate_and_run_refuse_without_seal(self):
        sb = Sandbox(self)
        for cmd in ("calibrate", "run"):
            proc = sb.cli(cmd, *sb.common())
            self.assertEqual(proc.returncode, 2)
            self.assertIn("no SEAL.json", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_a_change_to_any_sealed_file_is_refused(self):
        cases = (
            ("run.py", "evals/builtin-pin/run.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
            ("protocol", "evals/builtin-pin/PROTOCOL.md", lambda p: p.write_text("changed rule\n")),
            ("overhead runner", "evals/overhead/run.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
            ("compare runner", "evals/compare/run.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
            ("prompt", "evals/compare/tasks/lookup/prompt.md", lambda p: p.write_text(p.read_text() + "\nHurry.\n")),
            ("key", "evals/compare/tasks/lookup/key.json", lambda p: p.write_text("{}")),
            ("grader", "evals/compare/tasks/lookup/grade.py", lambda p: p.write_text("import sys\nsys.exit(0)\n")),
            ("new fixture file", "evals/compare/tasks/lookup/extra.txt", lambda p: p.write_text("x")),
        )
        for label, rel, mutate in cases:
            with self.subTest(label):
                sb = Sandbox(self)
                sb.seal()
                mutate(sb.root / rel)
                for cmd in ("calibrate", "run"):
                    proc = sb.cli(cmd, *sb.common())
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn("differ from SEAL.json", proc.stderr)
                self.assertEqual(sb.calls(), [])
                self.assertFalse((sb.out / "runs").exists())

    def test_a_different_seed_a_moved_commit_or_a_forged_schedule_is_refused(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.calibrate("--seed", "5")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sealed seed", proc.stderr)
        seal = json.loads(sb.seal_path.read_text())
        forged = dict(seal, schedule=list(reversed(seal["schedule"])))
        sb.seal_path.write_text(json.dumps(forged))
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("schedule does not follow", proc.stderr)
        sb.seal_path.write_text(json.dumps(dict(seal, plugin_tree_sha256="0" * 64)))
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("differs from the seal", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_a_flag_in_a_settings_file_is_refused_other_mitosis_settings_are_recorded(self):
        sb = Sandbox(self)
        sb.seal()
        (sb.config_dir / "settings.json").write_text(json.dumps({"env": {"CLAUDE_MITOSIS_PIN_BUILTINS": "0"}}))
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("CLAUDE_MITOSIS_PIN_BUILTINS is set in the `env` of a Claude Code settings file", proc.stderr)
        self.assertEqual(sb.calls(), [])
        (sb.config_dir / "settings.json").write_text(json.dumps({"env": {"CLAUDE_MITOSIS_DEBUG": "1", "CLAUDE_MITOSIS_FASTPATH_MODE": "advise"}}))
        self.assertEqual(sb.calibrate().returncode, 0)
        frozen = json.loads((sb.out / "frozen.json").read_text())
        self.assertEqual(frozen["settings_mitosis_env"], {"CLAUDE_MITOSIS_DEBUG": "1", "CLAUDE_MITOSIS_FASTPATH_MODE": "advise"})
        for r in sb.results("calibration"):
            self.assertEqual(r["settings_mitosis_env"], frozen["settings_mitosis_env"])


@POSIX_ONLY
class EndToEndTests(unittest.TestCase):
    def test_calibrate_then_run_records_both_arms_and_reports_supported(self):
        sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5})
        seal = sb.seal()
        sb.extra_env = {"CLAUDE_MITOSIS_PIN_BUILTINS": "1", "CLAUDE_MITOSIS_FASTPATH_MODE": "enforce", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("projected total for 36 scored + 2 warm-up runs: 14.00 USD", proc.stdout)      # 0.7 calibration + 19 * (0.2 + 0.5)
        self.assertEqual(len(sb.calls()), 2)
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = sb.calls()
        self.assertEqual(len(calls), 2 + 2 + 36)
        for c in calls:
            argv = c["argv"]
            self.assertIn("--plugin-dir", argv)
            self.assertNotIn("/worktrees/", c["cwd"])
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            # the caller's mitosis variables and subagent override never arrive; the arms differ only in the flag
            self.assertEqual(c["env"], {"CLAUDE_MITOSIS_DEBUG": "1", "CLAUDE_MITOSIS_PIN_BUILTINS": "1"} if c["arm"] == "pinned" else {"CLAUDE_MITOSIS_DEBUG": "1"})
        by_arm = {a: [c for c in calls if c["arm"] == a] for a in bp.ARMS}
        self.assertEqual([len(by_arm[a]) for a in bp.ARMS], [20, 20])
        strip = lambda c: [x for i, x in enumerate(c["argv"]) if i != 1]   # noqa: E731  (the prompt differs by batch)
        self.assertTrue(all(strip(c) == strip(calls[0]) for c in calls))
        # order: calibration, then warm-up in the sealed order, then the sealed schedule
        scored = calls[4:]
        self.assertEqual([(c["arm"], c["ids"]) for c in scored], [(a, seal["batches"][b - 1]) for _, b, a in seal["schedule"]])
        self.assertEqual([c["arm"] for c in calls[2:4]], seal["warmup_order"])
        results = sb.results()
        self.assertEqual(len(results), 36)
        self.assertTrue(all(r["valid"] and r["correct"] == 5 for r in results), [r["invalid_reasons"] for r in results if not r["valid"]])
        pinned = next(r for r in results if r["arm"] == "pinned")
        self.assertEqual(pinned["flag"], {"env_passed": "1", "settings_value": None, "resolved": "1", "expected": "1", "as_intended": True})
        self.assertEqual(pinned["pin_decisions"], 1)
        self.assertAlmostEqual(pinned["family_cost"]["haiku"], 0.05)
        self.assertEqual(pinned["family_output_tokens"]["haiku"], 9000)
        inherit = next(r for r in results if r["arm"] == "inherit")
        self.assertEqual(inherit["flag"]["resolved"], None)
        self.assertEqual(inherit["flag"]["env_passed"], None)
        self.assertEqual(inherit["family_cost"]["haiku"], 0)
        self.assertEqual(inherit["pin_decisions"], 0)
        self.assertEqual(inherit["compensation_calls"], {"Read": 0, "Grep": 0, "Glob": 0, "Bash": 0})    # the subagent's Grep is not the primary's
        self.assertTrue(len(sb.results("warmup")) == 2 and not any(r["scored"] for r in sb.results("warmup")))
        self.assertTrue(all(Path(sb.out / "runs" / r["id"] / "hook_debug.log").is_file() for r in results))
        self.assertEqual(len((sb.out / "sessions.txt").read_text().splitlines()), 40)
        self.assertFalse(list(sb.work.glob("r*__b*__*")))              # working directories removed
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertEqual(rep.returncode, 0, rep.stderr)
        self.assertIn("outcome: SUPPORTED", rep.stdout)
        self.assertIn("A (pinned correct) = 90, B (inherit correct) = 90", rep.stdout)
        self.assertIn("not a formal non-inferiority test", rep.stdout)
        self.assertIn("stays off in 2.7", rep.stdout)
        data = json.loads((sb.out / "report.json").read_text())
        self.assertEqual(data["decision"]["cheaper_pairs"], 18)
        self.assertAlmostEqual(data["decision"]["ratio"], 0.4)
        self.assertEqual(data["arms"]["pinned"]["valid"], 18)

    def test_quality_loss_and_wrong_models_and_missing_pin_decisions_make_invalid_runs(self):
        sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5}, wrong={"pinned": 1}, explore_model={"inherit": "claude-haiku-5-5"})
        sb.seal()
        self.assertEqual(sb.calibrate().returncode, 5)         # the inherit calibration is on the wrong model: not valid
        sb.cfg["explore_model"] = {}
        sb.cfg_path.write_text(json.dumps(sb.cfg))
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(sb.calls()), 3)                   # the pinned calibration was valid and was not repeated
        self.assertEqual(sb.cli("run", *sb.common()).returncode, 0)
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertIn("A (pinned correct) = 72, B (inherit correct) = 90", rep.stdout)       # one wrong answer in each of the 18 pinned runs
        self.assertIn("outcome: QUALITY LOSS", rep.stdout)

    def test_each_invalid_cause_is_recorded(self):
        causes = (
            ("wrong model", {"explore_model": {"pinned": "claude-opus-5-5"}}, "pinned", "explore_wrong_model:claude-opus-5-5"),
            ("no pin decision", {"no_pin_decision": True}, "pinned", "no_pin_decision_in_hook_log"),
            ("no log", {"no_log": True}, "pinned", "hook_log_missing"),
            ("no explore", {"no_explore": True}, "inherit", "no_explore_call"),
            ("cap", {"subtype": "error_max_budget_usd"}, "inherit", "hit_cap"),
        )
        for label, cfg, arm, reason in causes:
            with self.subTest(label):
                sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5})
                seal = sb.seal()
                self.assertEqual(sb.calibrate().returncode, 0)
                sb.cfg.update(cfg)
                sb.cfg_path.write_text(json.dumps(sb.cfg))
                n = next(i for i in range(1, 37) if {a for _, _, a in seal["schedule"][:i]} == set(bp.ARMS))     # a prefix with both arms
                self.assertEqual(sb.cli("run", *sb.common("--max-runs", str(n))).returncode, 0)
                bad = [r for r in sb.results() if r["arm"] == arm]
                self.assertTrue(bad)
                for r in bad:
                    self.assertFalse(r["valid"])
                    self.assertIn(reason, r["invalid_reasons"])
                capped = [r for r in sb.results() if r["capped"]]
                for r in capped:
                    self.assertEqual(r["cost"], bp.RUN_CAP_USD)      # charged and counted at the cap

    def test_compensation_calls_and_cost_disagreement_are_recorded(self):
        sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5}, comp={"pinned": 2}, total_factor=0.5)
        sb.seal()
        self.assertEqual(sb.calibrate().returncode, 0)
        self.assertEqual(sb.cli("run", *sb.common("--max-runs", "2")).returncode, 0)
        r = next(x for x in sb.results() if x["arm"] == "pinned")
        self.assertEqual(r["compensation_calls"]["Read"], 2)
        self.assertTrue(r["compensation"])
        self.assertTrue(r["cost_disagrees"])
        self.assertAlmostEqual(r["cost"], 0.2)               # the larger of the CLI total (half) and the modelUsage sum
        self.assertAlmostEqual(r["cli_cost"], 0.1)

    def test_run_needs_a_valid_calibration_first(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("run `run.py calibrate` first", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_calibration_projection_over_budget_exits_non_zero_and_run_refuses(self):
        sb = Sandbox(self, default_cost={"pinned": 0.5, "inherit": 0.6})        # 19 * 1.1 = 20.9 + 1.1
        sb.seal()
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertIn("projected total for 36 scored + 2 warm-up runs: 22.00 USD", proc.stdout)
        self.assertIn("exceeds the 20.00 USD budget", proc.stderr)
        self.assertEqual(len(sb.calls()), 2)
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("exceeds", proc.stderr)
        self.assertEqual(len(sb.calls()), 2)
        ok = Sandbox(self, default_cost={"pinned": 0.5, "inherit": 0.5})        # 19 + 1 = exactly 20.00 passes
        ok.seal()
        proc = ok.calibrate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(": 20.00 USD", proc.stdout)

    def test_spent_before_counts_toward_the_projection(self):
        sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5})
        sb.seal()
        self.assertEqual(sb.calibrate("--spent-before", "7").returncode, 5)      # 7 + 0.7 + 13.3 = 21.0

    def test_budget_stop_before_the_first_run(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.calibrate("--spent-before", "18.6")        # 18.6 + 1.5 > 20
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("stopped cleanly", proc.stdout)
        self.assertEqual(sb.calls(), [])
        self.assertEqual(json.loads((sb.out / "STOPPED.json").read_text())["reason"], "budget")

    def test_budget_stop_when_spent_plus_next_cap_would_exceed_twenty(self):
        sb = Sandbox(self, default_cost={"pinned": 0.2, "inherit": 0.5})
        sb.seal()
        self.assertEqual(sb.calibrate("--spent-before", "5").returncode, 0)          # spent 5.7
        sb.cfg["default_cost"] = {"pinned": 1.0, "inherit": 1.0}
        sb.cfg_path.write_text(json.dumps(sb.cfg))
        # the projection was made at the calibrated cost; the real cost is higher, so the budget check must stop the schedule.
        proc = sb.cli("run", *sb.common("--spent-before", "5"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        # 5.7 + 2 warm-ups at 1.0 = 7.7; each scored run adds 1.0; the check before the 12th run sees 18.7 + 1.5 > 20 and stops
        stopped = json.loads((sb.out / "STOPPED.json").read_text())
        self.assertEqual(stopped["reason"], "budget")
        self.assertAlmostEqual(stopped["spent"], 18.7, places=6)
        self.assertEqual(len(sb.results()), 11)
        self.assertGreater(stopped["spent"] + bp.RUN_CAP_USD, bp.BUDGET_LIMIT_USD)
        self.assertLessEqual(stopped["spent"], 19.0)
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertIn("outcome: INCONCLUSIVE", rep.stdout)
        self.assertIn("incomplete", rep.stdout)

    def test_ledger_charges_a_started_run_at_its_cap_and_uses_the_twenty_dollar_limit(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-ledger-"))
        self.addCleanup(rmtree, tmp)
        led = bp.Ledger(tmp, 1.0)
        led.start("a", "scored", 1.5)
        self.assertAlmostEqual(led.spent(), 2.5)
        led.end("a", "scored", 0.7)
        self.assertAlmostEqual(led.spent(), 1.7)
        led.check(18.3)
        with self.assertRaises(bp.BudgetStop):
            led.check(18.31)

    def test_resume_never_reruns_a_finished_cell(self):
        sb = Sandbox(self)
        sb.seal()
        self.assertEqual(sb.calibrate().returncode, 0)
        self.assertEqual(sb.cli("run", *sb.common("--max-runs", "2")).returncode, 0)
        self.assertEqual(len(sb.results()), 2)
        again = sb.cli("run", *sb.common())
        self.assertEqual(again.returncode, 2)
        self.assertIn("no top-up runs", again.stderr)
        before = len(sb.calls())
        self.assertEqual(sb.cli("run", *sb.common("--resume", "--max-runs", "1")).returncode, 0)
        self.assertEqual(len(sb.calls()) - before, 1)
        self.assertEqual(len(sb.results()), 3)

    def test_an_interrupted_cell_stays_charged_and_empty(self):
        sb = Sandbox(self)
        seal = sb.seal()
        self.assertEqual(sb.calibrate().returncode, 0)
        r, b, arm = seal["schedule"][0]
        stuck = sb.out / "runs" / ("r%d__b%d__%s" % (r, b, arm))
        stuck.mkdir(parents=True)
        with open(str(sb.out / "ledger.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "r%d__b%d__%s" % (r, b, arm), "kind": "scored", "event": "start", "charged": 1.5}) + "\n")
        proc = sb.cli("run", *sb.common("--resume", "--max-runs", "2"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("interrupted", proc.stdout)
        self.assertFalse((stuck / "result.json").exists())
        self.assertEqual(len(sb.results()), 2)

    def test_a_run_without_the_plugin_stops_the_runner(self):
        sb = Sandbox(self, no_plugin=True)
        sb.seal()
        proc = sb.calibrate()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("the plugin was not loaded", proc.stderr)
        self.assertEqual(json.loads((sb.out / "STOPPED.json").read_text())["reason"], "arm_state")
        self.assertEqual(len(sb.calls()), 1)

    def test_report_with_no_runs_is_inconclusive(self):
        sb = Sandbox(self)
        sb.out.mkdir()
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertEqual(rep.returncode, 0, rep.stderr)
        self.assertIn("outcome: INCONCLUSIVE", rep.stdout)
        self.assertEqual(sb.cli("report", "--out", str(sb.out / "nope")).returncode, 2)


class NoBytecodeTests(unittest.TestCase):
    def test_nothing_was_written_into_the_sealed_directories(self):
        self.assertEqual(snapshot(COMPARE), COMPARE_BEFORE)
        self.assertEqual(snapshot(OVERHEAD), OVERHEAD_BEFORE)
        self.assertEqual(snapshot(PIN), PIN_BEFORE)
        self.assertFalse(list(PIN.glob("**/*.pyc")))


if __name__ == "__main__":
    unittest.main()
