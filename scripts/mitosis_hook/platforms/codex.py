"""Codex collaboration tools and exec handling."""

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..common import (
    CODEX_SCOUT_ROLE,
    CODEX_WORKER_ROLE,
    Decision,
    SCOUT_TYPE,
    WORKER_TYPE,
    _allow,
    _deny,
    _no_opinion,
    is_claude_code_payload,
    is_codex_payload,
)
from ..guards import _parse_codex_exec, _rewrite_codex_exec_model
from ..models import codex_tier_effort, codex_tier_model
from .agy import _handle_invoke_subagent
from ..report import (
    _append_report_format_enabled,
    append_report_format,
    prompt_has_report_format,
    validate_subagent_report,
)
from ..routing import _tier_effort, decide_subagent, find_mutation
from ..state import (
    _RECENT_DECISIONS,
    _record_routing,
    _releases_state_locks,
    load_state,
    save_state,
    sync_subagents,
)


# Codex 0.163 (multi-agent v2) puts its agent tools in the `collaboration` namespace and
# names them in hook payloads with the namespace glued on: "collaborationspawn_agent".
CODEX_COLLAB_TOOLS = ("spawn_agent", "followup_task", "send_message", "wait_agent", "interrupt_agent", "list_agents")


CODEX_COLLAB_TOOL_RE = re.compile(r"^collaboration(?:__|[./:])?(" + "|".join(CODEX_COLLAB_TOOLS) + r")$")


# Codex 0.163 hands hooks the spawn_agent / followup_task / send_message text as an
# encrypted token ("gAAAAA..."), so no content rule can read it.
OPAQUE_MESSAGE_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]{20,}={0,2}$")


def _codex_tool_name(name: str) -> str:
    m = CODEX_COLLAB_TOOL_RE.match(name)
    return m.group(1) if m else name


def _is_opaque_message(text: str) -> bool:
    return bool(OPAQUE_MESSAGE_RE.match((text or "").strip()))


def _codex_agent_key(name: Any) -> str:
    """State key for a Codex agent: the last segment of its task path ("/root/probe" -> "probe")."""
    return str(name or "").strip().lower().rstrip("/").rsplit("/", 1)[-1]


def _extract_tool_call(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if is_codex_payload(payload):
        name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
        inp = payload.get("tool_input") or payload.get("args") or payload.get("arguments") or payload.get("input")
        if not name:
            tc = payload.get("tool_call") or payload.get("toolCall")
            if isinstance(tc, dict):
                name = str(tc.get("name") or "").strip()
                inp = tc.get("args") or tc.get("arguments") or tc.get("input")
        args = inp if isinstance(inp, dict) else {}
        return _codex_tool_name(name), args
    if is_claude_code_payload(payload):
        name = str(payload.get("tool_name") or "").strip()
        inp = payload.get("tool_input")
        args = inp if isinstance(inp, dict) else {}
        return name, args
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or "").strip()
        args = tool_call.get("args")
        return name, args if isinstance(args, dict) else {}
    return "", {}


def _handle_codex_exec_command(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any], cmd: str) -> Dict[str, Any]:
    parsed = _parse_codex_exec(cmd)
    if parsed is None:
        return _allow(payload=payload)
    prompt = parsed["prompt"]

    item = {"Prompt": prompt, "Role": "codex-subagent"}
    decision = decide_subagent(item)
    tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    target_model = codex_tier_model(tier)
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
    _record_routing(payload, decision, target_model, codex_tier_effort(tier), "codex", target_type)

    new_cmd = _rewrite_codex_exec_model(cmd, parsed, target_model)

    active_branch = "--worktree" in cmd
    spawns = [{
        "role": target_type,
        "type": target_type,
        "model": target_model,
        "tier": tier,
        "workspace": "branch" if active_branch else "inherit",
        "active_branch": active_branch,
    }]
    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    updated_args = copy.deepcopy(args)
    for key in ("command", "CommandLine", "commandLine", "cmd"):
        if key in updated_args:
            updated_args[key] = new_cmd

    return _allow(
        f"Mitosis Orchestrator pinned codex exec model to '{target_model}' ({reason})",
        updated_input=updated_args,
        payload=payload,
    )


