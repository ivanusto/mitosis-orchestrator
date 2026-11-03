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


POSIX_ONLY = unittest.skipIf(os.name == "nt", "the harness and its fake claude are POSIX-only")


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


class EncodingTests(unittest.TestCase):
    def test_every_non_alphanumeric_character_becomes_a_dash_and_matches_the_cost_report(self):
        cost_report = load_module("compare_cost_report", REPO / "scripts" / "mitosis-cost-report.py")
        for path in ("/tmp/exp_work/pilot__off", "/home/me/.gemini/x y", "/tmp/claude-1000/-home-me/a.b_c", "/plain/path"):
            self.assertEqual(cmp_run.encode_cwd(path), cost_report.encode_project_dir(path), path)
        self.assertEqual(cmp_run.encode_cwd("/tmp/exp_work/pilot__off"), "-tmp-exp-work-pilot--off")

    @POSIX_ONLY
    def test_project_dir_falls_back_to_a_search_by_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = d
            try:
                root = Path(d) / "projects"
                (root / "-some-other-name").mkdir(parents=True)
                (root / "-some-other-name" / "abc.jsonl").write_text("{}\n")
                self.assertEqual(cmp_run.project_dir_for("/no/such/dir", "abc"), root / "-some-other-name")
                self.assertEqual(cmp_run.project_dir_for("/no/such/dir", "zzz"), root / "-no-such-dir")
                hint = root / "-some-other-name" / "abc.jsonl"
                self.assertEqual(cmp_run.project_dir_for("/x", "abc", str(hint)), root / "-some-other-name")
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old


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
task = "lookup" if os.path.isdir("stdlib") else ("fix" if os.path.isdir("shop") else "ops")
with open(cfg["log"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": cwd, "arm": arm, "task": task, "stdin": stdin_data}) + "\n")
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])
if cfg.get("write_bytecode") and arm == "on":
    pdir = argv[argv.index("--plugin-dir") + 1]
    os.makedirs(os.path.join(pdir, "scripts", "__pycache__"), exist_ok=True)
    with open(os.path.join(pdir, "scripts", "__pycache__", "hook.cpython-312.pyc"), "wb") as bc:
        bc.write(b"\0bytecode")
    with open(os.path.join(pdir, "stray.pyc"), "wb") as bc:
        bc.write(b"\0bytecode")

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
        for part, name in (("A", "money.py"), ("B", "shipping.py"), ("C", "fulfilment.py"), ("D", "calendar.py"), ("E", "ledger.py"), ("F", "currency.py")):
            shutil.copy(os.path.join(tasks_dir, "fix", "reference", part, name), os.path.join("shop", name))
    else:
        subprocess.check_call([sys.executable, os.path.join(tasks_dir, "ops", "reference", "solve.py"), cwd])

sid = "sess-%s-%d" % (task, int(time.time() * 1000) % 100000000)
enc = "".join(c if c.isalnum() else "-" for c in cwd)
proj = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", enc)
os.makedirs(os.path.join(proj, sid, "subagents"), exist_ok=True)
prompt = argv[argv.index("-p") + 1]
expanded = prompt
if arm == "on" and cfg.get("expand", True):
    expanded = "<command-name>/mitosis-orchestrator:orchestrate</command-name> # Mitosis Orchestrator Workflow (`/orchestrate`, v9) ..."
with open(os.path.join(proj, sid + ".jsonl"), "w") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2026-11-03T10:00:00.000Z", "message": {"role": "user", "content": expanded}}) + "\n")
    fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-03T10:00:05.000Z", "message": {"id": "p1", "model": "claude-opus-5", "role": "assistant",
             "content": [{"type": "text", "text": "I read: # Mitosis Orchestrator Workflow" if cfg.get("quote_marker") else "ok"}],
             "usage": {"input_tokens": 0, "output_tokens": int(prim_usd * 40000)}}}) + "\n")
for i in range(n_agents):
    with open(os.path.join(proj, sid, "subagents", "agent-%d.jsonl" % i), "w") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-03T10:00:06.000Z", "isSidechain": True, "message": {"id": "s%d" % i, "model": cfg.get("sub_model", "claude-sonnet-5-5"),
                 "role": "assistant", "usage": {"input_tokens": 0, "output_tokens": int(sub_usd / n_agents * 100000)}}}) + "\n")

init = {"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": "opus", "permissionMode": "acceptEdits",
        "tools": ["Bash", "Read", "Edit"], "plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"],
        "memory_paths": {"auto": "/mem/" + sid}, "messaging_socket_path": "/run/sock-" + sid, "original_cwd": cwd}
if cfg.get("vary_volatile_only_in_on") and arm == "on":
    init["memory_paths"] = {"auto": "/mem/other"}
if arm == "on":
    pname = cfg.get("plugin_name", "mitosis-orchestrator")
    init["plugins"] = [{"name": pname, "path": "/frozen"}]
    if pname == "mitosis-orchestrator":
        init["agents"] += ["mitosis-orchestrator:flash-worker"]
        init["skills"] += ["mitosis-orchestrator:orchestrate"]
        init["slash_commands"] += ["mitosis-orchestrator:orchestrate"]
    else:
        init["agents"] += [pname + ":flash-worker:flash-worker"]
    if cfg.get("on_plugin_warnings"):
        init["plugin_warnings"] = cfg["on_plugin_warnings"]
    init.update(cfg.get("on_init_extra", {}))
out = [init]
for hook in cfg.get("hook_events", {}).get(arm, []):
    out.append({"type": "system", "subtype": "hook_started", "hook_name": hook})


