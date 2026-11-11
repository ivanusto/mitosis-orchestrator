#!/usr/bin/env python3
"""Turn a raw hook payload capture into a fixture that is safe to commit.

    sanitize-payloads.py IN OUT --platform P --cli-version V --scenario S [--provenance TEXT]

IN is the payloads.jsonl written by scripts/mitosis_hook/capture.py (MITOSIS_CAPTURE_PAYLOADS):
one {"mode", "payload", "result"} object per line. A bare payload per line is accepted too, and its
mode is read from hook_event_name. Top-level keys other than those three are dropped.

The sanitizer is an allowlist. Structure survives; a key name survives only when it has a plain
identifier shape (letters and underscores, at most 40 characters, no digit run, no hyphen or dot) or is a
known key, and never inside a free-form subtree (tool_response, env, headers, and the args or tool_input of
a tool that is not on the closed tool list), where every key outside a small allowlist becomes "__KEY<n>__".
A value survives only when its key has a rule below and the value passes it: a closed enum per key, a
model-name token of a known family, a tool name from a closed list or a plain identifier, a small int, a
boolean, a canned probe command. Everything else becomes a typed placeholder, so a key that is not
listed, including one a future CLI adds, can never carry a value out:

    string  -> "__STR__" (text keys: "__TEXT__", commands: "__CMD__"); "" stays "" (emptiness is a signal)
    int     -> kept when small, else 0        float -> 0.0        bool and null -> kept

Ids become "__ID1__", "__ID2__" ... (stable inside one file). Paths become "__WS__", "__HOME__",
"__ART__" or "__TRANSCRIPT__", optionally followed by a short allowlisted file name. Codex opaque
messages become FAKE_OPAQUE, which still matches OPAQUE_MESSAGE_RE. The first output line is a
{"_meta": ...} object without date or hostname.

A capture holds prompts, paths and tool output. Never commit or share the raw file.
Zero dependencies; Python 3.8+.
"""

import argparse
import getpass
import json
import os
import re
import socket
import sys
from typing import Any, Dict, List, Optional

SANITIZER_VERSION = 2

# Same pattern as scripts/mitosis_hook/platforms/codex.py OPAQUE_MESSAGE_RE (test_fixture_privacy keeps them equal).
OPAQUE_MESSAGE_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]{20,}={0,2}$")
FAKE_OPAQUE = "gAAAAAFAKEOPAQUETOKENFAKEOPAQUETOKEN"

KNOWN_MODES = (
    "session-start", "pre-invocation", "user-prompt-submit", "pre-tool-use", "post-tool-use",
    "post-tool-use-failure", "stop", "subagent-stop", "codex-notify",
)
EVENT_TO_MODE = {
    "SessionStart": "session-start", "PreInvocation": "pre-invocation", "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use", "PostToolUse": "post-tool-use", "PostToolUseFailure": "post-tool-use-failure",
    "Stop": "stop", "SubagentStop": "subagent-stop",
}

ID_KEYS = frozenset((
    "session_id", "conversationId", "turn_id", "thread_id", "thread-id", "tool_use_id", "agent_id", "agentId",
    "prompt_id", "id", "call_id", "turn-id", "task_name", "harnessSectionHash",
))
PATH_KEYS = frozenset((
    "cwd", "workspacePaths", "artifactDirectoryPath", "transcriptPath", "transcript_path", "agent_transcript_path",
    "Path", "path", "file_path", "notebook_path", "TargetFile", "AbsolutePath", "FilePath", "File", "DirectoryPath",
    "SearchPath", "workdir", "workingDirectory", "CWD", "Cwd",
))
TRANSCRIPT_KEYS = frozenset(("transcriptPath", "transcript_path", "agent_transcript_path"))
COMMAND_KEYS = frozenset(("command", "CommandLine", "commandLine", "cmd"))
TEXT_KEYS = frozenset((
    "lastUserInput", "prompt", "Prompt", "message", "last_assistant_message", "Content", "content", "text", "reason",
    "permissionDecisionReason", "systemMessage", "additionalContext", "toolSummary", "description", "error",
    "tool_response", "output", "stdout", "stderr", "old_string", "new_string", "old_str", "new_str", "pattern", "query",
    "url", "ephemeralMessage", "stopReason", "input-messages", "input_messages", "last-assistant-message",
))
# Exit-code header of a failure text: it carries the signal and no content. At most 3 digits.
EXIT_PREFIX_RE = re.compile(r"^(Exit code \d{1,3}(?!\d)|[Cc]ommand exited with code \d{1,3}(?!\d)\.?|[Pp]rocess exited with code \d{1,3}(?!\d)|exit status \d{1,3}(?!\d))")
JSON_TEXT_KEYS = frozenset(("tool_response", "output", "result"))

