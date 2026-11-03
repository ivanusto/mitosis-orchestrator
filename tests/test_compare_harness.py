#!/usr/bin/env python3
"""Tests for evals/compare: verdict rule, harness behaviour with a fake `claude`, and the task fixtures.

No real CLI is ever started: a fake `claude` executable on PATH plays the part. Nothing here spends money.
"""

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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPARE = REPO / "evals" / "compare"
TASKS = COMPARE / "tasks"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cmp_run = load_module("compare_run_under_test", COMPARE / "run.py")
solve_mod = load_module("compare_ops_reference", TASKS / "ops" / "reference" / "solve.py")
lookup_scaffold = load_module("compare_lookup_scaffold", TASKS / "lookup" / "scaffold.py")


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


def scaffold(task, workdir):
    proc = sh([sys.executable, str(TASKS / task / "scaffold.py"), str(workdir)])
    assert proc.returncode == 0, proc.stderr
    return Path(workdir)


def grade(task, workdir):
    return sh([sys.executable, str(TASKS / task / "grade.py"), str(workdir)])


# ---------------------------------------------------------------------------------------------
# verdict rule
# ---------------------------------------------------------------------------------------------

def runs(costs, successes=3):
    """Three (success, cost) pairs; the first `successes` are successes."""
    return [(i < successes, c) for i, c in enumerate(costs)]


class VerdictTable(unittest.TestCase):
    def check(self, on, off, want, note=""):
        label, reason = cmp_run.verdict(on, off)
        self.assertEqual(label, want, "%s: %s (%s)" % (note, label, reason))

    def test_table(self):
        table = [
            ("clear benefit", runs([1, 1.1, 1.2]), runs([2, 2.1, 2.2]), "BENEFIT"),
            ("saving exactly 25% passes", runs([70, 75, 80]), runs([100, 100, 100]), "BENEFIT"),
            ("saving 24.9% does not", runs([70, 75.1, 80]), runs([100, 100, 100]), "INCONCLUSIVE"),
            ("saving 25.1% passes", runs([70, 74.9, 80]), runs([100, 100, 100]), "BENEFIT"),
            ("float costs at exactly 25%", runs([0.3, 0.3, 0.3]), runs([0.4, 0.4, 0.4]), "BENEFIT"),
            ("tie between on max and off min is not separation", runs([1, 2, 3]), runs([3, 4, 5]), "INCONCLUSIVE"),
            ("one off-arm run cheaper than an on-arm run", runs([1, 2, 3.5]), runs([3, 4, 5]), "INCONCLUSIVE"),
            ("fewer on successes is HARM even when cheap", runs([1, 1, 1], 2), runs([2, 2, 2]), "HARM"),
            ("fewer on successes, 1 vs 3", runs([1, 1, 1], 1), runs([9, 9, 9]), "HARM"),
            ("more on successes but not separated", runs([3, 3, 3]), runs([3, 3, 3], 2), "INCONCLUSIVE"),
            ("more on successes and separated", runs([1, 1, 1]), runs([3, 3, 3], 2), "BENEFIT"),
            ("equal successes at zero, cheaper and separated", runs([1, 1, 1], 0), runs([3, 3, 3], 0), "BENEFIT"),
            ("HARM by cost, 11%", runs([111, 111, 112]), runs([100, 100, 100]), "HARM"),
            ("HARM by cost exactly 10%", runs([110, 110, 120]), runs([100, 100, 100]), "HARM"),
            ("on 9.9% dearer is not HARM", runs([105, 109.9, 120]), runs([100, 100, 100]), "INCONCLUSIVE"),
            ("dearer but not separated", runs([90, 120, 130]), runs([100, 100, 100]), "INCONCLUSIVE"),
            ("dearer, tie at the boundary", runs([100, 120, 130]), runs([100, 100, 100]), "INCONCLUSIVE"),
            ("identical costs", runs([1, 1, 1]), runs([1, 1, 1]), "INCONCLUSIVE"),
            ("on arm cap failure with equal successes breaks separation", runs([1, 1, 5.0]), runs([2, 2, 2]), "INCONCLUSIVE"),
            ("on arm cap failure counts as a lost success", runs([1, 1, 5.0], 2), runs([2, 2, 2]), "HARM"),
            ("off arm cap failure raises the off cost", runs([1, 1, 1]), runs([3, 3, 5.0], 2), "BENEFIT"),
            ("both arms have a cap failure", runs([1, 1, 5.0], 2), runs([3, 4, 5.0], 2), "INCONCLUSIVE"),
        ]
        for note, on, off, want in table:
            with self.subTest(note):
                self.check(on, off, want, note)

    def test_incomplete_data(self):
        self.check(runs([1, 1]), runs([2, 2, 2]), "INCONCLUSIVE", "two on runs")
        self.check([], [], "INCONCLUSIVE", "no runs")
        self.check(runs([1, 1, 1, 1]), runs([2, 2, 2, 2]), "INCONCLUSIVE", "four runs, rule is for three")

    def test_zero_off_cost_does_not_divide(self):
        self.check(runs([1, 1, 1]), runs([0, 0, 0]), "HARM")

    def test_actions(self):
        acts = cmp_run.overall_actions
        self.assertIn("invests in dispatch rate", " ".join(acts({"lookup": "BENEFIT", "fix": "BENEFIT", "ops": "INCONCLUSIVE"})))
        self.assertIn("invests in dispatch rate", " ".join(acts({"lookup": "BENEFIT", "fix": "BENEFIT", "ops": "BENEFIT"})))
        one = " ".join(acts({"lookup": "BENEFIT", "fix": "INCONCLUSIVE", "ops": "INCONCLUSIVE"}))
        self.assertNotIn("invests", one)
        self.assertIn("only claims what was measured", one)
        both = " ".join(acts({"lookup": "BENEFIT", "fix": "BENEFIT", "ops": "HARM"}))
        self.assertIn("excludes those task types", both)
        self.assertIn("ops", both)
        self.assertNotIn("invests", both)
        self.assertIn("HARM on fix", " ".join(acts({"lookup": "INCONCLUSIVE", "fix": "HARM", "ops": "INCONCLUSIVE"})))


