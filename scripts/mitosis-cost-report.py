#!/usr/bin/env python3
"""mitosis-cost-report: token and cost ledger for Claude Code sessions.

Reads Claude Code transcripts (standard library only) and aggregates
`message.usage` per model, split into the primary agent and its subagents,
then estimates USD from a built-in list-price table.

    python3 scripts/mitosis-cost-report.py                 # newest session of the current project
    python3 scripts/mitosis-cost-report.py --last 3        # the three newest sessions, side by side
    python3 scripts/mitosis-cost-report.py --session <id>  # one or more explicit session ids
    python3 scripts/mitosis-cost-report.py --project ~/.claude/projects/-home-me-repo --json
    python3 scripts/mitosis-cost-report.py --prices my-prices.json

Transcript layout (Claude Code 2.1.x):
    ~/.claude/projects/<encoded cwd>/<session_id>.jsonl                 primary agent
    ~/.claude/projects/<encoded cwd>/<session_id>/subagents/agent-*.jsonl subagents

The same `message.id` appears once per streamed content block, so usage is
de-duplicated by message id before summing.

Prices are Anthropic API list prices in USD per million tokens. The table
holds the 5-minute cache-write rate (1.25x input unless documented
otherwise); the 1-hour rate is 2x input. Claude Code's primary loop writes
1-hour cache entries while subagents matched the 5-minute rate when checked
against Claude Code's own cost accounting, so the defaults are
--primary-cache-ttl 1h and --subagent-cache-ttl 5m (this reproduced
`claude -p --output-format json` cost figures to the cent). They are still
approximations for comparing sessions, not an invoice: check the official
pricing page and override with --prices when they change. Subscription plans (Pro/Max) are not
billed per token, but their usage limits are weighted by model tier in the
same direction, so the relative numbers still tell you which model is eating
the budget.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# USD per million tokens: (input, cache_write, cache_read, output)
DEFAULT_PRICES: Dict[str, Tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 12.5, 0.25, 50.0),
    "claude-mythos-5-1": (10.0, 12.5, 0.25, 50.0),
    "claude-fable-5": (10.0, 12.5, 0.25, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 0.20, 20.0),
    "claude-opus-5": (5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-7": (5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-6": (5.0, 6.25, 0.50, 25.0),
    "claude-sonnet-5-5": (2.0, 2.5, 0.20, 10.0),
    "claude-sonnet-5": (2.0, 2.5, 0.20, 10.0),
    "claude-sonnet-4-6": (3.0, 3.75, 0.30, 15.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-haiku-4-5": (1.0, 1.25, 0.10, 5.0),
}

# Short aliases used when a transcript records a bare tier name.
ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}

USAGE_FIELDS = ("input", "cache_write", "cache_read", "output", "thinking")


def encode_project_dir(cwd: str) -> str:
    """Mirror Claude Code's project-directory encoding (`/home/me/repo` -> `-home-me-repo`)."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", cwd)


def default_project_dir(cwd: Optional[str] = None, projects_root: Optional[Path] = None) -> Path:
    root = projects_root or (Path(os.path.expanduser("~")) / ".claude" / "projects")
    candidate = root / encode_project_dir(cwd or os.getcwd())
    if candidate.is_dir():
        return candidate
    # Fall back to the most recently modified project directory.
    dirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    if not dirs:
        return candidate
    return max(dirs, key=lambda d: d.stat().st_mtime)


def resolve_prices(path: Optional[str]) -> Dict[str, Tuple[float, float, float, float]]:
    prices = dict(DEFAULT_PRICES)
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for model, row in data.items():
            if isinstance(row, dict):
                prices[model] = (float(row["input"]), float(row["cache_write"]), float(row["cache_read"]), float(row["output"]))
            else:
                prices[model] = tuple(float(x) for x in row)  # type: ignore[assignment]
    return prices


def price_for(model: str, prices: Dict[str, Tuple[float, float, float, float]]) -> Optional[Tuple[float, float, float, float]]:
    if model in prices:
        return prices[model]
    if model in ALIASES and ALIASES[model] in prices:
        return prices[ALIASES[model]]
    # Longest known prefix wins (e.g. a dated snapshot id).
    best = ""
    for known in prices:
        if model.startswith(known) and len(known) > len(best):
            best = known
    return prices.get(best) if best else None


