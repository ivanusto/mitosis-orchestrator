#!/usr/bin/env python3
"""agy 1.5.x toolset tests: generated agent tool lists and scout instructions, SessionStart notice.
Run: python -m unittest tests.test_agy_toolset -v"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mitosis_hook import models  # noqa: E402

# agy 1.5.5: an old name and its new name in one tools list fail to build the agent.
ALIASES = {"view_file": "read_file", "replace_file_content": "edit_file", "write_to_file": "write_file"}


def split(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    body = text.split("---", 2)[2]
    tools = re.findall(r"^\s*-\s*(\S+)\s*$", front.split("tools:", 1)[1].split("inheritCustomizations", 1)[0], re.M)
    return tools, body


class AgyToolsetTests(unittest.TestCase):
    def test_scout_tools(self):
        tools, _ = split("agents/flash-lite-scout/agent.md")
        self.assertIn("view_file", tools)  # resolves to read_file on agy 1.5.x
        for bad in ("run_command", "edit_file", "write_file", "replace_file_content", "write_to_file"):
            self.assertNotIn(bad, tools)

    def test_scout_body(self):
        _, body = split("agents/flash-lite-scout/agent.md")
        self.assertIn("read_file", body)
        self.assertIn("BLOCKED", body)
        self.assertNotIn("Use `find_by_name`, `grep_search`, `list_dir`", body)

    def test_worker_tools(self):
        tools, _ = split("agents/flash-worker/agent.md")
        for good in ("view_file", "replace_file_content", "write_to_file", "run_command"):
            self.assertIn(good, tools)

    def test_no_alias_pair_in_one_list(self):
        for path in ("agents/flash-lite-scout/agent.md", "agents/flash-worker/agent.md"):
            tools, _ = split(path)
            for old, new in ALIASES.items():
                with self.subTest(path=path, old=old):
                    self.assertFalse(old in tools and new in tools, f"{old} and {new} conflict on agy 1.5.x")

    def test_policy_notice(self):
        n = models.POLICY_NOTICE
        self.assertIn("exact paths", n)
        self.assertIn("read_file", n)
        self.assertIn("`flash`", n)
        self.assertIn("REPORT FORMAT", n)


if __name__ == "__main__":
    unittest.main()