def assistant(mid, ctx, blocks, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": mid, "model": "claude-opus-5", "content": blocks,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 10, "output_tokens": 5}}}


out.append(assistant("m1", 1000, [{"type": "text", "text": "start"}]))
bash_blocks = [{"type": "tool_use", "id": "tb%d" % i, "name": "Bash", "input": {"command": "ls"}} for i in range(cfg.get("bash_calls", 1))]
if cfg.get("leak"):
    bash_blocks.append({"type": "tool_use", "id": "tbl", "name": "Read", "input": {"file_path": cfg["leak"]}})
out.append(assistant("m2", 4000, bash_blocks))
agent_blocks = [{"type": "tool_use", "id": "ta%d" % i, "name": "Agent", "input": {"subagent_type": "mitosis-orchestrator:flash-worker"}} for i in range(n_agents)]
out.append(assistant("m3", 2500, agent_blocks or [{"type": "text", "text": "done"}]))
if n_agents:
    out.append(assistant("side1", 99999, [{"type": "tool_use", "id": "ts1", "name": "Read", "input": {}}], parent="ta0"))
subtype = cfg.get("subtype", "success")
denial_cfg = cfg.get("denials", {}).get(arm, [])
denials = [{"tool_name": d.get("tool_name", "Bash"), "tool_use_id": d.get("tool_use_id", "tb0"), "tool_input": {"command": "x"}} for d in denial_cfg]
for d in denial_cfg:
    if d.get("message") or d.get("decision_reason_type"):
        out.append({"type": "system", "subtype": "permission_denied", "tool_name": d.get("tool_name", "Bash"), "tool_use_id": d.get("tool_use_id", "tb0"),
                    "decision_reason_type": d.get("decision_reason_type", "other"), "message": d.get("message", "")})


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
        # these tests exercise the harness with a fake claude, not the pinned stdlib fixture: re-pin the sandbox copy to this interpreter
        subprocess.run([sys.executable, str(self.root / "evals" / "compare" / "tasks" / "lookup" / "scaffold.py"), "--make-manifest"], check=True)
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
        (self.plugin / "evals" / "compare" / "tasks" / "fix" / "hidden").mkdir(parents=True)
        (self.plugin / "evals" / "compare" / "tasks" / "fix" / "hidden" / "test_hidden.py").write_text("SECRET = 1\n")
        (self.plugin / "evals" / "README.md").write_text("evals\n")
        (self.plugin / "agents-link").symlink_to("claude-agents")
        (self.plugin / ".claude-plugin").mkdir()
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "mitosis-orchestrator"}\n')
        (self.plugin / ".agents").mkdir()
        (self.plugin / ".agents" / "notes.md").write_text("agents\n")
        (self.plugin / ".gitignore").write_text("*.pyc\n")
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
        if "--plugin-dir" not in extra and "--no-plugin" not in extra:
            extra = extra + ("--plugin-dir", str(self.plugin))
        extra = tuple(e for e in extra if e != "--no-plugin")
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

