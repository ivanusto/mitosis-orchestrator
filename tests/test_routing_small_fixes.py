#!/usr/bin/env python3
"""2.4 small fixes: scope-constraint and answer-format sentences in scout prompts, and the
scout Bash allowlist for uptime, systemctl and journalctl.  Run: python -m unittest discover tests"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_hardening  # noqa: E402  (EnvCase: payload and claude_tool helpers)

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("enforce_orchestrator_small_fixes", ROOT / "scripts" / "enforce-orchestrator.py")
eo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eo)

SCOUT = "mitosis-orchestrator:flash-lite-scout"
BLOCK = "STATUS: COMPLETED | BLOCKED | FAILED\nFILES_CHANGED: NONE\nVERIFICATION: <what you searched>\nNOTES: <findings>"
BOLD = "**STATUS:** COMPLETED | BLOCKED | FAILED\n**FILES_CHANGED:** NONE\n**VERIFICATION:** <what you searched>\n**NOTES:** <findings>"
LOOKUP = "Find which file defines main().\n"

STAYS_SCOUT = [
    "Find which file defines main(). Exclude the .git directory.",
    "Find which file defines main(). Ignore node_modules.",
    "List the callers of parse_token. Omit vendored copies from the results.",
    LOOKUP + "End your reply with this report format:\n" + BLOCK,
    LOOKUP + "Use this report format:\n" + BLOCK,
    LOOKUP + "Reply with exactly these lines:\n" + BLOCK,
    LOOKUP + "Your final message must follow this format:\n" + BLOCK,
    LOOKUP + "Finish with:\n" + BLOCK,
    LOOKUP + "End your reply with this report format:\n" + BOLD,
    "找出哪個檔案定義了 main()。\n最後請用以下格式回報：\n" + BLOCK,
    LOOKUP + "End your reply with this report format:\n" + BLOCK + "\n- found in src/main.c\n- no other definitions",
    "Find which file defines main(). End your reply with the four headers.",
    "Where is parse_token defined? Keep your answer under 3 lines.",
]

BECOMES_WORKER = [
    "Exclude vendor/ from tsconfig.json.",
    "Find the config. Exclude vendor/ from tsconfig.json.",
    "Find the flaky test. Exclude the flaky test from CI.",
    "Find the config. Ignore build/ in .gitignore.",
    "Fix the bug and report in this format:\nSTATUS: x\nFILES_CHANGED: x\nVERIFICATION: x\nNOTES: x",
    "Find main().\nSTATUS: x\nFILES_CHANGED: x\nVERIFICATION: x\nNOTES: x\nThen delete the old helper in util.py.",
    "Find main(). End your reply by committing the change.",
    "Find main(). Use this template to rewrite the README.",
    "Find main(). Format the report file with black.",
]

STAYS_SCOUT += [
    "systemctl --failed", "journalctl -u nginx -n 20", "systemctl status nginx", "uptime",
    "Find main(). End your reply by listing the files.", "Find main(). Ignore tests/ in your search.",
]
BECOMES_WORKER += [
    "systemctl restart nginx", "systemctl stop nginx", "journalctl -f", "journalctl --vacuum-size=1G",
    "Exclude tests from coverage.", "Find X. Exclude it from the build.", "Find main(). End the report by opening a PR.",
]


def tier(prompt):
    saved = os.environ.pop("MITOSIS_JUDGE_URL", None)
    try:
        return eo.decide_subagent({"prompt": prompt, "subagent_type": SCOUT})
    finally:
        if saved is not None:
            os.environ["MITOSIS_JUDGE_URL"] = saved


class TestScoutPromptRouting(unittest.TestCase):
    def check(self, prompts, want):
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                d = tier(prompt)
                self.assertEqual(d.tier, want, f"rule={d.rule!r} reason={d.reason!r}")

    def test_scope_and_format_sentences_keep_the_scout(self):
        self.check(STAYS_SCOUT, "flash_lite")

    def test_danger_direction_goes_to_the_worker(self):
        self.check(BECOMES_WORKER, "flash")


class TestScoutSystemCommands(unittest.TestCase):
    ALLOWED = [
        "uptime", "systemctl --failed", "systemctl --failed --no-pager", "systemctl status nginx",
        "systemctl is-active nginx", "systemctl list-units --type=service", "systemctl --user list-timers",
        "journalctl -n 50", "journalctl -u nginx --since today --no-pager", "journalctl -xe -n 20",
        "journalctl -b -p err", "journalctl -u nginx | tail -5",
    ]
    DENIED = [
        "systemctl restart nginx", "systemctl stop nginx", "systemctl daemon-reload", "systemctl --user daemon-reload",
        "systemctl enable --now nginx", "systemctl edit nginx", "systemctl -H host status", "systemctl --host=host status",
        "systemctl status nginx; systemctl stop nginx", "journalctl -f", "journalctl -fu nginx", "journalctl -uf nginx",
        "journalctl --follow", "journalctl --vacuum-size=1G", "journalctl --vacuum-time=1d", "journalctl --rotate",
        "journalctl --flush", "journalctl --setup-keys", "uptime > /tmp/x", "sudo systemctl status nginx",
    ]

    def test_allowed(self):
        for cmd in self.ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(eo.scout_command_is_read_only(cmd), (True, ""))

    def test_denied(self):
        for cmd in self.DENIED:
            with self.subTest(cmd=cmd):
                self.assertFalse(eo.scout_command_is_read_only(cmd)[0])

    def test_is_read_only_command_prompt_rule(self):
        for cmd in ("uptime", "systemctl --failed", "journalctl -n 50"):
            with self.subTest(cmd=cmd):
                self.assertTrue(eo.is_read_only_command(cmd))
        self.assertFalse(eo.is_read_only_command("systemctl restart nginx"))


class TestScoutHookEndToEnd(test_hardening.EnvCase):
    def hook(self, cmd):
        return eo.handle_pre_tool_use(self.claude_tool("Bash", {"command": cmd}, agent_id="a1", agent_type=SCOUT))

    def test_systemctl_failed_passes(self):
        self.assertEqual(self.hook("systemctl --failed"), {})

    def test_systemctl_stop_is_denied(self):
        out = self.hook("systemctl stop nginx")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
