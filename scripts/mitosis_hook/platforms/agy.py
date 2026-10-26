"""Antigravity (agy) subagent tools."""

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..common import SCOUT_TYPE, WORKER_TYPE, _get
from ..guards import _iter_strings, workflow_model_violations
from ..report import (
    _append_report_format_enabled,
    append_report_format,
    prompt_has_report_format,
    validate_subagent_report,
)
from ..routing import decide_subagent
from ..state import (
    _record_routing,
    apply_escalation,
    escalation_enabled,
    escalation_for_record,
    live_escalation,
    load_state,
    report_failure_status,
    save_state,
    start_escalation,
    sync_subagents,
)


_SPELLINGS = (("Prompt", "prompt"), ("TypeName", "typeName"), ("Model", "model"))


def _messages_dir(payload: Dict[str, Any]) -> Optional[Path]:
    artifact = payload.get("artifactDirectoryPath")
    if not artifact:
        return None
    path = Path(str(artifact)) / ".system_generated" / "messages"
    return path if path.is_dir() else None


def _sender_meta(payload: Dict[str, Any], sender: str) -> Dict[str, str]:
    if not sender or "/" in sender or "\\" in sender or sender.startswith("."):
        return {}
    meta_file = Path(str(payload.get("artifactDirectoryPath"))) / ".system_generated" / "subagents" / ("%s.json" % sender)
    try:
        with open(str(meta_file), encoding="utf-8") as fh:
            meta = json.load(fh)
        descriptor = (meta or {}).get("subagentDescriptor") or {}
        return {"type": str(descriptor.get("typeName") or ""), "role": str(descriptor.get("role") or "")}
    except (OSError, ValueError, AttributeError):
        return {}


