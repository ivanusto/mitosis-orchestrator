"""Hook mode handlers."""

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import (
    CODEX_SCOUT_ROLE,
    MUTATION_TOOLS,
    READ_TOOLS,
    SAFE_TERMINATION_REASONS,
    SCOUT_TYPE,
    _allow,
    _ask,
    _deny,
    _env_flag,
    _get,
    _get_env,
    _no_opinion,
    is_claude_code_payload,
    is_codex_hook_payload,
    is_codex_payload,
    is_primary_agent,
)
from . import heartbeat
from .guards import (
    HEURISTIC_REFUSAL_PREFIX,
    _iter_strings,
    PATH_ARG_KEYS,
    PATH_LIST_ARG_KEYS,
    WRITE_TOOL_RE,
    _extract_apply_patch_files,
    _parse_codex_exec,
    _codex_nonposix_shell_reachable,
    _windows_mode,
    _within_roots,
    allowed_roots,
    command_stays_in_workspace,
    scout_command_is_read_only,
)
from .pin_verify import _tail_records
from .models import (
    POLICY_NOTICE,
    _delegation_hint,
    _read_codex_config_toml,
    _read_settings,
    check_primary_model_settings,
    claude_policy_notice,
    codex_config_path,
    codex_policy_notice,
    is_frontier_tier,
    is_orchestrator_executor,
    settings_path,
)
from .platforms.agy import (
    _handle_define_subagent,
    _handle_invoke_subagent,
    invalid_report_steps,
    scan_new_messages,
    _handle_manage_subagents,
    _handle_run_workflow,
)
from .platforms.claude import (
    _claude_agent_ids_from_response,
    _claude_register_agent,
    _is_plugin_agent_type,
    _handle_agent_message,
    _handle_claude_subagent_stop,
    _handle_claude_task_or_agent,
)
from .platforms.codex import (
    CODEX_COLLAB_TOOLS,
    _codex_agent_key,
    _codex_confirm_spawn,
    _extract_tool_call,
    _handle_codex_exec_command,
    _handle_mitosis_tools,
    _handle_spawn_agent,
    codex_escalate,
    codex_report_block,
    codex_subagent_stop,
)
from .routing import TEST_RUNNER_RE
from .state import (
    _RECENT_DECISIONS,
    _record_tier,
    escalation_enabled,
    escalation_for_record,
    primary_model_alias,
    report_failure_status,
    start_escalation,
    _peek_state,
    _read_state_file,
    _releases_state_locks,
    _state_file,
    load_state,
    save_state,
    sync_subagents,
)


LOG_OR_TRANSCRIPT_RE = re.compile(
    r"(?:^|[/\\])(?:transcript(?:_full)?\.jsonl|[^/\\]+\.log)$",
    re.IGNORECASE,
)


SMALL_LOG_MAX_BYTES = 16 * 1024


def _small_log_max_bytes() -> int:
    raw = _get_env("CODEX_MITOSIS_SMALL_LOG_BYTES", "CLAUDE_MITOSIS_SMALL_LOG_BYTES", "AGY_MITOSIS_SMALL_LOG_BYTES")
    if not raw:
        return SMALL_LOG_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return SMALL_LOG_MAX_BYTES


def _is_small_regular_file(target: str, cwd: str) -> bool:
    limit = _small_log_max_bytes()
    if limit <= 0 or not target:
        return False
    try:
        path = os.path.expanduser(target)
        if not os.path.isabs(path):
            if not cwd or not os.path.isabs(cwd):
                return False
            path = os.path.join(cwd, path)
        path = os.path.realpath(path)
        st = os.stat(path)
        return stat.S_ISREG(st.st_mode) and st.st_size <= limit
    except (OSError, ValueError):
        return False


@_releases_state_locks
def handle_session_start(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = payload or {}
    target_file = "config.toml" if (is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ) else "settings.json"
    if is_codex_hook_payload(p) and p.get("model"):
        # Codex plugin hooks report the live model; config.toml may name another.
        model, target_file = str(p["model"]), "the session"
        if is_frontier_tier(model):
            status = "ok"
        elif _env_flag("CODEX_MITOSIS_ALLOW_NON_PRIMARY", "AGY_MITOSIS_ALLOW_NON_PRIMARY"):
            status = "silenced"
        else:
            status = "warn"
    else:
        status, model = check_primary_model_settings(p)
    if status == "ok":
        note = f"Primary model is '{model}'."
    elif status == "pinned":
        note = f"Restored primary model pin to '{model}' in {target_file} (pin settings opted-in)."
    elif status == "warn":
        note = (
            f"WARNING: {target_file} primary model is '{model or 'unset'}', not a frontier model. "
            "Ask the user to switch to a supported frontier model (Gemini Argon/Carbon/Pro/Ultra, Gemini 4 Hydrogen High/XHigh, agy Orchestrator, Claude Opus/Sonnet, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before orchestrating, "
            "or set CODEX_MITOSIS_ALLOW_NON_PRIMARY=1 / AGY_MITOSIS_ALLOW_NON_PRIMARY=1 / CLAUDE_MITOSIS_ALLOW_NON_PRIMARY=1 to silence this."
        )
    elif status == "silenced":
        note = f"Primary model is '{model}' (non-frontier warnings silenced)."
    else:
        note = f"{target_file} not found; the live model is checked on every turn instead."

    if is_claude_code_payload(p) and str(p.get("model") or "").strip():
        # Remembered for ESCALATE_PRIMARY (the next worker spawn runs on the primary's model).
        state = load_state(p)
        state["primary_model"] = str(p["model"]).strip()
        save_state(p, state)

    if is_codex_hook_payload(p):
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            },
        }
    if is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ:
        return {
            "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            "systemMessage": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            },
        }
    if is_claude_code_payload(p):
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {claude_policy_notice()}",
            }
        }
    return {
        "injectSteps": [
            {"systemMessage": {"systemMessage": f"[Mitosis Orchestrator Active] {note} {POLICY_NOTICE}"}}
        ]
    }


def _turn_nudge_seconds() -> int:
    raw = _get_env("AGY_MITOSIS_TURN_NUDGE_SECONDS", default="300")
    try:
        return max(0, int(raw))
    except ValueError:
        return 300