class ScheduleTests(unittest.TestCase):
    def test_schedule_is_fixed_by_seed_and_complete(self):
        a = cmp_run.make_schedule(7)
        self.assertEqual(a, cmp_run.make_schedule(7))
        self.assertNotEqual(a, cmp_run.make_schedule(8))
        self.assertEqual(len(a), 18)
        for r in (1, 2, 3):
            block = a[(r - 1) * 6:r * 6]
            self.assertEqual({c[0] for c in block}, {r})
            self.assertEqual({(c[1], c[2]) for c in block}, {(t, arm) for t in cmp_run.TASKS for arm in cmp_run.ARMS})
        self.assertEqual(sorted(cmp_run.warmup_order(7)), ["off", "on"])

    def test_order_differs_between_blocks(self):
        a = cmp_run.make_schedule(20261103)
        blocks = [tuple((t, arm) for _, t, arm in a[i * 6:(i + 1) * 6]) for i in range(3)]
        self.assertGreater(len(set(blocks)), 1)

    def test_remove_inside_refuses_outside(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a").mkdir()
            (Path(d) / "b").mkdir()
            with self.assertRaises(cmp_run.Refused):
                cmp_run.remove_inside(Path(d) / "a", Path(d) / "b")
            with self.assertRaises(cmp_run.Refused):
                cmp_run.remove_inside(Path(d) / "b", Path(d) / "b")
            cmp_run.remove_inside(Path(d) / "b" / "missing", Path(d) / "b")


# ---------------------------------------------------------------------------------------------
# fake claude and a sandbox copy of the harness
# ---------------------------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, time

cfg = json.load(open(os.environ["FAKE_CLAUDE_CFG"]))
argv = sys.argv[1:]
stdin_data = sys.stdin.read()
arm = "on" if "--plugin-dir" in argv else "off"
cwd = os.path.realpath(os.getcwd())
task = "lookup" if os.path.isdir("stdlib") else ("fix" if os.path.isdir("billing") else "ops")
with open(cfg["log"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": cwd, "arm": arm, "task": task, "stdin": stdin_data}) + "\n")
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])

cost = cfg.get("costs", {}).get(task + ":" + arm, cfg.get("cost", 1.0))
n_agents = cfg.get("agents", 2) if arm == "on" else 0
sub_usd = round(cost * 0.25, 6) if n_agents else 0.0
prim_usd = cost - sub_usd
cli_total = cost if cfg.get("cli_includes_sub", True) else prim_usd
if cfg.get("cli_cost") is not None:
    cli_total = cfg["cli_cost"]
tasks_dir = cfg["tasks_dir"]
if cfg.get("solve", True):
    if task == "lookup":
        shutil.copy(os.path.join(tasks_dir, "lookup", "reference", "answers.json"), "answers.json")
    elif task == "fix":
        shutil.copy(os.path.join(tasks_dir, "fix", "reference", "money.py"), os.path.join("billing", "money.py"))
    else:
        subprocess.check_call([sys.executable, os.path.join(tasks_dir, "ops", "reference", "solve.py"), cwd])

sid = "sess-%s-%d" % (task, int(time.time() * 1000) % 100000000)
enc = "".join(c if (c.isalnum() or c in "_-") else "-" for c in cwd)
proj = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", enc)
os.makedirs(os.path.join(proj, sid, "subagents"), exist_ok=True)
prompt = argv[argv.index("-p") + 1]
expanded = prompt
if arm == "on" and cfg.get("expand", True):
    expanded = "<command-name>/mitosis-orchestrator:orchestrate</command-name> # Mitosis Orchestrator Workflow (`/orchestrate`, v9) ..."
with open(os.path.join(proj, sid + ".jsonl"), "w") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2026-11-03T10:00:00.000Z", "message": {"role": "user", "content": expanded}}) + "\n")
    fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-03T10:00:05.000Z", "message": {"id": "p1", "model": "claude-opus-5", "role": "assistant",
             "usage": {"input_tokens": 0, "output_tokens": int(prim_usd * 40000)}}}) + "\n")
for i in range(n_agents):
    with open(os.path.join(proj, sid, "subagents", "agent-%d.jsonl" % i), "w") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-03T10:00:06.000Z", "isSidechain": True, "message": {"id": "s%d" % i, "model": cfg.get("sub_model", "claude-sonnet-5-5"),
                 "role": "assistant", "usage": {"input_tokens": 0, "output_tokens": int(sub_usd / n_agents * 100000)}}}) + "\n")

init = {"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": "opus", "permissionMode": "acceptEdits",
        "tools": ["Bash", "Read", "Edit"], "plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"]}
if arm == "on":
    init["plugins"] = [{"name": "mitosis-orchestrator", "path": "/frozen"}]
    init["agents"] += ["mitosis-orchestrator:flash-worker"]
    init["skills"] += ["mitosis-orchestrator:orchestrate"]
    init["slash_commands"] += ["mitosis-orchestrator:orchestrate"]
    init.update(cfg.get("on_init_extra", {}))
out = [init]
for hook in cfg.get("hook_events", {}).get(arm, []):
    out.append({"type": "system", "subtype": "hook_started", "hook_name": hook})


