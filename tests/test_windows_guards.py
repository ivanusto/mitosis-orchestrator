#!/usr/bin/env python3
"""Workspace path guard: Windows path forms (ntpath, so these run on any platform) and POSIX non-regression.
Run: python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mitosis_hook import guards  # noqa: E402

REPO = Path("C:\\repo")
SHARE = Path("\\\\server\\share")


def check(command, cwd, *roots):
    return guards.command_stays_in_workspace(command, cwd, list(roots))


# (command, cwd, root, expected_ok)
WINDOWS_CASES = [
    # outside: must be denied
    ("type C:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("type C:/Windows/win.ini", "C:\\repo", REPO, False),
    ("type c:\\windows\\win.ini", "C:\\repo", REPO, False),
    ("type D:\\data\\x.txt", "C:\\repo", REPO, False),
    ("cat C:\\repo2\\file", "C:\\repo", REPO, False),  # prefix trap
    ("cat C:\\repository\\file", "C:\\repo", REPO, False),
    ("cat C:/repo2/file", "C:\\repo", REPO, False),
    ("cat C:\\repo\\..\\secret", "C:\\repo", REPO, False),
    ("cat C:\\repo\\sub\\..\\..\\secret", "C:\\repo", REPO, False),
    ("type \\\\evil\\share\\x", "C:\\repo", REPO, False),
    ("type \\\\?\\C:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("type \\\\?\\UNC\\evil\\share\\x", "C:\\repo", REPO, False),
    ("type \\\\.\\C:\\Windows\\x", "C:\\repo", REPO, False),
    ("cd ..\\..", "C:\\repo\\a", REPO, False),
    ("cd ..\\..\\..", "C:\\repo\\a\\b", REPO, False),
    ("cd ../..", "C:\\repo\\a", REPO, False),
    ("type ..\\..\\secret", "C:\\repo\\a", REPO, False),
    ("type ..\\x", "C:\\repo", REPO, False),
    ("type %USERPROFILE%\\x", "C:\\repo", REPO, False),
    ("type %APPDATA%", "C:\\repo", REPO, False),
    ("type %unset_var%\\x", "C:\\repo", REPO, False),
    ("type \"C:\\Program Files\\x\\y.txt\"", "C:\\repo", REPO, False),
    ("type 'C:\\Windows\\a b\\y.txt'", "C:\\repo", REPO, False),
    ("type \"..\\..\\my dir\\y\"", "C:\\repo\\a", REPO, False),
    ("copy C:\\repo\\a.txt C:\\Users\\x\\a.txt", "C:\\repo", REPO, False),
    ("python x.py --out=C:\\Users\\x\\o", "C:\\repo", REPO, False),
    ("type \\\\server\\share2\\x", "\\\\server\\share", SHARE, False),
    ("type \\\\server\\other\\x", "\\\\server\\share", SHARE, False),
    ("type C:\\repo\\x", "\\\\server\\share", SHARE, False),
    # inside: must be allowed
    ("type C:\\repo\\sub\\file", "C:\\repo", REPO, True),
    ("type c:\\REPO\\Sub\\file", "C:\\repo", REPO, True),
    ("cat c:/REPO/file", "C:\\repo", REPO, True),
    ("cat C:/repo\\mixed/sep\\file", "C:\\repo", REPO, True),
    ("cat C:\\repo", "C:\\repo", REPO, True),
    ("cat C:\\repo\\", "C:\\repo", REPO, True),
    ("cat C:\\repo\\a\\..\\b", "C:\\repo", REPO, True),
    ("type \"C:\\repo\\my dir\\f.txt\"", "C:\\repo", REPO, True),
    ("type 'C:/repo/my dir/f.txt'", "C:\\repo", REPO, True),
    ("type \\\\?\\C:\\repo\\f", "C:\\repo", REPO, True),
    ("cd ..", "C:\\repo\\a", REPO, True),
    ("cd ..\\b", "C:\\repo\\a", REPO, True),
    ("type ..\\..\\f", "C:\\repo\\a\\b", REPO, True),
    ("type \"..\\my dir\\f\"", "C:\\repo\\a", REPO, True),
    ("dir", "C:\\repo", REPO, True),
    ("python -m unittest", "C:\\repo", REPO, True),
    ("type \\\\server\\share\\sub\\f", "\\\\server\\share", SHARE, True),
    ("type //server/share/f".replace("//", "\\\\").replace("/", "\\"), "\\\\server\\share", SHARE, True),
    ("type \\\\SERVER\\SHARE\\f", "\\\\server\\share", SHARE, True),
    ("cd ..", "\\\\server\\share\\a", SHARE, True),
    ("cd ..\\..", "\\\\server\\share\\a", SHARE, True),  # ntpath clamps at the share root
    # drive-relative, bare drives, flag-attached drives, PowerShell env, %CD%
    ("type C:secret.txt", "C:\\repo", REPO, False),
    ("type \"C:secret.txt\"", "C:\\repo", REPO, False),
    ("type D:", "C:\\repo", REPO, False),
    ("type C:", "C:\\repo", REPO, False),
    ("tool -IC:\\Windows\\inc", "C:\\repo", REPO, False),
    ("tool /IC:\\Windows\\inc", "C:\\repo", REPO, False),
    ("tool -LC:\\Windows\\lib", "C:\\repo", REPO, False),
    ("tool -o:C:\\x", "C:\\repo", REPO, False),
    ("tool -IC:/Windows/inc", "C:\\repo", REPO, False),
    ("powershell Get-Content $env:USERPROFILE\\x", "C:\\repo", REPO, False),
    ("powershell Get-Content ${env:USERPROFILE}\\x", "C:\\repo", REPO, False),
    ("powershell Get-Content $env:TEMP", "C:\\repo", REPO, False),
    ("type %CD%\\..\\..\\x", "C:\\repo", REPO, False),
    ("type %CD%\\..\\x", "C:\\repo", REPO, False),
    ("echo a:b", "C:\\repo", REPO, True),
    ("git log --format=x:y", "C:\\repo", REPO, True),
    ("git log --format=%h:%s", "C:\\repo", REPO, True),
    ("tool -Iinclude", "C:\\repo", REPO, True),
    ("tool -IC:\\repo\\inc", "C:\\repo", REPO, True),
    ("tool /IC:\\repo\\inc", "C:\\repo", REPO, True),
    ("tool -o:C:\\repo\\x", "C:\\repo", REPO, True),
    ("type %CD%\\a", "C:\\repo", REPO, True),
    ("type %cd%\\a", "C:\\repo\\sub", REPO, True),
    # adversarial: several paths glued into one token, cmd quirks, rooted paths
    ("type C:\\repo\\a,C:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("type C:\\repo\\a=C:\\Windows\\x", "C:\\repo", REPO, False),
    ("type C:\\repo\\a:C:\\Windows\\x", "C:\\repo", REPO, False),
    ("type \"C:\\repo\\a,C:\\Windows\\x\"", "C:\\repo", REPO, False),
    ("cd..\\..", "C:\\repo\\a", REPO, False),
    ("cd..\\b", "C:\\repo\\a", REPO, True),
    ("type \\Windows\\win.ini", "C:\\repo", REPO, False),
    ("type \\repo\\x", "C:\\repo", REPO, True),
    ("type %~dp0\\x", "C:\\repo", REPO, False),
    ("type %CD%..\\..\\x", "C:\\repo", REPO, False),
    ("tool -IC:secret", "C:\\repo", REPO, False),
    ("type C:\\repo\\a >C:\\Windows\\o", "C:\\repo", REPO, False),
    ("type C:\\repo\\a;type C:\\Windows\\x", "C:\\repo", REPO, False),
    ("type C:\\repo\\a&type D:\\x", "C:\\repo", REPO, False),
    ("type \\\\?\\GLOBALROOT\\Device\\x", "C:\\repo", REPO, False),
    ("type \\\\?\\C:\\repo\\..\\x", "C:\\repo", REPO, False),
    ("type \"C:\\repo\\a\" \"D:\\x\"", "C:\\repo", REPO, False),
    ("type .\\..\\x", "C:\\repo", REPO, False),
    ("type C:\\\\repo\\\\x", "C:\\repo", REPO, True),
    ("findstr \"\\.py\" f.txt", "C:\\repo", REPO, True),
    ("type C:\\repo\\f.txt:stream", "C:\\repo", REPO, True),
    ("type .\\sub\\..\\f", "C:\\repo", REPO, True),
    ("echo hi > out.txt", "C:\\repo", REPO, True),
    ("dir /b", "C:\\repo", REPO, True),
    ("dir /s /b C:\\repo\\sub", "C:\\repo", REPO, True),
    ("dir /s /b C:\\Windows", "C:\\repo", REPO, False),
    # quoted rooted paths and cmd carets
    ("type \"\\Windows\\win.ini\"", "C:\\repo", REPO, False),
    ("type '\\Windows\\win.ini'", "C:\\repo", REPO, False),
    ("more < \"\\Windows\\win.ini\"", "C:\\repo", REPO, False),
    ("copy a.txt \"\\Users\\x\\b\"", "C:\\repo", REPO, False),
    ("type C^:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("type ^C:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("t^ype C:\\Windows\\win.ini", "C:\\repo", REPO, False),
    ("s^udo dir", "C:\\repo", REPO, False),
    ("type \"\\repo\\x\"", "C:\\repo", REPO, True),
    ("findstr /n \"\\.py\" a.txt", "C:\\repo", REPO, True),
    ("findstr \"\\d+\"", "C:\\repo", REPO, True),
    ("grep \"\\s*x\"", "C:\\repo", REPO, True),
    ("findstr \"\\.py$\"", "C:\\repo", REPO, True),
    ("type \".\\a b.txt\"", "C:\\repo", REPO, True),
    ("dir /s /b", "C:\\repo", REPO, True),
    ("npm run build", "C:\\repo", REPO, True),
    ("copy a.txt b.txt", "C:\\repo", REPO, True),
    ("type C^:\\repo\\a", "C:\\repo", REPO, True),
    ("echo \"C^:\\Windows\\x\"", "C:\\repo", REPO, True),  # caret kept literally inside double quotes, so this is not C:\\Windows
]


class WindowsPathTableTest(unittest.TestCase):
    def test_table(self):
        # No drive exists as far as _drive_relative_is_path knows, so lowercase x:y is text on every machine.
        with mock.patch.object(guards.os.path, "exists", return_value=False):
            for command, cwd, root, expected in WINDOWS_CASES:
                with self.subTest(command=command, cwd=cwd, root=str(root)):
                    ok, why = check(command, cwd, root)
                    self.assertEqual(ok, expected, why)
                    self.assertEqual(bool(why), not expected)

    def test_lowercase_drive_relative_word_when_drive_is_absent(self):
        with mock.patch.object(guards.os, "name", "nt"), mock.patch.object(guards.os.path, "exists", return_value=False):
            self.assertTrue(check("git log --format=x:y", "C:\\repo", REPO)[0])

    def test_lowercase_drive_relative_word_when_drive_exists(self):
        with mock.patch.object(guards.os, "name", "nt"), mock.patch.object(guards.os.path, "exists", return_value=True):
            ok, why = check("git log --format=x:y", "C:\\repo", REPO)
            self.assertFalse(ok)
            self.assertIn("drive-relative", why)

    def test_any_root_may_match(self):
        self.assertTrue(check("type \\\\server\\share\\f", "C:\\repo", REPO, SHARE)[0])
        self.assertTrue(check("type C:\\repo\\f", "C:\\repo", REPO, SHARE)[0])
        self.assertFalse(check("type C:\\other\\f", "C:\\repo", REPO, SHARE)[0])

    def test_drive_root(self):
        self.assertTrue(check("type C:\\any\\thing", "C:\\", Path("C:\\"))[0])
        self.assertFalse(check("type D:\\any\\thing", "C:\\", Path("C:\\"))[0])

    def test_env_var_inside_root(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\repo\\home", "MYDIR": "C:\\elsewhere"}):
            self.assertTrue(check("type %USERPROFILE%\\x", "C:\\repo", REPO)[0])
            self.assertTrue(check("type %userprofile%\\x", "C:\\repo", REPO)[0])
            self.assertFalse(check("type %MYDIR%\\x", "C:\\repo", REPO)[0])

    def test_printf_percent_is_not_an_env_path(self):
        self.assertTrue(check("printf '%s%s' a b", "C:\\repo", REPO)[0])

    def test_still_blocks_escalation_in_windows_mode(self):
        self.assertFalse(check("sudo dir", "C:\\repo", REPO)[0])

    def test_urls_and_embedded_drive_text_are_not_paths(self):
        for command in ("curl -s https://x.example/a/b", "echo abcC:\\notapath", "echo ab:/cd"):
            with self.subTest(command=command):
                self.assertTrue(guards._WIN_TOKEN_RE.search(command) is None, command)

    def test_system_paths_still_allowed(self):
        self.assertTrue(check("type x > /dev/null", "C:\\repo", REPO)[0])


class PosixNoRegressionTest(unittest.TestCase):
    """With POSIX roots a backslash is not a separator and Windows tokens are not paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def ok(self, command):
        return guards.command_stays_in_workspace(command, str(self.root), [self.root])

    @unittest.skipIf(os.name == "nt", "POSIX roots only")
    def test_backslashes_are_not_paths(self):
        for command in (
            "grep 'a\\.b' file.txt",
            "sed 's/\\//x/' file.txt",
            "echo a\\ b",
            "echo \"a\\\\b\"",
            "grep -E 'x\\.\\.y' f",
            "printf '%s%s\\n' a b",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.ok(command)[0], self.ok(command)[1])

    @unittest.skipIf(os.name == "nt", "POSIX roots only")
    def test_windows_tokens_do_not_trigger_in_posix_mode(self):
        self.assertFalse(guards._windows_mode([self.root]))
        self.assertTrue(self.ok("echo C:\\Windows\\win.ini")[0])

    @unittest.skipIf(os.name == "nt", "POSIX roots only")
    def test_posix_quotes_carets_env_unchanged(self):
        for command in ("grep -r \"\\\\d\" .", "echo C:\\Windows", "echo $env:FOO", "echo a^b", "echo \"\\Windows\\x\""):
            with self.subTest(command=command):
                self.assertTrue(self.ok(command)[0], self.ok(command)[1])

    @unittest.skipIf(os.name == "nt", "POSIX roots only")
    def test_posix_outside_paths_still_denied(self):
        for command in ("cat /etc/passwd", "cat ~/x", "cat ../../x", "ls $HOME", "cat /"):
            with self.subTest(command=command):
                self.assertFalse(self.ok(command)[0])

    @unittest.skipIf(os.name == "nt", "POSIX roots only")
    def test_posix_inside_path_allowed(self):
        self.assertTrue(self.ok(f"cat {self.root}/a/b")[0])
        self.assertTrue(self.ok("cat ./a/../b")[0])


