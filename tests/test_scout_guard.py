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


if __name__ == "__main__":
    unittest.main()
