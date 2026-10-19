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
    AGY_MITOSIS_APPEND_REPORT_FORMAT=0  do not append the REPORT FORMAT block to subagent prompts
                                        that lack one (agy invoke_subagent, Claude Code Agent)
    AGY_MITOSIS_DEBUG=1                 append one JSON line per hook call to
                                        <artifactDirectoryPath>/mitosis_orchestrator_debug.log
    (All variables support legacy AGY_ARGON_* and AGY_ALLOW_NON_ARGON_PRIMARY fallbacks)
"""

import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

DEFAULT_PRIMARY_MODEL = "Gemini 4 Argon (High)"
DEFAULT_CODEX_PRIMARY_MODEL = "gpt-6.1-sol"
DEFAULT_CODEX_WORKER_MODEL = "gpt-5.6-terra"
DEFAULT_CODEX_SCOUT_MODEL = "gpt-5.6-luna"
DEFAULT_CLAUDE_PRIMARY_MODEL = "opus"
DEFAULT_CLAUDE_WORKER_MODEL = "sonnet"
DEFAULT_CLAUDE_SCOUT_MODEL = "haiku"
DEFAULT_CLAUDE_SCOUT_EFFORT = "low"
DEFAULT_CLAUDE_WORKER_EFFORT = "medium"
CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
CLAUDE_PLUGIN_PREFIX = "mitosis-orchestrator"
# Built-in Claude Code subagent types that the pinning policy must never rewrite.
CLAUDE_BUILTIN_AGENT_TYPES = {
    "explore", "plan", "general-purpose", "claude", "claude-code-guide",
    "statusline-setup", "output-style-setup", "bash",
}
CLAUDE_SUBAGENT_MODELS = {"haiku", "sonnet", "opus", "fable", "best"}
CLAUDE_HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop", "Stop", "Notification", "PreCompact",
}
ALLOWED_PRIMARY_MODELS = {
    "Gemini 4 Argon (High)",
    "Gemini 4 Argon (Medium)",
}
ALLOWED_SUBAGENT_MODELS = {"flash", "flash_lite"}
SCOUT_TYPE = "flash-lite-scout"
WORKER_TYPE = "flash-worker"
STATE_FILENAME = "mitosis_orchestrator_state.json"


def is_codex_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches OpenAI Codex CLI hook or MCP input schemas."""
    if not isinstance(payload, dict):
        return False
    if payload.get("client") == "codex" or payload.get("platform") == "codex":
        return True
    # Claude Code Stop / SubagentStop payloads also carry `last_assistant_message`;
    # a Claude hook event name settles it before the key-based heuristics below.
    if str(payload.get("hook_event_name") or "") in CLAUDE_HOOK_EVENTS:
        return False
    if any(k in payload for k in ("turn_id", "turn-id", "thread_id", "thread-id", "call_id", "last-assistant-message", "last_assistant_message", "input-messages", "input_messages")):
        return True
    if payload.get("type") in ("agent-turn-complete", "approval-requested", "turn-ended") or \
       payload.get("event") in ("agent-turn-complete", "approval-requested", "turn-ended"):
        return True
    if str(payload.get("hook_event_name") or "").lower() in ("codex-notify", "turn-ended"):
        return True
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if not tool_name and isinstance(payload.get("toolCall"), dict):
        tool_name = str(payload["toolCall"].get("name") or "").strip()
    if not tool_name and isinstance(payload.get("tool_call"), dict):
        tool_name = str(payload["tool_call"].get("name") or "").strip()
    if tool_name in (
        "apply_patch", "exec_command", "local_shell", "shell",
        "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
    ):
        return True
    return False


