#!/usr/bin/env python3
"""Scout read-only guard: fixed tables, hook-level checks and a shell oracle.

A false deny is acceptable, a false allow is a bug. The tables pin both directions; the oracle runs
accepted commands in bash, dash, busybox sh and zsh (when installed) against logging stubs.
Run: python -m unittest discover -s tests"""

import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scout_limits  # noqa: E402
import scout_oracle  # noqa: E402
from test_hardening import EnvCase, eo  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ALLOWED = (
    "rg -n 'a|b' .",
    'rg -n "a;b" src',
    "grep -E 'a>b' f",
    "rg '$(foo)' .",
    "rg a\\|b .",
    'rg "foo\\d" .',
    'rg "中文" .',
    "git --no-pager log -3",
    "git -C sub status",
    "git show 'HEAD@{1}'",
    "git show HEAD~3",
    "LC_ALL=C sort f",
    "ls >/dev/null 2>&1",
    "rg foo src/*.py",
    "ls -la\nwc -l f\n",
    "git status -sb 2>/dev/null",
    "rg -n foo src | head -20",
    "ls -la && wc -l *.py",
    "git config --get user.name",
    "tail -n 50 app.log",
    "systemctl status nginx --no-pager",
    "journalctl -u nginx -n 50 --no-pager",
    "find . -name '*.py' -type f",
    "ls ~/src",
    "git branch --list 'feat/*'",
    "ls '&'",
    "rg 'a\\' .",
    "git log --pretty=format:'%h %s' -5",
    "jq -r '.[] | select(.x > 1)' f.json",
    "find . -name '*.py' -not -path './.git/*'",
    "rg 'def (foo|bar)\\(' src",
    "grep -rn 'a&&b' src | head -5",
    'git show HEAD:README.md | head',
    "git tag -l 'v2.*'",
    'git remote get-url origin',
    'ls -la src/ | sort -k5 -n | tail -3',
    'wc -l src/*.py | tail -1',
    'du -sh . 2>/dev/null',
    'date -u +%F',
    'date -d tomorrow +%F',
    'sort -t, -k2,2n f',
    'diff -ru a b',
    'rg "\\$HOME" .',
    'NO_COLOR=1 git log -1',
    "git branch -a --list 'feat/*'",
    "git branch --list --no-merged main",
    "git branch --no-merged",
    "git tag -n5 -l 'v1.*'",
    "git tag -i -l 'v*'",
    "git config --get a.b c",
    "git config --get-regexp '^remote'",
    "git config --list --show-origin",
    "git config --list --file=.git/config",
    "git log --format=%h -3",
    "git log --diff-filter=A --name-only",
    "git log --stat -3",
    "git -C src log -1",
    "file -b x.gz",
    "file -i -L x",
    "grep -e foo f",
    "grep -n -- 'a' f",
)