def scan_new_messages(payload: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Process subagent reports the primary has not seen yet (primary side only: bundled agents
    run with inheritCustomizations false, so no plugin hook runs inside them). Mutates `state`;
    the caller saves it.

    Reports land in <artifactDirectoryPath>/.system_generated/messages/<uuid>.json as
    {"sender": childCid, "recipient": parentCid, "content": "<report>"}. Per sender this records
    report_ok/status on the spawn record, queues one notice for an invalid report
    (state["invalid_report_senders"]) and arms the scout -> worker escalation on FAILED/BLOCKED.
    The first scan of a conversation with no known spawns only marks old reports as seen.
    """
    msg_dir = _messages_dir(payload)
    if msg_dir is None:
        return []
    me = str(payload.get("conversationId") or "")
    first_scan = "seen_messages" not in state
    seen: List[str] = state.setdefault("seen_messages", [])
    try:
        files = sorted(msg_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return []
    if first_scan and not state.get("pending_spawns") and not state.get("subagents"):
        # Plugin enabled mid-conversation: older reports must not arm an escalation.
        seen.extend(f.stem for f in files if f.name != "read.json")
        del seen[:-500]
        return []
    fresh = [f for f in files if f.name != "read.json" and f.stem not in seen]
    if not fresh:
        return []
    sync_subagents(payload, state)
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    events: List[Dict[str, Any]] = []
    for f in fresh:
        try:
            with open(str(f), encoding="utf-8") as fh:
                msg = json.load(fh)
        except (OSError, ValueError):
            continue  # possibly half-written; retry next time
        seen.append(f.stem)
        if not isinstance(msg, dict):
            continue
        sender = str(msg.get("sender") or "").strip()
        if not sender or (me and str(msg.get("recipient") or "") not in ("", me)):
            continue
        content = str(msg.get("content") or "")
        meta = _sender_meta(payload, sender)
        if not meta and sender.lower() not in subagents:
            continue  # not one of our subagents
        record = subagents.get(sender.lower()) or subagents.get(sender)
        if record is None:
            is_scout_meta = meta.get("type", "").lower() == SCOUT_TYPE
            record = subagents.setdefault(sender.lower(), {
                "role": meta.get("role", ""), "type": meta.get("type", ""),
                "model": "flash_lite" if is_scout_meta else "flash",
                "tier": "flash_lite" if is_scout_meta else "flash",
            })
        problem = validate_subagent_report(content)
        status = report_failure_status(content) or ("COMPLETED" if re.search(
            r"^[ \t>*_`-]*STATUS:[ \t*_`]*COMPLETED\b", content, re.IGNORECASE | re.MULTILINE) else "")
        record["report_ok"] = problem is None
        record["status"] = status
        label = record.get("role") or meta.get("role") or meta.get("type") or sender[:8]
        events.append({"sender": sender, "label": str(label), "status": status,
                       "report_ok": problem is None, "problem": problem})
        if problem is not None and not record.get("report_notified"):
            state.setdefault("invalid_report_senders", []).append(
                {"sender": sender, "label": str(label), "problem": problem})
            record["report_notified"] = True
        if status in ("FAILED", "BLOCKED") and escalation_enabled() and not record.get("escalation_sent"):
            route = escalation_for_record(record)
            if route is not None:
                record["escalation_sent"] = True
                start_escalation(state, route, "%s (%s)" % (label, sender[:8]))
    del seen[:-500]
    return events


def invalid_report_steps(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pop queued invalid-report notices as agy ephemeralMessage inject steps (one per subagent)."""
    steps: List[Dict[str, Any]] = []
    for item in state.pop("invalid_report_senders", []) or []:
        steps.append({
            "ephemeralMessage": (
                f"[Mitosis Orchestrator] Subagent '{item.get('label')}' broke the report contract "
                f"({item.get('problem')}). Treat its report as unverified: check the claimed work "
                "yourself or re-dispatch the task before relying on it."
            )
        })
    return steps


def _process_subagent_items(payload: Dict[str, Any], updated: List[Any]) -> Tuple[Optional[str], List[str]]:
    """Shared pipeline for one invoke_subagent call (Subagents list or the flat agy 1.4.6 form).

    Mutates `updated` (deep copies). Returns (deny_reason, changes).
    """
    changes: List[str] = []
    spawns: List[Dict[str, Any]] = []

    for idx, item in enumerate(updated):
        if isinstance(item, dict) and not str(_get(item, "Prompt", "prompt", default="")).strip():
            label = (_get(item, "Role", "role", default=None)
                     or str(_get(item, "TypeName", "typeName", default="")).strip() or f"subagent[{idx}]")
            return f"Subagent '{label}' has an empty Prompt.", changes

    items = [it for it in updated if isinstance(it, dict)]
    # Role is a short label on agy; only the Prompt is routed.
    decisions = [decide_subagent(it, use_role=False) for it in items]

    state = load_state(payload)
    scan_new_messages(payload, state)
    escalated_item = None
    if items and live_escalation(state) is not None:
        pick = next((i for i, d in enumerate(decisions) if d.tier == "flash_lite"), 0)
        new, info = apply_escalation(state, decisions[pick], "agy", payload)
        decisions[pick] = new
        if info and info.get("mode") == "flash":
            escalated_item = items[pick]
    decision_for = {id(it): d for it, d in zip(items, decisions)}

    for idx, item in enumerate(updated):
        if not isinstance(item, dict):
            continue
        current_model = str(_get(item, "Model", "model", default="inherit")).strip()
        current_type = str(_get(item, "TypeName", "typeName", default="")).strip()
        label = _get(item, "Role", "role", default=None) or current_type or f"subagent[{idx}]"
        prompt = str(_get(item, "Prompt", "prompt", default="")).strip()

        decision = decision_for[id(item)]
        target_model, type_override, reason = decision.tier, decision.type_override, decision.reason
        if item is escalated_item:
            type_override = WORKER_TYPE
        _record_routing(payload, decision, target_model, None, "agy",
                        current_type or type_override or (SCOUT_TYPE if target_model == "flash_lite" else WORKER_TYPE))
        if current_model != target_model or "model" in item:
            item.pop("model", None)
            item["Model"] = target_model
            changes.append(f"{label}: {current_model} -> {target_model} ({reason})")
        if type_override and current_type.lower() != type_override.lower():
            item.pop("typeName", None)
            item["TypeName"] = type_override
            changes.append(f"{label}: TypeName {current_type} -> {type_override}")
        if _append_report_format_enabled() and not prompt_has_report_format(prompt):
            item.pop("prompt", None)
            item["Prompt"] = append_report_format(prompt, target_model)
            changes.append(f"{label}: appended REPORT FORMAT")

        workspace = str(_get(item, "Workspace", "workspace", default="inherit")).strip().lower()
        spawns.append(
            {
                "role": str(label),
                "type": str(_get(item, "TypeName", "typeName", default="")),
                "model": target_model,
                "tier": "flash_lite" if target_model == "flash_lite" else "flash",
                "workspace": workspace,
                "active_branch": workspace == "branch",
                **({"escalated": True} if item is escalated_item else {}),
            }
        )

    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)
    return None, changes


