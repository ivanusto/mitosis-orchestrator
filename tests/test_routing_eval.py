#!/usr/bin/env python3
"""Routing eval: labelled prompts (tests/routing_cases.jsonl, tests/routing_holdout.jsonl) against the rule layer.

A "flash" task routed to "flash_lite" fails (the scout cannot write), so that
cell of the confusion matrix must stay empty. The other error only costs money
and has a looser bound. The judge is disabled here: this measures the rules.

Run alone for the matrix: python3 tests/test_routing_eval.py -v
"""

import collections
import importlib.util
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(__file__).resolve().parent / "routing_cases.jsonl"
HOLDOUT = Path(__file__).resolve().parent / "routing_holdout.jsonl"
HOLDOUT_V2 = Path(__file__).resolve().parent / "routing_holdout_v2.jsonl"
HOLDOUT_V3 = Path(__file__).resolve().parent / "routing_holdout_v3.jsonl"
HOLDOUT_V4 = Path(__file__).resolve().parent / "routing_holdout_v4.jsonl"
HOLDOUT_V5 = Path(__file__).resolve().parent / "routing_holdout_v5.jsonl"
HOLDOUT_V6 = Path(__file__).resolve().parent / "routing_holdout_v6.jsonl"
HOLDOUT_V7 = Path(__file__).resolve().parent / "routing_holdout_v7.jsonl"
HOLDOUT_V8 = Path(__file__).resolve().parent / "routing_holdout_v8.jsonl"