class WindowsRootsTest(unittest.TestCase):
    def test_allowed_roots_keeps_windows_paths_lexical_off_windows(self):
        if os.name == "nt":
            self.skipTest("resolve() is the real Windows behaviour there")
        payload = {"workspacePaths": ["C:/repo/"], "cwd": "C:\\repo\\sub", "artifactDirectoryPath": "\\\\server\\share\\art"}
        roots = [str(r) for r in guards.allowed_roots(payload)]
        self.assertIn("C:\\repo", roots)
        self.assertIn("C:\\repo\\sub", roots)
        self.assertIn("\\\\server\\share\\art", roots)

    def test_within_roots_windows_targets(self):
        for target, expected in (
            ("C:\\repo\\a.txt", True),
            ("c:/Repo/a.txt", True),
            ("C:\\repo2\\a.txt", False),
            ("C:\\repo\\..\\x", False),
            ("file://C:/repo/a", True),
            ("D:\\repo\\a", False),
            ("\\\\server\\share\\a", False),
        ):
            with self.subTest(target=target):
                self.assertEqual(guards._within_roots(target, [REPO]), expected)

    def test_windows_roots_with_posix_targets(self):
        if os.name == "nt":
            self.skipTest("a /posix path resolves onto the current drive on Windows")
        self.assertFalse(guards._within_roots("/etc/hosts", [REPO]))


@unittest.skipUnless(os.name == "nt", "needs a real Windows filesystem (8.3 names, realpath)")
class RealWindowsTest(unittest.TestCase):
    def test_temp_dir_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inside = os.path.join(tmp, "sub", "f.txt")
            self.assertTrue(guards.command_stays_in_workspace(f'type "{inside}"', tmp, [root])[0])
            self.assertTrue(guards.command_stays_in_workspace(f"type {str(root).upper()}\\x", tmp, [root])[0])
            self.assertFalse(guards.command_stays_in_workspace("type C:\\Windows\\win.ini", tmp, [root])[0])
            self.assertFalse(guards.command_stays_in_workspace("cd ..\\..", tmp, [root])[0])


if __name__ == "__main__":
    unittest.main()
