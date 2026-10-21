#!/usr/bin/env python3
"""CODEX_MITOSIS_* tier model and effort overrides.
Run: python -m unittest discover tests"""

import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eo = _load("eo_codex_overrides", "enforce-orchestrator.py")
mcp = _load("mcp_codex_overrides", "mitosis-mcp-server.py")
setup_codex = _load("setup_codex_overrides", "setup-codex.py")

SCOUT = "gpt-5.6-luna"
WORKER = "gpt-5.6-terra"
LOOKUP = "Find where main is defined in this directory and report the file name."


def codex_pre(tool_name, tool_input):
    return {
        "session_id": "ovr-1", "transcript_path": "/nonexistent/rollout.jsonl", "cwd": "/tmp",
        "hook_event_name": "PreToolUse", "model": SCOUT, "permission_mode": "bypassPermissions",
        "turn_id": "t-1", "tool_name": tool_name, "tool_input": tool_input, "tool_use_id": "c1",
    }


class CodexOverridesTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(ENV_PREFIXES):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TMPDIR"] = self.tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_defaults_without_overrides(self):
        self.assertEqual(eo.codex_tier_model("flash_lite"), SCOUT)
        self.assertEqual(eo.codex_tier_model("flash"), WORKER)
        self.assertEqual(eo.codex_tier_effort("flash_lite"), "low")
        self.assertEqual(eo.codex_tier_effort("flash"), "medium")

    def test_override_applied_at_spawn_agent_rewrite(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        os.environ["CODEX_MITOSIS_WORKER_MODEL"] = "gpt-5.7"
        out = eo.handle_pre_tool_use(codex_pre("spawn_agent", {"message": LOOKUP}))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-5.7-mini")
        out = eo.handle_pre_tool_use(codex_pre("spawn_agent", {"message": "Edit app.py and fix the bug, then write the changes."}))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-5.7")

    def test_override_applied_at_codex_exec_pinning(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        out = eo.handle_pre_tool_use(codex_pre("Bash", {"command": f'codex exec "{LOOKUP}"'}))
        cmd = out["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn("-m gpt-5.7-mini", cmd)

    def test_override_applied_at_mcp_decide_task(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        os.environ["CODEX_MITOSIS_WORKER_MODEL"] = "gpt-5.7"
        model, _, _ = mcp.decide_task({"prompt": LOOKUP})
        self.assertEqual(model, "gpt-5.7-mini")
        model, _, _ = mcp.decide_task({"prompt": "Edit app.py and fix the bug, then write the changes."})
        self.assertEqual(model, "gpt-5.7")

    def test_mcp_accepts_effective_worker_as_explicit_model(self):
        os.environ["CODEX_MITOSIS_WORKER_MODEL"] = "gpt-5.7"

        def runner(**kw):
            return 0, "", ""

        res = mcp.execute_subagent_task({"prompt": LOOKUP, "model": "gpt-5.7"}, runner=runner)
        self.assertEqual(res["model"], "gpt-5.7")

    def test_invalid_model_is_ignored(self):
        for bad in ('gpt-5.6"; rm -rf /', "gpt\\5", "-leading", "a b", "x" * 80, "$(id)"):
            os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = bad
            os.environ["CODEX_MITOSIS_WORKER_MODEL"] = bad
            self.assertEqual(eo.codex_tier_model("flash_lite"), SCOUT, bad)
            self.assertEqual(eo.codex_tier_model("flash"), WORKER, bad)
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = 'gpt-5.6"; rm -rf /'
        out = eo.handle_pre_tool_use(codex_pre("Bash", {"command": f'codex exec "{LOOKUP}"'}))
        cmd = out["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertNotIn("rm -rf", cmd)
        self.assertIn(f"-m {SCOUT}", cmd)

    def test_effort_validation(self):
        os.environ["CODEX_MITOSIS_WORKER_EFFORT"] = "HIGH"
        self.assertEqual(eo.codex_tier_effort("flash"), "high")
        os.environ["CODEX_MITOSIS_WORKER_EFFORT"] = "minimal"
        self.assertEqual(eo.codex_tier_effort("flash"), "minimal")
        os.environ["CODEX_MITOSIS_WORKER_EFFORT"] = "turbo"
        self.assertEqual(eo.codex_tier_effort("flash"), "medium")

    def test_claude_and_agy_vars_do_not_affect_codex(self):
        os.environ["CLAUDE_MITOSIS_SCOUT_MODEL"] = "sonnet"
        os.environ["CLAUDE_MITOSIS_SCOUT_EFFORT"] = "high"
        os.environ["AGY_MITOSIS_SCOUT_MODEL"] = "haiku"
        self.assertEqual(eo.codex_tier_model("flash_lite"), SCOUT)
        self.assertEqual(eo.codex_tier_effort("flash_lite"), "low")

    def test_policy_notice_names_effective_models(self):
        self.assertIn(SCOUT, eo.codex_policy_notice())
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        self.assertIn("gpt-5.7-mini", eo.codex_policy_notice())
        self.assertNotIn(SCOUT, eo.codex_policy_notice())

    def test_mcp_tool_descriptions_use_effective_models(self):
        text = " ".join(t["description"] for t in mcp.TOOLS_DEFINITIONS)
        self.assertIn(SCOUT, text)
        self.assertIn(WORKER, text)

    def test_setup_codex_renders_overrides(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = "gpt-5.7-mini"
        os.environ["CODEX_MITOSIS_WORKER_EFFORT"] = "high"
        home = Path(self.tmp.name) / "codex-home"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            setup_codex.setup_codex(codex_home=home)
        scout = (home / "agents" / "explorer.toml").read_text(encoding="utf-8")
        self.assertRegex(scout, r'(?m)^model = "gpt-5.7-mini"$')
        self.assertRegex(scout, r'(?m)^model_reasoning_effort = "low"$')
        worker = (home / "agents" / "flash-worker.toml").read_text(encoding="utf-8")
        self.assertRegex(worker, r'(?m)^model = "gpt-5.6-terra"$')
        self.assertRegex(worker, r'(?m)^model_reasoning_effort = "high"$')
        profile = (home / "mitosis-scout.config.toml").read_text(encoding="utf-8")
        self.assertRegex(profile, r'(?m)^model = "gpt-5.7-mini"$')
        cfg = (home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.mitosis]", cfg)
        self.assertRegex(cfg, r'env = \{ [^}]*CODEX_MITOSIS_SCOUT_MODEL = "gpt-5.7-mini"')
        self.assertIn('CODEX_MITOSIS_WORKER_EFFORT = "high"', cfg)
        self.assertNotIn("CODEX_MITOSIS_WORKER_MODEL", cfg)

    def test_setup_codex_without_overrides_is_byte_identical(self):
        home = Path(self.tmp.name) / "codex-home2"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            setup_codex.setup_codex(codex_home=home)
        for name in ("explorer.toml", "flash-lite-scout.toml", "flash-worker.toml", "worker.toml"):
            self.assertEqual((home / "agents" / name).read_bytes(), (ROOT / "codex-agents" / name).read_bytes())
        for name in ("mitosis-scout.config.toml", "mitosis-worker.config.toml"):
            self.assertEqual((home / name).read_bytes(), (ROOT / "codex-config" / name).read_bytes())
        self.assertNotIn("env =", (home / "config.toml").read_text(encoding="utf-8"))

    def test_setup_codex_ignores_invalid_override(self):
        os.environ["CODEX_MITOSIS_SCOUT_MODEL"] = 'x"\nmodel = "evil'
        home = Path(self.tmp.name) / "codex-home3"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            setup_codex.setup_codex(codex_home=home)
        self.assertEqual((home / "agents" / "explorer.toml").read_bytes(), (ROOT / "codex-agents" / "explorer.toml").read_bytes())
        self.assertIsNone(re.search("evil", (home / "config.toml").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