DENIED = (
    ("background", "ls & rm -rf build"),
    ("path-program", "./evil/ls"),
    ("abs-program", "/tmp/x/cat f"),
    ("env-preload", "LD_PRELOAD=./x.so ls"),
    ("env-git-diff", "GIT_EXTERNAL_DIFF=./evil git diff"),
    ("var-option", "X=-delete; find . $X"),
    ("var-underscore", "echo -delete; find . $_"),
    ("brace-option", "find . {-delete,-true}"),
    ("find-fprint0", "find . -fprint0 out"),
    ("sort-o-attached", "sort -oout in"),
    ("sort-o-cluster", "sort -uo out in"),
    ("sort-abbrev", "sort --outp=x"),
    ("sort-compress", "sort --compress-program=./e f"),
    ("tree-o", "tree -oout"),
    ("rg-pre-space", "rg --pre ./e x ."),
    ("rg-pre-eq", "rg --pre=./e x"),
    ("rg-hostname-bin", "rg --hostname-bin=./e x"),
    ("git-grep-O", "git grep -O./e x"),
    ("git-ext-diff", "git diff --ext-diff"),
    ("git-diff-abbrev", "git diff --outp=x"),
    ("git-remote-update", "git remote update"),
    ("git-edit-desc", "git branch --edit-description"),
    ("git-unset-upstream", "git branch --unset-upstream"),
    ("git-C-value", "git -C status commit -m x"),
    ("git-dir-value", "git --git-dir status push"),
    ("git-exec-path", "git --exec-path=./x log"),
    ("file-compile", "file -C -m x"),
    ("date-set", "date -s 2020-01-01"),
    ("systemctl-type-value", "systemctl -t status stop nginx"),
    ("journalctl-abbrev", "journalctl --foll"),
    ("redirect-devnull-prefix", "echo x > /dev/nullfoo"),
    ("name-digit-redirect", "ls2>/dev/null"),
    ("prompt-expansion", "X=\\$\\(touch\\ p\\); echo ${X@P}"),
    ("old-arith", "echo $[X]"),
    ("dq-escaped-backslash-subst", 'echo "\\\\$(rm x)"'),
    ("ansi-c-quote", "echo $'\\'' ; rm x #'"),
    ("comment-quote", "ls #'\nrm x #'"),
    ("backslash-newline-split", "r\\\nm x"),
    ("function-def", "ls () ( rm x ); ls"),
    ("busybox-double-bracket-or", "rg x [[ || cat --pre=./evil"),
    ("busybox-double-bracket-and", "rg x [[ && cat --pre=./evil"),
    ("heredoc-subst", "cat <<EOF\n$(rm x)\nEOF"),
    ("carriage-return", "ls\rrm x"),
    ("empty", ""),
    ("zsh-process-subst", "ls =(rm x)"),
    ("zsh-glob-qualifier", "ls *(e:'rm x':)"),
    ("redirect-to-file", "ls > out"),
    ("write-program", "rm -rf build"),
    ("command-subst", "cat $(ls)"),
    ("backtick", "cat `ls`"),
    ("pipe-ampersand", "ls |& rm x"),
    ("here-string", "cat <<<x"),
    ("unicode-space", "ls\u00a0-la"),
    ("nul", "ls\x00 x"),
    ("trailing-or", "ls ||"),
    ("brace-group", "{ rm x; }"),
    ("glob-option", "rg * x"),
    ('git-log-help', 'git log --help'),
    ('git-status-abbrev-help', 'git status --he'),
    ('git-log-remerge-diff', 'git log --remerge-diff'),
    ('git-show-remerge-abbrev', 'git show --remerge'),
    ('file-p', 'file -p f'),
    ('file-pre', 'file --pre f'),
    ('tail-f', 'tail -f x'),
    ('tail-F-cluster', 'tail -Fn5 x'),
    ('tail-follow-eq', 'tail --follow=name x'),
    ('journalctl-M', 'journalctl -M c'),
    ('journalctl-image', 'journalctl --image=x'),
    ('systemctl-r', 'systemctl -r list-units'),
    ('grep-filter', 'grep --filter=x y f'),
    ('grep-pager', 'grep --pager=x y f'),
    ('grep-save-config', 'grep --save-config'),
    ('grep-Q', 'grep -Q x'),
    ('fgrep-view', 'fgrep --view=x y f'),
    ('double-semicolon', 'ls ;;'),
    ('double-and', 'ls && && wc'),
    ('leading-pipe', '| ls'),
    ('leading-and', '&& ls'),
    ('trailing-backslash', 'ls \\'),
    ('crlf', 'ls\r\n'),
    ('assign-semicolon', 'LC_ALL=C; ls'),
    ('redirect-glued', 'ls >/dev/null>x'),
    ('redirect-then-amp', 'ls >/dev/null&'),
    ('quoted-program', "'rm' x"),
    ('split-quoted-program', "r''m x"),
    ('backslash-program', '\\rm x'),
    ('find-split-delete', "find . -de''lete"),
    ('find-escaped-delete', 'find . \\-delete'),
    ('git-branch-l-creates', 'git branch -l x'),
    ('date-positional', 'date 0101120020'),
    ('date-positional-cc', 'date 010112002020'),
    ('date-positional-sec', 'date -u 0101120020.30'),
    ('redirect-only-newline', 'ls\n>/dev/null'),
    ('redirect-only-semicolon', 'ls; >/dev/null'),
    ('redirect-only-fd', 'ls\n 2>&1'),
    # Claude Code runs find as bfs, where -rm deletes like -delete (census, wrapper mode: false allows).
    ('bfs-rm', 'find . -rm'),
    ('bfs-rm-first', 'find -rm ./evil .'),
    # White-box review of c887e99: 28 cases in 7 causes (tests/whitebox/ in the v2.5.0 shots).
    ('wb-git-config-no-get-set', 'git config --get --no-get user.name evil'),
    ('wb-git-config-no-get-fsmonitor', 'git config --get --no-get core.fsmonitor ./evil'),
    ('wb-git-config-no-get-file', 'git config --get --no-get --file ./any.cfg a.b c'),
    ('wb-git-config-no-list', 'git config --list --no-list a.b c'),
    ('wb-git-config-l-no-list', 'git config -l --no-list a.b c'),
    ('wb-git-config-no-get-all', 'git config --get-all --no-get-all a.b c'),
    ('wb-git-config-no-get-regexp', 'git config --get-regexp --no-get-regexp a.b c'),
    ('wb-git-branch-no-list', 'git branch --list --no-list newb'),
    ('wb-git-branch-format-swallows-list', 'git branch --format --list newb'),
    ('wb-git-tag-format-swallows-list', 'git tag --format --list newt'),
    ('wb-git-tag-format-swallows-l', 'git tag --format -l newt'),
    ('wb-git-log-show-signature', 'git log --show-signature'),
    ('wb-git-show-show-signature', 'git show --show-signature HEAD'),
    ('wb-git-log-show-sig-abbrev', 'git log --show-sig'),
    ('wb-git-reflog-show-signature', 'git reflog show --show-signature'),
    ('wb-git-log-format-G', "git log --format='%G?'"),
    ('wb-git-show-format-GG', 'git show --format=%GG'),
    ('wb-git-branch-format-signature', "git branch --format='%(signature)'"),
    ('wb-git-log-diff-merges-remerge', 'git log --diff-merges=remerge'),
    ('wb-git-log-diff-merges-r', 'git log --diff-merges=r'),
    ('wb-git-show-diff-merges-spaced', 'git show --diff-merges remerge'),
    ('wb-file-z', 'file -z x.Z'),
    ('wb-file-zS', 'file -zS x.Z'),
    ('wb-file-no-sandbox-uncompress', 'file --no-sandbox --uncompress x.Z'),
    ('wb-file-Z', 'file -Z x.Z'),
    ('wb-ugrep-config-triple-dash', 'grep ---./evil x .'),
    ('wb-ugrep-config-default', 'egrep --- x .'),
    ('wb-git-C-glob-subcommand', 'git -C r* log'),
    ('wb-sib-date-glob-value', 'date -d 0*'),
    ('wb-sib-config-three-operands', 'git config --get a b c'),
    ('wb-sib-config-list-operand', 'git config --list x'),
    ('wb-sib-config-negated-other', 'git config --get --no-includes a.b'),
    ('wb-sib-config-set-first', 'git config a.b c'),
    ('wb-sib-branch-no-list-abbrev', 'git branch --list --no-l newb'),
    ('wb-sib-branch-valued-option-first', 'git branch --contains --list newb'),
    ('wb-sib-tag-no-list', 'git tag -l --no-list newt'),
    ('wb-sib-tag-sort-first', 'git tag --sort -l newt'),
    ('wb-sib-signature-abbrev', 'git log --show-sig'),
    ('wb-sib-signature-atom-colon', "git tag --format='%(signature:grade)' -l"),
    ('wb-sib-file-uncompress-abbrev', 'file --uncomp x.Z'),
    ('wb-sib-file-no-sandbox-abbrev', 'file --no-sand x'),
    ('wb-sib-git-C-glob-dir', 'git -C ./r* log'),
)

