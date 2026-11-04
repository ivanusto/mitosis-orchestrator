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
from .. import heartbeat
from ..guards import _iter_strings
from ..models import _delegation_hint, claude_tier_effort, claude_tier_model
from .codex import _codex_agent_key, _is_opaque_message
from ..report import (
    REPORT_BLOCK_REASON,
    _append_report_format_enabled,
    append_report_format,
    prompt_has_report_format,
    _report_problem,
    store_report_problem,
)
from ..pin_verify import claude_handback_report, claude_transcript_models, record_pin_verdict
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


def _pin_builtins_enabled() -> bool:
    """CLAUDE_MITOSIS_PIN_BUILTINS=1 opts in to pinning Claude Code's Explore and general-purpose agents."""
    return _get_env("CLAUDE_MITOSIS_PIN_BUILTINS", default="0") == "1"


def _pinnable_builtin(subagent_type: str) -> str:
    """'explore' or 'general-purpose' for an unprefixed built-in type when the flag is on, else ''."""
    bare = (subagent_type or "").strip().lower()
    if bare in ("explore", "general-purpose") and _pin_builtins_enabled():
        return bare
    return ""


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

    builtin = _pinnable_builtin(subagent_type)
    if builtin:
        if model_req in CLAUDE_SUBAGENT_MODELS:
            return _passthrough(f"explicit model '{model_req}' honoured")
        if builtin == "explore":
            return _pin_explore(payload, tool_name, args, description, workspace, prompt)
    else:
        in_scope, scope_reason = _claude_subagent_in_scope(subagent_type)
        if not in_scope:
            return _passthrough(scope_reason)

        # Untyped call with an explicit, valid Claude model: honour the caller's choice.
        if not subagent_type and model_req in CLAUDE_SUBAGENT_MODELS:
            return _passthrough(f"explicit model '{model_req}' honoured")

    # A pinned general-purpose call is routed, recorded and escalated exactly like an untyped one.
    routed_type = "" if builtin else subagent_type

    prefix = ""
    bare_type = routed_type
    if ":" in bare_type:
        prefix_part, bare_type = bare_type.split(":", 1)
        prefix = f"{prefix_part}:"
        bare_type = bare_type.strip()

    # Map Claude Code model strings for classification
    internal_model = "flash_lite" if model_req in ("haiku", "flash_lite") else ("flash" if model_req in ("sonnet", "flash") else model_req)

    item = {
        "TypeName": routed_type,
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

    target_type = type_override or routed_type
    _record_routing(payload, decision, claude_model, updated_input.get("effort") or claude_effort, "claude",
                    target_type or (SCOUT_TYPE if target_tier == "flash_lite" else WORKER_TYPE))
    spawns = [{
        "role": description or target_type or tool_name.lower(),
        "type": target_type,
        "model": target_tier,
        "tier": target_tier,
        "pinned_model": claude_model,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    if builtin:
        spawns[0]["builtin"] = builtin
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


def _pin_explore(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any], description: str,
                 workspace: str, prompt: str) -> Dict[str, Any]:
    """Opt-in pin of the built-in Explore agent to the scout model and effort.

    Deliberately skips routing, apply_escalation (an armed escalation must survive) and the REPORT FORMAT
    (the report contract is not enforced for built-ins). Recorded so pin verification and stop pairing work.
    """
    claude_model = claude_tier_model("flash_lite")
    updated_input = copy.deepcopy(args)
    updated_input["model"] = claude_model
    caller_effort = str(args.get("effort") or "").strip().lower()
    if caller_effort in CLAUDE_EFFORT_LEVELS:
        effort: Optional[str] = caller_effort
        effort_note = f"effort {caller_effort} (caller)"
    else:
        effort = claude_tier_effort("flash_lite")
        if effort:
            updated_input["effort"] = effort
            effort_note = f"effort {effort}"
        else:
            if caller_effort:
                updated_input.pop("effort", None)
            effort_note = "effort unpinned"
    reason = "built-in Explore pinned (CLAUDE_MITOSIS_PIN_BUILTINS=1)"
    decision = Decision("flash_lite", None, effort, 1.0, "rule", "builtin-pin", reason)
    _record_routing(payload, decision, claude_model, effort, "claude", "Explore")
    state = load_state(payload)
    state.setdefault("pending_spawns", []).append({
        "role": description or "Explore",
        "type": str(args.get("subagent_type") or args.get("subagentType") or "Explore").strip(),
        "model": "flash_lite",
        "tier": "flash_lite",
        "pinned_model": claude_model,
        "workspace": workspace,
        "active_branch": workspace == "branch",
        "builtin": "explore",
    })
    save_state(payload, state)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"Mitosis Orchestrator pinned built-in Explore: {claude_model}, {effort_note} ({reason})",
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


def _bare_agent_type(type_name: str) -> str:
    bare = (type_name or "").strip().lower()
    return bare.split(":", 1)[1].strip() if ":" in bare else bare


def _pending_spawn_index(pending: List[Dict[str, Any]], hint: str, from_spawn: bool) -> Optional[int]:
    """Which pending spawn belongs to this agent id, or None when it has no claim on any of them.

    SubagentStop also fires for agents nobody dispatched through a pinned call (Claude Code's internal
    forks, built-ins such as Explore), so a stop never takes a pending spawn just because it is the oldest.
    """
    bare = _bare_agent_type(hint)
    if bare:
        for i, spawn in enumerate(pending):
            if _bare_agent_type(str(spawn.get("type") or "")) == bare:
                return i
    plugin_hint = _is_plugin_agent_type(hint)
    if from_spawn:
        # The id comes from an Agent tool response, so this is a real launch. Its type may differ from the
        # pending one (the hook overrode it). A typed built-in call left unpinned has no pending entry.
        if bare == "general-purpose" and _pin_builtins_enabled():
            # CLAUDE_MITOSIS_PIN_BUILTINS=1: only a general-purpose call this plugin pinned (recorded untyped).
            for i, spawn in enumerate(pending):
                if spawn.get("builtin") == "general-purpose":
                    return i
            return None
        if bare and not plugin_hint:
            return None
        accepted = lambda t: not t or _is_plugin_agent_type(t)  # noqa: E731
    elif plugin_hint:
        accepted = _is_plugin_agent_type
    elif bare == "general-purpose":
        accepted = lambda t: not t  # noqa: E731  (an untyped pinned call stops as general-purpose)
    else:
        return None
    for i, spawn in enumerate(pending):
        if accepted(str(spawn.get("type") or "")):
            return i
    return None


def _claude_register_agent(state: Dict[str, Any], agent_id: str, agent_type_hint: str = "",
                           from_spawn: bool = False) -> Optional[Dict[str, Any]]:
    """Pair a Claude Code agent id with its pending spawn, or infer a record for a mitosis agent type.

    from_spawn: the id was read from an Agent tool response rather than a SubagentStop payload.
    """
    agent_id = (agent_id or "").strip().lower()
    if not agent_id:
        return None
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    if agent_id in subagents:
        return subagents[agent_id]
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    hint = (agent_type_hint or "").strip()
    idx = _pending_spawn_index(pending, hint, from_spawn)
    if idx is not None:
        record = pending.pop(idx)
    elif _is_plugin_agent_type(hint):
        tier = "flash_lite" if _bare_agent_type(hint) == SCOUT_TYPE else "flash"
        record = {
            "role": hint, "type": hint, "model": tier, "tier": tier,
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
    _note_builtin_stop(agent_type)
    cleared = False
    if record is not None:
        record["alive"] = False
        record["active_branch"] = False
        cleared = True
    if not cleared and not agent_id:  # a stop naming an unknown agent says nothing about the others
        for meta in state.setdefault("pending_spawns", []):
            meta["active_branch"] = False
        for meta in state.setdefault("subagents", {}).values():
            meta["active_branch"] = False

    # A systemMessage on SubagentStop never reaches the screen, so handle_stop delivers the warning.
    pin_warning = _claude_verify_pin(record, payload)
    if pin_warning:
        state.setdefault("pin_warnings", []).append(pin_warning)

    # Auto mode delivers the report through SubagentHandback, which leaves last_assistant_message unset.
    # PostToolUse keeps it; the transcript is only a fallback because it may lag behind this event.
    message = str(payload.get("last_assistant_message") or "")
    stored = state.get("handback_reports", {}).pop(agent_id, "") if isinstance(state.get("handback_reports"), dict) else ""
    handback = "" if message.strip() else (stored or claude_handback_report(payload.get("agent_transcript_path")))
    report = handback or message

    # One save for every outcome (the report_nagged flag is part of the same write).
    result: Dict[str, Any] = {}
    type_name = agent_type or (str(record.get("type") or "") if record else "")
    enforce = _get_env("CLAUDE_MITOSIS_ENFORCE_REPORT", default="1") != "0"
    # An untyped pinned Agent call stops as general-purpose: no report contract, but a failure still escalates.
    untyped_pinned = (record is not None and not str(record.get("type") or "").strip()
                      and bool(record.get("pinned_model")) and _bare_agent_type(agent_type) in ("", "general-purpose"))
    if _is_plugin_agent_type(type_name):
        scout = _bare_agent_type(type_name) == SCOUT_TYPE or (record is not None and _record_tier(record) == "flash_lite")
        found = _report_problem(report, require_log_uri=not scout)
        error = found[1] if found else None
        if record is not None and report.strip():
            record["report_ok"] = found is None
            store_report_problem(record, found)
        key = agent_id or type_name
        # A handed-back report is already delivered, and a stop without any report gives nothing to judge.
        will_block = (error is not None and enforce and payload.get("stop_hook_active") is not True and not handback
                      and "last_assistant_message" in payload and key not in state.setdefault("report_nagged", []))
        if will_block:
            state["report_nagged"].append(key)
            result = {"decision": "block", "reason": REPORT_BLOCK_REASON.format(error=error)}
        elif record is not None:
            # No retry can follow, so trust the declared status even when the report is malformed.
            _claude_escalate_on_stop(state, record, report, agent_id)
    elif untyped_pinned:
        _claude_escalate_on_stop(state, record, report, agent_id)
    save_state(payload, state)
    return result


def _note_builtin_stop(agent_type: str) -> None:
    """Heartbeat counter of built-in agent stops. Only an allowlisted name is ever stored, never text."""
    try:
        bare = _bare_agent_type(agent_type)
        if _is_plugin_agent_type(agent_type) or bare not in CLAUDE_BUILTIN_AGENT_TYPES:
            return
        name = {"explore": "Explore", "plan": "Plan", "general-purpose": "general-purpose"}.get(bare, "other")
        heartbeat.note(builtin=name)
    except Exception:
        pass


def _claude_verify_pin(record: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> str:
    """Compare the pinned model with the subagent's transcript. Returns a one-time warning on a mismatch.

    Only records with pinned_model take part: forks, built-ins and synthetic records were never pinned.
    """
    if record is None or not record.get("pinned_model") or _get_env("CLAUDE_MITOSIS_VERIFY_PIN", default="1") == "0":
        return ""
    models = claude_transcript_models(payload.get("agent_transcript_path"))
    verdict = record_pin_verdict(record, record["pinned_model"], models)
    if verdict is not False or record.get("pin_warned"):
        return ""
    record["pin_warned"] = True
    return (f"[Mitosis Orchestrator] Subagent '{record.get('role') or record.get('type')}' was pinned to "
            f"{record['pinned_model']} but ran on {record.get('actual_model')}, possibly a model fallback. "
            "Run scripts/mitosis-doctor.py.")


def _claude_escalate_on_stop(state: Dict[str, Any], record: Dict[str, Any], message: str, agent_id: str) -> None:
    """Async Agent calls deliver their report only here: start an escalation for a FAILED/BLOCKED one."""
    if record.get("escalation_sent") or report_failure_status(message) is None:
        return
    rtype = str(record.get("type") or "")
    if rtype and not _is_plugin_agent_type(rtype):
        return
    route = escalation_for_record(record)
    if route is None:
        return
    record["escalation_sent"] = True
    start_escalation(state, route, agent_id or str(record.get("role") or "subagent"))