def _turn_nudge_steps(payload: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """agy primary only: once per user request, tell the primary to write its deliverable when
    work was delegated and the request is taking long. Never blocks. The anchor is the user input,
    not invocationNum (agy resets that to 0 whenever a subagent message wakes the primary)."""
    last_input = payload.get("lastUserInput")
    last_input = last_input if isinstance(last_input, str) else ""
    now = time.time()
    anchor = state.get("turn_anchor")
    if not isinstance(anchor, dict) or anchor.get("input") != last_input:
        anchor = {"input": last_input, "started": now, "nudged": False}
        state["turn_anchor"] = anchor
    threshold = _turn_nudge_seconds()
    if threshold <= 0 or anchor.get("nudged"):
        return []
    try:
        elapsed = now - float(anchor.get("started"))
    except (TypeError, ValueError):
        anchor["started"] = now
        return []
    if elapsed < 0:
        anchor["started"] = now
        return []
    if elapsed < threshold:
        return []
    sync_subagents(payload, state)
    started = float(anchor["started"])
    mine: List[Dict[str, Any]] = []
    for group in (state.get("subagents"), state.get("pending_spawns")):
        records = group.values() if isinstance(group, dict) else (group or [])
        for r in records:
            stamp = r.get("dispatched_at") if isinstance(r, dict) else None
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp >= started:
                mine.append(r)
    if not mine:
        return []
    pending = sum(1 for r in mine if not r.get("status"))
    anchor["nudged"] = True
    minutes = max(1, int(elapsed // 60))
    tail = " (%d subagent(s) still pending)" % pending if pending else ""
    return [{"ephemeralMessage": (
        f"[Mitosis Orchestrator] {minutes} min since this request and you have delegated work{tail}. "
        "Write the deliverable now from the evidence you have, mark what is unverified, and do not dispatch "
        "new research or follow-ups; fold in late reports afterwards."
    )}]


@_releases_state_locks
def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    # A subagent's own prompt (Codex sends UserPromptSubmit with agent_id) is
    # neither a new primary turn nor a primary model to check.
    if payload.get("agent_id"):
        return {}
    state = load_state(payload)
    if is_codex_hook_payload(payload) and payload.get("cwd") and isinstance(payload["cwd"], str):
        # Codex reports a command's workdir as the PreToolUse cwd; the turn cwd is only seen here.
        state["codex_turn_cwd"] = payload["cwd"]
    agy = _is_agy_payload(payload)
    inv = payload.get("invocationNum", 0)
    # agy fires PreInvocation on every model call; its invocationNum returns to 0 on a new user turn.
    if not agy or isinstance(inv, bool) or not isinstance(inv, int) or inv == 0:
        # After a Stop-hook "continue" agy restarts at invocationNum 0 with the same user input: not a new turn.
        kept = state.pop("breaker_keep_for_input", None)
        resumed = agy and kept is not None and kept == payload.get("lastUserInput")
        advised = state.get("fastpath_advised")
        keep_advised = ["circuit_breaker"] if resumed and isinstance(advised, list) and "circuit_breaker" in advised else []
        state["turn_edited_files"] = []
        state["fastpath_advised"] = keep_advised
        if not resumed:
            state["consecutive_failures"] = 0
        state.pop("breaker_steps", None)
    agy_steps: List[Dict[str, Any]] = []
    if agy:
        # agy primary: read newly delivered subagent reports (arms escalation, flags invalid ones).
        scan_new_messages(payload, state)
        agy_steps = invalid_report_steps(state)
        _breaker_scan_agy_transcript(payload, state)
        _breaker_advise_agy(payload, state)
        for text in state.pop("fastpath_pending_advice", None) or []:
            agy_steps.append({"ephemeralMessage": text})
        agy_steps.extend(_turn_nudge_steps(payload, state))
    save_state(payload, state)

    model_name = str(payload.get("modelName") or payload.get("model_name") or payload.get("model") or "")
    if not model_name and (is_codex_payload(payload) or "CODEX_CONFIG_PATH" in os.environ):
        data, _ = _read_codex_config_toml(codex_config_path(payload))
        if data and isinstance(data, dict):
            model_name = str(data.get("model") or "")
    elif not model_name and is_claude_code_payload(payload):
        data, _ = _read_settings(settings_path(payload))
        if data and isinstance(data, dict):
            model_name = str(data.get("model") or "")

    agy_executor = (
        is_orchestrator_executor(model_name)
        and not is_codex_payload(payload)
        and not is_claude_code_payload(payload)
    )
    if not model_name or is_frontier_tier(model_name) or agy_executor or _env_flag(
        "CODEX_MITOSIS_ALLOW_NON_PRIMARY",
        "CLAUDE_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_ALLOW_NON_MITOSIS_PRIMARY",
        "AGY_ALLOW_NON_ARGON_PRIMARY",
        "AGY_ARGON_ALLOW_NON_PRIMARY",
    ):
        return {"injectSteps": agy_steps} if agy_steps else {}
    cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or "")
    inv_num = payload.get("invocationNum", 0)
    key = f"{cid}:{inv_num}"
    warned: List[str] = state.setdefault("warned_invocations", [])
    if key in warned:
        return {"injectSteps": agy_steps} if agy_steps else {}
    warned.append(key)
    save_state(payload, state)

    warn_text = (
        f"[Mitosis Orchestrator] The active session model is '{model_name}', which is "
        "not a frontier model. Tell the user to switch Model Selection to "
        "a supported frontier model (Claude Opus/Sonnet, Gemini Argon/Carbon/Pro/Ultra, Gemini 4 Hydrogen High/XHigh, agy Orchestrator, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before continuing orchestration work."
    )
    if is_codex_hook_payload(payload):
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": warn_text}}
    if is_codex_payload(payload):
        return {
            "additionalContext": warn_text,
            "systemMessage": warn_text,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn_text,
            },
        }
    if is_claude_code_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn_text,
            }
        }
    return {"injectSteps": agy_steps + [{"ephemeralMessage": warn_text}]}


