#!/usr/bin/env python3
"""Mitosis Orchestrator lifecycle hooks for Antigravity (agy).

Zero-dependency (Python 3.8+ standard library only).

Modes (first CLI argument):

session-start
    Reads ~/.gemini/antigravity-cli/settings.json and injects the orchestration
    policy notice. By default it only WARNS when the configured primary model is
    not a frontier model. Set AGY_MITOSIS_PIN_SETTINGS=1 (or legacy AGY_ARGON_PIN_SETTINGS=1)
    to opt in to the behaviour that rewrites settings.json back to a preferred model.

pre-invocation
    Checks the live session model (payload.modelName) on every turn. If it is not
    a frontier model, injects a one-time ephemeral warning per invocation so the
    primary agent asks the user to switch models. Resets per-turn tracking
    (turn_edited_files, consecutive_failures).

pre-tool-use
    invoke_subagent   -> context-classifies every subagent into flash / flash_lite
                         (strong signals always win over lite keywords; mutation
                         prompts never stay on flash_lite) and records pending
                         spawns for lifecycle tracking.
    send_message      -> denies mutation or multi-step (>3 numbered rules) tasks
                         sent to a known flash_lite recipient (Claude Code: `SendMessage`,
                         recipients paired by agent id at post-tool-use / subagent-stop).
    run_workflow      -> denies scripts whose agent(...) calls omit
                         model="flash" / model="flash_lite".
    define_subagent   -> forces enable_subagent_tools=False (no nested spawning).
    manage_subagents  -> marks killed branch workers as cleaned up.
    circuit breaker   -> denies run_command and invoke_subagent when consecutive
                         failures reach >= 2.
    context hygiene   -> blocks direct inspection of .log and transcript.jsonl
                         files by the primary agent (delegates to flash-lite-scout).
    fast-path budget  -> tracks direct workspace edits (max 3 by default) and blocks
                         direct workspace edit + test-runner loops in the primary agent.
    workspace guard   -> "allow" when cwd, paths and command text stay inside
                         workspacePaths or artifactDirectoryPath; "ask" for
                         paths outside, privilege escalation, destructive or
                         system-altering commands, force pushes, or downloads
                         piped to a shell; fallback returns explicit "allow"
                         pass-through so tools are never denied by hook output.

post-tool-use
    Rolls back pending spawns when invoke_subagent fails; otherwise pairs pending
    spawns with the conversationIds that appear in the transcript. Tracks
    consecutive failures across run_command and invoke_subagent for the circuit breaker.

stop
    When the turn is fully idle and Workspace:"branch" workers are still active,
    asks the primary agent (once per set of live branch workers) to merge/discard and kill them
    before finishing. Never blocks user cancellations, errors, or budget caps.

Environment variables:
    AGY_MITOSIS_PIN_SETTINGS=1          opt in to rewriting settings.json at session start
    AGY_MITOSIS_PRIMARY_MODEL=<name>    target model when pinning (default: Gemini 4 Argon (High))
    AGY_MITOSIS_ALLOW_NON_PRIMARY=1     silence all primary-model warnings
    AGY_ALLOW_NON_MITOSIS_PRIMARY=1     alias to silence all primary-model warnings
    AGY_ANTIGRAVITY_SETTINGS=<path>     override the settings.json location (tests)
    AGY_MITOSIS_AUTO_ALLOW=1            enable the workspace scope guard (off by default)
    AGY_MITOSIS_MAX_DIRECT_EDITS=<int>  max distinct workspace file edits per turn (default: 3)
    AGY_MITOSIS_ALLOW_DIRECT_LOGS=1     allow primary agent to read raw logs and transcripts
    AGY_MITOSIS_ALLOW_DIRECT_FASTPATH=1 allow primary agent direct edit-test loops and unlimited edits
    AGY_MITOSIS_CIRCUIT_BREAKER=0       disable the 2-failure circuit breaker
    CLAUDE_MITOSIS_PIN_SUBAGENTS=0      (Claude Code) stop rewriting Agent calls entirely
    CLAUDE_MITOSIS_SCOUT_MODEL=<m>      (Claude Code) scout model (default: haiku; haiku|sonnet|opus|fable)
    CLAUDE_MITOSIS_WORKER_MODEL=<m>     (Claude Code) worker model (default: sonnet)
    CLAUDE_MITOSIS_PIN_EFFORT=0         (Claude Code) stop adding `effort` to pinned Agent calls
    CLAUDE_MITOSIS_SCOUT_EFFORT=<e>     (Claude Code) scout effort (default: low; low|medium|high|xhigh|max)
    CLAUDE_MITOSIS_WORKER_EFFORT=<e>    (Claude Code) worker effort (default: medium)
    CLAUDE_MITOSIS_ENFORCE_REPORT=0     (Claude Code) do not block a plugin subagent's stop when its
                                        final message is not the four-header report
    CODEX_MITOSIS_ENFORCE_REPORT=0      (Codex) do not send a mitosis subagent back once when its
                                        final message is not the four-header report
    AGY_MITOSIS_WORKER_DOWNGRADE=0      keep every flash-worker typed call on the worker (default: a short
                                        plain read-only lookup typed flash-worker goes to the scout)
    AGY_MITOSIS_APPEND_REPORT_FORMAT=0  do not append the REPORT FORMAT block to subagent prompts
                                        that lack one (agy invoke_subagent, Claude Code Agent)
    AGY_MITOSIS_DEBUG=1                 append one JSON line per hook call to
                                        <artifactDirectoryPath>/mitosis_orchestrator_debug.log
    MITOSIS_OTEL=1                      emit one OTel GenAI invoke_agent span per routing decision to
                                        MITOSIS_OTEL_FILE (JSONL) and/or an OTLP/HTTP endpoint
                                        (OTEL_EXPORTER_OTLP_[TRACES_]ENDPOINT); see scripts/mitosis_otel.py
    (All variables support legacy AGY_ARGON_* and AGY_ALLOW_NON_ARGON_PRIMARY fallbacks)
"""

