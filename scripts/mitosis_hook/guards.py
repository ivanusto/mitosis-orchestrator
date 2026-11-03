"""Workspace/path guards, scout allowlist and shell/command parsing."""

import ntpath
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


# Windows path handling. ntpath is used (not os.path) so the logic is testable on
# any platform. It is only active when a root is a Windows path or os.name == "nt",
# so a backslash in a POSIX command is never read as a path separator.
_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:(?:[\\/]|$)|\\\\)")

# , = : end a token: each starts a new one, so C:\repo\a,C:\Windows\x checks both paths.
_WIN_TAIL = r"""[^\s"'<>|*?;&(),=:]*"""

_WIN_QUOTED_RE = re.compile(
    r"""(["'])((?:[A-Za-z]:[\\/]|\\\\|%[A-Za-z_]\w*%|~[\\/])[^"'\n]*?)\1"""
)

# A drive letter starts a path after a non-word character, or right after a short flag
# (-IC:\x, /IC:\x, -LC:\x). Flags of one or two letters only, so words like http: are not flags.
_WIN_DRIVE_START = (
    r"(?:(?<![\w\\/.:~-])[-/][A-Za-z]{1,2}(?=[A-Za-z]:)|(?<![A-Za-z0-9_]))"
)

_WIN_FLAG_PREFIX_RE = re.compile(r"[-/][A-Za-z]{1,2}(?=[A-Za-z]:)")

# C:name or a bare D: resolves against that drive's own current directory: unknown target.
# A letter right after - or / is an option (-o:, /C:"text"), not a drive.
_WIN_DRIVE_REL_RE = re.compile(_WIN_DRIVE_START + r"(?<![-/])([A-Za-z]):(?![\\/:])(" + _WIN_TAIL + r")")

_WIN_ENV_REF_RE = re.compile(r"\$\{?env:", re.IGNORECASE)

_WIN_TOKEN_RE = re.compile(
    r"(?:" + _WIN_DRIVE_START + r"[A-Za-z]:[\\/]" + _WIN_TAIL
    + r"|(?<![\w\\/])\\\\(?:[?.]\\(?:[A-Za-z]:)?|[\w.$-]+\\[\w.$-])" + _WIN_TAIL
    + r"|(?<![\w%])%(?:~[A-Za-z0-9$]*|[A-Za-z_]\w*%)" + _WIN_TAIL
    + r"|(?<![\w~])~[\\/]" + _WIN_TAIL + r")"
)

_WIN_QUOTED_DOTDOT_RE = re.compile(r"""(["'])([^"'\n]*\.\.[^"'\n]*)\1""")

# \Windows\x: rooted on the current drive. Unquoted only, so findstr "\.py" is left alone.
_WIN_ROOTED_RE = re.compile(r"""(?:^|[\s>=<(])(\\(?!\\)[^\s"'<>|*?;&(),=:]+)""")

# "\Windows\win.ini": a quoted rooted token whose segments all look like file names is a path.
# A regex-looking string ("\.py", "\d+", "\s*x", "\.py$") has a metacharacter or a first
# segment starting with "." and is left alone. Ambiguous strings such as "\w\d" look like
# file names and may be denied: a false deny is the safe direction.
_WIN_QUOTED_ROOTED_RE = re.compile(
    r"""(["'])(\\(?!\\)(?:[A-Za-z0-9_~@#-][A-Za-z0-9_ .~@#-]*)(?:\\[A-Za-z0-9_~@#-][A-Za-z0-9_ .~@#-]*)*)\1"""
)

# cmd accepts cd..\x: letters glued to a leading ..
_WIN_GLUED_DOTDOT_RE = re.compile(r"^[A-Za-z]+(?=\.\.)")

_WIN_DOTDOT_RE = re.compile(r"""(?:^|[\s=:"'(])([A-Za-z0-9._\-/\\]*\.\.[A-Za-z0-9._\-/\\]*)""")

_WIN_KNOWN_ENV = {
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOMEPATH", "HOMEDRIVE",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PROGRAMFILES", "PROGRAMDATA", "PUBLIC",
    "ALLUSERSPROFILE", "COMSPEC", "CD",
}


