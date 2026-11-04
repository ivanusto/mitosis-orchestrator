#!/usr/bin/env python3
"""mitosis-doctor: does Mitosis really take effect on this machine, per platform?

Static checks (install, config, trust, version), an offline probe that runs the REAL hook command
strings from the installed manifest through the shell with realistic payloads, and the wire
heartbeat the hooks keep. Anything it cannot determine is UNKNOWN, never OK.

    python3 scripts/mitosis-doctor.py [--platform claude|codex|agy]... [--json] [--live]
                                      [--root PATH] [--home PATH]

Statuses: OK, WARN, FAIL, UNKNOWN, SKIP (platform not installed).
Exit code: 0 if every check is OK or SKIP; 1 if any WARN or UNKNOWN; 2 if any FAIL.

Codex hook trust is decided offline: the declaration hash and the plugin-bundle content hash that
Codex computes (verified against Codex 0.166) are recomputed from the installed files and compared
with `[hooks.state]` in config.toml and the legacy `.hook-trust` baseline. On a Codex version not in
CODEX_TRUST_VERIFIED a mismatch is only UNKNOWN. Doctor never starts Codex to ask it.

Read-only: it never modifies the user's config, never prints env values, tokens, hashes, config or
file contents (only versions, paths, key names and verdicts), never reads ~/.codex/auth.json, and
makes no network call. `--live` runs scripts/smoke-claude.py, which spends real money (Claude only).
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OK, WARN, FAIL, UNKNOWN, SKIP = "OK", "WARN", "FAIL", "UNKNOWN", "SKIP"
PLATFORMS = ("claude", "codex", "agy")
PLUGIN = "mitosis-orchestrator"
PLUGIN_KEY = "mitosis-orchestrator@mitosis-orchestrator"
SELF_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_STALE_DAYS = 14
PROBE_TIMEOUT = 60
CODEX_EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit", "subagent_stop", "stop")
CODEX_TRUST_VERIFIED = ("0.166",)  # major.minor of Codex releases whose hook-trust hashing was checked against source
CODEX_EVENT_LABELS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
                      "PostToolUse": "post_tool_use", "SubagentStop": "subagent_stop", "Stop": "stop"}
CODEX_MATCHER_EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "subagent_stop")
CODEX_DEFAULT_CONTEXT_LIMIT = 2500
CODEX_ROLES = (("explorer", "scout", True), ("worker", "worker", True), ("flash-lite-scout", None, False), ("flash-worker", None, False))
ZH_LOOKUP = "請找出 parse_header 這個函式定義在哪個檔案,並回報檔名"
OPAQUE = (
    "gAAAAABq2W6q_XALFZgD1zKjI6St8hIilg6diYo5-ULKaJMEhHaMKEcdSxOmRa2b1_16hBGQv9tSsT-yYt4xtowdOA_"
    "fE7FIw96PB5VXsqtVMWeVsN8J_9wdsW49rMnfe9yNrXF8a8tipe-jXMH7_OFi1IT9rdtcvnDYiwyoN1jlPhnNo"
)
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:/+-]")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- helpers

def safe(value: Any, limit: int = 64) -> str:
    """A short, printable-safe token (names, versions, tool names). Never free text."""
    return _SAFE_RE.sub("?", str(value))[:limit] or "?"


def parse_version(text: Any) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", str(text))[:4])


def load_json(path: Path) -> Tuple[Any, Optional[str]]:
    try:
        with open(str(path), encoding="utf-8-sig") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"


# ---- TOML: tomllib when present, else a small tolerant parser (Python 3.8)

def _split_header(text: str) -> List[str]:
    parts: List[str] = []
    cur = ""
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw[:1] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end > 0 else raw[1:]
    raw = raw.split("#", 1)[0].strip()
    if raw == "true":
        return True
    if raw == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def parse_toml_fallback(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    cur = data
    skip_until = ""
    for line in text.splitlines():
        if skip_until:
            if skip_until in line:
                skip_until = ""
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = re.match(r"^\[(?!\[)(.+?)\]\s*(?:#.*)?$", stripped)
        if header:
            cur = data
            for part in _split_header(header.group(1)):
                nxt = cur.setdefault(part, {})
                cur = nxt if isinstance(nxt, dict) else {}
            continue
        if stripped.startswith("[["):
            cur = {}
            continue
        kv = re.match(r"^((?:\"[^\"]*\"|'[^']*'|[A-Za-z0-9_.-])+)\s*=\s*(.*)$", stripped)
        if not kv:
            continue
        key, value = kv.group(1).strip().strip("\"'"), kv.group(2).strip()
        if value.startswith('"""') and value.count('"""') < 2:
            skip_until = '"""'
        elif value.startswith("[") and "]" not in value:
            skip_until = "]"
        cur[key] = _scalar(value) if not value.startswith("[") else []
    return data


