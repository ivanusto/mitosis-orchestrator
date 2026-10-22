"""Antigravity (agy) subagent tools."""

import copy
from typing import Any, Dict, List

from ..common import SCOUT_TYPE, WORKER_TYPE, _get
from ..guards import _iter_strings, workflow_model_violations
from ..report import _append_report_format_enabled, append_report_format, prompt_has_report_format
from ..routing import decide_subagent
from ..state import _record_routing, load_state, save_state, sync_subagents


def _handle_invoke_subagent(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    key = "Subagents" if "Subagents" in args else ("subagents" if "subagents" in args else None)
    if key is None or not isinstance(args.get(key), list):
        return {"decision": "allow"}
    if not args[key]:
        return {"decision": "deny", "reason": "Subagents list cannot be empty."}

    updated: List[Any] = copy.deepcopy(args[key])
    changes: List[str] = []
    spawns: List[Dict[str, Any]] = []

    for idx, item in enumerate(updated):
        if not isinstance(item, dict):
            continue
        current_model = str(_get(item, "Model", "model", default="inherit")).strip()
        current_type = str(_get(item, "TypeName", "typeName", default="")).strip()
        label = _get(item, "Role", "role", default=None) or current_type or f"subagent[{idx}]"
        prompt = str(_get(item, "Prompt", "prompt", default="")).strip()
        if not prompt:
            return {"decision": "deny", "reason": f"Subagent '{label}' has an empty Prompt."}

        decision = decide_subagent(item)
        target_model, type_override, reason = decision.tier, decision.type_override, decision.reason
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
            }
        )

    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

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