def _handle_invoke_subagent(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    key = "Subagents" if "Subagents" in args else ("subagents" if "subagents" in args else None)
    if key is None or not isinstance(args.get(key), list):
        flat = any(k in args for k in ("Prompt", "prompt", "TypeName", "typeName"))
        if key is not None or not flat:
            return {"decision": "allow"}
        # agy 1.4.6: one flat call per subagent; args itself is the single item.
        item = copy.deepcopy(args)
        deny, changes = _process_subagent_items(payload, [item])
        if deny:
            return {"decision": "deny", "reason": deny}
        if not changes:
            return {"decision": "allow"}
        overwrite: Dict[str, Any] = {}
        for canon, lower in _SPELLINGS:
            if canon in item and item[canon] != args.get(canon):
                # Keep the caller's spelling, except Model which is always "Model".
                out_key = lower if (lower in args and canon != "Model") else canon
                overwrite[out_key] = item[canon]
        return {
            "decision": "allow",
            "reason": "Mitosis Orchestrator pinned subagent model(s): " + "; ".join(changes),
            "overwrite": overwrite,
        }

    if not args[key]:
        return {"decision": "deny", "reason": "Subagents list cannot be empty."}

    updated: List[Any] = copy.deepcopy(args[key])
    deny, changes = _process_subagent_items(payload, updated)
    if deny:
        return {"decision": "deny", "reason": deny}
    if not changes:
        return {"decision": "allow"}
    return {
        "decision": "allow",
        "reason": "Mitosis Orchestrator pinned subagent model(s): " + "; ".join(changes),
        "overwrite": {key: updated},
    }


def _handle_run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    violations: List[str] = []
    for text in _iter_strings(args):
        if "agent(" in text or "agent (" in text:
            violations.extend(workflow_model_violations(text))
    if not violations:
        return {"decision": "allow"}
    return {
        "decision": "deny",
        "reason": (
            "run_workflow script must pass model=\"flash\" or model=\"flash_lite\" to every "
            "agent(...) call: " + "; ".join(violations[:5])
        ),
    }


def _handle_define_subagent(args: Dict[str, Any]) -> Dict[str, Any]:
    if args.get("enable_subagent_tools") is True:
        return {
            "decision": "allow",
            "reason": "Mitosis Orchestrator: subagents may not spawn subagents (enable_subagent_tools forced to false).",
            "overwrite": {"enable_subagent_tools": False},
        }
    return {"decision": "allow"}


def _handle_manage_subagents(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(_get(args, "Action", "action", default="")).strip().lower()
    if action not in {"kill", "kill_all"}:
        return {"decision": "allow"}
    state = load_state(payload)
    sync_subagents(payload, state)
    subagents = state.setdefault("subagents", {})
    pending = state.setdefault("pending_spawns", [])
    if action == "kill_all":
        for meta in subagents.values():
            meta["active_branch"] = False
        for meta in pending:
            meta["active_branch"] = False
    else:
        targets = {str(c).strip().lower() for c in (_get(args, "ConversationIds", "conversationIds", default=[]) or [])}
        unmatched = 0
        for cid in targets:
            if cid in subagents:
                subagents[cid]["active_branch"] = False
            else:
                unmatched += 1
        # Kills that target spawns we never paired with a conversationId.
        for meta in pending:
            if unmatched <= 0:
                break
            if meta.get("active_branch"):
                meta["active_branch"] = False
                unmatched -= 1
    save_state(payload, state)
    return {"decision": "allow"}
