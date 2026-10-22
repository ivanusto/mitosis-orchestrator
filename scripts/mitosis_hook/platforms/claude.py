"""Claude Code Task/Agent handling."""

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from ..common import (
    CLAUDE_BUILTIN_AGENT_TYPES,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_PLUGIN_PREFIX,
    CLAUDE_SUBAGENT_MODELS,
    Decision,
    SCOUT_TYPE,
    WORKER_TYPE,
    _allow,
    _deny,
    _get,
    _get_env,
    _no_opinion,
    is_claude_code_payload,
    is_codex_hook_payload,
)
from ..guards import _iter_strings
from ..models import _delegation_hint, claude_tier_effort, claude_tier_model
from .codex import _codex_agent_key, _is_opaque_message
from ..report import (
    REPORT_BLOCK_REASON,
    _append_report_format_enabled,
    append_report_format,
    prompt_has_report_format,
    validate_subagent_report,
)
from ..routing import NUMBERED_RULE_RE, decide_subagent, find_mutation
from ..state import (
    apply_escalation,
    escalation_for_record,
    report_failure_status,
    start_escalation,
    _peek_state,
    _record_routing,
    _record_tier,
    load_state,
    save_state,
    sync_subagents,
)


CLAUDE_AGENT_ID_RE = re.compile(r"agentId\"?\s*[:=]\s*\"?([0-9a-f]{8,})", re.IGNORECASE)


def _claude_subagent_in_scope(subagent_type: str) -> Tuple[bool, str]:
    """Return (in_scope, reason). Only this plugin's own agent types and untyped calls are pinned."""
    raw = (subagent_type or "").strip()
    if not raw:
        return True, "untyped Agent call"
    prefix, bare = "", raw.lower()
    if ":" in bare:
        prefix, bare = bare.split(":", 1)
        prefix, bare = prefix.strip(), bare.strip()
    if bare in (WORKER_TYPE, SCOUT_TYPE) and prefix in ("", CLAUDE_PLUGIN_PREFIX):
        return True, f"{bare} type"
    if prefix and prefix != CLAUDE_PLUGIN_PREFIX:
        return False, f"agent '{raw}' belongs to plugin '{prefix}'"
    if bare in CLAUDE_BUILTIN_AGENT_TYPES:
        return False, f"built-in agent type '{raw}'"
    return False, f"unknown agent type '{raw}' left untouched"


def _passthrough_tier(subagent_type: str, model_req: str) -> str:
    bare = subagent_type.split(":")[-1].strip().lower()
    if bare == SCOUT_TYPE:
        return "flash_lite"
    if bare == WORKER_TYPE:
        return "flash"
    if model_req in ("haiku", "flash_lite"):
        return "flash_lite"
    return "flash"