def _turn_cwd_payload(payload: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """For a primary Codex hook call, a payload copy whose cwd is the stored turn cwd.

    Codex puts the command's resolved workdir in the PreToolUse cwd, so roots and the worktree test
    must use the turn cwd. Subagents and payloads without a stored turn cwd come back unchanged.
    """
    if not is_codex_hook_payload(payload) or payload.get("agent_id"):
        return payload
    if state is None:
        state = _peek_state(payload)
    turn_cwd = (state or {}).get("codex_turn_cwd")
    if not turn_cwd or not isinstance(turn_cwd, str):
        return payload
    return {**payload, "cwd": turn_cwd}


def _handle_workspace_guard(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Opt-in: the plugin ships with the scope guard off. Set AGY_MITOSIS_AUTO_ALLOW=1 (or CLAUDE_MITOSIS_AUTO_ALLOW=1 / CODEX_MITOSIS_AUTO_ALLOW=1)
    # to let project-scoped file and command tools skip the permission prompt.
    # When unset or for tools without path arguments, pass through without a verdict.
    if not _env_flag("CODEX_MITOSIS_AUTO_ALLOW", "CLAUDE_MITOSIS_AUTO_ALLOW", "AGY_MITOSIS_AUTO_ALLOW", "AGY_ARGON_AUTO_ALLOW"):
        return _no_opinion(payload)
    roots = allowed_roots(_turn_cwd_payload(payload))
    if not roots:
        return _no_opinion(payload)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cwd = str(_get(args, "cwd", "Cwd", default="") or payload.get("cwd") or "")
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "")
        if not cmd:
            return _no_opinion(payload)
        if cwd and not _within_roots(cwd, roots):
            return _ask("Mitosis Orchestrator: Cwd is outside the workspace.", payload=payload)
        ok, why = command_stays_in_workspace(cmd, cwd, roots)
        if ok:
            return _allow("Mitosis Orchestrator: command stays inside the workspace.", overrides=[f"command({cmd})"], payload=payload)
        if why.startswith(HEURISTIC_REFUSAL_PREFIX) and is_codex_hook_payload(payload):
            return _no_opinion(payload)  # a guess must not become a hard deny: _ask is a deny on Codex
        return _ask(f"Mitosis Orchestrator: {why}.", payload=payload)

    if tool_name == "apply_patch":
        targets = _extract_apply_patch_files(args)
        if not targets:
            return _no_opinion(payload)
        outside = [t for t in targets if not _within_roots(t, roots)]
        if outside:
            return _ask(f"Mitosis Orchestrator: {outside[0]!r} is outside the workspace.", payload=payload)
        return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"write_file({t})" for t in targets], payload=payload)

    targets: List[Any] = []
    for key in ("file_path", "notebook_path") + PATH_ARG_KEYS:
        value = _get(args, key, key[0].lower() + key[1:], default=None)
        if isinstance(value, str) and value:
            targets.append(value)
    for key in PATH_LIST_ARG_KEYS:
        value = _get(args, key, key[0].lower() + key[1:], default=None)
        if isinstance(value, list):
            targets.extend(v for v in value if isinstance(v, str) and v)
    if not targets:
        return _no_opinion(payload)
    outside = [t for t in targets if not _within_roots(t, roots)]
    if outside:
        return _ask(f"Mitosis Orchestrator: {outside[0]!r} is outside the workspace.", payload=payload)
    # agy 1.5.0 print mode ignores these grants: write_file in a home folder is denied with the exact
    # rule sent here, under decision allow and under ask, while the same rule in --settings works. Sent
    # for interactive runs, where they were not measured. Headless needs --mode accept-edits or a rule.
    action = "write_file" if (WRITE_TOOL_RE.search(tool_name) or tool_name in MUTATION_TOOLS) else "read_file"
    return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"{action}({t})" for t in targets], payload=payload)


# Every tool name handle_pre_tool_use dispatches on, plus the Codex collaboration tools that are
# known and intentionally unhandled (wait_agent ...). unhandled_spawn_tool() reads only this set.
KNOWN_SPAWN_TOOLS = frozenset(CODEX_COLLAB_TOOLS) | frozenset({
    "invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout",
    "mitosis_worker", "send_message", "SendMessage", "followup_task", "run_workflow",
    "define_subagent", "manage_subagents", "SubagentHandback",
})
SPAWN_LIKE_RE = re.compile(r"spawn|subagent|invoke|collaboration|delegate|(?:^|[^a-z])agent(?:$|[^a-z])", re.I)


def unhandled_spawn_tool(payload: Dict[str, Any]) -> Optional[str]:
    """Raw tool name when it looks like a spawn/delegation tool that no handler recognises, else None."""
    try:
        name, _ = _extract_tool_call(payload)
        if name.startswith("mcp__mitosis__"):
            name = name[len("mcp__mitosis__"):]
        if not name or name in KNOWN_SPAWN_TOOLS or not SPAWN_LIKE_RE.search(name):
            return None
        if name.startswith("mcp__"):  # another server's tools are not ours to dispatch
            return None
        return name
    except Exception:
        return None


