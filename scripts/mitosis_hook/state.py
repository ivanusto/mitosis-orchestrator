"""Per-session state file, locking and routing telemetry."""

import functools
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import (
    DEFAULT_CODEX_SCOUT_MODEL,
    Decision,
    SCOUT_TYPE,
    STATE_FILENAME,
    WORKER_TYPE,
    _env_flag,
    _get_env,
    _load_sibling,
)
from .models import _read_settings, codex_tier_model, settings_path


CONVERSATION_ID_RE = re.compile(
    r'\\*"conversationId\\*"\s*:\s*\\*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\*"',
    re.I,
)


def _empty_state() -> Dict[str, Any]:
    return {
        "warned_invocations": [],
        "pending_spawns": [],
        "subagents": {},
        "stop_reminded_sets": [],
        "transcript_offset": 0,
        "turn_edited_files": [],
        "consecutive_failures": 0,
    }


def _state_file(payload: Dict[str, Any]) -> Optional[Path]:
    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        base = Path(artifact_dir)
    else:
        cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or payload.get("turn_id") or "default")
        # The id comes from the payload; keep it a single safe path segment.
        cid = re.sub(r"[^A-Za-z0-9_-]", "_", cid)[:128] or "default"
        base = Path(tempfile.gettempdir()) / "mitosis-orchestrator" / cid
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / STATE_FILENAME


LOCK_FILENAME = ".mitosis_orchestrator_state.lock"


try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore


try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore


# Held lock fds keyed by lock path. flock locks belong to the open file description, so a
# second open of the same file in this process would block against the first; keeping the
# fd here makes re-entrant acquisition (nested handler calls) a no-op.
_HELD_LOCKS: Dict[str, int] = {}


def _lock_timeout() -> float:
    try:
        return max(0.0, float(_get_env("AGY_MITOSIS_LOCK_TIMEOUT", "CLAUDE_MITOSIS_LOCK_TIMEOUT", "CODEX_MITOSIS_LOCK_TIMEOUT", default="2")))
    except ValueError:
        return 2.0


def _try_lock_fd(fd: int) -> bool:
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _msvcrt is not None:  # pragma: no cover - Windows
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True


def _acquire_state_lock(state_path: Path) -> None:
    """Take the per-session lock (idempotent). Fails open after the timeout; never raises."""
    if _fcntl is None and _msvcrt is None:
        return
    lock_path = str(state_path.with_name(LOCK_FILENAME))
    if lock_path in _HELD_LOCKS:
        return
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return
    deadline = time.monotonic() + _lock_timeout()
    while True:
        if _try_lock_fd(fd):
            _HELD_LOCKS[lock_path] = fd
            return
        if time.monotonic() >= deadline:
            os.close(fd)  # proceed unlocked
            return
        time.sleep(0.02)


def _release_state_locks() -> None:
    for lock_path, fd in list(_HELD_LOCKS.items()):
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _HELD_LOCKS.pop(lock_path, None)


_HANDLER_DEPTH = [0]


def _releases_state_locks(func):
    """Decorator: the outermost public handler call always drops the state locks."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        _HANDLER_DEPTH[0] += 1
        try:
            return func(*args, **kwargs)
        finally:
            _HANDLER_DEPTH[0] -= 1
            if _HANDLER_DEPTH[0] == 0:
                _release_state_locks()
    return wrapper


def _read_state_file(path: Optional[Path]) -> Dict[str, Any]:
    state = _empty_state()
    if path and path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                state.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return state


def _peek_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Lock-free read for decisions made before any network call. Never save this copy."""
    return _read_state_file(_state_file(payload))


