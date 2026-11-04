#!/usr/bin/env python3
"""Overhead experiment: where does the plugin's extra cost on a short task come from?

    run.py seal  --plugin-commit SHA [--seed N] [--plugin-repo DIR]
    run.py probe --work-root DIR --out DIR [--spent-before USD]
    run.py run   --work-root DIR --out DIR [--spent-before USD] [--resume] [--keep]
    run.py report --out DIR

See PROTOCOL.md for the pre-registered design and the rule. Python 3.8, standard library only.
This file is part of the seal: changing it after `seal` makes `probe` and `run` refuse.

It reuses helpers from ../compare/run.py, loaded by path. That file is sealed by the compare experiment and is
never edited here; importing it must not leave bytecode in evals/compare, hence the flag below.

Exit codes: 0 ok, 1 failed report, 2 refused, 3 a run was not in its intended arm, 4 stopped by the budget.
"""

import sys

sys.dont_write_bytecode = True  # before importing compare/run.py: its tamper check trips on bytecode

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
COMPARE_DIR = HERE.parent / "compare"
FIXTURE = COMPARE_DIR / "tasks" / "lookup"
SEAL_PATH = HERE / "SEAL.json"

ARMS = ("off", "loaded", "orchestrate")
PLUGIN_ARMS = ("loaded", "orchestrate")
REPEATS = 3
RUN_CAP_USD = 2.0
PROBE_CAP_USD = 0.5
BUDGET_LIMIT_USD = 10.0
DEFAULT_SEED = 20261104
DEFAULT_TIMEOUT_S = 1800
PROBE_PROMPT = "Reply OK\n"
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write")
DIFFER_EXTRA = Fraction(10, 100)
SHARE_MIN = Fraction(50, 100)
PLUGIN_NAME = "mitosis-orchestrator"
AGENT_NAME = "mitosis-orchestrator:flash-worker"
SKILL_NAME = "mitosis-orchestrator:orchestrate"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cmp = _load("compare_run_for_overhead", COMPARE_DIR / "run.py")
Refused = cmp.Refused
BudgetStop = cmp.BudgetStop


class ArmStateError(Exception):
    """A run was not in the arm it was meant to be in."""


_COST_REPORT = []


def cost_report():
    if not _COST_REPORT:
        _COST_REPORT.append(_load("cost_report_for_overhead", REPO / "scripts" / "mitosis-cost-report.py"))
    return _COST_REPORT[0]


# ---------------------------------------------------------------------------------------------
# prices and decomposition. Pure functions.
# ---------------------------------------------------------------------------------------------

def _frac(value):
    return Fraction(str(value))


def price_of(model, sub=False):
    """(input, cache_write, cache_read, output) in USD per million tokens, as Fractions. Primary calls use the
    1-hour cache-write rate and subagent calls the 5-minute rate, as scripts/mitosis-cost-report.py does.
    A model without a price is priced as `opus`."""
    cr = cost_report()
    row = cr.price_for(str(model or ""), cr.DEFAULT_PRICES) or cr.price_for("opus", cr.DEFAULT_PRICES)
    p_in, p_cw, p_cr, p_out = [_frac(x) for x in row]
    if not sub:
        p_cw = _frac(cr.CACHE_1H_WRITE_MULTIPLIER) * p_in
    return p_in, p_cw, p_cr, p_out


def usd_by_class(calls, pricer=price_of):
    """USD per token class for a list of deduplicated calls."""
    out = {k: Fraction(0) for k in TOKEN_CLASSES}
    for c in calls:
        p_in, p_cw, p_cr, p_out = pricer(c.get("model"), bool(c.get("sub")))
        out["input"] += int(c.get("input") or 0) * p_in / 1000000
        out["output"] += int(c.get("output") or 0) * p_out / 1000000
        out["cache_read"] += int(c.get("cache_read") or 0) * p_cr / 1000000
        out["cache_write"] += int(c.get("cache_write") or 0) * p_cw / 1000000
    return out


def static_cost(extra_tokens, calls, p_cache_write, p_cache_read):
    """Modelled USD per run of `extra_tokens` of prefix text: written once, read on each later primary call."""
    extra = max(0, int(extra_tokens))
    k = max(1, int(calls))
    return Fraction(extra) * (p_cache_write + (k - 1) * p_cache_read) / 1000000


def first_prefix(calls):
    """input + cache read + cache write tokens of the first primary call, or None."""
    for c in calls:
        if not c.get("sub"):
            return int(c.get("input") or 0) + int(c.get("cache_read") or 0) + int(c.get("cache_write") or 0)
    return None


