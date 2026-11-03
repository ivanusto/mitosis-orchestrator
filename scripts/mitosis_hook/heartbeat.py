"""Persistent per-platform "wire heartbeat": what the hooks really saw on the wire.

One small JSON file per platform, `<dir>/heartbeat-<platform>.json`, so a doctor can tell a
silently dead platform (unhandled tool names, unreadable stdin, hook errors) from a healthy one.

Privacy: only counters, tool names (sanitised, capped) and exception CLASS names are stored.
Never a prompt, path, message, exception text, command or tool input.

Safety: every public function swallows all exceptions and never writes to stdout/stderr.
Lossy by design: last writer wins, no lock, and events seen while throttled in a short-lived
hook process are not counted (`events` is a lower bound).

MITOSIS_HEARTBEAT=0 disables; MITOSIS_HEARTBEAT_DIR overrides the directory.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .common import is_claude_code_payload, is_codex_payload

SCHEMA = 1
THROTTLE_SECONDS = 60.0
MAX_NAMES = 16
_NAME_RE = re.compile(r"[^A-Za-z0-9_.:/-]")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Counters gathered in this process and merged into the file at the next write.
_PENDING: Dict[str, Any] = {}
_EVENTS_PENDING = [0]
_VERSION: list = []


def enabled() -> bool:
    return os.environ.get("MITOSIS_HEARTBEAT", "").strip() != "0"


def platform_of(payload: Any) -> str:
    try:
        if is_codex_payload(payload):
            return "codex"
        if is_claude_code_payload(payload):
            return "claude"
    except Exception:
        pass
    return "agy"


def heartbeat_dir() -> Path:
    override = os.environ.get("MITOSIS_HEARTBEAT_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "mitosis-orchestrator"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mitosis-orchestrator"


def _clean_name(name: Any) -> str:
    return _NAME_RE.sub("?", str(name))[:64] or "?"


def _bump(bucket: Dict[str, int], key: str, n: int = 1) -> None:
    if key in bucket or len(bucket) < MAX_NAMES:
        bucket[key] = int(bucket.get(key, 0)) + n


def note(*, unhandled: Optional[str] = None, failure: Optional[str] = None,
         pin: Optional[str] = None, report: Optional[bool] = None) -> None:
    """Queue counters from anywhere in a hook. Cheap; flushed by record_event()."""
    try:
        if not enabled():
            return
        if unhandled:
            _bump(_PENDING.setdefault("unhandled_spawn_tools", {}), _clean_name(unhandled))
        if failure:
            _bump(_PENDING.setdefault("tool_failures", {}), _clean_name(failure))
        if pin in ("verified", "mismatch", "unknown"):
            p = _PENDING.setdefault("pin", {})
            p[pin] = p.get(pin, 0) + 1
        if report is not None:
            r = _PENDING.setdefault("report", {})
            key = "ok" if report else "bad"
            r[key] = r.get(key, 0) + 1
    except Exception:
        pass


def _plugin_version() -> Optional[str]:
    if not _VERSION:
        version = None
        try:
            manifest = Path(__file__).resolve().parent.parent.parent / "plugin.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            value = data.get("version") if isinstance(data, dict) else None
            version = value if isinstance(value, str) else None
        except Exception:
            version = None
        _VERSION.append(version)
    return _VERSION[0]


def _read_existing(path: Path) -> Dict[str, Any]:
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _merge_map(old: Any, new: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if isinstance(old, dict):
        for k, v in old.items():
            if len(out) < MAX_NAMES and isinstance(k, str):
                out[k] = _int(v)
    for k, v in new.items():
        _bump(out, k, v)
    return out


def _merge_counts(old: Any, new: Dict[str, int], keys: tuple) -> Dict[str, int]:
    base = old if isinstance(old, dict) else {}
    return {k: _int(base.get(k)) + int(new.get(k, 0)) for k in keys}


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (".%s.%d.%s.tmp" % (path.name, os.getpid(), os.urandom(4).hex()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(data, sort_keys=True).encode("utf-8"))
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def record_event(payload: Any, mode: str, *, error_class: Optional[str] = None,
                 replacement_chars: int = 0, unhandled: Optional[str] = None) -> None:
    """Record one finished hook call. Never raises, never prints."""
    try:
        if not enabled():
            return
        if unhandled:
            note(unhandled=unhandled)
        _EVENTS_PENDING[0] += 1
        informative = bool(error_class or replacement_chars or _PENDING)
        path = heartbeat_dir() / ("heartbeat-%s.json" % platform_of(payload))
        now = time.time()
        if not informative:
            try:
                if now - os.stat(str(path)).st_mtime < THROTTLE_SECONDS:
                    return  # fast path: one stat, counters stay in _EVENTS_PENDING
            except OSError:
                pass  # no file yet: write it
        old = _read_existing(path)
        pending = dict(_PENDING)
        events = _EVENTS_PENDING[0]
        data: Dict[str, Any] = {
            "schema": SCHEMA,
            "plugin_version": _plugin_version(),
            "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "last_event": _clean_name(mode),
            "last_event_at": int(now),
            "events": _int(old.get("events")) + events,
            "unhandled_spawn_tools": _merge_map(old.get("unhandled_spawn_tools"), pending.get("unhandled_spawn_tools", {})),
            "tool_failures": _merge_map(old.get("tool_failures"), pending.get("tool_failures", {})),
            "stdin_replacement_chars": _int(old.get("stdin_replacement_chars")) + int(replacement_chars),
            "pin": _merge_counts(old.get("pin"), pending.get("pin", {}), ("verified", "mismatch", "unknown")),
            "report": _merge_counts(old.get("report"), pending.get("report", {}), ("ok", "bad")),
            "last_error": old.get("last_error") if isinstance(old.get("last_error"), dict) else None,
        }
        if error_class:
            cls = error_class if _CLASS_RE.match(error_class) else "Exception"
            data["last_error"] = {"class": cls[:80], "event": _clean_name(mode), "at": int(now)}
        _atomic_write(path, data)
        _PENDING.clear()
        _EVENTS_PENDING[0] = 0
    except Exception:
        pass