import json
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mitosis_hook import common, guards, handlers, models, report, routing, state  # noqa: F401,E402
from mitosis_hook.platforms import agy, claude, codex  # noqa: F401,E402
from mitosis_hook.common import (  # noqa: F401,E402
    ALLOWED_PRIMARY_MODELS,
    ALLOWED_SUBAGENT_MODELS,
    CLAUDE_BUILTIN_AGENT_TYPES,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_HOOK_EVENTS,
    CLAUDE_PLUGIN_PREFIX,
    CLAUDE_SUBAGENT_MODELS,
    CODEX_SCOUT_ROLE,
    CODEX_WORKER_ROLE,
    DEFAULT_CLAUDE_PRIMARY_MODEL,
    DEFAULT_CLAUDE_SCOUT_EFFORT,
    DEFAULT_CLAUDE_SCOUT_MODEL,
    DEFAULT_CLAUDE_WORKER_EFFORT,
    DEFAULT_CLAUDE_WORKER_MODEL,
    DEFAULT_CODEX_PRIMARY_MODEL,
    DEFAULT_CODEX_SCOUT_EFFORT,
    DEFAULT_CODEX_SCOUT_MODEL,
    DEFAULT_CODEX_WORKER_EFFORT,
    DEFAULT_CODEX_WORKER_MODEL,
    DEFAULT_PRIMARY_MODEL,
    Decision,
    MUTATION_TOOLS,
    READ_TOOLS,
    SAFE_TERMINATION_REASONS,
    SCOUT_TYPE,
    STATE_FILENAME,
    WORKER_TYPE,
    _GEN,
    _allow,
    _ask,
    _deny,
    _env_flag,
    _get,
    _get_env,
    _load_sibling,
    _no_opinion,
    is_claude_code_payload,
    is_codex_hook_payload,
    is_codex_payload,
    is_primary_agent,
)
from mitosis_hook.models import (  # noqa: F401,E402
    CLAUDE_POLICY_NOTICE,
    CODEX_EFFORT_LEVELS,
    CODEX_MODEL_RE,
    CODEX_POLICY_NOTICE,
    FRONTIER_FAMILY_PREFIXES,
    FRONTIER_GPT_RE,
    FRONTIER_SEGMENTS,
    MIN_FRONTIER_PARAMS_B,
    PARAM_SIZE_RE,
    POLICY_NOTICE,
    SMALL_TIER_RE,
    _delegation_hint,
    _read_codex_config_toml,
    _read_settings,
    _write_codex_config_model,
    _write_settings_atomic,
    check_primary_model_settings,
    claude_policy_notice,
    claude_tier_effort,
    claude_tier_model,
    codex_config_path,
    codex_policy_notice,
    codex_tier_effort,
    codex_tier_model,
    is_argon_tier,
    is_frontier_tier,
    is_mitosis_tier,
    settings_path,
    valid_codex_model,
)
from mitosis_hook.report import (  # noqa: F401,E402
    MAX_REPORT_BULLET_LINES,
    MAX_SUBAGENT_REPORT_CHARS,
    REPORT_BLOCK_REASON,
    REPORT_BULLET_RE,
    REPORT_FENCE_RE,
    REPORT_FORMAT_HEADER,
    REPORT_FORMAT_HEADERS,
    REPORT_FORMAT_PHRASE_RE,
    REPORT_FORMAT_SCOUT,
    REPORT_FORMAT_SUPERSEDE,
    REPORT_FORMAT_TAIL_RE,
    REPORT_FORMAT_WORKER,
    REPORT_LINE_PATTERNS,
    REPORT_LOG_URI_RE,
    _REPORT_STATUS_ALT,
    _append_report_format_enabled,
    append_report_format,
    prompt_has_report_format,
    strip_report_format,
    validate_subagent_report,
)
from mitosis_hook.routing import (  # noqa: F401,E402
    CJK_DETERMINER_BEFORE_RE,
    CJK_HEAVY_NOUN_AFTER_RE,
    CJK_HEAVY_RE,
    CJK_LITE_RE,
    CJK_MUTATION_NOUN_AFTER_RE,
    CJK_MUTATION_RE,
    CJK_NEGATED_MUTATION_RE,
    CJK_NOUN_BEFORE_RE,
    CLAUSE_START_RE,
    CLI_START_RE,
    DETERMINER_BEFORE_RE,
    DOCS_LOOKUP_RE,
    EXECUTION_RE,
    EXECUTION_SIGNAL_RE,
    GIT_MUTATION_RE,
    HEAVY_RE,
    IMPERATIVE_MUTATION_RE,
    IMPERATIVE_VERBS,
    JUDGEABLE_RULES,
    QUESTION_NOUNS,
    is_read_only_command,
    LABEL_NOUN_AFTER_RE,
    LITE_RE,
    LOOKUP_SIGNAL_RE,
    LOOSE_DETERMINER_BEFORE_RE,
    MUTATION_RE,
    NEGATED_MUTATION_RE,
    NOUNISH_KEYWORDS,
    NUMBERED_RULE_RE,
    POLITE_CJK_RE,
    POLITE_STRONG_EN_RE,
    POLITE_WEAK_EN_RE,
    QUESTION_RE,
    READ_ONLY_CONFIDENCE,
    READ_ONLY_RE,
    RULE_CONFIDENCE,
    RUN_TASK_RE,
    RUN_VERBS,
    SOFT_REQUEST_RE,
    TEST_RUNNER_RE,
    _ADJ,
    _CHECKER_ALT,
    _JUDGE_MODULE,
    _MUTATION_ALT,
    _after_determiner,
    _apply_judge,
    _before_label,
    _blank,
    _decide_by_rules,
    _is_noun_use,
    _is_path_part,
    _load_judge,
    _read_only_has_last_word,
    _rule_decision,
    _strip_negated_mutations,
    _tier_effort,
    classify_subagent,
    classify_subagent_tier,
    decide_subagent,
    find_heavy,
    find_heavy_ex,
    find_lite,
    find_mutation,
    find_shell_mutation,
    find_trailing_imperative,
    find_mutations,
    find_polite_request,
    find_run_task,
)
from mitosis_hook.state import (  # noqa: F401,E402
    CONVERSATION_ID_RE,
    LOCK_FILENAME,
    SUBAGENT_ALIVE_STATE,
    _HANDLER_DEPTH,
    _HELD_LOCKS,
    _RECENT_DECISIONS,
    _acquire_state_lock,
    _empty_state,
    _fcntl,
    _flush_otel,
    _lock_timeout,
    _msvcrt,
    _otel_enabled,
    _payload_session_id,
    _peek_state,
    _read_state_file,
    _record_routing,
    _record_tier,
    _release_state_locks,
    _releases_state_locks,
    _state_file,
    _subagent_metadata_dir,
    _try_lock_fd,
    load_state,
    save_state,
    sync_subagents,
    sync_subagents_from_artifacts,
    sync_subagents_from_transcript,
)
from mitosis_hook.guards import (  # noqa: F401,E402
    ABS_PATH_TOKEN_RE,
    AGENT_CALL_RE,
    DOTDOT_TOKEN_RE,
    ESCALATION_RE,
    FORCE_PUSH_RE,
    MODEL_KWARG_RE,
    PATH_ARG_KEYS,
    PATH_LIST_ARG_KEYS,
    PIPE_TO_SHELL_RE,
    RECURSIVE_DELETE_OF_ROOT_RE,
    SCOUT_FORBIDDEN_ARGS,
    SCOUT_GIT_SUBCOMMANDS,
    SCOUT_READ_ONLY_COMMANDS,
    SYSTEM_ALTERING_RE,
    SYSTEM_PATH_PREFIXES,
    WRITE_TOOL_RE,
    _ASSIGN_RE,
    _CODEX_VALUE_OPTS,
    _HEREDOC_RE,
    _REDIRECT_RE,
    _SCOUT_HARMLESS_REDIRECT_RE,
    _SCOUT_SEGMENT_SPLIT_RE,
    _agent_call_bodies,
    _extract_apply_patch_files,
    _iter_strings,
    _parse_codex_exec,
    _rewrite_codex_exec_model,
    _split_simple_commands,
    _tokenize_with_spans,
    _within_roots,
    allowed_roots,
    command_stays_in_workspace,
    scout_command_is_read_only,
    workflow_model_violations,
)
from mitosis_hook.platforms.claude import (  # noqa: F401,E402
    CLAUDE_AGENT_ID_RE,
    _claude_agent_ids_from_response,
    _claude_register_agent,
    _claude_subagent_in_scope,
    _handle_agent_message,
    _handle_claude_subagent_stop,
    _handle_claude_task_or_agent,
    _is_plugin_agent_type,
    _passthrough_tier,
)
from mitosis_hook.platforms.codex import (  # noqa: F401,E402
    CODEX_COLLAB_TOOLS,
    CODEX_COLLAB_TOOL_RE,
    OPAQUE_MESSAGE_RE,
    _codex_agent_key,
    _codex_confirm_spawn,
    _codex_tool_name,
    _extract_tool_call,
    _handle_codex_exec_command,
    _handle_mitosis_tools,
    _handle_spawn_agent,
    _is_opaque_message,
    _codex_rollout_meta,
    codex_bind_agent,
    codex_subagent_stop,
    handle_codex_notify,
)
from mitosis_hook.platforms.agy import (  # noqa: F401,E402
    _handle_define_subagent,
    _handle_invoke_subagent,
    _handle_manage_subagents,
    _handle_run_workflow,
)
from mitosis_hook.handlers import (  # noqa: F401,E402
    LOG_OR_TRANSCRIPT_RE,
    _debug_log,
    _handle_workspace_guard,
    _tool_failed,
    handle_post_tool_use,
    handle_pre_invocation,
    handle_pre_tool_use,
    handle_session_start,
    handle_stop,
    handle_subagent_stop,
)