STRICT_ALLOWED = ("rg foo src | head -5",)
STRICT_DENIED = (
    "rg 'a|b' .",
    "ls; rm x",
    "ls && wc f",
    "LC_ALL=C ls",
    "sort /O out in",
    "ls %COMSPEC%",
    "ls C:\\Windows",
)


class TestScoutTables(unittest.TestCase):
    def test_legitimate_forms_stay_allowed(self):
        for cmd in ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(eo.scout_command_is_read_only(cmd), (True, ""))

    def test_denied_table(self):
        for cls, cmd in DENIED:
            with self.subTest(cls=cls, cmd=cmd):
                self.assertFalse(eo.scout_command_is_read_only(cmd)[0], "false allow: %r" % cmd)

    def test_denied_has_a_reason(self):
        for _, cmd in DENIED:
            ok, why = eo.scout_command_is_read_only(cmd)
            if not ok:
                self.assertTrue(why, cmd)

    def test_strict_windows(self):
        for cmd in STRICT_ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(eo.scout_command_is_read_only(cmd, strict_windows=True), (True, ""))
        for cmd in STRICT_DENIED:
            with self.subTest(cmd=cmd):
                self.assertFalse(eo.scout_command_is_read_only(cmd, strict_windows=True)[0], "false allow: %r" % cmd)