def _strip_cmd_carets(command: str) -> str:
    """cmd's ^ escape joins tokens (C^:\\, ^C:\\, t^ype). Dropped outside double quotes only,
    where cmd keeps it literally. ^ at end of line is a line continuation."""
    out: List[str] = []
    in_quote = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == '"':
            in_quote = not in_quote
        elif ch == "^" and not in_quote:
            i += 1
            if i < len(command) and command[i] in "\r\n":
                i += 1
                if command[i - 1] == "\r" and i < len(command) and command[i] == "\n":
                    i += 1
                continue
            if i < len(command):
                out.append(command[i])
                if command[i] == '"':
                    in_quote = not in_quote
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_windows_path(text: Any) -> bool:
    return bool(_WIN_ABS_RE.match(str(text)))


def _windows_mode(roots: List[Path]) -> bool:
    return os.name == "nt" or any(_is_windows_path(r) for r in roots)


def _win_norm(text: str) -> str:
    text = str(text)
    low = text.lower()
    if low.startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[8:]
    elif low.startswith(("\\\\?\\", "\\\\.\\")):
        text = text[4:]
    text = ntpath.normpath(text)
    if os.name == "nt":
        try:
            text = os.path.realpath(text)
        except (OSError, ValueError):
            pass
    return ntpath.normcase(text)


def _win_inside(target: str, roots: List[Path]) -> bool:
    if not _is_windows_path(_win_strip(target)):
        return False
    t = _win_norm(target).rstrip("\\")
    for root in roots:
        if not _is_windows_path(root):
            continue
        r = _win_norm(str(root)).rstrip("\\")
        if t == r or t.startswith(r + "\\"):
            return True
    return False


def _win_strip(text: str) -> str:
    low = text.lower()
    if low.startswith("\\\\?\\unc\\"):
        return "\\\\" + text[8:]
    if low.startswith(("\\\\?\\", "\\\\.\\")):
        return text[4:]
    return text


def _win_base(cwd: str, roots: List[Path]) -> str:
    expanded_cwd = os.path.expanduser(cwd) if cwd else ""
    if _is_windows_path(expanded_cwd):
        return expanded_cwd
    win_roots = [r for r in roots if _is_windows_path(r)]
    if win_roots:
        return str(win_roots[0])
    return str(Path.cwd())


def _drive_relative_is_path(drive: str, rest: str) -> bool:
    """C:name is ambiguous with words like a:b. Treat it as a path when it looks like one."""
    if not rest or drive.isupper() or any(c in rest for c in ".~\\/"):
        return True
    # lowercase word:word. On Windows an existing drive makes it a path; elsewhere it is text.
    return os.name == "nt" and os.path.exists(drive + ":\\")


def _win_expand(token: str, base: str = "") -> Optional[str]:
    """Expand ~ and %VAR% in a Windows path token; None when it cannot be resolved."""
    if token.startswith("~"):
        return ntpath.join(os.path.expanduser("~"), token[1:].lstrip("\\/"))
    if token.startswith("%~"):
        return None  # batch parameter modifiers such as %~dp0: unknown value
    m = re.match(r"%([^%]+)%(.*)$", token, re.DOTALL)
    if not m:
        return token
    name, rest = m.group(1), m.group(2)
    if rest and rest[0] not in "\\/" and name.upper() not in _WIN_KNOWN_ENV:
        return ""  # e.g. printf '%s%s': not an environment path
    if name.upper() == "CD" and base:
        return base + rest
    value = os.environ.get(name)
    if value is None:
        for key, val in os.environ.items():
            if key.upper() == name.upper():
                value = val
                break
    if not value:
        return None
    return value + rest