def load_toml(path: Path) -> Tuple[Any, Optional[str]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        tomllib = importlib.import_module("tomllib")
    except ImportError:
        tomllib = None
    try:
        if tomllib is not None:
            return tomllib.loads(raw.decode("utf-8")), None
        return parse_toml_fallback(raw.decode("utf-8")), None
    except Exception:
        return None, "unreadable"


def dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def spec_models(root: Optional[Path]) -> Optional[Dict[str, Any]]:
    for base in (root, SELF_ROOT):
        if base is None:
            continue
        data, err = load_toml(base / "spec" / "models.toml")
        if err is None and isinstance(data, dict):
            return data
    return None


def repo_version() -> Optional[str]:
    data, err = load_json(SELF_ROOT / "plugin.json")
    version = data.get("version") if err is None and isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def manifest_version(root: Path) -> Optional[str]:
    data, err = load_json(root / "plugin.json")
    version = data.get("version") if err is None and isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def run_cmd(argv: List[str], timeout: int = 15) -> Tuple[Optional[int], str]:
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def commands(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from commands(item)


def find_key(node: Any, name: str):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == name:
                yield value
            else:
                yield from find_key(value, name)
    elif isinstance(node, list):
        for item in node:
            yield from find_key(item, name)


def matcher_matches(matcher: Any, name: str) -> Optional[bool]:
    """Claude Code / Codex rule: [A-Za-z0-9_|] is an exact-name list, anything else a regex."""
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return None
    if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        return name in matcher.split("|")
    try:
        return re.search(matcher, name) is not None
    except re.error:
        return None


# --------------------------------------------------------------------------- context

class Ctx:
    def __init__(self, home: Path, root: Optional[Path], tmp: Path) -> None:
        self.home = home
        self.root = root
        self.tmp = tmp
        self.checks: List[Dict[str, str]] = []
        self.explicit_home = False
        self.counter = 0

    def add(self, platform: str, cid: str, status: str, message: str) -> None:
        self.checks.append({"platform": platform, "id": platform + "." + cid, "status": status, "message": message})

    def heartbeat_dir(self) -> Path:
        override = os.environ.get("MITOSIS_HEARTBEAT_DIR")
        if override:
            return Path(override)
        if os.name == "nt":
            base = (str(self.home / "AppData" / "Local") if self.explicit_home
                    else os.environ.get("LOCALAPPDATA") or str(self.home / "AppData" / "Local"))
        else:
            base = (str(self.home / ".cache") if self.explicit_home
                    else os.environ.get("XDG_CACHE_HOME") or str(self.home / ".cache"))
        return Path(base) / "mitosis-orchestrator"


def check_cli(ctx: Ctx, platform: str, exe: str, version_cheap: bool = True) -> Optional[str]:
    """Adds the cli row; returns the first line of `--version` when it could be read."""
    path = shutil.which(exe)
    if not path:
        ctx.add(platform, "cli", WARN, "`%s` is not on PATH (config may still exist, but this shell cannot start it)" % exe)
        return None
    if not version_cheap:
        ctx.add(platform, "cli", OK, "found %s" % path)
        return None
    rc, out = run_cmd([path, "--version"])
    line = out.strip().splitlines()[0] if out.strip() else ""
    if rc == 0 and line:
        ctx.add(platform, "cli", OK, "%s, version %s" % (path, re.sub(r"[^A-Za-z0-9 ._()+-]", "?", line)[:80]))
        return line
    ctx.add(platform, "cli", UNKNOWN, "found %s but `--version` gave no usable answer" % path)
    return None


def check_stale(ctx: Ctx, platform: str, installed: Optional[str], where: str) -> None:
    repo = repo_version()
    if not installed:
        ctx.add(platform, "version", UNKNOWN, "installed version could not be read from %s" % where)
    elif not repo:
        ctx.add(platform, "version", UNKNOWN, "installed %s, but this repo's plugin.json has no version" % safe(installed))
    elif installed == repo:
        ctx.add(platform, "version", OK, "installed %s matches this repo" % safe(installed))
    else:
        ctx.add(platform, "version", WARN, "installed %s differs from this repo %s (stale or newer install)" % (safe(installed), safe(repo)))


# --------------------------------------------------------------------------- offline probe

def probe_env(ctx: Ctx, cp950: bool) -> Dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        up = key.upper()
        if "MITOSIS" in up or up.startswith("PYTHON"):
            continue
        env[key] = value
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = str(ctx.tmp / "tmp")
    env["MITOSIS_HEARTBEAT"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # never write __pycache__ into an installed tree
    if cp950:
        env["PYTHONIOENCODING"] = "cp950"
        env["PYTHONUTF8"] = "0"
    return env


def run_shell(command: str, env: Dict[str, str], stdin: bytes, cwd: Path) -> Tuple[Optional[int], bytes]:
    if os.name == "nt":  # the raw command line, as `cmd /c <command>`
        argv: Any = 'cmd /d /s /c "' + command + '"'
    else:
        argv = ["/bin/sh", "-c", command]
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, env=env, cwd=str(cwd), timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, b"timeout"
    except OSError:
        return None, b"oserror"
    return proc.returncode, proc.stdout


def manifest_command(platform: str, root: Path, tool: str) -> Tuple[Optional[str], str, str]:
    """(command with the plugin root substituted, status, message) for the PreToolUse hook."""
    rel = {"claude": "hooks/hooks.json", "codex": "hooks/codex-hooks.json", "agy": "hooks.json"}[platform]
    data, err = load_json(root / rel)
    if err:
        return None, UNKNOWN, "%s is %s, nothing to probe" % (rel, err)
    groups = [g for block in find_key(data, "PreToolUse") if isinstance(block, list) for g in block if isinstance(g, dict)]
    pairs = [(g.get("matcher"), c) for g in groups for c in commands(g)]
    if not pairs:
        return None, FAIL, "%s has no PreToolUse command: the hook never runs" % rel
    matched = None
    verdict = False
    for matcher, command in pairs:
        result = matcher_matches(matcher, tool)
        if result is None:
            return None, UNKNOWN, "PreToolUse matcher in %s is not understood" % rel
        if result:
            matched, verdict = command, True
            break
    if not verdict:
        return None, FAIL, "PreToolUse matcher in %s does not match tool `%s`: the hook never sees it" % (rel, tool)
    posix = root.as_posix()
    command = matched.replace("${CLAUDE_PLUGIN_ROOT}", posix).replace("${PLUGIN_ROOT}", posix)
    if platform == "agy":
        command = command.replace("./scripts/enforce-orchestrator.py", '"%s/scripts/enforce-orchestrator.py"' % posix)
    return command, OK, ""


def parse_stdout(rc: Optional[int], out: bytes) -> Tuple[Optional[Any], Optional[str]]:
    """(parsed JSON, failure message). Empty stdout is reported as such: the launcher fell through."""
    if rc is None:
        return None, "the chain did not finish (%s)" % out.decode("ascii", "replace")
    if rc != 0:
        return None, "exit code %s" % rc
    if not out.strip():
        return None, "empty stdout: the launcher fell through (no Python started, or it crashed before answering)"
    try:
        return json.loads(out.decode("utf-8")), None
    except ValueError:
        return None, "stdout is not JSON"


def validate_claude(obj: Any, models: Dict[str, Any]) -> Tuple[str, str, str]:
    want = dig(models, "claude", "scout", "model")
    spec = dig(obj, "hookSpecificOutput")
    upd = dig(spec, "updatedInput")
    if not isinstance(upd, dict):
        return FAIL, "no hookSpecificOutput.updatedInput: the Agent call was not rewritten", ""
    if dig(spec, "permissionDecision") == "deny":
        return FAIL, "the probe Agent call was denied", ""
    if upd.get("model") != want:
        return FAIL, "updatedInput.model is not the scout model `%s`" % safe(want), ""
    if not str(upd.get("prompt", "")).startswith(ZH_LOOKUP):
        return FAIL, "the Chinese prompt came back altered (stdin decoding)", ""
    return OK, "scout call pinned to `%s`" % safe(want), json.dumps(upd, sort_keys=True)


def validate_codex(obj: Any, models: Dict[str, Any]) -> Tuple[str, str, str]:
    spec = dig(obj, "hookSpecificOutput")
    upd = dig(spec, "updatedInput")
    if not isinstance(upd, dict):
        return FAIL, "no updatedInput: `collaborationspawn_agent` was not recognised", ""
    if upd.get("message") != OPAQUE or upd.get("agent_type") != "worker" or "model" in upd:
        return FAIL, "opaque spawn was not rewritten to agent_type `worker` with the message kept", ""
    return OK, "opaque spawn routed to the worker role", json.dumps(upd, sort_keys=True)


def validate_codex_text(obj: Any, models: Dict[str, Any]) -> Tuple[str, str, str]:
    upd = dig(obj, "hookSpecificOutput", "updatedInput")
    if not isinstance(upd, dict):
        return FAIL, "no updatedInput for the plain-text spawn", ""
    msg = str(upd.get("message", ""))
    if not msg.startswith(ZH_LOOKUP) or "REPORT FORMAT" not in msg or "model" in upd:
        return FAIL, "plain-text spawn came back altered (stdin decoding) or without the report format", ""
    return OK, "plain-text spawn kept intact with the report format", json.dumps(upd, sort_keys=True)


def validate_agy(obj: Any, models: Dict[str, Any]) -> Tuple[str, str, str]:
    want = dig(models, "agy", "scout", "model")
    if obj == {}:
        return FAIL, "stdout `{}` is a DENIAL on agy PreToolUse", ""
    if not isinstance(obj, dict) or obj.get("decision") != "allow":
        return FAIL, "agy answered something other than decision=allow", ""
    over = obj.get("overwrite")
    if not isinstance(over, dict):
        return FAIL, "no `overwrite`: the flat invoke_subagent call was not pinned (flat form unhandled)", ""
    if over.get("Model") != want:
        return FAIL, "overwrite.Model is not the scout model `%s`" % safe(want), ""
    prompt = str(over.get("Prompt", ""))
    if not prompt.startswith(ZH_LOOKUP) or "REPORT FORMAT" not in prompt:
        return FAIL, "Prompt came back altered or without the report format", ""
    return OK, "flat invoke_subagent pinned to `%s`" % safe(want), json.dumps(over, sort_keys=True)


def payloads(platform: str, ctx: Ctx, tag: str) -> List[Tuple[str, str, str, Any, str]]:
    """[(case, tool, extra args, validator, payload JSON)] for the platform."""
    sid = "doctor-%s-%d" % (tag, ctx.counter)
    ctx.counter += 1
    if platform == "claude":
        p = {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Agent",
             "tool_input": {"subagent_type": "mitosis-orchestrator:flash-lite-scout", "prompt": ZH_LOOKUP, "description": "probe"}}
        return [("scout", "Agent", "", validate_claude, json.dumps(p, ensure_ascii=False))]
    if platform == "codex":
        base = {"session_id": sid, "transcript_path": "/nonexistent/rollout.jsonl", "cwd": str(ctx.tmp), "hook_event_name": "PreToolUse",
                "model": "gpt-6.1-sol", "permission_mode": "bypassPermissions", "turn_id": "doctor-turn"}
        opaque = dict(base, tool_name="collaborationspawn_agent",
                      tool_input={"task_name": "probe", "fork_turns": "none", "message": OPAQUE})
        text = dict(base, session_id=sid + "-t", tool_name="collaborationspawn_agent",
                    tool_input={"task_name": "probe2", "fork_turns": "none", "agent_type": "explorer", "message": ZH_LOOKUP})
        return [("opaque", "collaborationspawn_agent", "--platform=codex", validate_codex, json.dumps(opaque)),
                ("text", "collaborationspawn_agent", "--platform=codex", validate_codex_text, json.dumps(text, ensure_ascii=False))]
    artifact = ctx.tmp / "brain" / sid
    artifact.mkdir(parents=True, exist_ok=True)
    p = {"artifactDirectoryPath": str(artifact), "conversationId": sid, "modelName": "gemini-4-argon-high", "invocationNum": 1,
         "toolCall": {"name": "invoke_subagent", "args": {"Prompt": ZH_LOOKUP, "Role": "Probe Scout", "TypeName": "flash-lite-scout",
                                                          "toolAction": "probe", "toolSummary": "probe"}}}
    return [("flat", "invoke_subagent", "", validate_agy, json.dumps(p, ensure_ascii=False))]


def offline_probe(ctx: Ctx, platform: str, root: Optional[Path]) -> None:
    if root is None:
        ctx.add(platform, "probe", UNKNOWN, "no plugin tree to probe")
        return
    models = spec_models(root)
    if models is None:
        ctx.add(platform, "probe", UNKNOWN, "spec/models.toml unreadable: expected models unknown")
        return
    (ctx.tmp / "tmp").mkdir(parents=True, exist_ok=True)
    cwd = ctx.tmp / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    for index, spec in enumerate(payloads(platform, ctx, "n")):
        case, tool, extra, validator = spec[:4]
        command, status, message = manifest_command(platform, root, tool)
        cid = "probe." + case
        if command is None:
            ctx.add(platform, cid, status, message)
            continue
        if extra and extra not in command:
            ctx.add(platform, cid, FAIL, "manifest command lacks %s" % extra)
            continue
        results = {}
        for label, cp950 in (("", False), (".cp950", True)):
            payload = payloads(platform, ctx, "c" if cp950 else "p")[index][4]
            rc, out = run_shell(command, probe_env(ctx, cp950), payload.encode("utf-8"), cwd)
            obj, problem = parse_stdout(rc, out)
            if problem:
                results[label] = (FAIL, problem, "")
            else:
                results[label] = validator(obj, models)
        plain, cp = results[""], results[".cp950"]
        ctx.add(platform, cid, plain[0], plain[1])
        if cp[0] != OK:
            ctx.add(platform, cid + ".cp950", FAIL, "with PYTHONIOENCODING=cp950 PYTHONUTF8=0: " + cp[1])
        elif plain[0] == OK and cp[2] != plain[2]:
            ctx.add(platform, cid + ".cp950", FAIL, "result under cp950 differs from the UTF-8 run (stdin is not read as bytes)")
        elif plain[0] == OK:
            ctx.add(platform, cid + ".cp950", OK, "same result with PYTHONIOENCODING=cp950 PYTHONUTF8=0")
        else:
            ctx.add(platform, cid + ".cp950", cp[0], cp[1])


# --------------------------------------------------------------------------- heartbeat

def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return "%dd" % (seconds // 86400)
    if seconds >= 3600:
        return "%dh" % (seconds // 3600)
    return "%dm" % (seconds // 60)


def check_heartbeat(ctx: Ctx, platform: str, installed: Optional[str]) -> None:
    path = ctx.heartbeat_dir() / ("heartbeat-%s.json" % platform)
    data, err = load_json(path)
    if err == "missing":
        ctx.add(platform, "heartbeat", UNKNOWN, "no heartbeat file at %s: no hook has fired since install" % path)
        return
    if err or not isinstance(data, dict):
        ctx.add(platform, "heartbeat", UNKNOWN, "heartbeat file %s is unreadable" % path)
        return
    now = time.time()
    unhandled = data.get("unhandled_spawn_tools")
    if isinstance(unhandled, dict) and unhandled:
        names = ", ".join("`%s`" % safe(k) for k in sorted(unhandled)[:8])
        ctx.add(platform, "heartbeat.unhandled", FAIL, "spawn tool(s) the hook saw but does not handle: %s" % names)
    else:
        ctx.add(platform, "heartbeat.unhandled", OK, "no unhandled spawn tools recorded")
    last_error = data.get("last_error")
    if isinstance(last_error, dict) and last_error:
        cls = last_error.get("class")
        cls = cls if isinstance(cls, str) and _CLASS_RE.match(cls) else "?"
        at = last_error.get("at")
        age = " %s ago" % age_text(now - at) if isinstance(at, (int, float)) else ""
        ctx.add(platform, "heartbeat.error", WARN, "last hook error: %s in `%s`%s" % (safe(cls, 80), safe(last_error.get("event", "?")), age))
    else:
        ctx.add(platform, "heartbeat.error", OK, "no hook error recorded")
    repl = data.get("stdin_replacement_chars")
    if isinstance(repl, int) and repl > 0:
        ctx.add(platform, "heartbeat.stdin", WARN, "%d replacement characters seen in hook stdin (non-UTF-8 input)" % repl)
    else:
        ctx.add(platform, "heartbeat.stdin", OK, "no stdin decoding damage recorded")
    mismatch = dig(data, "pin", "mismatch")
    if isinstance(mismatch, int) and mismatch > 0:
        ctx.add(platform, "heartbeat.pin", WARN, "%d pin mismatch(es) recorded: a subagent ran on another model than pinned" % mismatch)
    else:
        ctx.add(platform, "heartbeat.pin", OK, "no pin mismatch recorded")
    seen = data.get("plugin_version")
    if not isinstance(seen, str) or not installed:
        ctx.add(platform, "heartbeat.version", UNKNOWN, "plugin version in heartbeat or install unknown")
    elif seen != installed:
        ctx.add(platform, "heartbeat.version", WARN, "heartbeat written by %s, installed is %s (stale)" % (safe(seen), safe(installed)))
    else:
        ctx.add(platform, "heartbeat.version", OK, "heartbeat written by the installed version %s" % safe(installed))
    last = data.get("last_event_at")
    if not isinstance(last, (int, float)):
        ctx.add(platform, "heartbeat.age", UNKNOWN, "heartbeat has no last_event_at")
    elif now - last > HEARTBEAT_STALE_DAYS * 86400:
        ctx.add(platform, "heartbeat.age", UNKNOWN, "no hook event for %s (over %d days): cannot tell if it still works" % (age_text(now - last), HEARTBEAT_STALE_DAYS))
    else:
        ctx.add(platform, "heartbeat.age", OK, "last hook event %s ago" % age_text(now - last))
    check_report_rejections(ctx, platform, data)


def _class_counts(value: Any) -> List[Tuple[str, int]]:
    """(class name, count) pairs from a heartbeat map. Names must look like identifiers: nothing else is shown."""
    if not isinstance(value, dict):
        return []
    out = [(k, v) for k, v in value.items()
           if isinstance(k, str) and _CLASS_RE.match(k) and isinstance(v, int) and not isinstance(v, bool) and v > 0]
    return sorted(out)[:12]


def check_report_rejections(ctx: Ctx, platform: str, data: Dict[str, Any]) -> None:
    """Informational: why subagent reports were rejected, by class name only. Never a warning by itself,
    because a rejected report is the gate working (and one that was fixed counts again)."""
    parts = []
    for label, key in (("scout", "report_bad_scout"), ("worker", "report_bad_worker")):
        pairs = _class_counts(data.get(key))
        if pairs:
            parts.append("%s: %s" % (label, ", ".join("%s=%d" % (safe(k, 48), n) for k, n in pairs)))
    if parts:
        ctx.add(platform, "heartbeat.report", OK, "info: subagent reports rejected by class: " + "; ".join(parts))
    else:
        ctx.add(platform, "heartbeat.report", OK, "info: no rejected subagent report recorded")


# --------------------------------------------------------------------------- Codex hook trust (offline)

class BundleError(Exception):
    """kind is "unsupported" (symlink, device, ...: Codex refuses the bundle) or "unreadable"."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def codex_bundle_hash(root: Path) -> str:
    """Codex's plugin content hash (plugin_fingerprint.rs, rust-v0.166.0), as `sha256:<hex>`."""
    rootb = os.fsencode(str(root))
    try:
        if not stat.S_ISDIR(os.lstat(rootb).st_mode):
            raise BundleError("unsupported")
    except OSError:
        raise BundleError("unreadable")
    h = hashlib.sha256()
    h.update(b"codex-plugin-bundle-v1")
    pending = [b""]
    while pending:
        rel = pending.pop()
        last = rel.rsplit(b"/", 1)[-1]
        if rel:
            try:
                text = last.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and (text in ("__pycache__", ".venv") or text.endswith(".pyc") or text.endswith(".egg-info")):
                continue
        h.update(len(rel).to_bytes(8, "little"))
        h.update(rel)
        full = rootb + b"/" + rel if rel else rootb
        try:
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                h.update(b"directory")
                names = sorted(os.listdir(full))
                pending.extend((rel + b"/" + n) if rel else n for n in reversed(names))
            elif stat.S_ISREG(st.st_mode):
                h.update(b"file")
                h.update(st.st_size.to_bytes(8, "little"))
                seen = 0
                with open(full, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        seen += len(chunk)
                        if seen > st.st_size:
                            break
                        h.update(chunk)
                if seen != st.st_size:
                    raise BundleError("unreadable")
            else:
                raise BundleError("unsupported")
        except OSError:
            raise BundleError("unreadable")
    return "sha256:" + h.hexdigest()


def codex_declaration_hash(label: str, matcher: Any, handler: Dict[str, Any]) -> Optional[str]:
    """Codex's hook declaration hash (discovery.rs hook_hash), as `sha256:<hex>`; None if not a plain command hook."""
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str) or not handler["command"].strip():
        return None
    timeout = handler.get("timeout")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0):
        return None
    run_async = handler.get("async", False)
    status_message = handler.get("statusMessage")
    limit = handler.get("additionalContextLimit")
    if not isinstance(run_async, bool) or (status_message is not None and not isinstance(status_message, str)):
        return None
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        return None
    entry: Dict[str, Any] = {"type": "command", "command": handler["command"], "timeout": max(1, 600 if timeout is None else timeout),
                             "async": run_async}
    if status_message is not None:
        entry["statusMessage"] = status_message
    if limit is not None and label in ("pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit") and limit != CODEX_DEFAULT_CONTEXT_LIMIT:
        entry["additionalContextLimit"] = limit
    identity: Dict[str, Any] = {"event_name": label, "hooks": [entry]}
    if label in CODEX_MATCHER_EVENTS and isinstance(matcher, str):
        identity["matcher"] = matcher
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def codex_legacy_baseline_path(config_file: Path, plugin_id: str) -> Path:
    ident = hashlib.sha256()
    for part in (os.fsencode(str(config_file)), plugin_id.encode("utf-8")):
        ident.update(len(part).to_bytes(8, "little"))
        ident.update(part)
    return config_file.parent / ".hook-trust" / ident.hexdigest()


def codex_hook_list(version_dir: Path) -> Tuple[Optional[List[Tuple[str, str, Optional[str]]]], Optional[str]]:
    """[(config key, event label, declaration hash or None)] for every command hook in the cached manifest."""
    data, err = load_json(version_dir / "hooks" / "codex-hooks.json")
    hooks = dig(data, "hooks") if err is None else None
    if not isinstance(hooks, dict):
        return None, err or "malformed"
    out = []
    for event, groups in hooks.items():
        label = CODEX_EVENT_LABELS.get(event)
        if label is None or not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for hi, handler in enumerate(handlers if isinstance(handlers, list) else []):
                key = "%s:hooks/codex-hooks.json:%s:%d:%d" % (PLUGIN_KEY, label, gi, hi)
                decl = codex_declaration_hash(label, group.get("matcher"), handler) if isinstance(handler, dict) else None
                out.append((key, label, decl))
    return out, None


def codex_trust_verified(cli_line: Optional[str]) -> bool:
    if os.name == "nt" or not cli_line:
        return False
    m = re.search(r"(\d+)\.(\d+)", cli_line)
    return bool(m) and ("%s.%s" % (m.group(1), m.group(2))) in CODEX_TRUST_VERIFIED


_RANK = {OK: 0, WARN: 1, UNKNOWN: 2, FAIL: 3}
REVIEW_HINT = ("Fix: open Codex once, accept the hook review, then run doctor again; the `hook:` lines "
               "in Codex must show Completed.")


def worst(results: List[Tuple[str, str]], default: Tuple[str, str]) -> Tuple[str, str]:
    return max(results, key=lambda r: _RANK[r[0]]) if results else default


def newest_mtime(root: Path) -> Optional[float]:
    newest = None
    try:
        for base, dirs, files in os.walk(str(root)):
            for name in files + dirs:
                try:
                    m = os.lstat(os.path.join(base, name)).st_mtime
                except OSError:
                    continue
                newest = m if newest is None or m > newest else newest
    except OSError:
        return None
    return newest


def check_codex_trust(ctx: Ctx, cfg: Any, err: Optional[str], xdir: Path, version_dir: Optional[Path], verified: bool) -> None:
    p = "codex"
    state = dig(cfg, "hooks", "state") if not err else None
    hooks, herr = (None, "no cache") if version_dir is None else codex_hook_list(version_dir)
    if err or hooks is None:
        why = "config.toml is %s" % err if err else ("no plugin cache to read the hook declarations from" if herr == "no cache"
                                                      else "hooks/codex-hooks.json in the cache is %s" % herr)
        for event in CODEX_EVENTS:
            ctx.add(p, "trust." + event, UNKNOWN, why)
        ctx.add(p, "trust.bundle", UNKNOWN, why)
        return
    states: Dict[str, Any] = {}
    for key, label, decl in hooks:
        item = state.get(key) if isinstance(state, dict) else None
        states[key] = item
    declared = {label for _k, label, _d in hooks}
    # pass 1: did any declaration match?
    any_match = any(
        decl is not None and isinstance(states[key], dict) and states[key].get("trusted_hash") == decl
        for key, _l, decl in hooks)
    per_event: Dict[str, List[Tuple[str, str]]] = {}
    for key, label, decl in hooks:
        item = states[key]
        res = per_event.setdefault(label, [])
        if not isinstance(item, dict) or item.get("trusted_hash") in (None, ""):
            res.append((FAIL, "no approval `[hooks.state.\"%s\"] trusted_hash` in config.toml: Codex refuses this hook. %s" % (key, REVIEW_HINT)))
        elif not isinstance(item["trusted_hash"], str) or item.get("enabled") not in (None, True, False):
            res.append((UNKNOWN, "the stored approval for this hook is malformed"))
        elif item.get("enabled") is False:
            res.append((FAIL, "the hook is disabled (`enabled = false`) in config.toml, Codex will not run it. Enable it in Codex `/hooks`, then run doctor again."))
        elif decl is None:
            res.append((UNKNOWN, "the hook declaration in hooks/codex-hooks.json is not a plain command hook, cannot compute its hash"))
        elif item["trusted_hash"] == decl:
            res.append((OK, "approval matches the hook declaration"))
        elif verified or any_match:
            res.append((FAIL, "the hook declaration changed since it was approved (Codex shows it as modified and refuses it). " + REVIEW_HINT))
        else:
            res.append((UNKNOWN, "the approval does not match this hook declaration, but this Codex version's hash method is not verified, so it may be this check that is out of date"))
    for event in CODEX_EVENTS:
        if event not in declared:
            ctx.add(p, "trust." + event, FAIL, "hooks/codex-hooks.json declares no `%s` hook. Reinstall the plugin." % event)
            continue
        status, message = worst(per_event[event], (UNKNOWN, "no result"))
        ctx.add(p, "trust." + event, status, message)

    # bundle
    try:
        content = codex_bundle_hash(version_dir)
        content_err = None
    except BundleError as exc:
        content, content_err = None, exc.kind
    results: List[Tuple[str, str]] = []
    if content_err == "unsupported":
        results.append((FAIL if verified else UNKNOWN, "the plugin tree holds a symlink or special file; Codex cannot fingerprint it and skips the hooks. Reinstall the plugin without it, then: " + REVIEW_HINT))
    elif content_err:
        results.append((UNKNOWN, "the plugin tree could not be read consistently (changed while hashing, or unreadable)"))
    else:
        baseline_text: Optional[str] = None
        baseline_state = "missing"
        cfg_file = xdir / "config.toml"
        for cand in dict.fromkeys([cfg_file, Path(os.path.realpath(str(cfg_file)))]):
            bpath = codex_legacy_baseline_path(cand, PLUGIN_KEY)
            try:
                baseline_text = bpath.read_text(encoding="utf-8")
                baseline_state = "found"
                break
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                baseline_state = "unreadable"
                break
        for key, label, decl in hooks:
            item = states[key]
            if not isinstance(item, dict) or not isinstance(item.get("trusted_hash"), str) or not item.get("trusted_hash"):
                continue
            stored = item.get("trusted_content_hash")
            if stored is not None:
                if not isinstance(stored, str):
                    results.append((UNKNOWN, "a stored content approval is malformed"))
                elif stored == content:
                    results.append((OK, "plugin contents match the stored approval"))
                else:
                    results.append((FAIL if verified else UNKNOWN, "the plugin files changed after the hooks were approved (Codex refuses them). " + REVIEW_HINT
                                    if verified else "the plugin contents differ from the stored approval, but this Codex version's hash method is not verified"))
            elif baseline_state == "found":
                if baseline_text == content:
                    results.append((OK, "plugin contents match the legacy approval baseline (no per-hook content approval stored)"))
                else:
                    results.append((FAIL if verified else UNKNOWN, ("the plugin files differ from the legacy approval baseline (Codex refuses the hooks). " + REVIEW_HINT)
                                    if verified else "the plugin contents differ from the legacy baseline, but this Codex version's hash method is not verified"))
            elif baseline_state == "unreadable":
                results.append((UNKNOWN, "the legacy approval baseline file is unreadable"))
            else:
                results.append((WARN if verified else UNKNOWN, "no content approval and no legacy baseline: Codex will pin whatever is installed, unreviewed, on the next hook run. Review the hooks in Codex `/hooks` first."))
    status, message = worst(results, (UNKNOWN, "no stored hook approval to compare the plugin contents with"))
    if status == UNKNOWN and not verified and any("not verified" in m for s, m in results):
        hint = stale_hint(ctx, version_dir)
        if hint:
            message += "; " + hint
    ctx.add(p, "trust.bundle", status, message)


def stale_hint(ctx: Ctx, version_dir: Optional[Path]) -> str:
    """A hook ran after the newest bundle file changed: then Codex accepted these files and this check is stale."""
    data, err = load_json(ctx.heartbeat_dir() / "heartbeat-codex.json")
    last = data.get("last_event_at") if err is None and isinstance(data, dict) else None
    seen = data.get("plugin_version") if err is None and isinstance(data, dict) else None
    if version_dir is None or not isinstance(last, (int, float)) or seen != version_dir.name:
        return ""
    newest = newest_mtime(version_dir)
    if newest is not None and last > newest:
        return "a hook ran after the newest plugin file changed, so the check method is probably stale for this Codex"
    return ""


# --------------------------------------------------------------------------- platforms

def not_installed(ctx: Ctx, platform: str, reason: str, tool: str) -> None:
    """One SKIP row. With --root the tree itself is still probed (that tests the tree, not the install)."""
    ctx.add(platform, "installed", SKIP, "plugin not installed: " + reason)
    if ctx.root is not None:
        offline_probe(ctx, platform, ctx.root)


def check_claude(ctx: Ctx, live: bool) -> None:
    p = "claude"
    cdir = ctx.home / ".claude"
    data, err = load_json(cdir / "plugins" / "installed_plugins.json")
    if err == "missing":
        return not_installed(ctx, p, "~/.claude/plugins/installed_plugins.json not found", "claude")
    plugins = dig(data, "plugins")
    if err is None and isinstance(plugins, dict) and plugins.get(PLUGIN_KEY) is None:
        return not_installed(ctx, p, "`%s` has no entry in installed_plugins.json" % PLUGIN_KEY, "claude")
    check_cli(ctx, p, "claude")
    root = ctx.root
    version = None
    if err or not isinstance(plugins, dict):
        ctx.add(p, "plugin", UNKNOWN, "installed_plugins.json is %s" % (err or "malformed (no `plugins` object)"))
    else:
        entries = plugins.get(PLUGIN_KEY)
        entry = next((e for e in entries if isinstance(e, dict) and isinstance(e.get("installPath"), str)), None) if isinstance(entries, list) else None
        if entry is None:
            ctx.add(p, "plugin", UNKNOWN, "the `%s` entry in installed_plugins.json is malformed (no installPath)" % PLUGIN_KEY)
        else:
            version = entry.get("version") if isinstance(entry.get("version"), str) else None
            sha = entry.get("gitCommitSha")
            sha_text = ", commit %s" % safe(sha, 12) if isinstance(sha, str) else ""
            install = Path(entry["installPath"])
            if not install.is_dir():
                ctx.add(p, "plugin", FAIL, "installPath %s does not exist" % install)
            else:
                ctx.add(p, "plugin", OK, "installed at %s, version %s%s" % (install, safe(version or "?"), sha_text))
                root = root or install
    check_stale(ctx, p, version, "installed_plugins.json")
    if root is not None:
        missing = [rel for rel in ("hooks/hooks.json", "claude-agents/flash-lite-scout.md", "claude-agents/flash-worker.md") if not (root / rel).is_file()]
        if missing:
            ctx.add(p, "files", FAIL, "missing under %s: %s" % (root, ", ".join(missing)))
        else:
            ctx.add(p, "files", OK, "hooks/hooks.json and both agent files present")
    else:
        ctx.add(p, "files", UNKNOWN, "no install path to inspect")
    states = []
    for name in ("settings.json", "settings.local.json"):
        sdata, serr = load_json(cdir / name)
        if serr == "unreadable":
            states.append("unreadable")
        elif serr is None:
            states.append(dig(sdata, "enabledPlugins", PLUGIN_KEY))
    if any(s is False for s in states):
        ctx.add(p, "enabled", FAIL, "enabledPlugins sets `%s` to false in the user settings" % PLUGIN_KEY)
    elif "unreadable" in states:
        ctx.add(p, "enabled", UNKNOWN, "a settings file is unreadable")
    elif any(s is True for s in states):
        ctx.add(p, "enabled", OK, "enabledPlugins has `%s` = true" % PLUGIN_KEY)
    else:
        ctx.add(p, "enabled", UNKNOWN, "not set in user settings; project or managed settings are not inspected")
    offline_probe(ctx, p, root)
    check_heartbeat(ctx, p, version)
    if live:
        run_live(ctx)


def run_live(ctx: Ctx) -> None:
    script = SELF_ROOT / "scripts" / "smoke-claude.py"
    if not script.is_file():
        ctx.add("claude", "live", UNKNOWN, "scripts/smoke-claude.py not found")
        return
    try:
        proc = subprocess.run([sys.executable, str(script)], stdin=subprocess.DEVNULL, capture_output=True, timeout=700)
    except (OSError, subprocess.SubprocessError):
        ctx.add("claude", "live", UNKNOWN, "smoke-claude.py did not finish")
        return
    rc = proc.returncode
    status = {0: OK, 1: FAIL}.get(rc, UNKNOWN)
    ctx.add("claude", "live", status, "smoke-claude.py exit code %s (0 pass, 1 fail, 2 could not run)" % rc)


def newest_version_dir(base: Path) -> Optional[Path]:
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return None
    dirs.sort(key=lambda d: parse_version(d.name))
    return dirs[-1] if dirs else None


def check_scout_shell_rules(ctx: Ctx, platform: str) -> None:
    """Informational. Which rule set the hook applies to a Codex scout's shell command: Codex's exec_command
    lets the model pick the shell, so a host where pwsh or powershell exists gets the strict rules, which
    refuse quotes, backslashes, redirects, globs and ; &. Same test as guards._codex_nonposix_shell_reachable."""
    try:
        found = shutil.which("pwsh") or shutil.which("powershell") or ("/usr/local/bin/pwsh" if os.path.exists("/usr/local/bin/pwsh") else None)
    except Exception:
        found = "unknown (the check failed)"
    if os.name == "nt":
        ctx.add(platform, "scout_shell_rules", OK, "info: strict (Windows host): scout commands may not use quotes, backslashes, redirects, globs, ; or &")
    elif found:
        ctx.add(platform, "scout_shell_rules", OK, "info: strict (PowerShell reachable: %s): scout commands may not use quotes, backslashes, redirects, globs, ; or &" % safe(os.path.basename(str(found)), 40))
    else:
        ctx.add(platform, "scout_shell_rules", OK, "info: posix (no pwsh or powershell found)")


def check_codex(ctx: Ctx) -> None:
    p = "codex"
    env_home = os.environ.get("CODEX_HOME")
    xdir = Path(env_home) if env_home and not ctx.explicit_home else ctx.home / ".codex"
    cfg, err = load_toml(xdir / "config.toml")
    cache_dir = newest_version_dir(xdir / "plugins" / "cache" / PLUGIN / PLUGIN)
    if cache_dir is None and err != "unreadable" and not isinstance(dig(cfg, "plugins", PLUGIN_KEY), dict):
        return not_installed(ctx, p, "no [plugins.\"%s\"] entry in ~/.codex/config.toml and no cache under ~/.codex/plugins/cache" % PLUGIN_KEY, "codex")
    cli_line = check_cli(ctx, p, "codex")
    if err == "missing":
        ctx.add(p, "hooks_feature", FAIL, "config.toml is missing: `[features] hooks = true` is absent, hooks are silently off")
    elif err:
        ctx.add(p, "hooks_feature", UNKNOWN, "config.toml is unreadable")
    elif dig(cfg, "features", "hooks") is True:
        ctx.add(p, "hooks_feature", OK, "[features] hooks = true")
    else:
        ctx.add(p, "hooks_feature", FAIL, "`[features] hooks = true` is not set: Codex silently ignores plugin hooks")
    if err:
        ctx.add(p, "plugin", UNKNOWN if err == "unreadable" else FAIL, "config.toml is %s" % err)
    else:
        entry = dig(cfg, "plugins", PLUGIN_KEY)
        if not isinstance(entry, dict):
            ctx.add(p, "plugin", FAIL, "no [plugins.\"%s\"] entry in config.toml" % PLUGIN_KEY)
        elif entry.get("enabled") is False:
            ctx.add(p, "plugin", FAIL, "plugin entry has enabled = false")
        elif entry.get("enabled") is True:
            ctx.add(p, "plugin", OK, "plugin entry present and enabled")
        else:
            ctx.add(p, "plugin", UNKNOWN, "plugin entry has no readable `enabled` key")
    version_dir = cache_dir
    root = ctx.root or version_dir
    version = version_dir.name if version_dir else None
    if version_dir is None:
        ctx.add(p, "cache", FAIL, "no plugin cache under ~/.codex/plugins/cache/%s/%s/<version>/" % (PLUGIN, PLUGIN))
    else:
        missing = [rel for rel in ("hooks/codex-hooks.json", "scripts/enforce-orchestrator.py") if not (version_dir / rel).is_file()]
        if missing:
            ctx.add(p, "cache", FAIL, "cache %s lacks: %s" % (version_dir, ", ".join(missing)))
        else:
            ctx.add(p, "cache", OK, "cache at %s" % version_dir)
    check_stale(ctx, p, version, "the cache directory name")
    check_codex_trust(ctx, cfg, err, xdir, version_dir, codex_trust_verified(cli_line))
    models = spec_models(root)
    for role, tier, required in CODEX_ROLES:
        rdata, rerr = load_toml(xdir / "agents" / (role + ".toml"))
        if rerr == "missing":
            ctx.add(p, "role." + role, FAIL if required else WARN, "~/.codex/agents/%s.toml is missing" % role)
            continue
        if rerr:
            ctx.add(p, "role." + role, UNKNOWN, "%s.toml is unreadable" % role)
            continue
        have = rdata.get("model") if isinstance(rdata, dict) else None
        want = dig(models, "codex", tier, "model") if tier else None
        if tier is None:
            ctx.add(p, "role." + role, OK if isinstance(have, str) and have else WARN,
                    "role file present" if isinstance(have, str) and have else "role file has no `model` key")
        elif not isinstance(want, str):
            ctx.add(p, "role." + role, UNKNOWN, "spec/models.toml has no codex.%s.model" % tier)
        elif have == want:
            ctx.add(p, "role." + role, OK, "model matches spec/models.toml")
        else:
            ctx.add(p, "role." + role, WARN, "model differs from spec/models.toml (expected `%s`); fine if you chose it" % safe(want))
    offline_probe(ctx, p, root)
    check_scout_shell_rules(ctx, p)
    check_heartbeat(ctx, p, version)


def check_agy(ctx: Ctx) -> None:
    p = "agy"
    default = ctx.home / ".gemini" / "config" / "plugins" / PLUGIN
    if not default.is_dir():
        return not_installed(ctx, p, "plugin directory ~/.gemini/config/plugins/%s not found" % PLUGIN, "agy")
    check_cli(ctx, p, "agy")
    root = ctx.root or default
    if not root.is_dir():
        ctx.add(p, "plugin", FAIL, "plugin directory %s does not exist" % root)
        root_ok = False
    else:
        ctx.add(p, "plugin", OK, "plugin at %s" % root)
        root_ok = True
    if root_ok and not (root / "hooks.json").is_file():
        ctx.add(p, "files", FAIL, "hooks.json is missing at the plugin root")
    elif root_ok:
        ctx.add(p, "files", OK, "hooks.json present at the plugin root")
    version = manifest_version(root) if root_ok else None
    check_stale(ctx, p, version, "plugin.json")
    offline_probe(ctx, p, root if root_ok else None)
    ctx.add(p, "pin_verify", UNKNOWN, "by design: agy runs no hooks inside child agents, so the child's real model cannot be verified")
    check_heartbeat(ctx, p, version)


# --------------------------------------------------------------------------- output

def exit_code(checks: List[Dict[str, str]]) -> int:
    statuses = {c["status"] for c in checks}
    if FAIL in statuses:
        return 2
    if WARN in statuses or UNKNOWN in statuses:
        return 1
    return 0


def render_text(checks: List[Dict[str, str]]) -> str:
    lines = []
    for platform in PLATFORMS:
        rows = [c for c in checks if c["platform"] == platform]
        if not rows:
            continue
        lines.append("== %s ==" % platform)
        for c in rows:
            lines.append("  [%-7s] %s: %s" % (c["status"], c["id"], c["message"]))
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in (OK, WARN, FAIL, UNKNOWN, SKIP)}
    lines.append("Summary: " + ", ".join("%d %s" % (n, s) for s, n in counts.items()) + "; exit code %d" % exit_code(checks))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Check whether Mitosis really takes effect on each platform.")
    ap.add_argument("--platform", action="append", choices=PLATFORMS, help="platform to check (repeatable; default all)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--live", action="store_true", help="also run scripts/smoke-claude.py (Claude only, COSTS MONEY)")
    ap.add_argument("--root", help="plugin tree to probe (default: the installed copy per platform)")
    ap.add_argument("--home", help="read config from here instead of ~ (for tests)")
    args = ap.parse_args(argv)
    platforms = [x for x in PLATFORMS if x in (args.platform or PLATFORMS)]

    if args.live:
        print("WARNING: --live runs a real Claude Code session via scripts/smoke-claude.py and costs money.",
              file=sys.stderr if args.json else sys.stdout, flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="mitosis-doctor-"))
    try:
        ctx = Ctx(Path(args.home) if args.home else Path.home(), Path(args.root) if args.root else None, tmp)
        ctx.explicit_home = bool(args.home)
        if "claude" in platforms:
            check_claude(ctx, args.live)
        if "codex" in platforms:
            check_codex(ctx)
        if "agy" in platforms:
            check_agy(ctx)
        if args.live and "claude" not in platforms:
            ctx.add("claude", "live", SKIP, "--live applies to Claude only and claude was not selected")
        code = exit_code(ctx.checks)
        if args.json:
            print(json.dumps({"exit_code": code, "checks": ctx.checks}, indent=2, ensure_ascii=False))
        else:
            print(render_text(ctx.checks))
        return code
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