@_releases_state_locks
def handle_pre_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, args = _extract_tool_call(payload)
    if not tool_name:
        return _no_opinion(payload)
    # Codex plugin hooks name MCP tools mcp__<server>__<tool>.
    if tool_name.startswith("mcp__mitosis__"):
        tool_name = tool_name[len("mcp__mitosis__"):]

    # Lock-free read: the dispatch below may call the judge over HTTP (classify_subagent),
    # which must never happen while the state lock is held. Handlers that mutate state call
    # load_state (locking) themselves after classification.
    state = _peek_state(payload)

    # Circuit breaker, enforce mode only: block retry loops on commands and subagents.
    # Advise mode tells the primary at the next model call on agy instead (_breaker_advise_agy).
    if _breaker_mode(payload) == "enforce":
        fails = int(state.get("consecutive_failures") or 0)
        if fails >= BREAKER_THRESHOLD and tool_name in BREAKER_TOOLS:
            heartbeat.note(breaker="denied")
            return _deny(
                f"[Mitosis Orchestrator] Circuit breaker tripped after {fails} consecutive failure(s). "
                "Stop retrying and report the blocker to the user.",
                payload,
            )

    if tool_name == "invoke_subagent":
        return _handle_invoke_subagent(payload, args)
    if tool_name in ("Task", "Agent"):
        return _handle_claude_task_or_agent(payload, tool_name, args)
    if tool_name == "spawn_agent":
        return _handle_spawn_agent(payload, tool_name, args)
    if tool_name in ("mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
        return _handle_mitosis_tools(payload, tool_name, args)
    if tool_name in ("send_message", "SendMessage", "followup_task"):
        return _handle_agent_message(payload, tool_name, args)
    if tool_name == "run_workflow":
        return _handle_run_workflow(args)
    if tool_name == "define_subagent":
        return _handle_define_subagent(args)
    if tool_name == "manage_subagents":
        return _handle_manage_subagents(payload, args)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
        if re.search(r"\bcodex\s+exec\b", cmd) and _parse_codex_exec(cmd) is not None:
            return _handle_codex_exec_command(payload, tool_name, args, cmd)

    # A scout (Claude flash-lite-scout, Codex explorer) is read-only whatever its prompt says:
    # Bash for lookups only, no file edits. Codex hides follow-up text from hooks, so this is
    # the layer that holds when a change request reaches a scout anyway.
    agent_type = str(payload.get("agent_type") or "").split(":")[-1].strip().lower()
    if agent_type in (SCOUT_TYPE, CODEX_SCOUT_ROLE) and (is_claude_code_payload(payload) or is_codex_hook_payload(payload)):
        if tool_name in MUTATION_TOOLS:
            return _deny(
                f"[Mitosis Orchestrator] {agent_type} is a read-only scout and cannot use {tool_name}. "
                "Report the finding and let the orchestrator hand changes to a worker.",
                payload,
            )
        if tool_name in ("Bash", "shell", "exec_command", "local_shell"):
            cmd = str(_get(args, "command", "cmd", default="") or "")
            # Codex on Windows may run PowerShell or cmd. A Claude scout's only shell is Bash or zsh. Codex's
            # exec_command lets the model pick the shell, so a POSIX host with pwsh gets the strict rules too.
            codex = is_codex_hook_payload(payload)
            on_windows = codex and _windows_mode(allowed_roots(payload))
            via_pwsh = codex and not on_windows and _codex_nonposix_shell_reachable()
            ok, why = scout_command_is_read_only(cmd, strict_windows=bool(on_windows or via_pwsh))
            if not ok and via_pwsh:
                why += (". This host has PowerShell, which Codex may pick for exec_command and which parses quotes and "
                        "backslashes differently, so quotes, backslashes, redirects, globs and ; & are refused: "
                        "use plain words and | only")
            if not ok:
                return _deny(
                    f"[Mitosis Orchestrator] {agent_type} runs read-only commands only ({why}). "
                    "Report the finding and let the orchestrator hand changes to a worker.",
                    payload,
                )

    advice: Optional[str] = None
    # Primary agent guards (context hygiene and fast-path budget)
    if is_primary_agent(_turn_cwd_payload(payload, state), state):
        # Context hygiene: block direct reads of raw logs or transcripts by primary agent
        if not _env_flag("CODEX_MITOSIS_ALLOW_DIRECT_LOGS", "CLAUDE_MITOSIS_ALLOW_DIRECT_LOGS", "AGY_MITOSIS_ALLOW_DIRECT_LOGS", "AGY_ARGON_ALLOW_DIRECT_LOGS") and tool_name in READ_TOOLS:
            target = str(
                args.get("file_path")
                or args.get("notebook_path")
                or args.get("AbsolutePath")
                or args.get("TargetFile")
                or args.get("FilePath")
                or args.get("Path")
                or args.get("File")
                or ""
            )
            if (
                target
                and LOG_OR_TRANSCRIPT_RE.search(target.replace("\\", "/"))
                and not _is_small_regular_file(target, str(payload.get("cwd") or ""))
            ):
                return _deny(
                    f"[Mitosis Orchestrator] Direct inspection of log/transcript file '{Path(target).name}' "
                    "by the primary agent is blocked for context hygiene. "
                    f"Delegate log slicing to {_delegation_hint(payload, 'flash_lite')}.",
                    payload,
                )

        # Fast-path budget: direct workspace file mutations and test-runner loops
        mode = _fastpath_mode()
        if mode != "off":
            enforce = mode == "enforce"
            if tool_name in MUTATION_TOOLS or tool_name == "apply_patch":
                if tool_name == "apply_patch":
                    patch_targets = _extract_apply_patch_files(args)
                    norm_targets = []
                    for t in patch_targets:
                        try:
                            norm_targets.append(str(Path(os.path.expanduser(t)).resolve()))
                        except Exception:
                            norm_targets.append(t)
                    state = load_state(payload)  # locked read-modify-write of the edit budget
                    edited: List[str] = state.setdefault("turn_edited_files", [])
                    max_edits = _max_direct_edits()
                    over = False
                    new_targets = [nt for nt in norm_targets if nt not in edited]
                    if len(edited) + len(new_targets) > max_edits:
                        if enforce:
                            return _deny(
                                f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                                f"when editing via apply_patch. Delegate multi-file modifications to "
                                f"{_delegation_hint(payload, 'flash')}.",
                                payload,
                            )
                        over = True
                        advice = _fastpath_advice(payload, state, "edit_limit",
                                                  f"more than {max_edits} files were edited directly this turn")
                    for nt in new_targets:
                        edited.append(nt)
                    if new_targets or over:
                        save_state(payload, state)
                else:
                    target = str(
                        args.get("file_path")
                        or args.get("notebook_path")
                        or args.get("TargetFile")
                        or args.get("AbsolutePath")
                        or args.get("FilePath")
                        or args.get("Path")  # agy 1.5.0 write_file and edit_file
                        or ""
                    )
                    artifact_dir = payload.get("artifactDirectoryPath")
                    is_artifact = False
                    if artifact_dir and target:
                        try:
                            is_artifact = _within_roots(target, [Path(artifact_dir)])
                        except Exception:
                            is_artifact = False
                    if target and not is_artifact:
                        try:
                            norm_target = str(Path(os.path.expanduser(target)).resolve())
                        except Exception:
                            norm_target = target
                        state = load_state(payload)  # locked read-modify-write of the edit budget
                        edited: List[str] = state.setdefault("turn_edited_files", [])
                        max_edits = _max_direct_edits()
                        over = False
                        if norm_target not in edited and len(edited) >= max_edits:
                            if enforce:
                                return _deny(
                                    f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                                    f"when editing '{Path(target).name}'. Delegate multi-file modifications to "
                                    f"{_delegation_hint(payload, 'flash')}.",
                                    payload,
                                )
                            over = True
                            advice = _fastpath_advice(payload, state, "edit_limit",
                                                      f"more than {max_edits} files were edited directly this turn")
                        if norm_target not in edited:
                            edited.append(norm_target)
                            save_state(payload, state)
                        elif over:
                            save_state(payload, state)

            elif tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
                cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
                edited = state.get("turn_edited_files", [])
                if edited and _runs_test_runner(cmd):
                    if enforce:
                        return _deny(
                            "[Mitosis Orchestrator] Direct workspace edit followed by test runner execution detected "
                            f"in the primary agent ('{cmd}'). Delegate code editing and test execution loops "
                            f"to {_delegation_hint(payload, 'flash')}.",
                            payload,
                        )
                    state = load_state(payload)
                    advice = _fastpath_advice(payload, state, "edit_test",
                                              "a test runner was started after direct edits in this context")
                    save_state(payload, state)

    verdict = _handle_workspace_guard(payload, tool_name, args)
    if advice and (is_claude_code_payload(payload) or is_codex_hook_payload(payload)):
        verdict = _attach_advice(verdict, advice)
    return verdict


def _fastpath_mode() -> str:
    if _env_flag("CODEX_MITOSIS_ALLOW_DIRECT_FASTPATH", "CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_ARGON_ALLOW_DIRECT_FASTPATH"):
        return "off"
    mode = _get_env("CODEX_MITOSIS_FASTPATH_MODE", "CLAUDE_MITOSIS_FASTPATH_MODE", "AGY_MITOSIS_FASTPATH_MODE", default="advise").lower()
    return mode if mode in ("enforce", "advise", "off") else "advise"


BREAKER_THRESHOLD = 2
BREAKER_TOOLS = (
    "run_command", "invoke_subagent", "Bash", "Task", "Agent",
    "shell", "exec_command", "local_shell", "spawn_agent",
    "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker",
)
_BREAKER_COMMAND_TOOLS = ("run_command", "Bash", "shell", "exec_command", "local_shell")
_BREAKER_EXIT_RE = re.compile(r"(?:exit status|exited with code)\s+(-?\d+)")
# The agy transcript header is "Command exited with code N."; "exit status" is not part of it.
_BREAKER_TRANSCRIPT_EXIT_RE = re.compile(r"Command exited with code (-?\d+)")
# agy writes these for a call that never ran. The first would otherwise let the breaker's own denial feed itself.
_BREAKER_NEUTRAL_ERRORS = ("permission check failed", "tool call denied by pre-tool hook")
_BREAKER_STEP_CAP = 64
_BREAKER_COMMAND_CAP = 4000
_BREAKER_CD_RE = re.compile(r"^\s*cd\s+[^\s;&|<>$`()'\"\\]+\s*&&\s*")
_BREAKER_SHELL_META_RE = re.compile(r"[;&|<>$`()\\\n\r{}*?~]")


def _breaker_prefix(payload: Optional[Dict[str, Any]]) -> str:
    """Env prefix of the platform this payload came from, "" when it cannot be told."""
    if not isinstance(payload, dict):
        return ""
    if is_codex_payload(payload):
        return "CODEX"
    if is_claude_code_payload(payload):
        return "CLAUDE"
    if _is_agy_payload(payload):
        return "AGY"
    return ""


def _own_env(*names: str) -> str:
    for name in names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return ""


def _breaker_mode(payload: Optional[Dict[str, Any]] = None) -> str:
    """advise (default), enforce or off. *_MITOSIS_CIRCUIT_BREAKER=0 means off and wins over the mode.

    With a payload of a known platform only that platform's variables are read for the mode, and its
    CIRCUIT_BREAKER variable is read first, then the cross-platform order. Otherwise: the old order."""
    prefix = _breaker_prefix(payload)
    legacy = ("CODEX_MITOSIS_CIRCUIT_BREAKER", "CLAUDE_MITOSIS_CIRCUIT_BREAKER", "AGY_MITOSIS_CIRCUIT_BREAKER", "AGY_ARGON_CIRCUIT_BREAKER")
    if prefix:
        own = (f"{prefix}_MITOSIS_CIRCUIT_BREAKER",) + (("AGY_ARGON_CIRCUIT_BREAKER",) if prefix == "AGY" else ())
        switch = _own_env(*own) or _get_env(*legacy, default="1")
        if switch == "0":
            return "off"
        mode = (_own_env(f"{prefix}_MITOSIS_CIRCUIT_BREAKER_MODE") or "advise").lower()
    else:
        if _get_env(*legacy, default="1") == "0":
            return "off"
        mode = _get_env("CODEX_MITOSIS_CIRCUIT_BREAKER_MODE", "CLAUDE_MITOSIS_CIRCUIT_BREAKER_MODE", "AGY_MITOSIS_CIRCUIT_BREAKER_MODE", default="advise").lower()
    return mode if mode in ("enforce", "advise", "off") else "advise"


def _breaker_read_only(command: str, payload: Dict[str, Any]) -> bool:
    """True when the command only asks a question, so exit 1 (or 0) says nothing about a retry loop."""
    if not command or len(command) > _BREAKER_COMMAND_CAP:
        return False
    rest = command
    while True:
        stripped = _BREAKER_CD_RE.sub("", rest, count=1)
        if stripped == rest:
            break
        rest = stripped
    rest = rest.strip()
    if not rest:
        return False
    if scout_command_is_read_only(rest, strict_windows=_windows_mode(allowed_roots(payload)))[0]:
        return True
    if _BREAKER_SHELL_META_RE.search(rest):
        return False
    words = rest.split()
    if words[0] == "test" or (words[0] == "[" and words[-1] == "]"):
        return True
    return (words[0] == "command" and len(words) >= 3 and words[1] == "-v") or (words[0] == "which" and len(words) >= 2)


def _breaker_command_signal(read_only: bool, exit_code: int) -> str:
    """fail, neutral or ok for a finished command.

    A read-only command says nothing about a retry loop: exit 1 is grep finding no match, and exit 0
    must not clear the streak either, or a failing test, a cat and a failing test would never reach 2.
    A read-only command that exits 2 or more did fail (a bad path, a bad pattern).
    """
    if exit_code == 0:
        return "neutral" if read_only else "ok"
    if read_only and exit_code == 1:
        return "neutral"
    return "fail"


def _breaker_signal(tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Classify a finished call from what the payload itself carries."""
    error = payload.get("error")
    if isinstance(error, str) and error.startswith(_BREAKER_NEUTRAL_ERRORS):
        return "neutral"
    command = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "") if tool_name in _BREAKER_COMMAND_TOOLS else ""
    if not _tool_failed(tool_name, payload):
        if tool_name in _BREAKER_COMMAND_TOOLS:
            return _breaker_command_signal(_breaker_read_only(command, payload), 0)
        # agy only reports that a spawn was accepted: it neither counts nor clears.
        return "neutral" if _is_agy_payload(payload) else "ok"
    if tool_name not in _BREAKER_COMMAND_TOOLS:
        return "fail"
    result = payload.get("toolResult") or payload.get("tool_result") or payload.get("result")
    code: Any = None
    if isinstance(result, dict):
        code = next((result.get(k) for k in ("exitCode", "exit_code", "returncode") if result.get(k) is not None), None)
    if not isinstance(code, int):
        match = _BREAKER_EXIT_RE.search(json.dumps(error if error else result, default=str))
        code = int(match.group(1)) if match else None
    return "fail" if code is None or code == 0 else _breaker_command_signal(_breaker_read_only(command, payload), code)


def _breaker_apply(state: Dict[str, Any], signal: str) -> None:
    if signal == "fail":
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    elif signal == "ok":
        state["consecutive_failures"] = 0
    else:
        heartbeat.note(breaker="neutral")


def _breaker_track(tool_name: str, args: Dict[str, Any], payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    """PostToolUse. agy 1.5.0 sends error="" and no result for a command that exits non-zero, and its
    transcript gets the step only after this hook, so keep whether the command was read-only and read
    the exit code at the next PreInvocation (_breaker_scan_agy_transcript)."""
    if _breaker_mode(payload) == "off":
        return
    has_result = any(payload.get(k) for k in ("toolResult", "tool_result", "result"))
    step = payload.get("stepIdx")
    if tool_name == "run_command" and _is_agy_payload(payload) and not payload.get("error") and not has_result \
            and isinstance(step, int) and not isinstance(step, bool):
        command = str(_get(args, "CommandLine", "commandLine", "command", "cmd", default="") or "")
        steps = state.get("breaker_steps")
        if not isinstance(steps, dict):
            steps = state["breaker_steps"] = {}
        key = str(step)
        while key not in steps and len(steps) >= _BREAKER_STEP_CAP:
            steps.pop(next(iter(steps)))  # evict the oldest: the newest command is the one that matters
        steps[key] = _breaker_read_only(command, payload)
        return
    _breaker_apply(state, _breaker_signal(tool_name, args, payload))


def _breaker_scan_agy_transcript(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Turn the commands kept by _breaker_track into failures, in step order, once their results are written."""
    steps = state.get("breaker_steps")
    if not isinstance(steps, dict) or not steps or _breaker_mode(payload) == "off":
        return
    for record in _tail_records(payload.get("transcriptPath")):
        key = str(record.get("step_index"))
        if key not in steps:
            continue
        read_only = steps.pop(key) is True  # an older state held the command text: not read-only
        code = record.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            # The header comes first; command output later in the step may quote the same words.
            match = _BREAKER_TRANSCRIPT_EXIT_RE.search(str(record.get("content") or "")[:300])
            code = int(match.group(1)) if match else None
        if code is None:  # still running in the background, or denied before it ran
            heartbeat.note(breaker="neutral")
            continue
        _breaker_apply(state, _breaker_command_signal(read_only, code))


def _breaker_advise_agy(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Advise mode on agy: one note per user turn, delivered with the next model call."""
    if _breaker_mode(payload) != "advise" or int(state.get("consecutive_failures") or 0) < BREAKER_THRESHOLD:
        return
    text = (
        f"[Mitosis Orchestrator] Advice, not a block: {state.get('consecutive_failures')} commands failed in a row "
        "this turn. Stop and find the cause before retrying, or report the blocker to the user. "
        "Set *_MITOSIS_CIRCUIT_BREAKER_MODE=enforce to turn this into a denial."
    )
    if "circuit_breaker" not in (state.get("fastpath_advised") or []):
        _queue_advice(payload, state, "circuit_breaker", text)
        heartbeat.note(breaker="advised")


def _max_direct_edits() -> int:
    raw = _get_env("CODEX_MITOSIS_MAX_DIRECT_EDITS", "CLAUDE_MITOSIS_MAX_DIRECT_EDITS", "AGY_MITOSIS_MAX_DIRECT_EDITS", "AGY_ARGON_MAX_DIRECT_EDITS", default="3")
    try:
        return int(raw)
    except ValueError:
        return 3


def _is_agy_payload(payload: Dict[str, Any]) -> bool:
    return bool(payload.get("artifactDirectoryPath")) and not is_codex_payload(payload) and not is_claude_code_payload(payload)


def _fastpath_advice(payload: Dict[str, Any], state: Dict[str, Any], kind: str, what: str) -> Optional[str]:
    """Once per kind per turn. Returns text to attach now (Claude/Codex hooks); queues it on agy."""
    return _queue_advice(payload, state, kind, (
        f"[Mitosis Orchestrator] Advice, not a block: {what}. Delegating to "
        f"{_delegation_hint(payload, 'flash')} protects this context but usually costs more in total, "
        "so do it only when this context matters. Set *_MITOSIS_FASTPATH_MODE=enforce to turn this into a denial."
    ))


def _queue_advice(payload: Dict[str, Any], state: Dict[str, Any], kind: str, text: str) -> Optional[str]:
    """Once per kind per turn. Returns the text to attach now on Claude/Codex; queues it on agy and returns None."""
    hook = is_claude_code_payload(payload) or is_codex_hook_payload(payload)
    agy = _is_agy_payload(payload)
    if not (hook or agy):
        return None
    advised = state.get("fastpath_advised")
    if not isinstance(advised, list):
        advised = state["fastpath_advised"] = []
    if kind in advised:
        return None
    advised.append(kind)
    if agy:
        pending = state.get("fastpath_pending_advice")
        if not isinstance(pending, list):
            pending = state["fastpath_pending_advice"] = []
        pending.append(text)
        return None
    return text


def _attach_advice(verdict: Dict[str, Any], advice: str) -> Dict[str, Any]:
    if not verdict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": advice}}
    hso = verdict.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") != "deny":
        hso["additionalContext"] = advice
    return verdict


_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\||\n|(?<![>&])&(?![>&])")
_SHELL_STRING_RE = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|sh|zsh|dash|eval)\s+(?:-\w*c\b|[\"'])")
_RUNS_NESTED_RE = re.compile(r"\$\(|`|\s-exec(?:dir)?\s|\s-ok\s")
_NON_RUNNER_WORDS = {"grep", "rg", "cat", "echo", "git", "sed", "awk", "ls", "find", "head", "tail"}


def _strip_heredocs(cmd: str) -> str:
    lines = cmd.split("\n")
    out: List[str] = []
    delim: Optional[str] = None
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        out.append(line)
        m = _HEREDOC_RE.search(line)
        if m:
            delim = m.group(2)
    return "\n".join(out)


def _runs_test_runner(cmd: str) -> bool:
    """True when a command actually runs a test runner, not when text merely names one."""
    body = _strip_heredocs(cmd)
    # A string handed to a shell is a command, so read it instead of removing it.
    if _SHELL_STRING_RE.search(body) and TEST_RUNNER_RE.search(body):
        return True
    text = _QUOTED_RE.sub(" ", body)
    for seg in _SEGMENT_SPLIT_RE.split(text):
        words = seg.split()
        if not words:
            continue
        if os.path.basename(words[0]) in _NON_RUNNER_WORDS and not _RUNS_NESTED_RE.search(seg):
            continue
        if TEST_RUNNER_RE.search(seg):
            return True
    return False


def _tool_failed(tool_name: str, payload: Dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    tool_result = payload.get("toolResult") or payload.get("tool_result") or payload.get("result")
    if isinstance(tool_result, dict):
        exit_code = tool_result.get("exitCode")
        if exit_code is None:
            exit_code = tool_result.get("exit_code")
        if exit_code is None:
            exit_code = tool_result.get("returncode")
        if exit_code is not None and exit_code != 0:
            return True
        if tool_result.get("error") or tool_result.get("status") == "error":
            return True
        output = str(tool_result.get("output") or "")
        if re.search(r"\bexited with code [1-9]", output):
            return True
        if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
            res_str = json.dumps(tool_result)
            if re.search(r"STATUS:\s*(?:FAILED|BLOCKED)\b", res_str):
                return True
    elif isinstance(tool_result, str):
        if re.search(r"\bexited with code [1-9]", tool_result):
            return True
        if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
            if re.search(r"STATUS:\s*(?:FAILED|BLOCKED)\b", tool_result):
                return True
    return False


HANDBACK_REPORT_CAP = 20000


def _response_failed(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("is_error") or response.get("isError"):
        return True
    for key in ("exitCode", "exit_code", "returncode"):
        code = response.get(key)
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return True
    return False


def _observe_claude_failure(tool_name: str, payload: Dict[str, Any]) -> None:
    """Count a failed Claude tool_response in the heartbeat. OBSERVE ONLY.

    This must not feed _tool_failed, consecutive_failures or the circuit breaker: a live breaker
    on this signal could trip after two failed greps (exit 1 means "no match"), which would
    deny the primary's next Bash/Agent call. It also runs for subagent payloads, which count in
    the heartbeat but never touch the primary's session state.
    """
    try:
        if is_claude_code_payload(payload) and tool_name and _response_failed(payload.get("tool_response")):
            heartbeat.note(failure=tool_name)
    except Exception:
        pass


@_releases_state_locks
def handle_post_tool_use_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    """PostToolUseFailure: OBSERVE ONLY. Returns {} and counts the failure, nothing else.

    It deliberately leaves consecutive_failures, the circuit breaker and pending_spawns alone
    (see _observe_claude_failure for why). A subagent's failure (agent_id set) is counted in the
    heartbeat only; the primary's session state is not touched.
    """
    try:
        tool_name, _ = _extract_tool_call(payload)
        tool_name = tool_name or str(payload.get("tool_name") or "")
        if tool_name:
            heartbeat.note(failure=tool_name)
            if not payload.get("agent_id"):
                state = load_state(payload)
                failures = state.get("tool_failures")
                if not isinstance(failures, dict):
                    failures = state["tool_failures"] = {}
                if tool_name in failures or len(failures) < 16:
                    failures[tool_name] = int(failures.get(tool_name) or 0) + 1
                save_state(payload, state)
    except Exception:
        pass
    return {}


@_releases_state_locks
def handle_post_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, tool_args = _extract_tool_call(payload)
    _observe_claude_failure(tool_name, payload)
    if tool_name == "SubagentHandback":
        # The transcript may not hold this call yet when SubagentStop fires, so keep the report for it.
        agent_id = str(payload.get("agent_id") or "").strip().lower()
        report = tool_args.get("message")
        if agent_id and isinstance(report, str) and report.strip():
            state = load_state(payload)
            state.setdefault("handback_reports", {})[agent_id] = report[:HANDBACK_REPORT_CAP]
            save_state(payload, state)
        return {}
    # A subagent's own tool call (Claude and Codex both send agent_id) must not touch the
    # primary's state: its failures are not the primary's failures, and skipping the
    # load/save avoids needless lock contention.
    if payload.get("agent_id") and tool_name not in ("Agent", "Task", "spawn_agent"):
        return {}
    state = load_state(payload)
    failed = _tool_failed(tool_name, payload)
    if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker") and (payload.get("error") or failed):
        pending = state.get("pending_spawns", [])
        if tool_name in ("invoke_subagent", "mitosis_invoke_subagent"):
            tool_call = payload.get("toolCall") or payload.get("tool_call") or {}
            args = tool_call.get("args") or tool_call.get("input") or {}
            tasks = _get(args, "Subagents", "subagents", "tasks", default=[]) or []
            n = len(tasks) if isinstance(tasks, list) else 1
            if n and len(pending) >= n:
                del pending[-n:]
            else:
                state["pending_spawns"] = []
        elif tool_name == "spawn_agent" and str(tool_args.get("task_name") or "").strip():
            state.setdefault("subagents", {}).pop(_codex_agent_key(tool_args.get("task_name")), None)
        else:
            if pending:
                pending.pop()
    else:
        if tool_name == "spawn_agent":
            _codex_confirm_spawn(state, tool_args, payload.get("tool_response"))
        if is_claude_code_payload(payload) and tool_name in ("Task", "Agent"):
            hint = str((payload.get("tool_input") or {}).get("subagent_type") or "")
            for aid in _claude_agent_ids_from_response(payload):
                _claude_register_agent(state, aid, hint, from_spawn=True)
        sync_subagents(payload, state)

    escalation_note = ""
    if tool_name in ("Task", "Agent") and is_claude_code_payload(payload) and not (payload.get("error") or failed):
        escalation_note = _claude_escalate(state, payload)

    # Track consecutive failures for the circuit breaker
    if tool_name in BREAKER_TOOLS:
        _breaker_track(tool_name, tool_args, payload, state)

    save_state(payload, state)
    if escalation_note:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": escalation_note}}
    return {}


def _claude_escalate(state: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Start a failure escalation when the finished Agent call was a mitosis spawn whose report says
    STATUS: FAILED/BLOCKED. Returns the additionalContext for the primary ("" when nothing starts)."""
    if not escalation_enabled():
        return ""
    status = ""
    for text in _iter_strings(payload.get("tool_response")):
        status = report_failure_status(text) or ""
        if status:
            break
    if not status:
        return ""
    tool_input = payload.get("tool_input") or {}
    hint = str(tool_input.get("subagent_type") or tool_input.get("subagentType") or "")
    record: Optional[Dict[str, Any]] = None
    for aid in _claude_agent_ids_from_response(payload):
        rec = state.get("subagents", {}).get(aid)
        if rec is not None and (not str(rec.get("type") or "").strip() or _is_plugin_agent_type(str(rec["type"]))):
            record = rec
            break
    if record is None and hint and _is_plugin_agent_type(hint):
        # No paired agent record: judge by the spawn's subagent_type.
        bare = hint.split(":")[-1].strip().lower()
        record = {"tier": "flash_lite" if bare == SCOUT_TYPE else "flash", "type": hint}
    route = escalation_for_record(record)
    if route is None:
        return ""
    source = str((record or {}).get("agent_id") or hint or "subagent")
    if record is not None:
        if record.get("escalation_sent"):
            return ""
        record["escalation_sent"] = True
    start_escalation(state, route, source)
    if route["to"] == "flash":
        return (
            f"[Mitosis Orchestrator] The flash-lite scout ({source}) reported STATUS: {status}. The next dispatch of "
            f"that task will run on {_delegation_hint(payload, 'flash')}. Re-dispatch the task with the Agent tool "
            "instead of doing it yourself."
        )
    alias = primary_model_alias(state, payload)
    return (
        f"[Mitosis Orchestrator] The flash-worker ({source}) reported STATUS: {status}. The next worker dispatch of "
        f"that task will run on the primary model ({alias}). Re-dispatch it with the Agent tool instead of doing it yourself."
    )


@_releases_state_locks
def handle_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload) or is_codex_payload(payload):
        if payload.get("stop_hook_active") is True:
            return {}
        state = load_state(payload)
        sync_subagents(payload, state)
        warnings = state.pop("pin_warnings", None)
        notice: Dict[str, Any] = {"systemMessage": " ".join(warnings)} if warnings else {}
        active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
        active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]
        if not active:
            save_state(payload, state)
            return notice

        key = ",".join(sorted(active))
        reminded: List[str] = state.setdefault("stop_reminded_sets", [])
        if key in reminded:
            save_state(payload, state)
            return notice
        reminded.append(key)
        save_state(payload, state)
        return {
            "decision": "block",
            "reason": (
                f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
                "Merge or discard their changes, then clean them up before finishing."
            ),
            **notice,
        }

    # Antigravity handling
    if str(payload.get("terminationReason") or "") in SAFE_TERMINATION_REASONS:
        return {"decision": "allow"}
    if not payload.get("fullyIdle", True):
        return {"decision": "allow"}

    state = load_state(payload)
    sync_subagents(payload, state)
    active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
    active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]
    if not active:
        save_state(payload, state)
        return {"decision": "allow"}

    # agy increments executionNum after every "continue", so keying on it would
    # nag on every attempt. Remind once per distinct set of live branch workers:
    # a new branch worker earns a new reminder, ignoring the same one does not.
    key = ",".join(sorted(active))
    reminded: List[str] = state.setdefault("stop_reminded_sets", [])
    if key in reminded:
        save_state(payload, state)
        return {"decision": "allow"}
    reminded.append(key)
    if payload.get("lastUserInput") is not None:
        state["breaker_keep_for_input"] = payload.get("lastUserInput")
    save_state(payload, state)
    return {
        "decision": "continue",
        "reason": (
            f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
            "Merge or discard their changes, then call manage_subagents(Action='kill') before finishing."
        ),
    }