# ----- per-key value rules (the privacy test imports these) -----
CLAUDE_HOOK_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop", "Stop", "Notification", "PreCompact",
)
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "plan", "auto", "dontAsk")
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
TERMINATION_REASONS = (
    "USER_CANCELED", "ERROR", "MAX_INVOCATIONS", "MAX_FORCED_INVOCATIONS", "MAX_TOKEN_BUDGET_EXCEEDED", "NO_TOOL_CALL",
)
PLUGIN_PREFIX = "mitosis-orchestrator:"
AGENT_TYPES = frozenset((
    "explorer", "worker", "default", "flash-lite-scout", "flash-worker", "explore", "plan", "general-purpose", "claude",
    "claude-code-guide", "statusline-setup", "output-style-setup", "bash", "fork", "web-fetch",
))
TOOL_NAMES = frozenset((
    "run_command", "Bash", "shell", "exec_command", "local_shell", "apply_patch", "invoke_subagent", "Task", "Agent",
    "spawn_agent", "wait_agent", "close_agent", "resume_agent", "interrupt_agent", "send_message", "SendMessage",
    "followup_task", "run_workflow", "define_subagent", "manage_subagents", "SubagentHandback",
    "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker", "write_file", "read_file", "write_to_file",
    "replace_file_content", "multi_replace_file_content", "view_file", "view_file_outline", "view_code_item",
    "edit_file", "create_file", "list_dir", "grep_search", "find_by_name", "Read", "Write", "Edit", "MultiEdit",
    "NotebookEdit", "NotebookRead", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "read",
    "collaborationspawn_agent", "collaborationwait_agent", "collaborationclose_agent", "collaborationresume_agent",
    "collaborationinterrupt_agent", "collaborationsend_message", "collaborationfollowup_task",
))
MCP_TOOL = "__MCP_TOOL__"
ROLE_CANNED = frozenset(("Reconnaissance Scout",))
# Plain identifier: letters and underscores (camelCase or snake_case), no digit run, no hyphen or dot.
IDENT_RE = re.compile(r"^(?!.*[0-9]{2})[A-Za-z_][A-Za-z0-9_]{0,39}$")
MODEL_FAMILY = frozenset((
    "gemini", "claude", "gpt", "chatgpt", "o1", "o3", "o4", "haiku", "sonnet", "opus", "fable", "mythos", "flash", "lite",
    "pro", "ultra", "argon", "carbon", "codex", "gemma", "llama", "qwen", "qwq", "deepseek", "mistral", "grok", "best", "inherit",
    "sol", "terra", "luna", "spark", "mini", "nano",
))
MODEL_WORDS = MODEL_FAMILY | frozenset((
    "preview", "latest", "high", "low", "medium", "minimal", "max", "xhigh", "thinking", "instruct", "chat", "exp",
    "experimental", "turbo", "image", "audio", "live", "tts",
))
_MODEL_NUM_RE = re.compile(r"^[A-Za-z]?[0-9]{1,8}[A-Za-z]{0,2}$")
_MODEL_DATE_RE = re.compile(r"^20[0-9]{6}$")


def _model_number_ok(segment: str) -> bool:
    return bool(_MODEL_NUM_RE.match(segment))


def model_ok(value: str) -> bool:
    """A model name of a known family: 'gpt-5.6-terra', 'haiku', or the agy display form 'Gemini 4 Argon (High)'."""
    text = re.sub(r"\[[0-9]+[mk]\]$", "", value)
    if not text or len(text) > 60:
        return False
    if " " in text:
        words = text.split(" ")
        if len(words) > 6 or words[0].lower() not in ("gemini", "claude", "gpt"):
            return False
        for word in words[1:]:
            inner = word[1:-1] if word.startswith("(") and word.endswith(")") else word
            if inner.lower() not in MODEL_WORDS and not re.match(r"^[0-9]{1,2}(\.[0-9]{1,2})?[A-Za-z]?$", inner):
                return False
        return True
    segments = [x for x in re.split(r"[-_.:/]", text) if x]
    if not segments or len(segments) > 12 or len(text) != len(text.strip()) or segments[0].lower() not in MODEL_FAMILY:
        return False
    digits = 0
    for seg in segments[1:]:
        if seg.lower() in MODEL_WORDS or _MODEL_DATE_RE.match(seg):
            continue
        if not _model_number_ok(seg):
            return False
        digits += sum(c.isdigit() for c in seg)
    return digits <= 5