def _check_windows_paths(command: str, cwd: str, roots: List[Path]) -> Tuple[bool, str, str]:
    """Check Windows-style path tokens. Returns (ok, reason, command with checked tokens blanked)."""
    tokens: List[str] = []
    base = _win_base(cwd, roots)
    if _WIN_ENV_REF_RE.search(command):
        return False, "PowerShell $env: reference has an unknown value", command

    def take(m: "re.Match[str]") -> str:
        if m.re is _WIN_TOKEN_RE:
            tokens.append(_WIN_FLAG_PREFIX_RE.sub("", m.group(0), count=1))
        else:
            tokens.append(m.group(2))
            # a quoted string may hold several paths: "C:\repo\a,C:\Windows\x"
            tokens.extend(
                t.group(0) for t in _WIN_TOKEN_RE.finditer(m.group(2), 1)
                if not t.group(0).startswith(("%",))
            )
        return " " * (m.end() - m.start())

    command = _WIN_QUOTED_RE.sub(take, command)
    command = _WIN_TOKEN_RE.sub(take, command)
    for m in _WIN_DRIVE_REL_RE.finditer(command):
        if _drive_relative_is_path(m.group(1), m.group(2)):
            return False, f"drive-relative path {m.group(0)!r} has an unknown target", command
    for token in tokens:
        expanded = _win_expand(token, base)
        if expanded == "":
            continue
        if expanded is None or not _win_inside(expanded, roots):
            return False, f"path {token!r} is outside the workspace", command
    rel: List[str] = []

    def take_rel(m: "re.Match[str]") -> str:
        rel.append(m.group(2))
        return " " * (m.end() - m.start())

    command = _WIN_QUOTED_DOTDOT_RE.sub(take_rel, command)
    command = _WIN_QUOTED_ROOTED_RE.sub(take_rel, command)
    for m in _WIN_ROOTED_RE.finditer(command):
        rel.append(m.group(1))
    for m in _WIN_DOTDOT_RE.finditer(command):
        rel.append(m.group(1))
        glued = _WIN_GLUED_DOTDOT_RE.sub("", m.group(1))
        if glued != m.group(1):
            rel.append(glued)
    if rel:
        for token in rel:
            candidate = ntpath.normpath(ntpath.join(base, token))
            if not _win_inside(candidate, roots):
                return False, f"relative path {token!r} escapes the workspace", command
    return True, "", command


def _resolve_root(text: str) -> Path:
    expanded = os.path.expanduser(str(text))
    if os.name != "nt" and _is_windows_path(expanded):
        return Path(ntpath.normpath(expanded))  # cannot resolve a Windows path here
    return Path(expanded).resolve()


def allowed_roots(payload: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for wp in payload.get("workspacePaths") or []:
        try:
            roots.append(_resolve_root(str(wp)))
        except OSError:
            pass
    cwd = payload.get("cwd")
    if cwd:
        try:
            roots.append(_resolve_root(str(cwd)))
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
            roots.append(_resolve_root(str(proj_dir)))
        except OSError:
            pass
    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        try:
            roots.append(_resolve_root(str(artifact_dir)))
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
        if _windows_mode(roots) and _is_windows_path(_win_strip(os.path.expanduser(text))):
            return _win_inside(os.path.expanduser(text), roots)
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


# Commands a scout may run through its shell tool. This is a text check, not a sandbox: it denies
# whatever it cannot read for certain, so a false deny is expected and a false allow is a bug.
SCOUT_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "rg", "grep", "egrep", "fgrep", "find", "stat", "file",
    "du", "df", "pwd", "which", "tree", "sort", "cut", "jq", "basename",
    "dirname", "realpath", "readlink", "echo", "date", "uname", "nl", "diff", "cmp", "md5sum",
    "sha256sum", "uptime",
}

# Programs where an option that a glob expands to cannot run anything or write a file.
_SCOUT_ANY_GLOB_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "stat", "du", "cut", "basename",
    "dirname", "realpath", "readlink", "echo", "nl", "cmp", "md5sum", "sha256sum",
}

_SCOUT_ENV_PREFIXES = {"LC_ALL", "LANG", "TZ", "NO_COLOR"}
_SCOUT_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+-]*$")

# System inspection. The first non-flag word of systemctl must be one of these; no word means list-units.
SCOUT_SYSTEMCTL_SUBCOMMANDS = {
    "status", "show", "cat", "is-active", "is-enabled", "is-failed", "is-system-running", "get-default",
    "list-units", "list-unit-files", "list-timers", "list-sockets", "list-jobs", "list-dependencies",
}
# An allowlist, with values attached by =: a spaced value (-t status stop) would read as the subcommand.
_SCOUT_SYSTEMCTL_FLAG_RE = re.compile(
    r"^(?:--failed|--no-pager|--no-legend|--user|--system|--all|-a|--full|-l|--plain|--value|--quiet|-q|"
    r"--reverse|--before|--after|-n[0-9]+|--lines=[0-9]+|"
    r"--(?:type|state|property|output|legend)=[A-Za-z0-9_.,:-]*)$"
)


SCOUT_GIT_SUBCOMMANDS = {
    "status", "log", "show", "diff", "blame", "branch", "rev-parse", "ls-files", "ls-tree",
    "grep", "describe", "shortlog", "reflog", "remote", "tag", "config",
}

