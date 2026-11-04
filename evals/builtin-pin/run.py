#!/usr/bin/env python3
"""Built-in pin experiment: what does CLAUDE_MITOSIS_PIN_BUILTINS=1 do to a lookup task?

    run.py seal      --plugin-commit SHA [--seed N] [--plugin-repo DIR]
    run.py calibrate --work-root DIR --out DIR [--spent-before USD]
    run.py run       --work-root DIR --out DIR [--spent-before USD] [--resume] [--keep] [--max-runs N]
    run.py report    --out DIR

See PROTOCOL.md for the pre-registered design and the decision rule. Python 3.8, standard library only.
This file is part of the seal: changing it after `seal` makes `calibrate` and `run` refuse.

It reuses helpers from ../overhead/run.py and ../compare/run.py, loaded by path. Neither is edited here, and
importing them must not leave bytecode in evals/compare, hence the flag below.

Exit codes: 0 ok, 1 failed report, 2 refused, 3 a run was not in its intended arm, 4 stopped by the budget,
5 calibration projects a total above the budget.
"""

import sys

sys.dont_write_bytecode = True  # before importing compare/run.py: its tamper check trips on bytecode

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
COMPARE_DIR = HERE.parent / "compare"
OVERHEAD_DIR = HERE.parent / "overhead"
FIXTURE = COMPARE_DIR / "tasks" / "lookup"
SEAL_PATH = HERE / "SEAL.json"

ARMS = ("pinned", "inherit")
FLAG = "CLAUDE_MITOSIS_PIN_BUILTINS"
DEBUG_FLAG = "CLAUDE_MITOSIS_DEBUG"
ROUNDS = 3
N_BATCHES = 6
BATCH_SIZE = 5
N_QUESTIONS = N_BATCHES * BATCH_SIZE
EXPECTED_RUNS = ROUNDS * N_BATCHES * len(ARMS)       # 36
RUNS_PER_ARM_PROJECTED = ROUNDS * N_BATCHES + 1      # 18 scored + 1 warm-up
RUN_CAP_USD = 1.5
BUDGET_LIMIT_USD = 20.0
DEFAULT_SEED = 20261105
DEFAULT_TIMEOUT_S = 1200
QUALITY_MARGIN = 5                    # QUALITY LOSS: A <= B - 5
SUPPORT_MARGIN = 2                    # SUPPORTED: A >= B - 2
COST_RATIO_MAX = Fraction(70, 100)    # SUPPORTED: pinned median cost <= 70% of inherit median cost
MIN_CHEAPER_PAIRS = 14                # SUPPORTED: pinned cheaper in at least 14 of 18 pairs
INVALID_SHARE_MAX = Fraction(20, 100)  # more than this share of invalid runs: INCONCLUSIVE
COMP_SHARE_MAX = Fraction(20, 100)     # more than this share of pinned runs with compensation reads: QUALITY LOSS
COST_DISAGREE_REL = Fraction(5, 100)
COST_DISAGREE_ABS = Fraction(5, 1000)
COMPENSATION_TOOLS = ("Read", "Grep", "Glob", "Bash")
STRIP_PREFIXES = ("CLAUDE_MITOSIS_", "AGY_MITOSIS_", "CODEX_MITOSIS_", "AGY_ARGON_")
STRIP_NAMES = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_EFFORT")
WANT_FAMILY = {"pinned": "haiku", "inherit": "opus"}
FAMILIES = ("opus", "sonnet", "haiku")