class TestScoutHook(EnvCase):
    SCOUT = "mitosis-orchestrator:flash-lite-scout"
    WORKER = "mitosis-orchestrator:flash-worker"

    def decision(self, result):
        return (result.get("hookSpecificOutput") or {}).get("permissionDecision")

    def claude(self, cmd, agent_type=None, **extra):
        return eo.handle_pre_tool_use(self.claude_tool("Bash", {"command": cmd}, agent_id="a1",
                                                       agent_type=agent_type or self.SCOUT, **extra))

    def codex(self, cmd, cwd):
        payload = dict(self.payload, tool_name="Bash", tool_input={"command": cmd}, agent_id="c1",
                       agent_type="explorer", turn_id="t1", cwd=cwd)
        return eo.handle_pre_tool_use(payload)

    def test_claude_scout_posix_rules(self):
        self.assertEqual(self.claude("rg -n 'a|b' ."), {})
        self.assertEqual(self.decision(self.claude("ls & rm x")), "deny")
        self.assertEqual(self.decision(self.claude("ls2>/dev/null")), "deny")

    def test_claude_scout_with_windows_cwd_keeps_posix_rules(self):
        self.assertEqual(self.claude("rg -n 'a|b' .", cwd="C:\\repo"), {})
        self.assertEqual(self.decision(self.claude("ls & rm x", cwd="C:\\repo")), "deny")

    def test_codex_explorer_with_windows_cwd_is_strict(self):
        self.assertEqual(self.codex("rg foo src | head -5", "C:\\repo"), {})
        self.assertEqual(self.decision(self.codex("rg 'a|b' .", "C:\\repo")), "deny")
        self.assertEqual(self.decision(self.codex("ls; rm x", "C:\\repo")), "deny")

    @unittest.skipIf(os.name == "nt", "os.name selects the strict rules on Windows whatever the cwd")
    def test_codex_explorer_with_posix_cwd_uses_posix_rules(self):
        self.assertEqual(self.codex("rg -n 'a|b' .", str(self.project)), {})
        self.assertEqual(self.decision(self.codex("ls & rm x", str(self.project))), "deny")

    def test_worker_is_unaffected(self):
        self.assertEqual(self.claude("ls & rm x", agent_type=self.WORKER), {})
        self.assertEqual(self.claude("rm -rf build", agent_type=self.WORKER), {})


# ---------------------------------------------------------------------------------------- oracle

def _oracle_shells():
    if os.name == "nt":
        return []
    spec = ["bash", "dash", "busybox"]
    zsh = os.environ.get("SCOUT_ORACLE_ZSH") or shutil.which("zsh")
    if zsh:
        spec.append("zsh=" + zsh)
    return scout_oracle.parse_shells(",".join(spec))


SHELLS = _oracle_shells()