def assistant(mid, ctx, blocks, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": mid, "model": "claude-opus-5", "content": blocks,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 10, "output_tokens": 5}}}


out.append(assistant("m1", 1000, [{"type": "text", "text": "start"}]))
out.append(assistant("m2", 4000, [{"type": "tool_use", "id": "tb1", "name": "Bash", "input": {"command": "ls"}}]))
agent_blocks = [{"type": "tool_use", "id": "ta%d" % i, "name": "Agent", "input": {"subagent_type": "mitosis-orchestrator:flash-worker"}} for i in range(n_agents)]
out.append(assistant("m3", 2500, agent_blocks or [{"type": "text", "text": "done"}]))
if n_agents:
    out.append(assistant("side1", 99999, [{"type": "tool_use", "id": "ts1", "name": "Read", "input": {}}], parent="ta0"))
subtype = cfg.get("subtype", "success")
denials = cfg.get("denials", {}).get(arm, [])


def result(total):
    return {"type": "result", "subtype": subtype, "is_error": subtype != "success", "session_id": sid, "total_cost_usd": total,
            "permission_denials": denials, "num_turns": 3, "duration_ms": 10}


if cfg.get("multi_result"):
    out.append(result(0.3))
out.append(result(cli_total))
for event in out:
    print(json.dumps(event), flush=True)
sys.exit(0 if subtype == "success" else 1)
'''


class Sandbox:
    """A throw-away copy of the harness, a clean git plugin tree and a fake claude on PATH."""

    def __init__(self, case, **cfg):
        self.tmp = Path(tempfile.mkdtemp(prefix="cmp-harness-"))
        case.addCleanup(rmtree, self.tmp)
        self.root = self.tmp / "repo"
        shutil.copytree(str(COMPARE), str(self.root / "evals" / "compare"), ignore=shutil.ignore_patterns("__pycache__", "SEAL.json", "*.pyc"))
        (self.root / "scripts").mkdir()
        for name in cmp_run.SEALED_REPO_FILES:
            shutil.copy(str(REPO / name), str(self.root / name))
        self.run_py = self.root / "evals" / "compare" / "run.py"
        self.plugin = self.tmp / "plugin"
        (self.plugin / "claude-agents").mkdir(parents=True)
        (self.plugin / "skills" / "orchestrate").mkdir(parents=True)
        (self.plugin / "claude-agents" / "flash-worker.md").write_text("---\nname: flash-worker\nmodel: sonnet\n---\n")
        (self.plugin / "claude-agents" / "flash-lite-scout.md").write_text("---\nname: flash-lite-scout\nmodel: haiku\n---\n")
        (self.plugin / "skills" / "orchestrate" / "SKILL.md").write_text("---\nname: orchestrate\n---\n\n# Mitosis Orchestrator Workflow (`/orchestrate`, v9)\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        for cmd in (git + ["init", "-q"], git + ["add", "-A"], git + ["commit", "-q", "-m", "x"]):
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
        return sh([sys.executable, str(self.run_py)] + list(args), env=self.env(), **kw)

    def common(self, *extra):
        return ["--plugin-dir", str(self.plugin), "--work-root", str(self.work), "--out", str(self.out)] + list(extra)

    def seal(self, *extra):
        proc = self.cli("seal", *extra)
        assert proc.returncode == 0, proc.stderr
        return json.loads((self.root / "evals" / "compare" / "SEAL.json").read_text())

    def calls(self):
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def result(self, cell):
        return json.loads((self.out / "runs" / cell / "result.json").read_text())

    def results(self):
        return [json.loads(p.read_text()) for p in sorted((self.out / "runs").glob("*/result.json"))]


# ---------------------------------------------------------------------------------------------
# seal and refusals
# ---------------------------------------------------------------------------------------------

class SealTests(unittest.TestCase):
    def test_seal_records_every_fixture_the_protocol_and_the_seed(self):
        sb = Sandbox(self)
        seal = sb.seal("--seed", "42", "--plugin-dir", str(sb.plugin))
        files = seal["files"]
        for name in ("PROTOCOL.md", "run.py", "tasks/lookup/prompt.md", "tasks/fix/prompt.md", "tasks/ops/prompt.md",
                     "tasks/lookup/grade.py", "tasks/fix/grade.py", "tasks/ops/grade.py", "tasks/lookup/scaffold.py",
                     "tasks/lookup/stdlib.manifest.json", "tasks/lookup/key.json", "tasks/fix/hidden/test_hidden.py",
                     "tasks/ops/allowed_tools.txt", "tasks/fix/template/billing/money.py", "repo:scripts/mitosis-cost-report.py"):
            self.assertIn(name, files)
            self.assertEqual(len(files[name]), 64)
        self.assertEqual(seal["seed"], 42)
        self.assertEqual(len(seal["schedule"]), 18)
        self.assertEqual(len(seal["plugin_head"]), 40)
        self.assertFalse([f for f in files if "__pycache__" in f or f.endswith(".pyc")])

    def test_run_refuses_without_seal(self):
        sb = Sandbox(self)
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no SEAL.json", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_run_refuses_changed_prompt_new_file_and_removed_file(self):
        for label, mutate in (
            ("prompt", lambda c: (c / "tasks" / "fix" / "prompt.md").write_text((c / "tasks" / "fix" / "prompt.md").read_text() + "\nAlso hurry.\n")),
            ("protocol", lambda c: (c / "PROTOCOL.md").write_text("changed rule\n")),
            ("grader", lambda c: (c / "tasks" / "ops" / "grade.py").write_text("import sys\nsys.exit(0)\n")),
            ("new", lambda c: (c / "tasks" / "ops" / "extra.txt").write_text("x")),
            ("removed", lambda c: (c / "tasks" / "ops" / "allowed_tools.txt").unlink()),
            ("runner", lambda c: (c / "run.py").write_text((c / "run.py").read_text() + "\n# tweak\n")),
        ):
            with self.subTest(label):
                sb = Sandbox(self)
                sb.seal()
                mutate(sb.root / "evals" / "compare")
                proc = sb.cli("run", *sb.common())
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("differ from SEAL.json", proc.stderr)
                self.assertEqual(sb.calls(), [])
                self.assertFalse((sb.out / "runs").exists())

    def test_run_refuses_a_different_seed_and_a_dirty_or_moved_plugin_tree(self):
        sb = Sandbox(self)
        sb.seal("--plugin-dir", str(sb.plugin))
        proc = sb.cli("run", *sb.common("--seed", "5"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sealed seed", proc.stderr)
        (sb.plugin / "stray.txt").write_text("dirty")
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("uncommitted", proc.stderr)
        (sb.plugin / "stray.txt").unlink()
        (sb.plugin / "claude-agents" / "flash-worker.md").write_text("---\nmodel: haiku\n---\n")
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("uncommitted", proc.stderr)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qam", "move"], cwd=str(sb.plugin), check=True)
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("seal records", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_run_refuses_a_plugin_tree_that_is_not_a_checkout(self):
        sb = Sandbox(self)
        sb.seal()
        plain = sb.tmp / "plain"
        plain.mkdir()
        proc = sb.cli("run", "--plugin-dir", str(plain), "--work-root", str(sb.work), "--out", str(sb.out))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a git checkout", proc.stderr)

    def test_pinned_stdlib_mismatch_is_refused_before_any_run(self):
        sb = Sandbox(self)
        manifest = sb.root / "evals" / "compare" / "tasks" / "lookup" / "stdlib.manifest.json"
        data = json.loads(manifest.read_text())
        first = sorted(data["files"])[0]
        data["files"][first] = "0" * 64
        manifest.write_text(json.dumps(data))
        sb.seal()
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("scaffold for lookup failed", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_lookup_scaffold_removes_its_directory_on_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d) / "lookup"
            shutil.copytree(str(TASKS / "lookup"), str(tdir), ignore=shutil.ignore_patterns("__pycache__"))
            manifest = tdir / "stdlib.manifest.json"
            data = json.loads(manifest.read_text())
            data["files"]["email/message.py"] = "f" * 64
            data["files"]["email/not_a_real_file.py"] = "a" * 64
            manifest.write_text(json.dumps(data))
            work = Path(d) / "w"
            proc = sh([sys.executable, str(tdir / "scaffold.py"), str(work)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("does not match", proc.stderr)
            self.assertFalse(work.exists())


# ---------------------------------------------------------------------------------------------
# run behaviour with the fake claude
# ---------------------------------------------------------------------------------------------

class RunTests(unittest.TestCase):
    def test_full_schedule_commands_ledger_cleanup_and_report(self):
        costs = {"lookup:off": 1.0, "lookup:on": 0.5, "fix:off": 1.0, "fix:on": 2.0, "ops:off": 1.0, "ops:on": 1.0}
        sb = Sandbox(self, costs=costs)
        seal = sb.seal()
        proc = sb.cli("run", *sb.common("--spent-before", "1.25"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = sb.calls()
        self.assertEqual(len(calls), 20)  # 2 warm-ups + 18 scored
        self.assertEqual({c["task"] for c in calls[:2]}, {"fix"})
        self.assertEqual([c["arm"] for c in calls[:2]], seal["warmup_order"])
        scored = calls[2:]
        self.assertEqual([(c["task"], c["arm"]) for c in scored], [(t, a) for _, t, a in map(tuple, seal["schedule"])])
        for c in calls:
            argv = c["argv"]
            self.assertEqual(argv[0], "-p")
            self.assertEqual(c["stdin"], "")
            self.assertNotIn("/worktrees/", c["cwd"])
            self.assertTrue(c["cwd"].startswith(os.path.realpath(str(sb.work))))
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertEqual(argv[argv.index("--effort") + 1], "medium")
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
            self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
            self.assertIn("--verbose", argv)
            self.assertEqual(argv[argv.index("--settings") + 1], '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}')
            tools = argv[argv.index("--allowedTools") + 1].split(",")
            want = [ln.strip() for ln in (TASKS / c["task"] / "allowed_tools.txt").read_text().splitlines() if ln.strip()]
            self.assertEqual(tools, want)
            cap = "1.5" if c in calls[:2] else "5"
            self.assertEqual(argv[argv.index("--max-budget-usd") + 1], cap)
            prompt = argv[1]
            body = (TASKS / c["task"] / "prompt.md").read_text().strip() + "\n"
            if c["arm"] == "on":
                self.assertIn("--plugin-dir", argv)
                self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(sb.plugin))
                self.assertEqual(prompt, "/mitosis-orchestrator:orchestrate " + body)
            else:
                self.assertNotIn("--plugin-dir", argv)
                self.assertEqual(prompt, body)
            for bad in ("bypassPermissions", "--dangerously-skip-permissions", "--bare", "dontAsk"):
                self.assertNotIn(bad, " ".join(argv))
        results = sb.results()
        self.assertEqual(len(results), 18)
        self.assertTrue(all(r["success"] for r in results), [r["id"] for r in results if not r["success"]])
        r = sb.result(next(iter(sorted(p.name for p in (sb.out / "runs").iterdir()))))
        for key in ("success", "cost", "cli_cost", "transcript_cost", "transcript_primary_cost", "transcript_subagent_cost", "peak_context_tokens",
                    "tool_calls", "agent_calls", "subagent_models", "wall_s", "permission_denials", "session_id", "exit_code"):
            self.assertIn(key, r)
        self.assertEqual(r["peak_context_tokens"], 4000)  # largest primary message; the sidechain's 99999 is ignored
        self.assertEqual(r["tool_calls"], 1 + 2 * (r["arm"] == "on") + (r["arm"] == "on"))  # Bash, 2 Agent, 1 sidechain Read
        on = sb.result("fix__on__r1")
        self.assertEqual(on["agent_calls"], 2)
        self.assertEqual(on["subagent_models"], ["claude-sonnet-5-5"])
        self.assertAlmostEqual(on["transcript_cost"], 2.0, places=2)
        self.assertAlmostEqual(on["transcript_subagent_cost"], 0.5, places=2)
        self.assertAlmostEqual(on["cost"], 2.0, places=2)
        # cleanup and bookkeeping
        self.assertEqual(list(sb.work.iterdir()), [])
        self.assertEqual(len((sb.out / "sessions.txt").read_text().splitlines()), 20)
        ledger = [json.loads(line) for line in (sb.out / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(ledger), 40)
        self.assertEqual(len(list((sb.out / "warmup").glob("*/result.json"))), 2)
        for sub in (sb.out / "runs").iterdir():
            self.assertTrue((sub / "stream.jsonl").is_file())
            self.assertTrue((sub / "result_events.json").is_file())
        # report
        rep = sb.cli("report", "--out", str(sb.out))
        self.assertEqual(rep.returncode, 0, rep.stderr)
        text = rep.stdout
        self.assertIn("== lookup ==", text)
        self.assertRegex(text, r"verdict: BENEFIT")
        self.assertRegex(text, r"verdict: HARM")
        self.assertRegex(text, r"verdict: INCONCLUSIVE")
        self.assertIn("HARM on fix", text)
        self.assertIn("does not decide any verdict", text)
        self.assertIn("median peak context 4000", text)
        # the same cells again: no top-up
        n = len(sb.calls())
        again = sb.cli("run", *sb.common())
        self.assertEqual(again.returncode, 2)
        self.assertIn("no top-up", again.stderr)
        resumed = sb.cli("run", *sb.common("--resume"))
        self.assertEqual(resumed.returncode, 0)
        self.assertEqual(len(sb.calls()), n)

    def test_keep_leaves_work_dirs_and_resume_never_reruns_finished_cells(self):
        sb = Sandbox(self)
        sb.seal()
        first = sb.cli("run", *sb.common("--keep", "--max-runs", "2"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(sb.calls()), 4)
        self.assertEqual(len(list(sb.out.glob("runs/*/result.json"))), 2)
        self.assertEqual(len([p for p in sb.work.iterdir()]), 4)
        blocked = sb.cli("run", *sb.common("--max-runs", "2"))
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(len(sb.calls()), 4)
        more = sb.cli("run", *sb.common("--resume", "--max-runs", "3"))
        self.assertEqual(more.returncode, 0, more.stderr)
        self.assertEqual(len(sb.calls()), 7)  # no new warm-ups, three new scored runs
        done = {p.parent.name for p in sb.out.glob("runs/*/result.json")}
        self.assertEqual(len(done), 5)

    def test_interrupted_cell_is_not_redone(self):
        sb = Sandbox(self)
        seal = sb.seal()
        r, t, a = seal["schedule"][0]
        (sb.out / "runs" / ("%s__%s__r%d" % (t, a, r))).mkdir(parents=True)
        proc = sb.cli("run", *sb.common("--resume"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("interrupted", proc.stderr)
        self.assertEqual(sb.calls(), [])

    def test_budget_stops_cleanly_before_the_run_that_could_exceed_60(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common("--spent-before", "56"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertEqual(len(sb.calls()), 2)  # warm-ups fit (57.5 and 58.5), the first scored run (58 + 5 > 60) does not
        self.assertEqual(sb.results(), [])
        stopped = json.loads((sb.out / "STOPPED.json").read_text())
        self.assertEqual(stopped["reason"], "budget")
        self.assertAlmostEqual(stopped["spent"], 58.0, places=1)

    def test_budget_boundary_spent_plus_5_equal_to_60_still_runs(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common("--spent-before", "53"))  # after two 1.0 warm-ups spent is 55, and 55 + 5 is not above 60
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(len(sb.results()), 1)
        self.assertEqual(len(sb.calls()), 3)

    def test_run_charges_the_larger_of_cli_and_transcript_cost(self):
        sb = Sandbox(self, cost=2.0, cli_includes_sub=False)  # on arm: CLI says 1.5, transcripts say 2.0
        sb.seal()
        proc = sb.cli("run", *sb.common("--max-runs", "6"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for r in sb.results():
            if r["arm"] == "on":
                self.assertAlmostEqual(r["cli_cost"], 1.5, places=3)
                self.assertAlmostEqual(r["transcript_cost"], 2.0, places=2)
                self.assertAlmostEqual(r["charged"], 2.0, places=2)
            else:
                self.assertAlmostEqual(r["charged"], 2.0, places=2)
        spent = sum(json.loads(ln)["charged"] for ln in (sb.out / "ledger.jsonl").read_text().splitlines() if '"end"' in ln)
        self.assertGreater(spent, 6 * 1.99)

    def test_last_of_several_result_events_is_used_and_all_are_kept(self):
        sb = Sandbox(self, multi_result=True, cost=0.9)
        sb.seal()
        proc = sb.cli("run", *sb.common("--max-runs", "1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = sb.results()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["n_result_events"], 2)
        self.assertAlmostEqual(res[0]["cli_cost"], 0.9)
        kept = json.loads((sb.out / "runs" / res[0]["id"] / "result_events.json").read_text())
        self.assertEqual([round(e["total_cost_usd"], 2) for e in kept], [0.3, 0.9])
        info = cmp_run.analyse_stream([{"type": "result", "total_cost_usd": 0.3, "session_id": "a"}, {"type": "result", "total_cost_usd": 0.9, "session_id": "b"}])
        self.assertEqual((info["cli_cost"], info["session_id"], len(info["results"])), (0.9, "b", 2))

    def test_budget_cap_hit_is_a_failure_at_the_cap_cost_even_if_the_grader_passes(self):
        sb = Sandbox(self, subtype="error_max_budget_usd", cli_cost=5.02, cost=5.02)
        sb.seal()
        proc = sb.cli("run", *sb.common("--max-runs", "1"))
        self.assertIn(proc.returncode, (0, 4), proc.stderr)
        res = sb.results()[0]
        self.assertFalse(res["success"])
        self.assertEqual(res["grade_exit"], 0)
        self.assertTrue(res["capped"])
        self.assertEqual(res["cost"], 5.0)
        self.assertGreaterEqual(res["charged"], 5.0)

    def test_timeout_is_a_failure_at_the_cap_cost(self):
        sb = Sandbox(self, sleep=60)
        sb.seal()
        proc = sb.cli("run", *sb.common("--max-runs", "1", "--timeout", "2"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = sb.results()[0]
        self.assertTrue(res["timed_out"])
        self.assertFalse(res["success"])
        self.assertEqual(res["cost"], 5.0)
        self.assertLess(res["wall_s"], 30)

    def test_failed_grade_counts_at_measured_cost(self):
        sb = Sandbox(self, solve=False, cost=0.7)
        sb.seal()
        proc = sb.cli("run", *sb.common("--max-runs", "1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = sb.results()[0]
        self.assertFalse(res["success"])
        self.assertNotEqual(res["grade_exit"], 0)
        self.assertAlmostEqual(res["cost"], 0.7, places=2)

    def test_missing_claude_is_refused(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common("--claude", "no-such-claude-binary"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not found", proc.stderr)

    def test_worktrees_path_is_refused(self):
        sb = Sandbox(self)
        sb.seal()
        bad = sb.tmp / "worktrees" / "w"
        proc = sb.cli("run", "--plugin-dir", str(sb.plugin), "--work-root", str(bad), "--out", str(sb.out))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("/worktrees/", proc.stderr)


# ---------------------------------------------------------------------------------------------
# pilot
# ---------------------------------------------------------------------------------------------

class PilotTests(unittest.TestCase):
    def status(self, text, fragment):
        lines = [ln for ln in text.splitlines() if fragment in ln]
        self.assertEqual(len(lines), 1, "%s in %s" % (fragment, text))
        return lines[0].split()[0]

    def test_pilot_runs_two_fix_runs_and_prints_checks(self):
        sb = Sandbox(self)
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = sb.calls()
        self.assertEqual([(c["task"], c["arm"]) for c in calls], [("fix", "off"), ("fix", "on")])
        for c in calls:
            self.assertEqual(c["argv"][c["argv"].index("--max-budget-usd") + 1], "1.5")
        out = proc.stdout
        self.assertEqual(self.status(out, "init events differ only in mitosis items"), "PASS")
        self.assertEqual(self.status(out, "off arm has no mitosis hooks firing"), "UNKNOWN")
        self.assertEqual(self.status(out, "on arm skill expanded under -p"), "PASS")
        self.assertEqual(self.status(out, "off arm: Bash allow rules work"), "PASS")
        self.assertEqual(self.status(out, "on arm: Bash allow rules work"), "PASS")
        self.assertEqual(self.status(out, "off arm: no permission denials"), "PASS")
        self.assertEqual(self.status(out, "on arm: no permission denials"), "PASS")
        self.assertEqual(self.status(out, "on arm: subagents ran with pinned models"), "PASS")
        self.assertEqual(self.status(out, "CLI total includes subagent cost"), "PASS")
        self.assertIn("yes:", [ln for ln in out.splitlines() if "CLI total includes" in ln][0])
        self.assertTrue((sb.out / "pilot" / "off" / "result.json").is_file())
        self.assertFalse((sb.out / "runs").exists())
        again = sb.cli("pilot", *sb.common())
        self.assertEqual(again.returncode, 2)
        self.assertEqual(len(sb.calls()), 2)

    def test_pilot_detects_a_cli_total_that_leaves_out_subagents(self):
        sb = Sandbox(self, cli_includes_sub=False)
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        line = [ln for ln in proc.stdout.splitlines() if "CLI total includes" in ln][0]
        self.assertTrue(line.startswith("PASS"))
        self.assertIn("no:", line)

    def test_pilot_stops_after_the_off_arm_on_a_fail(self):
        sb = Sandbox(self, denials={"off": [{"tool_name": "Bash", "tool_use_id": "x", "tool_input": {}}]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(sb.calls()), 1)
        self.assertEqual(self.status(proc.stdout, "off arm: Bash allow rules work"), "FAIL")
        self.assertEqual(self.status(proc.stdout, "off arm: no permission denials"), "FAIL")
        self.assertIn("pilot stopped", proc.stdout)

    def test_pilot_fails_on_mitosis_hooks_in_the_off_arm(self):
        sb = Sandbox(self, hook_events={"off": ["python3 enforce-orchestrator.py pre-tool-use"]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(sb.calls()), 1)
        self.assertEqual(self.status(proc.stdout, "off arm has no mitosis hooks firing"), "FAIL")

    def test_pilot_fails_when_init_differs_outside_mitosis_items(self):
        sb = Sandbox(self, on_init_extra={"tools": ["Bash", "Read", "Edit", "Extra"]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        line = [ln for ln in proc.stdout.splitlines() if "init events differ" in ln][0]
        self.assertTrue(line.startswith("FAIL"))
        self.assertIn("tools", line)

    def test_pilot_fails_without_subagents_or_with_unpinned_models_or_unexpanded_skill(self):
        for label, cfg, fragment in (
            ("no agents", {"agents": 0}, "on arm: subagents ran with pinned models"),
            ("wrong model", {"sub_model": "claude-opus-5"}, "on arm: subagents ran with pinned models"),
            ("not expanded", {"expand": False}, "on arm skill expanded under -p"),
        ):
            with self.subTest(label):
                sb = Sandbox(self, **cfg)
                proc = sb.cli("pilot", *sb.common())
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertEqual(self.status(proc.stdout, fragment), "FAIL")
                self.assertEqual(len(sb.calls()), 2)


# ---------------------------------------------------------------------------------------------
# prompts, tools, docs
# ---------------------------------------------------------------------------------------------

class TextRules(unittest.TestCase):
    FORBIDDEN = re.compile(r"delegate|subagent|agent|cost|orchestrat", re.I)

    def test_prompts_are_neutral(self):
        for task in ("lookup", "fix", "ops"):
            text = (TASKS / task / "prompt.md").read_text()
            self.assertIsNone(self.FORBIDDEN.search(text), "%s: %s" % (task, self.FORBIDDEN.search(text)))
            self.assertNotIn("experiment", text.lower())
            self.assertFalse(text.lstrip().startswith(("-", "/")))

    def test_allowed_tools_lists(self):
        always = {"Agent", "Read", "Grep", "Glob", "Edit", "Write", "Skill", "SendMessage"}
        bash = {
            "lookup": {"ls", "cat", "grep", "sed", "find", "wc", "head", "tail"},
            "fix": {"python3", "git", "ls", "cat", "grep", "sed", "find", "wc", "head", "tail"},
            "ops": {"python3", "ls", "cat", "grep", "sed", "find", "wc", "head", "tail", "mkdir", "cp", "mv", "sha256sum", "date"},
        }
        for task, cmds in bash.items():
            rules = [ln.strip() for ln in (TASKS / task / "allowed_tools.txt").read_text().splitlines() if ln.strip()]
            self.assertTrue(always <= set(rules), task)
            scoped = {re.match(r"^Bash\((\w+):\*\)$", r).group(1) for r in rules if r.startswith("Bash")}
            self.assertEqual(scoped, cmds, task)
            self.assertNotIn("Bash", rules)

    def test_docs_and_runner_text(self):
        banned_flags = ("bypassPermissions", "--dangerously-skip-permissions", "--bare")
        for path in (COMPARE / "PROTOCOL.md", COMPARE / "run.py", TASKS / "lookup" / "prompt.md", TASKS / "fix" / "prompt.md", TASKS / "ops" / "prompt.md"):
            text = path.read_text()
            self.assertNotIn("—", text, path.name)
            for flag in banned_flags:
                self.assertNotIn(flag, text, "%s mentions %s" % (path.name, flag))
        proto = (COMPARE / "PROTOCOL.md").read_text()
        for needle in ("BENEFIT", "HARM", "INCONCLUSIVE", "25%", "10%", "0.05", "explicit `/orchestrate`", "Claude only", "one machine", "small n",
                       "no top-up", "does not decide", "may not change after the seal", "cap cost"):
            self.assertIn(needle.lower(), proto.lower(), needle)

    def test_runner_has_the_four_subcommands(self):
        proc = sh([sys.executable, str(COMPARE / "run.py"), "--help"])
        self.assertEqual(proc.returncode, 0)
        for name in ("seal", "pilot", "run", "report"):
            self.assertIn(name, proc.stdout)


# ---------------------------------------------------------------------------------------------
# fixtures: each grader fails on the untouched scaffold and passes on the reference solution
# ---------------------------------------------------------------------------------------------

class LookupFixture(unittest.TestCase):
    def setUp(self):
        problems = lookup_scaffold.verify_tree(lookup_scaffold.stdlib_root())
        if problems:
            self.skipTest("this machine's Python standard library is not the pinned one: %s" % problems[:2])
        self.tmp = Path(tempfile.mkdtemp(prefix="cmp-lookup-"))
        self.addCleanup(rmtree, self.tmp)

    def test_grader_fails_on_scaffold_and_passes_on_reference(self):
        work = scaffold("lookup", self.tmp / "w")
        self.assertNotEqual(grade("lookup", work).returncode, 0)
        shutil.copy(str(TASKS / "lookup" / "reference" / "answers.json"), str(work / "answers.json"))
        proc = grade("lookup", work)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("8 of 8", proc.stdout)

    def test_all_answers_are_needed_and_expected_values_are_not_printed(self):
        work = scaffold("lookup", self.tmp / "w")
        key = json.loads((TASKS / "lookup" / "key.json").read_text())
        self.assertEqual(len(key), 8)
        for q in sorted(key):
            wrong = dict(key)
            wrong[q] = "wrong" if not isinstance(key[q], (int, float)) else key[q] + 1
            (work / "answers.json").write_text(json.dumps(wrong))
            proc = grade("lookup", work)
            self.assertEqual(proc.returncode, 1, q)
            self.assertIn("%s wrong" % q, proc.stdout)
            for value in key.values():
                if isinstance(value, str):
                    self.assertNotIn(value, proc.stdout)
        partial = dict(key)
        del partial["q3"]
        (work / "answers.json").write_text(json.dumps(partial))
        self.assertEqual(grade("lookup", work).returncode, 1)
        (work / "answers.json").write_text("not json")
        self.assertEqual(grade("lookup", work).returncode, 1)

    def test_key_is_derived_from_the_real_tree_by_script(self):
        proc = sh([sys.executable, str(TASKS / "lookup" / "verify_key.py")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 mismatches", proc.stdout)
        work = scaffold("lookup", self.tmp / "w")
        proc = sh([sys.executable, str(TASKS / "lookup" / "verify_key.py"), str(work / "stdlib")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_verify_key_catches_a_wrong_key(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(str(TASKS / "lookup"), str(Path(d) / "lookup"), ignore=shutil.ignore_patterns("__pycache__"))
            kp = Path(d) / "lookup" / "key.json"
            key = json.loads(kp.read_text())
            key["q1"] = 1
            kp.write_text(json.dumps(key))
            proc = sh([sys.executable, str(Path(d) / "lookup" / "verify_key.py")])
            self.assertEqual(proc.returncode, 1)
            self.assertIn("MISMATCH q1", proc.stdout)

    def test_tree_is_read_only_and_pinned(self):
        work = scaffold("lookup", self.tmp / "w")
        manifest = json.loads((TASKS / "lookup" / "stdlib.manifest.json").read_text())
        self.assertGreater(len(manifest["files"]), 50)
        self.assertEqual(lookup_scaffold.verify_tree(str(work / "stdlib")), [])
        if os.geteuid() != 0:
            with self.assertRaises(OSError):
                (work / "stdlib" / "http" / "new.py").write_text("x")
            with self.assertRaises(OSError):
                (work / "stdlib" / "http" / "client.py").write_text("x")
        self.assertNotIn("/worktrees/", str(work))

    def test_questions_are_not_answerable_from_a_wrong_tree(self):
        work = scaffold("lookup", self.tmp / "w")
        shutil.copy(str(TASKS / "lookup" / "reference" / "answers.json"), str(work / "answers.json"))
        # an edited copy fails the grader even with the right answers
        make = lookup_scaffold.make_writable
        make(str(work))
        (work / "stdlib" / "http" / "client.py").write_text((work / "stdlib" / "http" / "client.py").read_text() + "\n# edited\n")
        proc = grade("lookup", work)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no longer matches", proc.stdout)


class FixFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cmp-fix-"))
        self.addCleanup(rmtree, self.tmp)

    def suite(self, work, tests_dir):
        env = dict(os.environ, PYTHONPATH=str(work), PYTHONDONTWRITEBYTECODE="1")
        return sh([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(tests_dir)], cwd=str(work), env=env)

    def test_package_shape(self):
        mods = sorted(p.name for p in (TASKS / "fix" / "template" / "billing").glob("*.py"))
        self.assertGreaterEqual(len(mods), 10)
        self.assertLessEqual(len(mods), 12)

    def test_visible_and_hidden_fail_before_and_pass_after(self):
        work = scaffold("fix", self.tmp / "w")
        before_vis = self.suite(work, work / "tests")
        before_hid = self.suite(work, TASKS / "fix" / "hidden")
        self.assertNotEqual(before_vis.returncode, 0)
        self.assertNotEqual(before_hid.returncode, 0)
        failing_modules = set(re.findall(r"^(?:FAIL|ERROR): \w+ \((\w+)\.", before_vis.stderr, re.M))
        self.assertGreaterEqual(len(failing_modules), 3, failing_modules)
        self.assertNotEqual(grade("fix", work).returncode, 0)
        shutil.copy(str(TASKS / "fix" / "reference" / "money.py"), str(work / "billing" / "money.py"))
        self.assertEqual(self.suite(work, work / "tests").returncode, 0)
        self.assertEqual(self.suite(work, TASKS / "fix" / "hidden").returncode, 0)
        proc = grade("fix", work)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_reference_changes_only_the_root_cause_file(self):
        template = TASKS / "fix" / "template" / "billing"
        ref = (TASKS / "fix" / "reference" / "money.py").read_text()
        orig = (template / "money.py").read_text()
        diff = [(a, b) for a, b in zip(orig.splitlines(), ref.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertEqual(len(orig.splitlines()), len(ref.splitlines()))

    def test_a_symptom_patch_in_one_module_is_not_enough(self):
        work = scaffold("fix", self.tmp / "w")
        tax = work / "billing" / "tax.py"
        tax.write_text(tax.read_text().replace("return util.scale(subtotal_cents, rates.rate_for(region))",
                                              "return (subtotal_cents * rates.rate_for(region) + 5000) // 10000"))
        vis = self.suite(work, work / "tests")
        proc = grade("fix", work)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("hidden: exit 1", proc.stdout)
        self.assertNotEqual(vis.returncode, 0)  # discounts, invoice proration and report allocation still fail

    def test_editing_the_visible_tests_does_not_help(self):
        work = scaffold("fix", self.tmp / "w")
        for p in (work / "tests").glob("test_*.py"):
            p.write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        proc = grade("fix", work)
        self.assertNotEqual(proc.returncode, 0)

    def test_scaffold_is_a_git_repo_with_a_clean_initial_commit(self):
        work = scaffold("fix", self.tmp / "w")
        self.assertEqual(sh(["git", "status", "--porcelain"], cwd=str(work)).stdout.strip(), "")
        self.assertEqual(sh(["git", "rev-list", "--count", "HEAD"], cwd=str(work)).stdout.strip(), "1")


class OpsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cmp-ops-"))
        self.addCleanup(rmtree, self.tmp)
        self.n = 0

    def fresh(self):
        self.n += 1
        return scaffold("ops", self.tmp / ("w%d" % self.n))

    def test_grader_fails_on_scaffold_and_passes_on_reference(self):
        work = self.fresh()
        self.assertNotEqual(grade("ops", work).returncode, 0)
        proc = sh([sys.executable, str(TASKS / "ops" / "reference" / "solve.py"), str(work)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = grade("ops", work)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_grader_fails_on_every_incomplete_prefix(self):
        steps = solve_mod.STEPS
        for k in range(0, 7):
            with self.subTest(steps_done=k):
                work = self.fresh()
                for step in steps[:k]:
                    step(str(work))
                self.assertEqual(grade("ops", work).returncode, 1)
        for k in (8, 9):
            work = self.fresh()
            for step in steps[:k]:
                step(str(work))
            self.assertEqual(grade("ops", work).returncode, 0, k)

    def test_each_step_depends_on_the_one_before_it(self):
        steps = solve_mod.STEPS
        self.assertEqual(len(steps), 9)
        for idx in range(1, len(steps)):
            with self.subTest(step=steps[idx].__name__, without=steps[idx - 1].__name__):
                work = self.fresh()
                for step in steps[:idx - 1]:
                    step(str(work))
                with self.assertRaises(Exception):
                    steps[idx](str(work))

    def test_import_fails_the_first_time_unless_the_config_was_read(self):
        work = self.fresh()
        for step in (solve_mod.step_render, solve_mod.step_create_db, solve_mod.step_migrate):
            step(str(work))
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            solve_mod.step_import(str(work))
        self.assertIn("settings.site", str(ctx.exception))
        solve_mod.step_set_site(str(work))
        solve_mod.step_import(str(work))

    def test_tampering_after_the_manifest_is_caught(self):
        for label, mutate in (
            ("device row", lambda w: self._sql(w, "UPDATE devices SET qty = qty + 1 WHERE serial = 'N7-0001'")),
            ("summary", lambda w: (w / "out" / "summary.json").write_text('{"site": "x"}')),
            ("rotated log", lambda w: (w / "logs" / "app.log.1").write_text("other\n")),
            ("input csv", lambda w: (w / "data" / "devices.csv").write_text("serial,model,site,qty\n")),
            ("manifest order", lambda w: (w / "out" / "MANIFEST.sha256").write_text("".join(reversed((w / "out" / "MANIFEST.sha256").read_text().splitlines(True))))),
        ):
            with self.subTest(label):
                work = self.fresh()
                solve_mod.run(str(work))
                mutate(work)
                self.assertEqual(grade("ops", work).returncode, 1)

    def _sql(self, work, statement):
        import sqlite3
        con = sqlite3.connect(str(work / "state" / "north7.db"))
        con.execute(statement)
        con.commit()
        con.close()

    def test_grader_output_has_no_expected_values(self):
        work = self.fresh()
        out = grade("ops", work).stdout
        self.assertNotIn("north-7", out)
        self.assertNotIn(solve_mod.hashlib.sha256((TASKS / "ops" / "template" / "params.json").read_bytes()).hexdigest()[:12], out)

    def test_prompt_has_eight_numbered_steps(self):
        text = (TASKS / "ops" / "prompt.md").read_text()
        self.assertEqual(re.findall(r"^(\d+)\. ", text, re.M), [str(i) for i in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
