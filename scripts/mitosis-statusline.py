#!/usr/bin/env python3
"""Optional Claude Code statusLine: per-session scout and worker counts.

Configure in ~/.claude/settings.json:
    {"statusLine": {"type": "command", "command": "python3 /path/to/scripts/mitosis-statusline.py"}}

Reads the statusLine JSON on stdin, reads the hook's per-session state file
without a lock (writes are atomic replaces) and prints one line, for example:
    mitosis: scout 3 (1 live) | worker 2 | Opus | $1.23
Any problem prints `mitosis: idle`; the exit status is always 0.
Set MITOSIS_STATUSLINE_PREFIX to change the leading label. No ANSI colours.

Stdlib only, Python 3.8 compatible. The session id sanitising and state path
mirror scripts/enforce-orchestrator.py (_state_file), which is deliberately not
imported here because this runs on every statusline refresh.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

STATE_FILENAME = "mitosis_orchestrator_state.json"
SCOUT_MODELS = {"flash_lite", "haiku", "gpt-5.6-luna"}


def state_path(session_id: str) -> Path:
    cid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "default"))[:128] or "default"
    return Path(tempfile.gettempdir()) / "mitosis-orchestrator" / cid / STATE_FILENAME


def record_tier(info: Dict[str, Any]) -> str:
    tier = info.get("tier")
    if tier in ("flash_lite", "flash"):
        return str(tier)
    model = str(info.get("model") or "").strip().lower()
    return "flash_lite" if model in SCOUT_MODELS else "flash"


def count(records: Any) -> Dict[str, Tuple[int, int]]:
    """Return {tier: (total, live)}; a record without an `alive` flag counts as not live."""
    out = {"flash_lite": (0, 0), "flash": (0, 0)}
    items = records.values() if isinstance(records, dict) else (records if isinstance(records, list) else [])
    for info in items:
        if not isinstance(info, dict):
            continue
        tier = record_tier(info)
        total, live = out[tier]
        out[tier] = (total + 1, live + (1 if info.get("alive") else 0))
    return out


def render(data: Dict[str, Any], state: Dict[str, Any]) -> str:
    prefix = os.environ.get("MITOSIS_STATUSLINE_PREFIX", "").strip() or "mitosis"
    spawned = count(state.get("subagents"))
    pending = count(state.get("pending_spawns"))
    parts = []
    for label, tier in (("scout", "flash_lite"), ("worker", "flash")):
        total = spawned[tier][0] + pending[tier][0]
        live = spawned[tier][1]
        if total:
            parts.append("%s %d (%d live)" % (label, total, live) if live else "%s %d" % (label, total))
    if not parts:
        return prefix + ": idle"
    model = data.get("model")
    name = model.get("display_name") if isinstance(model, dict) else None
    if name:
        parts.append(str(name))
    cost = data.get("cost")
    usd = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        parts.append("$%.2f" % usd)
    return prefix + ": " + " | ".join(parts)


def main() -> int:
    prefix = os.environ.get("MITOSIS_STATUSLINE_PREFIX", "").strip() or "mitosis"
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        sid = data.get("session_id")
        if not sid:
            line = prefix + ": idle"
        else:
            state = json.loads(state_path(str(sid)).read_text(encoding="utf-8"))
            line = render(data, state if isinstance(state, dict) else {})
    except Exception:
        line = prefix + ": idle"
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