def is_claude_code_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches Claude Code hook input schemas."""
    if not isinstance(payload, dict):
        return False
    if is_codex_payload(payload):
        return False
    return any(k in payload for k in ("hook_event_name", "session_id", "tool_name", "tool_input"))

# ---------------------------------------------------------------------------
# Classification vocabularies
# ---------------------------------------------------------------------------

# Prompts that ask the subagent to change the workspace. These NEVER run on
# flash_lite, whatever the Workspace mode or the requested Model.
MUTATION_RE = re.compile(
    r"\b("
    r"implement|refactor|rewrite|edit|modify|patch|fix(?:es|ing)?|"
    r"write (?:a |the |new )?(?:file|test|tests|code|module|function|class)|"
    r"create (?:a |the |new )?(?:file|test|tests|module|function|class|script)|"
    r"update (?:the )?(?:file|files|config|test|tests|code)|"
    r"delete|remove|rename|replace|insert|append|"
    r"migrate|migration|add (?:a |the |new )?(?:test|tests|file|function|method|check)"
    r")\b",
    re.IGNORECASE,
)

# "Do not edit anything", "never modify", "without deleting" must not count as mutation requests.
# Up to two words may sit between the negation and the verb, and "or"/"nor"
# chains it, so "Do NOT git fetch/pull or modify" and "never delete or rename"
# are prohibitions too. "and" is left out: "don't X and fix Y" asks for a fix.
_MUTATION_ALT = MUTATION_RE.pattern[3:-3]
NEGATED_MUTATION_RE = re.compile(
    r"\b(?:do not|don't|dont|never|must not|mustn't|should not|shouldn't|without|not|no)\s+"
    r"(?:(?!\b(?:" + _MUTATION_ALT + r")\b)[\w/.-]+,?\s+){0,2}"
    r"(?:(?:or|nor)\s+)?"
    r"(?:" + _MUTATION_ALT + r")\b"
    r"(?:,?\s+(?:or|nor)\s+(?:" + _MUTATION_ALT + r")\b)*",
    re.IGNORECASE,
)


def _strip_negated_mutations(text: str) -> str:
    """Remove negated mutation phrases so prohibitions do not read as tasks."""
    return NEGATED_MUTATION_RE.sub(" ", text)


# Moderate-complexity work that needs flash even when read-only.
HEAVY_RE = re.compile(
    r"\b("
    r"tests?|pytest|unittest|jest|vitest|go test|cargo test|npm test|"
    r"debug|bug|root[- ]cause|multi-file|across (?:files|modules|packages)|"
    r"call[- ]?graph|dependency|dependencies|trace|synthesi[sz]e|"
    r"benchmark|build|compile|integration|deep[- ]dive|"
    r"search_web|read_url_content|documentation|docs? (?:for|on|about)"
    r")\b",
    re.IGNORECASE,
)

# Bounded single-hop lookups. Deliberately narrow: bare words such as
# "find", "ls", "count", "status" or "version" are too common in English and
# produced false flash_lite routing in 1.1.0.
LITE_RE = re.compile(
    r"\b("
    r"scout|look ?up|locate|where is|where are|"
    r"grep|ripgrep|glob|search for|find_by_name|grep_search|list_dir|view_file|"
    r"list (?:the |all )?files|list (?:the )?directory|"
    r"git status|git log|git blame|check (?:the )?logs?|tail (?:the )?logs?|"
    r"line numbers?|single file|one file|"
    r"summari[sz]e (?:this |the )?(?:file|readme|config)|"
    r"inspect (?:this |the )?(?:file|config)|"
    r"(?:file|path|symbol|function|class) exists?|does .{1,40} exist"
    r")\b",
    re.IGNORECASE,
)

# The caller declares the task read-only. Mutation and heavy keywords in such
# prompts are usually the topic ("review the fixes", "plan the migration"),
# so those rules lose their confidence and a judge may take over.
READ_ONLY_RE = re.compile(
    r"\bread[- ]?only\b|"
    r"\bdo(?: not|n't) (?:modify|edit|change|write|touch) (?:anything|any files?|the (?:repo|code|workspace))\b|"
    r"\bno (?:edits|writes|changes)\b|"
    r"唯讀|只讀|不要(?:修改|改動|寫入)",
    re.IGNORECASE,
)
READ_ONLY_CONFIDENCE = 0.6

NUMBERED_RULE_RE = re.compile(r"(?m)^\s*[0-9]+[.)]\s+")
CONVERSATION_ID_RE = re.compile(
    r'\\*"conversationId\\*"\s*:\s*\\*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\*"',
    re.I,
)
AGENT_CALL_RE = re.compile(r"\bagent\s*\(")
MODEL_KWARG_RE = re.compile(r"""\bmodel\s*=\s*(?:["']([^"']*)["']|([A-Za-z_][A-Za-z0-9_.]*))""")

SAFE_TERMINATION_REASONS = {
    "USER_CANCELED",
    "ERROR",
    "MAX_INVOCATIONS",
    "MAX_FORCED_INVOCATIONS",
    "MAX_TOKEN_BUDGET_EXCEEDED",
}

MAX_SUBAGENT_REPORT_CHARS = 1600
MAX_REPORT_BULLET_LINES = 10
REPORT_BULLET_RE = re.compile(r"^[-*]\s+\S+")
REPORT_LOG_URI_RE = re.compile(r"file://\S+\.log\b", re.IGNORECASE)
REPORT_FENCE_RE = re.compile(r"^\s*`{3,}[\w-]*\s*$")
CLAUDE_AGENT_ID_RE = re.compile(r"agentId\"?\s*[:=]\s*\"?([0-9a-f]{8,})", re.IGNORECASE)
REPORT_LINE_PATTERNS = (
    re.compile(r"^STATUS:\s*(?:COMPLETED|BLOCKED|FAILED)\b", re.IGNORECASE),
    re.compile(r"^FILES_CHANGED:\s*\S+"),
    re.compile(r"^VERIFICATION:\s*\S+"),
    re.compile(r"^NOTES:\s*\S+"),
)

REPORT_FORMAT_HEADER = "REPORT FORMAT:"
REPORT_FORMAT_HEADERS = ("STATUS:", "FILES_CHANGED:", "VERIFICATION:", "NOTES:")
REPORT_FORMAT_PHRASE_RE = re.compile(r"REPORT FORMAT", re.IGNORECASE)
REPORT_FORMAT_SUPERSEDE = (
    "REPORT FORMAT (required by the orchestrator hook; this supersedes any other report format above):"
)
REPORT_FORMAT_SCOUT = (
    "REPORT FORMAT:\n"
    "Return only these four headers (plus up to 10 `- ` bullet lines under NOTES if needed):\n"
    "STATUS: COMPLETED | BLOCKED | FAILED\n"
    "FILES_CHANGED: NONE\n"
    "VERIFICATION: <one sentence stating what was checked and the result>\n"
    "NOTES: <findings (and up to 10 `- ` bullets), or file:// log URI if BLOCKED/FAILED>"
)
REPORT_FORMAT_WORKER = (
    "REPORT FORMAT:\n"
    "Return only these four headers (plus up to 10 `- ` bullet lines under NOTES if needed):\n"
    "STATUS: COMPLETED | BLOCKED | FAILED\n"
    "FILES_CHANGED: <comma-separated paths or NONE>\n"
    "VERIFICATION: <one sentence stating the command run and exit result>\n"
    "NOTES: <summary (and up to 10 `- ` bullets), or file:// log URI if BLOCKED/FAILED>"
)


def prompt_has_report_format(prompt: str) -> bool:
    """True only when the prompt carries all four canonical headers; an ad-hoc
    'REPORT FORMAT:' with other headers does not count (SubagentStop would reject it)."""
    text = prompt or ""
    return all(h in text for h in REPORT_FORMAT_HEADERS)


def append_report_format(prompt: str, tier: str) -> str:
    """Append the bounded four-header REPORT FORMAT block when the prompt lacks one.

    If the prompt already names a different report format, the canonical block is
    still appended, headed by a line saying it supersedes the earlier one."""
    if prompt_has_report_format(prompt):
        return prompt
    block = REPORT_FORMAT_SCOUT if tier == "flash_lite" else REPORT_FORMAT_WORKER
    if REPORT_FORMAT_PHRASE_RE.search(prompt or ""):
        block = block.replace(REPORT_FORMAT_HEADER, REPORT_FORMAT_SUPERSEDE, 1)
    return (prompt or "").rstrip() + "\n\n" + block


def _append_report_format_enabled() -> bool:
    return _get_env("AGY_MITOSIS_APPEND_REPORT_FORMAT", default="1") != "0"


TEST_RUNNER_RE = re.compile(
    r"\b(pytest|python(?:\d+(?:\.\d+)?)?\s+-m\s+unittest|npm\s+test|pnpm\s+test|yarn\s+test|vitest|jest|cargo\s+test|go\s+test)\b",
    re.IGNORECASE,
)
LOG_OR_TRANSCRIPT_RE = re.compile(
    r"(?:^|[/\\])(?:transcript(?:_full)?\.jsonl|[^/\\]+\.log)$",
    re.IGNORECASE,
)
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "Read", "NotebookRead", "read_file", "read"}
MUTATION_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "write_file",
    "apply_patch",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _extract_apply_patch_files(patch_text_or_args: Any) -> List[str]:
    """Extract list of target file paths from apply_patch tool arguments or raw diff text."""
    files: List[str] = []
    text = ""
    if isinstance(patch_text_or_args, dict):
        for k in ("file_path", "filePath", "path", "target_file", "targetFile", "file"):
            val = patch_text_or_args.get(k)
            if isinstance(val, str) and val.strip():
                files.append(val.strip())
        list_val = patch_text_or_args.get("files") or patch_text_or_args.get("file_paths")
        if isinstance(list_val, list):
            for v in list_val:
                if isinstance(v, str) and v.strip():
                    files.append(v.strip())
        for k in ("patch", "diff", "content", "patch_text"):
            val = patch_text_or_args.get(k)
            if isinstance(val, str):
                text += "\n" + val
    elif isinstance(patch_text_or_args, str):
        text = patch_text_or_args

    if text:
        for m in re.finditer(r"^\s*diff\s+--git\s+[a-z]/(\S+)\s+[a-z]/(\S+)", text, re.MULTILINE):
            files.append(m.group(2))
        for m in re.finditer(r"^\s*Index:\s+([^\r\n\t]+)", text, re.MULTILINE):
            files.append(m.group(1).strip())
        for m in re.finditer(r"^\s*\*\*\*\s+([^\r\n\t]+)", text, re.MULTILINE):
            path_str = m.group(1).strip()
            path_str = re.split(r"\s{2,}|\t", path_str)[0].strip()
            if path_str.startswith(("a/", "b/")):
                path_str = path_str[2:]
            files.append(path_str)
        for m in re.finditer(r"^\s*(?:---|\+\+\+)\s+([^\r\n\t]+)", text, re.MULTILINE):
            path_str = m.group(1).strip()
            path_str = re.split(r"\s{2,}|\t", path_str)[0].strip()
            if path_str.startswith(("a/", "b/")):
                path_str = path_str[2:]
            files.append(path_str)

    cleaned: List[str] = []
    seen = set()
    for f in files:
        f = f.strip().strip("'\"")
        if not f or f in ("/dev/null", "dev/null"):
            continue
        if f not in seen:
            seen.add(f)
            cleaned.append(f)
    return cleaned


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
        return name, args
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


def _allow(
    reason: str = "Mitosis Orchestrator: pass-through.",
    overrides: Optional[List[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    updated_input: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if payload and is_claude_code_payload(payload):
        out: Dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            out["updatedInput"] = updated_input
        return {"hookSpecificOutput": out}
    if payload and is_codex_payload(payload):
        res: Dict[str, Any] = {
            "decision": "allow",
            "reason": reason,
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            res["updatedInput"] = updated_input
            res["overwrite"] = updated_input
        if overrides:
            res["permissionOverrides"] = overrides
        return res
    res: Dict[str, Any] = {"decision": "allow", "reason": reason}
    if updated_input is not None:
        res["overwrite"] = updated_input
    if overrides:
        res["permissionOverrides"] = overrides
    return res


def _deny(reason: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and is_claude_code_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if payload and is_codex_payload(payload):
        return {
            "decision": "deny",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    return {"decision": "deny", "reason": reason}


def _ask(reason: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and is_claude_code_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }
    if payload and is_codex_payload(payload):
        # Codex hooks honour allow/deny only; "ask" is treated as a failed hook.
        return {
            "decision": "deny",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    return {"decision": "ask", "reason": reason}


def _delegation_hint(payload: Optional[Dict[str, Any]], tier: str) -> str:
    """Platform-specific wording that tells the primary agent how to delegate."""
    if payload and is_claude_code_payload(payload):
        if tier == "flash_lite":
            return (
                f"`Agent` with `subagent_type: \"{CLAUDE_PLUGIN_PREFIX}:{SCOUT_TYPE}\"` "
                f"(model: {claude_tier_model('flash_lite')})"
            )
        return (
            f"`Agent` with `subagent_type: \"{CLAUDE_PLUGIN_PREFIX}:{WORKER_TYPE}\"` "
            f"(model: {claude_tier_model('flash')})"
        )
    if payload and is_codex_payload(payload):
        if tier == "flash_lite":
            return f"`spawn_agent` with `agent_type: \"explorer\"` ({DEFAULT_CODEX_SCOUT_MODEL})"
        return f"`spawn_agent` with `agent_type: \"worker\"` ({DEFAULT_CODEX_WORKER_MODEL})"
    if tier == "flash_lite":
        return f"`{SCOUT_TYPE}` (TypeName='{SCOUT_TYPE}', Model='flash_lite')"
    return f"`{WORKER_TYPE}` (TypeName='{WORKER_TYPE}', Model='flash')"


def is_primary_agent(payload: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
    """Return True if caller is the primary orchestrator agent (not a subagent)."""
    if payload.get("isSubagent") is True or payload.get("is_subagent") is True:
        return False
    # Claude Code adds agent_id / agent_type to hook input when a subagent makes the call.
    if payload.get("agent_id") or payload.get("agent_type"):
        return False
    cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or "").strip().lower()
    if state and cid and cid in state.get("subagents", {}):
        return False
    for wp in payload.get("workspacePaths") or []:
        if "/worktrees/" in str(wp).replace("\\", "/"):
            return False
    cwd = str(payload.get("cwd") or "").replace("\\", "/")
    if "/worktrees/" in cwd:
        return False
    return True


def validate_subagent_report(text: str) -> Optional[str]:
    """Validate that a subagent report strictly follows the 4-header bounded format.

    Headers required:
      STATUS: COMPLETED | BLOCKED | FAILED
      FILES_CHANGED: <comma-separated paths or NONE>
      VERIFICATION: <description of verification command and result>
      NOTES: <single sentence summary or up to 10 bullet lines (- or *)>

    Constraints:
      - Total length <= 1600 characters.
      - At most 10 bullet lines following NOTES:.
      - If STATUS is BLOCKED or FAILED, NOTES must include a file:// URI to a .log file.

    Returns None if valid, or an error description string if invalid.
    """
    stripped = "\n".join(l for l in (text or "").splitlines() if not REPORT_FENCE_RE.match(l)).strip()
    if not stripped:
        return "Subagent report is empty."
    if len(stripped) > MAX_SUBAGENT_REPORT_CHARS:
        return (
            f"Subagent report length ({len(stripped)} chars) exceeds maximum "
            f"{MAX_SUBAGENT_REPORT_CHARS} chars."
        )
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    max_lines = 4 + MAX_REPORT_BULLET_LINES
    if len(lines) < 4:
        return f"Expected at least 4 header lines (STATUS, FILES_CHANGED, VERIFICATION, NOTES), got {len(lines)}."
    if len(lines) > max_lines:
        return (
            f"Expected at most 4 headers plus {MAX_REPORT_BULLET_LINES} bullet lines "
            f"({max_lines} lines total), got {len(lines)}."
        )

    expected_prefixes = ("STATUS:", "FILES_CHANGED:", "VERIFICATION:", "NOTES:")
    for idx, (pattern, prefix) in enumerate(zip(REPORT_LINE_PATTERNS, expected_prefixes), start=1):
        if not pattern.match(lines[idx - 1]):
            # `NOTES:` may be bare when bullet lines follow it.
            if prefix == "NOTES:" and re.match(r"^NOTES:\s*$", lines[idx - 1], re.IGNORECASE) and len(lines) > 4:
                continue
            return f"Line {idx} must match '{prefix} <value>'."

    bullet_lines = lines[4:]
    for b_idx, b_line in enumerate(bullet_lines, start=5):
        if not REPORT_BULLET_RE.match(b_line):
            return f"Line {b_idx} after NOTES must be a compact bullet starting with '- ' or '* '."

    status_match = re.match(r"^STATUS:\s*(\w+)", lines[0], re.IGNORECASE)
    status_val = status_match.group(1).upper() if status_match else ""
    if status_val in {"BLOCKED", "FAILED"}:
        notes_body = lines[3].split(":", 1)[1].strip() if ":" in lines[3] else ""
        combined_notes = " ".join([notes_body] + [b[1:].strip() for b in bullet_lines]).strip()
        if not REPORT_LOG_URI_RE.search(combined_notes):
            return f"When STATUS is {status_val}, NOTES must include a 'file://.../*.log' scratch log URI."

    return None


SMALL_TIER_RE = re.compile(r"(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])", re.IGNORECASE)
FRONTIER_TOKENS = (
    "argon",
    "pro",
    "ultra",
    "claude",
    "opus",
    "sonnet",
    "fable",
    "mythos",
    "grok",
    "gpt-4",
    "gpt-5",
    "gpt-6",
    "chatgpt-4o",
    "o1",
    "o3",
    "o4",
    "deepseek",
    "qwen",
    "qwq",
)


def is_frontier_tier(model_name: str) -> bool:
    """True for Argon / Pro / Ultra / Claude / frontier models; False for Flash, Lite, Mini, Nano, Haiku, Auto or empty."""
    normalized = (model_name or "").strip().lower()
    if not normalized or normalized in {"auto", "none", "inherit"}:
        return False
    if SMALL_TIER_RE.search(normalized):
        return False
    return any(token in normalized for token in FRONTIER_TOKENS)


is_mitosis_tier = is_frontier_tier
is_argon_tier = is_frontier_tier


def _get_env(*names: str, default: str = "") -> str:
    expanded_names: List[str] = []
    for name in names:
        if name.startswith("AGY_MITOSIS_"):
            suffix = name[len("AGY_MITOSIS_"):]
            expanded_names.append("CODEX_MITOSIS_" + suffix)
            expanded_names.append("CLAUDE_MITOSIS_" + suffix)
        expanded_names.append(name)
    for name in expanded_names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return default


def _env_flag(*names: str) -> bool:
    expanded_names: List[str] = []
    for name in names:
        if name.startswith("AGY_MITOSIS_"):
            suffix = name[len("AGY_MITOSIS_"):]
            expanded_names.append("CODEX_MITOSIS_" + suffix)
            expanded_names.append("CLAUDE_MITOSIS_" + suffix)
        expanded_names.append(name)
    for name in expanded_names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip() == "1"
    return False


def claude_tier_model(tier: str) -> str:
    """Claude Code model for a subagent tier, honouring CLAUDE_MITOSIS_{SCOUT,WORKER}_MODEL."""
    if tier == "flash_lite":
        default, env_name = DEFAULT_CLAUDE_SCOUT_MODEL, "CLAUDE_MITOSIS_SCOUT_MODEL"
    else:
        default, env_name = DEFAULT_CLAUDE_WORKER_MODEL, "CLAUDE_MITOSIS_WORKER_MODEL"
    override = _get_env(env_name).lower()
    return override if override in CLAUDE_SUBAGENT_MODELS else default


def claude_tier_effort(tier: str) -> Optional[str]:
    """Claude Code effort for a subagent tier (None when CLAUDE_MITOSIS_PIN_EFFORT=0).

    Scouts default to `low`, workers to `medium`; CLAUDE_MITOSIS_{SCOUT,WORKER}_EFFORT
    override them. Invalid values fall back to the default.
    """
    if _get_env("CLAUDE_MITOSIS_PIN_EFFORT", default="1") == "0":
        return None
    if tier == "flash_lite":
        default, env_name = DEFAULT_CLAUDE_SCOUT_EFFORT, "CLAUDE_MITOSIS_SCOUT_EFFORT"
    else:
        default, env_name = DEFAULT_CLAUDE_WORKER_EFFORT, "CLAUDE_MITOSIS_WORKER_EFFORT"
    override = _get_env(env_name).lower()
    return override if override in CLAUDE_EFFORT_LEVELS else default


def codex_config_path(payload: Optional[Dict[str, Any]] = None) -> str:
    codex_override = os.environ.get("CODEX_CONFIG_PATH", "").strip()
    if codex_override:
        return os.path.expanduser(codex_override)
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return str(Path(os.path.expanduser(codex_home)) / "config.toml")
    return os.path.expanduser("~/.codex/config.toml")


def settings_path(payload: Optional[Dict[str, Any]] = None) -> str:
    codex_override = os.environ.get("CODEX_CONFIG_PATH", "").strip()
    if codex_override:
        return os.path.expanduser(codex_override)
    if payload and is_codex_payload(payload):
        return codex_config_path(payload)
    claude_override = os.environ.get("CLAUDE_SETTINGS_PATH", "").strip()
    if claude_override:
        return os.path.expanduser(claude_override)
    if payload and is_claude_code_payload(payload):
        return os.path.expanduser("~/.claude/settings.json")
    agy_override = os.environ.get("AGY_ANTIGRAVITY_SETTINGS", "").strip()
    if agy_override:
        return os.path.expanduser(agy_override)
    return os.path.expanduser("~/.gemini/antigravity-cli/settings.json")


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-tolerant lookup: _get(item, "Model", "model")."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


# ---------------------------------------------------------------------------
# State (per conversation, lives in the artifact directory)
# ---------------------------------------------------------------------------

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
        base = Path(tempfile.gettempdir()) / "mitosis-orchestrator" / cid
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / STATE_FILENAME


def load_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _state_file(payload)
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


def save_state(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = _state_file(payload)
    if not path:
        return
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
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


# ---------------------------------------------------------------------------
# session-start
# ---------------------------------------------------------------------------

def _read_settings(path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    target_path = path or settings_path()
    if not os.path.exists(target_path):
        return None, target_path
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else None), target_path
    except (OSError, json.JSONDecodeError):
        return None, target_path


def _write_settings_atomic(path: str, data: Dict[str, Any]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            json.dump(data, tmp_f, indent=2)
            tmp_f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _read_codex_config_toml(path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    target_path = path or codex_config_path()
    if not os.path.exists(target_path):
        return None, target_path
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'^\s*model\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        model = m.group(1).strip() if m else None
        data = {"model": model} if model else {}
        return data, target_path
    except OSError:
        return None, target_path


def _write_codex_config_model(path: str, model: str) -> None:
    dir_name = os.path.dirname(path) or "."
    try:
        os.makedirs(dir_name, exist_ok=True)
    except OSError:
        pass
    content = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    if re.search(r'^\s*model\s*=.*$', content, flags=re.MULTILINE):
        new_content = re.sub(r'^\s*model\s*=.*$', f'model = "{model}"', content, flags=re.MULTILINE)
    else:
        new_content = f'model = "{model}"\n' + content

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(new_content)
            if not new_content.endswith("\n"):
                tmp_f.write("\n")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def check_primary_model_settings(payload: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Return (status, model) where status is one of:
    'ok' (frontier/mitosis), 'warn' (non-frontier, not changed), 'pinned' (rewritten, opt-in),
    'unknown' (settings file missing/unreadable), 'silenced'.
    """
    path = settings_path(payload)
    is_codex = (payload and is_codex_payload(payload)) or "CODEX_CONFIG_PATH" in os.environ
    is_claude = (payload and is_claude_code_payload(payload)) or "CLAUDE_SETTINGS_PATH" in os.environ
    preferred = _get_env("CODEX_MITOSIS_PRIMARY_MODEL", "CLAUDE_MITOSIS_PRIMARY_MODEL", "AGY_MITOSIS_PRIMARY_MODEL", "AGY_ARGON_PRIMARY_MODEL")
    default_model = DEFAULT_CODEX_PRIMARY_MODEL if is_codex else (DEFAULT_CLAUDE_PRIMARY_MODEL if is_claude else DEFAULT_PRIMARY_MODEL)
    target = preferred if (preferred in ALLOWED_PRIMARY_MODELS or is_frontier_tier(preferred)) else default_model
    if is_codex:
        data, path = _read_codex_config_toml(path)
    else:
        data, path = _read_settings(path)
    if data is None:
        return "unknown", target
    current = str(data.get("model") or "").strip()

    if _env_flag(
        "CODEX_MITOSIS_ALLOW_NON_PRIMARY",
        "CLAUDE_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_ALLOW_NON_MITOSIS_PRIMARY",
        "AGY_ALLOW_NON_ARGON_PRIMARY",
        "AGY_ARGON_ALLOW_NON_PRIMARY",
    ):
        return "silenced", current or target
    if is_frontier_tier(current) and (not preferred or current == preferred):
        return "ok", current
    if not _env_flag("CODEX_MITOSIS_PIN_SETTINGS", "CLAUDE_MITOSIS_PIN_SETTINGS", "AGY_MITOSIS_PIN_SETTINGS", "AGY_ARGON_PIN_SETTINGS"):
        return "warn", current
    try:
        if is_codex:
            _write_codex_config_model(path, target)
        else:
            data["model"] = target
            _write_settings_atomic(path, data)
        return "pinned", target
    except Exception:
        return "warn", current


