#!/usr/bin/env python3
"""Long-session calibration: does delegating reads and test runs protect the primary's context, and at what price?

    run.py g0        --plugin-commit SHA --work-root D --out D [--cap-usd 1]
    run.py calibrate --arm off|delegate --plugin-commit SHA --work-root D --out D [--stages N] [--stage-cap 4]
                     [--run-cap 15] [--spent-before X] [--total-cap 20] [--resume-run] [--cost-mode per_stage|cumulative]
    run.py report    --out D

CALIBRATION ONLY. Nothing here is scored and nothing here decides anything: the output sizes the fixture and says
what the stages cost and how the context grew. Whether a sealed experiment is worth running is the reader's call.
See PROTOCOL.md. Python 3.8, standard library only.

It reuses helpers from ../compare/run.py, loaded by path (that file is never edited, and importing it must not leave
bytecode in evals/compare, hence the flag below).

Exit codes: 0 ok, 2 refused, 3 a stage was not in its intended arm, 4 stopped before a stage by a cap (partial),
5 a stage went over its cap, 6 infrastructure failure (resumable once with --resume-run).
"""

import sys

sys.dont_write_bytecode = True  # before importing compare/run.py: its tamper check trips on bytecode

import argparse
import contextlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
COMPARE_DIR = HERE.parent / "compare"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cmp = _load("compare_run_for_long", COMPARE_DIR / "run.py")
stages = _load("long_stages", HERE / "stages.py")
Refused = cmp.Refused
BudgetStop = cmp.BudgetStop

ARMS = ("off", "delegate")
STAGE_CAP_USD = 4.0
RUN_CAP_USD = 15.0
TOTAL_CAP_USD = 20.0
G0_CAP_USD = 1.0
DEFAULT_TIMEOUT_S = 3600
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write")
DISPLACEABLE_TOOLS = ("Read", "Bash", "Grep", "Glob", "WebFetch", "WebSearch")
PLUGIN_NAME = "mitosis-orchestrator"
WORKER_AGENT = "mitosis-orchestrator:flash-worker"
SCOUT_AGENT = "mitosis-orchestrator:flash-lite-scout"
COST_MODES = ("per_stage", "cumulative")
CALIBRATION_NOTICE = "Calibration only: unscored, no decision rule applied. This sizes the fixture; it is not a result about the plugin."
LIVE_STATUSES = ("running", "infra_failed")
BACKGROUND_MARKER = re.compile(r"async|background|launched|running in the", re.I)


class ArmStateError(Exception):
    """A stage was not in the arm it was meant to be in."""


_COST_REPORT = []


def cost_report():
    if not _COST_REPORT:
        _COST_REPORT.append(_load("cost_report_for_long", REPO / "scripts" / "mitosis-cost-report.py"))
    return _COST_REPORT[0]


# ---------------------------------------------------------------------------------------------
# prices, calls, context. Pure functions.
# ---------------------------------------------------------------------------------------------

def _frac(value):
    return Fraction(str(value))


def price_of(model, sub=False):
    """(input, cache_write, cache_read, output) in USD per million tokens, as Fractions. Primary calls use the 1-hour
    cache-write rate and subagent calls the 5-minute rate, as scripts/mitosis-cost-report.py does. An unpriced model is priced as `opus`."""
    cr = cost_report()
    row = cr.price_for(str(model or ""), cr.DEFAULT_PRICES) or cr.price_for("opus", cr.DEFAULT_PRICES)
    p_in, p_cw, p_cr, p_out = [_frac(x) for x in row]
    if not sub:
        p_cw = _frac(cr.CACHE_1H_WRITE_MULTIPLIER) * p_in
    return p_in, p_cw, p_cr, p_out


def usd_by_class(calls):
    out = {k: Fraction(0) for k in TOKEN_CLASSES}
    for c in calls:
        p_in, p_cw, p_cr, p_out = price_of(c.get("model"), bool(c.get("sub")))
        out["input"] += int(c.get("input") or 0) * p_in / 1000000
        out["output"] += int(c.get("output") or 0) * p_out / 1000000
        out["cache_read"] += int(c.get("cache_read") or 0) * p_cr / 1000000
        out["cache_write"] += int(c.get("cache_write") or 0) * p_cw / 1000000
    return out


def extract_calls(events):
    """One record per API call, deduplicated by message.id (the stream repeats it once per content block). Token counts are the
    maximum seen for that id; tool names are listed once per tool_use id. No content is kept."""
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


def context_of(call):
    return int(call.get("input") or 0) + int(call.get("cache_read") or 0) + int(call.get("cache_write") or 0)


def primary_contexts(calls):
    return [context_of(c) for c in calls if not c.get("sub")]


def growth(contexts, prev=None):
    """Cumulative context growth G: the sum of the positive differences between successive primary calls. A compaction
    reset shows up as one negative difference, which is skipped, so growth after the reset is counted from the lower level.
    `prev` is the last context of the stage before, so a stage boundary is not a gap."""
    total, last = 0, prev
    for c in contexts:
        if last is not None and c > last:
            total += c - last
        last = c
    return total


_CJK = re.compile("[⺀-鿿가-힯豈-﫿＀-￯]")


def est_tokens(text):
    """CJK-aware estimate: 1.5 tokens per CJK character, one token per four other characters. A reported number only."""
    text = text or ""
    cjk = len(_CJK.findall(text))
    return int(math.ceil((len(text) - cjk) / 4.0 + cjk * 1.5))


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def tool_output_stats(events):
    """Estimated tokens of the primary's own tool results: displaceable ones by tool, and the reports returned by Agent calls."""
    names = {}
    for e in events:
        if e.get("type") == "assistant" and not e.get("parent_tool_use_id") and isinstance(e.get("message"), dict):
            for b in e["message"].get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    names[b["id"]] = b.get("name")
    disp, reports, seen = {}, 0, set()
    for e in events:
        if e.get("type") != "user" or e.get("parent_tool_use_id") or not isinstance(e.get("message"), dict):
            continue
        content = e["message"].get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_result") or b.get("tool_use_id") in seen:
                continue
            seen.add(b.get("tool_use_id"))
            name = names.get(b.get("tool_use_id"))
            size = est_tokens(_result_text(b.get("content")))
            if name in DISPLACEABLE_TOOLS:
                disp[name] = disp.get(name, 0) + size
            elif name in cmp.AGENT_TOOL_NAMES:
                reports += size
    return {"displaceable_by_tool": disp, "displaceable_tokens": sum(disp.values()), "report_tokens": reports}


