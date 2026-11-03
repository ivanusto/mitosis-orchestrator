#!/usr/bin/env python3
"""On/off comparison harness for explicit /orchestrate on Claude Code.

    run.py seal  [--seed N] [--plugin-dir DIR]
    run.py pilot --plugin-dir DIR --work-root DIR --out DIR [--spent-before USD]
    run.py run   --plugin-dir DIR --work-root DIR --out DIR [--spent-before USD] [--keep] [--resume]
    run.py report --out DIR

See PROTOCOL.md for the pre-registered design and the verdict rule. Python 3.8, standard library only.
This file is part of the seal: changing it after `seal` makes `run` refuse.

Exit codes: 0 ok, 1 failed pilot check or failed report, 2 refused (seal, tree, existing results, bad
arguments), 4 stopped because the next run could push the budget over the limit.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TASKS = ("lookup", "fix", "ops")
ARMS = ("off", "on")
REPEATS = 3
RUN_CAP_USD = 5.0
PILOT_CAP_USD = 1.5
WARMUP_CAP_USD = 1.5
BUDGET_LIMIT_USD = 60.0
DEFAULT_SEED = 20261103
DEFAULT_TIMEOUT_S = 1800
SETTINGS_JSON = '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}'
PROMPT_PREFIX = "/mitosis-orchestrator:orchestrate "
SEAL_PATH = HERE / "SEAL.json"
SEALED_REPO_FILES = ("scripts/mitosis-cost-report.py", "scripts/mitosis_generated.py")
AGENT_TOOL_NAMES = ("Agent", "Task")
BENEFIT_SAVING = Fraction(25, 100)
HARM_EXTRA = Fraction(10, 100)


class Refused(Exception):
    """The harness will not start or continue; nothing was spent for this reason."""


class BudgetStop(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# verdict rule (PROTOCOL.md). Pure functions, no I/O.
# ---------------------------------------------------------------------------------------------

def _frac(value):
    return Fraction(str(value))


def verdict(on, off):
    """Apply the pre-registered rule. on and off are lists of (success, cost) with REPEATS entries each.

    Failed runs carry their effective cost (the cap for a run that hit the cap or the timeout).
    Returns (label, reason) with label BENEFIT, HARM or INCONCLUSIVE.
    """
    if len(on) != REPEATS or len(off) != REPEATS:
        return "INCONCLUSIVE", "incomplete data: %d on-arm and %d off-arm runs, the rule needs %d each" % (len(on), len(off), REPEATS)
    s_on = sum(1 for ok, _ in on if ok)
    s_off = sum(1 for ok, _ in off if ok)
    c_on = [_frac(c) for _, c in on]
    c_off = [_frac(c) for _, c in off]
    med_on, med_off = median(c_on), median(c_off)
    below = max(c_on) < min(c_off)      # every on-arm cost below every off-arm cost
    above = min(c_on) > max(c_off)      # every on-arm cost above every off-arm cost
    saving_ok = (med_off - med_on) >= BENEFIT_SAVING * med_off
    extra_ok = (med_on - med_off) >= HARM_EXTRA * med_off
    if s_on < s_off:
        return "HARM", "fewer on-arm successes (%d vs %d)" % (s_on, s_off)
    if above and extra_ok:
        return "HARM", "complete separation with the on arm at least 10%% more costly (median %s vs %s)" % (_money(med_on), _money(med_off))
    if below and saving_ok:
        return "BENEFIT", "successes %d vs %d, complete separation, median saving %s%%" % (s_on, s_off, _pct(med_off - med_on, med_off))
    why = []
    if not below:
        why.append("costs not completely separated in the on arm's favour")
    elif not saving_ok:
        why.append("median saving %s%% is below 25%%" % _pct(med_off - med_on, med_off))
    return "INCONCLUSIVE", "; ".join(why) or "no rule met"


def _money(x):
    return "%.4f" % float(x)


def _pct(num, den):
    return "n/a" if den == 0 else "%.1f" % (float(num / den) * 100)


def overall_actions(verdicts):
    """verdicts: dict task -> label. Returns a list of action lines."""
    benefit = [t for t, v in sorted(verdicts.items()) if v == "BENEFIT"]
    harm = [t for t, v in sorted(verdicts.items()) if v == "HARM"]
    lines = []
    if harm:
        lines.append("HARM on %s: the skill excludes those task types." % ", ".join(harm))
    if len(benefit) >= 2 and not harm:
        lines.append("Two or more BENEFIT and no HARM: 2.5 invests in dispatch rate.")
    else:
        lines.append("The README stops claiming savings and only claims what was measured.")
    return lines


# ---------------------------------------------------------------------------------------------
# schedule, seal
# ---------------------------------------------------------------------------------------------

def _order_key(seed, *parts):
    return hashlib.sha256((":".join([str(seed)] + [str(p) for p in parts])).encode("utf-8")).hexdigest()


def make_schedule(seed, tasks=TASKS, arms=ARMS, repeats=REPEATS):
    """List of (repeat, task, arm); the order inside each repeat block is a seeded shuffle."""
    cells = []
    for r in range(1, repeats + 1):
        block = sorted(((t, a) for t in tasks for a in arms), key=lambda ta: _order_key(seed, r, ta[0], ta[1]))
        cells.extend((r, t, a) for t, a in block)
    return cells


def warmup_order(seed):
    return sorted(ARMS, key=lambda a: _order_key(seed, "warmup", a))


def sha256_path(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sealed_files():
    """Map of label -> sha256 for every file the experiment depends on."""
    out = {}
    for base, dirs, files in os.walk(str(HERE)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name == "SEAL.json" or name.endswith(".pyc"):
                continue
            full = Path(base) / name
            out[full.relative_to(HERE).as_posix()] = sha256_path(full)
    for rel in SEALED_REPO_FILES:
        out["repo:" + rel] = sha256_path(REPO / rel)
    return out


def git(args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def plugin_head(plugin_dir):
    proc = git(["rev-parse", "HEAD"], plugin_dir)
    return proc.stdout.strip() if proc.returncode == 0 else None


def tree_hash(root):
    """sha256 over sorted relative paths and file contents (symlinks by target); independent of mtimes and ownership."""
    h = hashlib.sha256()
    root = str(root)
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(dirs)
        for name in sorted(files + [d for d in dirs if os.path.islink(os.path.join(base, d))]):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                h.update(("%s\0link:%s\n" % (rel, os.readlink(full))).encode("utf-8"))
            else:
                h.update(("%s\0%s\n" % (rel, sha256_path(full))).encode("utf-8"))
    return h.hexdigest()


def export_frozen(plugin_dir, dest):
    """Export HEAD of the plugin repository with `git archive` into dest, leaving out evals/ entirely.

    Returns (commit, tree_sha256). The on arm sees only this export, so answer keys and hidden tests,
    which live under evals/compare, are not in any path the primary can read from --plugin-dir.
    """
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        raise Refused("plugin tree not found: %s" % plugin_dir)
    commit = plugin_head(plugin_dir)
    if commit is None:
        raise Refused("plugin tree is not a git checkout, so it cannot be exported by commit: %s" % plugin_dir)
    proc = subprocess.run(["git", "archive", "--format=tar", commit], cwd=str(plugin_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise Refused("git archive failed: %s" % proc.stderr.decode("utf-8", "replace")[:300])
    dest = Path(dest)
    if dest.exists():
        remove_inside(dest, dest.parent)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar.getmembers():
            name = member.name.lstrip("./")
            if not name or name == "evals" or name.startswith("evals/") or name == "pax_global_header":
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise Refused("unsafe path in archive: " + member.name)
            target = dest / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(str(target), "wb") as out:
                    out.write(tar.extractfile(member).read())
                os.chmod(str(target), member.mode & 0o777 or 0o644)
            elif member.issym():
                if member.linkname.startswith("/") or ".." in member.linkname.split("/"):
                    raise Refused("unsafe symlink in archive: " + member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, str(target))
    return commit, tree_hash(dest)


def sealed_plugin_values(plugin_dir):
    """Commit and export hash of the plugin tree as it would be frozen now."""
    tmp = Path(tempfile.mkdtemp(prefix="compare-seal-"))
    try:
        return export_frozen(plugin_dir, tmp / "frozen")
    finally:
        make_tree_writable(tmp)
        shutil.rmtree(str(tmp), ignore_errors=True)


def cmd_seal(args):
    seed = args.seed if args.seed is not None else DEFAULT_SEED
    data = {
        "version": 1,
        "seed": seed,
        "repeats": REPEATS,
        "run_cap_usd": RUN_CAP_USD,
        "budget_limit_usd": BUDGET_LIMIT_USD,
        "warmup_order": warmup_order(seed),
        "schedule": [list(c) for c in make_schedule(seed)],
        "plugin_head": None,
        "plugin_tree_sha256": None,
        "files": sealed_files(),
    }
    if args.plugin_dir:
        data["plugin_head"], data["plugin_tree_sha256"] = sealed_plugin_values(args.plugin_dir)
    with open(str(SEAL_PATH), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("sealed %d files, seed %d, plugin head %s, export %s" % (len(data["files"]), seed, data["plugin_head"], data["plugin_tree_sha256"]))
    return 0


def load_seal():
    if not SEAL_PATH.is_file():
        raise Refused("no SEAL.json: run `run.py seal` after reviewing the fixtures")
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
    return problems


# ---------------------------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------------------------

class Ledger:
    """Append-only JSON lines. A started run is charged at its cap until its end line replaces that."""

    def __init__(self, out, spent_before):
        self.path = Path(out) / "ledger.jsonl"
        self.spent_before = float(spent_before)

    def _append(self, entry):
        with open(str(self.path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def start(self, cell_id, kind, cap):
        self._append({"id": cell_id, "kind": kind, "event": "start", "charged": float(cap)})

    def end(self, cell_id, kind, charged):
        self._append({"id": cell_id, "kind": kind, "event": "end", "charged": float(charged)})

    def spent(self):
        last = {}
        if self.path.is_file():
            with open(str(self.path), encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        e = json.loads(line)
                        last[e["id"]] = e["charged"]
        return self.spent_before + sum(last.values())

    def check(self, cap):
        spent = self.spent()
        if spent + cap > BUDGET_LIMIT_USD:
            raise BudgetStop("spent %.2f + cap %.2f would exceed %.2f" % (spent, cap, BUDGET_LIMIT_USD))


# ---------------------------------------------------------------------------------------------
# tasks and one run
# ---------------------------------------------------------------------------------------------

def read_allowed_tools(task):
    rules = []
    with open(str(HERE / "tasks" / task / "allowed_tools.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append(line)
    return rules


def read_prompt(task):
    with open(str(HERE / "tasks" / task / "prompt.md"), encoding="utf-8") as fh:
        return fh.read().strip() + "\n"


def build_command(claude, arm, task, prompt, plugin_dir, cap):
    full = (PROMPT_PREFIX + prompt) if arm == "on" else prompt
    cmd = [
        claude, "-p", full,
        "--model", "opus", "--effort", "medium",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(read_allowed_tools(task)),
        "--max-budget-usd", "%g" % cap,
        "--output-format", "stream-json", "--verbose",
        "--settings", SETTINGS_JSON,
    ]
    if arm == "on":
        cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


def make_tree_writable(root):
    for base, dirs, files in os.walk(str(root)):
        try:
            os.chmod(base, stat.S_IRWXU)
        except OSError:
            pass
        for name in files:
            try:
                os.chmod(os.path.join(base, name), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


def remove_inside(path, root):
    """Delete path only if it is strictly inside root."""
    real, real_root = os.path.realpath(str(path)), os.path.realpath(str(root))
    if not real.startswith(real_root + os.sep):
        raise Refused("refusing to delete outside %s: %s" % (root, path))
    if os.path.exists(real):
        make_tree_writable(real)
        shutil.rmtree(real, ignore_errors=True)


def read_events(path):
    events = []
    with open(str(path), encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def analyse_stream(events):
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    results = [e for e in events if e.get("type") == "result"]
    final = results[-1] if results else None
    tool_ids, agent_ids = set(), set()
    tool_blocks = []
    peak = 0
    for e in events:
        if e.get("type") != "assistant" or not isinstance(e.get("message"), dict):
            continue
        message = e["message"]
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                key = block.get("id") or id(block)
                if key not in tool_ids:
                    tool_blocks.append(block)
                tool_ids.add(key)
                if block.get("name") in AGENT_TOOL_NAMES:
                    agent_ids.add(key)
        if not e.get("parent_tool_use_id"):
            usage = message.get("usage") or {}
            ctx = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            peak = max(peak, ctx)
    denials = list((final or {}).get("permission_denials") or [])
    bash_calls = len([b for b in tool_blocks if b.get("name") == "Bash"])
    costs = [float(r["total_cost_usd"]) for r in results if isinstance(r.get("total_cost_usd"), (int, float))]
    return {
        "init": init,
        "results": results,
        "final": final,
        "session_id": (final or {}).get("session_id") or (init or {}).get("session_id"),
        "cli_cost": float((final or {}).get("total_cost_usd")) if isinstance((final or {}).get("total_cost_usd"), (int, float)) else None,
        "max_cli_cost": max(costs) if costs else None,
        "subtypes": [r.get("subtype") for r in results],
        "tool_calls": len(tool_ids),
        "agent_calls": len(agent_ids),
        "peak_context_tokens": peak,
        "denials": denials,
        "bash_calls": bash_calls,
        "tool_blocks": tool_blocks,
        "transcript_hint": (init or {}).get("transcript_path"),
    }


LEAK_PATTERNS = (
    re.compile(r"evals/compare"),
    re.compile(r"key\.json"),
    re.compile(r"hidden/|/hidden\b|test_hidden"),
    re.compile(r"(?<![A-Za-z0-9_])reference/"),
)


def _block_texts(blocks, workdir):
    for block in blocks:
        text = json.dumps(block.get("input", {}), sort_keys=True)
        yield block.get("name"), re.sub(re.escape(str(workdir)) + r"[^\s\"']*", "<workdir>", text)


def leak_scan(tool_blocks, workdir, subagent_dir=None):
    """Tool inputs that mention answer keys, hidden tests or reference solutions (paths inside the working dir are masked)."""
    blocks = list(tool_blocks)
    if subagent_dir and Path(subagent_dir).is_dir():
        for f in sorted(Path(subagent_dir).glob("agent-*.jsonl")):
            for rec in read_events(f):
                content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else None
                for b in content or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        blocks.append(b)
    hits = []
    for name, text in _block_texts(blocks, os.path.realpath(str(workdir))):
        for pat in LEAK_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append("%s: %s" % (name, text[max(0, m.start() - 30):m.end() + 30]))
    return hits


def denial_reason(d):
    if not isinstance(d, dict):
        return str(d)[:200]
    parts = [str(d[k]) for k in ("decision_reason_type", "decision_reason", "reason", "message") if d.get(k)]
    return " | ".join(parts)[:300]


CONFIG_REASON = re.compile(r"\b(mode|rules?|allowedtools|allowlist|allow list|deny list|settings)\b", re.I)


def projects_root():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "projects"


def encode_cwd(path):
    """Claude Code names a project directory after the cwd with every non-alphanumeric character as `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_dir_for(workdir, session_id=None, hint=None):
    """The transcript directory of a run: the path the stream names, else the encoded cwd, else a search by session id."""
    if hint and os.path.isfile(str(hint)):
        return Path(hint).parent
    root = projects_root()
    encoded = root / encode_cwd(os.path.realpath(str(workdir)))
    if session_id and not (encoded / (session_id + ".jsonl")).is_file() and root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / (session_id + ".jsonl")).is_file():
                return d
    return encoded


def transcript_cost(session_id, workdir, hint=None):
    """Cost from transcripts via scripts/mitosis-cost-report.py. Returns (dict, error)."""
    if not session_id:
        return None, "no session id"
    script = REPO / "scripts" / "mitosis-cost-report.py"
    proc = subprocess.run([sys.executable, str(script), "--project", str(project_dir_for(workdir, session_id, hint)), "--session", session_id, "--json"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=180)
    if proc.returncode != 0:
        return None, "cost report exit %d: %s" % (proc.returncode, proc.stderr.strip()[:300])
    try:
        summary = json.loads(proc.stdout)[0]
    except (ValueError, IndexError):
        return None, "cost report output not JSON"
    subs = sorted({r["model"] for r in summary.get("rows", []) if r.get("role") == "subagent"})
    return {
        "total_usd": summary.get("total_usd"),
        "primary_usd": (summary.get("totals_usd") or {}).get("primary"),
        "subagent_usd": (summary.get("totals_usd") or {}).get("subagent"),
        "subagent_models": subs,
        "subagent_files": summary.get("subagent_files"),
        "unknown_models": summary.get("unknown_models"),
    }, None


class Context:
    def __init__(self, args, seal=None):
        self.claude = args.claude
        self.plugin_dir = os.path.abspath(args.plugin_dir)
        self.frozen = None
        self.work_root = Path(os.path.abspath(args.work_root))
        self.out = Path(os.path.abspath(args.out))
        self.keep = args.keep
        self.timeout = args.timeout
        self.ledger = Ledger(self.out, args.spent_before)
        self.seal = seal


def scaffold(task, workdir):
    proc = subprocess.run([sys.executable, str(HERE / "tasks" / task / "scaffold.py"), str(workdir)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
    if proc.returncode != 0:
        raise Refused("scaffold for %s failed (exit %d): %s" % (task, proc.returncode, proc.stderr.strip()[:600]))


def run_claude(cmd, workdir, stream_path, stderr_path, timeout):
    """Run the CLI with stdin from /dev/null. Returns (exit_code, timed_out, wall_seconds)."""
    started = time.monotonic()
    timed_out = False
    with open(str(stream_path), "wb") as out, open(str(stderr_path), "wb") as err:
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            code = proc.wait()
    return code, timed_out, time.monotonic() - started


def execute(ctx, kind, task, arm, cell_id, cell_dir, cap):
    """One run: scaffold, claude, collect, cost, grade, ledger, clean up. Returns the result dict."""
    workdir = ctx.work_root / cell_id
    if "/worktrees/" in (os.path.realpath(str(ctx.work_root)) + "/"):
        raise Refused("work root must not contain /worktrees/")
    ctx.ledger.check(cap)
    if workdir.exists():
        remove_inside(workdir, ctx.work_root)
    scaffold(task, workdir)
    if arm == "on" and ctx.frozen and tree_hash(ctx.plugin_dir) != ctx.frozen["tree_sha256"]:
        raise Refused("the frozen plugin tree changed during the experiment")
    cell_dir = Path(cell_dir)
    cell_dir.mkdir(parents=True)
    prompt = read_prompt(task)
    cmd = build_command(ctx.claude, arm, task, prompt, ctx.plugin_dir, cap)
    with open(str(cell_dir / "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": cell_id, "kind": kind, "task": task, "arm": arm, "cap": cap, "workdir": str(workdir), "argv_without_prompt": [c for c in cmd if c != cmd[2]]}, fh, indent=1)
    ctx.ledger.start(cell_id, kind, cap)
    code, timed_out, wall = run_claude(cmd, workdir, cell_dir / "stream.jsonl", cell_dir / "stderr.log", ctx.timeout)
    info = analyse_stream(read_events(cell_dir / "stream.jsonl"))
    with open(str(cell_dir / "result_events.json"), "w", encoding="utf-8") as fh:
        json.dump(info["results"], fh, indent=1)
    tcost, terr = transcript_cost(info["session_id"], workdir, info["transcript_hint"])
    proj = project_dir_for(workdir, info["session_id"], info["transcript_hint"])
    leaks = leak_scan(info["tool_blocks"], workdir, proj / str(info["session_id"]) / "subagents" if info["session_id"] else None)
    grade = subprocess.run([sys.executable, str(HERE / "tasks" / task / "grade.py"), str(workdir)], cwd=str(cell_dir),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
    with open(str(cell_dir / "grade.txt"), "w", encoding="utf-8") as fh:
        fh.write(grade.stdout[-4000:])
    known = [c for c in (info["cli_cost"], info["max_cli_cost"], (tcost or {}).get("total_usd")) if c is not None]
    charged = max(known) if known else float(cap)
    capped = any("budget" in str(s) for s in info["subtypes"]) or charged >= cap
    if capped or timed_out:
        charged = max(charged, float(cap))
    success = grade.returncode == 0 and not capped and not timed_out and not leaks
    sid = info["session_id"]
    result = {
        "id": cell_id, "kind": kind, "task": task, "arm": arm, "cap": cap,
        "session_id": sid, "transcript_main": str(proj / (str(sid) + ".jsonl")) if sid else None,
        "exit_code": code, "timed_out": timed_out, "capped": capped, "wall_s": round(wall, 2),
        "grade_exit": grade.returncode, "success": success,
        "cli_cost": info["cli_cost"], "transcript_cost": (tcost or {}).get("total_usd"),
        "transcript_primary_cost": (tcost or {}).get("primary_usd"), "transcript_subagent_cost": (tcost or {}).get("subagent_usd"),
        "cost_report_error": terr, "charged": charged,
        "cost": float(cap) if (capped or timed_out) else charged,
        "peak_context_tokens": info["peak_context_tokens"], "tool_calls": info["tool_calls"], "agent_calls": info["agent_calls"],
        "subagent_models": (tcost or {}).get("subagent_models"), "subagent_files": (tcost or {}).get("subagent_files"),
        "permission_denials": len(info["denials"]), "permission_denial_tools": [str(d.get("tool_name")) for d in info["denials"] if isinstance(d, dict)],
        "permission_denial_reasons": [denial_reason(d) for d in info["denials"]], "bash_calls": info["bash_calls"],
        "leak_suspect": bool(leaks), "leak_hits": leaks[:5],
        "n_result_events": len(info["results"]), "result_subtypes": info["subtypes"],
    }
    with open(str(cell_dir / "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
    ctx.ledger.end(cell_id, kind, charged)
    if sid:
        with open(str(ctx.out / "sessions.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (sid, cell_id, proj))
    if not ctx.keep:
        remove_inside(workdir, ctx.work_root)
    result["_info"] = info
    return result


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------

def common_setup(args, need_seal):
    seal = None
    if need_seal:
        seal = load_seal()
        problems = seal_problems(seal)
        if problems:
            raise Refused("fixtures differ from SEAL.json:\n  " + "\n  ".join(problems[:20]))
        if args.seed is not None and args.seed != seal["seed"]:
            raise Refused("--seed %d differs from the sealed seed %d" % (args.seed, seal["seed"]))
    work_root = Path(os.path.abspath(args.work_root))
    out = Path(os.path.abspath(args.out))
    if "/worktrees/" in (os.path.realpath(str(work_root)) + "/"):
        raise Refused("work root must not contain /worktrees/")
    if shutil.which(args.claude) is None:
        raise Refused("claude executable not found: %s" % args.claude)
    work_root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    ctx = Context(args, seal)
    dirty = git(["status", "--porcelain", "--untracked-files=no"], args.plugin_dir)
    if dirty.returncode == 0 and dirty.stdout.strip():
        print("note: the plugin checkout has uncommitted changes; the frozen export is built from the commit, not from them")
    commit, digest = export_frozen(args.plugin_dir, work_root / "_frozen" / "plugin")
    if seal:
        if not seal.get("plugin_tree_sha256"):
            raise Refused("the seal did not record a plugin tree: run `run.py seal --plugin-dir DIR`")
        if commit != seal.get("plugin_head") or digest != seal["plugin_tree_sha256"]:
            raise Refused("plugin export (commit %s, sha256 %s) differs from the seal (commit %s, sha256 %s)"
                          % (commit, digest, seal.get("plugin_head"), seal["plugin_tree_sha256"]))
    ctx.plugin_dir = str(work_root / "_frozen" / "plugin")
    ctx.frozen = {"commit": commit, "tree_sha256": digest}
    with open(str(out / "frozen.json"), "w", encoding="utf-8") as fh:
        json.dump(ctx.frozen, fh, indent=1)
    return ctx


def preflight_scaffolds(ctx):
    for task in TASKS:
        probe = ctx.work_root / ("_preflight_" + task)
        if probe.exists():
            remove_inside(probe, ctx.work_root)
        scaffold(task, probe)
        remove_inside(probe, ctx.work_root)


def cell_dir_of(ctx, cell):
    r, t, a = cell
    return ctx.out / "runs" / ("%s__%s__r%d" % (t, a, r))


def cmd_run(args):
    ctx = common_setup(args, need_seal=True)
    seed = ctx.seal["seed"]
    cells = [tuple(c) for c in ctx.seal["schedule"]]
    warm = ["warmup__" + a for a in ctx.seal["warmup_order"]]
    existing = [str(cell_dir_of(ctx, c)) for c in cells if cell_dir_of(ctx, c).exists()]
    existing += [str(ctx.out / "warmup" / w.split("__")[1]) for w in warm if (ctx.out / "warmup" / w.split("__")[1]).exists()]
    if existing and not args.resume:
        raise Refused("results already exist for %d cell(s), no top-up runs (use --resume to continue unrun cells only): %s"
                      % (len(existing), existing[0]))
    for path in existing:
        if not os.path.isfile(os.path.join(path, "result.json")):
            raise Refused("a previous run was interrupted and left %s without a result; it was charged at its cap and cannot be redone" % path)
    preflight_scaffolds(ctx)
    ran = 0
    try:
        for w in warm:
            arm = w.split("__")[1]
            if (ctx.out / "warmup" / arm).exists():
                continue
            res = execute(ctx, "warmup", "fix", arm, w, ctx.out / "warmup" / arm, WARMUP_CAP_USD)
            print("[warm-up %s] cost=%.4f (not scored)" % (arm, res["charged"]))
        for i, cell in enumerate(cells, 1):
            if cell_dir_of(ctx, cell).exists():
                continue
            if args.max_runs is not None and ran >= args.max_runs:
                print("stopped after --max-runs %d" % ran)
                return 0
            r, t, a = cell
            res = execute(ctx, "scored", t, a, "%s__%s__r%d" % (t, a, r), cell_dir_of(ctx, cell), RUN_CAP_USD)
            ran += 1
            print("[%d/%d] %s %s r%d: %s cost=%.4f wall=%.0fs" % (i, len(cells), t, a, r, "success" if res["success"] else "FAILURE", res["cost"], res["wall_s"]))
    except BudgetStop as exc:
        with open(str(ctx.out / "STOPPED.json"), "w", encoding="utf-8") as fh:
            json.dump({"reason": "budget", "detail": str(exc), "spent": ctx.ledger.spent()}, fh, indent=1)
        print("stopped cleanly: " + str(exc))
        return 4
    print("schedule complete (seed %d), spent %.2f" % (seed, ctx.ledger.spent()))
    return 0


# ---------------------------------------------------------------------------------------------
# pilot
# ---------------------------------------------------------------------------------------------

INIT_VOLATILE = ("session_id", "uuid", "cwd", "timestamp")
INIT_MITOSIS_LISTS = ("plugins", "agents", "skills", "slash_commands")


def _items(value):
    return {(x if isinstance(x, str) else json.dumps(x, sort_keys=True)) for x in (value or [])}


def _is_mitosis(text):
    return "mitosis" in text.lower() or "enforce-orchestrator" in text.lower()


def pin_families(plugin_dir):
    """Model families the plugin's Claude subagents are pinned to (front matter `model:`)."""
    fams = set()
    folder = Path(plugin_dir) / "claude-agents"
    if folder.is_dir():
        for md in folder.glob("*.md"):
            m = re.search(r"^model:\s*(\S+)\s*$", md.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                fams.add(m.group(1).strip().lower())
    return fams


def pilot_checks(off, on, plugin_dir):
    """off/on: dicts with result, info (analyse_stream output) and transcript_text. on may be None.

    Returns a list of (status, name, detail) with status PASS, FAIL or UNKNOWN.
    """
    out = []
    off_init = off["info"]["init"]
    # 1. init events differ only in mitosis items
    name = "init events differ only in mitosis items"
    if on is None:
        out.append(("UNKNOWN", name, "on arm not run yet"))
    elif off_init is None or on["info"]["init"] is None:
        out.append(("UNKNOWN", name, "an init event is missing"))
    else:
        on_init = on["info"]["init"]
        bad = []
        for key in sorted((set(off_init) | set(on_init)) - set(INIT_VOLATILE) - set(INIT_MITOSIS_LISTS)):
            if off_init.get(key) != on_init.get(key):
                bad.append("key %s differs" % key)
        for key in INIT_MITOSIS_LISTS:
            diff = _items(off_init.get(key)) ^ _items(on_init.get(key))
            bad += ["%s: non-mitosis item differs: %s" % (key, d[:80]) for d in sorted(diff) if not _is_mitosis(d)]
        out.append(("FAIL", name, "; ".join(bad)) if bad else ("PASS", name, "only mitosis items differ"))
    # 2. off arm: no mitosis hooks firing
    name = "off arm has no mitosis hooks firing"
    leaked = [k for k in INIT_MITOSIS_LISTS if any(_is_mitosis(i) for i in _items((off_init or {}).get(k)))]
    hook_events = [e for e in off["info"].get("events", []) if e.get("type") == "system" and str(e.get("subtype", "")).startswith("hook")]
    mitosis_hooks = [e for e in hook_events if _is_mitosis(json.dumps(e))]
    other_mitosis = [e for e in off["info"].get("events", []) if e.get("subtype") != "init" and _is_mitosis(json.dumps(e))]
    if leaked or mitosis_hooks:
        out.append(("FAIL", name, "mitosis present in off arm: %s" % (", ".join(leaked) or "%d hook events" % len(mitosis_hooks))))
    elif hook_events:
        out.append(("PASS", name, "%d hook events seen, none from mitosis" % len(hook_events)))
    elif other_mitosis:
        out.append(("UNKNOWN", name, "hook events are not in the stream and %d events mention mitosis" % len(other_mitosis)))
    else:
        out.append(("UNKNOWN", name, "hook events are not in the stream; only absence of mitosis items and strings is shown"))
    # 3. on arm skill expanded under -p
    name = "on arm skill expanded under -p"
    if on is None:
        out.append(("UNKNOWN", name, "on arm not run yet"))
    else:
        marker = _skill_marker(plugin_dir)
        text = on.get("transcript_text")
        blob = json.dumps(on["info"].get("events", []))
        if marker and (marker in (text or "") or marker in blob):
            out.append(("PASS", name, "skill body marker found: %r" % marker))
        elif text is None:
            out.append(("UNKNOWN", name, "no transcript to inspect and no marker %r in the stream" % marker))
        else:
            out.append(("FAIL", name, "marker %r not found in the transcript; the slash command may not have expanded" % marker))
    # 4. Bash rules work, 5. denials. Claude Code's own limits on model-written compound commands are not
    # a misconfiguration: FAIL only when Bash could not run at all or a denial names the mode or the rules.
    for label, run in (("off", off), ("on", on)):
        if run is None:
            continue
        res = run["result"]
        reasons = res.get("permission_denial_reasons") or []
        config = [r for r in reasons if CONFIG_REASON.search(r)]
        bash_total = res.get("bash_calls", 0)
        denied_bash = len([t for t in res["permission_denial_tools"] if t == "Bash"])
        note = "%d denial(s)%s" % (len(reasons), (": " + "; ".join(r[:80] for r in reasons[:3])) if reasons else "")
        name = "%s arm: Bash allow rules work" % label
        if bash_total and denied_bash >= bash_total:
            out.append(("FAIL", name, "every Bash call was denied (%d of %d)" % (denied_bash, bash_total)))
        elif config:
            out.append(("FAIL", name, "a denial names the mode or the rules: %s" % config[0][:120]))
        elif not bash_total:
            out.append(("UNKNOWN", name, "the run made no Bash call"))
        else:
            out.append(("PASS", name, "%d Bash calls, %d denied (%s)" % (bash_total, denied_bash, note)))
        name = "%s arm: permission denials" % label
        if res["n_result_events"] == 0:
            out.append(("UNKNOWN", name, "no result event"))
        elif config:
            out.append(("FAIL", name, "a denial names the mode or the rules: %s" % config[0][:120]))
        elif reasons:
            out.append(("PASS", name, "%s; recorded, not a misconfiguration" % note))
        else:
            out.append(("PASS", name, "none"))
    # 6. subagents ran with the pinned models; 7. does the CLI total include subagents
    if on is None:
        out.append(("UNKNOWN", "on arm: subagents ran with pinned models", "on arm not run yet"))
        out.append(("UNKNOWN", "CLI total includes subagent cost", "on arm not run yet"))
    else:
        res = on["result"]
        name = "on arm: subagents ran with pinned models"
        fams = pin_families(plugin_dir)
        models = res.get("subagent_models") or []
        if res["agent_calls"] == 0:
            out.append(("FAIL", name, "the on arm made no Agent call"))
        elif not models:
            out.append(("UNKNOWN", name, "Agent calls made but no subagent models in the transcripts (%s)" % (res.get("cost_report_error") or "no subagent files")))
        elif not fams:
            out.append(("UNKNOWN", name, "no pinned families found in claude-agents/*.md; models seen: %s" % ", ".join(models)))
        else:
            off_pin = [m for m in models if not any(f in m.lower() for f in fams)]
            out.append(("FAIL", name, "models outside the pin %s: %s" % (sorted(fams), ", ".join(off_pin))) if off_pin
                       else ("PASS", name, "models %s match pins %s" % (", ".join(models), sorted(fams))))
        name = "CLI total includes subagent cost"
        cli, total, sub = res.get("cli_cost"), res.get("transcript_cost"), res.get("transcript_subagent_cost")
        if cli is None or not total or not sub:
            out.append(("UNKNOWN", name, "need a CLI cost and transcript costs with subagent spend (cli=%s transcript=%s subagent=%s)" % (cli, total, sub)))
        else:
            prim = res.get("transcript_primary_cost") or 0.0
            if abs(cli - total) <= 0.05 * total:
                out.append(("PASS", name, "yes: CLI %.4f is within 5%% of the transcript total %.4f (subagents %.4f)" % (cli, total, sub)))
            elif abs(cli - prim) <= 0.05 * max(prim, 1e-9):
                out.append(("PASS", name, "no: CLI %.4f matches the primary only (%.4f), transcript total %.4f; the harness charges the larger" % (cli, prim, total)))
            else:
                out.append(("UNKNOWN", name, "CLI %.4f matches neither the transcript total %.4f nor the primary %.4f" % (cli, total, prim)))
    return out


def _skill_marker(plugin_dir):
    path = Path(plugin_dir) / "skills" / "orchestrate" / "SKILL.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# "):
            return line[2:].split(" (")[0].strip()
    return None


def read_text_or_none(path):
    try:
        with open(str(path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, TypeError):
        return None


def print_checks(checks):
    for status, name, detail in checks:
        print("%-7s %s: %s" % (status, name, detail))


def cmd_pilot(args):
    ctx = common_setup(args, need_seal=False)
    try:
        problems = seal_problems(load_seal())
        print("note: fixtures match SEAL.json" if not problems else "note: fixtures differ from SEAL.json (%d), `run` will refuse until re-sealed" % len(problems))
    except Refused as exc:
        print("note: " + str(exc))
    for arm in ARMS:
        if (ctx.out / "pilot" / arm).exists():
            raise Refused("pilot results already exist in %s; use a new --out for another pilot" % ctx.out)
    runs = {}
    try:
        for arm in ("off", "on"):
            res = execute(ctx, "pilot", "fix", arm, "pilot__" + arm, ctx.out / "pilot" / arm, args.pilot_cap)
            info = res.pop("_info")
            info["events"] = read_events(ctx.out / "pilot" / arm / "stream.jsonl")
            runs[arm] = {"result": res, "info": info, "transcript_text": read_text_or_none(res["transcript_main"])}
            print("[pilot %s] success=%s cost=%.4f wall=%.0fs" % (arm, res["success"], res["cost"], res["wall_s"]))
            checks = pilot_checks(runs["off"], runs.get("on"), ctx.plugin_dir)
            if arm == "off":
                partial = [c for c in checks if c[0] == "FAIL"]
                if partial:
                    print_checks(checks)
                    print("pilot stopped after the off arm: a check failed")
                    return 1
        print_checks(checks)
    except BudgetStop as exc:
        print("stopped cleanly: " + str(exc))
        return 4
    if any(c[0] == "FAIL" for c in checks):
        print("pilot: FAIL")
        return 1
    print("pilot: no FAIL (%d UNKNOWN)" % sum(1 for c in checks if c[0] == "UNKNOWN"))
    return 0


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------

def load_results(out):
    runs = {}
    folder = Path(out) / "runs"
    if folder.is_dir():
        for sub in sorted(folder.iterdir()):
            f = sub / "result.json"
            if f.is_file():
                with open(str(f), encoding="utf-8") as fh:
                    r = json.load(fh)
                runs.setdefault(r["task"], {}).setdefault(r["arm"], []).append(r)
    return runs


def med(values):
    return float(median(values)) if values else None


def build_report(out):
    runs = load_results(out)
    tasks = {}
    for task in TASKS:
        by_arm = runs.get(task, {})
        row = {"arms": {}}
        for arm in ARMS:
            rs = sorted(by_arm.get(arm, []), key=lambda r: r["id"])
            row["arms"][arm] = {
                "n": len(rs), "successes": sum(1 for r in rs if r["success"]),
                "costs": [r["cost"] for r in rs], "median_cost": med([r["cost"] for r in rs]),
                "median_peak_context": med([r["peak_context_tokens"] for r in rs]),
                "median_wall_s": med([r["wall_s"] for r in rs]),
                "agent_calls": [r["agent_calls"] for r in rs], "ids": [r["id"] for r in rs],
                "denials": [r.get("permission_denials", 0) for r in rs],
                "denial_reasons": [r.get("permission_denial_reasons", []) for r in rs],
                "leak_suspects": [r["id"] for r in rs if r.get("leak_suspect")],
            }
        row["verdict"], row["reason"] = verdict([(r["success"], r["cost"]) for r in by_arm.get("on", [])],
                                                [(r["success"], r["cost"]) for r in by_arm.get("off", [])])
        tasks[task] = row
    labels = {t: tasks[t]["verdict"] for t in tasks}
    return {"tasks": tasks, "actions": overall_actions(labels)}


def render_report(report, ledger_total=None):
    lines = []
    for task, row in report["tasks"].items():
        lines.append("== %s ==" % task)
        for arm in ARMS:
            a = row["arms"][arm]
            costs = ", ".join("%.4f" % c for c in a["costs"]) or "none"
            lines.append("  %-3s successes %d/%d  median cost %s  costs [%s]  median peak context %s  agent calls %s" % (
                arm, a["successes"], a["n"], "n/a" if a["median_cost"] is None else "%.4f" % a["median_cost"], costs,
                "n/a" if a["median_peak_context"] is None else "%d" % a["median_peak_context"], a["agent_calls"]))
            lines.append("      permission denials per run %s (recorded only, they do not enter the verdict); leak suspects: %s" % (
                a["denials"], ", ".join(a["leak_suspects"]) or "none"))
            for rid, reasons in zip(a["ids"], a["denial_reasons"]):
                for reason in reasons:
                    lines.append("      denial in %s: %s" % (rid, reason[:120]))
        lines.append("  verdict: %s (%s)" % (row["verdict"], row["reason"]))
    lines.append("")
    lines.extend(report["actions"])
    lines.append("Peak context is reported above but does not decide any verdict.")
    if ledger_total is not None:
        lines.append("Ledger total (without --spent-before): %.2f" % ledger_total)
    return "\n".join(lines)


def cmd_report(args):
    out = Path(os.path.abspath(args.out))
    if not out.is_dir():
        raise Refused("no such output directory: " + str(out))
    report = build_report(out)
    print(render_report(report, Ledger(out, 0).spent()))
    return 0


# ---------------------------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(description="On/off comparison harness for explicit /orchestrate (see PROTOCOL.md)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("seal", help="write SEAL.json after review")
    p.add_argument("--seed", type=int)
    p.add_argument("--plugin-dir", help="record the commit of the frozen plugin tree")
    for name in ("pilot", "run"):
        p = sub.add_parser(name)
        p.add_argument("--plugin-dir", required=True, help="frozen plugin tree: a clean git checkout")
        p.add_argument("--work-root", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--spent-before", type=float, default=0.0)
        p.add_argument("--keep", action="store_true", help="keep the working directories")
        p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="wall timeout per run in seconds")
        p.add_argument("--claude", default="claude")
        p.add_argument("--seed", type=int, help="must equal the sealed seed if given")
        if name == "pilot":
            p.add_argument("--pilot-cap", type=float, default=PILOT_CAP_USD)
        else:
            p.add_argument("--resume", action="store_true", help="continue unrun cells; finished cells are never rerun")
            p.add_argument("--max-runs", type=int, help="run at most N scored runs in this invocation")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"seal": cmd_seal, "pilot": cmd_pilot, "run": cmd_run, "report": cmd_report}[args.command](args)
    except Refused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
