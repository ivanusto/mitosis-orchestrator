#!/usr/bin/env python3
"""file:// link targets are not counted in the report length, and the primary may read small logs.
Run: python -m unittest discover tests"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import test_enforce_orchestrator as T  # noqa: E402
from mitosis_hook.report import _report_problem, report_effective_length  # noqa: E402

eo = T.eo
HEAD = "STATUS: COMPLETED\nFILES_CHANGED: NONE\nVERIFICATION: ran tests\nNOTES:\n"


def _link(i, root):
    return f"[f{i}.py](file://{root}/pkg/sub/f{i}.py#L1-L20)"


class ReportLinkLengthTest(unittest.TestCase):
    def test_links_collapse_to_text(self):
        self.assertEqual(report_effective_length("a [x](file:///a/b.py#L1) b"), len("a x b"))
        self.assertEqual(report_effective_length("[](file:///a/b)"), len("link"))
        http = "[x](https://example.invalid/aaaa)"
        self.assertEqual(report_effective_length(http), len(http))

    def test_overlong_file_link_target_counts_in_full(self):
        short = "[a](file://" + "x" * 505 + ")"
        long_ = "[a](file://" + "x" * 506 + ")"
        self.assertEqual(report_effective_length(short), 1)
        self.assertEqual(report_effective_length(long_), len(long_))
        text = HEAD + "- [a](file:///" + "a" * 1700 + ")"
        self.assertEqual(_report_problem(text, require_log_uri=False)[0], "too_long")

    def test_many_links_pass(self):
        root = "/work/" + "d" * 30
        body = "\n".join("- " + _link(i, root) for i in range(8))
        text = HEAD + body
        pad = 1968 - len(text)
        text += "\n- " + "n" * (pad - 3)
        self.assertEqual(len(text), 1968)
        self.assertLess(report_effective_length(text), 1600)
        self.assertIsNone(_report_problem(text, require_log_uri=False))

    def test_long_report_with_one_link_fails(self):
        text = HEAD + "- " + _link(1, "/work") + "\n- " + "n" * 2700
        self.assertGreater(len(text), 2800 - 100)
        self.assertEqual(_report_problem(text, require_log_uri=False)[0], "too_long")

    def test_limit_with_http_link_unchanged(self):
        base = HEAD + "- [d](https://example.invalid/x) "
        ok = base + "n" * (1600 - len(base))
        self.assertEqual(len(ok), 1600)
        self.assertIsNone(_report_problem(ok, require_log_uri=False))
        self.assertEqual(_report_problem(ok + "n", require_log_uri=False)[0], "too_long")


def _write(directory, name, size_lines=None, size_bytes=None):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        if size_lines is not None:
            fh.write("".join(f"line {i} started ok\n" for i in range(size_lines)))
        else:
            fh.write("x" * size_bytes)
    return path


class AgySmallLogTest(T.HookBase):
    def tool(self, name, args, **extra):
        return dict(self.payload, toolCall={"name": name, "args": args}, **extra)

    def read(self, path):
        return eo.handle_pre_tool_use(self.tool("view_file", {"AbsolutePath": path}))

    def test_small_allowed_big_denied(self):
        small = _write(self.tmp.name, "start.log", size_lines=25)
        big = _write(self.tmp.name, "big.log", size_bytes=20 * 1024)
        self.assertEqual(self.read(small)["decision"], "allow")
        self.assertEqual(self.read(big)["decision"], "deny")
        self.assertEqual(self.read(os.path.join(self.tmp.name, "missing.log"))["decision"], "deny")

    def test_env_zero_disables(self):
        small = _write(self.tmp.name, "start.log", size_lines=25)
        os.environ["AGY_MITOSIS_SMALL_LOG_BYTES"] = "0"
        self.assertEqual(self.read(small)["decision"], "deny")

    def test_invalid_env_uses_default(self):
        small = _write(self.tmp.name, "start.log", size_lines=25)
        os.environ["AGY_MITOSIS_SMALL_LOG_BYTES"] = "abc"
        self.assertEqual(self.read(small)["decision"], "allow")


class ClaudeSmallLogTest(T.ClaudeHookBase):
    def read(self, path):
        call = dict(self.payload, tool_name="Read", tool_input={"file_path": path})
        return eo.handle_pre_tool_use(call)

    def test_small_allowed_big_denied(self):
        small = _write(str(self.project_dir), "start.log", size_lines=25)
        big = _write(str(self.project_dir), "big.log", size_bytes=20 * 1024)
        self.assertEqual(self.read(small), {})
        self.assertEqual(self.read(big)["hookSpecificOutput"]["permissionDecision"], "deny")
        missing = os.path.join(str(self.project_dir), "missing.log")
        self.assertEqual(self.read(missing)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_relative_path_resolves_against_cwd(self):
        _write(str(self.project_dir), "rel.log", size_lines=3)
        self.assertEqual(self.read("rel.log"), {})

    def test_env_zero_disables(self):
        small = _write(str(self.project_dir), "start.log", size_lines=25)
        os.environ["CLAUDE_MITOSIS_SMALL_LOG_BYTES"] = "0"
        self.assertEqual(self.read(small)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