def _handle_spawn_agent(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or args.get("instructions") or args.get("message") or "").strip()
    if not prompt:
        return _deny(f"{tool_name} prompt cannot be empty.", payload)
    role = str(args.get("role") or args.get("agent_type") or args.get("type") or args.get("name") or "").strip()
    workspace = str(args.get("workspace") or args.get("isolation") or "inherit").strip().lower()
    if workspace == "worktree":
        workspace = "branch"
    # Multi-agent v2 (Codex 0.163): task_name / fork_turns, no model parameter; the role file pins the model.
    task_name = str(args.get("task_name") or "").strip()
    v2 = "task_name" in args or "fork_turns" in args
    opaque = _is_opaque_message(prompt)
    if opaque:
        # The message is encrypted before hooks see it: route by the requested role and leave
        # the text alone (appending REPORT FORMAT would corrupt the token).
        bare = role.split(":")[-1].strip().lower()
        tier = "flash_lite" if bare in (SCOUT_TYPE, CODEX_SCOUT_ROLE, "scout") else "flash"
        decision = Decision(tier, None, _tier_effort(tier), 1.0 if bare else 0.5, "rule", "codex-role",
                            f"message hidden from hooks; routed by agent_type '{role or 'default'}'")
        _RECENT_DECISIONS.append(decision)
        del _RECENT_DECISIONS[:-16]
    else:
        decision = decide_subagent({
            "Prompt": prompt,
            "Role": role,
            "Workspace": workspace,
            "TypeName": role,
        })
    tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    target_model = codex_tier_model(tier)
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)

    updated_args = copy.deepcopy(args)
    if not v2:
        updated_args["model"] = target_model
    # Built-in Codex roles always exist; flash-lite-scout / flash-worker only exist after
    # scripts/setup-codex.py, and an unknown role makes Codex fail with "unknown agent_type".
    # Incoming flash-* names are accepted as aliases and rewritten to the built-in role.
    codex_role = CODEX_SCOUT_ROLE if tier == "flash_lite" else CODEX_WORKER_ROLE
    _record_routing(payload, decision, target_model, codex_tier_effort(tier), "codex", codex_role)
    for key in ("type", "agent_type"):
        if key in updated_args:
            updated_args[key] = codex_role
    if v2 and "agent_type" not in updated_args:
        # Without a role a v2 agent runs on the primary's (frontier) model.
        updated_args["agent_type"] = codex_role
    if not opaque and _append_report_format_enabled() and not prompt_has_report_format(prompt):
        prompt_key = next(k for k in ("prompt", "instructions", "message") if str(args.get(k) or "").strip())
        updated_args[prompt_key] = append_report_format(prompt, tier)

    record = {
        "role": codex_role,  # the Codex role sent in updatedInput.agent_type; "type" keeps flash-*
        "type": target_type,
        "model": target_model,
        "tier": tier,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }
    state = load_state(payload)
    if task_name:
        # The task name is the address followup_task / send_message use; register it now,
        # since PostToolUse is not guaranteed for collaboration tools.
        record["task_name"] = task_name
        state.setdefault("subagents", {})[_codex_agent_key(task_name)] = record
    else:
        state.setdefault("pending_spawns", []).append(record)
    save_state(payload, state)

    return _allow(
        f"Mitosis Orchestrator pinned subagent model: {target_model} ({reason})",
        updated_input=updated_args,
        payload=payload,
    )


