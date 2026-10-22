"""Workspace/path guards, scout allowlist and shell/command parsing."""

import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import ALLOWED_SUBAGENT_MODELS


AGENT_CALL_RE = re.compile(r"\bagent\s*\(")


MODEL_KWARG_RE = re.compile(r"""\bmodel\s*=\s*(?:["']([^"']*)["']|([A-Za-z_][A-Za-z0-9_.]*))""")


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
        literal = m.group(1)
        if literal is not None and literal.strip().lower() not in ALLOWED_SUBAGENT_MODELS:
            violations.append(f"agent() call #{n} uses model={literal!r}")
        # A bare identifier (variable) is accepted; it cannot be checked statically.
    return violations


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


# Commands a Claude Code scout may run through Bash. Anything else (and any
# output redirection) is denied, so a read-only scout stays read-only.
SCOUT_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "rg", "grep", "egrep", "fgrep", "find", "stat", "file",
    "du", "df", "pwd", "which", "tree", "sort", "cut", "jq", "basename",
    "dirname", "realpath", "readlink", "echo", "date", "uname", "nl", "diff", "cmp", "md5sum",
    "sha256sum",
}


SCOUT_GIT_SUBCOMMANDS = {
    "status", "log", "show", "diff", "blame", "branch", "rev-parse", "ls-files", "ls-tree",
    "grep", "describe", "shortlog", "reflog", "remote", "tag", "config",
}


SCOUT_FORBIDDEN_ARGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
    "tree": {"-o"},
    "diff": {"--output"},
    "log": {"--output"},
    "show": {"--output"},
    "branch": {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"},
    "tag": {"-d", "--delete", "-a", "-s", "-f"},
    "remote": {"add", "remove", "rm", "rename", "set-url", "prune", "set-head"},
    "config": {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--global", "--system"},
    "reflog": {"delete", "expire"},
}


_SCOUT_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;\n]")


_SCOUT_HARMLESS_REDIRECT_RE = re.compile(r"\d?>\s*/dev/null|2>&1")


def scout_command_is_read_only(command: str) -> Tuple[bool, str]:
    """Check a scout's Bash command against the read-only allowlist."""
    cleaned = _SCOUT_HARMLESS_REDIRECT_RE.sub(" ", command)
    if re.search(r"[<>`]|\$\(", cleaned):
        return False, "redirection or command substitution"
    for segment in _SCOUT_SEGMENT_SPLIT_RE.split(cleaned):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False, "unparseable command"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in SCOUT_GIT_SUBCOMMANDS:
                return False, f"git {sub or '(none)'} is not read-only"
            if sub == "config" and not any(w in ("--get", "--get-all", "--list", "-l", "--get-regexp") for w in words):
                return False, "git config without --get/--list"
            bad = {w for w in words[2:] for f in SCOUT_FORBIDDEN_ARGS.get(sub, set())
                   if w == f or (f.startswith("--") and w.startswith(f + "="))}
            if sub == "branch" and len([w for w in words[2:] if not w.startswith("-")]) > 0 and not bad:
                bad = {"<create>"}
            if sub == "tag" and len([w for w in words[2:] if not w.startswith("-")]) > 0 and "-l" not in words and "--list" not in words:
                bad = bad or {"<create>"}
            if bad:
                return False, f"git {sub} {sorted(bad)[0]} changes the repository"
            continue
        if prog not in SCOUT_READ_ONLY_COMMANDS:
            return False, f"'{prog}' is not on the scout allowlist"
        forbidden = SCOUT_FORBIDDEN_ARGS.get(prog, set())
        if any(w in forbidden or any(w.startswith(f) for f in forbidden if f.startswith("--")) for w in words[1:]):
            return False, f"'{prog}' with a writing option"
    return True, ""


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


_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([A-Za-z_][A-Za-z0-9_]*))")


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


_REDIRECT_RE = re.compile(r"^[0-9&]*[<>]")


# `codex exec` options that take a separate value, so that value is not the prompt.
_CODEX_VALUE_OPTS = frozenset((
    "-c", "--config", "-i", "--image", "-s", "--sandbox", "-p", "--profile", "-C", "--cd",
    "-m", "--model", "--color", "-o", "--output-last-message", "--output-schema",
    "--add-dir", "--local-provider", "--enable", "--disable",
))


