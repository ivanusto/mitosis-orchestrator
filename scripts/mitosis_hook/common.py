"""Shared constants, platform detection and hook-result helpers."""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

def _load_sibling(name: str) -> Any:
    """Load scripts/<name>.py by file path (scripts/ is not on sys.path), cached in sys.modules."""
    import importlib.util
    key = "_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().parent.parent / (name + ".py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


_GEN = _load_sibling("mitosis_generated")


DEFAULT_PRIMARY_MODEL = _GEN.DEFAULT_PRIMARY_MODEL


DEFAULT_CODEX_PRIMARY_MODEL = _GEN.DEFAULT_CODEX_PRIMARY_MODEL


DEFAULT_CODEX_WORKER_MODEL = _GEN.DEFAULT_CODEX_WORKER_MODEL


DEFAULT_CODEX_SCOUT_MODEL = _GEN.DEFAULT_CODEX_SCOUT_MODEL


DEFAULT_CODEX_SCOUT_EFFORT = _GEN.DEFAULT_CODEX_SCOUT_EFFORT


DEFAULT_CODEX_WORKER_EFFORT = _GEN.DEFAULT_CODEX_WORKER_EFFORT


DEFAULT_CLAUDE_PRIMARY_MODEL = _GEN.DEFAULT_CLAUDE_PRIMARY_MODEL


DEFAULT_CLAUDE_WORKER_MODEL = _GEN.DEFAULT_CLAUDE_WORKER_MODEL


DEFAULT_CLAUDE_SCOUT_MODEL = _GEN.DEFAULT_CLAUDE_SCOUT_MODEL


DEFAULT_CLAUDE_SCOUT_EFFORT = _GEN.DEFAULT_CLAUDE_SCOUT_EFFORT


DEFAULT_CLAUDE_WORKER_EFFORT = _GEN.DEFAULT_CLAUDE_WORKER_EFFORT


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


CODEX_SCOUT_ROLE = "explorer"


CODEX_WORKER_ROLE = "worker"


WORKER_TYPE = "flash-worker"


STATE_FILENAME = "mitosis_orchestrator_state.json"


def is_codex_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches OpenAI Codex CLI hook or MCP input schemas."""
    if not isinstance(payload, dict):
        return False
    if payload.get("client") == "codex" or payload.get("platform") == "codex":
        return True
    # Codex >= 0.128 plugin hooks speak Claude Code's protocol (same event names
    # and keys) but add turn_id, which Claude Code never sends.
    if payload.get("turn_id") is not None:
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


def is_codex_hook_payload(payload: Dict[str, Any]) -> bool:
    """Codex plugin hook (hooks/codex-hooks.json): Claude Code's protocol, Codex's tools."""
    return is_codex_payload(payload) and str(payload.get("hook_event_name") or "") in CLAUDE_HOOK_EVENTS


def is_claude_code_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches Claude Code hook input schemas."""
    if not isinstance(payload, dict):
        return False
    if is_codex_payload(payload):
        return False
    return any(k in payload for k in ("hook_event_name", "session_id", "tool_name", "tool_input"))


SAFE_TERMINATION_REASONS = {
    "USER_CANCELED",
    "ERROR",
    "MAX_INVOCATIONS",
    "MAX_FORCED_INVOCATIONS",
    "MAX_TOKEN_BUDGET_EXCEEDED",
}


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
    if payload and is_codex_hook_payload(payload):
        # Codex applies updatedInput only alongside "allow". A bare allow would
        # skip Codex's own approval, so pass-through stays silent.
        if updated_input is None and not overrides:
            return {}
        out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": reason}
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


def _no_opinion(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pass-through without a permission verdict.

    Claude Code treats permissionDecision "allow" as "skip the permission
    prompt", so a hook with nothing to say must return {} there. Antigravity
    and Codex read a bare "allow" as no objection.
    """
    if payload and is_claude_code_payload(payload):
        return {}
    return _allow("Mitosis Orchestrator: pass-through.", payload=payload)


def _deny(reason: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and (is_claude_code_payload(payload) or is_codex_hook_payload(payload)):
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
        if is_codex_hook_payload(payload):
            return _deny(reason, payload)
        return {
            "decision": "deny",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    return {"decision": "ask", "reason": reason}


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


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-tolerant lookup: _get(item, "Model", "model")."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


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
    model: Optional[str] = None
    platform: str = ""

    def as_log(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "tier": self.tier,
            "type_override": self.type_override,
            "effort": self.effort,
            "confidence": self.confidence,
            "source": self.source,
            "rule": self.rule,
            "reason": self.reason,
        }
        if self.model:
            out["model"] = self.model
        if self.platform:
            out["platform"] = self.platform
        return out