def extract_calls(events):
    """One record per API call, deduplicated by message.id (the stream repeats it once per content block).
    Token counts are the maximum seen for that id; tool names are listed once per tool_use id. No content."""
    order, best, seen_tools = [], {}, {}
    for e in events:
        if e.get("type") != "assistant" or not isinstance(e.get("message"), dict):
            continue
        message = e["message"]
        mid = message.get("id")
        if not mid:
            continue
        usage = message.get("usage") or {}
        rec = best.get(mid)
        if rec is None:
            rec = {"message_id": mid, "model": message.get("model"), "sub": bool(e.get("parent_tool_use_id")),
                   "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "tools": []}
            best[mid] = rec
            order.append(mid)
            seen_tools[mid] = set()
        for key, src in (("input", "input_tokens"), ("output", "output_tokens"),
                         ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
            rec[key] = max(rec[key], int(usage.get(src) or 0))
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                key = block.get("id") or id(block)
                if key not in seen_tools[mid]:
                    seen_tools[mid].add(key)
                    rec["tools"].append(str(block.get("name")))
    calls = []
    for mid in order:
        rec = best[mid]
        if rec["model"] == "<synthetic>" and not any(rec[k] for k in TOKEN_CLASSES):
            continue
        rec["n"] = len(calls) + 1
        calls.append(rec)
    return calls


# ---------------------------------------------------------------------------------------------
# arm state
# ---------------------------------------------------------------------------------------------

def arm_state(init, arm):
    """The plugin facts the init event shows, and whether they match the intended arm. Returns (state, ok, detail)."""
    if init is None:
        return {"plugin_loaded": None, "agents_loaded": None, "skill_loaded": None}, False, "no init event in the stream"
    plugins = cmp._names(init.get("plugins"))
    agents = cmp._names(init.get("agents"))
    skills = cmp._names(init.get("skills")) | cmp._names(init.get("slash_commands"))
    state = {
        "plugin_loaded": any(n == PLUGIN_NAME or n.startswith(PLUGIN_NAME + "@") for n in plugins),
        "agents_loaded": AGENT_NAME in agents,
        "skill_loaded": SKILL_NAME in skills,
    }
    anywhere = [n for n in plugins | agents | skills if cmp._is_mitosis(n)]
    if arm in PLUGIN_ARMS:
        missing = [k for k, v in state.items() if not v]
        return state, not missing, ("ok" if not missing else "expected loaded, missing: " + ", ".join(missing))
    bad = [k for k, v in state.items() if v]
    if anywhere and not bad:
        bad = ["mitosis item " + anywhere[0][:60]]
    return state, not bad, ("ok" if not bad else "expected no plugin, found: " + ", ".join(bad))


# ---------------------------------------------------------------------------------------------
# rule (PROTOCOL.md). Pure functions, no I/O.
# ---------------------------------------------------------------------------------------------

def differs(runs_a, runs_off):
    """DIFFERS(A): complete separation with A above off and a median at least 10% above the off median.
    runs are lists of (success, cost). Incomplete data never differs."""
    if len(runs_a) != REPEATS or len(runs_off) != REPEATS:
        return False
    a = [_frac(c) for _, c in runs_a]
    o = [_frac(c) for _, c in runs_off]
    return min(a) > max(o) and (median(a) - median(o)) >= DIFFER_EXTRA * median(o)


def share(part, whole):
    """part / whole as a Fraction, or None when the whole is not positive."""
    if part is None or whole is None or whole <= 0:
        return None
    return Fraction(part) / Fraction(whole)


def decide(runs, static_usd, output_usd):
    """runs: arm -> list of (success, cost). static_usd: arm -> modelled static USD per run or None.
    output_usd: arm -> median output-token USD per run or None. Returns {"outcome", "arms", "reason"}."""
    arms = {}
    off = runs.get("off", [])
    for arm in PLUGIN_ARMS:
        a = runs.get(arm, [])
        row = {"label": None, "differs": differs(a, off), "median_diff": None, "pct": None, "static_share": None, "output_share": None}
        if len(a) == REPEATS and len(off) == REPEATS:
            row["label"] = cmp.verdict(a, off)[0]
            diff = median([_frac(c) for _, c in a]) - median([_frac(c) for _, c in off])
            row["median_diff"] = diff
            m_off = median([_frac(c) for _, c in off])
            row["pct"] = None if m_off == 0 else diff / m_off * 100
            row["static_share"] = share(static_usd.get(arm), diff)
            o_a, o_off = output_usd.get(arm), output_usd.get("off")
            row["output_share"] = share(None if o_a is None or o_off is None else o_a - o_off, diff)
        else:
            row["label"] = "INCONCLUSIVE"
        arms[arm] = row
    trim = [a for a in PLUGIN_ARMS if arms[a]["differs"] and arms[a]["static_share"] is not None and arms[a]["static_share"] >= SHARE_MIN]
    o = arms["orchestrate"]
    if trim:
        return {"outcome": "TRIM TEXT", "arms": arms, "reason": "cost differs for %s and the static prefix explains at least 50%% of it" % ", ".join(trim)}
    if o["differs"] and not arms["loaded"]["differs"] and o["output_share"] is not None and o["output_share"] >= SHARE_MIN:
        return {"outcome": "REWORD SKILL", "arms": arms, "reason": "only the orchestrate arm differs and output tokens explain at least 50% of it"}
    return {"outcome": "NO TRIM", "arms": arms, "reason": "no arm met a trim or reword condition"}


# ---------------------------------------------------------------------------------------------
# schedule, seal
# ---------------------------------------------------------------------------------------------

def make_schedule(seed, arms=ARMS, repeats=REPEATS):
    """List of (repeat, arm); the order inside each repeat block is a seeded shuffle."""
    cells = []
    for r in range(1, repeats + 1):
        cells.extend((r, a) for a in sorted(arms, key=lambda x: cmp._order_key(seed, "overhead", r, x)))
    return cells


def probe_order(seed):
    return sorted(ARMS, key=lambda a: cmp._order_key(seed, "overhead-probe", a))


def sealed_files():
    """Label -> sha256 of every file the experiment depends on."""
    out = {"overhead:PROTOCOL.md": cmp.sha256_path(HERE / "PROTOCOL.md"), "overhead:run.py": cmp.sha256_path(HERE / "run.py"),
           "compare:run.py": cmp.sha256_path(COMPARE_DIR / "run.py"), "compare:PROTOCOL.md": cmp.sha256_path(COMPARE_DIR / "PROTOCOL.md")}
    for base, dirs, files in os.walk(str(FIXTURE)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = Path(base) / name
            out["lookup/" + full.relative_to(FIXTURE).as_posix()] = cmp.sha256_path(full)
    for rel in cmp.SEALED_REPO_FILES:
        out["repo:" + rel] = cmp.sha256_path(REPO / rel)
    return out


def resolve_commit(repo, ref):
    proc = cmp.git(["rev-parse", "--verify", "--quiet", str(ref) + "^{commit}"], repo)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Refused("not a commit of %s: %s" % (repo, ref))
    return proc.stdout.strip()


def export_commit(repo, ref, dest):
    """git archive of the named commit (evals/ left out) via compare's export_frozen, on a throw-away clone whose
    HEAD is that commit. Returns (full_sha, tree_sha256). The repository itself is only read."""
    sha = resolve_commit(repo, ref)
    scratch = Path(tempfile.mkdtemp(prefix="overhead-export-"))
    try:
        clone = scratch / "clone"
        proc = subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(repo), str(clone)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise Refused("git clone failed: " + proc.stderr.strip()[:300])
        proc = cmp.git(["update-ref", "--no-deref", "HEAD", sha], clone)
        if proc.returncode != 0:
            raise Refused("commit %s is not reachable from a branch of %s" % (sha, repo))
        commit, digest = cmp.export_frozen(clone, dest)
        if commit != sha:
            raise Refused("export is at %s, wanted %s" % (commit, sha))
        return sha, digest
    finally:
        cmp.make_tree_writable(scratch)
        shutil.rmtree(str(scratch), ignore_errors=True)


def cmd_seal(args):
    seed = args.seed if args.seed is not None else DEFAULT_SEED
    tmp = Path(tempfile.mkdtemp(prefix="overhead-seal-"))
    try:
        sha, digest = export_commit(args.plugin_repo, args.plugin_commit, tmp / "frozen")
    finally:
        cmp.make_tree_writable(tmp)
        shutil.rmtree(str(tmp), ignore_errors=True)
    data = {
        "version": 1, "seed": seed, "repeats": REPEATS, "run_cap_usd": RUN_CAP_USD, "probe_cap_usd": PROBE_CAP_USD,
        "budget_limit_usd": BUDGET_LIMIT_USD, "probe_order": probe_order(seed),
        "schedule": [list(c) for c in make_schedule(seed)],
        "plugin_commit": sha, "plugin_tree_sha256": digest, "files": sealed_files(),
    }
    with open(str(SEAL_PATH), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("sealed %d files, seed %d, plugin commit %s, export %s" % (len(data["files"]), seed, sha, digest))
    return 0


def load_seal():
    if not SEAL_PATH.is_file():
        raise Refused("no SEAL.json: run `run.py seal --plugin-commit SHA` after reviewing PROTOCOL.md")
    with open(str(SEAL_PATH), encoding="utf-8") as fh:
        return json.load(fh)


def seal_problems(seal):
    now = sealed_files()
    want = seal.get("files", {})
    problems = ["missing: " + p for p in sorted(set(want) - set(now))]
    problems += ["new file: " + p for p in sorted(set(now) - set(want))]
    problems += ["changed: " + p for p in sorted(set(want) & set(now)) if want[p] != now[p]]
    if [list(c) for c in make_schedule(seal.get("seed"))] != seal.get("schedule"):
        problems.append("schedule does not follow from the sealed seed")
    if probe_order(seal.get("seed")) != seal.get("probe_order"):
        problems.append("probe order does not follow from the sealed seed")
    return problems


# ---------------------------------------------------------------------------------------------
# ledger, commands
# ---------------------------------------------------------------------------------------------

class Ledger(cmp.Ledger):
    """compare's ledger with this experiment's limit."""

    def check(self, cap):
        spent = self.spent()
        if spent + cap > BUDGET_LIMIT_USD:
            raise BudgetStop("spent %.2f + cap %.2f would exceed %.2f" % (spent, cap, BUDGET_LIMIT_USD))


def build_command(claude, arm, prompt, plugin_dir, cap):
    """compare's off command, then the plugin directory and, for orchestrate, the prompt prefix."""
    if arm not in ARMS:
        raise ValueError("unknown arm " + str(arm))
    cmd = cmp.build_command(claude, "off", "lookup", prompt, None, cap)
    if arm == "orchestrate":
        cmd[2] = cmp.PROMPT_PREFIX + prompt
    if arm in PLUGIN_ARMS:
        cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


def arm_parity_problems(plugin_dir="/frozen"):
    """The arms must differ only by --plugin-dir and the prompt prefix."""
    off = build_command("claude", "off", "PROMPT", plugin_dir, RUN_CAP_USD)
    problems = []
    for arm in PLUGIN_ARMS:
        cmd = build_command("claude", arm, "PROMPT", plugin_dir, RUN_CAP_USD)
        i = cmd.index("--plugin-dir")
        stripped = cmd[:i] + cmd[i + 2:]
        stripped[2] = stripped[2].replace(cmp.PROMPT_PREFIX, "", 1)
        if stripped != off:
            problems.append("%s differs from off beyond --plugin-dir and the prompt prefix" % arm)
    return problems


class Context:
    def __init__(self, args, seal):
        self.claude = args.claude
        self.work_root = Path(os.path.abspath(args.work_root))
        self.out = Path(os.path.abspath(args.out))
        self.keep = args.keep
        self.timeout = args.timeout
        self.ledger = Ledger(self.out, args.spent_before)
        self.seal = seal
        self.plugin_dir = None


def setup(args):
    seal = load_seal()
    problems = seal_problems(seal)
    if problems:
        raise Refused("files differ from SEAL.json:\n  " + "\n  ".join(problems[:20]))
    if args.seed is not None and args.seed != seal["seed"]:
        raise Refused("--seed %d differs from the sealed seed %d" % (args.seed, seal["seed"]))
    problems = arm_parity_problems()
    if problems:
        raise Refused("arm parity check failed: " + "; ".join(problems))
    work_root = Path(os.path.abspath(args.work_root))
    out = Path(os.path.abspath(args.out))
    if "/worktrees/" in (os.path.realpath(str(work_root)) + "/"):
        raise Refused("work root must not contain /worktrees/")
    if shutil.which(args.claude) is None:
        raise Refused("claude executable not found: %s" % args.claude)
    work_root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    ctx = Context(args, seal)
    sha, digest = export_commit(args.plugin_repo, seal["plugin_commit"], work_root / "_frozen" / "plugin")
    if sha != seal["plugin_commit"] or digest != seal["plugin_tree_sha256"]:
        raise Refused("plugin export (commit %s, sha256 %s) differs from the seal (commit %s, sha256 %s)"
                      % (sha, digest, seal["plugin_commit"], seal["plugin_tree_sha256"]))
    ctx.plugin_dir = str(work_root / "_frozen" / "plugin")
    ctx.frozen = {"commit": sha, "tree_sha256": digest}
    with open(str(out / "frozen.json"), "w", encoding="utf-8") as fh:
        json.dump(ctx.frozen, fh, indent=1)
    probe = work_root / "_preflight"
    if probe.exists():
        cmp.remove_inside(probe, work_root)
    scaffold(probe)
    cmp.remove_inside(probe, work_root)
    return ctx


def scaffold(workdir):
    proc = subprocess.run([sys.executable, "-B", str(FIXTURE / "scaffold.py"), str(workdir)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
    if proc.returncode != 0:
        raise Refused("scaffold for lookup failed (exit %d): %s" % (proc.returncode, proc.stderr.strip()[:600]))


class RunLock:
    """One runner per output directory (POSIX flock)."""

    def __init__(self, out):
        self.path = Path(out) / ".lock"
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(str(self.path), "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise Refused("another runner holds %s" % self.path)
        return self

    def __exit__(self, *exc):
        self.fh.close()


# ---------------------------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------------------------

def execute(ctx, kind, arm, cell_id, cell_dir, cap, scored):
    """One run: workdir, claude, collect, cost, grade (scored only), ledger, clean up. Returns the result dict."""
    workdir = ctx.work_root / cell_id
    ctx.ledger.check(cap)
    if workdir.exists():
        cmp.remove_inside(workdir, ctx.work_root)
    if scored:
        scaffold(workdir)
    else:
        workdir.mkdir(parents=True)
    if arm in PLUGIN_ARMS and cmp.tree_hash(ctx.plugin_dir) != ctx.frozen["tree_sha256"]:
        raise Refused("the frozen plugin tree changed during the experiment")
    cell_dir = Path(cell_dir)
    cell_dir.mkdir(parents=True)
    prompt = cmp.read_prompt("lookup") if scored else PROBE_PROMPT
    cmd = build_command(ctx.claude, arm, prompt, ctx.plugin_dir, cap)
    with open(str(cell_dir / "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": cell_id, "kind": kind, "arm": arm, "cap": cap, "workdir": str(workdir), "argv_without_prompt": [c for c in cmd if c != cmd[2]]}, fh, indent=1)
    ctx.ledger.start(cell_id, kind, cap)
    code, timed_out, wall = cmp.run_claude(cmd, workdir, cell_dir / "stream.jsonl", cell_dir / "stderr.log", ctx.timeout)
    events = cmp.read_events(cell_dir / "stream.jsonl")
    info = cmp.analyse_stream(events)
    calls = extract_calls(events)
    with open(str(cell_dir / "calls.jsonl"), "w", encoding="utf-8") as fh:
        for c in calls:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    tcost, terr = cmp.transcript_cost(info["session_id"], workdir, info["transcript_hint"])
    proj = cmp.project_dir_for(workdir, info["session_id"], info["transcript_hint"])
    leaks = cmp.leak_scan(info["tool_blocks"], workdir, proj / str(info["session_id"]) / "subagents" if info["session_id"] else None)
    grade_exit = None
    if scored:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        grade = subprocess.run([sys.executable, "-B", str(FIXTURE / "grade.py"), str(workdir)], cwd=str(cell_dir), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        grade_exit = grade.returncode
        with open(str(cell_dir / "grade.txt"), "w", encoding="utf-8") as fh:
            fh.write(grade.stdout[-4000:])
    known = [c for c in (info["cli_cost"], info["max_cli_cost"], (tcost or {}).get("total_usd")) if c is not None]
    charged = max(known) if known else float(cap)
    capped = any("budget" in str(s) for s in info["subtypes"]) or charged >= cap
    if capped or timed_out:
        charged = max(charged, float(cap))
    graded_ok = (grade_exit == 0) if scored else (code == 0 and bool(info["results"]))
    success = graded_ok and not capped and not timed_out and not leaks
    state, state_ok, state_detail = arm_state(info["init"], arm)
    tool_names = {}
    for c in calls:
        for name in c["tools"]:
            tool_names[name] = tool_names.get(name, 0) + 1
    skills = [str((b.get("input") or {}).get("skill") or (b.get("input") or {}).get("name") or "?") for b in info["tool_blocks"] if b.get("name") == "Skill"]
    sid = info["session_id"]
    result = {
        "id": cell_id, "kind": kind, "arm": arm, "cap": cap, "scored": scored,
        "session_id": sid, "transcript_main": str(proj / (str(sid) + ".jsonl")) if sid else None,
        "exit_code": code, "timed_out": timed_out, "capped": capped, "wall_s": round(wall, 2),
        "grade_exit": grade_exit, "success": success,
        "cli_cost": info["cli_cost"], "transcript_cost": (tcost or {}).get("total_usd"), "cost_report_error": terr,
        "charged": charged, "cost": float(cap) if (capped or timed_out) else charged,
        "peak_context_tokens": info["peak_context_tokens"], "tool_calls": info["tool_calls"], "tool_names": tool_names,
        "agent_calls": info["agent_calls"], "skill_calls": skills, "n_api_calls": len(calls),
        "n_primary_api_calls": len([c for c in calls if not c["sub"]]),
        "permission_denials": len(info["denials"]), "permission_denial_reasons": [cmp.denial_reason(d) for d in info["denials"]],
        "leak_suspect": bool(leaks), "leak_hits": leaks[:5],
        "arm_state": state, "arm_state_ok": state_ok, "arm_state_detail": state_detail,
        "result_subtypes": info["subtypes"],
    }
    with open(str(cell_dir / "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
    ctx.ledger.end(cell_id, kind, charged)
    if sid:
        with open(str(ctx.out / "sessions.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (sid, cell_id, proj))
    if not ctx.keep:
        cmp.remove_inside(workdir, ctx.work_root)
    if not state_ok:
        raise ArmStateError("%s was not in its arm (%s): %s" % (cell_id, arm, state_detail))
    return result


def stop_file(ctx, reason, detail):
    with open(str(ctx.out / "STOPPED.json"), "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "detail": str(detail), "spent": ctx.ledger.spent()}, fh, indent=1)


def probe_dir(ctx, arm):
    return ctx.out / "probe" / arm


def cell_dir_of(ctx, cell):
    r, a = cell
    return ctx.out / "runs" / ("%s__r%d" % (a, r))


def guarded(ctx, fn):
    try:
        return fn()
    except BudgetStop as exc:
        stop_file(ctx, "budget", exc)
        print("stopped cleanly: " + str(exc))
        return 4
    except ArmStateError as exc:
        stop_file(ctx, "arm_state", exc)
        print("stopped: " + str(exc), file=sys.stderr)
        return 3


def cmd_probe(args):
    ctx = setup(args)
    with RunLock(ctx.out):
        order = ctx.seal["probe_order"]
        for arm in order:
            d = probe_dir(ctx, arm)
            if d.exists() and not (d / "result.json").is_file():
                raise Refused("a previous probe run was interrupted and left %s without a result; it was charged at its cap and cannot be redone" % d)

        def go():
            for arm in order:
                if (probe_dir(ctx, arm) / "result.json").is_file():
                    continue
                res = execute(ctx, "probe", arm, "probe__" + arm, probe_dir(ctx, arm), PROBE_CAP_USD, False)
                calls = read_calls(probe_dir(ctx, arm))
                print("[probe %s] cost=%.4f first-call prefix=%s tokens (not scored)" % (arm, res["charged"], first_prefix(calls)))
            return 0
        return guarded(ctx, go)


def read_calls(cell_dir):
    path = Path(cell_dir) / "calls.jsonl"
    if not path.is_file():
        return []
    return cmp.read_events(path)


def require_probe(ctx):
    for arm in ARMS:
        p = probe_dir(ctx, arm) / "result.json"
        if not p.is_file():
            raise Refused("probe for %s is missing: run `run.py probe` first" % arm)
        with open(str(p), encoding="utf-8") as fh:
            if not json.load(fh).get("arm_state_ok"):
                raise Refused("the probe for %s was not in its arm; the experiment is void" % arm)


def cmd_run(args):
    ctx = setup(args)
    cells = [tuple(c) for c in ctx.seal["schedule"]]
    require_probe(ctx)
    with RunLock(ctx.out):
        existing = [cell_dir_of(ctx, c) for c in cells if cell_dir_of(ctx, c).exists()]
        if existing and not args.resume:
            raise Refused("results already exist for %d cell(s), no top-up runs (use --resume to continue unrun cells only): %s" % (len(existing), existing[0]))
        interrupted = [d for d in existing if not (d / "result.json").is_file()]
        for d in interrupted:
            print("note: %s was interrupted, stays charged at its cap and stays empty" % d.name)
        ran = [0]

        def go():
            for i, cell in enumerate(cells, 1):
                d = cell_dir_of(ctx, cell)
                if d.exists():
                    continue
                if args.max_runs is not None and ran[0] >= args.max_runs:
                    print("stopped after --max-runs %d" % ran[0])
                    return 0
                r, a = cell
                res = execute(ctx, "scored", a, "%s__r%d" % (a, r), d, RUN_CAP_USD, True)
                ran[0] += 1
                print("[%d/%d] %s r%d: %s cost=%.4f wall=%.0fs" % (i, len(cells), a, r, "success" if res["success"] else "FAILURE", res["cost"], res["wall_s"]))
            print("schedule complete (seed %d), spent %.2f" % (ctx.seal["seed"], ctx.ledger.spent()))
            return 0
        return guarded(ctx, go)


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------

def load_cell(cell_dir):
    with open(str(Path(cell_dir) / "result.json"), encoding="utf-8") as fh:
        result = json.load(fh)
    return result, read_calls(cell_dir)


def med(values):
    values = [Fraction(v) for v in values]
    return median(values) if values else None


def arm_stats(records):
    """records: list of (result, calls) of one arm."""
    classes = [usd_by_class(c) for _, c in records]
    tokens = [{k: sum(int(x.get(k) or 0) for x in c) for k in TOKEN_CLASSES} for _, c in records]
    return {
        "n": len(records),
        "successes": sum(1 for r, _ in records if r["success"]),
        "runs": [(bool(r["success"]), r["cost"]) for r, _ in records],
        "median_cost": med([_frac(r["cost"]) for r, _ in records]),
        "median_peak_context": med([r["peak_context_tokens"] for r, _ in records]),
        "median_primary_calls": med([len([x for x in c if not x["sub"]]) for _, c in records]),
        "median_api_calls": med([len(c) for _, c in records]),
        "tool_calls": [r["tool_calls"] for r, _ in records],
        "agent_calls": [r["agent_calls"] for r, _ in records],
        "skill_calls": [len(r["skill_calls"]) for r, _ in records],
        "class_usd": {k: med([x[k] for x in classes]) for k in TOKEN_CLASSES} if records else {k: None for k in TOKEN_CLASSES},
        "class_tokens": {k: med([t[k] for t in tokens]) for k in TOKEN_CLASSES} if records else {k: None for k in TOKEN_CLASSES},
        "sum_class_usd": med([sum(x.values()) for x in classes]),
        "leak_suspects": [r["id"] for r, _ in records if r["leak_suspect"]],
        "denials": [r["permission_denials"] for r, _ in records],
        "ids": [r["id"] for r, _ in records],
    }


def build_report(out):
    out = Path(out)
    by_arm = {a: [] for a in ARMS}
    for p in sorted((out / "runs").glob("*/result.json")) if (out / "runs").is_dir() else []:
        result, calls = load_cell(p.parent)
        if result.get("scored") and result["arm"] in by_arm:
            by_arm[result["arm"]].append((result, calls))
    stats = {a: arm_stats(by_arm[a]) for a in ARMS}
    probes = {}
    for a in ARMS:
        p = probe_dir_of(out, a)
        if (p / "result.json").is_file():
            result, calls = load_cell(p)
            probes[a] = {"prefix": first_prefix(calls), "model": next((c["model"] for c in calls if not c["sub"]), None), "cost": result["cost"]}
    static_usd, output_usd = {}, {"off": stats["off"]["class_usd"]["output"]}
    model = (probes.get("off") or {}).get("model") or "opus"
    _, p_cw, p_cr, _ = price_of(model, False)
    for a in PLUGIN_ARMS:
        output_usd[a] = stats[a]["class_usd"]["output"]
        pa, po = (probes.get(a) or {}).get("prefix"), (probes.get("off") or {}).get("prefix")
        k = stats[a]["median_primary_calls"]
        static_usd[a] = None if pa is None or po is None or k is None else static_cost(max(0, pa - po), int(k), p_cw, p_cr)
    decision = decide({a: stats[a]["runs"] for a in ARMS}, static_usd, output_usd)
    return {"stats": stats, "probes": probes, "static_usd": static_usd, "decision": decision}


def probe_dir_of(out, arm):
    return Path(out) / "probe" / arm


def _f(x, nd=4):
    return "n/a" if x is None else ("%.*f" % (nd, float(x)))


def _pct(x):
    return "n/a" if x is None else "%.1f%%" % float(x)


def render_report(rep):
    st, dec = rep["stats"], rep["decision"]
    lines = ["== lookup, three arms, n=3 each (short task, one machine, opus medium) =="]
    for a in ARMS:
        s = st[a]
        costs = ", ".join("%.4f" % c for _, c in s["runs"]) or "none"
        lines.append("  %-11s successes %d/%d  median cost %s  costs [%s]  median peak context %s  median API calls %s" % (
            a, s["successes"], s["n"], _f(s["median_cost"]), costs, _f(s["median_peak_context"], 0), _f(s["median_api_calls"], 1)))
        lines.append("              Skill calls per run %s, Agent calls per run %s, tool calls per run %s, denials %s, leak suspects: %s" % (
            s["skill_calls"], s["agent_calls"], s["tool_calls"], s["denials"], ", ".join(s["leak_suspects"]) or "none"))
    lines.append("")
    lines.append("USD by token class (median over runs; tokens x list price), then arm minus off:")
    lines.append("  %-11s %9s %9s %9s %9s %9s %9s" % ("arm", "input", "output", "cache_rd", "cache_wr", "sum", "effective"))
    for a in ARMS:
        s = st[a]
        lines.append("  %-11s %9s %9s %9s %9s %9s %9s" % (a, *[_f(s["class_usd"][k]) for k in TOKEN_CLASSES], _f(s["sum_class_usd"]), _f(s["median_cost"])))
    for a in PLUGIN_ARMS:
        s, o = st[a], st["off"]
        cells = []
        for k in TOKEN_CLASSES:
            cells.append("n/a" if s["class_usd"][k] is None or o["class_usd"][k] is None else "%+.4f" % float(s["class_usd"][k] - o["class_usd"][k]))
        eff = "n/a" if s["median_cost"] is None or o["median_cost"] is None else "%+.4f" % float(s["median_cost"] - o["median_cost"])
        lines.append("  %-11s %9s %9s %9s %9s %9s %9s   (minus off)" % (a, *cells, "", eff))
    lines.append("")
    lines.append("Static probe, first primary call prefix (input + cache read + cache write tokens):")
    for a in ARMS:
        p = rep["probes"].get(a)
        lines.append("  %-11s %s tokens%s" % (a, "n/a" if not p or p["prefix"] is None else p["prefix"], "" if a == "off" or not p or not rep["probes"].get("off") or p["prefix"] is None or rep["probes"]["off"]["prefix"] is None
                                              else "  (%+d vs off)" % (p["prefix"] - rep["probes"]["off"]["prefix"])))
    lines.append("")
    for a in PLUGIN_ARMS:
        r = dec["arms"][a]
        lines.append("%s vs off: compare label %s, median difference %s USD (%s), DIFFERS %s, static share %s, output share %s" % (
            a, r["label"], _f(r["median_diff"]), _pct(r["pct"]), "yes" if r["differs"] else "no",
            _pct(None if r["static_share"] is None else r["static_share"] * 100), _pct(None if r["output_share"] is None else r["output_share"] * 100)))
    lines.append("")
    lines.append("outcome: %s (%s)" % (dec["outcome"], dec["reason"]))
    lines.append("Whatever the outcome, the inference that the extra cost 'comes from loading the skill and hook context' is corrected to what was measured above.")
    lines.append("Scope: short task, one machine, opus medium, n=3; this explains overhead, not long-session value.")
    return "\n".join(lines)


def jsonable(x):
    if isinstance(x, Fraction):
        return float(x)
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def cmd_report(args):
    out = Path(os.path.abspath(args.out))
    if not out.is_dir():
        raise Refused("no such output directory: " + str(out))
    rep = build_report(out)
    text = render_report(rep)
    print(text)
    with open(str(out / "report.json"), "w", encoding="utf-8") as fh:
        json.dump(jsonable(rep), fh, indent=1, sort_keys=True)
    with open(str(out / "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("ledger total (without --spent-before): %.2f" % Ledger(out, 0).spent())
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Overhead experiment (see PROTOCOL.md)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("seal")
    p.add_argument("--plugin-commit", required=True)
    p.add_argument("--plugin-repo", default=str(REPO))
    p.add_argument("--seed", type=int)
    for name in ("probe", "run"):
        p = sub.add_parser(name)
        p.add_argument("--work-root", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--spent-before", type=float, default=0.0)
        p.add_argument("--keep", action="store_true")
        p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
        p.add_argument("--claude", default="claude")
        p.add_argument("--plugin-repo", default=str(REPO))
        p.add_argument("--seed", type=int, help="must equal the sealed seed if given")
        if name == "run":
            p.add_argument("--resume", action="store_true")
            p.add_argument("--max-runs", type=int)
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"seal": cmd_seal, "probe": cmd_probe, "run": cmd_run, "report": cmd_report}[args.command](args)
    except Refused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