_SCOUT_GIT_GLOBAL_FLAGS = {"--no-pager", "-P", "--no-optional-locks"}
_SCOUT_GIT_DIFF_OPTIONS = {"--output", "--ext-diff", "--remerge-diff"}  # writes, runs a program, writes objects
_SCOUT_GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
_SCOUT_GIT_REMOTE_FLAGS = {"-v", "--verbose", "--all", "--push"}
_SCOUT_FIND_FORBIDDEN = ("-delete", "-rm", "-exec", "-ok", "-fprint", "-fls")  # -rm: bfs, which runs as find in Claude Code

_SCOUT_UGREP_FORBIDDEN = {
    "-Q", "-@", "--query", "--filter", "--filter-magic-label", "--pager", "--view", "--save-config", "--config",
    "--format-open",
}

# Options that run a program, write a file or change state. See _scout_option_hit for how they match.
SCOUT_FORBIDDEN_ARGS = {
    "sort": {"-o", "-T", "--output", "--compress-program", "--temporary-directory"},
    "tree": {"-o", "-R"},
    "file": {"-C", "--compile", "-p", "--preserve-date"},
    "tail": {"-f", "-F", "--follow"},  # never ends
    "date": {"-s", "--set"},
    "rg": {"-z", "--search-zip", "--pre", "--pre-glob", "--hostname-bin"},
    "diff": {"-l", "--paginate", "--output"},
    # Claude Code's shell runs ugrep as grep, and bfs as find. It sends these options to GNU grep, which
    # has none of them, but other hosts may not: ugrep runs filters and pagers and can write a config file.
    "grep": _SCOUT_UGREP_FORBIDDEN,
    "egrep": _SCOUT_UGREP_FORBIDDEN,
    "fgrep": _SCOUT_UGREP_FORBIDDEN,
    "journalctl": {
        "-f", "--follow", "--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "--sync",
        "--relinquish-var", "--smart-relinquish-var", "--setup-keys", "--update-catalog", "--cursor-file",
        "-M", "--machine", "--image", "--force",
    },
}

# The same, for git subcommands. Options before the subcommand have their own allowlist above.
SCOUT_GIT_FORBIDDEN_ARGS = {
    "log": _SCOUT_GIT_DIFF_OPTIONS,
    "show": _SCOUT_GIT_DIFF_OPTIONS,
    "diff": _SCOUT_GIT_DIFF_OPTIONS,
    "shortlog": _SCOUT_GIT_DIFF_OPTIONS,
    "blame": _SCOUT_GIT_DIFF_OPTIONS,
    "reflog": _SCOUT_GIT_DIFF_OPTIONS,
    "grep": {"-O", "--open-files-in-pager", "--ext-grep"},
    "branch": {
        "-d", "-D", "-m", "-M", "-c", "-C", "-u", "-f", "-t", "--delete", "--move", "--copy", "--force", "--track",
        "--no-track", "--set-upstream", "--set-upstream-to", "--unset-upstream", "--edit-description", "--create-reflog",
    },
    "tag": {
        "-d", "-a", "-s", "-f", "-m", "-F", "-e", "-u", "-v", "--delete", "--annotate", "--sign", "--no-sign", "--force",
        "--message", "--file", "--edit", "--local-user", "--verify", "--trailer", "--cleanup", "--create-reflog",
    },
    "config": {
        "-e", "--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section",
        "--edit", "--global", "--system",
    },
}


def _scout_option_hit(word: str, forbidden: Any) -> bool:
    """True when word is a forbidden option: --out and --output=x for --output (getopt accepts
    abbreviations), -uo and -ofile for -o. Letters of an attached value also count: a false deny."""
    if word.startswith("--"):
        name = word[2:].split("=", 1)[0]
        return bool(name) and any(f.startswith("--" + name) for f in forbidden)
    if word.startswith("-") and len(word) > 1:
        return any("-" + letter in forbidden for letter in word[1:])
    return False


_SCOUT_SCAN_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:=,+@%^"
)
_SCOUT_SCAN_GLOB_CHARS = frozenset("*?[]")
_SCOUT_SCAN_DQ_ESCAPES = frozenset('"\\$`')
_SCOUT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SCOUT_HARMLESS_REDIRECT_RE = re.compile(r"(?:[12]?>[ \t]*/dev/null|2>&1)(?=[ \t\n;|&]|\Z)")
_SCOUT_QUOTE_HINT = "use single quotes for a literal"


