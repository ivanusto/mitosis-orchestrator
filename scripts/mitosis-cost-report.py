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
    python3 scripts/mitosis-cost-report.py --format ccusage    # same shape as `ccusage session --json`
    python3 scripts/mitosis-cost-report.py --debug-log hook.log  # add routing stats from the hook debug log
    python3 scripts/mitosis-cost-report.py --fleet --since 2026-10-23 --until 2026-11-03   # fleet view: is orchestration used and pinned?

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
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

def _load_sibling(name: str) -> Any:
    """Load scripts/<name>.py by file path (scripts/ is not on sys.path), cached in sys.modules."""
    key = "_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


_GEN = _load_sibling("mitosis_generated")

# USD per million tokens: (input, cache_write, cache_read, output); source: spec/models.toml
DEFAULT_PRICES: Dict[str, Tuple[float, float, float, float]] = dict(_GEN.PRICES)

# Short aliases used when a transcript records a bare tier name.
ALIASES = dict(_GEN.PRICE_ALIASES)
CACHE_1H_WRITE_MULTIPLIER = _GEN.CACHE_1H_WRITE_MULTIPLIER

USAGE_FIELDS = ("input", "cache_write", "cache_read", "output", "thinking")


def encode_project_dir(cwd: str) -> str:
    """Mirror Claude Code's project-directory encoding: every character that is not a letter or digit becomes `-`.

    `/home/me/repo` -> `-home-me-repo`, `/tmp/a_b.c` -> `-tmp-a-b-c` (underscores too).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


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
        p_cw = CACHE_1H_WRITE_MULTIPLIER * p_in
    return (usage["input"] * p_in + usage["cache_write"] * p_cw + usage["cache_read"] * p_cr + usage["output"] * p_out) / 1_000_000


def compute_savings(
    rows: List[Dict[str, Any]],
    prices: Dict[str, Tuple[float, float, float, float]],
    subagent_cache_ttl: str = "5m",
) -> Optional[Dict[str, Any]]:
    """Actual cost vs the same tokens with every subagent row re-priced on the primary model.

    Primary model = most primary-role turns (ties: highest price). Rows without a
    known price are excluded from both sides. Returns None without a priced primary.
    """
    turns: Dict[str, int] = {}
    for r in rows:
        if r["role"] == "primary":
            turns[r["model"]] = turns.get(r["model"], 0) + r["turns"]
    if not turns:
        return None

    def rank(model: str) -> Tuple[int, float, float]:
        price = price_for(model, prices) or (0.0, 0.0, 0.0, 0.0)
        return (turns[model], price[3], price[0])

    primary_model = max(turns, key=rank)
    primary_price = price_for(primary_model, prices)
    if primary_price is None:
        return None
    actual = 0.0
    counterfactual = 0.0
    for r in rows:
        if r["usd"] is None:
            continue
        actual += r["usd"]
        if r["role"] == "subagent":
            counterfactual += cost_usd(r, primary_price, subagent_cache_ttl) or 0.0
        else:
            counterfactual += r["usd"]
    saved = counterfactual - actual
    return {
        "primary_model": primary_model,
        "actual_usd": actual,
        "counterfactual_usd": counterfactual,
        "savings_usd": saved,
        "savings_pct": (saved / counterfactual * 100.0) if counterfactual else 0.0,
    }


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
    savings = compute_savings(rows, prices, subagent_cache_ttl)
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
        "savings": savings,
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
    sv = summary.get("savings")
    if sv:
        lines.append(f"  Savings vs all-frontier (same tokens on {sv['primary_model']}): ${sv['savings_usd']:,.2f} ({sv['savings_pct']:.1f}%)")
        lines.append(f"    caveat: a frontier model may use a different number of tokens; actual ${sv['actual_usd']:,.2f} vs counterfactual ${sv['counterfactual_usd']:,.2f}.")
    if summary["unknown_models"]:
        lines.append(f"  (no price for: {', '.join(summary['unknown_models'])}; excluded from savings; pass --prices to add them)")
    routing = summary.get("routing")
    if routing:
        lines.append("  Routing (hook debug log)")
        lines.append(f"    decisions: {routing['decisions']}")
        tiers = routing["tiers"]
        lines.append("    tiers: " + (", ".join(f"{t} {v['count']} ({v['pct']:.1f}%)" for t, v in tiers.items()) or "n/a"))
        lines.append("    rules: " + (", ".join(f"{k} {v}" for k, v in routing["rules"].items()) or "n/a"))
        lines.append("    sources: " + (", ".join(f"{k} {v}" for k, v in routing["sources"].items()) or "n/a"))
        hr = routing["judge_hit_rate"]
        lines.append(f"    judge hit rate (judge decisions / decisions with source judge or default): {'n/a' if hr is None else f'{hr:.1%}'}")
        lines.append("    mean confidence per tier: " + (", ".join(f"{t} {c:.2f}" for t, c in routing["mean_confidence"].items()) or "n/a"))
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


def sanitise_sid(sid: str) -> str:
    """Same sanitising as the hook: keep [A-Za-z0-9_-], cut to 128 chars."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)[:128] or "default"