@unittest.skipIf(os.name == "nt", "the shell oracle needs a POSIX system")
class TestScoutOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = scout_oracle.load_guard(ROOT)

    def oracle(self, shells):
        o = scout_oracle.Oracle(self.guard, shells)
        self.addCleanup(o.close)
        return o

    def test_shell_availability_is_known(self):
        self.assertTrue(SHELLS, "no shell for the oracle: install bash, dash or busybox")

    def test_loaded_hook_is_under_root(self):
        self.assertTrue(str(Path(sys.modules["mitosis_hook"].__file__).resolve()).startswith(str(ROOT.resolve())))

    def test_legitimate_commands_match_the_scanner(self):
        for shell in SHELLS:
            oracle = self.oracle([shell])
            for cmd in ALLOWED:
                if "systemctl" in cmd or "journalctl" in cmd:
                    continue
                with self.subTest(shell=shell.label, cmd=cmd):
                    self.assertEqual(oracle.check_accepted(cmd), [])

    def test_fixed_seed_fuzz_has_no_violations(self):
        for shell in SHELLS:
            with self.subTest(shell=shell.label):
                report = scout_oracle.run_fuzz(self.guard, 6000, 20261103, [shell], workers=4)
                self.assertGreater(report["accepted"], 100)
                self.assertEqual(report["violations"], [])

    def test_self_check_flags_known_bad_commands(self):
        """Allowlist-only mode must see these, or a clean fuzz run proves nothing."""
        for shell in SHELLS:
            oracle = self.oracle([shell])
            for cmd in ("ls & rm x", "ls2>/dev/null", "ls () ( rm x ); ls", "ls > newfile.txt"):
                with self.subTest(shell=shell.label, cmd=cmd):
                    self.assertTrue(oracle.run_command(cmd, scanner_words=False), cmd)

    def test_self_check_clean_command_is_clean(self):
        for shell in SHELLS:
            oracle = self.oracle([shell])
            with self.subTest(shell=shell.label):
                self.assertEqual(oracle.run_command("echo a b | cat; ls src/*.py", scanner_words=False), [])
                self.assertEqual(oracle.run_command("echo a b | cat; ls src/*.py"), [])

    def test_scanner_words_check_catches_a_wrong_scan(self):
        """Hand the oracle a scan that differs from the shell's view: (c) must fire."""
        shell = SHELLS[0]
        oracle = self.oracle([shell])
        found = scout_oracle.check_run(
            "ls a", shell, oracle.box().run(shell, "ls a"), oracle.allowed, [[("ls", -1, True), ("b", -1, True)]],
            str(oracle.box().home))
        self.assertTrue({v["kind"] for v in found} >= {"argv", "missing"})


@unittest.skipIf(os.name == "nt", "the shell oracle needs a POSIX system")
class TestOracleSafeDefaults(unittest.TestCase):
    """The oracle and the census must not push a shared host into the OOM killer."""

    def test_meminfo_reader(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "meminfo"
            good.write_text("MemTotal: 100 kB\nMemAvailable:    2097152 kB\n", encoding="ascii")
            self.assertAlmostEqual(scout_limits.read_available_gib(str(good)), 2.0)
            bad = Path(tmp) / "bad"
            bad.write_text("MemTotal: 100 kB\n", encoding="ascii")
            self.assertIsNone(scout_limits.read_available_gib(str(bad)))
        self.assertIsNone(scout_limits.read_available_gib("/nonexistent/meminfo"))

    def test_floor_counts_skips_and_absent_reader_means_no_floor(self):
        low = scout_limits.MemoryFloor(3, reader=lambda: 2.5)
        self.assertFalse(low.allow())
        self.assertFalse(low.allow())
        self.assertEqual(low.report()["skipped_for_memory"], 2)
        self.assertEqual(low.report()["lowest_available_gib"], 2.5)
        self.assertTrue(scout_limits.MemoryFloor(3, reader=lambda: 3.0).allow())
        none = scout_limits.MemoryFloor(3, reader=lambda: None)
        self.assertTrue(none.allow())
        self.assertEqual(none.report()["skipped_for_memory"], 0)
        self.assertTrue(scout_limits.MemoryFloor(0, reader=lambda: 0.1).allow())

    def test_defaults(self):
        self.assertEqual(scout_limits.DEFAULT_CHILD_AS_GIB, 4.0)
        self.assertEqual(scout_limits.DEFAULT_MIN_AVAILABLE_GIB, 3.0)
        self.assertIsNone(scout_limits.child_preexec(0))
        self.assertTrue(callable(scout_limits.child_preexec(4)))

    def test_fuzz_skips_runs_under_the_floor_and_reports_it_apart(self):
        guard = scout_oracle.load_guard(ROOT)
        shells = _oracle_shells()[:1]
        floor = scout_limits.MemoryFloor(3, reader=lambda: 1.0)
        report = scout_oracle.run_fuzz(guard, 300, 3, shells, workers=2, floor=floor)
        self.assertGreater(report["unique_accepted"], 0)
        self.assertEqual(report["skipped_for_memory"], report["unique_accepted"])
        self.assertEqual(report["skipped_for_budget"], 0)
        self.assertEqual(report["runs"], {shells[0].label: 0})
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["min_available_gib"], 3)

    def test_children_run_under_the_address_space_limit(self):
        import subprocess
        out = subprocess.run(["/bin/sh", "-c", "ulimit -v"], stdout=subprocess.PIPE,
                             preexec_fn=scout_limits.child_preexec(4)).stdout.decode().strip()
        self.assertEqual(out, str(4 * 1024 * 1024))
        shell = _oracle_shells()[0]
        o = scout_oracle.Oracle(scout_oracle.load_guard(ROOT), [shell], child_as_gib=4)
        self.addCleanup(o.close)
        self.assertEqual(o.run_command("echo a | cat"), [])  # shells start under the limit


