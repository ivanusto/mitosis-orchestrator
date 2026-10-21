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


if __name__ == "__main__":
    for name, path in (("routing_cases", CASES), ("routing_holdout", HOLDOUT), ("routing_holdout_v2", HOLDOUT_V2), ("routing_holdout_v3", HOLDOUT_V3)):
        if path.exists():
            data = load(path)
            print(report(name, data, *evaluate(data)), end="\n\n")
    unittest.main()