def tool_name_ok(value: str) -> bool:
    return value in TOOL_NAMES or bool(IDENT_RE.match(value))


def agent_type_ok(value: str) -> bool:
    if value == "":
        return True  # Claude Code's internal fork stop carries an empty agent_type.
    bare = value[len(PLUGIN_PREFIX):] if value.startswith(PLUGIN_PREFIX) else value
    return bare.lower() in AGENT_TYPES


def _enum(*values: str) -> Any:
    allowed = frozenset(values)
    return lambda text: text in allowed


# key -> predicate. A string under one of these keys survives only when its predicate holds.
VALUE_RULES = {
    "status": _enum("completed", "running", "failed", "pending", "error", "cancelled", "shutdown", "timeout", "success"),
    "source": _enum("startup", "resume", "clear", "compact"),
    "type": _enum(
        "text", "message", "tool_use", "tool_result", "image", "thinking", "output_text", "input_text", "function_call",
        "function_call_output", "reasoning",
    ),
    "decision": _enum("allow", "block", "deny", "approve", "ask"),
    "permissionDecision": _enum("allow", "deny", "ask"),
    "hook_event_name": _enum(*CLAUDE_HOOK_EVENTS),
    "hookEventName": _enum(*CLAUDE_HOOK_EVENTS),
    "permission_mode": _enum(*PERMISSION_MODES),
    "mode": _enum(*(PERMISSION_MODES + (
        "session-start", "pre-invocation", "user-prompt-submit", "pre-tool-use", "post-tool-use", "post-tool-use-failure",
        "stop", "subagent-stop", "codex-notify",
    ))),
    "terminationReason": _enum(*TERMINATION_REASONS),
    "level": _enum(*EFFORT_LEVELS),
    "effort": _enum(*EFFORT_LEVELS),
    "fork_turns": _enum("all", "none"),
    "role": _enum("user", "assistant", "system", "tool", "developer"),
    "Model": _enum("flash", "flash_lite"),
    "TypeName": _enum("flash-lite-scout", "flash-worker"),
    "typeName": _enum("flash-lite-scout", "flash-worker"),
    "Role": lambda text: text in ROLE_CANNED,
    "agent_type": agent_type_ok,
    "agentType": agent_type_ok,
    "subagent_type": agent_type_ok,
    "model": model_ok,
    "modelName": model_ok,
    "resolvedModel": model_ok,
    "name": tool_name_ok,
    "tool_name": tool_name_ok,
    "tool": tool_name_ok,
    "toolName": tool_name_ok,
}
NAME_KEYS = frozenset(VALUE_RULES)


def rule_allows(key: str, text: str) -> Optional[bool]:
    """None when the key has no value rule; else whether the text survives the rule (placeholders do)."""
    rule = VALUE_RULES.get(key)
    if rule is None:
        return None
    if text == "__STR__" or text == MCP_TOOL:
        return True
    if text.startswith("mcp__"):
        return False
    return bool(rule(text))


# Key names that stay visible inside a free-form subtree (tool_response, env, headers, args of an unknown tool).
FREE_KEYS = frozenset((
    "agentId", "agentType", "canContinueAgent", "content", "harnessNoteCount", "harnessSectionHash", "harnessTailCount",
    "interrupted", "isImage", "noOutputExpected", "prompt", "resolvedModel", "status", "stderr", "stdout", "toolStats",
    "toolsUsed", "totalDurationMs", "totalTokens", "totalToolUseCount", "usage", "text", "type", "bashCount",
    "editFileCount", "linesAdded", "linesRemoved", "otherToolCount", "readCount", "searchCount", "cache_creation",
    "ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "input_tokens", "output_tokens", "iterations", "output_tokens_details", "thinking_tokens", "server_tool_use",
    "web_fetch_requests", "web_search_requests", "fallback_credit", "inference_geo", "service_tier", "speed",
    "message", "timed_out", "task_name", "command", "description", "effort", "model", "subagent_type", "agent_type",
    "fork_turns", "run_in_background", "timeout_ms", "target", "decision", "reason", "level", "source", "name",
    "Model", "Role", "TypeName", "Prompt", "CommandLine", "WaitMsBeforeAsync", "toolSummary", "Path", "Content",
))
FREE_PARENTS = frozenset(("tool_response", "env", "headers"))
# Kept apart: 'tool_input' and 'args' are free-form only when the tool next to them is not on TOOL_NAMES.
UNKNOWN_TOOL_PARENTS = frozenset(("tool_input", "args"))


