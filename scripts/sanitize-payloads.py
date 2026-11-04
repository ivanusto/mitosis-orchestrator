#!/usr/bin/env python3
"""Turn a raw hook payload capture into a fixture that is safe to commit.

    sanitize-payloads.py IN OUT --platform P --cli-version V --scenario S [--provenance TEXT]

IN is the payloads.jsonl written by scripts/mitosis_hook/capture.py (MITOSIS_CAPTURE_PAYLOADS):
one {"mode", "payload", "result"} object per line. A bare payload per line is accepted too, and its
mode is read from hook_event_name. Top-level keys other than those three are dropped.

The sanitizer is an allowlist. Key names and structure always survive. A value survives only when its
key is on a list below and the value passes that list's check (an enum-like name, a small int, a
boolean, a canned probe command). Everything else becomes a typed placeholder, so a key that is not
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

SANITIZER_VERSION = 1

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
    "prompt_id", "id", "call_id", "task_name", "harnessSectionHash",
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
    "url", "ephemeralMessage", "stopReason",
))
# Exit-code header of a failure text: it carries the signal and no content.
EXIT_PREFIX_RE = re.compile(r"^(Exit code \d+|[Cc]ommand exited with code \d+\.?|[Pp]rocess exited with code \d+|exit status \d+)")
JSON_TEXT_KEYS = frozenset(("tool_response", "output", "result"))
# Enum-like names: kept when they match the pattern. Spaces and parentheses cover agy model names.
NAME_KEYS = frozenset((
    "hook_event_name", "hookEventName", "tool_name", "permission_mode", "model", "modelName", "Model", "resolvedModel",
    "agent_type", "agentType", "subagent_type", "terminationReason", "source", "decision", "permissionDecision",
    "level", "status", "type", "effort", "mode", "fork_turns", "Role", "TypeName", "typeName", "role", "tool", "toolName",
))
NAME_RE = re.compile(r"^[A-Za-z0-9_.:()-][A-Za-z0-9_.:() -]{0,62}$")
INT_KEYS = frozenset((
    "invocationNum", "initialNumSteps", "stepIdx", "executionNum", "timeout_ms", "WaitMsBeforeAsync", "duration_ms",
    "yield_time_ms", "max_output_tokens", "fork_turns", "turn", "turnNumber", "step_index",
))
SMALL_INT = 1000

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

SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,40}$")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"gAAAA[A-Za-z0-9_-]{16,}")
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,}|Bearer\s+\S+")
HOME_PATH_RE = re.compile(r"^(/home/[^/]+|/Users/[^/]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+)")
PLACEHOLDER_RE = re.compile(r"__[A-Z]+[0-9]*__")


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

    def key(self, name: str) -> str:
        if SAFE_KEY_RE.match(name) and not self._bad(name):
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
    def value(self, value: Any, key: str, parent: str) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            if key in INT_KEYS or abs(value) <= SMALL_INT:
                return value if abs(value) <= 10 ** 9 else 0
            return 0
        if isinstance(value, float):
            return 0.0
        if isinstance(value, dict):
            return {self.key(str(k)): self.value(v, str(k), key) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key, parent) for v in value]
        if not isinstance(value, str):
            return "__STR__"
        return self.string(value, key, parent)

    def string(self, value: str, key: str, parent: str) -> str:
        if value == "":
            return ""
        if OPAQUE_MESSAGE_RE.match(value.strip()):
            return FAKE_OPAQUE
        if key in ID_KEYS:
            return self._id(value)
        if key in PATH_KEYS:
            return self.path(value, key)
        if key in COMMAND_KEYS:
            return self.command(value)
        if key == "name" and parent == "toolCall":
            return value if NAME_RE.match(value) and not self._bad(value) else "__STR__"
        if key in NAME_KEYS:
            return value if NAME_RE.match(value) and not self._bad(value) else "__STR__"
        if key in JSON_TEXT_KEYS and value.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(self.value(parsed, key, key), separators=(",", ":"), sort_keys=True)
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
            "result": san.value(line.get("result"), "result", ""),
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