PROMPT_TAIL = (
    "Work in two steps. First dispatch exactly one subagent of type `Explore` and give it the full text of every question above, "
    "so that it finds all the answers in `stdlib/` in one pass. Do not read, search or list any file yourself, and do not dispatch "
    "any other subagent. Second, when it has reported, write `answers.json` as described above, with one entry per question id "
    "listed above and no others.\n"
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ov = _load("overhead_run_for_builtin_pin", OVERHEAD_DIR / "run.py")
cmp = ov.cmp           # one copy of compare/run.py, so that Refused and BudgetStop are the same classes everywhere
Refused = cmp.Refused
BudgetStop = cmp.BudgetStop
ArmStateError = ov.ArmStateError

_GRADE = []


def grade_module():
    """grade.py of the lookup fixture, loaded by path (its `same` is the normalisation the full grader uses)."""
    if not _GRADE:
        _GRADE.append(_load("lookup_grade_for_builtin_pin", FIXTURE / "grade.py"))
    return _GRADE[0]


def _frac(value):
    return Fraction(str(value))


# ---------------------------------------------------------------------------------------------
# schedule, batches, prompt. Pure functions.
# ---------------------------------------------------------------------------------------------

def question_ids():
    with open(str(FIXTURE / "key.json"), encoding="utf-8") as fh:
        return sorted(json.load(fh))


def make_batches(seed, ids=None):
    """The 30 question ids shuffled by the seed into 6 batches of 5 (ids sorted inside a batch)."""
    ids = list(ids if ids is not None else question_ids())
    ordered = sorted(ids, key=lambda q: cmp._order_key(seed, "batch", q))
    return [sorted(ordered[i:i + BATCH_SIZE]) for i in range(0, len(ordered), BATCH_SIZE)]


def make_schedule(seed, rounds=ROUNDS, n_batches=N_BATCHES, arms=ARMS):
    """List of (round, batch, arm); batch is 1-based; the order inside each round is a seeded shuffle."""
    cells = []
    for r in range(1, rounds + 1):
        block = [(r, b, a) for b in range(1, n_batches + 1) for a in arms]
        cells.extend(sorted(block, key=lambda c: cmp._order_key(seed, "cell", c[0], c[1], c[2])))
    return cells


def warmup_order(seed):
    return sorted(ARMS, key=lambda a: cmp._order_key(seed, "warmup", a))


def calibration_order(seed):
    return sorted(ARMS, key=lambda a: cmp._order_key(seed, "calibrate", a))


QUESTION_LINE = re.compile(r"^- (q\d{2}): ")


def split_prompt(text):
    """(preamble, {id: line}) of the fixture prompt."""
    pre, lines = [], {}
    for line in text.splitlines():
        m = QUESTION_LINE.match(line)
        if m:
            lines[m.group(1)] = line
        elif not lines:
            pre.append(line)
    return "\n".join(pre).rstrip() + "\n", lines


def build_prompt(batch_ids):
    """The fixture's own preamble, the batch's question lines, then the two-step instruction."""
    pre, lines = split_prompt(cmp.read_prompt("lookup"))
    missing = [q for q in batch_ids if q not in lines]
    if missing:
        raise Refused("questions missing from the lookup prompt: " + ", ".join(missing))
    return pre + "\n" + "\n".join(lines[q] for q in batch_ids) + "\n\n" + PROMPT_TAIL


# ---------------------------------------------------------------------------------------------
# environment: the only difference between the arms is one variable
# ---------------------------------------------------------------------------------------------

def arm_env(arm, base=None):
    """Child environment. Every mitosis variable and the subagent overrides of the caller are removed in both arms,
    the debug log is switched on in both, and only the pinned arm gets the flag. The inherit arm has it removed."""
    if arm not in ARMS:
        raise ValueError("unknown arm " + str(arm))
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith(STRIP_PREFIXES) or key in STRIP_NAMES:
            del env[key]
    env[DEBUG_FLAG] = "1"
    if arm == "pinned":
        env[FLAG] = "1"
    else:
        env.pop(FLAG, None)
    return env


def env_parity_problems(base=None):
    probe = dict(base or {}, **{FLAG: "0", "CLAUDE_MITOSIS_PIN_EFFORT": "0", "AGY_MITOSIS_X": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "x", "KEEP_ME": "1"})
    a, b = arm_env("pinned", probe), arm_env("inherit", probe)
    problems = []
    if a.get(FLAG) != "1":
        problems.append("pinned arm does not set the flag to 1")
    if FLAG in b:
        problems.append("inherit arm still has the flag")
    if {k: v for k, v in a.items() if k != FLAG} != b:
        problems.append("the arms' environments differ beyond the flag")
    if "KEEP_ME" not in a or any(k.startswith(STRIP_PREFIXES) and k != DEBUG_FLAG and k != FLAG for k in a):
        problems.append("environment stripping is wrong")
    return problems


def claude_config_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))