class _ScoutWord:
    """One shell word: value after quote removal, index of its first unquoted glob character
    (-1 for none), and plain, which is True when no part of it was quoted or escaped."""

    __slots__ = ("value", "glob_at", "plain")

    def __init__(self) -> None:
        self.value = ""
        self.glob_at = -1
        self.plain = True


def _scout_scan(command: str) -> Tuple[List[List[_ScoutWord]], str]:
    """Split a command line into simple commands of words, or return why it cannot be read.

    Allows only what bash, dash, busybox sh and zsh read the same way. Anything that expands, groups
    or redirects is denied, not modelled: `$` and backticks outside single quotes, parentheses, braces,
    comments, here-documents, a lone `&`. Comments and $'...' matter most: without them a quote the
    shell does not see could hide a command from this scanner."""
    if _SCOUT_CONTROL_RE.search(command):
        return [], "control character"
    segments: List[List[_ScoutWord]] = []
    current: List[_ScoutWord] = []
    word: Optional[_ScoutWord] = None
    pending = ""  # the operator before the current segment
    redirected = False  # a harmless redirect in the current segment; alone on a line zsh runs cat for it
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch in " \t":
            if word is not None:
                current.append(word)
                word = None
            i += 1
            continue
        if ch == "\n" or ch == ";" or ch == "|" or ch == "&":
            if ch == "&" and command[i:i + 2] != "&&":
                return [], "background or redirect with &"
            if ch == "|" and command[i:i + 2] == "|&":
                return [], "pipe with |&"
            op = command[i:i + 2] if command[i:i + 2] in ("&&", "||") else ch
            if word is not None:
                current.append(word)
                word = None
            if current:
                segments.append(current)
                current = []
                pending = op
            elif redirected:
                return [], "redirect without a command"
            elif op != "\n":
                return [], f"empty command before {op}"
            redirected = False
            i += len(op)
            continue
        if ch == ">" or ch.isdigit():
            # Only outside a word: ls2>/dev/null is a program named ls2 on one shell and ls on another.
            if word is None:
                m = _SCOUT_HARMLESS_REDIRECT_RE.match(command, i)
                if m:
                    redirected = True
                    i = m.end()
                    continue
            if ch == ">":
                return [], "redirection"
        if ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return [], "unterminated single quote"
            body = command[i + 1:end]
            if "\n" in body:
                return [], "newline inside quotes"
            if word is None:
                word = _ScoutWord()
            word.value += body
            word.plain = False
            i = end + 1
            continue
        if ch == '"':
            if word is None:
                word = _ScoutWord()
            word.plain = False
            i += 1
            while True:
                if i >= n:
                    return [], "unterminated double quote"
                c = command[i]
                if c == '"':
                    i += 1
                    break
                if c in "$`!":
                    return [], f"{c} inside double quotes, {_SCOUT_QUOTE_HINT}"
                if c == "\n":
                    return [], "newline inside quotes"
                if c == "\\":
                    if i + 1 >= n or command[i + 1] == "\n":
                        return [], "backslash before a line end"
                    if not command[i - 1].isascii():
                        # A multibyte locale can read the byte before \ and the \ as one character.
                        return [], f"non-ASCII text before a backslash, {_SCOUT_QUOTE_HINT}"
                    if command[i + 1] in _SCOUT_SCAN_DQ_ESCAPES:
                        word.value += command[i + 1]
                        i += 2
                    else:
                        word.value += c
                        i += 1
                    continue
                word.value += c
                i += 1
            continue
        if ch == "\\":
            nxt = command[i + 1] if i + 1 < n else ""
            if not nxt or not nxt.isascii() or not nxt.isprintable():
                return [], "backslash before a line end or non-ASCII text"
            if word is None:
                word = _ScoutWord()
            word.value += nxt
            word.plain = False
            i += 2
            continue
        if ch in _SCOUT_SCAN_GLOB_CHARS:
            if word is None:
                word = _ScoutWord()
            if word.glob_at < 0:
                word.glob_at = len(word.value)
            word.value += ch
            i += 1
            continue
        if ch == "~":
            at_start = word is None
            if at_start and command[i + 1:i + 2] not in ("", "/", " ", "\t", "\n", ";", "|", "&"):
                return [], f"~name expansion, {_SCOUT_QUOTE_HINT}"
            if not at_start and word.value[-1:] in ("=", ":"):
                return [], f"tilde after = or :, {_SCOUT_QUOTE_HINT}"
            if word is None:
                word = _ScoutWord()
            word.value += ch
            i += 1
            continue
        if ch in _SCOUT_SCAN_WORD_CHARS:
            if ch == "=" and word is None:
                return [], f"word that starts with =, {_SCOUT_QUOTE_HINT}"
            if word is None:
                word = _ScoutWord()
            word.value += ch
            i += 1
            continue
        if ch == "$" or ch == "`":
            return [], f"variable or command substitution, {_SCOUT_QUOTE_HINT}"
        if ch == "#":
            return [], "comment, remove it"
        if ch == "<":
            return [], "input redirection or process substitution"
        if ch in "(){}":
            return [], f"grouping or brace expansion, {_SCOUT_QUOTE_HINT}"
        return [], f"character {ch!r} outside quotes, {_SCOUT_QUOTE_HINT}"

    if word is not None:
        current.append(word)
    if current:
        segments.append(current)
    elif redirected:
        return [], "redirect without a command"
    elif pending in ("|", "||", "&&"):
        return [], f"nothing after {pending}"
    if not segments:
        return [], "empty command"
    return segments, ""


