#!/usr/bin/env python3
"""Shell oracle for the scout read-only guard.

The guard (scripts/mitosis_hook/guards.py, scout_command_is_read_only) decides from text. This module
asks real shells what the text does. Every program name the guard knows is a logging stub on PATH, and
shell builtins are wrapped by functions, so a command can run with no real side effect. A command the
guard accepts is checked against what the shell executed:

  (a) every executed program is allowlisted
  (b) nothing reports "not found" (a program the guard did not predict)
  (c) every executed argv equals one segment of _scout_scan (env prefixes, ~ and globs allowed for)
  (d) the fixture tree is unchanged
  (e) the shell reports no syntax error
  (f) a glob in a slot the guard reads as one word (the value of git -C, the git subcommand, the value of
      date -d/-r/-f) did not expand to exactly one word. Only these slots are checked: a glob that expands to
      several words is normal for an operand (`ls a*`), so the oracle cannot call that wrong in general.
      Known limit: the slot table covers git and date only. Another program whose guard reads a glob word as one
      option value would need a row in _slot_words; the oracle does not find such a program by itself.

Because stubs exit 0, the right side of `||` never runs: executed is compared as a subset of expected,
and as an equal set when the command has no `||`. Allowlist-only mode drops (c) for guards that have
no _scout_scan.

CLI:
  python3 tests/scout_oracle.py --guard-root DIR --fuzz N --seed S --shells bash,dash,busybox[,zsh=/path]
                                [--bwrap] [--json out.json]
                                [--workers N] [--child-as-gib G] [--min-available-gib G]

Safe defaults (the host may run an inference engine): workers = min(4, cpu count), every child runs under
RLIMIT_AS 4 GiB (--child-as-gib 0 disables), and no new run starts while MemAvailable is under
--min-available-gib (default 3; 0 disables). Runs not started for that reason are counted in
"skipped_for_memory", separate from "skipped_for_budget". No /proc/meminfo means no floor.

Python 3.8, standard library only.
"""

import argparse
import concurrent.futures
import fnmatch
import importlib.util
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scout_limits  # noqa: E402

DEFAULT_ALLOWLIST = (
    "ls cat head tail wc rg grep egrep fgrep find stat file du df pwd which tree sort cut jq basename "
    "dirname realpath readlink echo date uname nl diff cmp md5sum sha256sum uptime"
).split()
EXTRA_ALLOWED = ("git", "systemctl", "journalctl")
SENTINELS = ("rm", "sh", "bash", "python3", "touch", "pwned", "xargs", "tee")
CANARIES = {
    "-delete": "canary delete\n",
    "--pre=./evil": "canary pre\n",
    "--output=x": "canary output\n",
    "a b": "canary space\n",
    "src/m.py": "print('m')\n",
}
NOT_FOUND_RE = re.compile(r"not found|No such file|Permission denied|cannot execute|inaccessible", re.I)
SYNTAX_RE = re.compile(r"syntax error|parse error|unexpected|unterminated|unmatched|"
                       r"bad substitution|unknown option|here-document", re.I)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TIMEOUT = 30


# --------------------------------------------------------------------------------------- guard loading

class Guard:
    """A loaded scout guard: verdict function, optional scanner, allowlist."""

    def __init__(self, root: Path, module: Any, guards: Any) -> None:
        self.root = root
        self.module = module
        self.verdict = module.scout_command_is_read_only
        self.scan = getattr(guards, "_scout_scan", None) if guards is not None else None
        names = getattr(module, "SCOUT_READ_ONLY_COMMANDS", None)
        if names is None and guards is not None:
            names = getattr(guards, "SCOUT_READ_ONLY_COMMANDS", None)
        self.allowlist = sorted(names) if names else list(DEFAULT_ALLOWLIST)
        self.allowed = set(self.allowlist) | set(EXTRA_ALLOWED)

    def check(self, command: str, strict_windows: bool = False) -> Tuple[bool, str]:
        try:
            return self.verdict(command, strict_windows=strict_windows)
        except TypeError:
            return self.verdict(command)

    def segments(self, command: str) -> Optional[List[List[Tuple[str, int, bool]]]]:
        """Scanner words for command as (value, glob_at, plain) tuples, or None without a scanner."""
        if self.scan is None:
            return None
        segments, why = self.scan(command.strip(" \t\n"))
        if why:
            return None
        return [[(w.value, w.glob_at, w.plain) for w in seg] for seg in segments]