def _handle_claude_task_or_agent(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _deny(f"{tool_name} prompt cannot be empty.", payload)

    subagent_type = str(args.get("subagent_type") or args.get("subagentType") or "").strip()
    description = str(args.get("description") or args.get("role") or "").strip()
    isolation = str(args.get("isolation") or args.get("workspace") or "inherit").strip().lower()
    workspace = "branch" if isolation == "worktree" else isolation
    model_req = str(args.get("model") or "inherit").strip().lower()

    def _passthrough(reason: str) -> Dict[str, Any]:
        # Still track worktree spawns so the Stop reminder can nag about live branches.
        if workspace == "branch":
            state = load_state(payload)
            state.setdefault("pending_spawns", []).append({
                "role": description or subagent_type or tool_name.lower(),
                "type": subagent_type,
                "model": model_req,
                "tier": _passthrough_tier(subagent_type, model_req),
                "workspace": workspace,
                "active_branch": True,
            })
            save_state(payload, state)
        return _allow(f"Mitosis Orchestrator: {reason}; not pinned.", payload=payload)

    if _get_env("CLAUDE_MITOSIS_PIN_SUBAGENTS", default="1") == "0":
        return _passthrough("subagent pinning disabled (CLAUDE_MITOSIS_PIN_SUBAGENTS=0)")

    in_scope, scope_reason = _claude_subagent_in_scope(subagent_type)
    if not in_scope:
        return _passthrough(scope_reason)

    # Untyped call with an explicit, valid Claude model: honour the caller's choice.
    if not subagent_type and model_req in CLAUDE_SUBAGENT_MODELS:
        return _passthrough(f"explicit model '{model_req}' honoured")

    prefix = ""
    bare_type = subagent_type
    if ":" in bare_type:
        prefix_part, bare_type = bare_type.split(":", 1)
        prefix = f"{prefix_part}:"
        bare_type = bare_type.strip()

    # Map Claude Code model strings for classification
    internal_model = "flash_lite" if model_req in ("haiku", "flash_lite") else ("flash" if model_req in ("sonnet", "flash") else model_req)

    item = {
        "TypeName": subagent_type,
        "Role": description,
        "Prompt": prompt,
        "Workspace": workspace,
        "Model": internal_model,
    }

    decision = decide_subagent(item)
    target_tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    # Failure escalation: runs under the state lock, after any judge/network call in decide_subagent.
    state = load_state(payload)
    decision, esc_info = apply_escalation(state, decision, "claude", payload)
    target_tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    if esc_info and esc_info["mode"] == "flash" and bare_type.strip().lower() == SCOUT_TYPE:
        type_override = WORKER_TYPE  # untyped calls stay untyped (model + effort only)
    claude_model = esc_info["model"] if esc_info and esc_info["mode"] == "primary" else claude_tier_model(target_tier)

    updated_input = copy.deepcopy(args)
    updated_input["model"] = claude_model
    # Effort: honour an explicit, valid caller value; otherwise pin per tier
    # (scouts low, workers medium) unless CLAUDE_MITOSIS_PIN_EFFORT=0.
    caller_effort = str(args.get("effort") or "").strip().lower()
    if caller_effort in CLAUDE_EFFORT_LEVELS:
        claude_effort: Optional[str] = caller_effort
        effort_note = f"effort {caller_effort} (caller)"
    else:
        claude_effort = claude_tier_effort(target_tier)
        if claude_effort:
            updated_input["effort"] = claude_effort
            effort_note = f"effort {claude_effort}"
        else:
            if caller_effort:
                updated_input.pop("effort", None)  # drop an invalid caller value
            effort_note = "effort unpinned"
    if type_override:
        if prefix and not type_override.startswith(prefix):
            type_override = f"{prefix}{type_override}"
        if "subagentType" in updated_input and "subagent_type" not in updated_input:
            updated_input["subagentType"] = type_override
        else:
            updated_input["subagent_type"] = type_override

    if _append_report_format_enabled() and not prompt_has_report_format(prompt):
        updated_input["prompt"] = append_report_format(prompt, target_tier)
        effort_note += ", REPORT FORMAT appended"

    target_type = type_override or subagent_type
    _record_routing(payload, decision, claude_model, updated_input.get("effort") or claude_effort, "claude",
                    target_type or (SCOUT_TYPE if target_tier == "flash_lite" else WORKER_TYPE))
    spawns = [{
        "role": description or target_type or tool_name.lower(),
        "type": target_type,
        "model": target_tier,
        "tier": target_tier,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    if esc_info:
        spawns[0]["escalated"] = True
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    reason_msg = f"Mitosis Orchestrator pinned subagent model: {claude_model}, {effort_note} ({reason})"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason_msg,
            "updatedInput": updated_input,
        }
    }


def _handle_agent_message(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Guard follow-up messages (SendMessage, send_message, followup_task) sent to a scout."""
    hook_protocol = is_claude_code_payload(payload) or is_codex_hook_payload(payload)
    # Codex keys agents by task path ("target": "probe" or "/root/probe"); Claude and agy by id.
    recipient = _codex_agent_key(_get(args, "Recipient", "recipient", "target", "task_name", "to", "To", default=""))
    message = str(_get(args, "Message", "message", default=""))
    tool_label = {"SendMessage": "SendMessage.message", "followup_task": "followup_task.message"}.get(
        tool_name, "send_message.message" if hook_protocol else "send_message.Message")

    def deny(reason: str) -> Dict[str, Any]:
        return _deny(reason, payload) if hook_protocol else {"decision": "deny", "reason": reason}

    def allow() -> Dict[str, Any]:
        return _no_opinion(payload) if hook_protocol else {"decision": "allow"}

    if not message.strip():
        return deny(f"{tool_label} cannot be empty.")

    # followup_task starts a new turn on the recipient, so it is routed like a fresh task.
    # Classify before load_state: the judge may be called over HTTP and the lock must not be held.
    followup: Optional[Decision] = None
    if tool_name == "followup_task" and not _is_opaque_message(message):
        peek = _peek_state(payload).get("subagents", {}).get(recipient)
        if peek and _record_tier(peek) == "flash_lite":
            followup = decide_subagent({"Prompt": message, "Role": "", "Workspace": "inherit", "TypeName": ""})

    state = load_state(payload)
    sync_subagents(payload, state)
    save_state(payload, state)

    info = state.get("subagents", {}).get(recipient)
    if not info or _record_tier(info) != "flash_lite":
        return allow()

    scout = f"Recipient '{recipient}' is a flash_lite '{info.get('type') or SCOUT_TYPE}' subagent"
    if _is_opaque_message(message):
        return deny(
            f"{scout}. Codex hides {tool_name} text from hooks, so it cannot be checked for code "
            f"changes: scouts take one lookup each. Spawn a new {_delegation_hint(payload, 'flash_lite')} "
            f"for another lookup, or use {_delegation_hint(payload, 'flash')} for changes or multi-step work."
        )

    mutation = find_mutation(message)
    rule_count = len(NUMBERED_RULE_RE.findall(message))
    if mutation or rule_count > 3:
        why = f"mutation verb '{mutation}'" if mutation else f"{rule_count} numbered steps"
        return deny(
            f"{scout} ({why}). Do not send code changes or multi-step (>3 rules) tasks to "
            f"a scout. Use {_delegation_hint(payload, 'flash')} instead."
        )
    if followup is not None and followup.tier == "flash":
        return deny(
            f"{scout} and this follow-up is worker work ({followup.reason}). "
            f"Use {_delegation_hint(payload, 'flash')} instead."
        )
    return allow()


def _claude_register_agent(state: Dict[str, Any], agent_id: str, agent_type_hint: str = "") -> Optional[Dict[str, Any]]:
    """Pair a Claude Code agent id with the oldest pending spawn (or infer a record from the type)."""
    agent_id = (agent_id or "").strip().lower()
    if not agent_id:
        return None
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    if agent_id in subagents:
        return subagents[agent_id]
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    hint = (agent_type_hint or "").strip()
    bare_hint = hint.split(":", 1)[1].lower() if ":" in hint else hint.lower()
    idx = next((i for i, p in enumerate(pending) if bare_hint and str(p.get("type", "")).split(":")[-1].lower() == bare_hint), None)
    if idx is None and pending:
        idx = 0
    if idx is not None:
        record = pending.pop(idx)
    elif bare_hint:
        record = {
            "role": hint, "type": hint,
            "model": "flash_lite" if bare_hint == SCOUT_TYPE else "flash",
            "tier": "flash_lite" if bare_hint == SCOUT_TYPE else "flash",
            "workspace": "inherit", "active_branch": False,
        }
    else:
        return None
    record["agent_id"] = agent_id
    record["alive"] = True
    subagents[agent_id] = record
    return record


def _claude_agent_ids_from_response(payload: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    resp = payload.get("tool_response")
    # Async launches answer {"isAsync": true, "status": "async_launched", "agentId": "..."}.
    if isinstance(resp, dict) and isinstance(resp.get("agentId"), str) and re.fullmatch(r"[0-9a-fA-F]{8,}", resp["agentId"]):
        ids.append(resp["agentId"].lower())
    for text in _iter_strings(resp):
        for m in CLAUDE_AGENT_ID_RE.finditer(text):
            aid = m.group(1).lower()
            if aid not in ids:
                ids.append(aid)
    return ids


def _is_plugin_agent_type(type_name: str) -> bool:
    bare = (type_name or "").strip().lower()
    if ":" in bare:
        prefix, bare = bare.split(":", 1)
        if prefix.strip() not in ("", CLAUDE_PLUGIN_PREFIX):
            return False
    return bare.strip() in (SCOUT_TYPE, WORKER_TYPE)


def _handle_claude_subagent_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state(payload)
    agent_id = str(payload.get("agent_id") or "").strip().lower()
    agent_type = str(payload.get("agent_type") or "").strip()
    record = _claude_register_agent(state, agent_id, agent_type)
    cleared = False
    if record is not None:
        record["alive"] = False
        record["active_branch"] = False
        cleared = True
    if not cleared:
        for meta in state.setdefault("pending_spawns", []):
            meta["active_branch"] = False
        for meta in state.setdefault("subagents", {}).values():
            meta["active_branch"] = False

    # One save for every outcome (the report_nagged flag is part of the same write).
    result: Dict[str, Any] = {}
    if payload.get("stop_hook_active") is not True and _get_env("CLAUDE_MITOSIS_ENFORCE_REPORT", default="1") != "0":
        type_name = agent_type or (str(record.get("type") or "") if record else "")
        if _is_plugin_agent_type(type_name):
            error = validate_subagent_report(str(payload.get("last_assistant_message") or ""))
            if error is not None:
                nagged: List[str] = state.setdefault("report_nagged", [])
                key = agent_id or type_name
                if key not in nagged:
                    nagged.append(key)
                    result = {"decision": "block", "reason": REPORT_BLOCK_REASON.format(error=error)}
            elif record is not None:
                _claude_escalate_on_stop(state, record, str(payload.get("last_assistant_message") or ""), agent_id)
    save_state(payload, state)
    return result


def _claude_escalate_on_stop(state: Dict[str, Any], record: Dict[str, Any], message: str, agent_id: str) -> None:
    """Async Agent calls deliver their report only here: start an escalation for a valid FAILED/BLOCKED one."""
    if record.get("escalation_sent") or not str(message).lstrip().upper().startswith("STATUS:"):
        return
    if report_failure_status(message) is None:
        return
    rtype = str(record.get("type") or "")
    if rtype and not _is_plugin_agent_type(rtype):
        return
    route = escalation_for_record(record)
    if route is None:
        return
    record["escalation_sent"] = True
    start_escalation(state, route, agent_id or str(record.get("role") or "subagent"))