POLICY_NOTICE = (
    "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
    "location, git/log checks, single-file inspection) to `flash_lite` "
    "(`TypeName: \"flash-lite-scout\"`) and multi-file research, test runs, web/docs "
    "research and scoped code edits to `flash` (`TypeName: \"flash-worker\"`). "
    "Never use `Model: \"inherit\"` or `Model: \"pro\"` on subagents. "
    "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
)

def claude_policy_notice() -> str:
    scout_model, worker_model = claude_tier_model("flash_lite"), claude_tier_model("flash")
    scout_effort, worker_effort = claude_tier_effort("flash_lite"), claude_tier_effort("flash")
    scout_tag = f"{scout_model}, effort {scout_effort}" if scout_effort else scout_model
    worker_tag = f"{worker_model}, effort {worker_effort}" if worker_effort else worker_model
    return (
        "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
        "location, git/log checks, single-file inspection) to the `Agent` tool with "
        f"`subagent_type: \"mitosis-orchestrator:flash-lite-scout\"` ({scout_tag}) and multi-file research, "
        "test runs, web/docs research and scoped code edits to "
        f"`subagent_type: \"mitosis-orchestrator:flash-worker\"` ({worker_tag}). "
        "Untyped `Agent` calls are classified by prompt; other plugins' agents and built-in types "
        "(Explore, Plan, general-purpose) are left untouched. "
        "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
    )