def load_guard(root: Any) -> Guard:
    """Load scripts/enforce-orchestrator.py from root; refuse a mitosis_hook that lives elsewhere."""
    root = Path(root).resolve()
    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("enforce_orchestrator_oracle", str(scripts / "enforce-orchestrator.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook = sys.modules.get("mitosis_hook")
    where = Path(getattr(hook, "__file__", "") or "").resolve()
    try:
        where.relative_to(root)
    except ValueError:
        raise RuntimeError("mitosis_hook was loaded from %s, not from %s" % (where, root))
    return Guard(root, module, sys.modules.get("mitosis_hook.guards"))


# ------------------------------------------------------------------------------------------- sandbox

def _snapshot(*bases: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for base in bases:
        for dirpath, dirnames, filenames in os.walk(str(base)):
            dirnames.sort()
            for name in dirnames:
                p = os.path.join(dirpath, name)
                out[p] = "dir" if not os.path.islink(p) else ("link", os.readlink(p))
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.path.islink(p):
                    out[p] = ("link", os.readlink(p))
                else:
                    with open(p, "rb") as handle:
                        out[p] = handle.read()
        out[str(base)] = "dir"
    return out


class Shell:
    def __init__(self, label: str, argv: List[str]) -> None:
        self.label = label
        self.argv = argv
        self.builtins: Optional[List[str]] = None


def parse_shells(spec: str) -> List[Shell]:
    """bash,dash,busybox,zsh=/path -> Shell list. A name without a path is searched on PATH."""
    out: List[Shell] = []
    for item in [s for s in spec.split(",") if s]:
        name, _, path = item.partition("=")
        if name == "busybox":
            exe = path or shutil.which("busybox")
            if exe:
                out.append(Shell("busybox", [exe, "sh"]))
            continue
        exe = path or shutil.which(name)
        if exe and os.path.exists(exe):
            out.append(Shell(name, [exe]))
    return out


class Violation(dict):
    pass


class Sandbox:
    """One temp tree: stubs/ log/ work/ home/. Not thread safe, use one per worker."""

    def __init__(self, stub_names: Sequence[str], allowed: Sequence[str], bwrap: bool = False,
                 env_extra: Optional[Dict[str, str]] = None, base: Optional[str] = None,
                 child_as_gib: float = scout_limits.DEFAULT_CHILD_AS_GIB) -> None:
        self.preexec = scout_limits.child_preexec(child_as_gib)
        self.tmp = Path(tempfile.mkdtemp(prefix="scout-oracle-", dir=base)).resolve()
        self.stubs = self.tmp / "stubs"
        self.logs = self.tmp / "log"
        self.work = self.tmp / "work"
        self.home = self.tmp / "home"
        self.stub_names = sorted(set(stub_names))
        self.allowed = set(allowed)
        self.bwrap = bwrap
        self.env_extra = dict(env_extra or {})
        self.stubs.mkdir()
        self.logs.mkdir()
        for name in self.stub_names:
            path = self.stubs / name
            # An absolute /bin/sh shebang and one NUL-separated record per process: count, name, args.
            path.write_text(
                "#!/bin/sh\nprintf '%s\\0' \"$#\" '" + name + "' \"$@\" >> '" + str(self.logs) + "'/\"$$\"\nexit 0\n",
                encoding="utf-8",
            )
            path.chmod(0o755)
        self._fixture()
        self.base_tree = _snapshot(self.work, self.home)

    def _fixture(self) -> None:
        for base in (self.work, self.home):
            if base.exists():
                shutil.rmtree(str(base))
            base.mkdir()
        for name, text in CANARIES.items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def prologue(self, shell: Shell) -> str:
        if shell.builtins is None:
            shell.builtins = self._builtins(shell)
        return "".join("%s() { '%s/%s' \"$@\"; }\n" % (n, self.stubs, n) for n in shell.builtins)

    def _builtins(self, shell: Shell) -> List[str]:
        probe = "for n in %s; do printf '%%s\\t%%s\\n' \"$n\" \"$(command -v \"$n\")\"; done" % " ".join(self.stub_names)
        try:
            res = subprocess.run(["/usr/bin/env", "-i", "PATH=/nonexistent", "HOME=" + str(self.home)] + shell.argv + ["-c", probe],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT, cwd=str(self.work),
                                 preexec_fn=self.preexec)
        except (OSError, subprocess.TimeoutExpired):
            return []
        names = []
        for line in res.stdout.decode("utf-8", "replace").splitlines():
            name, _, found = line.partition("\t")
            if found and not found.startswith("/") and re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", name):
                names.append(name)
        return names

    def run(self, shell: Shell, command: str) -> Dict[str, Any]:
        for entry in os.listdir(str(self.logs)):
            os.unlink(str(self.logs / entry))
        text = self.prologue(shell) + command
        argv = ["/usr/bin/env", "-i", "PATH=" + str(self.stubs), "HOME=" + str(self.home)]
        argv += ["%s=%s" % kv for kv in sorted(self.env_extra.items())]
        argv += shell.argv + ["-c", text]
        if self.bwrap:
            argv = ["bwrap", "--ro-bind", "/", "/", "--bind", str(self.tmp), str(self.tmp), "--dev", "/dev",
                    "--unshare-net"] + argv
        timed_out = False
        proc = subprocess.Popen([a.encode("utf-8", "surrogateescape") for a in argv], cwd=str(self.work),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=self.preexec)
        try:
            out, err = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            out, err = proc.communicate()
        records = []
        for entry in sorted(os.listdir(str(self.logs))):
            data = (self.logs / entry).read_bytes().split(b"\0")
            i = 0
            while i + 1 < len(data):
                try:
                    count = int(data[i])
                except ValueError:
                    break
                name = data[i + 1].decode("utf-8", "surrogateescape")
                args = [d.decode("utf-8", "surrogateescape") for d in data[i + 2:i + 2 + count]]
                records.append((name, args))
                i += 2 + count
        tree = _snapshot(self.work, self.home)
        changed = tree != self.base_tree
        if changed:
            self._fixture()
            self.base_tree = _snapshot(self.work, self.home)
        return {"records": records, "stderr": err.decode("utf-8", "replace"), "timed_out": timed_out,
                "tree_changed": changed, "tree": tree}


# -------------------------------------------------------------------------------------------- checks

def _word_ok(word: Tuple[str, int, bool], arg: str, home: str) -> bool:
    value, glob_at, _ = word
    if arg == value:
        return True
    if value.startswith("~") and (value[1:2] in ("", "/")):
        if arg == home + value[1:]:
            return True
        value = home + value[1:]
        if glob_at >= 0:
            glob_at += len(home) - 1
    if glob_at >= 0:
        # [^x] negates in bash and zsh; zsh reads **/ as zero or more directories.
        negated = value.replace("[^", "[!")
        variants = {value, negated, value.replace("**/", ""), negated.replace("**/", "")}
        return any(fnmatch.fnmatchcase(arg, v) for v in variants)
    return arg == value


def _args_match(words: List[Tuple[str, int, bool]], args: List[str], home: str,
                single: Sequence[int] = ()) -> bool:
    """A glob word takes one or more arguments (it expands to every match), any other word takes one.
    Words whose index is in `single` take exactly one even when they are globs (see _slot_words)."""
    reach = {0}
    for index, word in enumerate(words):
        nxt = set()
        for j in reach:
            if word[1] >= 0 and index not in single:
                k = j
                while k < len(args) and _word_ok(word, args[k], home):
                    k += 1
                    nxt.add(k)
            elif j < len(args) and _word_ok(word, args[j], home):
                nxt.add(j + 1)
        reach = nxt
        if not reach:
            return False
    return len(args) in reach


def _slot_words(name: str, words: List[Tuple[str, int, bool]]) -> List[int]:
    """Indexes of glob words (after the program name) that sit where the guard reads one word as the value of an
    option or as the subcommand, so that a glob expanding to several words would shift what follows:
    git: a glob before or at the subcommand, such as the value of -C (`git -C r* log` ran `git -C repo rm log`);
    date: the value of -d, -r or -f. Ordinary operands (`ls a*`) take any number of words and are not slots.
    _args_match with these forced to one word fails when only a multi-word expansion makes the match work."""
    slots: List[int] = []
    if name == "git":
        i = 0
        while i < len(words):
            value = words[i][0]
            if words[i][1] < 0 and words[i][2] and value == "-C":
                if i + 1 < len(words) and words[i + 1][1] >= 0:
                    slots.append(i + 1)
                i += 2
            elif words[i][1] < 0 and value.startswith("-"):
                i += 1
            else:
                if words[i][1] >= 0:
                    slots.append(i)
                break
    elif name == "date":
        for i, (value, glob_at, plain) in enumerate(words[:-1]):
            if glob_at < 0 and value in ("-d", "--date", "-r", "--reference", "-f", "--file") and words[i + 1][1] >= 0:
                slots.append(i + 1)
    return slots


def expected_commands(segments: List[List[Tuple[str, int, bool]]]) -> List[Tuple[Tuple[str, int, bool], List[Tuple[str, int, bool]]]]:
    out = []
    for words in segments:
        i = 0
        while i < len(words) and words[i][2] and "=" in words[i][0] and ENV_NAME_RE.match(words[i][0].partition("=")[0]):
            i += 1
        rest = words[i:]
        if rest:
            out.append((rest[0], rest[1:]))
    return out


def check_run(command: str, shell: Shell, result: Dict[str, Any], allowed: set, segments: Optional[list],
              home: str) -> List[Violation]:
    found: List[Violation] = []

    def add(kind: str, detail: str) -> None:
        found.append(Violation(shell=shell.label, kind=kind, detail=detail, command=command))

    if result["timed_out"]:
        add("timeout", "no exit within %ss" % TIMEOUT)
    for name, args in result["records"]:
        if name not in allowed:
            add("program", "executed %s %r" % (name, args))
    stderr = result["stderr"]
    if NOT_FOUND_RE.search(stderr):
        add("not-found", stderr.strip()[:300])
    if SYNTAX_RE.search(stderr):
        add("syntax", stderr.strip()[:300])
    if result["tree_changed"]:
        add("tree", "fixture tree changed")
    if segments is not None:
        expected = expected_commands(segments)
        unused = list(range(len(expected)))
        for name, args in result["records"]:
            for k in unused:
                head, rest = expected[k]
                if head[0] == name and head[1] < 0 and _args_match(rest, args, home):
                    slots = _slot_words(name, rest)
                    if slots and not _args_match(rest, args, home, slots):
                        add("glob-slot", "a glob in an option-value or subcommand slot expanded to several words: "
                                         "executed %s %r for scanned %r" % (name, args, [w[0] for w in rest]))
                    unused.remove(k)
                    break
            else:
                add("argv", "executed %s %r matches no scanned segment" % (name, args))
        # `||` right sides never run (stubs exit 0). zsh stops at a glob with no match.
        zsh_glob = shell.label == "zsh" and any(w[1] >= 0 for seg in segments for w in seg)
        # zsh also refuses a command at run time (bad pattern for an unclosed [, no matches found): safe, not run.
        zsh_glob = zsh_glob or bool(re.search(r"bad pattern|no matches found", stderr))
        if unused and "||" not in command and not zsh_glob and not result["timed_out"]:
            missing = ["%s %s" % (expected[k][0][0], [w[0] for w in expected[k][1]]) for k in unused]
            add("missing", "scanned but not executed: %s" % "; ".join(missing))
    return found


class Oracle:
    """Runs commands in real shells and reports violations. One Sandbox per thread."""

    def __init__(self, guard: Optional[Guard], shells: List[Shell], bwrap: bool = False,
                 env_extra: Optional[Dict[str, str]] = None, base: Optional[str] = None,
                 allowlist: Optional[Sequence[str]] = None,
                 child_as_gib: float = scout_limits.DEFAULT_CHILD_AS_GIB) -> None:
        self.child_as_gib = child_as_gib
        self.guard = guard
        self.shells = shells
        self.bwrap = bwrap
        self.env_extra = env_extra
        self.base = base
        names = list(allowlist) if allowlist is not None else (guard.allowlist if guard else DEFAULT_ALLOWLIST)
        self.allowed = set(names) | set(EXTRA_ALLOWED)
        self.stub_names = sorted(self.allowed | set(SENTINELS))
        self._local = threading.local()
        self._boxes: List[Sandbox] = []
        self._lock = threading.Lock()
        self.runs = {s.label: 0 for s in shells}

    def box(self) -> Sandbox:
        box = getattr(self._local, "box", None)
        if box is None:
            box = Sandbox(self.stub_names, self.allowed, self.bwrap, self.env_extra, self.base, self.child_as_gib)
            self._local.box = box
            with self._lock:
                self._boxes.append(box)
        return box

    def close(self) -> None:
        for box in self._boxes:
            box.close()
        self._boxes = []

    def run_command(self, command: str, scanner_words: bool = True) -> List[Violation]:
        """Run command in every shell and check it. Does not ask the guard: use for self-checks."""
        command = command.strip(" \t\n")
        segments = None
        if scanner_words and self.guard is not None:
            segments = self.guard.segments(command)
        box = self.box()
        found: List[Violation] = []
        if "\0" in command:
            # A NUL cannot be passed to a shell in argv, and shells disagree on it: accepting one is a finding.
            return [Violation(shell="-", kind="nul", detail="guard accepted a NUL byte", command=command)]
        for shell in self.shells:
            result = box.run(shell, command)
            with self._lock:
                self.runs[shell.label] += 1
            found += check_run(command, shell, result, self.allowed, segments, str(box.home))
        return found

    def check_accepted(self, command: str) -> List[Violation]:
        """Ask the guard first. A denied command is skipped, an accepted one is run and checked."""
        if self.guard is None:
            raise RuntimeError("no guard loaded")
        try:
            ok, _ = self.guard.check(command)
        except Exception as exc:  # a crashing guard is a finding of its own
            return [Violation(shell="-", kind="guard-crash", detail=repr(exc), command=command)]
        if not ok:
            return []
        return self.run_command(command)


# ---------------------------------------------------------------------------------------- generator

NAMES = ["ls", "cat", "head", "tail", "wc", "rg", "grep", "find", "stat", "file", "du", "pwd", "tree", "sort",
         "cut", "jq", "basename", "echo", "date", "diff", "git", "systemctl", "journalctl", "uname", "which",
         "rm", "sh", "touch", "tee", "xargs"]
BARE = ["foo", "a.txt", "src/m.py", "-n", "-la", "--color=never", "5", ".", "..", "./x", "~", "~/x", "a-b", "-",
        "--", "HEAD~3", "-delete", "-o", "-type", "f", "x=y", "a:b", "A,B", "%", "+x", "@", "^a", "-3", "--stat",
        "status", "log", "show", "diff", "branch", "-C", "sub", "--no-pager", "-name", "*.py", "-exec"]
SQ = ["'a b'", "'a|b'", "'a;b'", "'$(x)'", "'\\'", "''", "'\"'", "'#'", "'a&b'", "'a>b'", "'`x`'", "'~'", "'*'",
      "'-delete'", "'{a,b}'", "'(x)'", "'\\d'", "'a\\nb'"]
DQ = ['"a;b"', '"foo\\d"', '"中文"', '"a b"', '"\\\\"', '"\\""', '"\\$x"', '"$x"', '"`x`"', '"!x"', '"a\\\\$(rm x)"',
      '"\\\\$(rm x)"', '""', '"a|b"', '"*"', '"~"', '"\\`"', '"é\\\\"']
ESC = ["a\\ b", "\\|", "\\*", "\\'", '\\"', "\\;", "\\&", "\\>", "\\$x", "\\#", "\\~", "\\(", "\\\\", "r\\m", "\\-delete",
       "\\\n", "a\\\nb", "\\é", "\\ "]
DOLLAR = ["$X", "${X}", "$(x)", "$((1))", "$'a'", "$'\\''", '$"a"', "`x`", "${X@P}", "$[X]", "$?", "$$", "$1", "$@",
          "$_", "${X:-a}", "$'\\x41'", "$(rm x)", "`rm x`", "${X=(rm x)}", "$-"]
REDIR = [">f", "> f", ">/dev/null", "2>/dev/null", "2>&1", "<f", ">>f", "<<EOF", "<<<x", "&>f", ">|f", "<(x)", ">(x)",
         "=(x)", "1>/dev/null", ">/dev/nullfoo", "2>&1foo", "&>/dev/null", ">&2", "3>f", "2>/dev/null\n", ">/dev/null 2>&1"]
KEYWORDS = ["if", "then", "fi", "for", "do", "done", "while", "case", "esac", "time", "!", "{", "}", "function", "[[",
            "]]", "coproc", "select", "until", "in", "else", "elif", "(", ")", "((", "))", "()"]
ENVS = ["LC_ALL=C", "LANG=C", "TZ=UTC", "NO_COLOR=1", "X=1", "PATH=./x", "LD_PRELOAD=./x.so", "GIT_EXTERNAL_DIFF=./evil",
        "IFS=x", "X=-delete", "X='$(rm x)'", "LC_ALL=", "X=~", "BASH_ENV=./e", "ENV=./e", "X=a=b", "x=1"]
SEPS = [" ", " ", " ", "\n", ";", " ; ", "|", " | ", "&&", " && ", "||", " || ", "&", " & ", ";;", "\n\n", "|&", " &\n"]
GLOBS = ["*", "*.py", "?", "[a-z]*", "src/*", "*(e:'rm x':)", "**/*", "[!a]*", "[^a]*", "*(.)", "-*", "--*", "./-*",
         "{a,b}", "{-delete,-true}", "{1..3}", "a{b}", "*[", "[", "]", "~/*", "?(a)", "@(a|b)", "+(a)", "<1-3>"]
UNI = [" ", " ", "　", "\x85", " ", "​", "﻿", " ", " ", "\x1c", "\x1f", "\x0b",
       "\x0c", "\r", "\x00", "\x7f", "\x1b", "\x08", "\x9b"]
HEREDOCS = ["cat <<EOF\n$(rm x)\nEOF", "cat <<'EOF'\nrm x\nEOF", "cat <<-EOF\n\tx\n\tEOF", "cat <<EOF\nx\nEOF\nrm x",
            "cat <<A\n`rm x`\nA", "cat << 'E'\n'\nE"]
COMMENTS = ["# note", "#", "ls #'", "ls #\"", "# $(rm x)", "#!x", "ls # x\nrm x"]
MUT_CHARS = list(" \t\n;|&<>()$`'\"\\#{}[]*?~!=%^") + ["\r", "\x00", "\x7f", "é", "中", " ", " ", "\x85", "\x1b"]


def _pick(rng: random.Random, pools: Sequence[Sequence[str]]) -> str:
    return rng.choice(rng.choice(pools))


def gen_word(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.42:
        return rng.choice(BARE)
    if kind < 0.55:
        return rng.choice(SQ)
    if kind < 0.65:
        return rng.choice(DQ)
    if kind < 0.72:
        return rng.choice(ESC)
    if kind < 0.77:
        return rng.choice(DOLLAR)
    if kind < 0.82:
        return rng.choice(GLOBS)
    if kind < 0.86:
        return rng.choice(REDIR)
    if kind < 0.89:
        return rng.choice(KEYWORDS)
    if kind < 0.91:
        return rng.choice(UNI)
    # concatenation of pieces into one word
    return "".join(gen_word_simple(rng) for _ in range(rng.randint(2, 3)))


def gen_word_simple(rng: random.Random) -> str:
    return _pick(rng, (BARE, SQ, DQ, ESC, GLOBS))


def gen_segment(rng: random.Random) -> str:
    parts: List[str] = []
    for _ in range(rng.choice((0, 0, 0, 0, 1, 1, 2))):
        parts.append(rng.choice(ENVS))
    parts.append(rng.choice(NAMES) if rng.random() < 0.97 else _pick(rng, (KEYWORDS, DOLLAR, SQ)))
    for _ in range(rng.choice((0, 1, 1, 2, 2, 3, 4))):
        parts.append(gen_word(rng))
    gap = " " if rng.random() < 0.93 else rng.choice(("  ", "\t", " \t", "", " "))
    return gap.join(parts)


def generate(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.03:
        return rng.choice(HEREDOCS)
    if r < 0.06:
        return rng.choice(COMMENTS)
    if r < 0.08:
        return gen_segment(rng) + rng.choice(UNI) + gen_segment(rng)
    segs = [gen_segment(rng) for _ in range(rng.choice((1, 1, 1, 2, 2, 3, 4)))]
    out = segs[0]
    for seg in segs[1:]:
        out += rng.choice(SEPS) + seg
    if rng.random() < 0.08:
        out += rng.choice(("\n", " ", ";", " #c", "\n\n", " &", " |", " ||", " &&"))
    if rng.random() < 0.05:
        out = rng.choice(("( ", "{ ", "! ", "time ", "if ", "ls () ( rm x ); ", "function ls { rm x; }; ")) + out
    return out


def mutate(rng: random.Random, command: str) -> str:
    data = bytearray(command.encode("utf-8", "surrogateescape"))
    for _ in range(rng.randint(1, 3)):
        op = rng.random()
        pos = rng.randrange(len(data) + 1)
        piece = rng.choice(MUT_CHARS).encode("utf-8")
        if op < 0.4:
            data[pos:pos] = piece
        elif op < 0.6 and data:
            del data[min(pos, len(data) - 1)]
        elif op < 0.8 and data:
            data[min(pos, len(data) - 1)] = rng.randrange(256) if rng.random() < 0.3 else piece[0]
        elif data:
            a = min(pos, len(data) - 1)
            data[a:a] = data[a:a + rng.randint(1, 4)]
    return data.decode("utf-8", "surrogateescape")


def gen_command(rng: random.Random) -> str:
    cmd = generate(rng)
    if rng.random() < 0.3:
        cmd = mutate(rng, cmd)
    return cmd


# --------------------------------------------------------------------------------------------- driver

def run_fuzz(guard: Guard, count: int, seed: int, shells: List[Shell], bwrap: bool = False, workers: int = 0,
             env_extra: Optional[Dict[str, str]] = None, base: Optional[str] = None,
             budget: float = 0.0, child_as_gib: float = scout_limits.DEFAULT_CHILD_AS_GIB,
             min_available_gib: float = scout_limits.DEFAULT_MIN_AVAILABLE_GIB,
             floor: Optional["scout_limits.MemoryFloor"] = None) -> Dict[str, Any]:
    """Generate count strings, ask the guard, run each distinct accepted one in every shell.
    budget (seconds, 0 = none) stops starting new runs; the report says how many were skipped.
    A run is also not started while MemAvailable is under min_available_gib: those are counted apart as
    skipped_for_memory. workers defaults to min(4, cpu count)."""
    rng = random.Random(seed)
    started = time.time()
    commands = [gen_command(rng) for _ in range(count)]
    oracle = Oracle(guard, shells, bwrap=bwrap, env_extra=env_extra, base=base, child_as_gib=child_as_gib)
    floor = floor or scout_limits.MemoryFloor(min_available_gib)
    accepted: List[str] = []
    accepted_seen = set()
    violations: List[Violation] = []
    for cmd in commands:
        try:
            ok, _ = guard.check(cmd)
        except Exception as exc:
            violations.append(Violation(shell="-", kind="guard-crash", detail=repr(exc), command=cmd))
            continue
        if ok:
            accepted_seen.add(cmd)
            accepted.append(cmd)
    to_run = sorted(accepted_seen)
    workers = workers or min(4, (os.cpu_count() or 4))
    skipped = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            def job(cmd: str) -> Optional[List[Violation]]:
                if budget and time.time() - started > budget:
                    return None
                if not floor.allow():
                    return None
                return oracle.run_command(cmd)

            for found in pool.map(job, to_run):
                if found is None:
                    skipped += 1
                else:
                    violations += found
    finally:
        oracle.close()
    return {
        "guard_root": str(guard.root), "mitosis_hook": str(sys.modules["mitosis_hook"].__file__), "seed": seed,
        "tried": count, "accepted": len(accepted), "unique_accepted": len(accepted_seen),
        "skipped_for_budget": skipped - floor.skipped, "workers": workers, "child_as_gib": child_as_gib,
        **floor.report(),
        "runs": dict(oracle.runs), "bwrap": bwrap, "violation_count": len(violations),
        "violations": [dict(v) for v in violations[:2000]], "seconds": round(time.time() - started, 1),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--guard-root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--fuzz", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--shells", default="bash,dash,busybox")
    ap.add_argument("--bwrap", action="store_true")
    ap.add_argument("--workers", type=int, default=0, help="default min(4, cpu count)")
    ap.add_argument("--child-as-gib", type=float, default=scout_limits.DEFAULT_CHILD_AS_GIB,
                    help="RLIMIT_AS for spawned children in GiB, 0 disables")
    ap.add_argument("--min-available-gib", type=float, default=scout_limits.DEFAULT_MIN_AVAILABLE_GIB,
                    help="stop starting runs when MemAvailable is below this, 0 disables")
    ap.add_argument("--lc", default="", help="set LC_ALL for the shell, for example C.UTF-8")
    ap.add_argument("--tmp-base", default=None)
    ap.add_argument("--allowlist-only", action="append", default=[], metavar="COMMAND",
                    help="run COMMAND without the guard or scanner words and print violations (self-check)")
    ap.add_argument("--budget", type=float, default=0.0, help="stop starting runs after this many seconds")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    shells = parse_shells(args.shells)
    if not shells:
        print("no shell found", file=sys.stderr)
        return 2
    env_extra = {"LC_ALL": args.lc} if args.lc else None
    guard = load_guard(args.guard_root)
    if args.allowlist_only:
        oracle = Oracle(guard, shells, bwrap=args.bwrap, env_extra=env_extra, base=args.tmp_base,
                        child_as_gib=args.child_as_gib)
        try:
            bad = 0
            for cmd in args.allowlist_only:
                found = oracle.run_command(cmd, scanner_words=False)
                bad += bool(found)
                print("%s  %r  %s" % ("FLAGGED" if found else "clean  ", cmd, sorted({v["shell"] + ":" + v["kind"] for v in found})))
        finally:
            oracle.close()
        return 0
    report = run_fuzz(guard, args.fuzz, args.seed, shells, args.bwrap, args.workers, env_extra, args.tmp_base,
                      args.budget, args.child_as_gib, args.min_available_gib)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=True), encoding="utf-8")
    print("guard %s" % report["guard_root"])
    print("tried %d  accepted %d (unique %d, skipped for budget %d, skipped for memory %d)  runs %s  violations %d  %ss" % (
        report["tried"], report["accepted"], report["unique_accepted"], report["skipped_for_budget"],
        report["skipped_for_memory"], report["runs"],
        report["violation_count"], report["seconds"]))
    for v in report["violations"][:50]:
        print("  %-8s %-9s %r  %s" % (v["shell"], v["kind"], v["command"], v["detail"][:120]))
    return 1 if report["violation_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
