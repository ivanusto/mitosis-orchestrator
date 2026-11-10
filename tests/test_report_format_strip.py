#!/usr/bin/env python3
"""Format-only instructions that name no canonical headers are dropped before classification, and the
SessionStart notice no longer sends subagents looking for a file.  Run: python -m unittest discover tests"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("enforce_orchestrator_fmt_strip", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)
from mitosis_hook.report import strip_report_format  # noqa: E402
from mitosis_hook import models  # noqa: E402

SCOUT = "mitosis-orchestrator:flash-lite-scout"
LOOKUP = "Find which file defines main()."
REAL = (
    "In the directory /tmp/proj, list all Python (*.py) files (recursively, excluding .git), and determine which one "
    "defines a function named main() (i.e. contains `def main(`). Read-only task; do not modify anything.\n\n"
    "Follow the four-line REPORT FORMAT from rules/AGENTS.md if you can locate it; otherwise end your reply with exactly these four lines:\n"
    "STATUS: <done|partial|blocked>\n"
    "FILES: <comma-separated list of all Python files found>\n"
    "FINDING: <path of the file that defines main(), with line number>\n"
    'NOTES: <anything uncertain or worth flagging, or "none">'
)
TEMPLATE = "STATUS: <done>\nFILES: <list>\nFINDING: <path>\nNOTES: <none>"

STAYS_SCOUT = [
    REAL,
    LOOKUP + " Follow the REPORT FORMAT.",
    LOOKUP + " Use the REPORT FORMAT from rules/AGENTS.md.",
    LOOKUP + " End your reply with the four-line REPORT FORMAT (if you can find it).",
    LOOKUP + "\nOtherwise end your reply with exactly these four lines:\n" + TEMPLATE,
    LOOKUP + "\nReply with these lines:\n" + TEMPLATE + "\nFollow the REPORT FORMAT if you can locate it.",
    "找出哪個檔案定義了 main()。依照 rules/AGENTS.md 的 REPORT FORMAT 回報",
    "找出哪個檔案定義了 main()。\n最後用以下四行回報：\n" + TEMPLATE,
]
BECOMES_WORKER = [
    LOOKUP + " Follow the REPORT FORMAT and delete build/.",
    LOOKUP + " Follow the REPORT FORMAT; then run npm publish.",
    LOOKUP + "\nReply with these lines:\n" + TEMPLATE + "\nThen delete the old helper in util.py.",
    LOOKUP + " Follow the REPORT FORMAT, then commit the change.",
    "找出哪個檔案定義了 main()。依照 REPORT FORMAT 回報並刪除 build/",
    "找出哪個檔案定義了 main()。\n最後用以下四行回報：\n" + TEMPLATE + "\n然後刪除 build/",
]

SEQ_WORKER = ['Use the REPORT FORMAT. Afterwards push the branch to origin.', 'Afterwards push the branch to origin.', 'Find main(). Afterwards push the branch to origin.', 'Find main(). Afterward push the branch.', 'Find main(). Finally run npm publish.', 'Find main(). Once you are done, push the branch.', 'Find main(). Lastly, restart nginx.', 'Find main(). After that push the branch.', 'Find main(). Additionally push the branch to origin.', 'Where is parse_args defined? Next, deploy it to staging.', 'Find main().\nThen delete the build/ directory.', '找出 main() 在哪。然後把 build 刪掉。', '找出 main() 在哪。完成後把變更推到遠端。', '找出 main() 在哪。最後把 build 刪掉。']
SEQ_SCOUT = ['Which file defines main()? Then report which file defines main().', 'Which file defines main()? Finally list the callers.', 'Which file defines main()? 然後告訴我哪個檔案定義 main()。', 'Find main(). Afterwards list the callers of main().', 'Find main(). Then tell me the line number.', 'Where is parse_args defined? Next, show the signature.', 'Which file defines parse_args()? Use the four-line REPORT FORMAT from rules/AGENTS.md.', 'Find main(). Once you are done, report the path.', '哪個檔案定義了 main()？之後說明它的用途。', '哪個檔案定義了 main()？最後回報行號。']


def tier(prompt):
    saved = os.environ.pop("MITOSIS_JUDGE_URL", None)
    try:
        return eo.decide_subagent({"prompt": prompt, "subagent_type": SCOUT})
    finally:
        if saved is not None:
            os.environ["MITOSIS_JUDGE_URL"] = saved


class TestStripFormatInstructions(unittest.TestCase):
    def test_real_prompt_loses_every_format_line(self):
        out = strip_report_format(REAL)
        self.assertNotIn("REPORT FORMAT", out)
        self.assertNotIn("FINDING", out)
        self.assertNotIn("otherwise", out)
        self.assertIn("Read-only task", out)

    def test_text_after_the_block_is_kept(self):
        out = strip_report_format(LOOKUP + "\nReply with these lines:\n" + TEMPLATE + "\nThen delete util.py.")
        self.assertIn("Then delete util.py.", out)

    def test_compound_action_is_kept(self):
        self.assertIn("delete build/", strip_report_format("Follow the REPORT FORMAT and delete build/."))
        self.assertIn("npm publish", strip_report_format("Follow the REPORT FORMAT; then run npm publish."))

    def test_two_header_lines_are_not_a_template(self):
        text = "Fix it.\nSTATUS: <x>\nNOTES: <y>"
        self.assertEqual(strip_report_format(text), text)

    def test_stays_scout(self):
        for prompt in STAYS_SCOUT:
            with self.subTest(prompt=prompt):
                d = tier(prompt)
                self.assertEqual(d.tier, "flash_lite", f"rule={d.rule!r} reason={d.reason!r}")

    def test_becomes_worker(self):
        for prompt in BECOMES_WORKER:
            with self.subTest(prompt=prompt):
                self.assertEqual(tier(prompt).tier, "flash")


class TestSequencingWords(unittest.TestCase):
    def test_sequenced_actions_go_to_the_worker(self):
        for prompt in SEQ_WORKER:
            with self.subTest(prompt=prompt):
                d = tier(prompt)
                self.assertEqual(d.tier, "flash", f"rule={d.rule!r}")

    def test_sequenced_reads_stay_scout(self):
        for prompt in SEQ_SCOUT:
            with self.subTest(prompt=prompt):
                d = tier(prompt)
                self.assertEqual(d.tier, "flash_lite", f"rule={d.rule!r}")


class TestNoticeDoesNotPointAtAFile(unittest.TestCase):
    def test_notices(self):
        for text in (models.POLICY_NOTICE, models.claude_policy_notice(), models.codex_policy_notice()):
            self.assertNotIn("must end with the four-line REPORT FORMAT from rules/AGENTS.md", text)
            self.assertIn("do not ask subagents to find rules/AGENTS.md", text)
            self.assertIn("STATUS / FILES_CHANGED / VERIFICATION / NOTES", text)


if __name__ == "__main__":
    unittest.main()
