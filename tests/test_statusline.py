#!/usr/bin/env python3
"""Claude Code statusline script. Run: python -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "mitosis-statusline.py"
SID = "sl-session/1"


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MITOSIS_")}
        self.env["TMPDIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, state, sid="sl-session_1"):
        d = Path(self.tmp.name) / "mitosis-orchestrator" / sid
        d.mkdir(parents=True)
        (d / "mitosis_orchestrator_state.json").write_text(json.dumps(state))

    def run_script(self, stdin, extra=None):
        env = dict(self.env)
        env.update(extra or {})
        proc = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True,
                              env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return proc.stdout

    def payload(self):
        return json.dumps({"session_id": SID, "model": {"display_name": "Opus"}, "cost": {"total_cost_usd": 1.234}})

    def test_mixed_tiers_and_old_records(self):
        self.write_state({
            "subagents": {
                "a": {"tier": "flash_lite", "alive": True},
                "b": {"tier": "flash_lite", "alive": False},
                "c": {"model": "haiku"},
                "d": {"tier": "flash", "alive": False},
                "e": {"model": "sonnet"},
            },
            "pending_spawns": [{"tier": "flash"}],
        })
        self.assertEqual(self.run_script(self.payload()), "mitosis: scout 3 (1 live) | worker 3 | Opus | $1.23\n")

    def test_prefix_override(self):
        self.write_state({"subagents": {"a": {"tier": "flash"}}})
        out = self.run_script(self.payload(), {"MITOSIS_STATUSLINE_PREFIX": "mx"})
        self.assertEqual(out, "mx: worker 1 | Opus | $1.23\n")

    def test_empty_stdin(self):
        self.assertEqual(self.run_script(""), "mitosis: idle\n")

    def test_garbage_stdin(self):
        self.assertEqual(self.run_script("{not json"), "mitosis: idle\n")
        self.assertEqual(self.run_script("[1, 2]"), "mitosis: idle\n")

    def test_missing_state(self):
        self.assertEqual(self.run_script(self.payload()), "mitosis: idle\n")

    def test_corrupt_state(self):
        self.write_state({"subagents": "oops", "pending_spawns": 5})
        self.assertEqual(self.run_script(self.payload()), "mitosis: idle\n")


if __name__ == "__main__":
    unittest.main()