def default_debug_logs(sid: str) -> List[Path]:
    cid = sanitise_sid(sid)
    candidates = [
        Path(tempfile.gettempdir()) / "mitosis-orchestrator" / cid / "mitosis_orchestrator_debug.log",
        Path(os.path.expanduser("~")) / "mitosis-judge" / "logs" / f"{sid}.log",
    ]
    return [c for c in candidates if c.is_file()]


def routing_stats(paths: List[Path]) -> Optional[Dict[str, Any]]:
    """Aggregate routing decisions from hook debug logs; None when there are none."""
    decisions: List[Dict[str, Any]] = []
    for path in paths:
        for rec in iter_records(path):
            items = rec.get("decisions")
            if isinstance(items, list):
                decisions += [d for d in items if isinstance(d, dict)]
    if not decisions:
        return None
    n = len(decisions)
    tier_counts: Dict[str, int] = {}
    conf: Dict[str, List[float]] = {}
    rules: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    for d in decisions:
        tier = str(d.get("tier") or "unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        c = d.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            conf.setdefault(tier, []).append(float(c))
        if d.get("rule"):
            rules[str(d["rule"])] = rules.get(str(d["rule"]), 0) + 1
        src = str(d.get("source") or "unknown")
        sources[src] = sources.get(src, 0) + 1
    eligible = sources.get("judge", 0) + sources.get("default", 0)
    return {
        "logs": [str(p) for p in paths],
        "decisions": n,
        "tiers": {t: {"count": c, "pct": c / n * 100.0} for t, c in sorted(tier_counts.items())},
        "rules": dict(sorted(rules.items(), key=lambda kv: (-kv[1], kv[0]))),
        "sources": dict(sorted(sources.items())),
        "judge_eligible": eligible,
        "judge_hit_rate": (sources.get("judge", 0) / eligible) if eligible else None,
        "mean_confidence": {t: sum(v) / len(v) for t, v in sorted(conf.items())},
    }


def _iso_ms(value: Optional[str]) -> Optional[str]:
    ts = _parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def ccusage_report(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape of `ccusage session --json`, built from summarize() output."""
    sessions = []
    for s in summaries:
        agg: Dict[str, Dict[str, Any]] = {}
        for r in s["rows"]:
            b = agg.setdefault(r["model"], {"modelName": r["model"], "inputTokens": 0, "outputTokens": 0,
                                            "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0.0, "_unpriced": False})
            b["inputTokens"] += r["input"]
            b["outputTokens"] += r["output"]
            b["cacheCreationTokens"] += r["cache_write"]
            b["cacheReadTokens"] += r["cache_read"]
            if r["usd"] is None:
                b["_unpriced"] = True
            else:
                b["cost"] += r["usd"]
        breakdowns = []
        for b in sorted(agg.values(), key=lambda x: -x["cost"]):
            if b.pop("_unpriced"):
                b["missingPricing"] = True
            breakdowns.append(b)
        item = {
            "sessionId": s["session_id"],
            "inputTokens": sum(b["inputTokens"] for b in breakdowns),
            "outputTokens": sum(b["outputTokens"] for b in breakdowns),
            "cacheCreationTokens": sum(b["cacheCreationTokens"] for b in breakdowns),
            "cacheReadTokens": sum(b["cacheReadTokens"] for b in breakdowns),
        }
        item["totalTokens"] = item["inputTokens"] + item["outputTokens"] + item["cacheCreationTokens"] + item["cacheReadTokens"]
        item["totalCost"] = s["total_usd"]
        item["lastActivity"] = _iso_ms(s["end"])
        item["firstActivity"] = _iso_ms(s["start"])
        item["modelsUsed"] = sorted(agg)
        item["modelBreakdowns"] = breakdowns
        item["projectPath"] = Path(s["project_dir"]).name or None
        if item["totalTokens"] > 0:
            sessions.append(item)
    sessions.sort(key=lambda x: -x["totalCost"])
    totals: Dict[str, Any] = {k: sum(x[k] for x in sessions) for k in
                              ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens")}
    totals["totalCost"] = sum(x["totalCost"] for x in sessions)
    unpriced = sorted({b["modelName"] for x in sessions for b in x["modelBreakdowns"] if b.get("missingPricing")})
    if unpriced:
        totals["unpricedModels"] = unpriced
    return {"sessions": sessions, "totals": totals}


def list_sessions(project_dir: Path) -> List[str]:
    files = [f for f in project_dir.glob("*.jsonl") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.stem for f in files]


# ---------------------------------------------------------------- fleet view

PINNED_PREFIX = "mitosis-orchestrator:flash-"
DISPATCH_TOOLS = ("Agent", "Task")
SYNTHETIC_MODEL = "<synthetic>"
ENTRYPOINT_CHOICES = ("cli", "sdk-cli", "all")


def _parse_day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def _in_window(ts: Optional[datetime], since: Optional[datetime], until_excl: Optional[datetime]) -> bool:
    if since is None and until_excl is None:
        return True
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (since is None or ts >= since) and (until_excl is None or ts < until_excl)


def _agent_type(meta_path: Path) -> str:
    """agentType from agent-*.meta.json; 'unknown' when missing or malformed. Value is sanitised."""
    try:
        with open(meta_path, encoding="utf-8") as fh:
            value = json.load(fh).get("agentType")
    except (OSError, ValueError, AttributeError):
        return "unknown"
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_:.\-]", "?", value.strip())[:80]


def _session_entrypoint(path: Path) -> str:
    for rec in iter_records(path):
        ep = rec.get("entrypoint")
        if isinstance(ep, str) and ep:
            return ep
    return "unknown"


def _scan_transcript(
    path: Path,
    sid: str,
    role: str,
    agent_type: str,
    since: Optional[datetime],
    until_excl: Optional[datetime],
    usage_seen: Dict[str, Dict[str, Any]],
    tool_seen: Dict[str, Dict[str, Any]],
) -> None:
    """Fold one transcript into the fleet-wide de-dup maps (last streamed record per message id wins)."""
    for rec in iter_records(path):
        if rec.get("type") != "assistant":
            continue
        if role == "primary" and rec.get("isSidechain"):
            continue
        if not _in_window(_parse_ts(rec.get("timestamp")), since, until_excl):
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        model = str(message.get("model") or "unknown")
        if model == SYNTHETIC_MODEL:
            continue
        usage = _usage_of(rec)
        if usage is not None:
            key = str(message.get("id") or rec.get("uuid") or "")
            if key:
                usage_seen[key] = {"sid": sid, "role": role, "agent": path.name if role == "subagent" else "",
                                   "type": agent_type, "model": model, "usage": usage}
        content = message.get("content")
        if role == "primary" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    bid = str(block.get("id") or "")
                    if bid:
                        tool_seen[bid] = {"sid": sid, "name": str(block.get("name") or "")}


def _cost_parts(usage: Dict[str, int], price: Optional[Tuple[float, float, float, float]], ttl: str) -> Dict[str, float]:
    if price is None:
        return {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
    p_in, p_cw, p_cr, p_out = price
    if ttl == "1h":
        p_cw = CACHE_1H_WRITE_MULTIPLIER * p_in
    return {"input": usage["input"] * p_in / 1e6, "cache_write": usage["cache_write"] * p_cw / 1e6,
            "cache_read": usage["cache_read"] * p_cr / 1e6, "output": usage["output"] * p_out / 1e6}


def collect_fleet(
    project_dirs: List[Path],
    prices: Dict[str, Tuple[float, float, float, float]],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    entrypoint: str = "cli",
    primary_cache_ttl: str = "1h",
    subagent_cache_ttl: str = "5m",
) -> Dict[str, Any]:
    until_excl = (until + timedelta(days=1)) if until else None
    usage_seen: Dict[str, Dict[str, Any]] = {}
    tool_seen: Dict[str, Dict[str, Any]] = {}
    entry_tally: Dict[str, int] = {}
    session_ep: Dict[str, str] = {}
    agent_files: Dict[Tuple[str, str], str] = {}  # (sid, agent file) -> agent type
    n_projects = 0
    for project_dir in project_dirs:
        n_projects += 1
        for main_file in sorted(project_dir.glob("*.jsonl")):
            sid = main_file.stem
            ep = _session_entrypoint(main_file)
            wanted = entrypoint == "all" or ep == entrypoint
            sub_dir = project_dir / sid / "subagents"
            if not wanted:
                # Tally only: does the session have any in-window record at all?
                if any(_in_window(_parse_ts(r.get("timestamp")), since, until_excl) for r in iter_records(main_file)):
                    entry_tally[ep] = entry_tally.get(ep, 0) + 1
                continue
            sid_key = f"{project_dir.name}/{sid}"
            session_ep[sid_key] = ep
            _scan_transcript(main_file, sid_key, "primary", "", since, until_excl, usage_seen, tool_seen)
            if sub_dir.is_dir():
                for sub in sorted(sub_dir.glob("agent-*.jsonl")):
                    atype = _agent_type(sub.with_name(sub.name[: -len(".jsonl")] + ".meta.json"))
                    agent_files[(sid_key, sub.name)] = atype
                    _scan_transcript(sub, sid_key, "subagent", atype, since, until_excl, usage_seen, tool_seen)

    # Per-session primary model = most primary turns.
    turns: Dict[str, Dict[str, int]] = {}
    for e in usage_seen.values():
        if e["role"] == "primary":
            d = turns.setdefault(e["sid"], {})
            d[e["model"]] = d.get(e["model"], 0) + 1
    primary_model = {sid: max(d, key=lambda m: (d[m], m)) for sid, d in turns.items()}
    sessions = set(primary_model)
    for t in tool_seen.values():
        sessions.add(t["sid"])
    for sid in sessions:
        ep = session_ep.get(sid, "unknown")
        entry_tally[ep] = entry_tally.get(ep, 0) + 1

    tool_calls = len(tool_seen)
    bash = sum(1 for t in tool_seen.values() if t["name"] == "Bash")
    agent_calls = sum(1 for t in tool_seen.values() if t["name"] in DISPATCH_TOOLS)
    dispatch_sessions = len({t["sid"] for t in tool_seen.values() if t["name"] in DISPATCH_TOOLS})

    split = {"cache_read": 0.0, "cache_write": 0.0, "output": 0.0, "input": 0.0}
    sub_usd = 0.0
    unpriced: List[str] = []
    spend: Dict[Tuple[str, str], Dict[str, float]] = {}  # (type, model) -> usd
    file_models: Dict[Tuple[str, str], Dict[str, int]] = {}
    pinned_usd = 0.0
    unpinned_usd = 0.0
    for e in usage_seen.values():
        price = price_for(e["model"], prices)
        if price is None and e["model"] not in unpriced:
            unpriced.append(e["model"])
        if e["role"] == "primary":
            for k, v in _cost_parts(e["usage"], price, primary_cache_ttl).items():
                split[k] += v
            continue
        usd = sum(_cost_parts(e["usage"], price, subagent_cache_ttl).values())
        sub_usd += usd
        cell = spend.setdefault((e["type"], e["model"]), {"usd": 0.0})
        cell["usd"] += usd
        fm = file_models.setdefault((e["sid"], e["agent"]), {})
        fm[e["model"]] = fm.get(e["model"], 0) + 1
        if e["type"].startswith(PINNED_PREFIX):
            pinned = True
        elif e["type"] == "unknown":
            pinned = e["model"] != primary_model.get(e["sid"])
        else:
            pinned = False
        if pinned:
            pinned_usd += usd
        else:
            unpinned_usd += usd
    counts: Dict[Tuple[str, str], int] = {}
    for (sid_key, fname), fm in file_models.items():
        model = max(fm, key=lambda m: (fm[m], m))
        atype = agent_files.get((sid_key, fname), "unknown")
        counts[(atype, model)] = counts.get((atype, model), 0) + 1
    by_type = []
    for key in sorted(set(spend) | set(counts), key=lambda k: (-spend.get(k, {"usd": 0.0})["usd"], k)):
        by_type.append({"agent_type": key[0], "model": key[1], "count": counts.get(key, 0),
                        "usd": spend.get(key, {"usd": 0.0})["usd"]})
    primary_usd = sum(split.values())
    total_usd = primary_usd + sub_usd
    sub_total = pinned_usd + unpinned_usd
    mitosis_dispatches = sum(r["count"] for r in by_type if r["agent_type"].startswith("mitosis-orchestrator:"))
    haiku = any("haiku" in r["model"] for r in by_type)
    return {
        "filters": {"since": since.date().isoformat() if since else None, "until": until.date().isoformat() if until else None,
                    "entrypoint": entrypoint, "timezone": "UTC"},
        "projects": n_projects,
        "sessions": len(sessions),
        "sessions_by_entrypoint": dict(sorted(entry_tally.items())),
        "unknown_entrypoint_sessions": entry_tally.get("unknown", 0),
        "sessions_with_dispatch": dispatch_sessions,
        "primary_tool_calls": tool_calls,
        "agent_calls": agent_calls,
        "dispatch_rate": (agent_calls / tool_calls) if tool_calls else None,
        "bash_calls": bash,
        "bash_share": (bash / tool_calls) if tool_calls else None,
        "primary_usd": primary_usd,
        "primary_usd_split": split,
        "subagent_usd": sub_usd,
        "total_usd": total_usd,
        "subagent_share": (sub_usd / total_usd) if total_usd else None,
        "subagents": by_type,
        "subagent_transcripts": sum(r["count"] for r in by_type),
        "pinned_usd": pinned_usd,
        "unpinned_usd": unpinned_usd,
        "pinned_share": (pinned_usd / sub_total) if sub_total else None,
        "unpinned_share": (unpinned_usd / sub_total) if sub_total else None,
        "mitosis_dispatches": mitosis_dispatches,
        "zero_haiku": bool(mitosis_dispatches) and not haiku,
        "unpriced_models": sorted(unpriced),
    }


def render_fleet_text(r: Dict[str, Any]) -> str:
    f = r["filters"]

    def pct(v: Optional[float]) -> str:
        return "n/a" if v is None else f"{v:.1%}"

    lines = [
        f"Fleet report  since {f['since'] or '-'}  until {f['until'] or '-'} ({f['timezone']}, per record)  entrypoint {f['entrypoint']}  projects {r['projects']}",
        f"  sessions: {r['sessions']}  with dispatch: {r['sessions_with_dispatch']}  unknown-entrypoint sessions: {r['unknown_entrypoint_sessions']}",
    ]
    lines.append("  sessions by entrypoint (in window, before filter): " + (", ".join(f"{k} {v}" for k, v in r["sessions_by_entrypoint"].items()) or "none"))
    lines.append(f"  primary tool calls: {r['primary_tool_calls']:,}  Agent/Task calls: {r['agent_calls']}  dispatch rate: {pct(r['dispatch_rate'])}  Bash share: {pct(r['bash_share'])}")
    s = r["primary_usd_split"]
    lines.append(f"  primary USD {r['primary_usd']:,.2f}: cache read {s['cache_read']:,.2f}, cache write {s['cache_write']:,.2f}, output {s['output']:,.2f}, input {s['input']:,.2f}")
    lines.append(f"  subagent USD {r['subagent_usd']:,.2f}  total {r['total_usd']:,.2f}  subagent share {pct(r['subagent_share'])}")
    lines.append(f"  {'agent type':<42}{'model':<24}{'count':>6}{'USD':>10}")
    for row in r["subagents"]:
        lines.append(f"  {row['agent_type']:<42}{row['model']:<24}{row['count']:>6}{row['usd']:>10.2f}")
    lines.append(f"  pinned subagent spend {r['pinned_usd']:,.2f} ({pct(r['pinned_share'])}), unpinned {r['unpinned_usd']:,.2f} ({pct(r['unpinned_share'])})")
    lines.append(f"  mitosis dispatches {r['mitosis_dispatches']}  zero-haiku flag: {'YES' if r['zero_haiku'] else 'no'}")
    if r["unpriced_models"]:
        lines.append(f"  (no price for: {', '.join(r['unpriced_models'])}; counted as 0 USD)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Token and cost ledger for Claude Code sessions (primary vs subagents).")
    ap.add_argument("--project", help="Claude Code project directory (default: derived from cwd under ~/.claude/projects)")
    ap.add_argument("--session", action="append", default=[], help="session id (repeatable); default: newest")
    ap.add_argument("--last", type=int, default=1, help="number of newest sessions when --session is not given (default 1)")
    ap.add_argument("--prices", help="JSON file overriding the built-in price table")
    ap.add_argument("--primary-cache-ttl", choices=CACHE_TTLS, default="1h", help="cache-write rate for the primary agent (default 1h, as Claude Code bills it)")
    ap.add_argument("--subagent-cache-ttl", choices=CACHE_TTLS, default="5m", help="cache-write rate for subagents (default 5m)")
    ap.add_argument("--format", choices=("text", "json", "ccusage"), default="text", help="output format (default text; ccusage mimics `ccusage session --json`)")
    ap.add_argument("--json", action="store_true", help="alias for --format json")
    ap.add_argument("--debug-log", action="append", default=[], help="hook debug log for the routing section (repeatable; default: auto-detect per session)")
    ap.add_argument("--fleet", action="store_true", help="fleet view across all projects (or --project): dispatch use, cost split, pinned vs unpinned subagents")
    ap.add_argument("--since", type=_parse_day, help="fleet: first day (YYYY-MM-DD, UTC), applied per record by timestamp")
    ap.add_argument("--until", type=_parse_day, help="fleet: last day inclusive (YYYY-MM-DD, UTC), applied per record by timestamp")
    ap.add_argument("--entrypoint", choices=ENTRYPOINT_CHOICES, default="cli", help="fleet: session entrypoint to include (default cli; excludes headless sdk-cli)")
    args = ap.parse_args(argv)
    if not args.fleet and (args.since or args.until):
        ap.error("--since/--until require --fleet")
    if args.since and args.until and args.until < args.since:
        ap.error("--until is before --since")
    if args.fleet and args.session:
        ap.error("--fleet cannot be combined with --session")

    if args.fleet:
        if args.project:
            dirs = [Path(os.path.expanduser(args.project))]
        else:
            root = Path(os.path.expanduser("~")) / ".claude" / "projects"
            dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
        if not dirs or not all(d.is_dir() for d in dirs):
            print("project directory not found", file=sys.stderr)
            return 2
        report = collect_fleet(dirs, resolve_prices(args.prices), args.since, args.until, args.entrypoint,
                               args.primary_cache_ttl, args.subagent_cache_ttl)
        if args.json or args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(render_fleet_text(report))
        return 0

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
    fmt = "json" if args.json else args.format
    if fmt == "ccusage":
        print(json.dumps(ccusage_report(summaries), indent=2))
        return 0
    for s in summaries:
        logs = [Path(os.path.expanduser(p)) for p in args.debug_log] or default_debug_logs(s["session_id"])
        s["routing"] = routing_stats([p for p in logs if p.is_file()])
    if fmt == "json":
        print(json.dumps(summaries, indent=2))
        return 0
    print("\n\n".join(render_text(s) for s in summaries))
    if len(summaries) > 1:
        print()
        print(render_comparison(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