def _handle_mitosis_tools(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Classification (which may call the judge over HTTP) always runs before load_state
    # takes the lock, so the lock is never held across network I/O.
    def _record(state_spawns: List[Dict[str, Any]]) -> None:
        state = load_state(payload)
        state.setdefault("pending_spawns", []).extend(state_spawns)
        save_state(payload, state)

    if tool_name == "mitosis_scout":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_scout prompt cannot be empty.", payload)
        mutation = find_mutation(prompt)
        if mutation:
            tier = "flash"
            target_model = codex_tier_model("flash")
            target_type = WORKER_TYPE
            reason = f"mutation verb '{mutation}' upgraded scout to worker"
        else:
            tier = "flash_lite"
            target_model = codex_tier_model("flash_lite")
            target_type = SCOUT_TYPE
            reason = "read-only scout"
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        _record_routing(payload, Decision(tier, None, _tier_effort(tier), 1.0, "rule", "scout-tool", reason),
                        target_model, codex_tier_effort(tier), "codex", target_type)
        _record([{
            "role": str(args.get("role") or target_type),
            "type": target_type,
            "model": target_model,
            "tier": tier,
            "workspace": "inherit",
            "active_branch": False,
        }])
        return _allow(f"Mitosis Orchestrator pinned scout: {target_model} ({reason})", updated_input=updated_args, payload=payload)

    if tool_name == "mitosis_worker":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_worker prompt cannot be empty.", payload)
        target_model = codex_tier_model("flash")
        workspace = str(args.get("workspace") or "inherit").strip().lower()
        active_branch = workspace in ("branch", "worktree")
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        _record_routing(payload, Decision("flash", None, _tier_effort("flash"), 1.0, "rule", "worker-tool",
                                          "mitosis_worker tool"),
                        target_model, codex_tier_effort("flash"), "codex", WORKER_TYPE)
        _record([{
            "role": str(args.get("role") or WORKER_TYPE),
            "type": WORKER_TYPE,
            "model": target_model,
            "tier": "flash",
            "workspace": "branch" if active_branch else workspace,
            "active_branch": active_branch,
        }])
        return _allow(f"Mitosis Orchestrator pinned worker: {target_model}", updated_input=updated_args, payload=payload)

    if tool_name == "mitosis_invoke_subagent":
        if "Subagents" in args or "subagents" in args:
            return _handle_invoke_subagent(payload, args)
        tasks = args.get("tasks")
        if isinstance(tasks, list):
            updated_tasks = copy.deepcopy(tasks)
            spawns = []
            for t in updated_tasks:
                if not isinstance(t, dict):
                    continue
                decision = decide_subagent(t)
                tier, reason = decision.tier, decision.reason
                model = codex_tier_model(tier)
                _record_routing(payload, decision, model, codex_tier_effort(tier), "codex",
                                SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
                t["model"] = model
                ws = str(t.get("workspace") or "inherit").strip().lower()
                spawns.append({
                    "role": str(t.get("role") or tier),
                    "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                    "model": model,
                    "tier": tier,
                    "workspace": ws,
                    "active_branch": ws in ("branch", "worktree"),
                })
            _record(spawns)
            updated_args = copy.deepcopy(args)
            updated_args["tasks"] = updated_tasks
            return _allow("Mitosis Orchestrator pinned tasks", updated_input=updated_args, payload=payload)
        prompt = str(args.get("prompt") or "").strip()
        if prompt:
            decision = decide_subagent(args)
            tier, reason = decision.tier, decision.reason
            model = codex_tier_model(tier)
            _record_routing(payload, decision, model, codex_tier_effort(tier), "codex",
                            SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
            updated_args = copy.deepcopy(args)
            updated_args["model"] = model
            _record([{
                "role": str(args.get("role") or tier),
                "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                "model": model,
                "tier": tier,
                "workspace": "inherit",
                "active_branch": False,
            }])
            return _allow(f"Mitosis Orchestrator pinned subagent: {model} ({reason})", updated_input=updated_args, payload=payload)
        return _deny("mitosis_invoke_subagent requires tasks or prompt.", payload)

    return _no_opinion(payload)


@_releases_state_locks
def handle_codex_notify(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state(payload)
    sync_subagents(payload, state)

    last_msg = str(payload.get("last-assistant-message") or payload.get("last_assistant_message") or "").strip()
    report_error = None
    if last_msg and ("STATUS:" in last_msg or "VERIFICATION:" in last_msg):
        report_error = validate_subagent_report(last_msg)

    active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
    active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]

    result: Dict[str, Any] = {"status": "ok", "decision": "allow"}
    if report_error:
        result["report_validation"] = "failed"
        result["report_error"] = report_error
    elif last_msg and ("STATUS:" in last_msg):
        result["report_validation"] = "passed"

    if active:
        key = ",".join(sorted(active))
        reminded: List[str] = state.setdefault("stop_reminded_sets", [])
        if key not in reminded:
            reminded.append(key)
            save_state(payload, state)
            result["decision"] = "continue"
            result["reason"] = (
                f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
                "Merge or discard their changes, then clean them up before finishing."
            )
            return result

    save_state(payload, state)
    return result


def _codex_confirm_spawn(state: Dict[str, Any], args: Dict[str, Any], response: Any) -> None:
    """Store the task path spawn_agent returned ({"task_name": "/root/probe"}) on its record."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return
    path = str(response.get("task_name") or "").strip() if isinstance(response, dict) else ""
    if not path:
        return
    subagents = state.setdefault("subagents", {})
    key = _codex_agent_key(path)
    record = subagents.get(key) or subagents.get(_codex_agent_key(args.get("task_name")))
    if record is not None:
        record["agent_path"] = path
        subagents[key] = record


_ROLLOUT_FIRST_LINE_CAP = 1024 * 1024


def _codex_rollout_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    """session_meta payload from the first line of the child's rollout JSONL ({} on any problem)."""
    for key in ("transcript_path", "agent_transcript_path"):
        path = payload.get(key)
        if not isinstance(path, str) or not path:
            continue
        try:
            with open(path, "rb") as fh:
                line = fh.readline(_ROLLOUT_FIRST_LINE_CAP)
            first = json.loads(line.decode("utf-8"))
            meta = first.get("payload") if isinstance(first, dict) else None
            if isinstance(meta, dict):
                return meta
        except Exception:
            continue
    return {}


def codex_bind_agent(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    """Map a Codex child uuid (payload.agent_id) to its task_name record; return the record key.

    Binds via the rollout's session_meta.agent_path, else via the only unbound record of the
    child's role. Writes record["agent_id"] and state["codex_agent_ids"][uuid]; None if unbound.
    """
    if not is_codex_payload(payload):
        return None
    uuid = str(payload.get("agent_id") or "").strip()
    if not uuid:
        return None
    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        return None
    ids = state.get("codex_agent_ids")
    known = ids.get(uuid) if isinstance(ids, dict) else None
    if known and isinstance(subagents.get(known), dict):
        return known

    key = None
    meta = _codex_rollout_meta(payload)
    agent_path = str(meta.get("agent_path") or "").strip()
    if agent_path and str(meta.get("id") or uuid) == uuid:
        key = _codex_agent_key(agent_path)
        if not isinstance(subagents.get(key), dict):
            key = None
    elif not agent_path:
        agent_type = str(payload.get("agent_type") or "").strip()
        cands = [k for k, r in subagents.items()
                 if isinstance(r, dict) and agent_type and r.get("role") == agent_type and not r.get("agent_id")]
        if len(cands) == 1:
            key = cands[0]
    if key is None:
        return None
    subagents[key]["agent_id"] = uuid
    state.setdefault("codex_agent_ids", {})[uuid] = key
    return key


def codex_subagent_stop(payload: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    """Codex SubagentStop fires after every child turn: update only the bound record."""
    key = codex_bind_agent(state, payload)
    if key is None:
        return None
    rec = state["subagents"][key]
    rec["active_branch"] = False
    rec["stops"] = int(rec.get("stops") or 0) + 1
    rec["last_turn_id"] = payload.get("turn_id")
    if rec.get("type") in (WORKER_TYPE, SCOUT_TYPE):
        rec["report_ok"] = validate_subagent_report(str(payload.get("last_assistant_message") or "")) is None
    return key
