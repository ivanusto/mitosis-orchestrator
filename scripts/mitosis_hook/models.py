"""Tier/frontier logic and primary-model settings."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .common import (
    ALLOWED_PRIMARY_MODELS,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_PLUGIN_PREFIX,
    CLAUDE_SUBAGENT_MODELS,
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
    SCOUT_TYPE,
    WORKER_TYPE,
    _env_flag,
    _get_env,
    is_claude_code_payload,
    is_codex_payload,
)


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
            return f"`spawn_agent` with `agent_type: \"explorer\"` ({codex_tier_model('flash_lite')})"
        return f"`spawn_agent` with `agent_type: \"worker\"` ({codex_tier_model('flash')})"
    if tier == "flash_lite":
        return f"`{SCOUT_TYPE}` (TypeName='{SCOUT_TYPE}', Model='flash_lite')"
    return f"`{WORKER_TYPE}` (TypeName='{WORKER_TYPE}', Model='flash')"


SMALL_TIER_RE = re.compile(r"(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])", re.IGNORECASE)


# Matched against whole name segments, so "pro" does not match "proxy" and
# "o1" does not match "product-o1x". Family names match as segment prefixes
# ("qwen3", "deepseek-v4").
FRONTIER_SEGMENTS = {
    "argon", "pro", "ultra", "claude", "opus", "sonnet", "fable", "mythos", "grok", "o1", "o3", "o4",
}


FRONTIER_FAMILY_PREFIXES = ("deepseek", "qwen", "qwq")


FRONTIER_GPT_RE = re.compile(r"(?<![a-z0-9])(?:chat)?gpt-?[4-9]")


# Open-weight checkpoints below this size are not frontier, whatever the family.
PARAM_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")


MIN_FRONTIER_PARAMS_B = 30.0


def is_frontier_tier(model_name: str) -> bool:
    """True for Argon / Pro / Ultra / Claude / frontier models; False for Flash, Lite, Mini, Nano, Haiku, Auto or empty."""
    normalized = (model_name or "").strip().lower()
    if not normalized or normalized in {"auto", "none", "inherit"}:
        return False
    if SMALL_TIER_RE.search(normalized):
        return False
    sizes = [float(m.group(1)) for m in PARAM_SIZE_RE.finditer(normalized)]
    if sizes and max(sizes) < MIN_FRONTIER_PARAMS_B:
        return False
    if FRONTIER_GPT_RE.search(normalized):
        return True
    segments = [seg for seg in re.split(r"[^a-z0-9]+", normalized) if seg]
    return any(seg in FRONTIER_SEGMENTS or seg.startswith(FRONTIER_FAMILY_PREFIXES) for seg in segments)


is_mitosis_tier = is_frontier_tier


is_argon_tier = is_frontier_tier


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


CODEX_EFFORT_LEVELS = CLAUDE_EFFORT_LEVELS | {"minimal"}


CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def valid_codex_model(value: Optional[str]) -> bool:
    """True when value is safe to embed in a shell command and a TOML string."""
    return bool(value) and CODEX_MODEL_RE.match(value or "") is not None


# These CODEX_MITOSIS_* names are read straight from os.environ on purpose and have no
# AGY_ form: _get_env expands AGY_MITOSIS_X into CLAUDE_MITOSIS_X as well, so an
# AGY_MITOSIS_SCOUT_MODEL would become CLAUDE_MITOSIS_SCOUT_MODEL=haiku and leak into Codex.
def codex_tier_model(tier: str) -> str:
    """Codex model for a subagent tier, honouring CODEX_MITOSIS_{SCOUT,WORKER}_MODEL.

    Values that do not match CODEX_MODEL_RE are ignored, because the model is later
    spliced into a shell command by the codex exec pinning path.
    """
    if tier == "flash_lite":
        default, env_name = DEFAULT_CODEX_SCOUT_MODEL, "CODEX_MITOSIS_SCOUT_MODEL"
    else:
        default, env_name = DEFAULT_CODEX_WORKER_MODEL, "CODEX_MITOSIS_WORKER_MODEL"
    override = os.environ.get(env_name, "").strip()
    return override if valid_codex_model(override) else default


def codex_tier_effort(tier: str) -> str:
    """Codex reasoning effort for a subagent tier, honouring CODEX_MITOSIS_{SCOUT,WORKER}_EFFORT."""
    if tier == "flash_lite":
        default, env_name = DEFAULT_CODEX_SCOUT_EFFORT, "CODEX_MITOSIS_SCOUT_EFFORT"
    else:
        default, env_name = DEFAULT_CODEX_WORKER_EFFORT, "CODEX_MITOSIS_WORKER_EFFORT"
    override = os.environ.get(env_name, "").strip().lower()
    return override if override in CODEX_EFFORT_LEVELS else default


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


def codex_policy_notice() -> str:
    return (
        "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
        f"location, git/log checks, single-file inspection) to `{codex_tier_model('flash_lite')}` "
        "(`agent_type: \"explorer\"`) and multi-file research, test runs, web/docs "
        f"research and scoped code edits to `{codex_tier_model('flash')}` (`agent_type: \"worker\"`). "
        "Never use `model: \"inherit\"` on subagents. "
        "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md. "
        "Do not dictate other report headings in spawn_agent messages: subagents answer with "
        "STATUS/FILES_CHANGED/VERIFICATION/NOTES."
    )


# Backwards-compatible constant (default configuration).
CODEX_POLICY_NOTICE = codex_policy_notice()