def _observe_stop_record(record: Any) -> None:
    """Count one stop's pin verdict and report outcome from the record the handler just updated."""
    try:
        if not isinstance(record, dict):
            return
        if "pin_verified" in record or record.get("pinned_model"):
            verdict = record.get("pin_verified")
            heartbeat.note(pin="unknown" if verdict is None else ("verified" if verdict else "mismatch"))
        if "report_ok" in record:
            ok = bool(record.get("report_ok"))
            problem = None if ok else record.get("report_problem")
            scout = _record_tier(record) == "flash_lite"
            heartbeat.note(report=ok, report_problem=problem, report_scout=scout)
    except Exception:
        pass


@_releases_state_locks
def handle_subagent_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload):
        result = _handle_claude_subagent_stop(payload)
        if heartbeat.enabled():
            agent_id = str(payload.get("agent_id") or "").strip().lower()
            records = _peek_state(payload).get("subagents")
            _observe_stop_record(records.get(agent_id) if isinstance(records, dict) and agent_id else None)
        return result
    state = load_state(payload)
    sync_subagents(payload, state)
    if is_codex_payload(payload):
        bound = codex_subagent_stop(payload, state)
        _observe_stop_record(state.get("subagents", {}).get(bound) if bound else None)
        _RECENT_BINDINGS.append({"agentId": payload.get("agent_id"), "boundTo": bound})
        del _RECENT_BINDINGS[:-16]
        result = codex_report_block(state, bound, payload)
        if not result:
            codex_escalate(state, bound, payload)
        save_state(payload, state)
        return result
    subagents = state.setdefault("subagents", {})
    pending = state.setdefault("pending_spawns", [])

    subagent_id = str(payload.get("subagent_id") or "").strip().lower()
    cleared = False
    if subagent_id and subagent_id in subagents:
        subagents[subagent_id]["active_branch"] = False
        cleared = True
    for meta in pending:
        if subagent_id and (meta.get("role", "").lower() == subagent_id or meta.get("type", "").lower() == subagent_id):
            meta["active_branch"] = False
            cleared = True
    if not cleared:
        for meta in pending:
            if meta.get("active_branch"):
                meta["active_branch"] = False
        for meta in subagents.values():
            if meta.get("active_branch"):
                meta["active_branch"] = False
    save_state(payload, state)
    return {}