def _split_simple_commands(cmd: str) -> List[Tuple[int, int]]:
    """(start, end) spans of the simple commands in a shell command line.

    Splits at unquoted ; & | ( ) and newlines, skips here-document bodies and comments."""
    spans: List[Tuple[int, int]] = []
    n = len(cmd)
    i = 0
    seg = 0
    quote = ""
    pending: List[Tuple[str, bool]] = []
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            nl = cmd.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = _HEREDOC_RE.match(cmd, i)
            if m:
                word = m.group(2) or m.group(3) or m.group(4)
                pending.append((word, m.group(1) == "-"))
                i = m.end()
                continue
        if ch == "\n":
            spans.append((seg, i))
            i += 1
            while pending:
                word, strip = pending.pop(0)
                while i < n:
                    nl = cmd.find("\n", i)
                    line = cmd[i:] if nl < 0 else cmd[i:nl]
                    i = n if nl < 0 else nl + 1
                    if (line.lstrip("\t") if strip else line) == word:
                        break
            seg = i
            continue
        if ch == "&" and ((i > 0 and cmd[i - 1] in "<>") or cmd.startswith("&>", i)):
            i += 1
            continue
        if ch in ";&|()":
            spans.append((seg, i))
            i += 1
            seg = i
            continue
        i += 1
    spans.append((seg, n))
    return [(s, e) for s, e in spans if s < e and cmd[s:e].strip()]


def _tokenize_with_spans(text: str, base: int) -> Optional[List[Tuple[int, int, str]]]:
    """Shell words of one simple command as (start, end, value), offsets relative to the
    whole command. None when the quoting is unbalanced."""
    toks: List[Tuple[int, int, str]] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
            continue
        start = i
        buf: List[str] = []
        while i < n and text[i] not in " \t\r\n":
            ch = text[i]
            if ch == "\\":
                if i + 1 >= n:
                    return None
                buf.append(text[i + 1])
                i += 2
            elif ch == "'":
                j = text.find("'", i + 1)
                if j < 0:
                    return None
                buf.append(text[i + 1:j])
                i = j + 1
            elif ch == '"':
                j = i + 1
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`':
                        buf.append(text[j + 1])
                        j += 2
                        continue
                    buf.append(text[j])
                    j += 1
                if j >= n:
                    return None
                i = j + 1
            else:
                buf.append(ch)
                i += 1
        toks.append((base + start, base + i, "".join(buf)))
    return toks


def _parse_codex_exec(cmd: str) -> Optional[Dict[str, Any]]:
    """Find a real `codex exec` invocation in a shell command line, or None.

    Returns {"prompt", "edit": (start, end, replacement-kind)} where edit locates the
    model value to replace or the insertion point after `exec`."""
    for s, e in _split_simple_commands(cmd):
        try:
            shlex.split(cmd[s:e], posix=True)
        except ValueError:
            continue
        toks = _tokenize_with_spans(cmd[s:e], s)
        if not toks:
            continue
        k = 0
        while k < len(toks):
            val = toks[k][2]
            base = os.path.basename(val)
            if _ASSIGN_RE.match(val) or base in ("env", "nohup", "time", "sudo", "exec", "command"):
                k += 1
                continue
            break
        if k >= len(toks):
            continue
        if os.path.basename(toks[k][2]) == "npx":
            k += 1
            while k < len(toks) and toks[k][2].startswith("-"):
                k += 1
            if k >= len(toks) or toks[k][2] not in ("codex", "@openai/codex"):
                continue
        elif os.path.basename(toks[k][2]) != "codex":
            continue
        if k + 1 >= len(toks) or toks[k + 1][2] != "exec":
            continue
        exec_tok = toks[k + 1]
        rest = toks[k + 2:]
        model_edit: Optional[Tuple[int, int, str]] = None
        prompt_parts: List[str] = []
        j = 0
        while j < len(rest):
            ts, te, val = rest[j]
            if _REDIRECT_RE.match(val):
                j += 1 if re.search(r"[<>]\S", val) else 2
                continue
            if val == "--":
                prompt_parts = [t[2] for t in rest[j + 1:]]
                break
            if val in ("-m", "--model"):
                if j + 1 < len(rest) and model_edit is None:
                    model_edit = (rest[j + 1][0], rest[j + 1][1], "value")
                j += 2
                continue
            if val.startswith("--model="):
                if model_edit is None:
                    model_edit = (ts + len("--model="), te, "value")
                j += 1
                continue
            if val.startswith("-m") and not val.startswith("--") and len(val) > 2:
                if model_edit is None:
                    model_edit = (ts, te, "short")
                j += 1
                continue
            if val in _CODEX_VALUE_OPTS:
                j += 2
                continue
            if val.startswith("-") and len(val) > 1:
                j += 1
                continue
            prompt_parts = [val]
            break
        return {
            "prompt": " ".join(prompt_parts).strip(),
            "model_edit": model_edit,
            "insert_at": exec_tok[1],
        }
    return None


def _rewrite_codex_exec_model(cmd: str, parsed: Dict[str, Any], target_model: str) -> str:
    edit = parsed["model_edit"]
    if edit is None:
        at = parsed["insert_at"]
        return cmd[:at] + " -m " + shlex.quote(target_model) + cmd[at:]
    start, end, kind = edit
    replacement = shlex.quote(target_model)
    if kind == "short":
        replacement = "-m " + replacement
    return cmd[:start] + replacement + cmd[end:]
