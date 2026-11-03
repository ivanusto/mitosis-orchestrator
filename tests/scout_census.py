#!/usr/bin/env python3
"""Option-level census of false allows in scout_command_is_read_only.

Local tool, not part of the unit test suite. Python 3.8, stdlib only.

For every program in SCOUT_READ_ONLY_COMMANDS and every git subcommand in SCOUT_GIT_SUBCOMMANDS it
  1. derives the option list from the installed tool (--help, -h, man page, git <sub> -h, git man page,
     and for find the binary's own string table), not from the guard's tables;
  2. builds command forms, keeps those the guard accepts;
  3. runs each accepted form with the REAL binary under strace inside bwrap, in a throwaway fixture repo;
  4. classifies the run: VIOLATION, BENIGN-INDEX, NETWORK, TIMEOUT, CLEAN.
systemctl and journalctl are never run (only --help is read) and are compared with the guard statically.

    python3 tests/scout_census.py --guard-root <dir> --out <dir> [--programs ls,git:log] [--jobs 8]
    python3 tests/scout_census.py --wrappers --guard-root <dir> --out <dir> [--programs 'find(bfs),grep(ugrep)']
    python3 tests/scout_census.py --merge <dir> <dir> ... --out <dir>

Wrapper mode (--wrappers) adds the programs Claude Code's Bash tool puts behind shell functions: find runs bfs and
grep runs ugrep, both embedded in the claude binary ($CLAUDE_CODE_EXECPATH or ~/.local/bin/claude), and rg runs the
embedded ripgrep. They are counted as separate programs, "find(bfs)", "grep(ugrep)" and "rg(claude)". Each is run as
exec -a <name> <claude> <wrapper prefix> <args>; the grep wrapper's fallback to GNU grep for some options is left
out on purpose, so the options it would divert are still covered.

<dir> for --guard-root holds scripts/enforce-orchestrator.py (the repo root, or an extracted archive).
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

TRACE = ("execve,execveat,openat,creat,unlink,unlinkat,rename,renameat,renameat2,mkdir,mkdirat,rmdir,"
         "symlink,symlinkat,link,linkat,chmod,fchmod,fchmodat,truncate,ftruncate,clock_settime,"
         "settimeofday,connect")
SYSPATH = "/usr/local/bin:/usr/bin:/bin"
EVIL = "./evil"
SYSTEMD = ("systemctl", "journalctl")

TEMPLATES = {
    "ls": ["ls {F} f.txt"], "cat": ["cat {F} f.txt"], "head": ["head {F} f.txt"], "tail": ["tail {F} f.txt"],
    "wc": ["wc {F} f.txt"], "rg": ["rg {F} foo ."], "grep": ["grep {F} foo f.txt"],
    "egrep": ["egrep {F} foo f.txt"], "fgrep": ["fgrep {F} foo f.txt"],
    "find": ["find . {F}", "find {F} ."], "stat": ["stat {F} f.txt"], "file": ["file {F} f.txt"],
    "du": ["du {F} ."], "df": ["df {F} ."], "pwd": ["pwd {F}"], "which": ["which {F} ls"],
    "tree": ["tree {F} ."], "sort": ["sort {F} f.txt"], "cut": ["cut {F} f.txt"],
    "jq": ["jq {F} . data.json"], "basename": ["basename {F} f.txt"], "dirname": ["dirname {F} f.txt"],
    "realpath": ["realpath {F} f.txt"], "readlink": ["readlink {F} f.txt"], "echo": ["echo {F} hi"],
    "date": ["date {F}"], "uname": ["uname {F}"], "nl": ["nl {F} f.txt"], "diff": ["diff {F} f.txt g.txt"],
    "cmp": ["cmp {F} f.txt g.txt"], "md5sum": ["md5sum {F} f.txt"], "sha256sum": ["sha256sum {F} f.txt"],
    "uptime": ["uptime {F}"],
}
GIT_TEMPLATES = {
    "status": "git status {F}", "log": "git log {F}", "show": "git show {F}", "diff": "git diff {F}",
    "blame": "git blame {F} f.txt", "branch": "git branch {F}", "rev-parse": "git rev-parse {F}",
    "ls-files": "git ls-files {F}", "ls-tree": "git ls-tree {F} HEAD", "grep": "git grep {F} foo",
    "describe": "git describe {F}", "shortlog": "git shortlog {F}", "reflog": "git reflog {F}",
    "remote": "git remote {F}", "tag": "git tag {F}", "config": "git config --list {F}",
}
# Options every tool may take without listing them in its own help (git wraps --help into man).
UNIVERSAL = set(["--help", "-h", "--version", "-V", "--usage"])
SHELL_SCRIPT_OK = ("sh", "dash", "bash")
# label -> (guard program name, argv0, arguments the shell function puts before the user's arguments)
WRAPPERS = {
    "find(bfs)": ("find", "bfs", ["-S", "dfs", "-regextype", "findutils-default"]),
    "grep(ugrep)": ("grep", "ugrep", ["-G", "--ignore-files", "--hidden", "-I", "--exclude-dir=.git",
                                      "--exclude-dir=.svn", "--exclude-dir=.hg", "--exclude-dir=.bzr",
                                      "--exclude-dir=.jj", "--exclude-dir=.sl"]),
    "rg(claude)": ("rg", "rg", []),
}


# ----------------------------------------------------------------------------------------------
# guard loading

def load_guard(root):
    path = os.path.join(os.path.abspath(root), "scripts", "enforce-orchestrator.py")
    scripts = os.path.dirname(path)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("census_guard_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------------------------
# tools and versions

def sysrun(argv, timeout=20, env=None, text=True):
    e = dict(os.environ)
    e.update({"LC_ALL": "C", "LANG": "C", "PATH": SYSPATH, "TERM": "dumb", "MANWIDTH": "200",
              "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1"})
    if env:
        e.update(env)
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=e)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "ERR %r" % (exc,)


def find_rg():
    cands = []
    if os.environ.get("RG_BIN"):
        cands.append(os.environ["RG_BIN"])
    w = shutil.which("rg", path=SYSPATH)
    if w:
        cands.append(w)
    home = os.path.expanduser("~")
    for pat in (home + "/.cache/opencode/bin/rg", home + "/.cargo/bin/rg", home + "/.local/bin/rg",
                home + "/dsh-app/node_modules/@vscode/ripgrep-*/bin/rg", "/opt/*/bin/rg", "/usr/local/*/rg"):
        cands.extend(sorted(glob.glob(pat))[:5])
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            rc, out = sysrun([c, "--version"], timeout=10)
            if rc == 0 and out.startswith("ripgrep"):
                return c
    return None


def resolve_binaries(progs):
    bins = {}
    for p in progs:
        if p == "rg":
            bins[p] = find_rg()
        else:
            bins[p] = shutil.which(p, path=SYSPATH)
    return bins


def versions(bins):
    out = {}
    for p in ("rg", "git", "find", "tree", "file", "jq", "ls", "sort", "grep", "diff", "date"):
        b = bins.get(p) or shutil.which(p, path=SYSPATH)
        if not b:
            out[p] = "not installed"
            continue
        rc, text = sysrun([b, "--version"], timeout=10)
        out[p] = (text.splitlines() or [""])[0] + "  [" + b + "]"
    out["coreutils"] = out.get("ls", "")
    for p in SYSTEMD:
        b = shutil.which(p, path=SYSPATH)
        out[p] = "present at %s (only --help read)" % b if b else "not installed"
    rc, text = sysrun(["bwrap", "--version"])
    out["bwrap"] = text.strip()
    rc, text = sysrun(["strace", "-V"])
    out["strace"] = (text.splitlines() or [""])[0]
    out["python"] = sys.version.split()[0]
    return out


def find_claude():
    cands = [os.environ.get("CLAUDE_CODE_EXECPATH", ""), os.path.expanduser("~/.local/bin/claude")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.realpath(c)
    return None


def sysrun_argv0(claude, argv0, args, timeout=20):
    return sysrun(["bash", "-c", 'exec -a "$0" "$@"', argv0, claude] + list(args), timeout=timeout)


# ----------------------------------------------------------------------------------------------
# option enumeration

_LINE_OPT = re.compile(r"^\s{1,40}(-.*)$")
_TOKEN = re.compile(r"(?<![\w/.\-=<\[])(--?[A-Za-z0-9][\w\-]*(?:\[[^\]\s]*\])?)")


def _norm(tok):
    """Expand one raw token into option names. Returns a list of strings that start with -."""
    tok = tok.strip().rstrip(",.;:)")
    tok = re.sub(r"[=<\[{].*$", "", tok) if not tok.startswith("--[no-]") else tok
    if tok.startswith("--[no-]"):
        base = tok[len("--[no-]"):]
        base = re.sub(r"[=<\[{].*$", "", base)
        return ["--" + base, "--no-" + base] if base else []
    if tok.startswith("--"):
        return [tok] if len(tok) > 2 else []
    if tok.startswith("-") and len(tok) >= 2:
        return [tok]
    return []


def parse_options(text):
    """Return (listed, inline). listed = options at the start of an indented line; inline = any token."""
    listed, inline = set(), set()
    for line in text.splitlines():
        m = _LINE_OPT.match(line)
        if m:
            body = m.group(1)
            head = re.split(r"\s{2,}", body)[0]
            for part in re.split(r"[,\s]+(?:or\s+)?", head):
                if part.startswith("-"):
                    for t in _norm(part):
                        listed.add(t)
        for m2 in _TOKEN.finditer(line):
            for t in _norm(m2.group(1)):
                inline.add(t)
    return listed, inline


def binary_strings_options(path):
    """Option-looking NUL-terminated strings in an executable (for find: -exec, -fprint, ...)."""
    found = set()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return found
    for m in re.finditer(rb"(?<![\x21-\x7e])(-[a-z][a-z0-9_]{2,24})(?![\x21-\x7e])", data):
        found.add(m.group(1).decode())
    return found


def classify(options):
    longs, shorts, words = set(), set(), set()
    for o in options:
        if o.startswith("--"):
            longs.add(o[2:])
        elif len(o) == 2:
            shorts.add(o)
        elif re.match(r"^-[A-Za-z][A-Za-z0-9_]+$", o):
            words.add(o)
    longs = set(l for l in longs if re.match(r"^[A-Za-z0-9][\w\-]*$", l))
    return longs, shorts, words


def enumerate_program(prog, binpath):
    texts = {}
    if not binpath:
        return None
    for name, argv in (("help", [binpath, "--help"]),):
        rc, out = sysrun(argv)
        texts[name] = out
    if len(parse_options(texts["help"])[0]) < 3:
        rc, out = sysrun([binpath, "-h"])
        texts["help-h"] = out
    rc, out = sysrun(["man", "-P", "cat", os.path.basename(binpath)])
    if rc == 0 and "NAME" in out:
        texts["man"] = out
    listed, inline = set(), set()
    for t in texts.values():
        a, b = parse_options(t)
        listed |= a
        inline |= b
    extra = set()
    if prog == "find":
        extra = binary_strings_options(binpath)
    allopts = listed | inline | extra | UNIVERSAL
    return {"sources": sorted(texts), "listed": len(listed), "inline": len(inline), "binary_strings": len(extra),
            "options": sorted(allopts)}


def enumerate_git_sub(gitbin, sub):
    texts = {}
    rc, out = sysrun([gitbin, sub, "-h"])
    texts["-h"] = out
    rc, out = sysrun(["man", "-P", "cat", "git-" + sub])
    if rc == 0 and "NAME" in out:
        texts["man"] = out
    rc, out = sysrun([gitbin, "help", "-m", sub])
    listed, inline = set(), set()
    for t in texts.values():
        a, b = parse_options(t)
        listed |= a
        inline |= b
    return {"sources": sorted(texts), "listed": len(listed), "inline": len(inline), "binary_strings": 0,
            "options": sorted(listed | inline | UNIVERSAL)}


_BRACKET = re.compile(r"^-\[([A-Za-z]+)\]([a-z]+)$")


def _expand_bfs(tok):
    """bfs prints -[aBcm]min and -newerXY as patterns; return the concrete option names."""
    m = _BRACKET.match(tok)
    if m:
        return ["-" + c + m.group(2) for c in m.group(1)]
    if tok == "-newerXY":
        return ["-newer" + x + y for x in "aBcm" for y in "aBcmt"]
    if tok == "-ON":
        return ["-O0", "-O1", "-O2", "-O3", "-O4"]
    if tok == "-jN":
        return ["-j1", "-j4"]
    return None


def enumerate_wrapper(label, claude):
    """Options of bfs (-help) or ugrep (--help and every --help WHAT) or the embedded ripgrep (--help)."""
    prog, argv0, _prefix = WRAPPERS[label]
    texts = {}
    if argv0 == "bfs":
        rc, out = sysrun_argv0(claude, argv0, ["-help"])
        texts["-help"] = out
        raw = []
        for line in out.splitlines():
            if re.match(r"^\s{1,40}-", line):
                raw.extend(t for t in re.split(r"[,\s]+", line.strip()) if t.startswith("-"))
        expanded = set()
        for t in raw:
            e = _expand_bfs(t)
            if e:
                expanded.update(e)
    else:
        rc, out = sysrun_argv0(claude, argv0, ["--help"])
        texts["--help"] = out
        expanded = set()
        a, b = parse_options(out)
        for sub in sorted(set(x[2:] for x in a | b if x.startswith("--"))) + ["format", "regex", "globs", "fuzzy"]:
            rc2, out2 = sysrun_argv0(claude, argv0, ["--help", sub])
            if rc2 == 0:
                texts["--help " + sub] = out2
        if argv0 == "ugrep":
            expanded.update("-" + str(d) for d in range(1, 10))  # -NUM context shorthand
    listed, inline = set(), set()
    for t in texts.values():
        a, b = parse_options(t)
        listed |= a
        inline |= b
    allopts = listed | inline | expanded | UNIVERSAL
    if argv0 == "bfs":
        allopts |= set(["-help", "-version"])
    return {"sources": sorted(texts), "listed": len(listed), "inline": len(inline), "binary_strings": len(expanded),
            "options": sorted(allopts)}


# ----------------------------------------------------------------------------------------------
# form generation

def forms_for(opts, extra_word_forms=True):
    """Return list of (form_string, option, kind). kind says how the option name was spelled."""
    longs, shorts, words = classify(opts)
    out = []
    for l in sorted(longs):
        o = "--" + l
        out.append((o, o, "long"))
        out.append((o + "=" + EVIL, o, "long"))
        out.append((o + " " + EVIL, o, "long"))
    for s in sorted(shorts):
        out.append((s, s, "short"))
        out.append((s + " " + EVIL, s, "short"))
        out.append((s + EVIL, s, "short"))
        out.append((s + "evil", s, "short"))
    for w in sorted(words):
        out.append((w, w, "word"))
        out.append((w + " " + EVIL, w, "word"))
    return out, longs


def abbreviation_forms(longs, accepted_fn):
    """Unique getopt prefixes of long options. A prefix form is kept when the guard accepts it and either
    the guard rejects the full name or the prefix is the shortest unique one."""
    out = []
    for l in sorted(longs):
        shortest = None
        uniq = []
        for k in range(1, len(l)):
            p = l[:k]
            if sum(1 for x in longs if x.startswith(p)) == 1:
                uniq.append(p)
        if uniq:
            shortest = uniq[0]
        for p in uniq:
            for suffix in ("", "=" + EVIL, " " + EVIL):
                f_p = "--" + p + suffix
                f_full = "--" + l + suffix
                yield_it = accepted_fn(f_p) and (not accepted_fn(f_full) or p == shortest)
                if yield_it:
                    out.append((f_p, "--" + l, "abbrev"))
    return out


# ----------------------------------------------------------------------------------------------
# fixture

def build_template(path, tag):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    env = {"GIT_AUTHOR_NAME": "census", "GIT_AUTHOR_EMAIL": "c@example.invalid",
           "GIT_COMMITTER_NAME": "census", "GIT_COMMITTER_EMAIL": "c@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z", "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z",
           "HOME": os.path.join(os.path.dirname(path), "home"), "GIT_CONFIG_NOSYSTEM": "1"}
    os.makedirs(env["HOME"], exist_ok=True)

    def g(*a):
        rc, out = sysrun(["git", "-C", path] + list(a), env=env)
        if rc != 0:
            raise RuntimeError("fixture git %s failed: %s" % (a, out))
    g("init", "-q", "-b", "main", "--template=")
    with open(os.path.join(path, "f.txt"), "w") as fh:
        fh.write("foo one\nbar two\nfoo three\nzeta\nalpha\n")
    with open(os.path.join(path, "g.txt"), "w") as fh:
        fh.write("foo one\nbar 2\nfoo three\nzeta\n")
    with open(os.path.join(path, "data.json"), "w") as fh:
        fh.write('{"a": 1, "b": [1, 2, 3], "c": {"d": "foo"}}\n')
    with open(os.path.join(path, "evil"), "w") as fh:
        fh.write("#!/bin/sh\necho ran > '%s/EVIL_RAN'\n" % path)
    os.chmod(os.path.join(path, "evil"), 0o755)
    g("add", "f.txt", "g.txt", "data.json", "evil")
    g("commit", "-q", "-m", "first")
    g("tag", "-a", "-m", "release one", "v1")
    g("branch", "feature")
    rc, _ = sysrun(["sh", "-c", "cd '%s' && gzip -c f.txt > f.txt.gz" % path])
    return path


def snapshot(root):
    snap = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            full = os.path.join(dp, n)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                snap[os.path.relpath(full, root)] = (st.st_mode,)  # dir mtime moves with lock files
            else:
                snap[os.path.relpath(full, root)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return snap


def restore(template, dest):
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(template, dest, symlinks=True)
    evil = os.path.join(dest, "evil")
    with open(evil, "w") as fh:
        fh.write("#!/bin/sh\necho ran > '%s/EVIL_RAN'\n" % dest)
    os.chmod(evil, 0o755)


# ----------------------------------------------------------------------------------------------
# strace parsing and classification

_CALL = re.compile(r"^(\d+)\s+(\w+)\((.*)$")
_RET = re.compile(r"\)\s+=\s+(-?\d+|\?)(?:\s|$)(.*)$")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_TMPFILE")
PATH_CALLS = {"creat", "unlink", "unlinkat", "rename", "renameat", "renameat2", "mkdir", "mkdirat", "rmdir",
              "symlink", "symlinkat", "link", "linkat", "chmod", "fchmodat", "truncate"}
IGNORE_PREFIXES = ("/dev/", "/proc/")
NET_OK = ("/var/run/nscd/socket", "/run/nscd/socket")
NET_OK_PREFIX = ("/run/systemd/", "/var/run/systemd/")


def unq(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_trace(text):
    events = []
    for line in text.splitlines():
        m = _CALL.match(line)
        if not m:
            continue
        pid, name, rest = m.group(1), m.group(2), m.group(3)
        r = _RET.search(rest)
        ret = None
        if r and "<unfinished" not in rest:
            ret = r.group(1)
        events.append({"pid": pid, "name": name, "args": rest, "ret": ret})
    return events


def allowed_exec(path, prog, binpath, is_script, gitexec):
    base = os.path.basename(path)
    real = os.path.realpath(path) if os.path.exists(path) else path
    if base == prog or real == os.path.realpath(binpath):
        return True
    if prog == "git" and (path.startswith(gitexec + "/") or base.startswith("git")):
        return True
    if is_script and base in SHELL_SCRIPT_OK:
        return True
    if prog in ("egrep", "fgrep") and base in ("grep",):
        return True
    return False


def judge(events, fx, prog, binpath, is_script, gitexec, evil_ran, fsdiff):
    """Return dict(classes=set, evidence=list)."""
    ev = []
    cls = set()
    benign_paths = set([os.path.join(fx, ".git", "index"), os.path.join(fx, ".git", "index.lock")])
    first_exec_seen = False
    index_touch = False
    for e in events:
        name, args = e["name"], e["args"]
        quoted = [unq(q) for q in _QUOTED.findall(args)]
        if name in ("execve", "execveat"):
            if not first_exec_seen and quoted and quoted[0]:
                first_exec_seen = True
                continue
            if quoted:
                p = quoted[0] if name == "execve" else (quoted[1] if len(quoted) > 1 else quoted[0])
                if not allowed_exec(p, prog, binpath, is_script, gitexec):
                    cls.add("VIOLATION")
                    ev.append("exec %s ret=%s" % (p, e["ret"]))
        elif name in ("clock_settime", "settimeofday"):
            cls.add("VIOLATION")
            ev.append("%s %s ret=%s" % (name, args[:60], e["ret"]))
        elif name == "connect":
            mm = re.search(r"sa_family=(AF_\w+)", args)
            fam = mm.group(1) if mm else "?"
            if fam == "AF_UNIX":
                sp = re.search(r'sun_path="([^"]*)"', args)
                path = sp.group(1) if sp else ""
                if path in NET_OK or path.startswith(NET_OK_PREFIX):
                    continue
                cls.add("NETWORK")
                ev.append("connect unix %s" % path)
            elif fam in ("AF_INET", "AF_INET6"):
                cls.add("NETWORK")
                ev.append("connect " + args[:90])
            elif fam not in ("AF_NETLINK", "AF_UNSPEC"):
                cls.add("NETWORK")
                ev.append("connect " + args[:90])
        else:
            paths = []
            if name == "openat" or name == "open":
                if any(f in args for f in WRITE_FLAGS) and quoted:
                    paths = quoted[:1]
            elif name in PATH_CALLS and quoted:
                paths = quoted
            for pi, p in enumerate(paths):
                full = p if p.startswith("/") else os.path.normpath(os.path.join(fx, p))
                full = os.path.normpath(full)
                if full.startswith(IGNORE_PREFIXES):
                    continue
                if full in benign_paths:
                    index_touch = True
                    continue
                if name in ("symlink", "symlinkat") and pi != len(paths) - 1:
                    continue
                where = "inside" if full.startswith(fx + "/") else "outside"
                cls.add("VIOLATION")
                ev.append("%s %s (%s) ret=%s" % (name, full, where, e["ret"]))
    if evil_ran:
        cls.add("VIOLATION")
        ev.append("EVIL_RAN exists")
    unexplained = [p for p in fsdiff if p not in (".git/index", ".git/index.lock")]
    if unexplained:
        cls.add("VIOLATION")
        ev.append("fixture changed: " + ",".join(sorted(unexplained)[:6]))
    elif fsdiff or index_touch:
        if "VIOLATION" not in cls:
            cls.add("BENIGN-INDEX")
    return {"classes": cls, "evidence": ev}


PRIORITY = ("VIOLATION", "NETWORK", "TIMEOUT", "BENIGN-INDEX", "CLEAN")


class Runner(object):
    def __init__(self, work, template, slot, timeout, gitexec):
        self.fx = os.path.join(work, "fx%d" % slot)
        self.logdir = os.path.join(work, "log%d" % slot)
        os.makedirs(self.logdir, exist_ok=True)
        self.template = template
        self.timeout = timeout
        self.gitexec = gitexec
        self.home = os.path.join(work, "home")
        os.makedirs(self.home, exist_ok=True)
        restore(template, self.fx)
        self.base = snapshot(self.fx)

    def run(self, argv, prog, binpath, is_script, wrap=None):
        fx = self.fx
        log = os.path.join(self.logdir, "trace.txt")
        if os.path.exists(log):
            os.unlink(log)
        cmd = ["bwrap", "--ro-bind", "/", "/", "--bind", fx, fx, "--bind", self.logdir, self.logdir,
               "--dev", "/dev", "--proc", "/proc", "--unshare-net", "--die-with-parent", "--chdir", fx,
               "strace", "-f", "-s", "300", "-o", log, "-e", "trace=" + TRACE, "--"]
        if wrap:  # (argv0, prefix): the shell function's exec -a <argv0> <claude> <prefix> <args>
            cmd += ["bash", "-c", 'exec -a "$0" "$@"', wrap[0], binpath] + list(wrap[1]) + argv[1:]
        else:
            cmd += [binpath] + argv[1:]
        env = {"PATH": fx + ":" + SYSPATH, "HOME": self.home, "LC_ALL": "C", "TERM": "dumb",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat", "PWD": fx}
        t0 = time.time()
        timed_out = False
        err = b""
        rc = None
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=env, start_new_session=True)
        try:
            _, err = p.communicate(timeout=self.timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                _, err = p.communicate(timeout=5)
            except Exception:
                pass
        elapsed = time.time() - t0
        try:
            with open(log, "r", errors="replace") as fh:
                trace = fh.read()
        except OSError:
            trace = ""
        evil_ran = os.path.exists(os.path.join(fx, "EVIL_RAN"))
        after = snapshot(fx)
        changed = sorted(set(k for k in set(after) | set(self.base) if after.get(k) != self.base.get(k)))
        events = parse_trace(trace)
        res = judge(events, fx, prog, binpath, is_script, self.gitexec, evil_ran, changed)
        meta = [k for k in changed if k in after and k in self.base and after[k][:2] == self.base[k][:2]
                and not stat.S_ISDIR(after[k][0])]
        if meta:
            res["evidence"].append("mtime-only change (same size and mode, content not compared): " + ",".join(meta[:4]))
        if timed_out:
            res["classes"].add("TIMEOUT")
        if changed or evil_ran:
            restore(self.template, fx)
            self.base = snapshot(fx)
        primary = next(c for c in PRIORITY if c in res["classes"] or c == "CLEAN")
        errtxt = err.decode("utf-8", "replace").strip()[:160]
        if "bwrap:" in errtxt or "strace:" in errtxt and "Operation not permitted" in errtxt:
            res["evidence"].append("HARNESS? " + errtxt)
        return {"persistent": bool(changed or evil_ran), "class": primary, "all": sorted(res["classes"]), "evidence": res["evidence"], "rc": rc,
                "elapsed": round(elapsed, 2), "stderr": errtxt, "nevents": len(events)}


# ----------------------------------------------------------------------------------------------
# main census

def run_census(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)
    mod = load_guard(args.guard_root)
    ok_fn = mod.scout_command_is_read_only
    guard_progs = sorted(mod.SCOUT_READ_ONLY_COMMANDS)
    guard_subs = sorted(mod.SCOUT_GIT_SUBCOMMANDS)
    bins = resolve_binaries(guard_progs + ["git"])
    gitexec = sysrun(["git", "--exec-path"])[1].strip()
    want = None
    if args.programs:
        want = set(args.programs.split(","))
    template = build_template(os.path.join(work, "template"), "t")
    results = {"guard_root": os.path.abspath(args.guard_root), "versions": versions(bins), "programs": {},
               "violations": [], "network": [], "timeouts": [], "harness_warnings": [], "unchecked": [],
               "counts": {c: 0 for c in PRIORITY}}
    results["counts"]["forms_generated"] = 0
    results["counts"]["accepted"] = 0
    results["guard_programs"] = guard_progs
    results["guard_git_subcommands"] = guard_subs
    plan = []  # (label, prog, binpath, is_script, enum, templates)
    for p in guard_progs:
        if want and p not in want:
            continue
        if p == "git":
            continue
        b = bins.get(p)
        if not b:
            results["unchecked"].append("%s: binary not installed" % p)
            results["programs"][p] = {"error": "binary not installed"}
            continue
        en = enumerate_program(p, b)
        plan.append((p, p, b, open(b, "rb").read(2) == b"#!", en, TEMPLATES.get(p, [p + " {F}"])))
    claude = find_claude() if args.wrappers else None
    wraps = {}
    if args.wrappers:
        for label in sorted(WRAPPERS):
            if want and label not in want:
                continue
            gprog, argv0, prefix = WRAPPERS[label]
            if not claude or gprog not in guard_progs:
                results["unchecked"].append("%s: claude binary or guard program missing" % label)
                continue
            en = enumerate_wrapper(label, claude)
            wraps[label] = (argv0, prefix)
            tpl = TEMPLATES.get(gprog, [gprog + " {F}"])
            plan.append((label, gprog, claude, False, en, tpl))
        results["wrappers"] = {"claude": claude, "claude_version": sysrun([claude, "--version"])[1].strip(),
                               "rg_census_default": bins.get("rg"),
                               "rg_census_default_version": (sysrun([bins["rg"], "--version"])[1].splitlines() or [""])[0]
                               if bins.get("rg") else "none",
                               "rg_shell_function_version": (sysrun_argv0(claude, "rg", ["--version"])[1].splitlines()
                                                             or [""])[0] if claude else "none",
                               "bfs_version": sysrun_argv0(claude, "bfs", ["-version"])[1].strip()[:200] if claude else "",
                               "ugrep_version": (sysrun_argv0(claude, "ugrep", ["--version"])[1].splitlines() or [""])[0]
                               if claude else ""}
    gitbin = bins.get("git")
    for s in guard_subs:
        label = "git:" + s
        if want and label not in want and "git" not in want:
            continue
        en = enumerate_git_sub(gitbin, s)
        plan.append((label, "git", gitbin, False, en, [GIT_TEMPLATES.get(s, "git %s {F}" % s)]))

    jobs = []  # (label, argv_string, option, kind)
    for label, prog, b, script, en, templates in plan:
        opts = en["options"]
        forms, longs = forms_for(opts)
        rows = []
        for tpl in templates:
            for f, o, kind in forms:
                rows.append((tpl.replace("{F}", f), o, kind))
            for f, o, kind in abbreviation_forms(longs, lambda x, tpl=tpl: ok_fn(tpl.replace("{F}", x))[0]):
                rows.append((tpl.replace("{F}", f), o, kind))
        seen, acc, generated = set(), [], 0
        for cmd, o, kind in rows:
            generated += 1
            if cmd in seen:
                continue
            seen.add(cmd)
            if ok_fn(cmd)[0]:
                acc.append((label, cmd, o, kind))
        listed_n = en["listed"]
        warn = len(en["options"]) < 15
        results["programs"][label] = {
            "sources": en["sources"], "options_listed": listed_n, "options_inline": en["inline"],
            "options_binary_strings": en["binary_strings"], "options_total": len(en["options"]),
            "long": len(classify(opts)[0]), "short": len(classify(opts)[1]), "word": len(classify(opts)[2]),
            "forms_generated": generated, "forms_unique": len(seen), "accepted": len(acc),
            "parse_warning": ("FEW OPTIONS PARSED (%d): not coverage" % len(en["options"])) if warn else "",
            "classes": {c: 0 for c in PRIORITY}, "flag_timeout": 0}
        for a in acc:
            jobs.append(a)
    if args.max_runs:
        jobs = jobs[:args.max_runs]

    progmap = dict((l, (p, b, s)) for l, p, b, s, e, t in plan)
    wrapmap = wraps
    jobs_n = max(1, args.jobs)
    runners = []
    for i in range(jobs_n):
        runners.append(Runner(work, template, i, args.timeout, gitexec))
    free = list(range(jobs_n))
    lock = threading.Lock()
    done = [0]
    started = time.time()

    def work_one(job):
        label, cmd, o, kind = job
        with lock:
            slot = free.pop()
        try:
            prog, b, script = progmap[label]
            r = runners[slot].run(cmd.split(), prog, b, script, wrapmap.get(label))
            if "TIMEOUT" in r["all"]:  # a stall under load is not a finding: run once more
                r2 = runners[slot].run(cmd.split(), prog, b, script, wrapmap.get(label))
                r2["retried_after_timeout"] = True
                r = r2
        finally:
            with lock:
                free.append(slot)
        with lock:
            done[0] += 1
            if done[0] % 500 == 0:
                sys.stderr.write("  %d/%d runs, %.0fs\n" % (done[0], len(jobs), time.time() - started))
        return job, r

    with ThreadPoolExecutor(max_workers=jobs_n) as ex:
        futs = []
        for job in jobs:
            futs.append(ex.submit(work_one, job))
        collected = [f.result() for f in futs]
    # A stall while eight runs share the machine is not a hang: run every TIMEOUT again, alone.
    rechecked = 0
    for idx, (job, r) in enumerate(collected):
        if "TIMEOUT" in r["all"]:
            label = job[0]
            prog, b, script = progmap[label]
            r2 = runners[0].run(job[1].split(), prog, b, script, wrapmap.get(label))
            r2["rechecked_alone"] = True
            r2["first_pass_timeout"] = True
            collected[idx] = (job, r2)
            rechecked += 1
    results["rechecked_alone"] = rechecked
    if True:
        for job, r in collected:
            label, cmd, o, kind = job
            ps = results["programs"][label]
            ps["classes"][r["class"]] += 1
            if "TIMEOUT" in r["all"]:
                ps["flag_timeout"] += 1
            results["counts"][r["class"]] += 1
            results["counts"]["accepted"] += 1
            row = {"program": label, "command": cmd, "option": o, "kind": kind, "class": r["class"],
                   "all": r["all"], "persistent": r["persistent"], "evidence": r["evidence"], "rc": r["rc"], "stderr": r["stderr"],
                   "elapsed": r["elapsed"], "first_pass_timeout": bool(r.get("first_pass_timeout"))}
            if "VIOLATION" in r["all"]:
                results["violations"].append(row)
            if "NETWORK" in r["all"]:
                results["network"].append(row)
            if "TIMEOUT" in r["all"]:
                results["timeouts"].append(row)
            if any(x.startswith("HARNESS?") for x in r["evidence"]):
                results["harness_warnings"].append(row)
    results["counts"]["forms_generated"] = sum(p.get("forms_generated", 0) for p in results["programs"].values())
    results["elapsed_s"] = round(time.time() - started, 1)
    results["violations_by_program"] = {}
    for v in results["violations"]:
        results["violations_by_program"].setdefault(v["program"], []).append(v["command"])
    shutil.rmtree(work, ignore_errors=True)
    return results


# ----------------------------------------------------------------------------------------------
# systemctl / journalctl static comparison

RISK = re.compile(r"follow|vacuum|rotate|flush|sync|delet|remov|write|writ|creat|enabl|disabl|start|stop|restart|"
                  r"reload|force|kill|reboot|power|host|machine|remote|ssh|connect|network|url|upload|image|mount|"
                  r"setup|updat|cursor-file|root|namespace|change|modif|set |sets |reset|edit|install|mask|"
                  r"runtime|now|signal|wait|directory|file", re.I)
STATE = re.compile(r"follow|vacuum|rotate|flush|sync|delet|remov|writ|creat|enabl|disabl|start|stop|restart|"
                   r"reload|force|kill|reboot|power|host|machine|remote|ssh|connect|image|mount|setup|updat|"
                   r"edit|install|mask|runtime|signal|--now|\bnow\b", re.I)


def systemd_report(mod):
    rep = {}
    ok_fn = mod.scout_command_is_read_only
    for prog in SYSTEMD:
        b = shutil.which(prog, path=SYSPATH)
        if not b:
            rep[prog] = {"error": "not installed"}
            continue
        rc, text = sysrun([b, "--help"])
        desc = {}
        for line in text.splitlines():
            m = re.match(r"^\s+((?:-\w(?:\s+\w+)?,?\s*)?(?:--[\w\-\[\]]+(?:[= ]\S+)?)?(?:\s+-\w)?)\s{2,}(\S.*)$", line)
            if m:
                for t in _norm_many(m.group(1)):
                    desc[t] = m.group(2)
            elif re.match(r"^\s{20,}\S", line) and desc:
                last = list(desc)[-1]
                desc[last] += " " + line.strip()
        listed, inline = parse_options(text)
        opts = sorted(listed | inline)
        longs, shorts, words = classify(opts)
        rows = []
        for o in opts:
            if prog == "systemctl":
                tpls = ["systemctl status {F}", "systemctl {F}", "systemctl {F} status"]
            else:
                tpls = ["journalctl {F}"]
            forms = [o]
            if o.startswith("--"):
                forms += [o + "=" + EVIL, o + " " + EVIL]
            elif len(o) == 2:
                forms += [o + " " + EVIL, o + EVIL, o + "evil", o + "1"]
            acc = []
            for tpl in tpls:
                for f in forms:
                    c = tpl.replace("{F}", f)
                    if ok_fn(c)[0]:
                        acc.append(c)
            if acc:
                d = desc.get(o, "")
                rows.append({"option": o, "accepted_forms": acc[:6], "n_accepted": len(acc), "help": d,
                             "flag": bool(STATE.search(d + " " + o))})
        # abbreviations the guard accepts for long options whose full spelling it rejects
        abbr = []
        for l in sorted(longs):
            for k in range(2, len(l)):
                p = l[:k]
                if sum(1 for x in longs if x.startswith(p)) != 1:
                    continue
                for tpl in (["journalctl {F}"] if prog == "journalctl" else ["systemctl status {F}"]):
                    if ok_fn(tpl.replace("{F}", "--" + p))[0] and not ok_fn(tpl.replace("{F}", "--" + l))[0]:
                        abbr.append("--" + p + " (of --" + l + ")")
                        break
        rep[prog] = {"options_parsed": len(opts), "listed": len(listed), "inline": len(inline),
                     "help_exit": rc, "accepted_options": rows,
                     "accepted_and_flagged": [r["option"] + ": " + r["help"][:100] for r in rows if r["flag"]],
                     "abbrev_accepted_while_full_rejected": abbr[:50],
                     "parse_warning": "FEW OPTIONS PARSED" if len(opts) < 15 else ""}
    return rep


def _norm_many(s):
    out = []
    for part in re.split(r"[,\s]+", s.strip()):
        if part.startswith("-"):
            out.extend(_norm(part))
    return out


# ----------------------------------------------------------------------------------------------

def merge(dirs, out):
    merged = None
    for d in dirs:
        with open(os.path.join(d, "results.json")) as fh:
            r = json.load(fh)
        if merged is None:
            merged = r
            continue
        merged["programs"].update(r["programs"])
        for k in ("violations", "network", "timeouts", "harness_warnings", "unchecked"):
            merged[k].extend(r[k])
        for k, v in r["counts"].items():
            merged["counts"][k] = merged["counts"].get(k, 0) + v
        merged["elapsed_s"] = merged.get("elapsed_s", 0) + r.get("elapsed_s", 0)
    merged["violations_by_program"] = {}
    for v in merged["violations"]:
        merged["violations_by_program"].setdefault(v["program"], []).append(v["command"])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump(merged, fh, indent=1, sort_keys=True)
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guard-root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--programs", default="", help="comma list: ls,rg,git:log (default all); 'git' = all subcommands")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--budget", type=float, default=520.0)
    ap.add_argument("--max-runs", type=int, default=0)
    ap.add_argument("--skip-systemd", action="store_true")
    ap.add_argument("--wrappers", action="store_true",
                    help="also census find(bfs), grep(ugrep) and rg(claude) from the claude binary")
    ap.add_argument("--merge", nargs="+")
    args = ap.parse_args()
    if args.merge:
        m = merge(args.merge, args.out)
        print(json.dumps({"counts": m["counts"], "violations": len(m["violations"])}))
        return 0
    if not args.guard_root:
        ap.error("--guard-root is required")
    for tool in ("bwrap", "strace"):
        if not shutil.which(tool):
            sys.stderr.write("missing %s\n" % tool)
            return 2
    res = run_census(args)
    if not args.skip_systemd:
        res["systemd"] = systemd_report(load_guard(args.guard_root))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.json"), "w") as fh:
        json.dump(res, fh, indent=1, sort_keys=True)
    print(json.dumps({"counts": res["counts"], "violations": len(res["violations"]),
                      "network": len(res["network"]), "timeouts": len(res["timeouts"]),
                      "elapsed_s": res.get("elapsed_s")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