def compaction_events(events):
    """Compaction boundaries the stream shows: trigger and preTokens, nothing else."""
    out = []
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            meta = e.get("compact_metadata") or e.get("compactMetadata") or {}
            out.append({"trigger": meta.get("trigger"), "pre_tokens": meta.get("pre_tokens", meta.get("preTokens"))})
    return out


def transcript_compactions(path):
    """Number of compaction boundaries in a main transcript file (cumulative over the session), or None if it cannot be read."""
    if not path or not Path(path).is_file():
        return None
    n = 0
    for rec in cmp.read_events(path):
        if rec.get("subtype") == "compact_boundary" or rec.get("compactMetadata") or rec.get("compact_metadata"):
            n += 1
    return n


def transcript_ids(path):
    if not path or not Path(path).is_file():
        return set()
    return {(r.get("message") or {}).get("id") for r in cmp.read_events(path)
            if r.get("type") == "assistant" and isinstance(r.get("message"), dict) and (r.get("message") or {}).get("id")}


def agent_evidence(events):
    """How Agent calls came back in -p mode: counts and booleans only, no content."""
    ids = []
    for e in events:
        if e.get("type") == "assistant" and not e.get("parent_tool_use_id") and isinstance(e.get("message"), dict):
            for b in e["message"].get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in cmp.AGENT_TOOL_NAMES and b.get("id") not in ids:
                    ids.append(b.get("id"))
    result_at = next((i for i, e in enumerate(events) if e.get("type") == "result"), None)
    returned, before_result, background, sub_events = 0, 0, 0, 0
    for i, e in enumerate(events):
        if e.get("parent_tool_use_id") in ids:
            sub_events += 1
        if e.get("type") == "user" and isinstance(e.get("message"), dict) and isinstance(e["message"].get("content"), list):
            for b in e["message"]["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                    returned += 1
                    if result_at is None or i < result_at:
                        before_result += 1
                    if BACKGROUND_MARKER.search(_result_text(b.get("content"))[:300]):
                        background += 1
    return {"agent_calls": len(ids), "returned": returned, "returned_before_result": before_result, "background_marked": background,
            "subagent_events": sub_events}


# ---------------------------------------------------------------------------------------------
# cost handling. Pure functions.
# ---------------------------------------------------------------------------------------------

def cli_stage_cost(mode, total, prev_total):
    """The CLI's total_cost_usd of a stage. If the field is cumulative over the session, the stage is the difference; if it is per stage
    (or not yet known), the stage is the field itself. A fall below the earlier total is clamped to zero."""
    if total is None:
        return None
    if mode == "cumulative":
        return max(0.0, float(total) - float(prev_total or 0.0))
    return float(total)


def transcript_stage_cost(total, prev_total, cumulative):
    """A transcript total for the session of this stage. If the stage's transcript holds the stages before it (same session id, or message
    ids shared with the stage before), the earlier total is taken off, otherwise it would be counted twice."""
    if total is None:
        return None
    if cumulative and prev_total is not None:
        return max(0.0, float(total) - float(prev_total))
    return float(total)


def effective_cost(cli_stage, transcript_stage, cap):
    known = [c for c in (cli_stage, transcript_stage) if c is not None]
    return max(known) if known else float(cap)


def decide_cli_mode(cli1, cli2, stage2_transcript):
    """From two consecutive stages of one session: 'per_stage', 'cumulative' or None when the data cannot tell."""
    if cli1 is None or cli2 is None:
        return None
    if cli2 < cli1 - 1e-9:
        return "per_stage"
    if stage2_transcript is None:
        return None
    per, cum = float(stage2_transcript), float(cli1) + float(stage2_transcript)
    if abs(cum - per) < 0.15 * max(cum, 1e-9):
        return None
    return "per_stage" if abs(cli2 - per) < abs(cli2 - cum) else "cumulative"


# ---------------------------------------------------------------------------------------------
# arm state and command
# ---------------------------------------------------------------------------------------------

def arm_state(init, arm):
    """The plugin facts the init event shows, and whether they match the arm. Returns (state, ok, detail)."""
    if init is None:
        return {"plugin_loaded": None, "agents_loaded": None, "scout_loaded": None, "skill_loaded": None}, False, "no init event in the stream"
    plugins, agents = cmp._names(init.get("plugins")), cmp._names(init.get("agents"))
    skills = cmp._names(init.get("skills")) | cmp._names(init.get("slash_commands"))
    state = {
        "plugin_loaded": any(n == PLUGIN_NAME or n.startswith(PLUGIN_NAME + "@") for n in plugins),
        "agents_loaded": WORKER_AGENT in agents,
        "scout_loaded": SCOUT_AGENT in agents,
        "skill_loaded": any(cmp._is_mitosis(n) and "orchestrate" in n for n in skills),
    }
    anywhere = [n for n in plugins | agents | skills if cmp._is_mitosis(n)]
    if arm == "delegate":
        missing = [k for k in ("plugin_loaded", "agents_loaded", "scout_loaded") if not state[k]]
        return state, not missing, ("ok" if not missing else "expected loaded, missing: " + ", ".join(missing))
    bad = [k for k, v in state.items() if v]
    if anywhere and not bad:
        bad = ["mitosis item " + anywhere[0][:60]]
    return state, not bad, ("ok" if not bad else "expected no plugin, found: " + ", ".join(bad))


def build_command(claude, arm, prompt, plugin_dir, cap, resume=None):
    """Compare's flags for the off arm; the delegate arm adds only --plugin-dir. Stage 2 onwards add --resume."""
    if arm not in ARMS:
        raise ValueError("unknown arm " + str(arm))
    cmd = [
        claude, "-p", prompt,
        "--model", "opus", "--effort", "medium",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(stages.allowed_tools()),
        "--max-budget-usd", "%g" % cap,
        "--output-format", "stream-json", "--verbose",
        "--settings", cmp.SETTINGS_JSON,
        "--strict-mcp-config",
    ]
    if resume:
        cmd += ["--resume", str(resume)]
    if arm == "delegate":
        cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


# ---------------------------------------------------------------------------------------------
# ledger, lock, export
# ---------------------------------------------------------------------------------------------

class Ledger(cmp.Ledger):
    """compare's ledger with this experiment's total cap. A started stage is charged at its cap until its end line replaces that."""

    def __init__(self, out, spent_before, total_cap=TOTAL_CAP_USD):
        super().__init__(out, spent_before)
        self.total_cap = float(total_cap)

    def last(self):
        last = {}
        if self.path.is_file():
            with open(str(self.path), encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        e = json.loads(line)
                        last[e["id"]] = e["charged"]
        return last

    def spent_run(self, prefix):
        return sum(v for k, v in self.last().items() if k.startswith(prefix))

    def check(self, cap):
        spent = self.spent()
        if spent + cap > self.total_cap:
            raise BudgetStop("spent %.2f + cap %.2f would exceed the total cap %.2f" % (spent, cap, self.total_cap))


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


def resolve_commit(repo, ref):
    proc = cmp.git(["rev-parse", "--verify", "--quiet", str(ref) + "^{commit}"], repo)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Refused("not a commit of %s: %s" % (repo, ref))
    return proc.stdout.strip()


def export_commit(repo, ref, dest):
    """git archive of the named commit with evals/ left out (compare's export_frozen on a throw-away clone whose HEAD is that commit).
    Returns (full_sha, tree_sha256). The repository itself is only read."""
    sha = resolve_commit(repo, ref)
    scratch = Path(tempfile.mkdtemp(prefix="long-export-"))
    try:
        clone = scratch / "clone"
        proc = subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(repo), str(clone)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
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


@contextlib.contextmanager
def patched_env(extra):
    old = {k: os.environ.get(k) for k in extra}
    os.environ.update({k: str(v) for k, v in extra.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def write_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(path))


def read_json(path):
    with open(str(path), encoding="utf-8") as fh:
        return json.load(fh)


class Context:
    def __init__(self, args):
        self.claude = args.claude
        self.work_root = Path(os.path.abspath(args.work_root))
        self.out = Path(os.path.abspath(args.out))
        self.keep = getattr(args, "keep", False)
        self.timeout = args.timeout
        self.ledger = Ledger(self.out, getattr(args, "spent_before", 0.0), getattr(args, "total_cap", TOTAL_CAP_USD))
        self.plugin_repo = getattr(args, "plugin_repo", str(REPO))
        self.plugin_commit = None
        self.plugin_dir = None
        self.frozen = None


def setup(args, arms):
    work_root, out = Path(os.path.abspath(args.work_root)), Path(os.path.abspath(args.out))
    if "/worktrees/" in (os.path.realpath(str(work_root)) + "/"):
        raise Refused("work root must not contain /worktrees/")
    if shutil.which(args.claude) is None:
        raise Refused("claude executable not found: %s" % args.claude)
    ctx = Context(args)
    ctx.plugin_commit = resolve_commit(ctx.plugin_repo, args.plugin_commit)
    work_root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    if "delegate" in arms:
        dest = work_root / "_frozen" / ctx.plugin_commit[:12]
        sha, digest = export_commit(ctx.plugin_repo, ctx.plugin_commit, dest)
        ctx.plugin_dir, ctx.frozen = str(dest), {"commit": sha, "tree_sha256": digest}
        write_json(out / "frozen.json", ctx.frozen)
    return ctx


# ---------------------------------------------------------------------------------------------
# one stage
# ---------------------------------------------------------------------------------------------

def execute_stage(ctx, spec):
    """One `claude -p` call: ledger, run, collect, cost, grade. spec keys: kind, arm, n, prompt, cap, ws, stage_dir, ledger_id,
    resume (session id or None), prev (the previous stage's result or None), grade (callable ws -> dict, or None), env (dict)."""
    arm, n, cap, ws = spec["arm"], spec["n"], spec["cap"], Path(spec["ws"])
    stage_dir = Path(spec["stage_dir"])
    ctx.ledger.check(cap)
    if arm == "delegate" and cmp.tree_hash(ctx.plugin_dir) != ctx.frozen["tree_sha256"]:
        raise Refused("the frozen plugin tree changed during the run")
    stage_dir.mkdir(parents=True)
    cmd = build_command(ctx.claude, arm, spec["prompt"], ctx.plugin_dir, cap, spec.get("resume"))
    write_json(stage_dir / "meta.json", {"id": spec["ledger_id"], "kind": spec["kind"], "arm": arm, "stage": n, "cap": cap, "resume": bool(spec.get("resume")),
                                         "argv_without_prompt": [c for i, c in enumerate(cmd) if i != 2]})
    ctx.ledger.start(spec["ledger_id"], spec["kind"], cap)
    with patched_env(spec.get("env") or {}):
        code, timed_out, wall = cmp.run_claude(cmd, ws, stage_dir / "stream.jsonl", stage_dir / "stderr.log", ctx.timeout)
    events = cmp.read_events(stage_dir / "stream.jsonl")
    info = cmp.analyse_stream(events)
    calls = extract_calls(events)
    with open(str(stage_dir / "calls.jsonl"), "w", encoding="utf-8") as fh:
        for c in calls:
            fh.write(json.dumps(c, sort_keys=True) + "\n")
    prev = spec.get("prev") or {}
    sid = info["session_id"]
    proj = cmp.project_dir_for(ws, sid, info["transcript_hint"])
    tcost, terr = cmp.transcript_cost(sid, ws, info["transcript_hint"])
    t_total = (tcost or {}).get("total_usd")
    main_path = proj / (str(sid) + ".jsonl") if sid else None
    prev_sid = prev.get("session_id")
    if prev_sid is None:
        cumulative = False
    elif prev_sid == sid:
        cumulative = True
    else:
        cumulative = bool(transcript_ids(main_path) & transcript_ids(proj / (str(prev_sid) + ".jsonl")))
    t_stage = transcript_stage_cost(t_total, prev.get("transcript_total"), cumulative)
    mode = spec.get("cost_mode")
    c_stage = cli_stage_cost(mode, info["cli_cost"], prev.get("cli_total"))
    if mode is None and t_stage is not None:
        c_stage = None  # g0 only: the CLI total may be a running total, so the transcript alone sizes the stage until g0 has said which
    effective = effective_cost(c_stage, t_stage, cap)
    subtypes = info["subtypes"]
    capped = any("budget" in str(s) for s in subtypes) or effective >= cap
    final = info["final"] or {}
    infra = bool(timed_out or not info["results"] or (final.get("is_error") and not capped))
    charged = max(effective, float(cap)) if (capped or timed_out) else effective
    leaks = cmp.leak_scan(info["tool_blocks"], ws, proj / str(sid) / "subagents" if sid else None)
    state, state_ok, state_detail = arm_state(info["init"], arm)
    grade = None
    if spec.get("grade") and not infra:
        try:
            grade = spec["grade"](ws)
        except Exception as exc:  # a grader that cannot run is a finding, not a reason to lose the stage
            grade = {"kind": "error", "ok": None, "label": "grader error", "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    contexts = primary_contexts(calls)
    g = growth(contexts, prev.get("last_primary_context"))
    stats = tool_output_stats(events)
    cls_p = usd_by_class([c for c in calls if not c["sub"]])
    cls_s = usd_by_class([c for c in calls if c["sub"]])
    tool_names = {}
    for c in calls:
        for name in c["tools"]:
            tool_names[name] = tool_names.get(name, 0) + 1
    result = {
        "id": spec["ledger_id"], "kind": spec["kind"], "arm": arm, "stage": n, "cap": cap, "resume": bool(spec.get("resume")),
        "session_id": sid, "exit_code": code, "timed_out": timed_out, "wall_s": round(wall, 2), "infra_failure": infra,
        "result_subtypes": subtypes, "capped": capped,
        "cli_total": info["cli_cost"], "cli_stage": c_stage, "cost_mode": mode,
        "transcript_total": t_total, "transcript_stage": t_stage, "transcript_cumulative": cumulative, "cost_report_error": terr,
        "effective_cost": effective, "charged": charged,
        "usd_primary": float(sum(cls_p.values())), "usd_subagent": float(sum(cls_s.values())),
        "usd_primary_by_class": {k: float(v) for k, v in cls_p.items()}, "usd_subagent_by_class": {k: float(v) for k, v in cls_s.items()},
        "n_api_calls": len(calls), "n_primary_api_calls": len(contexts), "n_subagent_api_calls": len(calls) - len(contexts),
        "peak_context_tokens": info["peak_context_tokens"], "last_primary_context": contexts[-1] if contexts else prev.get("last_primary_context"),
        "growth_g": g, "tool_calls": info["tool_calls"], "tool_names": tool_names, "agent_calls": info["agent_calls"],
        "displaceable_tokens": stats["displaceable_tokens"], "displaceable_by_tool": stats["displaceable_by_tool"],
        "displaceable_share_of_g": (stats["displaceable_tokens"] / float(g)) if g > 0 else None, "report_tokens": stats["report_tokens"],
        "grade": grade, "permission_denials": len(info["denials"]), "permission_denial_reasons": [cmp.denial_reason(d) for d in info["denials"]],
        "leak_suspect": bool(leaks), "leak_hits": leaks[:5],
        "compactions": compaction_events(events), "transcript_compactions_cum": transcript_compactions(main_path),
        "arm_state": state, "arm_state_ok": state_ok, "arm_state_detail": state_detail,
        "agent_evidence": agent_evidence(events),
    }
    if infra:
        write_json(stage_dir / "failed.json", result)
    else:
        write_json(stage_dir / "result.json", result)
    ctx.ledger.end(spec["ledger_id"], spec["kind"], charged)
    if sid:
        with open(str(ctx.out / "sessions.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (sid, spec["ledger_id"], proj))
    return result


# ---------------------------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------------------------

def stage_dir_of(run_dir, n):
    return Path(run_dir) / ("stage-%02d" % n)


def load_stage_results(run_dir):
    out = []
    for p in sorted(Path(run_dir).glob("stage-??/result.json")):
        out.append(read_json(p))
    return out


def read_cost_mode(args, out):
    if args.cost_mode:
        return args.cost_mode
    path = Path(args.g0) if args.g0 else out / "g0.json"
    if path.is_file():
        mode = read_json(path).get("cli_cost_mode")
        if mode in COST_MODES:
            return mode
    raise Refused("whether the CLI cost total is per stage or cumulative is not known: run `g0` first (it writes g0.json) or pass --cost-mode")


def finish(run_dir, run, status, reason=None, results=None):
    run["status"] = status
    run["stop_reason"] = reason
    results = results if results is not None else load_stage_results(run_dir)
    run["stages_completed"] = [r["stage"] for r in results]
    write_json(Path(run_dir) / "run.json", run)
    first = results[0] if results else {}
    summary = {
        "arm": run["arm"], "status": status, "stop_reason": reason, "stages_planned": run["stages_planned"], "stages_completed": run["stages_completed"],
        "partial": status != "complete", "plugin_state": first.get("arm_state"), "plugin_state_ok": first.get("arm_state_ok"),
        "plugin_state_detail": first.get("arm_state_detail"), "plugin_commit": run.get("plugin_commit"), "frozen_tree_sha256": run.get("frozen_tree_sha256"),
        "cost_mode": run.get("cost_mode"), "resumes": run.get("resumes", 0),
        "effective_cost_total": sum(r["effective_cost"] for r in results),
        "agent_calls_total": sum(r["agent_calls"] for r in results), "growth_g_total": sum(r["growth_g"] for r in results),
        "plugin_state_by_stage": [r.get("arm_state") for r in results],
    }
    write_json(Path(run_dir) / "result.json", summary)
    return summary


def cmd_calibrate(args):
    if args.arm not in ARMS:
        raise Refused("unknown arm " + str(args.arm))
    if not 1 <= args.stages <= stages.MAX_STAGES:
        raise Refused("--stages must be 1..%d" % stages.MAX_STAGES)
    if args.stage_cap > args.run_cap:
        raise Refused("--stage-cap is above --run-cap")
    ctx = setup(args, [args.arm])
    mode = read_cost_mode(args, ctx.out)
    arm = args.arm
    run_dir = ctx.out / ("cal__" + arm)
    ws = ctx.work_root / ("cal__" + arm) / "ws"
    with RunLock(ctx.out):
        if args.resume_run:
            if not (run_dir / "run.json").is_file():
                raise Refused("nothing to resume: %s has no run.json" % run_dir)
            run = read_json(run_dir / "run.json")
            if run.get("resumes", 0) >= 1:
                raise Refused("this run was already resumed once")
            if run.get("status") not in LIVE_STATUSES:
                raise Refused("run status is %s: only an infrastructure failure or a runner that died can be resumed" % run.get("status"))
            if run.get("stages_planned") != args.stages:
                raise Refused("--stages %d differs from the run's %d" % (args.stages, run.get("stages_planned")))
            run["resumes"] = run.get("resumes", 0) + 1
            if not ws.is_dir():
                raise Refused("the workspace of the run is gone: " + str(ws))
        else:
            if run_dir.exists():
                raise Refused("%s exists: no top-up runs; use --resume-run after an infrastructure failure" % run_dir)
            run = {"version": 1, "arm": arm, "stages_planned": args.stages, "status": "running", "resumes": 0, "attempts": {},
                   "plugin_commit": ctx.plugin_commit, "frozen_tree_sha256": (ctx.frozen or {}).get("tree_sha256"), "cost_mode": mode}
            if ws.parent.exists():
                cmp.remove_inside(ws.parent, ctx.work_root)
            stages.build_workspace(ws)
            run_dir.mkdir(parents=True)
        run["status"] = "running"
        write_json(run_dir / "run.json", run)
        try:
            summary = run_loop(ctx, args, run, run_dir, ws, mode)
        except BudgetStop as exc:
            summary = finish(run_dir, run, "partial", "total cap: " + str(exc))
        except Refused as exc:
            finish(run_dir, run, "refused", "refused during the run: " + str(exc))
            raise
    return report_calibrate(ctx, run_dir, summary, ws)


def run_loop(ctx, args, run, run_dir, ws, mode):
    arm = run["arm"]
    results = load_stage_results(run_dir)
    run_id = "cal:%s:" % arm
    for n in range(len(results) + 1, args.stages + 1):
        cap = args.stage_cap
        if ctx.ledger.spent_run(run_id) + cap > args.run_cap:
            return finish(run_dir, run, "partial", "run cap: spent %.2f + stage cap %.2f would pass %.2f before stage %d"
                          % (ctx.ledger.spent_run(run_id), cap, args.run_cap, n), results)
        if ctx.ledger.spent() + cap > ctx.ledger.total_cap:
            return finish(run_dir, run, "partial", "total cap: spent %.2f + stage cap %.2f would pass %.2f before stage %d"
                          % (ctx.ledger.spent(), cap, ctx.ledger.total_cap, n), results)
        sd = stage_dir_of(run_dir, n)
        attempt = run["attempts"].get(str(n), 0) + 1
        run["attempts"][str(n)] = attempt
        if sd.exists():
            if args.keep_failed:
                sd.rename(sd.with_name(sd.name + ".attempt-%d" % (attempt - 1)))
            else:
                cmp.remove_inside(sd, run_dir)
        write_json(run_dir / "run.json", run)
        prev = results[-1] if results else None
        spec = {"kind": "cal", "arm": arm, "n": n, "prompt": stages.stage_prompt(n, arm), "cap": cap, "ws": ws, "stage_dir": sd,
                "ledger_id": "cal:%s:s%02d" % (arm, n) + ("" if attempt == 1 else ":a%d" % attempt),
                "resume": prev["session_id"] if prev else None, "prev": prev, "cost_mode": mode,
                "grade": (lambda w, _n=n: stages.grade_stage(w, _n))}
        res = execute_stage(ctx, spec)
        print("[%s stage %d/%d] %s: cost=%.4f wall=%.0fs agent_calls=%d%s" % (
            arm, n, args.stages, stages.STAGES[n - 1]["name"], res["effective_cost"], res["wall_s"], res["agent_calls"],
            "" if not res["grade"] else " grade=" + str(res["grade"].get("label"))))
        if res["infra_failure"]:
            return finish(run_dir, run, "infra_failed", "stage %d: no usable result (exit %s, timed out %s)" % (n, res["exit_code"], res["timed_out"]), results)
        results.append(res)
        if not res["arm_state_ok"]:
            return finish(run_dir, run, "arm_state_failed", "stage %d: %s" % (n, res["arm_state_detail"]), results)
        if res["capped"]:
            return finish(run_dir, run, "stage_cap_exceeded", "stage %d reached its cap of %.2f USD (effective %.4f)" % (n, cap, res["effective_cost"]), results)
    return finish(run_dir, run, "complete", None, results)


STATUS_EXIT = {"complete": 0, "partial": 4, "stage_cap_exceeded": 5, "infra_failed": 6, "arm_state_failed": 3}


def report_calibrate(ctx, run_dir, summary, ws):
    print("run %s: %s%s; %d of %d stage(s) done; spent %.2f USD in total." % (
        summary["arm"], summary["status"], "" if not summary["stop_reason"] else " (" + summary["stop_reason"] + ")",
        len(summary["stages_completed"]), summary["stages_planned"], ctx.ledger.spent()))
    if summary["status"] == "infra_failed":
        print("The runner can continue once with --resume-run from the last completed stage.")
    if summary["status"] not in LIVE_STATUSES and not ctx.keep and ws.parent.exists():
        cmp.remove_inside(ws.parent, ctx.work_root)
    print(CALIBRATION_NOTICE)
    return STATUS_EXIT.get(summary["status"], 1)


# ---------------------------------------------------------------------------------------------
# g0
# ---------------------------------------------------------------------------------------------

G0_PROMPTS = {
    1: "Reply OK\n",
    2: {"off": "Use the Agent tool once: have one subagent read the file `small.txt` in the current working directory and report how many lines it has. Then tell me that number.\n",
        "delegate": "Use the Agent tool once, with subagent type mitosis-orchestrator:flash-lite-scout: have it read the file `small.txt` in the current working directory "
                    "and report how many lines it has. Then tell me that number.\n"},
}


def read_debug(tmp):
    """Debug log entries below a private TMPDIR: path -> list of event names. Nothing else is kept."""
    out = {}
    for p in sorted(Path(tmp).rglob("mitosis_orchestrator_debug.log")):
        names = []
        with open(str(p), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    names.append(str(json.loads(line).get("event")))
                except ValueError:
                    continue
        out[str(p)] = names
    return out


def debug_delta(before, after):
    """Event names logged between two snapshots, and the number of log files that exist."""
    new = []
    for path, names in after.items():
        new += names[len(before.get(path, [])):]
    return new


def g0_modes(facts):
    """Per arm, the CLI cost mode the two stages show, with the evidence lines."""
    modes, ev = [], []
    for a in facts:
        s = (facts.get(a) or {}).get("stages") or []
        if len(s) != 2:
            continue
        d2 = transcript_stage_cost(s[1].get("transcript_total"), s[0].get("transcript_total"), s[1].get("transcript_cumulative"))
        mode = decide_cli_mode(s[0].get("cli_total"), s[1].get("cli_total"), d2)
        modes.append(mode)
        ev.append("%s: cli %s -> %s, transcript stage 2 %s -> %s" % (a, _f(s[0].get("cli_total")), _f(s[1].get("cli_total")), _f(d2), mode or "undecided"))
    return modes, ev


def g0_cost_mode(facts):
    modes, _ev = g0_modes(facts)
    known = {m for m in modes if m}
    return list(known)[0] if len(known) == 1 and None not in modes else None


def g0_findings(facts):
    """facts: arm -> {"stages": [stage-1 facts, stage-2 facts], "debug": {"events": [[...], [...]], "state_files": int} or None}.
    Stage facts: sid, cli_total, transcript_total, transcript_cumulative, agent_evidence. Returns a list of (name, status, evidence)."""
    out = []

    def have(arm):
        s = (facts.get(arm) or {}).get("stages") or []
        return s if len(s) == 2 and all(x for x in s) else None

    sids = {a: [x.get("sid") for x in have(a)] for a in facts if have(a)}
    if not sids or any(None in v for v in sids.values()):
        out.append(("session_id_stable", "UNKNOWN", "no session id in the stream of every stage"))
    else:
        changed = [a for a, v in sids.items() if v[0] != v[1]]
        out.append(("session_id_stable", "FAIL" if changed else "PASS",
                    ("id changed on --resume in: " + ", ".join(changed) + "; the harness follows the newest id") if changed else "same id in both stages, both arms"))
    dbg = (facts.get("delegate") or {}).get("debug")
    if not dbg or not dbg.get("events") or not any(dbg["events"]):
        out.append(("sessionstart_each_stage", "UNKNOWN", "no hook debug log (the plugin hooks wrote nothing under the private TMPDIR)"))
        out.append(("plugin_state_keyed", "UNKNOWN", "no hook debug log"))
    else:
        counts = [sum(1 for e in ev if e == "SessionStart") for ev in dbg["events"]]
        out.append(("sessionstart_each_stage", "PASS" if all(c >= 1 for c in counts) else "FAIL", "SessionStart entries per stage: %s" % counts))
        files, second = dbg.get("state_files"), len(dbg["events"][1])
        out.append(("plugin_state_keyed", "PASS" if files == 1 and second > 0 else "FAIL",
                    "hook state files: %s, hook entries in stage 2: %s" % (files, second)))
    modes, ev = g0_modes(facts)
    known = {m for m in modes if m}
    if len(known) == 1 and None not in modes:
        out.append(("cli_total_cost_cumulative", "PASS", "; ".join(ev) + " => %s" % list(known)[0]))
    elif len(known) > 1:
        out.append(("cli_total_cost_cumulative", "FAIL", "arms disagree: " + "; ".join(ev)))
    else:
        out.append(("cli_total_cost_cumulative", "UNKNOWN", "; ".join(ev) or "no cost data"))
    td, tev = [], []
    for a in facts:
        s = have(a)
        if not s or s[0].get("transcript_total") is None or s[1].get("transcript_total") is None:
            continue
        t1, t2, cum = s[0]["transcript_total"], s[1]["transcript_total"], s[1].get("transcript_cumulative")
        bad = bool(cum and t2 < t1 - 1e-9)
        td.append(not bad)
        tev.append("%s: stage-1 transcript %s, stage-2 transcript %s, holds earlier stages %s, summing both would add %s" % (a, _f(t1), _f(t2), cum, _f(t1 if cum else 0)))
    out.append(("transcripts_not_double_counted", "UNKNOWN" if not td else ("PASS" if all(td) else "FAIL"),
                "; ".join(tev) + ("" if not td else "; the harness takes differences when a stage's transcript holds the stage before") or "no transcript totals"))
    sync, sev = [], []
    for a in facts:
        s = have(a)
        if not s:
            continue
        e = s[1].get("agent_evidence") or {}
        if not e.get("agent_calls"):
            sync.append(None)
            sev.append("%s: no Agent call seen" % a)
            continue
        ok = e["returned"] >= e["agent_calls"] and e["returned_before_result"] >= e["agent_calls"] and not e["background_marked"]
        sync.append(ok)
        sev.append("%s: Agent calls %d, results returned %d, before the stage result %d, background-marked %d, subagent events streamed %d" % (
            a, e["agent_calls"], e["returned"], e["returned_before_result"], e["background_marked"], e["subagent_events"]))
    if not sync or all(x is None for x in sync):
        out.append(("agent_calls_synchronous", "UNKNOWN", "; ".join(sev) or "no stage 2 data"))
    else:
        known_sync = [x for x in sync if x is not None]
        out.append(("agent_calls_synchronous", "PASS" if all(known_sync) else "FAIL", "; ".join(sev)))
    return out


def _f(x):
    return "n/a" if x is None else "%.4f" % float(x)


def cmd_g0(args):
    ctx = setup(args, ARMS)
    cap_each = float(args.cap_usd) / (len(ARMS) * 2)
    facts, stopped = {}, None
    with RunLock(ctx.out):
        try:
            for arm in ARMS:
                base = ctx.work_root / ("g0__" + arm)
                if base.exists():
                    cmp.remove_inside(base, ctx.work_root)
                ws, tmp = base / "ws", base / "tmp"
                ws.mkdir(parents=True)
                tmp.mkdir()
                (ws / "small.txt").write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n", encoding="utf-8")
                run_dir = ctx.out / ("g0__" + arm)
                if run_dir.exists():
                    cmp.remove_inside(run_dir, ctx.out)
                run_dir.mkdir(parents=True)
                env = {"CLAUDE_MITOSIS_DEBUG": "1", "TMPDIR": str(tmp)}
                prev, snaps, entry = None, [{}], {"stages": [], "debug": None}
                for n in (1, 2):
                    prompt = G0_PROMPTS[1] if n == 1 else G0_PROMPTS[2][arm]
                    spec = {"kind": "g0", "arm": arm, "n": n, "prompt": prompt, "cap": cap_each, "ws": ws, "stage_dir": stage_dir_of(run_dir, n),
                            "ledger_id": "g0:%s:s%02d" % (arm, n), "resume": prev["session_id"] if prev else None, "prev": prev, "cost_mode": None,
                            "grade": None, "env": env}
                    res = execute_stage(ctx, spec)
                    snaps.append(read_debug(tmp))
                    entry["stages"].append({"sid": res["session_id"], "cli_total": res["cli_total"], "transcript_total": res["transcript_total"],
                                            "transcript_cumulative": res["transcript_cumulative"], "agent_evidence": res["agent_evidence"],
                                            "arm_state_ok": res["arm_state_ok"]})
                    prev = res
                    if res["infra_failure"] or not res["session_id"]:
                        break
                if len(entry["stages"]) == 2 and arm == "delegate":
                    events = [debug_delta(snaps[i], snaps[i + 1]) for i in (0, 1)]
                    entry["debug"] = {"events": events, "state_files": len(snaps[2])}
                facts[arm] = entry
        except BudgetStop as exc:
            stopped = str(exc)
        if not args.keep and ctx.work_root.exists():
            for arm in ARMS:
                if (ctx.work_root / ("g0__" + arm)).exists():
                    cmp.remove_inside(ctx.work_root / ("g0__" + arm), ctx.work_root)
    findings = g0_findings(facts)
    mode = g0_cost_mode(facts)
    write_json(ctx.out / "g0.json", {"cli_cost_mode": mode, "stopped": stopped, "findings": [{"name": n, "status": s, "evidence": e} for n, s, e in findings]})
    for name, status, evidence in findings:
        print("%-32s %-8s %s" % (name, status, evidence))
    if stopped:
        print("stopped cleanly: " + stopped)
    print("spent %.2f USD in total. CLI cost mode written to g0.json: %s" % (ctx.ledger.spent(), mode or "undecided (pass --cost-mode to calibrate)"))
    print(CALIBRATION_NOTICE)
    return 4 if stopped else 0


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------

def _num(x, nd=4):
    return "n/a" if x is None else ("%.*f" % (nd, float(x)))


def _int(x):
    return "n/a" if x is None else str(int(round(float(x))))


def _pct(x):
    return "n/a" if x is None else "%.1f%%" % (100 * float(x))


def load_runs(out):
    runs = {}
    for arm in ARMS:
        d = Path(out) / ("cal__" + arm)
        if (d / "run.json").is_file():
            runs[arm] = {"run": read_json(d / "run.json"), "stages": load_stage_results(d)}
    return runs


def grade_summary(results):
    parts = []
    look = [r for r in results if (r.get("grade") or {}).get("kind") == "lookup"]
    if look:
        parts.append("lookup %d/%d" % (sum(r["grade"]["correct"] for r in look), sum(r["grade"]["total"] for r in look)))
    for r in results:
        g = r.get("grade") or {}
        if g.get("kind") == "fix" and g.get("graded"):
            parts.append("fix " + ("pass" if g["ok"] else "fail"))
        if g.get("kind") == "ops_final":
            parts.append("ops " + ("pass" if g["ops_ok"] else "fail") + ", change " + ("pass" if g["change_ok"] else "fail"))
        if g.get("kind") == "recall":
            parts.append("recall %d/%d" % (g["correct"], g["total"]))
    return ", ".join(parts) or "none graded"


def totals(results):
    ok = [r for r in results]
    g = sum(r["growth_g"] for r in ok)
    disp = sum(r["displaceable_tokens"] for r in ok)
    return {
        "stages": len(ok), "cost": sum(r["effective_cost"] for r in ok), "usd_primary": sum(r["usd_primary"] for r in ok),
        "usd_subagent": sum(r["usd_subagent"] for r in ok), "api_calls": sum(r["n_api_calls"] for r in ok),
        "primary_calls": sum(r["n_primary_api_calls"] for r in ok), "peak_context": max([r["peak_context_tokens"] for r in ok] or [0]),
        "growth_g": g, "displaceable": disp, "displaceable_share": (disp / float(g)) if g > 0 else None,
        "agent_calls": sum(r["agent_calls"] for r in ok), "report_tokens": sum(r["report_tokens"] for r in ok),
        "denials": sum(r["permission_denials"] for r in ok), "leak_suspects": sum(1 for r in ok if r["leak_suspect"]),
        "compactions": sum(len(r["compactions"]) for r in ok), "wall_s": sum(r["wall_s"] for r in ok), "grades": grade_summary(ok),
    }


def build_report(out):
    runs = load_runs(out)
    common = None
    for arm in ARMS:
        if arm in runs:
            have = {r["stage"] for r in runs[arm]["stages"]}
            common = have if common is None else common & have
    arms = {}
    for arm, data in runs.items():
        res = data["stages"]
        arms[arm] = {"run": data["run"], "all": totals(res), "common": totals([r for r in res if common is None or r["stage"] in common])}
    return {"runs": runs, "arms": arms, "common_stages": sorted(common or []), "spent": Ledger(out, 0).spent()}


STAGE_HEAD = "%-3s %-34s %7s %7s %7s %9s %9s %8s %6s %5s %7s %-18s %3s %4s %4s %6s"


def render_run(arm, data):
    run = data["run"]
    lines = ["== arm %s: status %s%s, stages %d of %d, cost mode %s, resumed %d time(s) ==" % (
        arm, run.get("status"), "" if not run.get("stop_reason") else " (" + run["stop_reason"] + ")", len(data["stages"]), run.get("stages_planned"),
        run.get("cost_mode"), run.get("resumes", 0))]
    first = data["stages"][0] if data["stages"] else None
    if first:
        lines.append("   plugin state in init: %s (%s)" % (json.dumps(first["arm_state"], sort_keys=True), first["arm_state_detail"]))
    lines.append(STAGE_HEAD % ("stg", "name", "USD", "prim$", "sub$", "calls p/s", "peak ctx", "G", "disp%G", "Agent", "rep tok", "grade", "den", "leak", "comp", "wall s"))
    for r in data["stages"]:
        lines.append(STAGE_HEAD % (
            r["stage"], stages.STAGES[r["stage"] - 1]["name"][:34], _num(r["effective_cost"]), _num(r["usd_primary"]), _num(r["usd_subagent"]),
            "%d/%d" % (r["n_primary_api_calls"], r["n_subagent_api_calls"]), r["peak_context_tokens"], r["growth_g"], _pct(r["displaceable_share_of_g"]),
            r["agent_calls"], r["report_tokens"], ((r.get("grade") or {}).get("label") or "-")[:18], r["permission_denials"], "yes" if r["leak_suspect"] else "no",
            len(r["compactions"]), "%.0f" % r["wall_s"]))
    t = totals(data["stages"])
    lines.append(STAGE_HEAD % ("sum", "", _num(t["cost"]), _num(t["usd_primary"]), _num(t["usd_subagent"]), "%d/%d" % (t["primary_calls"], t["api_calls"] - t["primary_calls"]),
                               t["peak_context"], t["growth_g"], _pct(t["displaceable_share"]), t["agent_calls"], t["report_tokens"], "", t["denials"],
                               t["leak_suspects"], t["compactions"], "%.0f" % t["wall_s"]))
    comp = [(r["stage"], c["trigger"], c["pre_tokens"]) for r in data["stages"] for c in r["compactions"]]
    lines.append("   compactions (stage, trigger, preTokens): %s" % (comp or "none seen in the stream"))
    lines.append("   graded: " + t["grades"])
    return lines


def render_report(rep):
    lines = [CALIBRATION_NOTICE, ""]
    for arm in ARMS:
        if arm in rep["runs"]:
            lines += render_run(arm, rep["runs"][arm]) + [""]
    if len(rep["arms"]) == 2:
        c = rep["common_stages"]
        lines.append("== arm comparison over the %d stage(s) both runs completed (%s) ==" % (len(c), ",".join(str(x) for x in c) or "none"))
        o, d = rep["arms"]["off"]["common"], rep["arms"]["delegate"]["common"]
        lines.append("%-30s %12s %12s %12s %9s" % ("", "off", "delegate", "difference", "relative"))
        for label, key, fmt in (("effective USD", "cost", _num), ("primary USD", "usd_primary", _num), ("subagent USD", "usd_subagent", _num),
                                ("API calls", "api_calls", _int), ("primary API calls", "primary_calls", _int), ("peak context (max)", "peak_context", _int),
                                ("cumulative context growth G", "growth_g", _int), ("displaceable tokens (est.)", "displaceable", _int),
                                ("Agent calls", "agent_calls", _int), ("report tokens returned (est.)", "report_tokens", _int),
                                ("permission denials", "denials", _int), ("leak suspects", "leak_suspects", _int), ("compactions", "compactions", _int),
                                ("wall seconds", "wall_s", _int)):
            diff = d[key] - o[key]
            rel = None if not o[key] else diff / float(o[key])
            lines.append("%-30s %12s %12s %12s %9s" % (label, fmt(o[key]), fmt(d[key]), ("%+g" % round(diff, 4)), "n/a" if rel is None else "%+.1f%%" % (100 * rel)))
        lines.append("%-30s %12s %12s" % ("displaceable share of G", _pct(o["displaceable_share"]), _pct(d["displaceable_share"])))
        lines.append("%-30s %s | %s" % ("graded", o["grades"], d["grades"]))
        lines.append("One run per arm. Read these as sizes, not as an outcome.")
    lines += ["", "ledger total (without --spent-before): %.2f USD" % rep["spent"]]
    return "\n".join(lines)


def cmd_report(args):
    out = Path(os.path.abspath(args.out))
    if not out.is_dir():
        raise Refused("no such output directory: " + str(out))
    rep = build_report(out)
    text = render_report(rep)
    print(text)
    write_json(out / "report.json", {"arms": rep["arms"], "common_stages": rep["common_stages"], "spent": rep["spent"]})
    with open(str(out / "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Long-session calibration (unscored; see PROTOCOL.md)")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("g0", "calibrate"):
        p = sub.add_parser(name)
        p.add_argument("--plugin-commit", required=True)
        p.add_argument("--plugin-repo", default=str(REPO))
        p.add_argument("--work-root", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--spent-before", type=float, default=0.0)
        p.add_argument("--total-cap", type=float, default=TOTAL_CAP_USD)
        p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
        p.add_argument("--claude", default="claude")
        p.add_argument("--keep", action="store_true")
        if name == "g0":
            p.add_argument("--cap-usd", type=float, default=G0_CAP_USD)
        else:
            p.add_argument("--arm", required=True, choices=ARMS)
            p.add_argument("--stages", type=int, default=stages.MAX_STAGES)
            p.add_argument("--stage-cap", type=float, default=STAGE_CAP_USD)
            p.add_argument("--run-cap", type=float, default=RUN_CAP_USD)
            p.add_argument("--resume-run", action="store_true")
            p.add_argument("--cost-mode", choices=COST_MODES)
            p.add_argument("--g0", help="g0.json to read the cost mode from (default: <out>/g0.json)")
            p.add_argument("--keep-failed", action="store_true", help="keep the files of a failed stage attempt as stage-NN.attempt-K")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"g0": cmd_g0, "calibrate": cmd_calibrate, "report": cmd_report}[args.command](args)
    except Refused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