# Backwards-compatible constant (default configuration).
CLAUDE_POLICY_NOTICE = claude_policy_notice()

CODEX_POLICY_NOTICE = (
    "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
    "location, git/log checks, single-file inspection) to `gpt-5.6-luna` "
    "(`subagent_type: \"flash-lite-scout\"`) and multi-file research, test runs, web/docs "
    "research and scoped code edits to `gpt-5.6-terra` (`subagent_type: \"flash-worker\"`). "
    "Never use `model: \"inherit\"` on subagents. "
    "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
)


def handle_session_start(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = payload or {}
    status, model = check_primary_model_settings(p)
    target_file = "config.toml" if (is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ) else "settings.json"
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

    if is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ:
        return {
            "additionalContext": f"[Mitosis Orchestrator Active] {note} {CODEX_POLICY_NOTICE}",
            "systemMessage": f"[Mitosis Orchestrator Active] {note} {CODEX_POLICY_NOTICE}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {CODEX_POLICY_NOTICE}",
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


# ---------------------------------------------------------------------------
# pre-invocation / user-prompt-submit
# ---------------------------------------------------------------------------

def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class Decision(NamedTuple):
    """One routing decision with the evidence behind it.

    `confidence` is a prior per rule for now (see RULE_CONFIDENCE); `source`
    says which layer decided: rule, model, judge or default.
    """
    tier: str
    type_override: Optional[str]
    effort: Optional[str]
    confidence: float
    source: str
    rule: str
    reason: str

    def as_log(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "type_override": self.type_override,
            "effort": self.effort,
            "confidence": self.confidence,
            "source": self.source,
            "rule": self.rule,
            "reason": self.reason,
        }


# Prior confidence per rule. Explicit signals are near certain; the prompt
# length fallbacks are guesses and are the cases a judge should take over.
RULE_CONFIDENCE = {
    "worker-type": 0.95,
    "mutation": 0.95,
    "scout-type": 0.95,
    "workspace": 0.95,
    "heavy": 0.9,
    "self-type": 0.95,
    "explicit-model": 0.95,
    "lite": 0.85,
    "long-prompt": 0.5,
    "short-prompt": 0.5,
    "default": 0.5,
}

# Rules a judge may overrule when their confidence is low. Type, workspace and
# explicit-model rules state the caller's intent and are never sent to a judge.
JUDGEABLE_RULES = {"mutation", "heavy", "lite", "long-prompt", "short-prompt", "default"}

# Decisions made during this process, read by _debug_log. Hooks are one-shot
# processes; the cap keeps the long-running MCP server from growing it.
_RECENT_DECISIONS: List[Decision] = []

_JUDGE_MODULE: Any = None


def _tier_effort(tier: str) -> str:
    return "low" if tier == "flash_lite" else "medium"


def _rule_decision(tier: str, type_override: Optional[str], rule: str, reason: str) -> Decision:
    return Decision(tier, type_override, _tier_effort(tier), RULE_CONFIDENCE[rule], "rule", rule, reason)


def _load_judge() -> Any:
    """Import scripts/mitosis_judge.py lazily, only when a judge is configured."""
    global _JUDGE_MODULE
    if _JUDGE_MODULE is None:
        import importlib.util
        path = Path(__file__).resolve().with_name("mitosis_judge.py")
        spec = importlib.util.spec_from_file_location("mitosis_judge", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _JUDGE_MODULE = module
    return _JUDGE_MODULE


def _apply_judge(decision: Decision, prompt: str, scout_typed: bool, worker_type_name: str) -> Decision:
    """Hand a low-confidence rule decision to the configured judge (MITOSIS_JUDGE_URL)."""
    if decision.rule not in JUDGEABLE_RULES or not _get_env("MITOSIS_JUDGE_URL"):
        return decision
    try:
        threshold = float(_get_env("MITOSIS_JUDGE_THRESHOLD", default="0.9"))
    except ValueError:
        threshold = 0.9
    if decision.confidence >= threshold:
        return decision
    try:
        verdict = _load_judge().judge_tier(prompt)
    except Exception as exc:  # the judge must never break routing
        verdict = None
        decision = decision._replace(reason=f"{decision.reason}; judge error {type(exc).__name__}")
    if verdict is None:
        # Unreachable or unsure judge: take the tier that cannot fail the task.
        return Decision("flash", decision.type_override, _tier_effort("flash"), decision.confidence,
                        "default", decision.rule, f"{decision.reason}; judge unavailable, conservative flash")
    tier, confidence = verdict
    type_override = decision.type_override
    if scout_typed:
        # Only a mutation verb reaches here for a scout: the judge confirms or vetoes the upgrade.
        type_override = worker_type_name if tier == "flash" else None
    return Decision(tier, type_override, _tier_effort(tier), round(confidence, 3), "judge", decision.rule,
                    f"{decision.reason}; judge chose {tier} ({confidence:.2f})")


def decide_subagent(subagent: Dict[str, Any]) -> Decision:
    """Return the routing Decision for a subagent spec: rules first, then the optional judge."""
    decision, prompt, scout_typed, worker_name = _decide_by_rules(subagent)
    if READ_ONLY_RE.search(prompt) and decision.rule in ("mutation", "heavy"):
        decision = decision._replace(confidence=min(decision.confidence, READ_ONLY_CONFIDENCE),
                                     reason=f"{decision.reason}; prompt declares read-only")
    decision = _apply_judge(decision, prompt, scout_typed, worker_name)
    _RECENT_DECISIONS.append(decision)
    del _RECENT_DECISIONS[:-16]
    return decision


def _decide_by_rules(subagent: Dict[str, Any]) -> Tuple[Decision, str, bool, str]:
    """Keyword rules. Returns (decision, role+prompt text, scout_typed, worker type name).

    Precedence (first match wins):
      1. flash-worker type                      -> flash
      2. mutation verbs in prompt               -> flash  (scout type is upgraded to worker)
      3. flash-lite-scout type                  -> flash_lite
      4. Workspace branch/share                 -> flash
      5. heavy signals (tests, debug, trace...) -> flash  (beats lite keywords)
      6. TypeName self (full tool set)          -> flash
      7. explicit Model flash/flash_lite        -> honoured
      8. lite signals, prompt <= 600 chars      -> flash_lite
      9. prompt > 450 chars                     -> flash
     10. short prompt (<= 320 chars)            -> flash_lite
     11. otherwise                              -> flash
    """
    raw_type_name = str(_get(subagent, "TypeName", "typeName", "subagent_type", "subagentType", default="")).strip()
    prefix = ""
    bare_type = raw_type_name.lower()
    if ":" in bare_type:
        prefix_part, bare_type = raw_type_name.split(":", 1)
        prefix = f"{prefix_part}:"
        bare_type = bare_type.strip().lower()

    role = str(_get(subagent, "Role", "role", default="")).strip()
    prompt = str(_get(subagent, "Prompt", "prompt", default="")).strip()
    workspace = str(_get(subagent, "Workspace", "workspace", default="inherit")).strip().lower()
    requested = str(_get(subagent, "Model", "model", default="")).strip().lower()
    text = f"{role}\n{prompt}"
    scout_typed = bare_type == SCOUT_TYPE
    worker_name = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE

    def done(decision: Decision) -> Tuple[Decision, str, bool, str]:
        return decision, text, scout_typed, worker_name

    mutation = MUTATION_RE.search(_strip_negated_mutations(text))
    heavy = HEAVY_RE.search(text)
    lite = LITE_RE.search(text)

    if bare_type == WORKER_TYPE:
        return done(_rule_decision("flash", None, "worker-type", "flash-worker type"))
    if mutation:
        if bare_type == SCOUT_TYPE:
            upgrade_type = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE
            return done(_rule_decision("flash", upgrade_type, "mutation",
                                  f"mutation verb '{mutation.group(0)}' on read-only scout; upgraded to flash-worker"))
        return done(_rule_decision("flash", None, "mutation", f"mutation verb '{mutation.group(0)}'"))
    if bare_type == SCOUT_TYPE:
        return done(_rule_decision("flash_lite", None, "scout-type", "flash-lite-scout type"))
    if workspace in ("branch", "share"):
        return done(_rule_decision("flash", None, "workspace", f"Workspace '{workspace}' implies writes"))
    if heavy:
        return done(_rule_decision("flash", None, "heavy", f"heavy signal '{heavy.group(0)}'"))
    if bare_type == "self":
        return done(_rule_decision("flash", None, "self-type", "self type carries the full tool set"))
    if requested in ALLOWED_SUBAGENT_MODELS:
        return done(_rule_decision(requested, None, "explicit-model", "explicit model honoured"))
    if lite and len(prompt) <= 600:
        return done(_rule_decision("flash_lite", None, "lite", f"lite signal '{lite.group(0)}'"))
    if len(prompt) > 450:
        return done(_rule_decision("flash", None, "long-prompt", "long prompt"))
    if len(prompt) <= 320:
        return done(_rule_decision("flash_lite", None, "short-prompt", "short prompt without heavy signals"))
    return done(_rule_decision("flash", None, "default", "default"))


def classify_subagent(subagent: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
    """Return (model, type_override_or_None, reason); see decide_subagent."""
    decision = decide_subagent(subagent)
    return decision.tier, decision.type_override, decision.reason


def classify_subagent_tier(subagent: Dict[str, Any]) -> str:
    """Backwards-compatible wrapper returning only the model tier."""
    return classify_subagent(subagent)[0]


# ---------------------------------------------------------------------------
# pre-tool-use handlers
# ---------------------------------------------------------------------------

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

        target_model, type_override, reason = classify_subagent(item)
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

    target_tier, type_override, reason = classify_subagent(item)
    claude_model = claude_tier_model(target_tier)

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
    spawns = [{
        "role": description or target_type or tool_name.lower(),
        "type": target_type,
        "model": target_tier,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    state = load_state(payload)
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


def _handle_send_message(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    is_claude = is_claude_code_payload(payload)
    recipient = str(_get(args, "Recipient", "recipient", "to", "To", default="")).strip().lower()
    message = str(_get(args, "Message", "message", default=""))
    tool_label = "SendMessage.message" if is_claude else "send_message.Message"
    if not message.strip():
        return _deny(f"{tool_label} cannot be empty.", payload) if is_claude else {"decision": "deny", "reason": f"{tool_label} cannot be empty."}

    state = load_state(payload)
    sync_subagents(payload, state)
    save_state(payload, state)

    info = state.get("subagents", {}).get(recipient)
    if not info or info.get("model") != "flash_lite":
        return _allow("Mitosis Orchestrator: pass-through.", payload=payload) if is_claude else {"decision": "allow"}

    mutation = MUTATION_RE.search(_strip_negated_mutations(message))
    rule_count = len(NUMBERED_RULE_RE.findall(message))
    if mutation or rule_count > 3:
        why = f"mutation verb '{mutation.group(0)}'" if mutation else f"{rule_count} numbered steps"
        reason = (
            f"Recipient '{recipient}' is a flash_lite '{info.get('type') or SCOUT_TYPE}' "
            f"subagent ({why}). Do not send code changes or multi-step (>3 rules) tasks to "
            f"a scout. Use {_delegation_hint(payload, 'flash')} instead."
        )
        return _deny(reason, payload) if is_claude else {"decision": "deny", "reason": reason}
    return _allow("Mitosis Orchestrator: pass-through.", payload=payload) if is_claude else {"decision": "allow"}


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
    for text in _iter_strings(payload.get("tool_response")):
        for m in CLAUDE_AGENT_ID_RE.finditer(text):
            aid = m.group(1).lower()
            if aid not in ids:
                ids.append(aid)
    return ids


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _agent_call_bodies(script: str) -> List[str]:
    """Return the argument text of every top-level agent(...) call (balanced parens)."""
    bodies = []
    for m in AGENT_CALL_RE.finditer(script):
        depth, i, start = 1, m.end(), m.end()
        while i < len(script) and depth:
            ch = script[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        bodies.append(script[start : i - 1] if depth == 0 else script[start:])
    return bodies


def workflow_model_violations(script: str) -> List[str]:
    violations = []
    for n, body in enumerate(_agent_call_bodies(script), 1):
        m = MODEL_KWARG_RE.search(body)
        if not m:
            violations.append(f"agent() call #{n} has no model= argument")
            continue
        literal, ident = m.group(1), m.group(2)
        if literal is not None and literal.strip().lower() not in ALLOWED_SUBAGENT_MODELS:
            violations.append(f"agent() call #{n} uses model={literal!r}")
        # A bare identifier (variable) is accepted; it cannot be checked statically.
    return violations


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


# ---------------------------------------------------------------------------
# Workspace scope guard
# ---------------------------------------------------------------------------
# Project-scoped file and command tools are answered "allow" so the primary
# agent is not interrupted for routine work inside its own workspace.
# Interactive sessions only: headless `agy -p` soft-denies every tool that
# would need a prompt regardless of hook output (verified on agy 1.4.0). Anything
# that reaches outside the workspace, looks destructive, or is a tool this
# guard does not understand is left to Antigravity's normal permission flow
# ("ask" or no opinion).

PATH_ARG_KEYS = (
    "TargetFile", "AbsolutePath", "SearchPath", "DirectoryPath", "FilePath",
    "NotebookPath", "Path", "SearchDirectory",
)
PATH_LIST_ARG_KEYS = ("Paths", "Files", "TargetFiles", "Includes")
WRITE_TOOL_RE = re.compile(r"write|replace|edit|create|delete|remove|rename|move|notebook", re.IGNORECASE)

# Absolute prefixes a command may mention without leaving the workspace
# (interpreters, system binaries, scratch space).
SYSTEM_PATH_PREFIXES = (
    "/dev/", "/usr/", "/bin/", "/sbin/", "/lib/", "/opt/", "/proc/",
    "/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/",
    "/System/", "/Library/", "/Applications/",
)

# Commands that must always go through the human permission prompt.
ESCALATION_RE = re.compile(
    r"(?:^|[\s;&|(])(?:sudo|doas|su)\b",
    re.IGNORECASE,
)
RECURSIVE_DELETE_OF_ROOT_RE = re.compile(
    r"(?:^|[\s;&|(])rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/|~|\$HOME)(?:\s|$)",
    re.IGNORECASE,
)
SYSTEM_ALTERING_RE = re.compile(
    r"(?:^|[\s;&|(])(?:mkfs(?:\.\w+)?|diskutil\s+erase\w*|shutdown|reboot|"
    r"launchctl\s+(?:unload|remove|bootout)|chown\s+-R|chmod\s+-R\s+\d+\s+/)",
    re.IGNORECASE,
)
FORCE_PUSH_RE = re.compile(r"\bgit\s+push\b[^|;&]*(?:--force\b|\s-f\b)", re.IGNORECASE)
PIPE_TO_SHELL_RE = re.compile(
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", re.IGNORECASE
)
ABS_PATH_TOKEN_RE = re.compile(r"""(?:^|[\s=:"'(])((?:/|~/?|\$HOME/?)[A-Za-z0-9._/\-]*)""")
DOTDOT_TOKEN_RE = re.compile(r"""(?:^|[\s=:"'(])([A-Za-z0-9._\-/]*\.\.[A-Za-z0-9._\-/]*)""")


def allowed_roots(payload: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for wp in payload.get("workspacePaths") or []:
        try:
            roots.append(Path(os.path.expanduser(str(wp))).resolve())
        except OSError:
            pass
    cwd = payload.get("cwd")
    if cwd:
        try:
            roots.append(Path(os.path.expanduser(str(cwd))).resolve())
        except OSError:
            pass
    proj_dir = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("CODEX_PROJECT_DIR")
        or payload.get("project_dir")
        or payload.get("workspace_dir")
    )
    if proj_dir:
        try:
            roots.append(Path(os.path.expanduser(str(proj_dir))).resolve())
        except OSError:
            pass
    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        try:
            roots.append(Path(str(artifact_dir)).resolve())
        except OSError:
            pass
    return roots


def _within_roots(target: Any, roots: List[Path]) -> bool:
    if not target or not roots:
        return False
    try:
        text = str(target)
        if text.startswith("file://"):
            text = text[len("file://"):]
        resolved = Path(os.path.expanduser(text)).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def command_stays_in_workspace(command: str, cwd: str, roots: List[Path]) -> Tuple[bool, str]:
    """Return (ok, reason). ok is False when the command names a path outside the
    roots or matches a pattern that should always be confirmed by a human."""
    for pattern, why in (
        (ESCALATION_RE, "privilege escalation"),
        (RECURSIVE_DELETE_OF_ROOT_RE, "recursive delete of a root directory"),
        (SYSTEM_ALTERING_RE, "system-altering command"),
        (FORCE_PUSH_RE, "force push"),
        (PIPE_TO_SHELL_RE, "download piped to a shell"),
    ):
        if pattern.search(command):
            return False, why
    home = os.path.expanduser("~")
    for m in ABS_PATH_TOKEN_RE.finditer(command):
        token = m.group(1)
        if token in ("/", "~", "~/", "$HOME", "$HOME/"):
            return False, f"path token {token!r} is outside the workspace"
        expanded = os.path.expanduser(token.replace("$HOME", home))
        if expanded.startswith(SYSTEM_PATH_PREFIXES):
            continue
        if not _within_roots(expanded, roots):
            return False, f"path {token!r} is outside the workspace"
    base = Path(os.path.expanduser(cwd)).resolve() if cwd else (roots[0] if roots else Path.cwd())
    for m in DOTDOT_TOKEN_RE.finditer(command):
        candidate = (base / m.group(1)).resolve()
        if not _within_roots(str(candidate), roots):
            return False, f"relative path {m.group(1)!r} escapes the workspace"
    return True, ""


def _handle_codex_exec_command(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any], cmd: str) -> Dict[str, Any]:
    matches = re.findall(r"""(?:"([^"]*)"|'([^']*)')""", cmd)
    prompt = ""
    for m in reversed(matches):
        candidate = (m[0] or m[1]).strip()
        if candidate and len(candidate) > 2 and not candidate.startswith("-"):
            prompt = candidate
            break
    if not prompt:
        cleaned = re.sub(r"\bcodex\s+exec\b", "", cmd)
        cleaned = re.sub(r"-(?:m|c|i|s|p|C|-model|-config|-sandbox|-cd)\s+\S+", "", cleaned)
        cleaned = re.sub(r"--\S+", "", cleaned)
        prompt = cleaned.strip() or cmd

    item = {"Prompt": prompt, "Role": "codex-subagent"}
    tier, type_override, reason = classify_subagent(item)
    target_model = DEFAULT_CODEX_SCOUT_MODEL if tier == "flash_lite" else DEFAULT_CODEX_WORKER_MODEL
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)

    if re.search(r"(?:-m|--model)\s+(\S+)", cmd):
        new_cmd = re.sub(r"(-m|--model)\s+\S+", f"-m {target_model}", cmd, count=1)
    else:
        new_cmd = re.sub(r"\bcodex\s+exec\b", f"codex exec -m {target_model}", cmd, count=1)

    active_branch = "--worktree" in cmd
    spawns = [{
        "role": target_type,
        "type": target_type,
        "model": target_model,
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
    role = str(args.get("role") or args.get("type") or args.get("name") or "").strip()
    workspace = str(args.get("workspace") or args.get("isolation") or "inherit").strip().lower()
    if workspace == "worktree":
        workspace = "branch"
    item = {
        "Prompt": prompt,
        "Role": role,
        "Workspace": workspace,
        "TypeName": role,
    }
    tier, type_override, reason = classify_subagent(item)
    target_model = DEFAULT_CODEX_SCOUT_MODEL if tier == "flash_lite" else DEFAULT_CODEX_WORKER_MODEL
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)

    updated_args = copy.deepcopy(args)
    updated_args["model"] = target_model
    if "type" in updated_args:
        updated_args["type"] = target_type

    spawns = [{
        "role": role or target_type,
        "type": target_type,
        "model": target_model,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    return _allow(
        f"Mitosis Orchestrator pinned subagent model: {target_model} ({reason})",
        updated_input=updated_args,
        payload=payload,
    )


def _handle_mitosis_tools(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state(payload)
    if tool_name == "mitosis_scout":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_scout prompt cannot be empty.", payload)
        mutation = MUTATION_RE.search(_strip_negated_mutations(prompt))
        if mutation:
            target_model = DEFAULT_CODEX_WORKER_MODEL
            target_type = WORKER_TYPE
            reason = f"mutation verb '{mutation.group(0)}' upgraded scout to worker"
        else:
            target_model = DEFAULT_CODEX_SCOUT_MODEL
            target_type = SCOUT_TYPE
            reason = "read-only scout"
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        state.setdefault("pending_spawns", []).append({
            "role": str(args.get("role") or target_type),
            "type": target_type,
            "model": target_model,
            "workspace": "inherit",
            "active_branch": False,
        })
        save_state(payload, state)
        return _allow(f"Mitosis Orchestrator pinned scout: {target_model} ({reason})", updated_input=updated_args, payload=payload)

    if tool_name == "mitosis_worker":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_worker prompt cannot be empty.", payload)
        target_model = DEFAULT_CODEX_WORKER_MODEL
        workspace = str(args.get("workspace") or "inherit").strip().lower()
        active_branch = workspace in ("branch", "worktree")
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        state.setdefault("pending_spawns", []).append({
            "role": str(args.get("role") or WORKER_TYPE),
            "type": WORKER_TYPE,
            "model": target_model,
            "workspace": "branch" if active_branch else workspace,
            "active_branch": active_branch,
        })
        save_state(payload, state)
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
                p = str(t.get("prompt") or "").strip()
                tier, _, reason = classify_subagent(t)
                model = DEFAULT_CODEX_SCOUT_MODEL if tier == "flash_lite" else DEFAULT_CODEX_WORKER_MODEL
                t["model"] = model
                ws = str(t.get("workspace") or "inherit").strip().lower()
                spawns.append({
                    "role": str(t.get("role") or tier),
                    "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                    "model": model,
                    "workspace": ws,
                    "active_branch": ws in ("branch", "worktree"),
                })
            state.setdefault("pending_spawns", []).extend(spawns)
            save_state(payload, state)
            updated_args = copy.deepcopy(args)
            updated_args["tasks"] = updated_tasks
            return _allow("Mitosis Orchestrator pinned tasks", updated_input=updated_args, payload=payload)
        prompt = str(args.get("prompt") or "").strip()
        if prompt:
            tier, _, reason = classify_subagent(args)
            model = DEFAULT_CODEX_SCOUT_MODEL if tier == "flash_lite" else DEFAULT_CODEX_WORKER_MODEL
            updated_args = copy.deepcopy(args)
            updated_args["model"] = model
            state.setdefault("pending_spawns", []).append({
                "role": str(args.get("role") or tier),
                "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                "model": model,
                "workspace": "inherit",
                "active_branch": False,
            })
            save_state(payload, state)
            return _allow(f"Mitosis Orchestrator pinned subagent: {model} ({reason})", updated_input=updated_args, payload=payload)
        return _deny("mitosis_invoke_subagent requires tasks or prompt.", payload)

    return _allow("Mitosis Orchestrator: pass-through.", payload=payload)


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


def _handle_workspace_guard(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Opt-in: the plugin ships with the scope guard off. Set AGY_MITOSIS_AUTO_ALLOW=1 (or CLAUDE_MITOSIS_AUTO_ALLOW=1 / CODEX_MITOSIS_AUTO_ALLOW=1)
    # to let project-scoped file and command tools skip the permission prompt.
    # When unset or for tools without path arguments, return an explicit "allow" pass-through.
    if not _env_flag("CODEX_MITOSIS_AUTO_ALLOW", "CLAUDE_MITOSIS_AUTO_ALLOW", "AGY_MITOSIS_AUTO_ALLOW", "AGY_ARGON_AUTO_ALLOW"):
        return _allow("Mitosis Orchestrator: pass-through.", payload=payload)
    roots = allowed_roots(payload)
    if not roots:
        return _allow("Mitosis Orchestrator: pass-through.", payload=payload)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cwd = str(_get(args, "cwd", "Cwd", default="") or payload.get("cwd") or "")
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "")
        if not cmd:
            return _allow("Mitosis Orchestrator: pass-through.", payload=payload)
        if cwd and not _within_roots(cwd, roots):
            return _ask("Mitosis Orchestrator: Cwd is outside the workspace.", payload=payload)
        ok, why = command_stays_in_workspace(cmd, cwd, roots)
        if ok:
            return _allow("Mitosis Orchestrator: command stays inside the workspace.", overrides=[f"command({cmd})"], payload=payload)
        return _ask(f"Mitosis Orchestrator: {why}.", payload=payload)

    if tool_name == "apply_patch":
        targets = _extract_apply_patch_files(args)
        if not targets:
            return _allow("Mitosis Orchestrator: pass-through.", payload=payload)
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
        return _allow("Mitosis Orchestrator: pass-through.", payload=payload)
    outside = [t for t in targets if not _within_roots(t, roots)]
    if outside:
        return _ask(f"Mitosis Orchestrator: {outside[0]!r} is outside the workspace.", payload=payload)
    # "allow" alone does not satisfy Antigravity's permission system (headless
    # runs still auto-deny), so grant the matching read_file/write_file rule
    # for exactly these in-workspace paths.
    action = "write_file" if (WRITE_TOOL_RE.search(tool_name) or tool_name in MUTATION_TOOLS) else "read_file"
    return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"{action}({t})" for t in targets], payload=payload)


def handle_pre_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, args = _extract_tool_call(payload)
    if not tool_name:
        return _allow("Mitosis Orchestrator: pass-through.", payload=payload)

    state = load_state(payload)

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
    if tool_name in ("send_message", "SendMessage"):
        return _handle_send_message(payload, args)
    if tool_name == "run_workflow":
        return _handle_run_workflow(args)
    if tool_name == "define_subagent":
        return _handle_define_subagent(args)
    if tool_name == "manage_subagents":
        return _handle_manage_subagents(payload, args)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
        if re.search(r"\bcodex\s+exec\b", cmd):
            return _handle_codex_exec_command(payload, tool_name, args, cmd)

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


# ---------------------------------------------------------------------------
# post-tool-use / stop
# ---------------------------------------------------------------------------

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


def handle_post_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, _ = _extract_tool_call(payload)
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
        else:
            if pending:
                pending.pop()
    else:
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


REPORT_BLOCK_REASON = (
    "[Mitosis Orchestrator] Your final message must be the bounded four-header report ({error}). "
    "Reply again with exactly these lines and nothing else (no preamble, no code fences, at most 1,600 characters):\n"
    "STATUS: COMPLETED | BLOCKED | FAILED\n"
    "FILES_CHANGED: <comma-separated paths or NONE>\n"
    "VERIFICATION: <one sentence stating the command run and its exit result>\n"
    "NOTES: <one sentence or up to 10 '- ' bullets; include a file://.../*.log URI when BLOCKED or FAILED>"
)


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
    save_state(payload, state)

    if payload.get("stop_hook_active") is True:
        return {}
    if _get_env("CLAUDE_MITOSIS_ENFORCE_REPORT", default="1") == "0":
        return {}
    type_name = agent_type or (str(record.get("type") or "") if record else "")
    if not _is_plugin_agent_type(type_name):
        return {}
    error = validate_subagent_report(str(payload.get("last_assistant_message") or ""))
    if error is None:
        return {}
    nagged: List[str] = state.setdefault("report_nagged", [])
    key = agent_id or type_name
    if key in nagged:
        return {}
    nagged.append(key)
    save_state(payload, state)
    return {"decision": "block", "reason": REPORT_BLOCK_REASON.format(error=error)}


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


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _debug_log(payload: Dict[str, Any], mode: str, result: Dict[str, Any]) -> None:
    """With AGY_MITOSIS_DEBUG=1 / CLAUDE_MITOSIS_DEBUG=1 (or legacy AGY_ARGON_DEBUG=1), append one JSON line per hook call to the artifact directory."""
    if not _env_flag("CLAUDE_MITOSIS_DEBUG", "AGY_MITOSIS_DEBUG", "AGY_ARGON_DEBUG"):
        return
    path = _state_file(payload)
    if not path:
        return
    tool_name, _ = _extract_tool_call(payload)
    entry = {
        "mode": mode,
        "tool": tool_name,
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
        with open(path.with_name("mitosis_orchestrator_debug.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
    mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("{") else None
    try:
        raw = sys.stdin.read()
        if not raw.strip() and len(sys.argv) > 1 and sys.argv[-1].startswith("{"):
            raw = sys.argv[-1]
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

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
        result = _allow("Mitosis Orchestrator: pass-through.", payload=payload) if mode in ("stop", "pre-tool-use") else {}
        sys.stderr.write(f"[mitosis-orchestrator] {mode} hook error: {exc!r}\n")
    _debug_log(payload, mode, result)

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