def settings_files(env=None):
    base = claude_config_dir(env)
    return [base / "settings.json", base / "settings.local.json", Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"), Path("C:/Program Files/ClaudeCode/managed-settings.json")]


def read_settings_env(files):
    """(merged `env` objects of the settings files that exist, files read). Claude Code applies these to every session,
    identically in both arms and over the process environment."""
    merged, used = {}, []
    for f in files:
        try:
            with open(str(f), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        used.append(str(f))
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, dict):
            merged.update({str(k): str(v) for k, v in env.items()})
    return merged, used


def flag_resolution(arm, env, settings_env):
    """What the flag resolves to for this run: settings env overrides the process environment."""
    passed = env.get(FLAG)
    in_settings = settings_env.get(FLAG)
    resolved = in_settings if in_settings is not None else passed
    expected = "1" if arm == "pinned" else None
    ok = (resolved == "1") if arm == "pinned" else (resolved != "1")
    return {"env_passed": passed, "settings_value": in_settings, "resolved": resolved, "expected": expected, "as_intended": ok}


# ---------------------------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------------------------

def build_command(claude, prompt, plugin_dir, cap):
    """compare's off command (opus, medium, acceptEdits, lookup allowed tools, cap, stream-json, settings that switch off
    the installed copy, strict MCP), then the frozen plugin. Identical for both arms: only the environment differs."""
    cmd = cmp.build_command(claude, "off", "lookup", prompt, None, cap)
    cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


def run_claude_env(cmd, workdir, stream_path, stderr_path, timeout, env):
    """compare's run_claude with an explicit child environment (compare's takes none and is sealed)."""
    started = time.monotonic()
    timed_out = False
    with open(str(stream_path), "wb") as out, open(str(stderr_path), "wb") as err:
        proc = subprocess.Popen(cmd, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                pass
            code = proc.wait()
    return code, timed_out, time.monotonic() - started


# ---------------------------------------------------------------------------------------------
# reading a run: cost, tools, subagents, hook log, answers. Pure where possible.
# ---------------------------------------------------------------------------------------------

def model_family(name):
    low = str(name or "").lower()
    for fam in FAMILIES:
        if fam in low:
            return fam
    return "other"


def parse_cost(final):
    """Per-model cost and tokens from the result event's `modelUsage`, which counts every call including subagents'.
    (stream-json per-call usage is not used: it undercounts output tokens about tenfold.)"""
    final = final or {}
    models = {}
    for name, u in (final.get("modelUsage") or {}).items():
        if not isinstance(u, dict):
            continue
        models[str(name)] = {
            "cost_usd": float(u.get("costUSD") or 0), "input": int(u.get("inputTokens") or 0), "output": int(u.get("outputTokens") or 0),
            "cache_read": int(u.get("cacheReadInputTokens") or 0), "cache_write": int(u.get("cacheCreationInputTokens") or 0),
        }
    fam_cost = {f: 0.0 for f in FAMILIES + ("other",)}
    fam_out = {f: 0 for f in FAMILIES + ("other",)}
    for name, u in models.items():
        fam_cost[model_family(name)] += u["cost_usd"]
        fam_out[model_family(name)] += u["output"]
    total = final.get("total_cost_usd")
    total = float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None
    usage_sum = sum(u["cost_usd"] for u in models.values()) if models else None
    known = [c for c in (total, usage_sum) if c is not None]
    effective = max(known) if known else None
    disagree = False
    if total is not None and usage_sum is not None:
        diff = abs(_frac(total) - _frac(usage_sum))
        disagree = diff > COST_DISAGREE_ABS and diff > COST_DISAGREE_REL * max(_frac(total), _frac(usage_sum))
    return {"models": models, "family_cost": fam_cost, "family_output": fam_out, "total_cost_usd": total, "usage_cost_sum": usage_sum,
            "effective": effective, "disagree": disagree}


def primary_tool_counts(events):
    """Tool calls of the primary only (no parent_tool_use_id), once per tool_use id, and the Agent calls with their types."""
    seen, counts, agents = set(), {}, []
    for e in events:
        if e.get("type") != "assistant" or e.get("parent_tool_use_id") or not isinstance(e.get("message"), dict):
            continue
        for block in e["message"].get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            key = block.get("id") or id(block)
            if key in seen:
                continue
            seen.add(key)
            name = str(block.get("name"))
            counts[name] = counts.get(name, 0) + 1
            if name in cmp.AGENT_TOOL_NAMES:
                agents.append(str((block.get("input") or {}).get("subagent_type") or "").strip())
    return counts, agents


def is_explore(agent_type):
    return str(agent_type or "").strip().lower().split(":")[-1] == "explore"


def subagent_facts(sub_dir):
    """One record per subagent: agentType and model from agent-*.meta.json, models from the transcript agent-*.jsonl.
    Content of the transcript is not kept."""
    out = []
    sub_dir = Path(sub_dir)
    if not sub_dir.is_dir():
        return out
    for meta in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            with open(str(meta), encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            m = {}
        if not isinstance(m, dict):
            m = {}
        transcript = meta.with_name(meta.name[:-len(".meta.json")] + ".jsonl")
        models = set()
        if transcript.is_file():
            for rec in cmp.read_events(transcript):
                msg = rec.get("message")
                name = msg.get("model") if isinstance(msg, dict) else None
                if isinstance(name, str) and name and name != "<synthetic>":
                    models.add(name)
        out.append({"file": meta.name, "agent_type": m.get("agentType"), "meta_model": m.get("model"), "meta_effort": m.get("effort"),
                    "models": sorted(models), "has_transcript": transcript.is_file()})
    return out


def model_problems(arm, facts, explore_calls):
    """Why the subagent evidence does not fit the arm (empty list: it fits). pinned needs a haiku model, inherit an opus model."""
    want = WANT_FAMILY[arm]
    problems = []
    if explore_calls < 1:
        problems.append("no_explore_call")
    explore = [f for f in facts if is_explore(f.get("agent_type"))]
    if not explore:
        problems.append("no_explore_subagent_record")
    for f in explore:
        if not f.get("models"):
            problems.append("explore_model_unverifiable")
            continue
        wrong = [m for m in f["models"] if want not in m.lower()]
        if wrong:
            problems.append("explore_wrong_model:" + ",".join(wrong))
        meta_model = str(f.get("meta_model") or "").lower()
        if meta_model and want not in meta_model and any(fam in meta_model for fam in FAMILIES):
            problems.append("explore_meta_wrong_model:" + meta_model)
    return problems


def hook_debug_path(session_id, tmp=None):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:128]
    return Path(tmp or tempfile.gettempdir()) / "mitosis-orchestrator" / safe / "mitosis_orchestrator_debug.log"


def pin_decisions(path):
    """(number of builtin-pin decisions in the hook debug log, number of log lines); (0, None) when there is no log."""
    path = Path(path)
    if not path.is_file():
        return 0, None
    count = entries = 0
    for entry in cmp.read_events(path):
        entries += 1
        if entry.get("mode") != "pre-tool-use" or entry.get("tool") not in cmp.AGENT_TOOL_NAMES:
            continue
        decisions = [d for d in entry.get("decisions") or [] if isinstance(d, dict)]
        reason = str((((entry.get("result") or {}).get("hookSpecificOutput")) or {}).get("permissionDecisionReason") or "")
        if any(d.get("rule") == "builtin-pin" for d in decisions) or "pinned built-in Explore" in reason:
            count += 1
    return count, entries


def score_answers(answers, ids, key, same=None):
    """Exact matches against the key through grade.py's own `same`. Returns (correct, wrong ids)."""
    same = same or grade_module().same
    wrong = [q for q in ids if not isinstance(answers, dict) or q not in answers or not same(answers[q], key[q])]
    return len(ids) - len(wrong), wrong


def load_answers(workdir):
    """(answers or None, status)."""
    path = Path(workdir) / "answers.json"
    if not path.is_file():
        return None, "missing"
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        return None, "bad_json"
    if not isinstance(data, dict):
        return None, "not_object"
    return data, "ok"


def validity(arm, facts, explore_calls, pins, pin_log_entries, flag, run):
    """Reasons a run is invalid (empty list: valid)."""
    reasons = model_problems(arm, facts, explore_calls)
    if not flag["as_intended"]:
        reasons.append("flag_not_as_intended:%s" % flag["resolved"])
    if arm == "pinned":
        if pin_log_entries is None:
            reasons.append("hook_log_missing")
        elif pins < 1:
            reasons.append("no_pin_decision_in_hook_log")
    elif pins > 0:
        reasons.append("pin_decision_in_inherit_arm")
    if run.get("capped"):
        reasons.append("hit_cap")
    if run.get("timed_out"):
        reasons.append("timed_out")
    if not run.get("has_result"):
        reasons.append("no_result_event")
    if run.get("leak_suspect"):
        reasons.append("leak_suspect")
    if not run.get("stdlib_ok", True):
        reasons.append("stdlib_changed")
    return reasons


# ---------------------------------------------------------------------------------------------
# decision rule (PROTOCOL.md). Pure functions, no I/O.
# ---------------------------------------------------------------------------------------------

def share_exceeds(part, whole, limit):
    """part / whole > limit, exactly. (Equal to the limit does not exceed it.)"""
    return whole > 0 and Fraction(part, whole) > limit


def invalid_exceeds(n_invalid, n_total):
    return share_exceeds(n_invalid, n_total, INVALID_SHARE_MAX)


def comp_exceeds(n_comp, n_valid_pinned):
    return share_exceeds(n_comp, n_valid_pinned, COMP_SHARE_MAX)


def pairs_of(records):
    """(batch, round) -> {arm: record}."""
    pairs = {}
    for r in records:
        pairs.setdefault((r["batch"], r["round"]), {})[r["arm"]] = r
    return pairs


def decide(records, expected=EXPECTED_RUNS):
    """records: one dict per scored run with arm, batch, round, valid, correct, cost, comp (primary made Read/Grep/Glob/Bash calls).
    Order: INCONCLUSIVE for incomplete data or too many invalid runs, then QUALITY LOSS, then SUPPORTED, else INCONCLUSIVE.
    A and B and the costs come from pairs (batch, round) in which both runs are valid."""
    n = len(records)
    n_invalid = sum(1 for r in records if not r["valid"])
    usable = [(p["pinned"], p["inherit"]) for p in pairs_of(records).values() if "pinned" in p and "inherit" in p and p["pinned"]["valid"] and p["inherit"]["valid"]]
    pinned_valid = [r for r in records if r["arm"] == "pinned" and r["valid"]]
    n_comp = sum(1 for r in pinned_valid if r["comp"])
    a = sum(int(p["correct"]) for p, _ in usable)
    b = sum(int(i["correct"]) for _, i in usable)
    med_p = median([_frac(p["cost"]) for p, _ in usable]) if usable else None
    med_i = median([_frac(i["cost"]) for _, i in usable]) if usable else None
    cheaper = sum(1 for p, i in usable if _frac(p["cost"]) < _frac(i["cost"]))
    out = {"A": a, "B": b, "n_runs": n, "n_invalid": n_invalid, "n_pairs_usable": len(usable), "median_pinned": med_p, "median_inherit": med_i,
           "ratio": (med_p / med_i) if usable and med_i != 0 else None, "cheaper_pairs": cheaper, "n_comp": n_comp, "n_pinned_valid": len(pinned_valid)}
    if n < expected:
        out.update(outcome="INCONCLUSIVE", reason="incomplete data: %d of %d scored runs" % (n, expected))
    elif invalid_exceeds(n_invalid, expected):
        out.update(outcome="INCONCLUSIVE", reason="%d of %d runs are invalid (more than 20%%)" % (n_invalid, expected))
    elif not usable:
        out.update(outcome="INCONCLUSIVE", reason="no pair with two valid runs")
    elif a <= b - QUALITY_MARGIN:
        out.update(outcome="QUALITY LOSS", reason="A=%d is at least %d below B=%d" % (a, QUALITY_MARGIN, b))
    elif comp_exceeds(n_comp, len(pinned_valid)):
        out.update(outcome="QUALITY LOSS", reason="%d of %d valid pinned runs have compensation reads (more than 20%%)" % (n_comp, len(pinned_valid)))
    else:
        fail = []
        if a < b - SUPPORT_MARGIN:
            fail.append("A=%d is more than %d below B=%d" % (a, SUPPORT_MARGIN, b))
        if med_p * 10 > med_i * 7:
            fail.append("pinned median cost is above 70%% of inherit (%s vs %s)" % (_money(med_p), _money(med_i)))
        if cheaper < MIN_CHEAPER_PAIRS:
            fail.append("pinned is cheaper in %d pairs, %d needed" % (cheaper, MIN_CHEAPER_PAIRS))
        if fail:
            out.update(outcome="INCONCLUSIVE", reason="; ".join(fail))
        else:
            out.update(outcome="SUPPORTED", reason="A=%d, B=%d, median cost ratio %.1f%%, pinned cheaper in %d of %d pairs"
                       % (a, b, float(med_p / med_i) * 100, cheaper, len(usable)))
    return out


def _money(x):
    return "n/a" if x is None else "%.4f" % float(x)


def project_total(spent, cost_pinned, cost_inherit):
    """Spent so far plus the warm-up and the 18 scored runs of each arm at the calibrated cost per run."""
    return float(spent) + RUNS_PER_ARM_PROJECTED * (float(cost_pinned) + float(cost_inherit))


# ---------------------------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------------------------

def sealed_files():
    out = {"builtin-pin:PROTOCOL.md": cmp.sha256_path(HERE / "PROTOCOL.md"), "builtin-pin:run.py": cmp.sha256_path(HERE / "run.py"),
           "overhead:run.py": cmp.sha256_path(OVERHEAD_DIR / "run.py"),
           "compare:run.py": cmp.sha256_path(COMPARE_DIR / "run.py"), "compare:PROTOCOL.md": cmp.sha256_path(COMPARE_DIR / "PROTOCOL.md")}
    for base, dirs, files in os.walk(str(FIXTURE)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = Path(base) / name
            out["lookup/" + full.relative_to(FIXTURE).as_posix()] = cmp.sha256_path(full)
    return out


def seal_content(seed, commit, digest):
    return {
        "version": 1, "seed": seed, "rounds": ROUNDS, "run_cap_usd": RUN_CAP_USD, "budget_limit_usd": BUDGET_LIMIT_USD,
        "batches": make_batches(seed), "schedule": [list(c) for c in make_schedule(seed)], "warmup_order": warmup_order(seed),
        "calibration_order": calibration_order(seed), "plugin_commit": commit, "plugin_tree_sha256": digest, "files": sealed_files(),
    }


def cmd_seal(args):
    if SEAL_PATH.exists():
        raise Refused("SEAL.json exists: a sealed experiment is never re-sealed; delete the file by hand if no run was made")
    seed = args.seed if args.seed is not None else DEFAULT_SEED
    tmp = Path(tempfile.mkdtemp(prefix="builtin-pin-seal-"))
    try:
        sha, digest = ov.export_commit(args.plugin_repo, args.plugin_commit, tmp / "frozen")
    finally:
        cmp.make_tree_writable(tmp)
        shutil.rmtree(str(tmp), ignore_errors=True)
    data = seal_content(seed, sha, digest)
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
    seed = seal.get("seed")
    if [list(c) for c in make_schedule(seed)] != seal.get("schedule"):
        problems.append("schedule does not follow from the sealed seed")
    if make_batches(seed) != seal.get("batches"):
        problems.append("batches do not follow from the sealed seed")
    if warmup_order(seed) != seal.get("warmup_order") or calibration_order(seed) != seal.get("calibration_order"):
        problems.append("warm-up or calibration order does not follow from the sealed seed")
    return problems


# ---------------------------------------------------------------------------------------------
# ledger, context, setup
# ---------------------------------------------------------------------------------------------

class Ledger(cmp.Ledger):
    def check(self, cap):
        spent = self.spent()
        if spent + cap > BUDGET_LIMIT_USD:
            raise BudgetStop("spent %.2f + cap %.2f would exceed %.2f" % (spent, cap, BUDGET_LIMIT_USD))


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
        self.frozen = None
        self.settings_env = {}
        self.settings_files = []


def setup(args):
    seal = load_seal()
    problems = seal_problems(seal)
    if problems:
        raise Refused("files differ from SEAL.json:\n  " + "\n  ".join(problems[:20]))
    if args.seed is not None and args.seed != seal["seed"]:
        raise Refused("--seed %d differs from the sealed seed %d" % (args.seed, seal["seed"]))
    problems = env_parity_problems()
    if problems:
        raise Refused("arm parity check failed: " + "; ".join(problems))
    settings_env, used = read_settings_env(settings_files())
    if FLAG in settings_env:
        raise Refused("%s is set in the `env` of a Claude Code settings file (%s): it would override the process environment in both arms. "
                      "Remove it for the duration of the experiment." % (FLAG, ", ".join(used)))
    work_root = Path(os.path.abspath(args.work_root))
    out = Path(os.path.abspath(args.out))
    if "/worktrees/" in (os.path.realpath(str(work_root)) + "/"):
        raise Refused("work root must not contain /worktrees/")
    if shutil.which(args.claude) is None:
        raise Refused("claude executable not found: %s" % args.claude)
    work_root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    ctx = Context(args, seal)
    ctx.settings_env, ctx.settings_files = settings_env, used
    sha, digest = ov.export_commit(args.plugin_repo, seal["plugin_commit"], work_root / "_frozen" / "plugin")
    if sha != seal["plugin_commit"] or digest != seal["plugin_tree_sha256"]:
        raise Refused("plugin export (commit %s, sha256 %s) differs from the seal (commit %s, sha256 %s)"
                      % (sha, digest, seal["plugin_commit"], seal["plugin_tree_sha256"]))
    ctx.plugin_dir = str(work_root / "_frozen" / "plugin")
    ctx.frozen = {"commit": sha, "tree_sha256": digest}
    mitosis_settings = {k: v for k, v in settings_env.items() if k.startswith(STRIP_PREFIXES) or k in STRIP_NAMES}
    with open(str(out / "frozen.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(ctx.frozen, settings_files=used, settings_mitosis_env=mitosis_settings), fh, indent=1)
    probe = work_root / "_preflight"
    if probe.exists():
        cmp.remove_inside(probe, work_root)
    ov.scaffold(probe)
    cmp.remove_inside(probe, work_root)
    return ctx


# ---------------------------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------------------------

def remove_debug_session(session_id):
    """Delete the hook's per-session directory in the system temp dir, after its log was copied."""
    path = hook_debug_path(session_id).parent
    if path.parent.name == "mitosis-orchestrator" and path.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path.name):
        shutil.rmtree(str(path), ignore_errors=True)


def execute(ctx, kind, arm, batch_ids, cell_id, cell_dir, cap, scored, round_no=None, batch_no=None):
    """One run: workdir, claude, collect, validity, score, ledger, clean up. Returns the result dict."""
    workdir = ctx.work_root / cell_id
    ctx.ledger.check(cap)
    if workdir.exists():
        cmp.remove_inside(workdir, ctx.work_root)
    ov.scaffold(workdir)
    if cmp.tree_hash(ctx.plugin_dir) != ctx.frozen["tree_sha256"]:
        raise Refused("the frozen plugin tree changed during the experiment")
    cell_dir = Path(cell_dir)
    cell_dir.mkdir(parents=True)
    prompt = build_prompt(batch_ids)
    cmd = build_command(ctx.claude, prompt, ctx.plugin_dir, cap)
    env = arm_env(arm)
    flag = flag_resolution(arm, env, ctx.settings_env)
    mitosis_settings = {k: v for k, v in ctx.settings_env.items() if k.startswith(STRIP_PREFIXES) or k in STRIP_NAMES}
    with open(str(cell_dir / "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": cell_id, "kind": kind, "arm": arm, "cap": cap, "workdir": str(workdir), "batch_ids": batch_ids, "flag": flag,
                   "settings_mitosis_env": mitosis_settings, "argv_without_prompt": [c for c in cmd if c != cmd[2]]}, fh, indent=1)
    ctx.ledger.start(cell_id, kind, cap)
    code, timed_out, wall = run_claude_env(cmd, workdir, cell_dir / "stream.jsonl", cell_dir / "stderr.log", ctx.timeout, env)
    events = cmp.read_events(cell_dir / "stream.jsonl")
    info = cmp.analyse_stream(events)
    cost = parse_cost(info["final"])
    counts, agent_types = primary_tool_counts(events)
    explore_calls = len([t for t in agent_types if is_explore(t)])
    sid = info["session_id"]
    proj = cmp.project_dir_for(workdir, sid, info["transcript_hint"])
    sub_dir = proj / str(sid) / "subagents" if sid else None
    facts = subagent_facts(sub_dir) if sub_dir else []
    leaks = cmp.leak_scan(info["tool_blocks"], workdir, sub_dir)
    log_path = hook_debug_path(sid) if sid else None
    pins, log_entries = pin_decisions(log_path) if log_path else (0, None)
    if log_path and log_path.is_file():
        shutil.copyfile(str(log_path), str(cell_dir / "hook_debug.log"))
    answers, answers_status = load_answers(workdir)
    stdlib_ok = not grade_module().scaffold.verify_tree(str(workdir / "stdlib"))
    with open(str(FIXTURE / "key.json"), encoding="utf-8") as fh:
        key = json.load(fh)
    correct, wrong = score_answers(answers, batch_ids, key)
    known = [c for c in (info["cli_cost"], info["max_cli_cost"], cost["usage_cost_sum"]) if c is not None]
    charged = max(known) if known else float(cap)
    capped = any("budget" in str(s) for s in info["subtypes"]) or charged >= cap
    if capped or timed_out:
        charged = max(charged, float(cap))
    run = {"capped": capped, "timed_out": timed_out, "has_result": info["final"] is not None, "leak_suspect": bool(leaks), "stdlib_ok": stdlib_ok}
    reasons = validity(arm, facts, explore_calls, pins, log_entries, flag, run)
    comp = {t: counts.get(t, 0) for t in COMPENSATION_TOOLS}
    result = {
        "id": cell_id, "kind": kind, "arm": arm, "round": round_no, "batch": batch_no, "batch_ids": batch_ids, "cap": cap, "scored": scored,
        "session_id": sid, "exit_code": code, "timed_out": timed_out, "capped": capped, "wall_s": round(wall, 2),
        "valid": not reasons, "invalid_reasons": reasons,
        "correct": correct, "n_questions": len(batch_ids), "wrong_ids": wrong, "answers_status": answers_status, "stdlib_ok": stdlib_ok,
        "cli_cost": info["cli_cost"], "usage_cost_sum": cost["usage_cost_sum"], "effective_cost": cost["effective"], "cost_disagrees": cost["disagree"],
        "charged": charged, "cost": float(cap) if (capped or timed_out) else (cost["effective"] if cost["effective"] is not None else charged),
        "model_usage": cost["models"], "family_cost": cost["family_cost"], "family_output_tokens": cost["family_output"],
        "compensation_calls": comp, "compensation": sum(comp.values()) > 0, "primary_tool_counts": counts,
        "explore_calls": explore_calls, "agent_types": agent_types, "subagents": facts,
        "flag": flag, "settings_mitosis_env": mitosis_settings, "pin_decisions": pins, "hook_log_entries": log_entries,
        "leak_suspect": bool(leaks), "leak_hits": leaks[:5], "permission_denials": len(info["denials"]), "result_subtypes": info["subtypes"],
    }
    state, state_ok, state_detail = ov.arm_state(info["init"], "loaded")
    result.update(arm_state=state, arm_state_ok=state_ok, arm_state_detail=state_detail)
    with open(str(cell_dir / "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
    ctx.ledger.end(cell_id, kind, charged)
    if sid:
        with open(str(ctx.out / "sessions.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (sid, cell_id, proj))
        if not ctx.keep:
            remove_debug_session(sid)
    if not ctx.keep:
        cmp.remove_inside(workdir, ctx.work_root)
    if not state_ok:
        raise ArmStateError("%s: the plugin was not loaded (%s)" % (cell_id, state_detail))
    return result


def stop_file(ctx, reason, detail):
    with open(str(ctx.out / "STOPPED.json"), "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "detail": str(detail), "spent": ctx.ledger.spent()}, fh, indent=1)


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


def calib_dirs(ctx, arm):
    folder = ctx.out / "calibration"
    return sorted(folder.glob(arm + "__*")) if folder.is_dir() else []


def latest_calibration(ctx, arm):
    """result.json of the newest calibration run of the arm, or None."""
    for d in reversed(calib_dirs(ctx, arm)):
        f = d / "result.json"
        if f.is_file():
            with open(str(f), encoding="utf-8") as fh:
                return json.load(fh)
        return None
    return None


def cmd_calibrate(args):
    ctx = setup(args)
    batch = ctx.seal["batches"][0]
    with RunLock(ctx.out):
        def go():
            for arm in ctx.seal["calibration_order"]:
                res = latest_calibration(ctx, arm)
                if res is not None and res["valid"]:
                    continue
                n = len(calib_dirs(ctx, arm)) + 1
                res = execute(ctx, "calibration", arm, batch, "calib__%s__%d" % (arm, n), ctx.out / "calibration" / ("%s__%d" % (arm, n)), RUN_CAP_USD, False)
                print("[calibrate %s] cost=%.4f correct=%d/%d valid=%s%s (not scored)" % (
                    arm, res["cost"], res["correct"], res["n_questions"], res["valid"], "" if res["valid"] else " " + ",".join(res["invalid_reasons"])))
            return report_projection(ctx)
        return guarded(ctx, go)


def report_projection(ctx):
    """Print the projected total for 36 + 2 runs; exit 5 when it exceeds the budget or a calibration run is not valid."""
    res = {a: latest_calibration(ctx, a) for a in ARMS}
    bad = [a for a in ARMS if res[a] is None or not res[a]["valid"]]
    if bad:
        print("calibration: no valid run for %s; run `calibrate` again (each repeat is charged)" % ", ".join(bad), file=sys.stderr)
        return 5
    spent = ctx.ledger.spent()
    total = project_total(spent, res["pinned"]["cost"], res["inherit"]["cost"])
    print("projected total for %d scored + %d warm-up runs: %.2f USD (spent so far %.2f incl. --spent-before; calibrated cost per run: pinned %.4f, inherit %.4f; budget %.2f)" % (
        EXPECTED_RUNS, len(ARMS), total, spent, res["pinned"]["cost"], res["inherit"]["cost"], BUDGET_LIMIT_USD))
    with open(str(ctx.out / "projection.json"), "w", encoding="utf-8") as fh:
        json.dump({"projected_total": total, "spent": spent, "budget": BUDGET_LIMIT_USD, "ok": total <= BUDGET_LIMIT_USD}, fh, indent=1)
    if total > BUDGET_LIMIT_USD:
        print("projected total %.2f USD exceeds the %.2f USD budget: do not run. Reduce nothing silently; decide with the user." % (total, BUDGET_LIMIT_USD), file=sys.stderr)
        return 5
    return 0


RunLock = ov.RunLock


def cell_dir_of(ctx, cell):
    r, b, a = cell
    return ctx.out / "runs" / ("r%d__b%d__%s" % (r, b, a))


def cmd_run(args):
    ctx = setup(args)
    cells = [tuple(c) for c in ctx.seal["schedule"]]
    for a in ARMS:
        res = latest_calibration(ctx, a)
        if res is None or not res["valid"]:
            raise Refused("no valid calibration run for %s: run `run.py calibrate` first" % a)
    total = project_total(ctx.ledger.spent(), latest_calibration(ctx, "pinned")["cost"], latest_calibration(ctx, "inherit")["cost"])
    if total > BUDGET_LIMIT_USD:
        raise Refused("projected total %.2f USD exceeds the %.2f USD budget" % (total, BUDGET_LIMIT_USD))
    with RunLock(ctx.out):
        warm = [(a, ctx.out / "warmup" / a) for a in ctx.seal["warmup_order"]]
        existing = [d for _, d in warm if d.exists()] + [cell_dir_of(ctx, c) for c in cells if cell_dir_of(ctx, c).exists()]
        if existing and not args.resume:
            raise Refused("results already exist for %d cell(s), no top-up runs (use --resume to continue unrun cells only): %s" % (len(existing), existing[0]))
        for d in existing:
            if not (d / "result.json").is_file():
                print("note: %s was interrupted, stays charged at its cap and stays empty" % d.name)
        ran = [0]

        def go():
            for arm, d in warm:
                if d.exists():
                    continue
                res = execute(ctx, "warmup", arm, ctx.seal["batches"][0], "warmup__" + arm, d, RUN_CAP_USD, False)
                print("[warm-up %s] cost=%.4f valid=%s (not scored)" % (arm, res["cost"], res["valid"]))
            for i, cell in enumerate(cells, 1):
                d = cell_dir_of(ctx, cell)
                if d.exists():
                    continue
                if args.max_runs is not None and ran[0] >= args.max_runs:
                    print("stopped after --max-runs %d" % ran[0])
                    return 0
                r, b, a = cell
                res = execute(ctx, "scored", a, ctx.seal["batches"][b - 1], "r%d__b%d__%s" % (r, b, a), d, RUN_CAP_USD, True, r, b)
                ran[0] += 1
                print("[%d/%d] round %d batch %d %s: %s correct=%d/%d cost=%.4f wall=%.0fs" % (
                    i, len(cells), r, b, a, "valid" if res["valid"] else "INVALID(" + ",".join(res["invalid_reasons"]) + ")",
                    res["correct"], res["n_questions"], res["cost"], res["wall_s"]))
            print("schedule complete (seed %d), spent %.2f" % (ctx.seal["seed"], ctx.ledger.spent()))
            return 0
        return guarded(ctx, go)


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------

def load_runs(out):
    runs = []
    folder = Path(out) / "runs"
    if folder.is_dir():
        for f in sorted(folder.glob("*/result.json")):
            with open(str(f), encoding="utf-8") as fh:
                r = json.load(fh)
            if r.get("scored") and r.get("arm") in ARMS:
                runs.append(r)
    return runs


def record_of(r):
    return {"id": r["id"], "arm": r["arm"], "batch": r["batch"], "round": r["round"], "valid": bool(r["valid"]), "correct": r["correct"],
            "cost": r["cost"], "comp": bool(r["compensation"]), "reasons": r["invalid_reasons"]}


def med(values):
    values = [_frac(v) for v in values]
    return median(values) if values else None


def build_report(out):
    runs = load_runs(out)
    records = [record_of(r) for r in runs]
    decision = decide(records)
    arms = {}
    for a in ARMS:
        rs = [r for r in runs if r["arm"] == a]
        valid = [r for r in rs if r["valid"]]
        arms[a] = {
            "n": len(rs), "valid": len(valid), "correct_all": sum(r["correct"] for r in rs), "correct_valid": sum(r["correct"] for r in valid),
            "median_cost": med([r["cost"] for r in valid]),
            "median_family_cost": {f: med([r["family_cost"].get(f, 0) for r in valid]) for f in FAMILIES + ("other",)},
            "median_family_output": {f: med([r["family_output_tokens"].get(f, 0) for r in valid]) for f in FAMILIES + ("other",)},
            "median_wall_s": med([r["wall_s"] for r in valid]),
            "compensation_runs": [r["id"] for r in rs if r["compensation"]],
            "compensation_calls": {t: sum(r["compensation_calls"].get(t, 0) for r in rs) for t in COMPENSATION_TOOLS},
            "explore_calls": [r["explore_calls"] for r in rs],
            "invalid": {r["id"]: r["invalid_reasons"] for r in rs if not r["valid"]},
            "cost_disagreements": [r["id"] for r in rs if r["cost_disagrees"]],
            "ids": [r["id"] for r in rs],
        }
    pairs = []
    for (b, rnd), p in sorted(pairs_of(records).items()):
        row = {"batch": b, "round": rnd}
        for a in ARMS:
            if a in p:
                row[a] = {"correct": p[a]["correct"], "cost": p[a]["cost"], "valid": p[a]["valid"]}
        pairs.append(row)
    return {"arms": arms, "decision": decision, "pairs": pairs, "n_runs": len(runs)}


def _f(x, nd=4):
    return "n/a" if x is None else "%.*f" % (nd, float(x))


def render_report(rep):
    d = rep["decision"]
    lines = ["== lookup, 6 batches x 3 rounds x 2 arms, opus medium primary, Explore pinned (haiku, low) vs inheriting (opus, medium) =="]
    for a in ARMS:
        s = rep["arms"][a]
        lines.append("  %-8s runs %d, valid %d, correct (valid runs) %d of %d, (all runs) %d; median effective cost %s; median wall %s s" % (
            a, s["n"], s["valid"], s["correct_valid"], 5 * s["valid"], s["correct_all"], _f(s["median_cost"]), _f(s["median_wall_s"], 0)))
        lines.append("           median cost by model family: %s; median output tokens: %s" % (
            ", ".join("%s %s" % (f, _f(s["median_family_cost"][f])) for f in FAMILIES + ("other",)),
            ", ".join("%s %s" % (f, _f(s["median_family_output"][f], 0)) for f in FAMILIES + ("other",))))
        lines.append("           primary's own Read/Grep/Glob/Bash calls: %s in %d run(s) %s" % (s["compensation_calls"], len(s["compensation_runs"]), s["compensation_runs"]))
        lines.append("           Explore calls per run %s; CLI total and modelUsage disagree in: %s" % (s["explore_calls"], ", ".join(s["cost_disagreements"]) or "none"))
        for rid, why in sorted(s["invalid"].items()):
            lines.append("           invalid %s: %s" % (rid, ", ".join(why)))
    lines.append("")
    lines.append("paired by (batch, round), usable pairs %d of %d: A (pinned correct) = %d, B (inherit correct) = %d" % (
        d["n_pairs_usable"], ROUNDS * N_BATCHES, d["A"], d["B"]))
    lines.append("  median cost pinned %s, inherit %s, ratio %s; pinned cheaper in %d pairs" % (
        _f(d["median_pinned"]), _f(d["median_inherit"]), "n/a" if d["ratio"] is None else "%.1f%%" % (float(d["ratio"]) * 100), d["cheaper_pairs"]))
    lines.append("  invalid runs %d of %d; valid pinned runs with compensation reads %d of %d" % (d["n_invalid"], d["n_runs"], d["n_comp"], d["n_pinned_valid"]))
    for row in rep["pairs"]:
        cells = []
        for a in ARMS:
            c = row.get(a)
            cells.append("%s %s/5 %s%s" % (a, "n/a" if c is None else c["correct"], "n/a" if c is None else _f(c["cost"]), "" if c is None or c["valid"] else " (invalid)"))
        lines.append("    batch %d round %d: %s" % (row["batch"], row["round"], "; ".join(cells)))
    lines.append("")
    lines.append("outcome: %s (%s)" % (d["outcome"], d["reason"]))
    lines.append("Rule (PROTOCOL.md): INCONCLUSIVE for >20%% invalid or incomplete runs first; QUALITY LOSS if A <= B-%d or >20%% of pinned runs have compensation reads; "
                 "SUPPORTED if A >= B-%d, pinned median cost <= 70%% of inherit and pinned cheaper in >= %d of 18 pairs; else INCONCLUSIVE." % (QUALITY_MARGIN, SUPPORT_MARGIN, MIN_CHEAPER_PAIRS))
    lines.append("Scope: questions cluster within runs, so this is not a formal non-inferiority test. It measures the shipped bundle (haiku and low effort together), not model or effort alone.")
    lines.append("The default of CLAUDE_MITOSIS_PIN_BUILTINS stays off in 2.7 whatever the outcome; the results are published as numbers.")
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
    ap = argparse.ArgumentParser(description="Built-in pin experiment (see PROTOCOL.md)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("seal")
    p.add_argument("--plugin-commit", required=True)
    p.add_argument("--plugin-repo", default=str(REPO))
    p.add_argument("--seed", type=int)
    for name in ("calibrate", "run"):
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
        return {"seal": cmd_seal, "calibrate": cmd_calibrate, "run": cmd_run, "report": cmd_report}[args.command](args)
    except Refused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