def _usage_of(record: Dict[str, Any]) -> Optional[Dict[str, int]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details") or {}
    return {
        "input": int(usage.get("input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "thinking": int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0,
    }


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_records(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield data
    except OSError:
        return


def collect_session(project_dir: Path, session_id: str) -> Dict[str, Any]:
    """Aggregate one session: {models: {model: {role: {fields..., turns}}}, start, end, files}."""
    main_file = project_dir / f"{session_id}.jsonl"
    sub_dir = project_dir / session_id / "subagents"
    sources: List[Tuple[Path, str]] = [(main_file, "primary")]
    if sub_dir.is_dir():
        sources += [(f, "subagent") for f in sorted(sub_dir.glob("agent-*.jsonl"))]

    seen: Dict[str, Tuple[str, str, Dict[str, int]]] = {}
    order: List[str] = []
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    subagent_files = 0
    for path, default_role in sources:
        if not path.is_file():
            continue
        if default_role == "subagent":
            subagent_files += 1
        for rec in iter_records(path):
            ts = _parse_ts(rec.get("timestamp"))
            if ts:
                start = ts if start is None or ts < start else start
                end = ts if end is None or ts > end else end
            if rec.get("type") != "assistant":
                continue
            usage = _usage_of(rec)
            if usage is None:
                continue
            message = rec["message"]
            model = str(message.get("model") or "unknown")
            role = "subagent" if (default_role == "subagent" or rec.get("isSidechain")) else "primary"
            key = str(message.get("id") or rec.get("uuid") or f"{path.name}:{len(order)}")
            key = f"{path.name}:{key}" if default_role == "subagent" else key
            if key not in seen:
                order.append(key)
            seen[key] = (model, role, usage)

    models: Dict[str, Dict[str, Dict[str, int]]] = {}
    for key in order:
        model, role, usage = seen[key]
        bucket = models.setdefault(model, {}).setdefault(role, {f: 0 for f in USAGE_FIELDS})
        bucket.setdefault("turns", 0)
        bucket["turns"] += 1
        for f in USAGE_FIELDS:
            bucket[f] += usage[f]
    return {
        "session_id": session_id,
        "project_dir": str(project_dir),
        "models": models,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "duration_s": (end - start).total_seconds() if start and end else None,
        "subagent_files": subagent_files,
    }


CACHE_TTLS = ("5m", "1h")


def cost_usd(usage: Dict[str, int], price: Optional[Tuple[float, float, float, float]], cache_ttl: str = "5m") -> Optional[float]:
    """USD for one usage bucket. `cache_ttl` picks the cache-write rate: 5m = table value, 1h = 2x input."""
    if price is None:
        return None
    p_in, p_cw, p_cr, p_out = price
    if cache_ttl == "1h":
        p_cw = 2.0 * p_in
    return (usage["input"] * p_in + usage["cache_write"] * p_cw + usage["cache_read"] * p_cr + usage["output"] * p_out) / 1_000_000


def summarize(
    session: Dict[str, Any],
    prices: Dict[str, Tuple[float, float, float, float]],
    primary_cache_ttl: str = "1h",
    subagent_cache_ttl: str = "5m",
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    totals = {"primary": {f: 0 for f in USAGE_FIELDS}, "subagent": {f: 0 for f in USAGE_FIELDS}}
    totals_usd = {"primary": 0.0, "subagent": 0.0}
    unknown: List[str] = []
    for model, roles in sorted(session["models"].items()):
        for role in ("primary", "subagent"):
            usage = roles.get(role)
            if not usage:
                continue
            price = price_for(model, prices)
            usd = cost_usd(usage, price, primary_cache_ttl if role == "primary" else subagent_cache_ttl)
            if usd is None and model not in unknown:
                unknown.append(model)
            rows.append({"model": model, "role": role, "usd": usd, **usage})
            for f in USAGE_FIELDS:
                totals[role][f] += usage[f]
            totals_usd[role] += usd or 0.0
    total_tokens = {r: sum(v for k, v in totals[r].items() if k != "thinking") for r in totals}
    all_tokens = total_tokens["primary"] + total_tokens["subagent"]
    all_usd = totals_usd["primary"] + totals_usd["subagent"]
    return {
        "session_id": session["session_id"],
        "project_dir": session["project_dir"],
        "start": session["start"],
        "end": session["end"],
        "duration_s": session["duration_s"],
        "subagent_files": session["subagent_files"],
        "rows": rows,
        "totals": totals,
        "totals_usd": totals_usd,
        "total_usd": all_usd,
        "primary_token_share": (total_tokens["primary"] / all_tokens) if all_tokens else None,
        "primary_usd_share": (totals_usd["primary"] / all_usd) if all_usd else None,
        "unknown_models": unknown,
        "cache_ttl": {"primary": primary_cache_ttl, "subagent": subagent_cache_ttl},
    }


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_usd(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:,.3f}"


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def render_text(summary: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"Session {summary['session_id']}  ({summary['project_dir']})")
    lines.append(f"  window: {summary['start'] or 'n/a'} -> {summary['end'] or 'n/a'}  duration {_fmt_duration(summary['duration_s'])}  subagent transcripts: {summary['subagent_files']}")
    ttl = summary.get("cache_ttl", {})
    lines.append(f"  cache-write rate: primary {ttl.get('primary', '1h')}, subagents {ttl.get('subagent', '5m')} (list prices; see --prices / --*-cache-ttl)")
    header = f"  {'model':<22}{'role':<10}{'turns':>7}{'input':>11}{'cache_w':>11}{'cache_r':>12}{'output':>10}{'thinking':>10}{'USD':>10}"
    lines.append(header)
    for r in summary["rows"]:
        lines.append(
            f"  {r['model']:<22}{r['role']:<10}{r['turns']:>7}{_fmt_int(r['input']):>11}{_fmt_int(r['cache_write']):>11}"
            f"{_fmt_int(r['cache_read']):>12}{_fmt_int(r['output']):>10}{_fmt_int(r['thinking']):>10}{_fmt_usd(r['usd']):>10}"
        )
    for role in ("primary", "subagent"):
        t = summary["totals"][role]
        lines.append(
            f"  {'TOTAL':<22}{role:<10}{'':>7}{_fmt_int(t['input']):>11}{_fmt_int(t['cache_write']):>11}"
            f"{_fmt_int(t['cache_read']):>12}{_fmt_int(t['output']):>10}{_fmt_int(t['thinking']):>10}{_fmt_usd(summary['totals_usd'][role]):>10}"
        )
    share_t = summary["primary_token_share"]
    share_u = summary["primary_usd_share"]
    lines.append(
        f"  TOTAL USD {_fmt_usd(summary['total_usd'])}   primary share: "
        f"{'n/a' if share_t is None else f'{share_t:.0%}'} of tokens, {'n/a' if share_u is None else f'{share_u:.0%}'} of USD"
    )
    if summary["unknown_models"]:
        lines.append(f"  (no price for: {', '.join(summary['unknown_models'])}; pass --prices to add them)")
    return "\n".join(lines)


def render_comparison(summaries: List[Dict[str, Any]]) -> str:
    lines = ["Comparison", f"  {'session':<14}{'primary model(s)':<34}{'duration':>10}{'primary USD':>13}{'subagent USD':>14}{'total USD':>11}"]
    for s in summaries:
        primaries = sorted({r["model"] for r in s["rows"] if r["role"] == "primary"}) or ["n/a"]
        lines.append(
            f"  {s['session_id'][:12]:<14}{', '.join(primaries)[:33]:<34}{_fmt_duration(s['duration_s']):>10}"
            f"{_fmt_usd(s['totals_usd']['primary']):>13}{_fmt_usd(s['totals_usd']['subagent']):>14}{_fmt_usd(s['total_usd']):>11}"
        )
    return "\n".join(lines)


def list_sessions(project_dir: Path) -> List[str]:
    files = [f for f in project_dir.glob("*.jsonl") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.stem for f in files]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Token and cost ledger for Claude Code sessions (primary vs subagents).")
    ap.add_argument("--project", help="Claude Code project directory (default: derived from cwd under ~/.claude/projects)")
    ap.add_argument("--session", action="append", default=[], help="session id (repeatable); default: newest")
    ap.add_argument("--last", type=int, default=1, help="number of newest sessions when --session is not given (default 1)")
    ap.add_argument("--prices", help="JSON file overriding the built-in price table")
    ap.add_argument("--primary-cache-ttl", choices=CACHE_TTLS, default="1h", help="cache-write rate for the primary agent (default 1h, as Claude Code bills it)")
    ap.add_argument("--subagent-cache-ttl", choices=CACHE_TTLS, default="5m", help="cache-write rate for subagents (default 5m)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    project_dir = Path(os.path.expanduser(args.project)) if args.project else default_project_dir()
    if not project_dir.is_dir():
        print(f"project directory not found: {project_dir}", file=sys.stderr)
        return 2
    sessions = args.session or list_sessions(project_dir)[: max(args.last, 1)]
    if not sessions:
        print(f"no transcripts under {project_dir}", file=sys.stderr)
        return 2
    prices = resolve_prices(args.prices)
    summaries = [
        summarize(collect_session(project_dir, sid), prices, args.primary_cache_ttl, args.subagent_cache_ttl)
        for sid in sessions
    ]
    if args.json:
        print(json.dumps(summaries, indent=2))
        return 0
    print("\n\n".join(render_text(s) for s in summaries))
    if len(summaries) > 1:
        print()
        print(render_comparison(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
