"""Hook mode handlers."""

import json
import os
import re
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
from .guards import (
    PATH_ARG_KEYS,
    PATH_LIST_ARG_KEYS,
    WRITE_TOOL_RE,
    _extract_apply_patch_files,
    _parse_codex_exec,
    _within_roots,
    allowed_roots,
    command_stays_in_workspace,
    scout_command_is_read_only,
)
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
    settings_path,
)
from .platforms.agy import (
    _handle_define_subagent,
    _handle_invoke_subagent,
    _handle_manage_subagents,
    _handle_run_workflow,
)
from .platforms.claude import (
    _claude_agent_ids_from_response,
    _claude_register_agent,
    _handle_agent_message,
    _handle_claude_subagent_stop,
    _handle_claude_task_or_agent,
)
from .platforms.codex import (
    _codex_agent_key,
    _codex_confirm_spawn,
    _extract_tool_call,
    _handle_codex_exec_command,
    _handle_mitosis_tools,
    _handle_spawn_agent,
)
from .routing import TEST_RUNNER_RE
from .state import (
    _RECENT_DECISIONS,
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
            "Ask the user to switch to a supported frontier model (Gemini Argon/Pro/Ultra, Claude Opus/Sonnet, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before orchestrating, "
            "or set CODEX_MITOSIS_ALLOW_NON_PRIMARY=1 / AGY_MITOSIS_ALLOW_NON_PRIMARY=1 / CLAUDE_MITOSIS_ALLOW_NON_PRIMARY=1 to silence this."
        )
    elif status == "silenced":
        note = f"Primary model is '{model}' (non-frontier warnings silenced)."
    else:
        note = f"{target_file} not found; the live model is checked on every turn instead."

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


