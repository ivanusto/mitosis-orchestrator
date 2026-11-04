#!/usr/bin/env python3
"""Tests for evals/overhead: arms, seal, budget, deduplication, decomposition, the rule and the compare seal.

No real CLI is ever started: a fake `claude` executable on PATH plays the part. Nothing here spends money.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPARE = REPO / "evals" / "compare"
OVERHEAD = REPO / "evals" / "overhead"


def snapshot(root):
    """Every file under root (bytecode included) -> sha256."""
    out = {}
    for base, dirs, files in os.walk(str(root)):
        dirs.sort()
        for name in sorted(files):
            full = Path(base) / name
            out[full.relative_to(root).as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


COMPARE_BEFORE = snapshot(COMPARE)  # taken before this module imports anything from evals

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("overhead_run_under_test", str(OVERHEAD / "run.py"))
ov = importlib.util.module_from_spec(_spec)
sys.modules["overhead_run_under_test"] = ov
_spec.loader.exec_module(ov)

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


def sh(args, cwd=None, env=None, timeout=300):
    return subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)


def runs(costs, successes=3):
    return [(i < successes, c) for i, c in enumerate(costs)]


# ---------------------------------------------------------------------------------------------
# pure pieces
# ---------------------------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    def test_arms_build_the_right_commands(self):
        off = ov.build_command("claude", "off", "PROMPT\n", "/frozen", 2.0)
        loaded = ov.build_command("claude", "loaded", "PROMPT\n", "/frozen", 2.0)
        orch = ov.build_command("claude", "orchestrate", "PROMPT\n", "/frozen", 2.0)
        self.assertNotIn("--plugin-dir", off)
        self.assertEqual(off[2], "PROMPT\n")
        self.assertEqual(loaded[2], "PROMPT\n")
        self.assertEqual(loaded[loaded.index("--plugin-dir") + 1], "/frozen")
        self.assertEqual(orch[2], "/mitosis-orchestrator:orchestrate PROMPT\n")
        self.assertEqual(orch[orch.index("--plugin-dir") + 1], "/frozen")
        for cmd in (off, loaded, orch):
            self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
            self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
            self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "2")
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn('"mitosis-orchestrator@mitosis-orchestrator":false', cmd[cmd.index("--settings") + 1])
            self.assertIn("Skill", cmd[cmd.index("--allowedTools") + 1].split(","))
        self.assertEqual(ov.arm_parity_problems(), [])
        with self.assertRaises(ValueError):
            ov.build_command("claude", "on", "P", "/f", 2.0)

    def test_probe_commands_differ_only_in_the_prompt(self):
        loaded = ov.build_command("claude", "loaded", ov.PROBE_PROMPT, "/f", ov.PROBE_CAP_USD)
        self.assertEqual(loaded[2], "Reply OK\n")
        self.assertEqual(loaded[loaded.index("--max-budget-usd") + 1], "0.5")

    def test_schedule_is_a_seeded_shuffle_per_block(self):
        a, b = ov.make_schedule(7), ov.make_schedule(7)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 9)
        for r in (1, 2, 3):
            self.assertEqual(sorted(x for rr, x in a if rr == r), sorted(ov.ARMS))
        self.assertTrue(any(ov.make_schedule(s) != a for s in range(8, 30)))
        self.assertEqual(sorted(ov.probe_order(7)), sorted(ov.ARMS))


class DedupeTests(unittest.TestCase):
    def ev(self, mid, usage, blocks, parent=None, model="claude-opus-5-5"):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": mid, "model": model, "content": blocks, "usage": usage}}

    def test_one_record_per_message_id_with_max_tokens_and_tools_once(self):
        u1 = {"input_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 7, "output_tokens": 3}
        u2 = dict(u1, output_tokens=40)
        events = [
            {"type": "system", "subtype": "init"},
            self.ev("m1", u1, [{"type": "text", "text": "SECRET WORDS"}]),
            self.ev("m1", u2, [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x"}}]),
            self.ev("m1", u2, [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x"}}]),
            self.ev("m2", {"input_tokens": 1, "output_tokens": 2}, [{"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "q"}}]),
            self.ev("s1", {"input_tokens": 9, "output_tokens": 9}, [], parent="t2", model="claude-sonnet-5-5"),
            self.ev("syn", {}, [], model="<synthetic>"),
        ]
        calls = ov.extract_calls(events)
        self.assertEqual([c["message_id"] for c in calls], ["m1", "m2", "s1"])
        self.assertEqual(calls[0]["output"], 40)
        self.assertEqual((calls[0]["input"], calls[0]["cache_read"], calls[0]["cache_write"]), (5, 7, 100))
        self.assertEqual(calls[0]["tools"], ["Read"])
        self.assertEqual(calls[1]["tools"], ["Skill"])
        self.assertTrue(calls[2]["sub"])
        self.assertFalse(calls[0]["sub"])
        self.assertEqual([c["n"] for c in calls], [1, 2, 3])
        self.assertNotIn("SECRET", json.dumps(calls))
        self.assertEqual(set(calls[0]), {"n", "message_id", "model", "sub", "input", "output", "cache_read", "cache_write", "tools"})

    def test_first_prefix_skips_subagent_calls(self):
        calls = [{"sub": True, "input": 9, "cache_read": 9, "cache_write": 9},
                 {"sub": False, "input": 3, "cache_read": 4, "cache_write": 5}, {"sub": False, "input": 99}]
        self.assertEqual(ov.first_prefix(calls), 12)
        self.assertIsNone(ov.first_prefix([{"sub": True}]))


class DecompositionTests(unittest.TestCase):
    PRICES = {"p": (Fraction(4), Fraction(8), Fraction("0.2"), Fraction(20))}

    def pricer(self, model, sub):
        i, cw, cr, out = self.PRICES["p"]
        return (i, cw / 2 if sub else cw, cr, out)

    def test_fixed_numbers(self):
        calls = [{"model": "p", "sub": False, "input": 1000, "output": 500, "cache_read": 20000, "cache_write": 3000},
                 {"model": "p", "sub": True, "input": 0, "output": 0, "cache_read": 0, "cache_write": 1000000}]
        usd = ov.usd_by_class(calls, self.pricer)
        self.assertEqual(usd["input"], Fraction("0.004"))
        self.assertEqual(usd["output"], Fraction("0.01"))
        self.assertEqual(usd["cache_read"], Fraction("0.004"))
        self.assertEqual(usd["cache_write"], Fraction("0.024") + 4)
        self.assertEqual(sum(usd.values()), Fraction("4.042"))

    def test_static_cost(self):
        self.assertEqual(ov.static_cost(2000, 5, Fraction(8), Fraction("0.2")), Fraction("0.0176"))
        self.assertEqual(ov.static_cost(2000, 1, Fraction(8), Fraction("0.2")), Fraction("0.016"))
        self.assertEqual(ov.static_cost(-5, 3, Fraction(8), Fraction("0.2")), 0)
        self.assertEqual(ov.static_cost(1000, 0, Fraction(8), Fraction("0.2")), Fraction("0.008"))

    def test_real_prices_use_one_hour_write_for_primary_and_five_minute_for_subagents(self):
        p_in, p_cw, p_cr, p_out = ov.price_of("claude-opus-5-5", False)
        self.assertEqual(p_cw, 2 * p_in)
        _, s_cw, _, _ = ov.price_of("claude-opus-5-5", True)
        self.assertLess(s_cw, p_cw)
        self.assertEqual(ov.price_of("no-such-model", False), ov.price_of("opus", False))


class ArmStateTests(unittest.TestCase):
    LOADED = {"plugins": [{"name": "mitosis-orchestrator", "path": "/f"}], "agents": ["general-purpose", "mitosis-orchestrator:flash-worker"],
              "skills": ["mitosis-orchestrator:orchestrate"], "slash_commands": ["compact"]}
    OFF = {"plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"]}

    def test_states(self):
        for arm in ("loaded", "orchestrate"):
            state, ok, _ = ov.arm_state(self.LOADED, arm)
            self.assertTrue(ok)
            self.assertEqual(state, {"plugin_loaded": True, "agents_loaded": True, "skill_loaded": True})
            self.assertFalse(ov.arm_state(self.OFF, arm)[1])
            self.assertFalse(ov.arm_state(dict(self.LOADED, agents=["general-purpose"]), arm)[1])
            self.assertFalse(ov.arm_state(dict(self.LOADED, skills=[], slash_commands=[]), arm)[1])
        self.assertTrue(ov.arm_state(self.OFF, "off")[1])
        self.assertFalse(ov.arm_state(self.LOADED, "off")[1])
        self.assertFalse(ov.arm_state(dict(self.OFF, skills=["mitosis-x"]), "off")[1])
        self.assertFalse(ov.arm_state(None, "off")[1])
        self.assertFalse(ov.arm_state(None, "loaded")[1])


class RuleTests(unittest.TestCase):
    OFF = runs([100, 100, 100])

    def decide(self, loaded, orch, static=None, output=None, off=None):
        static = static or {}
        output = dict({"off": Fraction(1)}, **(output or {}))
        return ov.decide({"off": off or self.OFF, "loaded": loaded, "orchestrate": orch}, static, output)

    def test_differs_thresholds(self):
        flat = runs([100, 100, 100])
        self.assertTrue(ov.differs(runs([110, 110, 110]), self.OFF))      # exactly 10.0%
        self.assertFalse(ov.differs(runs([109.9, 109.9, 109.9]), self.OFF))
        self.assertTrue(ov.differs(runs([110.1, 110.1, 110.1]), self.OFF))
        self.assertFalse(ov.differs(runs([110, 110, 110]), runs([100, 100, 110])))  # tie breaks separation
        self.assertFalse(ov.differs(runs([90, 120, 130]), self.OFF))
        self.assertFalse(ov.differs(runs([110, 110]), self.OFF))
        self.assertFalse(ov.differs(runs([90, 90, 90]), flat))
        self.assertTrue(ov.differs(runs([110, 110, 110], 0), self.OFF))   # fewer successes is not part of DIFFERS

    def test_trim_text_at_exact_thresholds(self):
        loaded = runs([110, 110, 110])
        self.assertEqual(self.decide(loaded, self.OFF, {"loaded": Fraction(5)})["outcome"], "TRIM TEXT")         # 10.0% and 50.0%
        self.assertEqual(self.decide(loaded, self.OFF, {"loaded": Fraction("4.99")})["outcome"], "NO TRIM")      # 49.9%
        self.assertEqual(self.decide(runs([109.9] * 3), self.OFF, {"loaded": Fraction(9)})["outcome"], "NO TRIM")  # 9.9%
        self.assertEqual(self.decide(loaded, self.OFF, {"loaded": Fraction(10)})["outcome"], "TRIM TEXT")
        self.assertEqual(self.decide(loaded, self.OFF, {"loaded": None})["outcome"], "NO TRIM")
        self.assertEqual(self.decide(loaded, self.OFF, {})["outcome"], "NO TRIM")

    def test_trim_text_can_come_from_the_orchestrate_arm(self):
        r = self.decide(self.OFF, runs([110, 110, 110]), {"orchestrate": Fraction(5)})
        self.assertEqual(r["outcome"], "TRIM TEXT")
        self.assertTrue(r["arms"]["orchestrate"]["differs"])

    def test_reword_skill_at_exact_thresholds(self):
        orch = runs([110, 110, 110])
        quiet = runs([105, 105, 105])
        self.assertEqual(self.decide(quiet, orch, output={"orchestrate": Fraction(6)})["outcome"], "REWORD SKILL")   # output 5 of 10 = 50.0%
        self.assertEqual(self.decide(quiet, orch, output={"orchestrate": Fraction("5.99")})["outcome"], "NO TRIM")   # 49.9%
        self.assertEqual(self.decide(quiet, runs([109.9] * 3), output={"orchestrate": Fraction(9)})["outcome"], "NO TRIM")
        self.assertEqual(self.decide(quiet, orch, output={"orchestrate": None})["outcome"], "NO TRIM")

    def test_reword_needs_loaded_to_be_quiet_and_trim_takes_precedence(self):
        loud = runs([110, 110, 110])
        self.assertEqual(self.decide(loud, loud, static={"loaded": Fraction(1)}, output={"orchestrate": Fraction(6)})["outcome"], "NO TRIM")
        both = self.decide(runs([105] * 3), loud, static={"orchestrate": Fraction(5)}, output={"orchestrate": Fraction(6)})
        self.assertEqual(both["outcome"], "TRIM TEXT")

    def test_incomplete_data_is_no_trim(self):
        self.assertEqual(self.decide(runs([110, 110]), runs([110, 110]), {"loaded": Fraction(9)})["outcome"], "NO TRIM")
        self.assertEqual(self.decide(runs([110] * 3), runs([110] * 3), {"loaded": Fraction(9)}, off=runs([100, 100]))["outcome"], "NO TRIM")

    def test_labels_use_compares_definitions(self):
        r = self.decide(runs([110, 110, 110]), runs([50, 50, 50]))
        self.assertEqual(r["arms"]["loaded"]["label"], "HARM")
        self.assertEqual(r["arms"]["orchestrate"]["label"], "BENEFIT")
        self.assertEqual(r["arms"]["loaded"]["pct"], 10)

    def test_a_compare_help_function_is_the_same_object(self):
        self.assertIs(ov.cmp.verdict, sys.modules["compare_run_for_overhead"].verdict)
        self.assertIs(ov.Refused, ov.cmp.Refused)


# ---------------------------------------------------------------------------------------------
# fake claude and a sandbox copy of the harness
# ---------------------------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, shutil, sys, time

cfg = json.load(open(os.environ["FAKE_CLAUDE_CFG"]))
argv = sys.argv[1:]
sys.stdin.read()
prompt = argv[argv.index("-p") + 1]
cwd = os.path.realpath(os.getcwd())
probe = prompt.rstrip().endswith("Reply OK")
if "--plugin-dir" not in argv:
    arm = "off"
elif prompt.startswith("/mitosis-orchestrator:orchestrate "):
    arm = "orchestrate"
else:
    arm = "loaded"
with open(cfg["log"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": cwd, "arm": arm, "probe": probe}) + "\n")
idx = len([1 for ln in open(cfg["log"]) if json.loads(ln)["arm"] == arm and not json.loads(ln)["probe"]]) - 1
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])
if not probe and os.path.isdir("stdlib"):
    shutil.copy(os.path.join(cfg["tasks_dir"], "lookup", "reference", "answers.json"), "answers.json")
sid = "sess-%s-%d" % (arm, int(time.time() * 1000000) % 1000000000)
if probe:
    cost = cfg.get("probe_cost", 0.1)
    usage = [cfg.get("probe_usage", {}).get(arm, [10, 0, 1000, 2])]
else:
    cost = cfg.get("costs", {}).get(arm, [0.5] * 9)[idx]
    usage = cfg.get("usage", {}).get(arm, [[10, 1000, 0, 20], [10, 0, 1000, 50]])
state_arm = arm
if cfg.get("wrong_state", {}).get(arm + (":probe" if probe else ":run")):
    state_arm = "off" if arm != "off" else "loaded"
init = {"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"]}
if state_arm != "off":
    init["plugins"] = [{"name": "mitosis-orchestrator", "path": "/frozen"}]
    init["agents"].append("mitosis-orchestrator:flash-worker")
    init["skills"].append("mitosis-orchestrator:orchestrate")
out = [init]
for i, u in enumerate(usage):
    usage_obj = {"input_tokens": u[0], "cache_creation_input_tokens": u[1], "cache_read_input_tokens": u[2], "output_tokens": u[3]}
    blocks = [{"type": "text", "text": "words"}, {"type": "tool_use", "id": "tb%d" % i, "name": "Bash", "input": {"command": "ls"}}]
    for b in blocks:
        out.append({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m%d" % i, "model": "claude-opus-5-5", "content": [b], "usage": usage_obj}})
if cfg.get("skill_call") and arm != "off":
    out.append({"type": "assistant", "message": {"id": "mk", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "tool_use", "id": "tsk", "name": "Skill", "input": {"skill": "mitosis-orchestrator:orchestrate"}}]}})
out.append({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": cost, "permission_denials": [], "num_turns": 2})
if cfg.get("transcript_output"):
    enc = "".join(c if c.isalnum() else "-" for c in cwd)
    proj = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", enc)
    os.makedirs(os.path.join(proj, sid, "subagents"), exist_ok=True)
    with open(os.path.join(proj, sid + ".jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-04T10:00:05.000Z", "message": {"id": "p1", "model": "claude-opus-5", "role": "assistant",
                 "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 0, "output_tokens": cfg["transcript_output"]}}}) + "\n")
for event in out:
    print(json.dumps(event), flush=True)
'''


class Sandbox:
    """A throw-away copy of the repository layout, a clean git plugin tree and a fake claude on PATH."""

    def __init__(self, case, **cfg):
        self.tmp = Path(tempfile.mkdtemp(prefix="ovh-harness-"))
        case.addCleanup(rmtree, self.tmp)
        self.root = self.tmp / "repo"
        shutil.copytree(str(COMPARE), str(self.root / "evals" / "compare"), ignore=shutil.ignore_patterns("__pycache__", "SEAL.json", "*.pyc"))
        (self.root / "evals" / "overhead").mkdir(parents=True)
        for name in ("PROTOCOL.md", "run.py"):
            shutil.copy(str(OVERHEAD / name), str(self.root / "evals" / "overhead" / name))
        subprocess.run([sys.executable, "-B", str(self.root / "evals" / "compare" / "tasks" / "lookup" / "scaffold.py"), "--make-manifest"], check=True)
        (self.root / "scripts").mkdir()
        for name in ov.cmp.SEALED_REPO_FILES:
            shutil.copy(str(REPO / name), str(self.root / name))
        self.run_py = self.root / "evals" / "overhead" / "run.py"
        self.plugin = self.tmp / "plugin"
        (self.plugin / "skills" / "orchestrate").mkdir(parents=True)
        (self.plugin / "skills" / "orchestrate" / "SKILL.md").write_text("---\nname: orchestrate\n---\n\n# Mitosis\n")
        (self.plugin / "evals" / "compare").mkdir(parents=True)
        (self.plugin / "evals" / "compare" / "key.json").write_text("{}\n")
        (self.plugin / ".claude-plugin").mkdir()
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "mitosis-orchestrator"}\n')
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        for cmd in (git + ["init", "-q"], git + ["add", "-A"], git + ["commit", "-q", "-m", "x"]):
            subprocess.run(cmd, cwd=str(self.plugin), check=True, stdout=subprocess.DEVNULL)
        self.commit = sh(["git", "rev-parse", "HEAD"], cwd=str(self.plugin)).stdout.strip()
        # a second commit, so the named commit differs from HEAD
        (self.plugin / "later.txt").write_text("later\n")
        for cmd in (git + ["add", "-A"], git + ["commit", "-q", "-m", "later"]):
            subprocess.run(cmd, cwd=str(self.plugin), check=True, stdout=subprocess.DEVNULL)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        exe = self.bin / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "claude-calls.jsonl"
        self.cfg_path = self.tmp / "fake.json"
        self.config_dir = self.tmp / "claude-config"
        self.work = self.tmp / "work"
        self.out = self.tmp / "out"
        self.cfg = {"log": str(self.log), "tasks_dir": str(self.root / "evals" / "compare" / "tasks")}
        self.cfg.update(cfg)
        self.write_cfg()

    def write_cfg(self):
        self.cfg_path.write_text(json.dumps(self.cfg))

    def env(self):
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["FAKE_CLAUDE_CFG"] = str(self.cfg_path)
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return env

    def cli(self, *args, **kw):
        return sh([sys.executable, "-B", str(self.run_py)] + list(args), env=self.env(), **kw)

    def common(self, *extra):
        return ["--plugin-repo", str(self.plugin), "--work-root", str(self.work), "--out", str(self.out)] + list(extra)

    def seal(self, *extra):
        proc = self.cli("seal", "--plugin-repo", str(self.plugin), "--plugin-commit", self.commit, *extra)
        assert proc.returncode == 0, proc.stderr
        return json.loads((self.root / "evals" / "overhead" / "SEAL.json").read_text())

    def calls(self):
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def result(self, rel):
        return json.loads((self.out / rel / "result.json").read_text())

    def results(self):
        return [json.loads(p.read_text()) for p in sorted((self.out / "runs").glob("*/result.json"))]

    def probe_and_run(self, *run_extra):
        proc = self.cli("probe", *self.common())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return self.cli("run", *self.common(*run_extra))


@POSIX_ONLY
class SealTests(unittest.TestCase):
    def test_seal_records_files_commit_and_schedule(self):
        sb = Sandbox(self)
        seal = sb.seal("--seed", "42")
        for name in ("overhead:PROTOCOL.md", "overhead:run.py", "compare:run.py", "lookup/prompt.md", "lookup/grade.py", "lookup/scaffold.py",
                     "lookup/stdlib.manifest.json", "lookup/key.json", "lookup/allowed_tools.txt", "repo:scripts/mitosis-cost-report.py"):
            self.assertIn(name, seal["files"])
            self.assertEqual(len(seal["files"][name]), 64)
        self.assertFalse([f for f in seal["files"] if "__pycache__" in f or f.endswith(".pyc")])
        self.assertEqual(seal["seed"], 42)
        self.assertEqual(len(seal["schedule"]), 9)
        self.assertEqual(seal["plugin_commit"], sb.commit)
        self.assertNotEqual(seal["plugin_commit"], sh(["git", "rev-parse", "HEAD"], cwd=str(sb.plugin)).stdout.strip())
        self.assertEqual(len(seal["plugin_tree_sha256"]), 64)

    def test_seal_refuses_a_bad_commit(self):
        sb = Sandbox(self)
        proc = sb.cli("seal", "--plugin-repo", str(sb.plugin), "--plugin-commit", "deadbeef")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a commit", proc.stderr)

    def test_probe_and_run_refuse_without_seal(self):
        sb = Sandbox(self)
        for cmd in ("probe", "run"):
            proc = sb.cli(cmd, *sb.common())
            self.assertEqual(proc.returncode, 2)
            self.assertIn("no SEAL.json", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_a_change_to_any_sealed_file_is_refused(self):
        root = "evals/overhead"
        cases = (
            ("protocol", root + "/PROTOCOL.md", lambda p: p.write_text("changed rule\n")),
            ("runner", root + "/run.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
            ("compare runner", "evals/compare/run.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
            ("prompt", "evals/compare/tasks/lookup/prompt.md", lambda p: p.write_text(p.read_text() + "\nHurry.\n")),
            ("grader", "evals/compare/tasks/lookup/grade.py", lambda p: p.write_text("import sys\nsys.exit(0)\n")),
            ("tools", "evals/compare/tasks/lookup/allowed_tools.txt", lambda p: p.unlink()),
            ("new fixture file", "evals/compare/tasks/lookup/extra.txt", lambda p: p.write_text("x")),
            ("cost script", "scripts/mitosis-cost-report.py", lambda p: p.write_text(p.read_text() + "\n# tweak\n")),
        )
        for label, rel, mutate in cases:
            with self.subTest(label):
                sb = Sandbox(self)
                sb.seal()
                mutate(sb.root / rel)
                for cmd in ("probe", "run"):
                    proc = sb.cli(cmd, *sb.common())
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn("differ from SEAL.json", proc.stderr)
                self.assertEqual(sb.calls(), [])
                self.assertFalse((sb.out / "runs").exists())

    def test_a_different_seed_or_moved_commit_is_refused(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("probe", *sb.common("--seed", "5"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sealed seed", proc.stderr)
        seal_path = sb.root / "evals" / "overhead" / "SEAL.json"
        seal = json.loads(seal_path.read_text())
        seal["plugin_tree_sha256"] = "0" * 64
        seal_path.write_text(json.dumps(seal))
        proc = sb.cli("probe", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("differs from the seal", proc.stderr)
        self.assertEqual(sb.calls(), [])


@POSIX_ONLY
class EndToEndTests(unittest.TestCase):
    def test_probe_then_run_records_every_arm_and_builds_the_report(self):
        sb = Sandbox(self, costs={"off": [0.5, 0.5, 0.5], "loaded": [0.6, 0.6, 0.6], "orchestrate": [0.9, 0.9, 0.9]},
                     probe_usage={"off": [10, 0, 5000, 2], "loaded": [10, 0, 8000, 2], "orchestrate": [10, 3000, 6000, 2]},
                     usage={"off": [[10, 1000, 0, 20], [10, 0, 1000, 50]], "orchestrate": [[10, 1000, 0, 20], [10, 0, 1000, 50000]]},
                     skill_call=True)
        sb.seal()
        proc = sb.probe_and_run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = sb.calls()
        self.assertEqual(len(calls), 12)
        self.assertEqual([c["arm"] for c in calls if c["probe"]], json.loads((sb.root / "evals/overhead/SEAL.json").read_text())["probe_order"])
        for c in calls:
            argv = c["argv"]
            self.assertEqual(("--plugin-dir" in argv), c["arm"] != "off")
            self.assertEqual(argv[argv.index("-p") + 1].startswith("/mitosis-orchestrator:orchestrate "), c["arm"] == "orchestrate")
            self.assertNotIn("/worktrees/", c["cwd"])
        results = sb.results()
        self.assertEqual(len(results), 9)
        self.assertTrue(all(r["success"] for r in results), [r["id"] for r in results if not r["success"]])
        self.assertTrue(all(r["arm_state_ok"] for r in results))
        loaded = sb.result("runs/loaded__r1")
        self.assertEqual(loaded["arm_state"], {"plugin_loaded": True, "agents_loaded": True, "skill_loaded": True})
        self.assertEqual(loaded["skill_calls"], ["mitosis-orchestrator:orchestrate"])
        self.assertEqual(sb.result("runs/off__r1")["skill_calls"], [])
        self.assertEqual(sb.result("runs/off__r1")["tool_names"], {"Bash": 2})
        self.assertEqual(loaded["n_api_calls"], 3)
        lines = (sb.out / "runs" / "off__r1" / "calls.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)   # two message ids, each streamed twice
        self.assertNotIn("words", (sb.out / "runs" / "off__r1" / "calls.jsonl").read_text())
        self.assertTrue((sb.out / "sessions.txt").is_file())
        self.assertEqual(len((sb.out / "sessions.txt").read_text().splitlines()), 12)
        self.assertFalse(list(sb.work.glob("off__r*")))          # working directories removed
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertEqual(rep.returncode, 0, rep.stderr)
        self.assertIn("outcome: NO TRIM", rep.stdout)          # orchestrate differs, but static probe share is below 50%
        self.assertIn("minus off", rep.stdout)
        data = json.loads((sb.out / "report.json").read_text())
        self.assertEqual(data["probes"]["orchestrate"]["prefix"], 9010)
        self.assertEqual(data["probes"]["off"]["prefix"], 5010)
        self.assertAlmostEqual(data["stats"]["orchestrate"]["class_usd"]["output"] - data["stats"]["off"]["class_usd"]["output"], (50020 + 1 - 70) * 20 / 1e6, places=6)
        self.assertEqual(data["decision"]["arms"]["orchestrate"]["label"], "HARM")

    def test_cost_is_the_larger_of_cli_and_transcript(self):
        sb = Sandbox(self, costs={"off": [0.2] * 3, "loaded": [0.2] * 3, "orchestrate": [0.2] * 3}, transcript_output=20000)
        sb.seal()
        self.assertEqual(sb.probe_and_run("--max-runs", "1").returncode, 0)
        r = sb.results()[0]
        self.assertAlmostEqual(r["cli_cost"], 0.2)
        self.assertGreater(r["transcript_cost"], 0.3)
        self.assertAlmostEqual(r["cost"], r["transcript_cost"])

    def test_run_needs_the_probe_first(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("run `run.py probe` first", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_resume_never_reruns_a_finished_cell(self):
        sb = Sandbox(self)
        sb.seal()
        self.assertEqual(sb.probe_and_run("--max-runs", "2").returncode, 0)
        self.assertEqual(len(sb.results()), 2)
        again = sb.cli("run", *sb.common())
        self.assertEqual(again.returncode, 2)
        self.assertIn("no top-up runs", again.stderr)
        before = len(sb.calls())
        self.assertEqual(sb.cli("run", *sb.common("--resume")).returncode, 0)
        self.assertEqual(len(sb.calls()) - before, 7)
        self.assertEqual(len(sb.results()), 9)
        self.assertEqual(sb.cli("run", *sb.common("--resume")).returncode, 0)
        self.assertEqual(len(sb.calls()) - before, 7)

    def test_an_interrupted_cell_stays_charged_and_empty_and_the_rest_continue(self):
        sb = Sandbox(self)
        seal = sb.seal()
        self.assertEqual(sb.cli("probe", *sb.common()).returncode, 0)
        r, arm = seal["schedule"][0]
        stuck = sb.out / "runs" / ("%s__r%d" % (arm, r))
        stuck.mkdir(parents=True)
        (sb.out / "ledger.jsonl").write_text(json.dumps({"id": "%s__r%d" % (arm, r), "kind": "scored", "event": "start", "charged": 2.0}) + "\n", encoding="utf-8")
        proc = sb.cli("run", *sb.common("--resume"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("interrupted", proc.stdout)
        self.assertEqual(len(sb.results()), 8)
        self.assertFalse((stuck / "result.json").exists())
        self.assertEqual(len([c for c in sb.calls() if not c["probe"]]), 8)

    def test_budget_stop_before_the_first_probe(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("probe", *sb.common("--spent-before", "9.6"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("stopped cleanly", proc.stdout)
        self.assertEqual(sb.calls(), [])
        self.assertEqual(json.loads((sb.out / "STOPPED.json").read_text())["reason"], "budget")

    def test_budget_stop_when_spent_plus_next_cap_would_exceed_ten(self):
        sb = Sandbox(self, probe_cost=0.1, costs={a: [1.0] * 9 for a in ov.ARMS})
        sb.seal()
        self.assertEqual(sb.cli("probe", *sb.common("--spent-before", "5.5")).returncode, 0)
        proc = sb.cli("run", *sb.common("--spent-before", "5.5"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        # 5.5 + 0.3 probes = 5.8; runs cost 1.0: checks at 5.8, 6.8, 7.8 pass (7.8 + 2 = 9.8); 8.8 + 2 > 10 stops
        self.assertEqual(len(sb.results()), 3)
        self.assertEqual(len([c for c in sb.calls() if not c["probe"]]), 3)
        stopped = json.loads((sb.out / "STOPPED.json").read_text())
        self.assertEqual(stopped["reason"], "budget")
        self.assertAlmostEqual(stopped["spent"], 8.8, places=6)
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertIn("outcome: NO TRIM", rep.stdout)

    def test_ledger_charges_a_started_run_at_its_cap(self):
        sb = Sandbox(self)
        led = ov.Ledger(sb.tmp, 1.0)
        led.start("a", "scored", 2.0)
        self.assertAlmostEqual(led.spent(), 3.0)
        led.end("a", "scored", 0.7)
        self.assertAlmostEqual(led.spent(), 1.7)
        led.check(8.3)
        with self.assertRaises(ov.BudgetStop):
            led.check(8.31)

    def test_a_probe_in_the_wrong_arm_stops_everything(self):
        sb = Sandbox(self, wrong_state={"loaded:probe": True})
        sb.seal()
        proc = sb.cli("probe", *sb.common())
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(json.loads((sb.out / "STOPPED.json").read_text())["reason"], "arm_state")
        n = len(sb.calls())
        self.assertLessEqual(n, 3)
        run = sb.cli("run", *sb.common())
        self.assertEqual(run.returncode, 2)
        self.assertEqual(len(sb.calls()), n)

    def test_a_scored_run_in_the_wrong_arm_stops_the_runner(self):
        sb = Sandbox(self, wrong_state={"orchestrate:run": True})
        seal = sb.seal()
        proc = sb.probe_and_run()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        done = sb.results()
        first_bad = [i for i, (r, a) in enumerate(seal["schedule"]) if a == "orchestrate"][0]
        self.assertEqual(len(done), first_bad + 1)
        bad = [r for r in done if not r["arm_state_ok"]]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["arm"], "orchestrate")
        self.assertFalse(bad[0]["arm_state"]["skill_loaded"])
        self.assertEqual(json.loads((sb.out / "STOPPED.json").read_text())["reason"], "arm_state")

    def test_a_second_runner_on_the_same_output_is_refused(self):
        sb = Sandbox(self, sleep=3)
        sb.seal()
        first = subprocess.Popen([sys.executable, "-B", str(sb.run_py), "probe"] + sb.common(), env=sb.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            for _ in range(100):
                if sb.calls():
                    break
                import time
                time.sleep(0.1)
            second = sb.cli("probe", *sb.common())
            self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
            self.assertIn("another runner", second.stderr)
        finally:
            first.kill()
            first.wait()


# ---------------------------------------------------------------------------------------------
# compare's seal state
# ---------------------------------------------------------------------------------------------

class CompareUntouchedTests(unittest.TestCase):
    def test_nothing_under_evals_compare_changed_or_gained_bytecode(self):
        sb = None
        if os.name != "nt":
            sb = Sandbox(self)
            sb.seal()
            sb.cli("probe", *sb.common())
        after = snapshot(COMPARE)
        self.assertEqual(sorted(after), sorted(COMPARE_BEFORE))
        self.assertEqual(after, COMPARE_BEFORE)
        listing = sh(["git", "status", "--porcelain", "--", "evals/compare"], cwd=str(REPO))
        if listing.returncode == 0:
            self.assertEqual([ln for ln in listing.stdout.splitlines() if ln.strip()], self.dirty_at_import())

    @staticmethod
    def dirty_at_import():
        return DIRTY_AT_IMPORT


_dirty = sh(["git", "status", "--porcelain", "--", "evals/compare"], cwd=str(REPO))
DIRTY_AT_IMPORT = [ln for ln in _dirty.stdout.splitlines() if ln.strip()] if _dirty.returncode == 0 else []


if __name__ == "__main__":
    unittest.main()
