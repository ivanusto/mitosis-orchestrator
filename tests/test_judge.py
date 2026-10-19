#!/usr/bin/env python3
"""Unit tests for scripts/mitosis_judge.py and the judge layer in enforce-orchestrator.py."""

import importlib.util
import io
import json
import os
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mj = _load("mitosis_judge", "mitosis_judge.py")
eo = _load("enforce_orchestrator_judge", "enforce-orchestrator.py")

JUDGE_ENV = ("MITOSIS_JUDGE_URL", "MITOSIS_JUDGE_KEY", "MITOSIS_JUDGE_MODEL", "MITOSIS_JUDGE_TIMEOUT",
             "MITOSIS_JUDGE_MIN_CONFIDENCE", "MITOSIS_JUDGE_THRESHOLD")


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in JUDGE_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)


def fake_opener(body=None, exc=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request, timeout))
        if exc is not None:
            raise exc
        return io.BytesIO(json.dumps(body).encode("utf-8"))
    return opener


class TestRedactAndRequest(unittest.TestCase):
    def test_redact_masks_secrets_addresses_and_homes(self):
        text = ("key sk-ant-abcdef123456 mail a.b@example.com host 203.0.113.7 "
                "path /home/alice/repo and C:\\Users\\Bob\\x sha " + "a" * 40)
        out = mj.redact(text)
        for leaked in ("sk-ant-abcdef123456", "a.b@example.com", "203.0.113.7", "alice", "Bob", "a" * 40):
            self.assertNotIn(leaked, out)
        self.assertIn("~/repo", out)

    def test_build_request_is_jev_choice_and_truncated(self):
        req = mj.build_request("x " * 5000, model="m")
        self.assertEqual(req["model"], "m")
        self.assertEqual(len(req["state"]), mj.MAX_STATE_CHARS)
        q = req["questions"]["tier"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(set(q["criteria"]), {"scout", "worker"})


class TestParseResponse(unittest.TestCase):
    def test_probability_dict(self):
        body = {"answers": {"tier": {"probabilities": {"scout": 0.2, "worker": 0.8}}}}
        self.assertEqual(mj.parse_response(body), ("flash", 0.8))

    def test_probability_list_follows_criteria_order(self):
        body = {"answers": {"tier": {"probabilities": [0.9, 0.1]}}}
        self.assertEqual(mj.parse_response(body), ("flash_lite", 0.9))

    def test_choice_with_confidence(self):
        body = {"answers": {"tier": {"choice": "scout", "confidence": 0.75}}}
        self.assertEqual(mj.parse_response(body), ("flash_lite", 0.75))

    def test_unknown_shapes(self):
        for body in (None, [], {}, {"answers": {}}, {"answers": {"tier": {"choice": "maybe"}}}):
            self.assertIsNone(mj.parse_response(body))


class TestJudgeTier(EnvCase):
    def test_no_url_means_no_call(self):
        seen = []
        self.assertIsNone(mj.judge_tier("Locate x", opener=fake_opener({}, seen=seen)))
        self.assertEqual(seen, [])

    def test_success_sends_key_model_and_timeout(self):
        os.environ.update(MITOSIS_JUDGE_URL="http://judge.test/v1", MITOSIS_JUDGE_KEY="k",
                          MITOSIS_JUDGE_MODEL="jev-x", MITOSIS_JUDGE_TIMEOUT="0.5")
        seen = []
        body = {"answers": {"tier": {"probabilities": {"scout": 0.9, "worker": 0.1}}}}
        self.assertEqual(mj.judge_tier("Locate x", opener=fake_opener(body, seen=seen)), ("flash_lite", 0.9))
        request, timeout = seen[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer k")
        self.assertEqual(json.loads(request.data)["model"], "jev-x")
        self.assertEqual(timeout, 0.5)

    def test_errors_and_unsure_answers_return_none(self):
        os.environ["MITOSIS_JUDGE_URL"] = "http://judge.test/v1"
        self.assertIsNone(mj.judge_tier("x", opener=fake_opener(exc=TimeoutError())))
        self.assertIsNone(mj.judge_tier("x", opener=fake_opener(exc=OSError("HTTP 500"))))
        unsure = {"answers": {"tier": {"probabilities": {"scout": 0.55, "worker": 0.45}}}}
        self.assertIsNone(mj.judge_tier("x", opener=fake_opener(unsure)))


class StubJudge:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, []

    def judge_tier(self, prompt):
        self.calls.append(prompt)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class TestJudgeLayer(EnvCase):
    def use(self, verdict):
        stub = StubJudge(verdict)
        eo._JUDGE_MODULE = stub
        self.addCleanup(setattr, eo, "_JUDGE_MODULE", None)
        os.environ["MITOSIS_JUDGE_URL"] = "http://judge.test/v1"
        return stub

    def test_judge_off_by_default(self):
        stub = StubJudge(("flash", 0.99))
        eo._JUDGE_MODULE = stub
        self.addCleanup(setattr, eo, "_JUDGE_MODULE", None)
        d = eo.decide_subagent({"prompt": "Which file defines UserRepository?"})
        self.assertEqual((d.tier, d.source), ("flash_lite", "rule"))
        self.assertEqual(stub.calls, [])

    def test_low_confidence_rule_goes_to_judge(self):
        stub = self.use(("flash", 0.8))
        d = eo.decide_subagent({"prompt": "Which file defines UserRepository?"})
        self.assertEqual((d.tier, d.source, d.rule, d.effort), ("flash", "judge", "short-prompt", "medium"))
        self.assertEqual(len(stub.calls), 1)

    def test_confident_rule_skips_judge(self):
        stub = self.use(("flash_lite", 0.99))
        d = eo.decide_subagent({"prompt": "Implement the retry logic and run pytest."})
        self.assertEqual((d.tier, d.source), ("flash", "rule"))
        self.assertEqual(stub.calls, [])

    def test_typed_and_workspace_rules_are_never_judged(self):
        stub = self.use(("flash", 0.99))
        d = eo.decide_subagent({"prompt": "Look up parse_token", "subagent_type": "mitosis-orchestrator:flash-lite-scout"})
        self.assertEqual((d.tier, d.source), ("flash_lite", "rule"))
        self.assertEqual(stub.calls, [])

    def test_read_only_declaration_lowers_confidence_and_invites_judge(self):
        prompt = "Read-only review: summarize the fixes listed in CHANGELOG.md."
        os.environ.pop("MITOSIS_JUDGE_URL", None)
        d = eo.decide_subagent({"prompt": prompt})
        self.assertEqual((d.tier, d.rule, d.confidence), ("flash", "mutation", eo.READ_ONLY_CONFIDENCE))
        self.use(("flash_lite", 0.85))
        d = eo.decide_subagent({"prompt": prompt})
        self.assertEqual((d.tier, d.source), ("flash_lite", "judge"))

    def test_judge_can_veto_scout_upgrade(self):
        prompt = "Read-only: list the fixes mentioned in the last three commits."
        scout = "mitosis-orchestrator:flash-lite-scout"
        self.use(("flash_lite", 0.9))
        d = eo.decide_subagent({"prompt": prompt, "subagent_type": scout})
        self.assertEqual((d.tier, d.type_override, d.source), ("flash_lite", None, "judge"))
        self.use(("flash", 0.9))
        d = eo.decide_subagent({"prompt": prompt, "subagent_type": scout})
        self.assertEqual((d.tier, d.type_override), ("flash", "mitosis-orchestrator:flash-worker"))

    def test_unavailable_or_broken_judge_falls_back_to_flash(self):
        for verdict in (None, RuntimeError("boom")):
            with self.subTest(verdict=repr(verdict)):
                self.use(verdict)
                d = eo.decide_subagent({"prompt": "Which file defines UserRepository?"})
                self.assertEqual((d.tier, d.source), ("flash", "default"))

    def test_threshold_env(self):
        stub = self.use(("flash", 0.99))
        os.environ["MITOSIS_JUDGE_THRESHOLD"] = "0.4"
        d = eo.decide_subagent({"prompt": "Which file defines UserRepository?"})
        self.assertEqual(d.source, "rule")
        self.assertEqual(stub.calls, [])


class TestNegationWidening(unittest.TestCase):
    CASES = [
        ("Read-only research. Do NOT git fetch/pull or modify any local repo; use gh api", False),
        ("Never delete or rename files", False),
        ("Don't touch tests or fix the parser", False),
        ("Don't touch tests and fix the parser", True),
        ("Do not stop until you fix the bug", True),
        ("Do not modify config.py; rewrite utils.py instead", True),
    ]

    def test_cases(self):
        for text, mutation in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(bool(eo.MUTATION_RE.search(eo._strip_negated_mutations(text))), mutation)


if __name__ == "__main__":
    unittest.main()