@_releases_state_locks
def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    # A subagent's own prompt (Codex sends UserPromptSubmit with agent_id) is
    # neither a new primary turn nor a primary model to check.
    if payload.get("agent_id"):
        return {}
    state = load_state(payload)
    state["turn_edited_files"] = []
    state["consecutive_failures"] = 0
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

    if not model_name or is_frontier_tier(model_name) or _env_flag(
        "CODEX_MITOSIS_ALLOW_NON_PRIMARY",
        "CLAUDE_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_ALLOW_NON_MITOSIS_PRIMARY",
        "AGY_ALLOW_NON_ARGON_PRIMARY",
        "AGY_ARGON_ALLOW_NON_PRIMARY",
    ):
        return {}
    cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or "")
    inv_num = payload.get("invocationNum", 0)
    key = f"{cid}:{inv_num}"
    warned: List[str] = state.setdefault("warned_invocations", [])
    if key in warned:
        return {}
    warned.append(key)
    save_state(payload, state)

    warn_text = (
        f"[Mitosis Orchestrator] The active session model is '{model_name}', which is "
        "not a frontier model. Tell the user to switch Model Selection to "
        "a supported frontier model (Claude Opus/Sonnet, Gemini Argon/Pro/Ultra, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before continuing orchestration work."
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
    return {
        "injectSteps": [
            {
                "ephemeralMessage": warn_text
            }
        ]
    }


def _handle_workspace_guard(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Opt-in: the plugin ships with the scope guard off. Set AGY_MITOSIS_AUTO_ALLOW=1 (or CLAUDE_MITOSIS_AUTO_ALLOW=1 / CODEX_MITOSIS_AUTO_ALLOW=1)
    # to let project-scoped file and command tools skip the permission prompt.
    # When unset or for tools without path arguments, pass through without a verdict.
    if not _env_flag("CODEX_MITOSIS_AUTO_ALLOW", "CLAUDE_MITOSIS_AUTO_ALLOW", "AGY_MITOSIS_AUTO_ALLOW", "AGY_ARGON_AUTO_ALLOW"):
        return _no_opinion(payload)
    roots = allowed_roots(payload)
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
    # "allow" alone does not satisfy Antigravity's permission system (headless
    # runs still auto-deny), so grant the matching read_file/write_file rule
    # for exactly these in-workspace paths.
    action = "write_file" if (WRITE_TOOL_RE.search(tool_name) or tool_name in MUTATION_TOOLS) else "read_file"
    return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"{action}({t})" for t in targets], payload=payload)


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

    # Circuit breaker: if consecutive failures >= 2 and breaker is not disabled ("0"),
    # block retry loops on commands and subagents.
    if _get_env("CODEX_MITOSIS_CIRCUIT_BREAKER", "CLAUDE_MITOSIS_CIRCUIT_BREAKER", "AGY_MITOSIS_CIRCUIT_BREAKER", "AGY_ARGON_CIRCUIT_BREAKER", default="1") != "0":
        fails = int(state.get("consecutive_failures") or 0)
        if fails >= 2 and tool_name in (
            "run_command", "invoke_subagent", "Bash", "Task", "Agent",
            "shell", "exec_command", "local_shell", "spawn_agent",
            "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
        ):
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
            ok, why = scout_command_is_read_only(cmd)
            if not ok:
                return _deny(
                    f"[Mitosis Orchestrator] {agent_type} runs read-only commands only ({why}). "
                    "Report the finding and let the orchestrator hand changes to a worker.",
                    payload,
                )

    # Primary agent guards (context hygiene and fast-path budget)
    if is_primary_agent(payload, state):
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
            if target and LOG_OR_TRANSCRIPT_RE.search(target.replace("\\", "/")):
                return _deny(
                    f"[Mitosis Orchestrator] Direct inspection of log/transcript file '{Path(target).name}' "
                    "by the primary agent is blocked for context hygiene. "
                    f"Delegate log slicing to {_delegation_hint(payload, 'flash_lite')}.",
                    payload,
                )

        # Fast-path budget: direct workspace file mutations and test-runner loops
        if not _env_flag("CODEX_MITOSIS_ALLOW_DIRECT_FASTPATH", "CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_ARGON_ALLOW_DIRECT_FASTPATH"):
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
                    max_edits_str = _get_env("CODEX_MITOSIS_MAX_DIRECT_EDITS", "CLAUDE_MITOSIS_MAX_DIRECT_EDITS", "AGY_MITOSIS_MAX_DIRECT_EDITS", "AGY_ARGON_MAX_DIRECT_EDITS", default="3")
                    try:
                        max_edits = int(max_edits_str)
                    except ValueError:
                        max_edits = 3
                    new_targets = [nt for nt in norm_targets if nt not in edited]
                    if len(edited) + len(new_targets) > max_edits:
                        return _deny(
                            f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                            f"when editing via apply_patch. Delegate multi-file modifications to "
                            f"{_delegation_hint(payload, 'flash')}.",
                            payload,
                        )
                    for nt in new_targets:
                        edited.append(nt)
                    if new_targets:
                        save_state(payload, state)
                else:
                    target = str(
                        args.get("file_path")
                        or args.get("notebook_path")
                        or args.get("TargetFile")
                        or args.get("AbsolutePath")
                        or args.get("FilePath")
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
                        max_edits_str = _get_env("CODEX_MITOSIS_MAX_DIRECT_EDITS", "CLAUDE_MITOSIS_MAX_DIRECT_EDITS", "AGY_MITOSIS_MAX_DIRECT_EDITS", "AGY_ARGON_MAX_DIRECT_EDITS", default="3")
                        try:
                            max_edits = int(max_edits_str)
                        except ValueError:
                            max_edits = 3
                        if norm_target not in edited and len(edited) >= max_edits:
                            return _deny(
                                f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                                f"when editing '{Path(target).name}'. Delegate multi-file modifications to "
                                f"{_delegation_hint(payload, 'flash')}.",
                                payload,
                            )
                        if norm_target not in edited:
                            edited.append(norm_target)
                            save_state(payload, state)

            elif tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
                cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
                edited = state.get("turn_edited_files", [])
                if edited and TEST_RUNNER_RE.search(cmd):
                    return _deny(
                        "[Mitosis Orchestrator] Direct workspace edit followed by test runner execution detected "
                        f"in the primary agent ('{cmd}'). Delegate code editing and test execution loops "
                        f"to {_delegation_hint(payload, 'flash')}.",
                        payload,
                    )

    return _handle_workspace_guard(payload, tool_name, args)


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


@_releases_state_locks
def handle_post_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, tool_args = _extract_tool_call(payload)
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
                _claude_register_agent(state, aid, hint)
        sync_subagents(payload, state)

    # Track consecutive failures for the circuit breaker
    if tool_name in (
        "run_command", "invoke_subagent", "Bash", "Task", "Agent",
        "shell", "exec_command", "local_shell", "spawn_agent",
        "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
    ):
        if failed:
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        else:
            state["consecutive_failures"] = 0

    save_state(payload, state)
    return {}


@_releases_state_locks
def handle_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload) or is_codex_payload(payload):
        if payload.get("stop_hook_active") is True:
            return {}
        state = load_state(payload)
        sync_subagents(payload, state)
        active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
        active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]
        if not active:
            save_state(payload, state)
            return {}

        key = ",".join(sorted(active))
        reminded: List[str] = state.setdefault("stop_reminded_sets", [])
        if key in reminded:
            save_state(payload, state)
            return {}
        reminded.append(key)
        save_state(payload, state)
        return {
            "decision": "block",
            "reason": (
                f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
                "Merge or discard their changes, then clean them up before finishing."
            ),
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
    save_state(payload, state)
    return {
        "decision": "continue",
        "reason": (
            f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
            "Merge or discard their changes, then call manage_subagents(Action='kill') before finishing."
        ),
    }


@_releases_state_locks
def handle_subagent_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload):
        return _handle_claude_subagent_stop(payload)
    state = load_state(payload)
    sync_subagents(payload, state)
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
        "toolInputKeys": sorted(args.keys()) if args else None,
        "toolResponse": str(payload.get("tool_response"))[:500] if payload.get("tool_response") is not None else None,
        "state": {
            "pending": len(state.get("pending_spawns") or []),
            "subagents": sorted((state.get("subagents") or {}).keys())[:16],
        },
        "modelName": payload.get("modelName") or payload.get("model_name") or payload.get("model"),
        "invocationNum": payload.get("invocationNum"),
        "executionNum": payload.get("executionNum"),
        "fullyIdle": payload.get("fullyIdle"),
        "terminationReason": payload.get("terminationReason"),
        "payloadKeys": sorted(payload.keys()),
        "result": result,
    }
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