_RECENT_BINDINGS: List[Dict[str, Any]] = []


def _debug_log(payload: Dict[str, Any], mode: str, result: Dict[str, Any]) -> None:
    """With AGY_MITOSIS_DEBUG=1 / CLAUDE_MITOSIS_DEBUG=1 / CODEX_MITOSIS_DEBUG=1 (or legacy AGY_ARGON_DEBUG=1), append one JSON line per hook call to the artifact directory."""
    if not _env_flag("CLAUDE_MITOSIS_DEBUG", "AGY_MITOSIS_DEBUG", "AGY_ARGON_DEBUG"):
        return
    path = _state_file(payload)
    if not path:
        return
    tool_name, args = _extract_tool_call(payload)
    raw_tool = str(payload.get("tool_name") or "")
    state = _read_state_file(path)
    entry = {
        "mode": mode,
        "event": payload.get("hook_event_name"),
        "tool": tool_name,
        "rawTool": raw_tool if raw_tool and raw_tool != tool_name else None,
        "agentId": payload.get("agent_id"),
        "agentType": payload.get("agent_type"),
        "toolInputKeys": sorted(args.keys()) if args else None,
        "toolResponse": str(payload.get("tool_response"))[:500] if payload.get("tool_response") is not None else None,
        "state": {
            "pending": len(state.get("pending_spawns") or []),
            "subagents": sorted((state.get("subagents") or {}).keys())[:16],
            "consecutiveFailures": state.get("consecutive_failures"),
        },
        "modelName": payload.get("modelName") or payload.get("model_name") or payload.get("model"),
        "invocationNum": payload.get("invocationNum"),
        "executionNum": payload.get("executionNum"),
        "fullyIdle": payload.get("fullyIdle"),
        "terminationReason": payload.get("terminationReason"),
        "payloadKeys": sorted(payload.keys()),
        "result": result,
    }
    if _RECENT_BINDINGS:
        entry["codexBinding"] = list(_RECENT_BINDINGS)
        _RECENT_BINDINGS.clear()
    if _RECENT_DECISIONS:
        entry["decisions"] = [d.as_log() for d in _RECENT_DECISIONS]
        _RECENT_DECISIONS.clear()
    try:
        # One O_APPEND write per entry so concurrent hooks never interleave lines.
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(
            str(path.with_name("mitosis_orchestrator_debug.log")),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass
