#!/usr/bin/env python3
"""PowerShell harness for the strict scout guard (scout_command_is_read_only(..., strict_windows=True)).

The strict mode is the character set a Codex scout gets on Windows and on a POSIX host where pwsh is
reachable: letters, digits and _ . / : = + | space tab -, nothing else. It has to be checked against a real
PowerShell, and tests/scout_oracle.py cannot do that: its stubs and shell-function prologue are POSIX sh.
This harness reuses the oracle's guard loader, scanner words, argv matcher and Violation type, and replaces
the sandbox with one that fits PowerShell.

What it does:
  1. aliases: for every program on the scout allowlist, Get-Command in the chosen shell says whether the
     name resolves to the PATH stub (an Application) or to an Alias, Cmdlet or Function. The latter
     shadow the stub. Many aliases (ls, cat, sort, echo, sleep) are removed on Linux pwsh and exist on Windows.
  2. generate: fixed PowerShell cases plus seeded fuzz of allowlisted programs and PowerShell-significant
     tokens (also strings with characters strict mode must refuse: those only test the filter).
  3. keep only strings the real guard accepts with strict_windows=True.
  4. run each as `<shell> -NoProfile -NonInteractive -Command <string>` in a fresh work directory, with PATH
     holding only logging stubs (one per allowlisted and sentinel name), HOME and TEMP in a scratch
     directory, and a timeout.

Violation classes (same vocabulary as the oracle where it fits):
  tree          a file in the work directory or the sentinel directory was created, changed or removed
  program       a stub ran whose name is not allowlisted (touch, curl, whoami, rm ...)
  argv          an allowlisted stub ran with argv that matches no scanned segment
  alias_shadow  a scanned program name resolves to an Alias, Cmdlet or Function, so the stub did not run
                (reported separately: a shadowing alias may change which options matter)
  missing       a scanned segment never ran (not reported when the command has `||`)
  not-found     the shell reported a command it could not find
  syntax        the shell reported a parse error
  timeout, nul, guard-crash

Known limit on Windows: stubs are .cmd files that call python, so argv passes through cmd.exe, which
rewrites % ^ and quotes. An argv finding with those characters on Windows may come from the stub.
Strict mode refuses those characters, so it should not show up for accepted strings.

CLI:
  python3 tests/scout_pwsh_harness.py [--fuzz N] [--seed S] [--shell pwsh|powershell] [--workers N]
                                      [--json PATH] [--self-check] [--budget SECONDS]
Exit: 0 nothing that fails the run, 1 it found something, 2 could not run (no such shell).
alias_shadow, not-found and syntax are counted and printed but do not fail a run: a program that reports
an error wrote nothing, and an alias is checked once per run against READ_ONLY_ALIAS_TARGETS instead.
--self-check runs the detection against a guard that accepts every string, on a small set, and exits 1
if it finds no violation (the harness would then be unable to catch anything).

Python 3.8, standard library only.
"""

import argparse
import concurrent.futures
import json
import os
import platform
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
import scout_oracle  # noqa: E402
from scout_oracle import Violation  # noqa: E402

WINDOWS = os.name == "nt"
TIMEOUT = 60 if WINDOWS else 30
SENTINEL_NAMES = ("touch", "curl", "wget", "whoami", "id", "pwned", "rm", "sh", "bash", "python3", "xargs", "tee",
                  "mkdir", "cp", "mv", "ni", "sc", "ri", "iex", "iwr", "start", "kill", "sleep")
CANARIES = {"canary.txt": "canary\n", "a b.txt": "canary space\n", "src/m.py": "print('m')\n"}
SENTINEL_FILE = ("sentinel", "s.txt")
NOT_FOUND_RE = re.compile(r"is not recognized|CommandNotFoundException|ObjectNotFound|not found|Cannot find path|"
                          r"cannot be found", re.I)
SYNTAX_RE = re.compile(r"ParserError|ParseException|Unexpected token|Missing |Unrecognized token|"
                       r"is not a valid|syntax", re.I)
SHADOW_TYPES = ("Alias", "Cmdlet", "Function")

# ----------------------------------------------------------------------------------------- generator

DASHES = ["\u2013", "\u2014"]
QUOTES = ["\u2018", "\u2019", "\u201c", "\u201d"]
PS_SIG = [
    "@(ls)", "$(ls)", "(ls)", "{ls}", "{ ls }", ",", "a,b", "::", "[System.IO.File]::", "System.IO.File::",
    "[IO.File]::WriteAllText", "--%", "`", "`n", "%", "?", "|", "||", "&&", "#", "<#", "#>", "=", "+", "..",
    "-Encoding", "-FilePath", "-Path", "-LiteralPath", "-Value", "-Force", "-Recurse", "-Name:x", ">", "2>", "*>",
    ">>", "<", "Tee-Object", "Out-File", "Set-Content", "Add-Content", "New-Item", "Remove-Item", "Copy-Item",
    "Move-Item", "Invoke-Expression", "Invoke-WebRequest", "Start-Process", "Get-Content", "Get-ChildItem",
    "ni", "iex", "iwr", "sc", "rm", "ri", "cp", "mv", "start", "kill", "del", "erase", "rd", "md", "cd", "sl",
    "&", ".", "./ls", "./s.ps1", "&ls", ".ls", "x=y", "$x", "$env:PATH", "${x}", "-eq", "-and", "-or", "-f",
    "-replace", "-join", "-split", "--", "-", "a:b", "C:/x", "C:x", "//x", "../sentinel/s.txt",
    "sentinel/s.txt", "canary.txt", "x.txt", "NUL", "con", "$null", "-o", "-n", "-la", "-R", "5", "1..3",
    "a+b", "+x", "x+", "ls+", "-x:y", "-Path:canary.txt", "Out-Null", "Out-String", "Select-String",
    "Get-Alias", "Set-Alias", "New-Alias", "Set-Location", "Write-Host", "Write-Output", "ForEach-Object",
    "Where-Object", "Sort-Object", "Measure-Object", "Select-Object", "foreach", "where", "select", "sort",
]
SAFE_WORDS = ["foo", "a.txt", "src/m.py", "-n", "-la", "5", ".", "..", "./x", "a-b", "-", "--", "x=y", "a:b",
              "-name", "-type", "f", "status", "log", "canary.txt", "sentinel/s.txt", "../sentinel/s.txt", "+x"]
PS_SEPS = [" ", " ", " | ", "|", " || ", "||", " && ", " & ", "; ", "\n", " , ", " # ", " > ", " 2> ", " *> ",
           " <# ", " #> "]
PROGS = ["ls", "cat", "head", "tail", "wc", "rg", "grep", "find", "stat", "file", "du", "df", "pwd", "which",
         "tree", "sort", "cut", "jq", "basename", "dirname", "realpath", "readlink", "echo", "date", "uname",
         "nl", "diff", "cmp", "md5sum", "sha256sum", "uptime"]
SHELL_PROGS = ["touch", "rm", "curl", "whoami", "tee", "ni", "iex", "iwr", "sc", "ri", "cp", "mv", "start",
               "Out-File", "Set-Content", "Tee-Object"]

FIXED = [
    "ls", "ls -la", "cat canary.txt", "echo hello", "echo a+b", "echo a=b", "echo a:b", "pwd", "date", "uname",
    "sort canary.txt", "sort -o x.txt canary.txt", "sort -Unique canary.txt", "ls | sort", "ls | cat",
    "ls | Out-File x.txt", "ls | Tee-Object x.txt", "ls | tee x.txt", "ls | Set-Content x.txt",
    "echo hi | Out-File -FilePath x.txt", "echo hi | Out-File -Encoding utf8 x.txt",
    "echo hi | Set-Content -Path x.txt", "echo hi | Add-Content x.txt", "echo hi | sc x.txt",
    "echo hi | ni x.txt", "ni x.txt", "sc x.txt hi", "rm canary.txt", "ri canary.txt", "cp canary.txt x.txt",
    "mv canary.txt x.txt", "touch x.txt", "curl http://127.0.0.1:9", "whoami", "start whoami", "iex whoami",
    "iwr http://127.0.0.1:9", "ls || touch x.txt", "ls || rm canary.txt", "ls | iex", "ls | whoami",
    "ls .. ", "ls ../sentinel", "ls ../sentinel/s.txt", "cat ../sentinel/s.txt", "cat sentinel/s.txt",
    "ls -Path canary.txt", "ls -Path:canary.txt", "ls -LiteralPath canary.txt", "ls -Force -Recurse",
    "ls -Name", "ls -r", "ls -fo", "cat -Encoding utf8 canary.txt", "cat -Raw canary.txt",
    "cat -TotalCount 1 canary.txt", "echo -n hi", "echo -NoNewline hi", "sleep 1", "sleep -s 1",
    "System.IO.File::WriteAllText x.txt y", "System.IO.File::Delete canary.txt", "Out-File x.txt",
    "Set-Content x.txt hi", "Tee-Object x.txt", "ls ::", "ls ::x", "ls a::b", "cat ::x", "ls .", "ls ..",
    "ls ./", "ls ../", "ls ./canary.txt", ". ls", ". ./ls", "& ls", "&ls", ".ls", "./ls", "./touch x.txt",
    ". touch x.txt", ". ./touch x.txt", "ls ./ls", "echo --%", "echo -- x", "ls x=y", "x=y ls", "x=y",
    "ls=y", "ls = y", "ls + 1", "ls +x", "echo 1 + 1", "echo 1..3", "echo -1..-3", "ls a..b", "ls ..\\x",
    "ls:x", "ls: x", "C:ls", "C:/Windows/System32/whoami.exe", "./whoami", "ls -Encoding", "ls -FilePath x.txt",
    "cat -FilePath x.txt", "echo x > x.txt", "echo x 2> x.txt", "echo x *> x.txt", "echo x >> x.txt",
    "echo `touch x.txt`", "echo $(touch x.txt)", "echo @(touch x.txt)", "echo {touch x.txt}", "(ls)", "{ls}",
    "ls, touch x.txt", "ls,touch", "echo a,b", "echo a % b", "ls | % { touch x.txt }", "ls | ? { 1 }",
    "ls # touch x.txt", "ls <# touch x.txt #>", "ls && touch x.txt", "ls & touch x.txt", "ls; touch x.txt",
    "ls `\ntouch x.txt", "echo --% touch x.txt", "ls --% & touch x.txt",
    "ls \u2013la", "ls \u2014la", "echo \u2018a\u2019", "echo \u201ca\u201d", "echo \u2018;touch x.txt;\u2019",
    "ls \u2013Path canary.txt", "cat \u2013Raw canary.txt", "echo \u2019 ; touch x.txt ; \u2018",
    "echo \u201d ; touch x.txt ; \u201c", "ls \u2013 la", "echo a \u2013 b",
    "echo hi | Out-File \u2013FilePath x.txt", "echo hi | Out-File \u2014FilePath x.txt",
    "ls | Out-Null", "ls | Out-String", "ls | Select-String x", "ls | Sort-Object", "ls | Measure-Object",
    "ls | ForEach-Object touch", "ls | foreach touch", "ls | where x", "ls | select x", "Get-Content canary.txt",
    "Get-ChildItem", "Get-Alias", "Set-Alias ls touch", "New-Alias x touch", "Set-Location ..", "cd ..",
    "sl ..", "ls | sort | cat | head | tail | wc", "sort | sort", "ls ||", "ls |", "| ls", "ls || ls",
    "ls -x:y", "date -d x", "date -Format x", "date -Date x", "date -Set x", "Get-Date",
    "echo $x", "echo $env:PATH", "echo ${x}", "echo @x", "echo [x]", "echo 'x'", 'echo "x"', "ls *", "ls ?",
]


def _word(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.45:
        return rng.choice(SAFE_WORDS)
    if r < 0.85:
        return rng.choice(PS_SIG)
    if r < 0.92:
        return rng.choice(DASHES) + rng.choice(("la", "Path", "Raw", "o", "n", "FilePath", "Encoding"))
    if r < 0.96:
        return rng.choice(QUOTES) + rng.choice(("a", "x;touch x", "")) + rng.choice(QUOTES)
    return rng.choice(PS_SIG) + rng.choice(SAFE_WORDS)


def _segment(rng: random.Random) -> str:
    r = rng.random()
    parts: List[str] = []
    if r < 0.06:
        parts.append(rng.choice((".", "&", "&&", ".\\", "./")))
    if rng.random() < 0.94:
        parts.append(rng.choice(PROGS))
    else:
        parts.append(rng.choice(SHELL_PROGS))
    for _ in range(rng.choice((0, 1, 1, 2, 2, 3, 4))):
        parts.append(_word(rng))
    return " ".join(parts)


def gen_command(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.02:
        return scout_oracle.gen_command(rng)
    if r < 0.10:
        return rng.choice(FIXED)
    segs = [_segment(rng) for _ in range(rng.choice((1, 1, 2, 2, 3)))]
    out = segs[0]
    for seg in segs[1:]:
        out += rng.choice(PS_SEPS) + seg
    if rng.random() < 0.15:
        out = scout_oracle.mutate(rng, out)
    return out


def generate(count: int, seed: int) -> List[str]:
    rng = random.Random(seed)
    out = list(FIXED)
    out += [gen_command(rng) for _ in range(count)]
    return out


# ---------------------------------------------------------------------------------------------- shell

class PsShell:
    def __init__(self, label: str, exe: str) -> None:
        self.label = label
        self.exe = exe
        self.version = ""

    def argv(self, command: str) -> List[str]:
        return [self.exe, "-NoProfile", "-NonInteractive", "-Command", command]


def find_shell(name: str) -> Optional[PsShell]:
    exe = shutil.which(name)
    if not exe:
        return None
    shell = PsShell(name, exe)
    try:
        res = subprocess.run(shell.argv("$PSVersionTable.PSVersion.ToString()"), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        shell.version = res.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return shell


class Sandbox:
    """stubs/ log/ work/ sentinel/ home/ in one temp tree. One per worker thread; work and sentinel are
    recreated before every run."""

    def __init__(self, names: Sequence[str], base: Optional[str] = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="scout-pwsh-", dir=base)).resolve()
        self.stubs = self.tmp / "stubs"
        self.logs = self.tmp / "log"
        self.work = self.tmp / "work"
        self.sentinel = self.tmp / "sentinel"
        self.home = self.tmp / "home"
        self.stubs.mkdir()
        self.logs.mkdir()
        self.home.mkdir()
        (self.home / "tmp").mkdir()
        self.names = sorted(set(names))
        if WINDOWS:
            helper = self.tmp / "stub.py"
            helper.write_text(
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "rec = b''.join(a.encode('utf-8', 'surrogateescape') + b'\\0' for a in [str(len(args) - 1)] + args)\n"
                "with open(os.path.join(%r, str(os.getpid())), 'ab') as fh:\n"
                "    fh.write(rec)\n" % str(self.logs), encoding="utf-8")
            for name in self.names:
                (self.stubs / (name + ".cmd")).write_text(
                    '@echo off\r\n"%s" "%s" %s %%*\r\nexit /b 0\r\n' % (sys.executable, helper, name), encoding="ascii")
        else:
            for name in self.names:
                path = self.stubs / name
                path.write_text("#!/bin/sh\nprintf '%s\\0' \"$#\" '" + name + "' \"$@\" >> '" + str(self.logs)
                                + "'/\"$$\"\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
        self.reset()

    def reset(self) -> None:
        for base in (self.work, self.sentinel):
            if base.exists():
                shutil.rmtree(str(base), ignore_errors=True)
            base.mkdir()
        for name, text in CANARIES.items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        (self.sentinel / SENTINEL_FILE[1]).write_text("sentinel\n", encoding="utf-8")
        for entry in os.listdir(str(self.logs)):
            try:
                os.unlink(str(self.logs / entry))
            except OSError:
                pass
        self.base_tree = scout_oracle._snapshot(self.work, self.sentinel)

    def close(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def env(self) -> Dict[str, str]:
        env = {"PATH": str(self.stubs), "HOME": str(self.home), "TMPDIR": str(self.home / "tmp"),
               "TEMP": str(self.home / "tmp"), "TMP": str(self.home / "tmp"), "USERPROFILE": str(self.home),
               "XDG_CACHE_HOME": str(self.home / ".cache"), "XDG_DATA_HOME": str(self.home / ".local" / "share"),
               "XDG_CONFIG_HOME": str(self.home / ".config"), "POWERSHELL_TELEMETRY_OPTOUT": "1",
               "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "POWERSHELL_UPDATECHECK": "Off",
               "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1", "NO_COLOR": "1"}
        if WINDOWS:
            for key in ("SystemRoot", "windir", "ComSpec", "PATHEXT", "SystemDrive", "ProgramFiles", "ProgramData",
                        "ProgramFiles(x86)", "ALLUSERSPROFILE", "PSModulePath", "COMPUTERNAME", "OS",
                        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "APPDATA", "LOCALAPPDATA"):
                if key in os.environ:
                    env[key] = os.environ[key]
            env["APPDATA"] = str(self.home)
            env["LOCALAPPDATA"] = str(self.home)
        return env

    def run(self, shell: PsShell, command: str, timeout: int = TIMEOUT) -> Dict[str, Any]:
        self.reset()
        kwargs: Dict[str, Any] = {}
        if not WINDOWS:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(shell.argv(command), cwd=str(self.work), env=self.env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            out, err = proc.communicate()
        records: List[Tuple[str, List[str]]] = []
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
        tree = scout_oracle._snapshot(self.work, self.sentinel)
        return {"records": records, "stderr": err.decode("utf-8", "replace"), "stdout": out.decode("utf-8", "replace"),
                "timed_out": timed_out, "tree_changed": tree != self.base_tree}


# ---------------------------------------------------------------------------------------------- aliases

# What an allowlisted name may resolve to in PowerShell. None of these write, whatever options they get.
READ_ONLY_ALIAS_TARGETS = {"Get-ChildItem", "Get-Content", "Write-Output", "Get-Location", "Sort-Object", "Compare-Object"}
INFO_KINDS = {"alias_shadow", "not-found", "syntax"}


def probe_aliases(shell: PsShell, box: Sandbox, names: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """name -> {type, source} as Get-Command resolves it with only the stubs on PATH."""
    script = ("foreach ($n in @(%s)) { $c = Get-Command $n -ErrorAction SilentlyContinue | Select-Object -First 1; "
              "if ($c) { \"$n`t$($c.CommandType)`t$($c.Source)`t$($c.ResolvedCommandName)\" } else { \"$n`tNone`t\" } }"
              % ",".join("'%s'" % n for n in names))
    res = box.run(shell, script)
    out: Dict[str, Dict[str, str]] = {}
    for line in res["stdout"].splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) >= 2:
            out[parts[0]] = {"type": parts[1], "source": parts[2] if len(parts) > 2 else "",
                             "target": parts[3] if len(parts) > 3 else ""}
    return out


def shadows_of(table: Dict[str, Dict[str, str]]) -> List[str]:
    return sorted(n for n, v in table.items() if v["type"] in SHADOW_TYPES)


# ---------------------------------------------------------------------------------------------- checks

def check_run(command: str, shell: PsShell, result: Dict[str, Any], allowed: set, segments: Optional[list],
              shadows: set, home: str) -> List[Violation]:
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
        add("tree", "work or sentinel directory changed")
    if segments is None:
        return found
    expected = scout_oracle.expected_commands(segments)
    shadowed = sorted({e[0][0] for e in expected if e[0][0] in shadows})
    for name in shadowed:
        add("alias_shadow", "%s resolves to an alias, cmdlet or function here, not the stub" % name)
    expected = [e for e in expected if e[0][0] not in shadows]
    unused = list(range(len(expected)))
    for name, args in result["records"]:
        if name not in allowed:
            continue
        for k in unused:
            head, rest = expected[k]
            if head[0] == name and scout_oracle._args_match(rest, args, home):
                unused.remove(k)
                break
        else:
            if name not in shadows:
                add("argv", "executed %s %r matches no scanned segment" % (name, args))
    if unused and "||" not in command and not result["timed_out"] and not shadowed:
        missing = ["%s %s" % (expected[k][0][0], [w[0] for w in expected[k][1]]) for k in unused]
        add("missing", "scanned but not executed: %s" % "; ".join(missing))
    return found


class Permissive:
    """A guard that accepts every string, for --self-check. Scanner words come from the real guard when it can
    read the string; argv is not compared otherwise."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.allowlist = real.allowlist
        self.allowed = real.allowed

    def check(self, command: str, strict_windows: bool = False) -> Tuple[bool, str]:
        return True, "permissive"

    def segments(self, command: str) -> Optional[list]:
        return self.real.segments(command)


class Harness:
    def __init__(self, guard: Any, shell: PsShell, shadows: Sequence[str], base: Optional[str] = None) -> None:
        self.guard = guard
        self.shell = shell
        self.shadows = set(shadows)
        self.base = base
        self.names = sorted(set(guard.allowlist) | set(scout_oracle.EXTRA_ALLOWED) | set(SENTINEL_NAMES))
        self._local = threading.local()
        self._boxes: List[Sandbox] = []
        self._lock = threading.Lock()
        self.runs = 0

    def box(self) -> Sandbox:
        box = getattr(self._local, "box", None)
        if box is None:
            box = Sandbox(self.names, self.base)
            self._local.box = box
            with self._lock:
                self._boxes.append(box)
        return box

    def close(self) -> None:
        for box in self._boxes:
            box.close()
        self._boxes = []

    def run_command(self, command: str) -> List[Violation]:
        command = command.strip(" \t\n")
        if "\0" in command:
            return [Violation(shell=self.shell.label, kind="nul", detail="guard accepted a NUL byte", command=command)]
        box = self.box()
        segments = self.guard.segments(command)
        result = box.run(self.shell, command)
        with self._lock:
            self.runs += 1
        return check_run(command, self.shell, result, self.guard.allowed, segments, self.shadows, str(box.home))


def run_fuzz(guard: Any, shell: PsShell, count: int, seed: int, shadows: Sequence[str], workers: int,
             budget: float = 0.0, min_available_gib: float = scout_limits.DEFAULT_MIN_AVAILABLE_GIB,
             base: Optional[str] = None, strict: bool = True) -> Dict[str, Any]:
    started = time.time()
    commands = generate(count, seed)
    accepted: List[str] = []
    seen = set()
    violations: List[Violation] = []
    for cmd in commands:
        try:
            ok, _ = guard.check(cmd, strict_windows=strict)
        except Exception as exc:
            violations.append(Violation(shell=shell.label, kind="guard-crash", detail=repr(exc), command=cmd))
            continue
        if ok and cmd.strip(" \t\n") not in seen:
            seen.add(cmd.strip(" \t\n"))
            accepted.append(cmd.strip(" \t\n"))
    harness = Harness(guard, shell, shadows, base)
    floor = scout_limits.MemoryFloor(min_available_gib)
    skipped = 0

    def job(cmd: str) -> Optional[List[Violation]]:
        if budget and time.time() - started > budget:
            return None
        if not floor.allow():
            return None
        return harness.run_command(cmd)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for found in pool.map(job, accepted):
                if found is None:
                    skipped += 1
                else:
                    violations += found
    finally:
        harness.close()
    by_class: Dict[str, int] = {}
    for v in violations:
        by_class[v["kind"]] = by_class.get(v["kind"], 0) + 1
    return {"generated": len(commands), "accepted": len(accepted), "run": harness.runs,
            "skipped_for_budget": skipped - floor.skipped, "skipped_for_memory": floor.skipped,
            "violation_count": len(violations), "violations_by_class": by_class,
            "violations": [dict(v) for v in violations[:2000]], "seconds": round(time.time() - started, 1)}


SELF_CHECK_COMMANDS = ["touch pwned.txt", "rm canary.txt", "whoami", "curl http://127.0.0.1:9",
                       "ni pwned.txt", "sc pwned.txt hi", "echo hi | Out-File x.txt", "ls | Tee-Object t.txt",
                       "Set-Content x.txt hi", "echo hi > x.txt", "ls ../sentinel/s.txt | Remove-Item"]


def self_check(real_guard: Any, shell: PsShell, shadows: Sequence[str], base: Optional[str]) -> Dict[str, Any]:
    harness = Harness(Permissive(real_guard), shell, shadows, base)
    found: List[Violation] = []
    try:
        for cmd in SELF_CHECK_COMMANDS:
            found += harness.run_command(cmd)
    finally:
        harness.close()
    by_class: Dict[str, int] = {}
    for v in found:
        by_class[v["kind"]] = by_class.get(v["kind"], 0) + 1
    return {"tried": len(SELF_CHECK_COMMANDS), "violation_count": len(found), "violations_by_class": by_class,
            "violations": [dict(v) for v in found]}


# ------------------------------------------------------------------------------------------------ main

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--guard-root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--fuzz", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--shell", choices=("pwsh", "powershell"), default="pwsh")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--json", default="")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--budget", type=float, default=0.0, help="stop starting runs after this many seconds")
    ap.add_argument("--min-available-gib", type=float, default=scout_limits.DEFAULT_MIN_AVAILABLE_GIB)
    ap.add_argument("--tmp-base", default=None)
    args = ap.parse_args(argv)

    shell = find_shell(args.shell)
    if shell is None:
        print("cannot run: %s was not found on PATH (or did not start); install PowerShell and retry" % args.shell,
              file=sys.stderr)
        return 2
    guard = scout_oracle.load_guard(args.guard_root)
    print("shell %s %s on %s %s" % (shell.label, shell.version, platform.system(), platform.release()))

    probe_box = Sandbox(sorted(set(guard.allowlist) | set(SENTINEL_NAMES)), args.tmp_base)
    try:
        table = probe_aliases(shell, probe_box, sorted(guard.allowlist))
    finally:
        probe_box.close()
    shadows = shadows_of(table)
    stubbed = sorted(n for n, v in table.items() if v["type"] == "Application")
    other = sorted(n for n, v in table.items() if v["type"] not in SHADOW_TYPES + ("Application",))
    print("aliases on %s: %d of %d allowlisted names shadow the stub: %s" % (
        platform.system(), len(shadows), len(guard.allowlist), " ".join("%s=%s" % (n, table[n]["type"]) for n in shadows) or "-"))
    print("  resolve to the stub: %d   other (missing or unknown): %s" % (len(stubbed), " ".join(other) or "-"))

    if args.self_check:
        report = self_check(guard, shell, shadows, args.tmp_base)
        report["aliases"] = table
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=True), encoding="utf-8")
        print("self-check: tried %d  violations %d  %s" % (report["tried"], report["violation_count"],
                                                            report["violations_by_class"]))
        for v in report["violations"][:30]:
            print("  %-9s %r  %s" % (v["kind"], v["command"], v["detail"][:100]))
        if not report["violation_count"]:
            print("self-check FAILED: a guard that accepts everything produced no violation, so this harness "
                  "cannot catch anything", file=sys.stderr)
            return 1
        return 0

    report = run_fuzz(guard, shell, args.fuzz, args.seed, shadows, args.workers, args.budget,
                      args.min_available_gib, args.tmp_base)
    report.update({"shell": shell.label, "shell_version": shell.version, "platform": platform.platform(),
                   "seed": args.seed, "aliases": table, "alias_shadowed": shadows})
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=True), encoding="utf-8")
    print("generated %d  accepted %d  run %d  (skipped: budget %d, memory %d)  violations %d %s  aliases %d  %ss" % (
        report["generated"], report["accepted"], report["run"], report["skipped_for_budget"],
        report["skipped_for_memory"], report["violation_count"], report["violations_by_class"], len(shadows),
        report["seconds"]))
    for v in report["violations"][:200]:
        print("  %-12s %r  %s" % (v["kind"], v["command"], v["detail"][:140]))
    unsafe = sorted("%s=%s" % (n, table[n].get("target")) for n in shadows
                    if table[n].get("target") not in READ_ONLY_ALIAS_TARGETS)
    failing = {k: c for k, c in report["violations_by_class"].items() if k not in INFO_KINDS}
    print("failing classes: %s  unsafe aliases: %s" % (failing or "none", unsafe or "none"))
    return 1 if failing or unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