def _scout_git_denied(args: List[str]) -> str:
    i = 0
    while i < len(args):
        word = args[i]
        if word in _SCOUT_GIT_GLOBAL_FLAGS:
            i += 1
        elif word == "-C" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            i += 2
        elif word.startswith("-"):
            return f"git global option {word} is not allowed"
        else:
            break
    if i >= len(args):
        return "git (none) is not read-only"
    sub = args[i]
    rest = args[i + 1:]
    if sub not in SCOUT_GIT_SUBCOMMANDS:
        return f"git {sub} is not read-only"
    positional = [w for w in rest if not w.startswith("-")]
    if sub == "config" and not (rest and rest[0] in _SCOUT_GIT_CONFIG_READS):
        return "git config must start with --get or --list"
    if sub == "remote":
        if positional and positional[0] != "get-url":
            return f"git remote {positional[0]} is not read-only"
        if any(w.startswith("-") and w not in _SCOUT_GIT_REMOTE_FLAGS for w in rest):
            return "git remote option is not read-only"
    if sub == "reflog" and positional and positional[0] not in ("show", "exists"):
        return "git reflog needs show or exists"
    if any(_scout_option_hit(w, ("--help",)) for w in rest):
        return "git --help starts man, use -h"
    forbidden = SCOUT_GIT_FORBIDDEN_ARGS.get(sub, ())
    hit = next((w for w in rest if _scout_option_hit(w, forbidden)), "")
    if hit:
        return f"git {sub} {hit} changes the repository or runs a program"
    # Only --list: -l meant --create-reflog before git 2.20, so `git branch -l x` created a branch there.
    if sub == "branch" and positional and "--list" not in rest:
        return "git branch with a name creates a branch"
    if sub == "tag" and positional and "-l" not in rest and "--list" not in rest:
        return "git tag with a name creates a tag"
    return ""


def _scout_systemctl_denied(args: List[str]) -> str:
    sub = next((w for w in args if not w.startswith("-")), None)
    if sub is not None and sub not in SCOUT_SYSTEMCTL_SUBCOMMANDS:
        return f"systemctl {sub!r} is not read-only"
    bad = next((w for w in args if w.startswith("-") and not _SCOUT_SYSTEMCTL_FLAG_RE.match(w)), "")
    return f"systemctl option {bad} is not allowed, attach values with =" if bad else ""