HANDLERS = {
    "session-start": handle_session_start,
    "pre-invocation": handle_pre_invocation,
    "user-prompt-submit": handle_pre_invocation,
    "pre-tool-use": handle_pre_tool_use,
    "post-tool-use": handle_post_tool_use,
    "stop": handle_stop,
    "subagent-stop": handle_subagent_stop,
    "codex-notify": handle_codex_notify,
}

EVENT_NAME_TO_MODE = {
    "SessionStart": "session-start",
    "PreInvocation": "pre-invocation",
    "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use",
    "PostToolUseFailure": "post-tool-use",
    "Stop": "stop",
    "SubagentStop": "subagent-stop",
    "CodexNotify": "codex-notify",
    "codex-notify": "codex-notify",
    "agent-turn-complete": "codex-notify",
    "turn-ended": "codex-notify",
}


def main() -> None:
    # hooks/codex-hooks.json passes --platform=codex: Codex SessionStart carries no turn_id.
    platform = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--platform=")), "")
    argv = [a for a in sys.argv if not a.startswith("--platform=")]
    mode = argv[1] if len(argv) > 1 and not argv[1].startswith("{") else None
    try:
        raw = sys.stdin.read()
        if not raw.strip() and len(sys.argv) > 1 and sys.argv[-1].startswith("{"):
            raw = sys.argv[-1]
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    if platform:
        payload["platform"] = platform

    if not mode:
        ev = payload.get("hook_event_name") or payload.get("event") or payload.get("type")
        if ev:
            mode = EVENT_NAME_TO_MODE.get(ev)
    if not mode:
        mode = "pre-tool-use"

    handler = HANDLERS.get(mode)
    try:
        result = handler(payload) if handler else {}
    except Exception as exc:  # never break the agent loop because of the guard
        result = _no_opinion(payload) if mode in ("stop", "pre-tool-use") else {}
        sys.stderr.write(f"[mitosis-orchestrator] {mode} hook error: {exc!r}\n")
    finally:
        _release_state_locks()
    _debug_log(payload, mode, result)

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    _flush_otel()  # after the response and after the locks are released


if __name__ == "__main__":
    main()