@POSIX_ONLY
class SealTests(unittest.TestCase):
    def test_seal_records_every_fixture_the_protocol_and_the_seed(self):
        sb = Sandbox(self)
        seal = sb.seal("--seed", "42")
        files = seal["files"]
        for name in ("PROTOCOL.md", "run.py", "tasks/lookup/prompt.md", "tasks/fix/prompt.md", "tasks/ops/prompt.md",
                     "tasks/lookup/grade.py", "tasks/fix/grade.py", "tasks/ops/grade.py", "tasks/lookup/scaffold.py",
                     "tasks/lookup/stdlib.manifest.json", "tasks/lookup/key.json", "tasks/fix/hidden/test_hidden.py",
                     "tasks/ops/allowed_tools.txt", "tasks/fix/template/shop/money.py", "repo:scripts/mitosis-cost-report.py"):
            self.assertIn(name, files)
            self.assertEqual(len(files[name]), 64)
        self.assertEqual(seal["seed"], 42)
        self.assertEqual(len(seal["schedule"]), 18)
        self.assertEqual(len(seal["plugin_head"]), 40)
        self.assertEqual(len(seal["plugin_tree_sha256"]), 64)
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

    def test_run_refuses_a_different_seed_a_moved_commit_or_an_unrecorded_tree(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli("run", *sb.common("--seed", "5"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sealed seed", proc.stderr)
        # uncommitted edits do not matter: the export is built from the sealed commit (and they are reported)
        (sb.plugin / "claude-agents" / "flash-worker.md").write_text("---\nmodel: haiku\n---\n")
        proc = sb.cli("run", *sb.common("--spent-before", "59"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("uncommitted changes", proc.stdout)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qam", "move"], cwd=str(sb.plugin), check=True)
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("differs from the seal", proc.stderr)
        self.assertEqual(sb.calls(), [])
        other = Sandbox(self)
        other.seal("--no-plugin")
        proc = other.cli("run", *other.common())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("did not record a plugin tree", proc.stderr)

    def test_export_keeps_dot_directories_and_matches_git_archive_minus_evals(self):
        sb = Sandbox(self)
        dest = sb.tmp / "export"
        cmp_run.export_frozen(sb.plugin, dest)
        self.assertTrue((dest / ".claude-plugin" / "plugin.json").is_file())
        self.assertTrue((dest / ".agents" / "notes.md").is_file())
        self.assertTrue((dest / ".gitignore").is_file())
        self.assertFalse((dest / "claude-plugin").exists())
        self.assertFalse((dest / "evals").exists())
        listing = subprocess.run("git archive --format=tar HEAD | tar -t", shell=True, cwd=str(sb.plugin), stdout=subprocess.PIPE, universal_newlines=True).stdout
        want = {ln.rstrip("/") for ln in listing.splitlines() if ln.strip() and not ln.startswith("evals")}
        got = set()
        for base, dirs, files in os.walk(str(dest)):
            for name in dirs + files:
                got.add(os.path.relpath(os.path.join(base, name), str(dest)))
        self.assertEqual(got, want)

    def test_frozen_export_leaves_out_evals_and_is_hashed(self):
        sb = Sandbox(self)
        dest = sb.tmp / "export"
        commit, digest = cmp_run.export_frozen(sb.plugin, dest)
        self.assertEqual(len(commit), 40)
        self.assertFalse((dest / "evals").exists())
        self.assertTrue((dest / "skills" / "orchestrate" / "SKILL.md").is_file())
        self.assertTrue((dest / "agents-link").is_symlink())
        self.assertEqual(cmp_run.tree_hash(dest), digest)
        self.assertEqual(cmp_run.export_frozen(sb.plugin, sb.tmp / "export2")[1], digest)
        (dest / "claude-agents" / "flash-worker.md").write_text("changed")
        self.assertNotEqual(cmp_run.tree_hash(dest), digest)
        self.assertEqual(sh(["grep", "-r", "SECRET", str(sb.tmp / "export2")]).returncode, 1)

    def test_tree_hash_ignores_bytecode_but_not_other_files(self):
        sb = Sandbox(self)
        dest = sb.tmp / "export"
        _, digest = cmp_run.export_frozen(sb.plugin, dest)
        (dest / "scripts" / "__pycache__").mkdir(parents=True)
        (dest / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\0abc")
        (dest / "a.pyc").write_bytes(b"\0abc")
        self.assertEqual(cmp_run.tree_hash(dest), digest)
        (dest / "scripts" / "hook.py").write_text("x = 1\n")
        with_py = cmp_run.tree_hash(dest)
        self.assertNotEqual(with_py, digest)
        (dest / "scripts" / "hook.py").write_text("x = 2\n")
        self.assertNotEqual(cmp_run.tree_hash(dest), with_py)
        (dest / "scripts" / "hook.py").unlink()
        self.assertEqual(cmp_run.tree_hash(dest), digest)
        (dest / "notes.txt").write_text("n")
        self.assertNotEqual(cmp_run.tree_hash(dest), digest)
        (dest / "notes.txt").unlink()
        (dest / "claude-agents" / "flash-worker.md").write_text("changed")
        self.assertNotEqual(cmp_run.tree_hash(dest), digest)

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

@POSIX_ONLY
class RunTests(unittest.TestCase):
    def test_bytecode_written_into_the_export_during_warmup_is_not_a_change(self):
        sb = Sandbox(self, write_bytecode=True)
        sb.seal()
        proc = sb.cli("run", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("frozen plugin tree changed", proc.stdout + proc.stderr)
        self.assertEqual(len(sb.calls()), 20)

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
            self.assertIn("--strict-mcp-config", argv)
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
                frozen = argv[argv.index("--plugin-dir") + 1]
                self.assertEqual(frozen, str(sb.work / "_frozen" / "plugin"))
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
        self.assertEqual([p.name for p in sb.work.iterdir()], ["_frozen"])
        self.assertFalse((sb.work / "_frozen" / "plugin" / "evals").exists())
        frozen_info = json.loads((sb.out / "frozen.json").read_text())
        self.assertEqual(frozen_info["tree_sha256"], seal["plugin_tree_sha256"])
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
        self.assertEqual(len([p for p in sb.work.iterdir()]), 5)  # four kept work dirs and _frozen
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

    def test_leak_check_marks_a_run_as_a_failure_and_the_report_says_so(self):
        for leak in ("/x/evals/compare/tasks/fix/hidden/test_hidden.py", "/x/anything/key.json", "/x/tasks/ops/reference/solve.py"):
            with self.subTest(leak):
                sb = Sandbox(self, leak=leak)
                sb.seal()
                proc = sb.cli("run", *sb.common("--max-runs", "1"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                res = sb.results()[0]
                self.assertTrue(res["leak_suspect"])
                self.assertEqual(res["grade_exit"], 0)
                self.assertFalse(res["success"])
                self.assertTrue(res["leak_hits"])
                report = sb.cli("report", "--out", str(sb.out)).stdout
                self.assertIn(res["id"], report.split("leak suspects:")[1].split("\n")[0] + report)

    def test_clean_runs_are_not_leak_suspects(self):
        sb = Sandbox(self)
        sb.seal()
        sb.cli("run", *sb.common("--max-runs", "2"))
        for res in sb.results():
            self.assertFalse(res["leak_suspect"])
            self.assertEqual(res["leak_hits"], [])
        # paths inside the working directory never count, however they are named
        blocks = [{"name": "Read", "input": {"file_path": "/w/run1/hidden/reference/key.json"}}]
        self.assertEqual(cmp_run.leak_scan(blocks, "/w/run1"), [])
        self.assertTrue(cmp_run.leak_scan([{"name": "Bash", "input": {"command": "cat /repo/evals/compare/x"}}], "/w/run1"))
        self.assertFalse(cmp_run.leak_scan([{"name": "Bash", "input": {"command": "ls shop; cat shop/money.py; echo hidden state"}}], "/w/run1"))

    def test_transcript_lookup_survives_underscores_and_other_characters(self):
        sb = Sandbox(self)
        # the work root name has underscores and dots, as the real pilot's did
        weird = sb.tmp / "exp_work.v2"
        sb.seal()
        proc = sb.cli("run", "--plugin-dir", str(sb.plugin), "--work-root", str(weird), "--out", str(sb.out), "--max-runs", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = sb.results()[0]
        self.assertIsNone(res["cost_report_error"])
        self.assertGreater(res["transcript_cost"], 0)

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

@POSIX_ONLY
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
        self.assertEqual(self.status(out, "off arm: permission denials"), "PASS")
        self.assertEqual(self.status(out, "on arm: permission denials"), "PASS")
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
        self.assertIn("every Bash call was denied", proc.stdout)
        self.assertIn("pilot stopped", proc.stdout)

    def test_a_denied_compound_command_is_recorded_but_is_not_a_misconfiguration(self):
        denial = {"tool_name": "Bash", "tool_use_id": "x", "decision_reason_type": "other",
                  "message": "A variable in this command can't be checked before it runs"}
        sb = Sandbox(self, bash_calls=4, denials={"off": [denial]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PASS") and "off arm: permission denials" in ln][0]
        self.assertIn("1 denial(s)", line)
        self.assertIn("can't be checked", line)
        self.assertEqual(self.status(proc.stdout, "off arm: Bash allow rules work"), "PASS")
        result = json.loads((sb.out / "pilot" / "off" / "result.json").read_text())
        self.assertEqual(result["permission_denials"], 1)
        self.assertIn("can't be checked", result["permission_denial_reasons"][0])
        self.assertEqual(result["bash_calls"], 4)

    def test_a_denial_that_names_the_mode_or_rules_fails_the_pilot(self):
        denial = {"tool_name": "Bash", "decision_reason_type": "rule", "message": "Bash(ls:*) is not in the allow rules for mode acceptEdits"}
        sb = Sandbox(self, bash_calls=4, denials={"off": [denial]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.status(proc.stdout, "off arm: Bash allow rules work"), "FAIL")
        self.assertEqual(self.status(proc.stdout, "off arm: permission denials"), "FAIL")
        self.assertEqual(len(sb.calls()), 1)

    def test_real_denial_messages_pass_unless_they_name_the_permission_mode(self):
        benign = [
            "A nested command in this command can't be checked before it runs",
            'This Bash command contains multiple operations. The following part requires approval: echo "EXIT=$?"',
        ]
        for message in benign:
            with self.subTest(message[:40]):
                sb = Sandbox(self, bash_calls=5, denials={"off": [{"tool_name": "Bash", "tool_use_id": "tb2", "decision_reason_type": "other", "message": message}]})
                proc = sb.cli("pilot", *sb.common())
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(self.status(proc.stdout, "off arm: permission denials"), "PASS")
                self.assertEqual(self.status(proc.stdout, "off arm: Bash allow rules work"), "PASS")
                result = json.loads((sb.out / "pilot" / "off" / "result.json").read_text())
                self.assertIn(message[:30], result["permission_denial_reasons"][0])
        mode = "Permission to use Bash has been denied because Claude Code is running in don't ask mode."
        sb = Sandbox(self, bash_calls=5, denials={"off": [{"tool_name": "Bash", "tool_use_id": "tb2", "decision_reason_type": "mode", "message": mode}]})
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.status(proc.stdout, "off arm: permission denials"), "FAIL")
        self.assertEqual(self.status(proc.stdout, "off arm: Bash allow rules work"), "FAIL")

    def test_denial_events_are_joined_to_the_result_entries_by_tool_use_id(self):
        events = [{"type": "system", "subtype": "permission_denied", "tool_use_id": "t1", "tool_name": "Bash", "decision_reason_type": "other", "message": "m1"},
                  {"type": "system", "subtype": "permission_denied", "tool_use_id": "t9", "tool_name": "Bash", "decision_reason_type": "mode", "message": "only in the stream"},
                  {"type": "result", "subtype": "success", "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {}},
                                                                                   {"tool_name": "Edit", "tool_use_id": "t2", "tool_input": {}}]}]
        info = cmp_run.analyse_stream(events)
        reasons = {d.get("tool_use_id"): cmp_run.denial_reason(d) for d in info["denials"]}
        self.assertEqual(sorted(reasons), ["t1", "t2", "t9"])
        self.assertEqual(reasons["t1"], "other | m1")
        self.assertEqual(reasons["t2"], "")
        self.assertIn("mode", reasons["t9"])

    def test_pilot_fails_when_the_plugin_loaded_under_the_wrong_name(self):
        sb = Sandbox(self, plugin_name="plugin")
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 1)
        line = [ln for ln in proc.stdout.splitlines() if "plugin, slash command and agent are present" in ln][0]
        self.assertTrue(line.startswith("FAIL"))
        self.assertIn("mitosis-orchestrator:orchestrate", line)
        self.assertIn("plugin:flash-worker:flash-worker", line)

    def test_skill_expansion_is_judged_on_user_side_text_only(self):
        # the assistant quotes the H1 but the user message was never expanded: still FAIL
        sb = Sandbox(self, expand=False, quote_marker=True)
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(self.status(proc.stdout, "on arm skill expanded under -p"), "FAIL")
        self.assertIn("-p does not appear to expand plugin slash commands", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_run_specific_init_fields_are_not_a_plugin_effect_but_real_ones_are(self):
        sb = Sandbox(self, vary_volatile_only_in_on=True)
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.status(proc.stdout, "init events differ only in mitosis items"), "PASS")
        sb2 = Sandbox(self, on_init_extra={"model": "sonnet"})
        proc2 = sb2.cli("pilot", *sb2.common())
        self.assertEqual(proc2.returncode, 1)
        self.assertIn("key model differs", proc2.stdout)

    def test_plugin_warnings_of_the_plugin_itself_are_a_mitosis_item_but_others_fail(self):
        ok = ['mitosis-orchestrator: Default agents/ folder is ignored because the manifest sets "agents"']
        sb = Sandbox(self, on_plugin_warnings=ok)
        proc = sb.cli("pilot", *sb.common())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.status(proc.stdout, "init events differ only in mitosis items"), "PASS")
        for label, warnings in (("other plugin", ok + ["other-plugin: manifest problem"]), ("unnamed", ['Default agents/ folder is ignored because the manifest sets "agents"'])):
            with self.subTest(label):
                sb = Sandbox(self, on_plugin_warnings=warnings)
                proc = sb.cli("pilot", *sb.common())
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(self.status(proc.stdout, "init events differ only in mitosis items"), "FAIL")
                self.assertIn("plugin_warnings", proc.stdout)

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


@POSIX_ONLY
class CalibrateTests(unittest.TestCase):
    def args(self, sb, task, arm, *extra):
        return ["calibrate", "--task", task, "--arm", arm, "--plugin-dir", str(sb.plugin), "--work-root", str(sb.work), "--out", str(sb.out)] + list(extra)

    def test_one_unscored_run_is_printed_charged_and_cleaned_up(self):
        sb = Sandbox(self, cost=0.8)
        proc = sb.cli(*self.args(sb, "ops", "on", "--cap", "3", "--spent-before", "2"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for needle in ("cost 0.8", "tool calls", "wall", "peak context 4000", "grade: pass"):
            self.assertIn(needle, proc.stdout)
        calls = sb.calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(calls[0]["task"], "ops")
        ledger = [json.loads(ln) for ln in (sb.out / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in ledger], ["start", "end"])
        self.assertEqual(ledger[0]["kind"], "calibration")
        self.assertFalse((sb.out / "runs").exists())
        self.assertEqual([p.name for p in sb.work.iterdir()], ["_frozen"])
        again = sb.cli(*self.args(sb, "ops", "on"))
        self.assertEqual(again.returncode, 0)
        self.assertEqual(len(list((sb.out / "calibration").glob("ops__on__*"))), 2)

    def test_reports_a_failed_grade_and_respects_the_budget(self):
        sb = Sandbox(self, solve=False)
        proc = sb.cli(*self.args(sb, "fix", "off"))
        self.assertIn("grade: fail", proc.stdout)
        stopped = sb.cli(*self.args(sb, "fix", "off", "--spent-before", "59"))
        self.assertEqual(stopped.returncode, 4)
        self.assertEqual(len(sb.calls()), 1)

    def test_refuses_once_a_seal_exists(self):
        sb = Sandbox(self)
        sb.seal()
        proc = sb.cli(*self.args(sb, "lookup", "off"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("SEAL.json exists", proc.stderr)
        self.assertEqual(sb.calls(), [])


class ArmParityTests(unittest.TestCase):
    def test_arms_differ_only_by_plugin_dir_and_prefix(self):
        self.assertEqual(cmp_run.arm_parity_problems(), [])
        off = cmp_run.build_command("claude", "off", "fix", "P", "/f", 5.0)
        on = cmp_run.build_command("claude", "on", "fix", "P", "/f", 5.0)
        self.assertIn("--strict-mcp-config", off)
        self.assertIn("--strict-mcp-config", on)
        self.assertEqual(len(on) - len(off), 2)

    def test_a_flag_on_one_arm_only_is_caught(self):
        original = cmp_run.build_command

        def skewed(claude, arm, task, prompt, plugin_dir, cap):
            cmd = original(claude, arm, task, prompt, plugin_dir, cap)
            return cmd + ["--verbose"] if arm == "on" else cmd
        cmp_run.build_command = skewed
        try:
            self.assertTrue(cmp_run.arm_parity_problems())
        finally:
            cmp_run.build_command = original


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
        extra = {"echo", "cd", "pwd", "sort", "uniq", "cut", "diff"}
        bash = {
            "lookup": {"ls", "cat", "grep", "sed", "find", "wc", "head", "tail"} | extra,
            "fix": {"python3", "git", "ls", "cat", "grep", "sed", "find", "wc", "head", "tail"} | extra,
            "ops": {"python3", "ls", "cat", "grep", "sed", "find", "wc", "head", "tail", "mkdir", "cp", "mv", "sha256sum", "date"} | extra,
        }
        for task, cmds in bash.items():
            rules = [ln.strip() for ln in (TASKS / task / "allowed_tools.txt").read_text().splitlines() if ln.strip()]
            self.assertTrue(always <= set(rules), task)
            scoped = {re.match(r"^Bash\((\w+):\*\)$", r).group(1) for r in rules if r.startswith("Bash")}
            self.assertEqual(scoped, cmds, task)
            self.assertEqual("python3" in scoped, task != "lookup")
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
        for name in ("seal", "pilot", "calibrate", "run", "report"):
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
        self.assertIn("30 of 30", proc.stdout)

    def test_pass_mark_is_at_most_one_wrong_and_expected_values_are_not_printed(self):
        work = scaffold("lookup", self.tmp / "w")
        key = json.loads((TASKS / "lookup" / "key.json").read_text())
        self.assertEqual(len(key), 30)

        def broken(*ids):
            out = dict(key)
            for q in ids:
                out[q] = "wrong" if not isinstance(key[q], (int, float)) else key[q] + 1
            (work / "answers.json").write_text(json.dumps(out))
            return grade("lookup", work)

        ids = sorted(key)
        for q in ids:
            proc = broken(q)
            self.assertEqual(proc.returncode, 0, q)        # one slip is within the pass mark
            self.assertIn("%s wrong" % q, proc.stdout)
            for value in key.values():
                if isinstance(value, str):
                    self.assertNotIn(value, proc.stdout)
        two = broken(ids[0], ids[1])
        self.assertEqual(two.returncode, 1)
        self.assertIn("28 of 30", two.stdout)
        partial = dict(key)
        del partial[ids[0]], partial[ids[1]]
        (work / "answers.json").write_text(json.dumps(partial))
        self.assertEqual(grade("lookup", work).returncode, 1)
        (work / "answers.json").write_text("not json")
        self.assertEqual(grade("lookup", work).returncode, 1)
        self.assertIn("MAX_WRONG = 1", (TASKS / "lookup" / "grade.py").read_text())

    def test_questions_need_more_than_one_grep(self):
        specs = json.loads((TASKS / "lookup" / "questions.json").read_text())
        self.assertEqual(len(specs), 30)
        self.assertEqual([s["id"] for s in specs], ["q%02d" % i for i in range(1, 31)])
        self.assertLessEqual({s["type"] for s in specs}, {"imported_const", "default_chain", "method_origin", "caller_callee", "raise_class", "const_chain"})
        self.assertGreaterEqual(len({s["path"] for s in specs}), 28)
        self.assertGreaterEqual(len({s["path"].split("/")[0] for s in specs}), 8)
        key = json.loads((TASKS / "lookup" / "key.json").read_text())
        for s in specs:
            answer = key[s["id"]]
            if s["type"] in ("method_origin", "caller_callee", "raise_class"):
                self.assertNotEqual(answer.split(":")[0], s["path"], s["id"])   # the answer is always in another file
        text = (TASKS / "lookup" / "prompt.md").read_text()
        for s in specs:
            self.assertIn("- %s: " % s["id"], text)

    def test_key_is_derived_from_the_real_tree_by_script(self):
        proc = sh([sys.executable, str(TASKS / "lookup" / "verify_key.py")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 mismatches", proc.stdout)
        work = scaffold("lookup", self.tmp / "w")
        proc = sh([sys.executable, str(TASKS / "lookup" / "verify_key.py"), str(work / "stdlib")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_questions_are_reproducible_from_the_generator(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(str(TASKS / "lookup"), str(Path(d) / "lookup"), ignore=shutil.ignore_patterns("__pycache__"))
            proc = sh([sys.executable, str(Path(d) / "lookup" / "make_questions.py")])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            for name in ("questions.json", "key.json", "prompt.md"):
                self.assertEqual((Path(d) / "lookup" / name).read_text(), (TASKS / "lookup" / name).read_text(), name)

    def test_verify_key_catches_a_wrong_key(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(str(TASKS / "lookup"), str(Path(d) / "lookup"), ignore=shutil.ignore_patterns("__pycache__"))
            kp = Path(d) / "lookup" / "key.json"
            key = json.loads(kp.read_text())
            key["q01"] = "somewhere/else.py:1"
            kp.write_text(json.dumps(key))
            proc = sh([sys.executable, str(Path(d) / "lookup" / "verify_key.py")])
            self.assertEqual(proc.returncode, 1)
            self.assertIn("MISMATCH q01", proc.stdout)

    def test_tree_is_read_only_and_pinned(self):
        work = scaffold("lookup", self.tmp / "w")
        manifest = json.loads((TASKS / "lookup" / "stdlib.manifest.json").read_text())
        self.assertGreater(len(manifest["files"]), 150)
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


ALT_ALLOCATE = '''def allocate(total, weights):
    from fractions import Fraction
    if not weights:
        raise ValueError("allocate needs at least one weight")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("weights must be non-negative and not all zero")
    sign = -1 if total < 0 else 1
    exact = [Fraction(abs(total) * w, sum(weights)) for w in weights]
    parts = [int(x) for x in exact]
    spare = abs(total) - sum(parts)
    ranked = sorted(range(len(parts)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in ranked[:spare]:
        parts[i] += 1
    return [sign * p for p in parts]'''

NON_MIRRORING_ALLOCATE = '''def allocate(total, weights):
    if not weights:
        raise ValueError("allocate needs at least one weight")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("weights must be non-negative and not all zero")
    ws = sum(weights)
    shares = [total * w // ws for w in weights]
    leftover = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-((total * weights[i]) % ws), i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares'''

FIX_PARTS = {"A": "money.py", "B": "shipping.py", "C": "fulfilment.py", "D": "calendar.py", "E": "ledger.py", "F": "currency.py"}
MASKED_BY = {"C": "B", "E": "D"}


class FixFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cmp-fix-"))
        cls.base = scaffold("fix", cls.tmp / "base")

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.tmp)

    def setUp(self):
        self.n = 0

    def with_fixes(self, fixes):
        """The scaffolded package with the reference file of every cause in `fixes` put in place."""
        for part, name in FIX_PARTS.items():
            src = (TASKS / "fix" / "reference" / part / name) if part in fixes else (TASKS / "fix" / "template" / "shop" / name)
            shutil.copy(str(src), str(self.base / "shop" / name))
        return self.base

    def suite(self, work, tests_dir):
        env = dict(os.environ, PYTHONPATH=str(work), PYTHONDONTWRITEBYTECODE="1")
        return sh([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(tests_dir)], cwd=str(work), env=env)

    def both(self, work):
        return self.suite(work, work / "tests").returncode, self.suite(work, TASKS / "fix" / "hidden").returncode

    def test_package_size_and_layers(self):
        pkg = TASKS / "fix" / "template" / "shop"
        mods = sorted(p.name for p in pkg.glob("*.py"))
        self.assertGreaterEqual(len(mods), 60)
        lines = sum(len(p.read_text().splitlines()) for p in pkg.glob("*.py"))
        tests = sum(len(p.read_text().splitlines()) for p in (TASKS / "fix" / "template" / "tests").glob("*.py"))
        self.assertGreaterEqual(lines, 3500)
        self.assertGreaterEqual(lines + tests, 5000)
        layers = {0: ["errors", "clock", "ids", "units", "money", "validate", "textutil", "calendar", "events", "cache", "rate_limit", "serializers"],
                  1: ["store", "catalog", "customers", "addresses", "inventory", "fx_rates", "suppliers", "warehouse", "address_book", "wishlist", "reviews",
                      "permissions", "payments"],
                  2: ["pricing", "tax", "promotions", "discounts", "shipping", "loyalty", "currency", "pricelists", "tax_rules", "carriers", "sla", "bundles",
                      "gift_cards", "returns_policy", "recommendations"],
                  3: ["orders", "fulfilment", "invoicing", "returns", "ledger", "subscriptions", "dunning", "purchasing", "picking", "tracking"]}
        rank = {m: n for n, names in layers.items() for m in names}
        self.assertEqual(sorted(rank), sorted(m[:-3] for m in mods if m not in ("__init__.py",) and m[:-3] not in
                                              ("audit", "notifications", "config", "search", "forecast", "reports", "exports", "api", "cli",
                                               "analytics", "dashboard", "importers")))
        for mod, n in rank.items():
            text = (pkg / (mod + ".py")).read_text()
            for other in re.findall(r"^(?:from shop import|from shop\.(\w+) import) ?(.*)$", text, re.M):
                named = [other[0]] if other[0] else [x.strip() for x in other[1].split(",")]
                for name in named:
                    if name in rank:
                        self.assertLessEqual(rank[name], n, "%s (layer %d) imports %s (layer %d)" % (mod, n, name, rank[name]))

    def test_untouched_scaffold_fails_and_the_reference_passes(self):
        self.assertEqual(self.both(self.with_fixes("")), (1, 1))
        self.assertNotEqual(grade("fix", self.with_fixes("")).returncode, 0)
        full = self.with_fixes("ABCDEF")
        self.assertEqual(self.both(full), (0, 0))
        proc = grade("fix", full)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_no_proper_subset_of_the_six_fixes_is_enough(self):
        import itertools
        checked = 0
        for r in range(0, 6):
            for fixes in itertools.combinations("ABCDEF", r):
                work = self.with_fixes("".join(fixes))
                self.assertEqual(self.both(work), (1, 1), "".join(fixes) or "none")
                checked += 1
        self.assertEqual(checked, 63)  # every proper subset of the six fixes, the empty one included
        self.assertNotEqual(grade("fix", self.with_fixes("ABCDE")).returncode, 0)

    def test_the_causes_are_in_six_modules_and_two_are_masked(self):
        template = TASKS / "fix" / "template" / "shop"
        self.assertEqual(len(set(FIX_PARTS.values())), 6)
        for part, name in FIX_PARTS.items():
            ref = TASKS / "fix" / "reference" / part
            self.assertEqual([f for f in os.listdir(str(ref)) if f != "__pycache__"], [name])
            self.assertNotEqual((ref / name).read_text(), (template / name).read_text())
        for masked, hider in MASKED_BY.items():
            names = {"C": "test_cancelling_after_a_partial_shipment_only_releases_the_rest", "E": "test_payments_settle_the_oldest_invoice_first"}
            test = names[masked]
            first = self.suite(self.with_fixes(""), self.base / "tests").stderr
            self.assertIn("ERROR: " + test, first)
            self.assertNotIn("FAIL: " + test, first)
            after = self.suite(self.with_fixes(hider), self.base / "tests").stderr
            self.assertIn("FAIL: " + test, after)

    def test_failures_show_up_in_several_test_modules(self):
        text = self.suite(self.with_fixes(""), self.base / "tests").stderr
        modules = set(re.findall(r"^(?:FAIL|ERROR): \w+ \((\w+)\.", text, re.M))
        self.assertGreaterEqual(len(modules), 6, modules)

    def test_a_patch_that_works_around_the_symptoms_passes_visible_and_fails_hidden(self):
        work = self.with_fixes("")
        saved = {}
        for f in sorted((TASKS / "fix" / "symptom").glob("*.py")):
            saved[f.name] = (work / "shop" / f.name).read_text()
            shutil.copy(str(f), str(work / "shop" / f.name))
        try:
            self.assertEqual(self.suite(work, work / "tests").returncode, 0)
            self.assertNotEqual(self.suite(work, TASKS / "fix" / "hidden").returncode, 0)
            proc = grade("fix", work)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("visible: exit 0", proc.stdout)
            self.assertIn("hidden: exit 1", proc.stdout)
        finally:
            for name, text in saved.items():
                (work / "shop" / name).write_text(text)
        self.assertEqual(sorted(saved), ["api.py", "discounts.py", "fulfilment.py", "invoicing.py", "orders.py", "pricelists.py", "returns.py", "subscriptions.py"])

    def test_hidden_requirements_are_all_documented_or_pinned_by_visible_tests(self):
        pkg = TASKS / "fix" / "template" / "shop"
        money_src = (pkg / "money.py").read_text()
        doc = money_src.split("def allocate")[1].split('"""')[1]
        self.assertIn("negative total", doc)
        self.assertIn("sign flipped", doc)
        self.assertIn("add up to total exactly", doc)
        self.assertIn("earlier parts", money_src.split("def split_evenly")[1].split('"""')[1])
        self.assertIn("heaviest parcel in grams that fits the tier", (pkg / "shipping.py").read_text())
        self.assertIn("clamped to its last day", (pkg / "calendar.py").read_text().split("def add_months")[1])
        self.assertIn("never takes more from an invoice than its remaining balance", " ".join((pkg / "ledger.py").read_text().split()))
        self.assertIn("oldest due date first (invoice number breaks ties)", (pkg / "ledger.py").read_text())
        self.assertIn("rounded to the nearest cent (halves away from zero)", (pkg / "currency.py").read_text())
        self.assertIn("Always counted from the start date", " ".join((pkg / "subscriptions.py").read_text().split()))
        visible = "\n".join(p.read_text() for p in (TASKS / "fix" / "template" / "tests").glob("test_*.py"))
        for pinned in ("[145, 331, 24]", "[34, 33, 33]", "largest_remainders", "(5000, 5000, 1295)", "[209, 476, 110]", "-1563",
                       "DATE(2026, 2, 28), DATE(2026, 3, 31)", '[("SIN-00001", 700), ("SIN-00002", 500)]', '"P-BOOK": 2169'):
            self.assertIn(pinned, visible)

    def test_a_fix_that_follows_every_documented_requirement_passes_hidden(self):
        work = self.with_fixes("BCDEF")
        text = (work / "shop" / "money.py").read_text()
        a, b = text.index("def allocate"), text.index("def split_evenly")
        (work / "shop" / "money.py").write_text(text[:a] + ALT_ALLOCATE + "\n\n" + text[b:])
        try:
            self.assertEqual(self.both(work), (0, 0))
        finally:
            shutil.copy(str(TASKS / "fix" / "template" / "shop" / "money.py"), str(work / "shop" / "money.py"))

    def test_a_largest_remainder_fix_that_does_not_mirror_negatives_fails_exactly_the_two_documented_hidden_tests(self):
        work = self.with_fixes("BCDEF")
        text = (work / "shop" / "money.py").read_text()
        a, b = text.index("def allocate"), text.index("def split_evenly")
        (work / "shop" / "money.py").write_text(text[:a] + NON_MIRRORING_ALLOCATE + "\n\n" + text[b:])
        try:
            self.assertEqual(self.suite(work, work / "tests").returncode, 0)
            out = self.suite(work, TASKS / "fix" / "hidden").stderr
            failed = sorted(set(re.findall(r"^FAIL: (\w+)", out, re.M)))
            self.assertEqual(failed, ["test_installments_and_refund_splits_elsewhere", "test_negative_totals_mirror_positive_ones"])
        finally:
            shutil.copy(str(TASKS / "fix" / "template" / "shop" / "money.py"), str(work / "shop" / "money.py"))

    def test_editing_the_visible_tests_does_not_help(self):
        work = scaffold("fix", self.tmp / "edit")
        for p in (work / "tests").glob("test_*.py"):
            p.write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        self.assertNotEqual(grade("fix", work).returncode, 0)

    def test_scaffold_is_a_git_repo_with_a_clean_initial_commit(self):
        work = scaffold("fix", self.tmp / "git")
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

    def test_three_sites_and_a_long_chain(self):
        self.assertEqual(solve_mod.SITES, ["north-7", "south-2", "east-5"])
        self.assertEqual(len(solve_mod.STEPS), 3 * 6 + 3)
        self.assertEqual([p.name for p in sorted((TASKS / "ops" / "template" / "params").iterdir())], ["east-5.json", "north-7.json", "south-2.json"])
        work = self.fresh()
        solve_mod.run(str(work))
        manifest = (work / "out" / "MANIFEST.sha256").read_text().splitlines()
        self.assertEqual(len(manifest), 1 + 3 * 5)
        conf = {s: solve_mod.read_conf(str(work), s) for s in solve_mod.SITES}
        self.assertEqual([conf[s]["upstream_build"] for s in solve_mod.SITES], ["none", conf["north-7"]["build_id"], conf["south-2"]["build_id"]])
        self.assertEqual(len({c["db_path"] for c in conf.values()}), 3)
        self.assertEqual(len({os.path.dirname(c["log_path"]) for c in conf.values()}), 2)

    def test_grader_fails_on_every_incomplete_state(self):
        work = self.fresh()
        self.assertEqual(grade("ops", work).returncode, 1)
        steps = solve_mod.STEPS
        for i, (label, fn) in enumerate(steps, 1):
            fn(str(work))
            expected = 1 if i < len(steps) - 1 else 0          # complete once the manifest is written; the last step only checks
            self.assertEqual(grade("ops", work).returncode, expected, "after %s" % label)

    def test_each_step_depends_on_the_one_before_it(self):
        steps = solve_mod.STEPS
        for idx in range(1, len(steps)):
            with self.subTest(step=steps[idx][0], without=steps[idx - 1][0]):
                work = self.fresh()
                for _, fn in steps[:idx - 1]:
                    fn(str(work))
                with self.assertRaises(Exception):
                    steps[idx][1](str(work))

    def test_the_site_guard_bites_the_first_import_unless_the_config_was_read(self):
        import sqlite3
        work = self.fresh()
        for label in ("render north-7", "create_db north-7", "migrate north-7"):
            dict(solve_mod.STEPS)[label](str(work))
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            solve_mod.step_import(str(work), "north-7")
        self.assertIn("settings.site", str(ctx.exception))
        solve_mod.step_set_site(str(work), "north-7")
        solve_mod.step_import(str(work), "north-7")

    def test_a_script_copied_from_north_7_fails_on_south_2_and_on_east_5(self):
        import sqlite3
        steps = solve_mod.STEPS
        work = self.fresh()
        for _, fn in steps[:6 + 5]:               # north-7 done, south-2 through its import
            fn(str(work))
        with self.assertRaises(FileNotFoundError):
            solve_mod.naive_rotate(str(work), "south-2")      # its log is not at logs/<site>.log
        self.assertIn("south/current.log", solve_mod.read_conf(str(work), "south-2")["log_path"])
        work2 = self.fresh()
        for _, fn in steps[:12 + 4]:              # everything up to east-5's set_site
            fn(str(work2))
        with self.assertRaises(sqlite3.IntegrityError):
            solve_mod.naive_import(str(work2), "east-5")      # duplicate serials hit the unique index
        solve_mod.step_import(str(work2), "east-5")           # the stated rule resolves them

    def test_duplicate_rule_keeps_the_largest_qty_and_the_earlier_row_on_a_tie(self):
        rows = {r[0]: r for r in solve_mod.csv_rows(str(TASKS / "ops" / "template"), "east-5")}
        self.assertEqual(rows["E5-0003"][3], 21)
        self.assertEqual(rows["E5-0007"][1], "Gateway G2")
        self.assertEqual(rows["E5-0011"][3], 7)
        self.assertEqual(len(solve_mod.csv_rows(str(TASKS / "ops" / "template"), "east-5", dedupe=False)) - len(rows), 3)

    def test_tampering_after_the_manifest_is_caught(self):
        for label, mutate in (
            ("device row", lambda w: self._sql(w, "UPDATE devices SET qty = qty + 1 WHERE serial = 'S2-0001'")),
            ("summary", lambda w: (w / "out" / "summary.json").write_text('{"sites": {}}')),
            ("rotated log", lambda w: (w / "logs" / "south" / "current.log.1").write_text("other\n")),
            ("input csv", lambda w: (w / "data" / "east-5.csv").write_text("serial,model,site,qty\n")),
            ("conf", lambda w: (w / "conf" / "east-5.conf").write_text("x\n")),
            ("manifest order", lambda w: (w / "out" / "MANIFEST.sha256").write_text("".join(reversed((w / "out" / "MANIFEST.sha256").read_text().splitlines(True))))),
        ):
            with self.subTest(label):
                work = self.fresh()
                solve_mod.run(str(work))
                mutate(work)
                self.assertEqual(grade("ops", work).returncode, 1)

    def _sql(self, work, statement):
        import sqlite3
        con = sqlite3.connect(str(work / "state" / "south" / "s2.db"))
        con.execute(statement)
        con.commit()
        con.close()

    def test_grader_output_has_no_expected_values(self):
        work = self.fresh()
        out = grade("ops", work).stdout
        self.assertNotIn("north-7", out.replace("[north-7]", ""))
        digest = solve_mod.hashlib.sha256((TASKS / "ops" / "template" / "params" / "north-7.json").read_bytes()).hexdigest()[:12]
        self.assertNotIn(digest, out)

    def test_prompt_has_seven_numbered_steps(self):
        text = (TASKS / "ops" / "prompt.md").read_text()
        self.assertEqual(re.findall(r"^(\d+)\. ", text, re.M), [str(i) for i in range(1, 8)])


if __name__ == "__main__":
    unittest.main()