def _scout_date_denied(args: List[str]) -> str:
    """date MMDDhhmm sets the clock with no option at all, so only +FORMAT may stand alone."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in ("-d", "--date", "-r", "--reference", "-f", "--file"):
            skip = True
        elif not word.startswith(("-", "+")):
            return f"date {word} sets the clock"
    return ""


def _scout_simple_command_denied(words: List[_ScoutWord], strict_windows: bool) -> str:
    index = 0
    while index < len(words) and words[index].plain and "=" in words[index].value:
        name, _, value = words[index].value.partition("=")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) is None:
            break
        if strict_windows:
            return "assignment is not allowed on this shell"
        if name not in _SCOUT_ENV_PREFIXES or not _SCOUT_ENV_VALUE_RE.match(value):
            return f"variable {name} is not allowed before a command"
        index += 1
    words = words[index:]
    if not words:
        return "assignment without a command"
    if any(word.plain and word.value == "[[" for word in words):
        # busybox sh reads || and && after this word as plain arguments, so the commands this scanner
        # split apart would be one command there, and its options would go unchecked.
        return f"the word [[, {_SCOUT_QUOTE_HINT}"
    head = words[0]
    prog = head.value
    if head.glob_at >= 0:
        return "glob in the program name"
    if prog != "git" and prog != "systemctl" and prog != "journalctl" and prog not in SCOUT_READ_ONLY_COMMANDS:
        return f"'{prog}' is not on the scout allowlist, a program with a path is not allowed either"
    for word in words[1:]:
        if word.glob_at >= 0 and prog not in _SCOUT_ANY_GLOB_COMMANDS:
            prefix = word.value[:word.glob_at]
            if not prefix or prefix.startswith("-"):
                return f"a glob could expand to an option of {prog}, start it with ./ or {_SCOUT_QUOTE_HINT}"
    args = [word.value for word in words[1:]]
    if prog == "git":
        return _scout_git_denied(args)
    if prog == "systemctl":
        return _scout_systemctl_denied(args)
    if prog == "find":
        bad = next((w for w in args if w.startswith(_SCOUT_FIND_FORBIDDEN)), "")
        return f"'find' with {bad}" if bad else ""
    bad = next((w for w in args if _scout_option_hit(w, SCOUT_FORBIDDEN_ARGS.get(prog, ()))), "")
    if bad:
        return f"'{prog}' with {bad} writes a file, runs a program or changes state"
    return _scout_date_denied(args) if prog == "date" else ""


# Codex on Windows may run PowerShell or cmd, where quotes and backslashes differ from POSIX
# ('a&b' is not a quote on cmd, \" is not an escape on PowerShell). Nothing that differs is accepted.
_SCOUT_STRICT_WINDOWS_RE = re.compile(r"^[A-Za-z0-9_./:=+| \t-]+$")


def scout_command_is_read_only(command: str, strict_windows: bool = False) -> Tuple[bool, str]:
    """Check a scout's shell command against the read-only allowlist. Returns (ok, reason)."""
    command = command.strip(" \t\n")
    if not command:
        return False, "empty command"
    if strict_windows:
        if not _SCOUT_STRICT_WINDOWS_RE.match(command):
            return False, "quotes, backslashes, redirects and globs are not allowed on this shell"
        if any(w.startswith("/") for w in command.split()):
            return False, "a word that starts with / is not allowed on this shell"
    segments, why = _scout_scan(command)
    if why:
        return False, why
    for words in segments:
        why = _scout_simple_command_denied(words, strict_windows)
        if why:
            return False, why
    return True, ""


def command_stays_in_workspace(command: str, cwd: str, roots: List[Path]) -> Tuple[bool, str]:
    """Return (ok, reason). ok is False when the command names a path outside the
    roots or matches a pattern that should always be confirmed by a human."""
    if _windows_mode(roots):
        command = _strip_cmd_carets(command)
    for pattern, why in (
        (ESCALATION_RE, "privilege escalation"),
        (RECURSIVE_DELETE_OF_ROOT_RE, "recursive delete of a root directory"),
        (SYSTEM_ALTERING_RE, "system-altering command"),
        (FORCE_PUSH_RE, "force push"),
        (PIPE_TO_SHELL_RE, "download piped to a shell"),
    ):
        if pattern.search(command):
            return False, why
    win_mode = _windows_mode(roots)
    if win_mode:
        ok, why, command = _check_windows_paths(command, cwd, roots)
        if not ok:
            return False, why
    home = os.path.expanduser("~")
    for m in ABS_PATH_TOKEN_RE.finditer(command):
        token = m.group(1)
        if win_mode and re.fullmatch(r"/[A-Za-z0-9]{1,2}", token):
            continue  # cmd switch such as dir /b, not a path
        if token in ("/", "~", "~/", "$HOME", "$HOME/"):
            return False, f"path token {token!r} is outside the workspace"
        expanded = os.path.expanduser(token.replace("$HOME", home))
        if expanded.startswith(SYSTEM_PATH_PREFIXES):
            continue
        if not _within_roots(expanded, roots):
            return False, f"path {token!r} is outside the workspace"
    if win_mode:
        return True, ""
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