def load_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Read the session state and take its lock (held until the handler returns)."""
    path = _state_file(payload)
    if path:
        _acquire_state_lock(path)
    return _read_state_file(path)


def save_state(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = _state_file(payload)
    if not path:
        return
    # A unique temp name per writer: parallel hooks sharing one ".tmp" could
    # rename each other's half-written file into place.
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def sync_subagents_from_transcript(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Pair pending spawns (FIFO) with new subagent conversationIds in the transcript.

    Only lines mentioning invoke_subagent are inspected, so unrelated UUIDs in
    the transcript (artifacts, task ids, the parent conversation) are ignored.
    Reading resumes from the last byte offset to stay cheap on long sessions.
    """
    transcript_path = payload.get("transcriptPath")
    if not transcript_path or not os.path.exists(transcript_path):
        return
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    parent_cid = str(payload.get("conversationId") or "").lower()
    offset = int(state.get("transcript_offset") or 0)
    try:
        size = os.path.getsize(transcript_path)
        if offset > size:
            offset = 0
        with open(transcript_path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                offset += len(raw)
                if not pending:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if "invoke_subagent" not in line and "Created the following subagents" not in line:
                    continue
                for match in CONVERSATION_ID_RE.finditer(line):
                    cid = match.group(1).lower()
                    if cid == parent_cid or cid in subagents:
                        continue
                    if not pending:
                        break
                    subagents[cid] = pending.pop(0)
        state["transcript_offset"] = offset
    except OSError:
        return


SUBAGENT_ALIVE_STATE = "SUBAGENT_STATE_ALIVE"


def _subagent_metadata_dir(payload: Dict[str, Any]) -> Optional[Path]:
    artifact_dir = payload.get("artifactDirectoryPath")
    if not artifact_dir:
        return None
    path = Path(artifact_dir) / ".system_generated" / "subagents"
    return path if path.is_dir() else None


def sync_subagents_from_artifacts(payload: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Pair pending spawns with <artifactDirectoryPath>/.system_generated/subagents/<cid>.json.

    Antigravity writes one file per spawned subagent with its typeName, role,
    live state and workspace URIs. This is authoritative, so it runs before the
    transcript fallback. Returns True when the directory exists.
    """
    meta_dir = _subagent_metadata_dir(payload)
    if meta_dir is None:
        return False
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    try:
        files = sorted(meta_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return True
    for file in files:
        try:
            with open(file, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        cid = str(meta.get("conversationId") or file.stem).strip().lower()
        descriptor = meta.get("subagentDescriptor") or {}
        type_name = str(descriptor.get("typeName") or "").strip()
        role = str(descriptor.get("role") or "").strip()
        alive = str(meta.get("state") or "") == SUBAGENT_ALIVE_STATE
        uris = meta.get("workspaceUris") or []
        in_worktree = any("/worktrees/" in str(u) for u in uris)

        record = subagents.get(cid)
        if record is None:
            # Prefer an exact (type, role) match, then a type match, then FIFO.
            idx = next((i for i, p in enumerate(pending)
                        if p.get("type", "").lower() == type_name.lower() and p.get("role") == role), None)
            if idx is None:
                idx = next((i for i, p in enumerate(pending)
                            if p.get("type", "").lower() == type_name.lower()), None)
            if idx is None and pending:
                idx = 0
            if idx is not None:
                record = pending.pop(idx)
            else:
                record = {
                    "role": role or type_name,
                    "type": type_name,
                    "model": "flash_lite" if type_name.lower() == SCOUT_TYPE else "flash",
                    "tier": "flash_lite" if type_name.lower() == SCOUT_TYPE else "flash",
                    "workspace": "branch" if in_worktree else "inherit",
                }
            subagents[cid] = record
        if in_worktree:
            record["workspace"] = "branch"
        record["alive"] = alive
        record["active_branch"] = bool(record.get("workspace") == "branch" and alive)
    return True


def sync_subagents(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    sync_subagents_from_artifacts(payload, state)
    if state.get("pending_spawns"):
        sync_subagents_from_transcript(payload, state)


# Decisions made during this process, read by _debug_log. Hooks are one-shot
# processes; the cap keeps the long-running MCP server from growing it.
_RECENT_DECISIONS: List[Decision] = []


def _payload_session_id(payload: Dict[str, Any]) -> str:
    for key in ("conversationId", "session_id", "thread_id", "thread-id", "turn_id"):
        if payload.get(key):
            return str(payload[key])
    return ""


def _record_routing(payload: Dict[str, Any], decision: Decision, model: Optional[str],
                    effort: Optional[str], platform: str, agent_name: str) -> None:
    """Record what was actually pinned (model, effort, platform) for the debug log and OTel.

    Replaces `decision` in _RECENT_DECISIONS (matched by identity, appended when absent)
    so the log shows the effort written into updatedInput, not the tier default.
    """
    final = decision._replace(model=model, effort=effort or decision.effort, platform=platform)
    for i, d in enumerate(_RECENT_DECISIONS):
        if d is decision:
            _RECENT_DECISIONS[i] = final
            break
    else:
        _RECENT_DECISIONS.append(final)
        del _RECENT_DECISIONS[:-16]
    try:
        if _otel_enabled():
            _load_sibling("mitosis_otel").queue({
                "agent_name": agent_name,
                "platform": platform,
                "tier": final.tier,
                "rule": final.rule,
                "confidence": final.confidence,
                "source": final.source,
                "model": model,
                "effort": effort,
                "session_id": _payload_session_id(payload),
            })
    except Exception:
        pass


def _otel_enabled() -> bool:
    """Cheap env check so mitosis_otel is only imported when telemetry is on."""
    if not _env_flag("CLAUDE_MITOSIS_OTEL", "AGY_MITOSIS_OTEL", "CODEX_MITOSIS_OTEL", "MITOSIS_OTEL"):
        return False
    return bool(os.environ.get("MITOSIS_OTEL_FILE", "").strip()
                or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def _flush_otel() -> None:
    if _otel_enabled():
        try:
            _load_sibling("mitosis_otel").flush()
        except Exception:
            pass


def _record_tier(info: Dict[str, Any]) -> str:
    """Tier of a spawn record: its `tier` field, else inferred from `model` (old state files)."""
    tier = info.get("tier")
    if tier in ("flash_lite", "flash"):
        return str(tier)
    model = str(info.get("model") or "").strip().lower()
    if model in ("flash_lite", "haiku", DEFAULT_CODEX_SCOUT_MODEL.lower(), codex_tier_model("flash_lite").lower()):
        return "flash_lite"
    return "flash"


# --- Failure escalation -------------------------------------------------------------------
# A scout (flash_lite) that reports STATUS: FAILED/BLOCKED makes the next scout dispatch run on
# the worker (flash); with *_MITOSIS_ESCALATE_PRIMARY=1 a failed worker makes the next worker
# dispatch run on the primary's model (Claude only). State key: "escalation".

ESCALATION_TTL_SECONDS = 30 * 60
ESCALATION_SPAWNS = 3
ESCALATION_STATUS_RE = re.compile(r"^[ \t>*_`-]*STATUS:[ \t*_`]*(FAILED|BLOCKED)\b", re.IGNORECASE | re.MULTILINE)
_PRIMARY_ALIASES = ("opus", "fable", "sonnet")


def escalation_enabled() -> bool:
    """AGY_MITOSIS_ESCALATE=0 (or CLAUDE_/CODEX_) turns all failure escalation off."""
    return _get_env("AGY_MITOSIS_ESCALATE", default="1") != "0"


def escalate_primary_enabled() -> bool:
    return escalation_enabled() and _env_flag("AGY_MITOSIS_ESCALATE_PRIMARY")


def report_failure_status(text: str) -> Optional[str]:
    """'FAILED' or 'BLOCKED' when a report line says so, else None."""
    m = ESCALATION_STATUS_RE.search(text or "")
    return m.group(1).upper() if m else None


def live_escalation(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The pending escalation, dropping it from `state` when disabled or expired."""
    esc = state.get("escalation")
    if not isinstance(esc, dict):
        state.pop("escalation", None)
        return None
    try:
        expired = int(esc.get("spawns_left") or 0) <= 0 or time.time() - float(esc.get("ts") or 0) > ESCALATION_TTL_SECONDS
    except (TypeError, ValueError):
        expired = True
    if expired or not escalation_enabled():
        state.pop("escalation", None)
        return None
    return esc


def escalation_for_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """{'from', 'to'} a finished spawn would escalate along, or None (already escalated, off, no route)."""
    if not record or record.get("escalated") or not escalation_enabled():
        return None
    if _record_tier(record) == "flash_lite":
        return {"from": "flash_lite", "to": "flash"}
    if escalate_primary_enabled():
        return {"from": "flash", "to": "primary"}
    return None


def start_escalation(state: Dict[str, Any], route: Dict[str, str], source: str) -> Dict[str, Any]:
    esc = {"from": route["from"], "to": route["to"], "ts": time.time(),
           "spawns_left": ESCALATION_SPAWNS, "source": source}
    state["escalation"] = esc
    return esc


def primary_model_alias(state: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> str:
    """Claude alias (opus/fable/sonnet) of the primary: recorded SessionStart model, else settings.json, else opus."""
    candidates = [str(state.get("primary_model") or "")]
    if not candidates[0]:
        try:
            data, _ = _read_settings(settings_path(payload))
            candidates.append(str((data or {}).get("model") or ""))
        except Exception:
            pass
    for name in candidates:
        low = name.lower()
        for alias in _PRIMARY_ALIASES:
            if alias in low:
                return alias
    return "opus"


def apply_escalation(state: Dict[str, Any], decision: Decision, platform: str,
                     payload: Optional[Dict[str, Any]] = None) -> Tuple[Decision, Optional[Dict[str, Any]]]:
    """Run one spawn through the pending escalation. Mutates `state`; the caller saves it.

    Returns (decision, info). `info` is None for an untouched spawn, else
    {"mode": "flash" | "primary" | "note", "model": <claude alias or None>, "source": ...} and
    the spawn record must be marked escalated. A spawn that is already on the target tier just
    clears the escalation (the task was re-dispatched to a worker anyway).
    """
    esc = live_escalation(state)
    if esc is None:
        return decision, None
    src = str(esc.get("source") or "a subagent")
    if esc.get("to") == "flash":
        state.pop("escalation", None)
        if decision.tier != "flash_lite":
            return decision, None
        new = decision._replace(
            tier="flash", type_override=WORKER_TYPE if decision.type_override else None, effort="medium", confidence=1.0, source="rule",
            rule="escalation", reason=f"scout {src} reported FAILED/BLOCKED; re-dispatched on the worker")
        _swap_recent_decision(decision, new)
        return new, {"mode": "flash", "model": None, "source": src}
    if esc.get("to") == "primary":
        if not escalate_primary_enabled():
            state.pop("escalation", None)
            return decision, None
        if decision.tier != "flash":
            esc["spawns_left"] = int(esc.get("spawns_left") or 0) - 1
            if esc["spawns_left"] <= 0:
                state.pop("escalation", None)
            return decision, None
        state.pop("escalation", None)
        if platform != "claude":
            note = f"worker {src} reported FAILED/BLOCKED; {platform} has no per-spawn model, so no escalation to the primary model"
            new = decision._replace(reason=f"{decision.reason}; {note}")
            _swap_recent_decision(decision, new)
            return new, {"mode": "note", "model": None, "source": src}
        alias = primary_model_alias(state, payload)
        new = decision._replace(
            rule="escalation-primary", source="rule", confidence=1.0,
            reason=f"worker {src} reported FAILED/BLOCKED; re-dispatched on the primary model ({alias})")
        _swap_recent_decision(decision, new)
        return new, {"mode": "primary", "model": alias, "source": src}
    state.pop("escalation", None)
    return decision, None


def _swap_recent_decision(old: Decision, new: Decision) -> None:
    for i, d in enumerate(_RECENT_DECISIONS):
        if d is old:
            _RECENT_DECISIONS[i] = new
            return