def child_is_free(key: str, node: Dict[str, Any], parent_key: str, free: bool) -> bool:
    """Whether the value under `key` of dict `node` is a free-form subtree."""
    if free or key in FREE_PARENTS:
        return True
    if key in UNKNOWN_TOOL_PARENTS:
        tool = node.get("name") if parent_key == "toolCall" else node.get("tool_name")
        return not (isinstance(tool, str) and tool in TOOL_NAMES)
    return False


PLAIN_KEY_RE = IDENT_RE
KEY_PLACEHOLDER_RE = re.compile(r"^__KEY[0-9]+__$")
INT_KEYS = frozenset((
    "invocationNum", "initialNumSteps", "stepIdx", "executionNum", "timeout_ms", "WaitMsBeforeAsync", "duration_ms",
    "yield_time_ms", "max_output_tokens", "fork_turns", "turn", "turnNumber", "step_index",
))
SMALL_INT = 1000
INT_MAX = 10 ** 7
KNOWN_KEYS = ID_KEYS | PATH_KEYS | COMMAND_KEYS | TEXT_KEYS | NAME_KEYS | INT_KEYS | FREE_KEYS

# Canned probe commands. Anything else a CLI ran becomes __CMD__.
CANNED_COMMANDS = frozenset((
    "ls", "ls -la", "ls /nonexistent-a", "ls /nonexistent-b", "ls /nonexistent-c", "ls nonexistent-a",
    "grep -c zzz a.txt", "wc -l a.txt", "cat a.txt", "pwd", "true", "false", "pytest", "pytest -q", "echo hello",
    "rm -f /tmp/mitosis-p0-does-not-exist-xyz",
))
# File names that may follow a placeholder. Any other name becomes __NAME__.
SAFE_FILE_NAMES = frozenset((
    "a.txt", "b.txt", "c.txt", "f.txt", "x.txt", "hello.txt", "app.py", "main.py", "a.py", "b.py", "README.md", "src",
))
SYSTEM_PATHS = frozenset(("/", "/tmp", "/dev/null", "/nonexistent-a", "/nonexistent-b", "/nonexistent-c"))

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"gAAAA[A-Za-z0-9_-]{16,}")
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,}|Bearer\s+\S+|xox[a-z]-[A-Za-z0-9-]{4,}|github_pat_[A-Za-z0-9_]{6,}|AIza[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{6,}\.")
HOME_PATH_RE = re.compile(r"^(/home/[^/]+|/Users/[^/]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+)")
PLACEHOLDER_RE = re.compile(r"__[A-Z]+(?:_[A-Z]+)*[0-9]*__")


def _local_names() -> List[str]:
    names = []  # type: List[str]
    for raw in (os.environ.get("USER"), os.environ.get("USERNAME"), os.environ.get("LOGNAME")):
        names.append(raw or "")
    try:
        names.append(getpass.getuser())
    except Exception:
        pass
    try:
        host = socket.gethostname()
        names.extend([host, host.split(".")[0]])
    except Exception:
        pass
    return sorted({n.lower() for n in names if n and len(n) >= 3})


class Sanitizer:
    def __init__(self, roots: Optional[List[str]] = None, artifacts: Optional[List[str]] = None, transcripts: Optional[List[str]] = None):
        self.ids = {}  # type: Dict[str, str]
        self.key_names = {}  # type: Dict[str, str]
        self.roots = sorted({_norm(r) for r in (roots or []) if r}, key=len, reverse=True)
        self.artifacts = sorted({_norm(r) for r in (artifacts or []) if r}, key=len, reverse=True)
        self.transcripts = {_norm(t) for t in (transcripts or []) if t}
        self.home = _norm(os.path.expanduser("~"))
        self.local_names = _local_names()

    # ----- helpers -----
    def _id(self, value: str) -> str:
        if value not in self.ids:
            self.ids[value] = "__ID%d__" % (len(self.ids) + 1)
        return self.ids[value]

    def _bad(self, text: str) -> bool:
        """True when a string we are about to emit still looks private."""
        if PLACEHOLDER_RE.fullmatch(text) or text == FAKE_OPAQUE:
            return False
        low = text.lower()
        if "/home/" in low or "/users/" in low or "c:\\" in low or "c:/" in low or SECRET_RE.search(text):
            return True
        if EMAIL_RE.search(text) or UUID_RE.search(text) or TOKEN_RE.search(text):
            return True
        for name in self.local_names:
            # A short name ("run", "dev") must stand alone, or it would match inside ordinary words.
            pattern = re.escape(name) if len(name) >= 6 else r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(name)
            if re.search(pattern, low):
                return True
        return False

    def _emit(self, text: str, placeholder: str) -> str:
        return placeholder if self._bad(text) else text

    def key(self, name: str, free: bool = False) -> str:
        """A key keeps its name when it is a plain identifier (or a known key) outside a free-form subtree."""
        if free:
            visible = name in FREE_KEYS
        else:
            visible = (name in KNOWN_KEYS or bool(PLAIN_KEY_RE.match(name))) and not self._bad(name)
        if visible:
            return name
        if name not in self.key_names:
            self.key_names[name] = "__KEY%d__" % (len(self.key_names) + 1)
        return self.key_names[name]

    def path(self, value: str, key: str) -> str:
        if not value:
            return ""
        norm = _norm(value)
        if key in TRANSCRIPT_KEYS or norm in self.transcripts:
            return "__TRANSCRIPT__"
        for base, tag in [(a, "__ART__") for a in self.artifacts] + [(r, "__WS__") for r in self.roots]:
            if norm == base:
                return tag
            if norm.startswith(base + "/"):
                return tag + _tail(norm[len(base) + 1:])
        home = HOME_PATH_RE.match(value)
        if (self.home and norm == self.home) or (self.home and norm.startswith(self.home + "/")) or home:
            rest = norm[len(self.home) + 1:] if self.home and norm.startswith(self.home + "/") else ""
            return "__HOME__" + _tail(rest)
        if value in SYSTEM_PATHS:
            return value
        if not (value.startswith("/") or HOME_PATH_RE.match(value) or re.match(r"^[A-Za-z]:[\\/]", value)):
            return _tail(norm).lstrip("/") or "__PATH__"  # relative path: filtered file names only
        return "__PATH__"

    def command(self, value: str) -> str:
        text = value.strip()
        if text.startswith("*** Begin Patch"):
            return self.patch(value)
        return text if text in CANNED_COMMANDS else "__CMD__"

    def patch(self, value: str) -> str:
        """Keep the structure of an apply_patch body; every file name is filtered and every body line becomes x."""
        out = []  # type: List[str]
        for line in value.split("\n"):
            head = re.match(r"^(\*\*\* (?:Add File|Update File|Delete File|Move to): )(.*)$", line)
            if head:
                name = os.path.basename(head.group(2).strip().replace("\\", "/"))
                out.append(head.group(1) + (name if name in SAFE_FILE_NAMES else "__NAME__"))
            elif line.startswith("***") or line.startswith("@@"):
                out.append(line if line in ("*** Begin Patch", "*** End Patch", "*** End of File", "@@") else "@@")
            elif line[:1] in ("+", "-", " "):
                out.append(line[0] + "x")
            else:
                out.append("x" if line else "")
        return "\n".join(out)

    def text(self, value: str, key: str) -> str:
        if not value:
            return ""
        if OPAQUE_MESSAGE_RE.match(value.strip()):
            return FAKE_OPAQUE
        if key in ("error", "tool_response", "output", "stdout", "stderr"):
            match = EXIT_PREFIX_RE.match(value)
            if match:
                return match.group(1) + ("\n__TEXT__" if len(value) > match.end() else "")
        return "__TEXT__"

    # ----- the walk -----
    def value(self, value: Any, key: str, parent: str, free: bool = False, strict: bool = False) -> Any:
        """free: inside a free-form subtree (keys outside FREE_KEYS are renamed). strict: tool_response or hook result."""
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            if key in INT_KEYS:
                return value if abs(value) <= INT_MAX else 0
            if strict and key not in FREE_KEYS:
                return 0
            return value if abs(value) <= SMALL_INT else 0
        if isinstance(value, float):
            return 0.0
        if isinstance(value, dict):
            out = {}  # type: Dict[str, Any]
            for k, v in value.items():
                name = str(k)
                child_free = child_is_free(name, value, key, free)
                shown = self.key(name, free)
                out[shown] = self.value(v, shown, key, child_free, strict or name == "tool_response")
            return out
        if isinstance(value, list):
            return [self.value(v, key, parent, free, strict) for v in value]
        if not isinstance(value, str):
            return "__STR__"
        return self.string(value, key, parent, strict)

    def string(self, value: str, key: str, parent: str, strict: bool = False) -> str:
        if value == "":
            return ""
        rule = VALUE_RULES.get(key)
        if rule is not None:
            if value.startswith("mcp__"):
                return MCP_TOOL
            return value if rule(value) and not self._bad(value) else "__STR__"
        if OPAQUE_MESSAGE_RE.match(value.strip()):
            return FAKE_OPAQUE
        if key in ID_KEYS:
            return self._id(value)
        if key in PATH_KEYS:
            return self.path(value, key)
        if key in COMMAND_KEYS:
            return self.command(value)
        if key in JSON_TEXT_KEYS and value.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                free = key == "tool_response"
                return json.dumps(self.value(parsed, key, key, free, True), separators=(",", ":"), sort_keys=True)
        if key in TEXT_KEYS:
            return self.text(value, key)
        return "__STR__"


def _norm(path: str) -> str:
    text = str(path).replace("\\", "/")
    return text.rstrip("/") if len(text) > 1 else text


def _tail(rest: str) -> str:
    """'/' plus the path components after a known base, each kept only when it is an allowlisted name."""
    parts = [p for p in rest.split("/") if p and p != "."]
    if not parts:
        return ""
    return "/" + "/".join(p if p in SAFE_FILE_NAMES else "__NAME__" for p in parts)


def _collect(lines: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Directories and files the capture itself names, so they can be mapped to placeholders."""
    found = {"roots": [], "artifacts": [], "transcripts": []}  # type: Dict[str, List[str]]
    for line in lines:
        for source in (line.get("payload"), line.get("result")):
            if not isinstance(source, dict):
                continue
            for item in source.get("workspacePaths") or []:
                if isinstance(item, str):
                    found["roots"].append(item)
            for name in ("cwd", "workdir"):
                if isinstance(source.get(name), str):
                    found["roots"].append(source[name])
            if isinstance(source.get("artifactDirectoryPath"), str):
                found["artifacts"].append(source["artifactDirectoryPath"])
            for name in TRANSCRIPT_KEYS:
                if isinstance(source.get(name), str):
                    found["transcripts"].append(source[name])
    return found


def sanitize_lines(lines: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The list of output objects: the _meta line first, then one object per input line."""
    found = _collect(lines)
    san = Sanitizer(found["roots"], found["artifacts"], found["transcripts"])
    out = [{"_meta": meta}]  # type: List[Dict[str, Any]]
    for line in lines:
        payload = line.get("payload") if "payload" in line else {k: v for k, v in line.items() if k not in ("mode", "result")}
        mode = line.get("mode")
        if not isinstance(mode, str):
            event = payload.get("hook_event_name") if isinstance(payload, dict) else None
            mode = EVENT_TO_MODE.get(event, "pre-tool-use") if isinstance(event, str) else "pre-tool-use"
        out.append({
            "mode": mode if mode in KNOWN_MODES else "__MODE__",
            "payload": san.value(payload, "payload", ""),
            "result": san.value(line.get("result"), "result", "", False, True),
        })
    return out


def sanitize_text(raw: str, meta: Dict[str, Any]) -> str:
    lines = []  # type: List[Dict[str, Any]]
    for number, text in enumerate(raw.splitlines(), 1):
        if not text.strip():
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            raise SystemExit("input line %d is not JSON" % number)
        if not isinstance(obj, dict):
            raise SystemExit("input line %d is not an object" % number)
        lines.append(obj)
    return "".join(json.dumps(o, sort_keys=True, ensure_ascii=True) + "\n" for o in sanitize_lines(lines, meta))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sanitize a raw hook payload capture into a committable fixture.")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--cli-version", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--provenance", default="")
    args = parser.parse_args(argv)
    meta = {"platform": args.platform, "cli_version": args.cli_version, "scenario": args.scenario, "sanitizer_version": SANITIZER_VERSION}
    if args.provenance:
        meta["provenance"] = args.provenance
    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        sys.stderr.write("cannot read %s: %s\n" % (args.input, exc))
        return 2
    text = sanitize_text(raw, meta)
    parent = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(parent, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