def _git_version():
    import re
    import subprocess
    if not shutil.which("git"):
        return None
    out = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


@unittest.skipIf(os.name == "nt", "needs a POSIX shell")
@unittest.skipIf(_git_version() is None, "git is not installed")
class TestRealGitWrites(unittest.TestCase):
    """The three commands the white-box review confirmed against git 2.43.0 write to the repository when
    run. They are run here in a throwaway repo, then the guard must deny them. git status refreshing
    .git/index is known and allowed on purpose (documented in SECURITY.md); it is not tested here."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="scout-realgit-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp, "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "LC_ALL": "C"}
        self.git("init", "-q")
        Path(self.repo, "f").write_text("x\n", encoding="utf-8")
        self.git("add", "f")
        self.git("commit", "-q", "-m", "one")

    def git(self, *args):
        import subprocess
        return subprocess.run(["git"] + list(args), cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def sh(self, command):
        import subprocess
        return subprocess.run(["/bin/sh", "-c", command], cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def test_config_no_get_writes_the_config(self):
        cmd = "git config --get --no-get core.scoutprobe val"
        self.assertFalse(eo.scout_command_is_read_only(cmd)[0])
        self.sh(cmd)
        if self.git("config", "--get", "core.scoutprobe").stdout.strip() != b"val":
            self.skipTest("this git (%s) ignores --no-get after --get" % (_git_version(),))
        self.assertEqual(self.git("config", "--get", "core.scoutprobe").stdout.strip(), b"val")

    def test_branch_no_list_creates_a_branch(self):
        cmd = "git branch --list --no-list scoutbranch"
        self.assertFalse(eo.scout_command_is_read_only(cmd)[0])
        self.sh(cmd)
        if self.git("rev-parse", "--verify", "-q", "refs/heads/scoutbranch").returncode != 0:
            self.skipTest("this git (%s) did not create the branch" % (_git_version(),))

    def test_tag_format_swallows_list_and_creates_a_tag(self):
        cmd = "git tag --format --list scouttag"
        self.assertFalse(eo.scout_command_is_read_only(cmd)[0])
        self.sh(cmd)
        if self.git("rev-parse", "--verify", "-q", "refs/tags/scouttag").returncode != 0:
            self.skipTest("this git (%s) did not create the tag" % (_git_version(),))

    def test_legitimate_listing_changes_nothing(self):
        for cmd in ("git branch --list 'sc*'", "git tag -l 'sc*'", "git config --get core.scoutprobe"):
            self.assertTrue(eo.scout_command_is_read_only(cmd)[0], cmd)
            before = sorted(os.listdir(os.path.join(self.repo, ".git", "refs", "heads")))
            self.sh(cmd)
            self.assertEqual(sorted(os.listdir(os.path.join(self.repo, ".git", "refs", "heads"))), before)
        self.assertEqual(self.git("tag").stdout, b"")
        self.assertEqual(self.git("config", "--get", "core.scoutprobe").returncode, 1)


if __name__ == "__main__":
    unittest.main()