spec = importlib.util.spec_from_file_location("enforce_orchestrator_eval", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

MIN_ACCURACY = 0.95
MAX_COST_ERRORS = 0.05  # flash_lite tasks sent to flash, as a share of all cases


def load(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def evaluate(cases):
    """Return (confusion Counter keyed (expected, got), list of misroutes)."""
    saved = os.environ.pop("MITOSIS_JUDGE_URL", None)
    try:
        matrix, misses = collections.Counter(), []
        for case in cases:
            spec_ = {"prompt": case["prompt"]}
            if case.get("subagent_type"):
                spec_["subagent_type"] = case["subagent_type"]
            decision = eo.decide_subagent(spec_)
            matrix[(case["expected"], decision.tier)] += 1
            if decision.tier != case["expected"]:
                misses.append((case["id"], case["expected"], decision.tier, decision.reason))
        return matrix, misses
    finally:
        if saved is not None:
            os.environ["MITOSIS_JUDGE_URL"] = saved


def report(name, cases, matrix, misses):
    total = len(cases)
    right = matrix[("flash", "flash")] + matrix[("flash_lite", "flash_lite")]
    lines = [
        f"{name}: {right}/{total} correct ({right / total:.1%})",
        "                 got flash  got flash_lite",
        f"  want flash      {matrix[('flash', 'flash')]:>9}  {matrix[('flash', 'flash_lite')]:>14}",
        f"  want flash_lite {matrix[('flash_lite', 'flash')]:>9}  {matrix[('flash_lite', 'flash_lite')]:>14}",
    ]
    lines += [f"  miss {cid}: want {want}, got {got} ({why})" for cid, want, got, why in misses]
    return "\n".join(lines)


class TestRoutingEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load(CASES)
        cls.matrix, cls.misses = evaluate(cls.cases)

    def test_dataset_is_well_formed(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            self.assertIn(c["expected"], ("flash", "flash_lite"), c["id"])
            self.assertIn(c["lang"], ("en", "zh"), c["id"])

    def test_no_worker_task_reaches_a_scout(self):
        failing = [m for m in self.misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_cases", self.cases, self.matrix, self.misses))

    def test_accuracy_and_cost_errors(self):
        total = len(self.cases)
        right = self.matrix[("flash", "flash")] + self.matrix[("flash_lite", "flash_lite")]
        msg = report("routing_cases", self.cases, self.matrix, self.misses)
        self.assertGreaterEqual(right / total, MIN_ACCURACY, msg)
        self.assertLessEqual(self.matrix[("flash_lite", "flash")] / total, MAX_COST_ERRORS, msg)


class TestRoutingHoldout(unittest.TestCase):
    """40 prompts written blind to the rules. Scored 28/40 with 4 worker tasks
    sent to the scout on first contact; the soft-request vocabulary added after
    that run brought it to 32/40 with none, so the set is no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout", cases, matrix, misses))


class TestRoutingHoldoutV2(unittest.TestCase):
    """40 more prompts, written by a subagent that never saw scripts/ or the
    earlier sets. v1.6.0 baseline: 25/40 with 4 worker tasks sent to the scout.
    It is now training data, used for tuning in v1.7.0, so it is no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V2)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v2", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V2)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


class TestRoutingHoldoutV3(unittest.TestCase):
    """Written blind by a subagent; first contact on v1.7.0 rules after commit 0f460d0: 29/40 with 6 dangerous (v1.6.0: 25/40, 9 dangerous). Used for tuning after that, so no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V3)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v3", cases, matrix, misses))


class TestRoutingHoldoutV4(unittest.TestCase):
    """Written blind by a subagent and scored once on v1.7.0 (commit e69aac8) with no tuning afterwards: 33/40, no worker task sent to the scout, 7 lookups sent to the worker. After the question-noun, wider negation and read-only-command rules it scores 40/40, but those rules were tuned on its misses, so it is no longer blind. v1.6.0 scored 25/40 with 8 dangerous."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V4)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v4", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V4)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


class TestRoutingHoldoutV5(unittest.TestCase):
    """v5 first scored once on 2026-10-22 against the 2.0 candidate rules: 37/40, 2 cost, 1 dangerous (v5-en-19, a polite gerund request also misrouted by 1.8.1, which scored 29/40, 10 cost, 1 dangerous). The polite-request rule was then written from new cases only; v5 is no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V5)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v5", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V5)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


class TestRoutingHoldoutV6(unittest.TestCase):
    """Written blind by a subagent and sealed in commit 1c5d524 (sha256 69d3cec3...). Scored once on 2026-10-22 in separate processes: v2.0.0 29/40, 8 cost, 3 dangerous; 2.1.0 32/40, 8 cost, 0 dangerous (4 of the cost errors are lookups typed flash-worker, which always routes to the worker). No tuning on it afterwards."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V6)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v6", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V6)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


class TestRoutingHoldoutV7(unittest.TestCase):
    """Written blind by a subagent and sealed in commit 7dffeb0 (sha256 8b6bf916...). Scored once on 2026-10-22 in separate processes: 2.1.0 31/40, 7 cost, 2 dangerous; 2.2 candidate (c648116) 34/40, 4 cost, 2 dangerous. The two dangerous misses (scout-typed prompts that open with an action or run a writing command) were then fixed with general rules from new hand-written cases (416f754): 36/40, 4 cost, 0 dangerous, no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V7)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v7", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V7)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


class TestRoutingHoldoutV8(unittest.TestCase):
    """Written blind by a subagent and sealed in commit 27914e3 (sha256 f9824228...). Scored once on 2026-10-22 in separate processes: 2.2.0 35/40, 4 cost, 1 dangerous; 2.3 candidate (e237483) 35/40, 4 cost, 1 dangerous, the same five misses. The dangerous miss (a scout-typed comparison of third-party frameworks' current docs, which needs the web) was then fixed with general rules from new hand-written cases (d0b2624): 37/40, 3 cost, 0 dangerous, no longer blind."""

    def test_no_worker_task_reaches_a_scout(self):
        cases = load(HOLDOUT_V8)
        matrix, misses = evaluate(cases)
        failing = [m for m in misses if m[1] == "flash"]
        self.assertEqual(failing, [], report("routing_holdout_v8", cases, matrix, misses))

    def test_dataset_is_well_formed(self):
        cases = load(HOLDOUT_V8)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({c["id"] for c in cases}), 40)
        split = collections.Counter((c["lang"], c["expected"]) for c in cases)
        self.assertEqual(set(split.values()), {10}, split)


if __name__ == "__main__":
    for name, path in (("routing_cases", CASES), ("routing_holdout", HOLDOUT), ("routing_holdout_v2", HOLDOUT_V2), ("routing_holdout_v3", HOLDOUT_V3), ("routing_holdout_v4", HOLDOUT_V4), ("routing_holdout_v5", HOLDOUT_V5), ("routing_holdout_v6", HOLDOUT_V6), ("routing_holdout_v7", HOLDOUT_V7), ("routing_holdout_v8", HOLDOUT_V8)):
        if path.exists():
            data = load(path)
            print(report(name, data, *evaluate(data)), end="\n\n")
    unittest.main()
