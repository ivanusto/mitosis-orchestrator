#!/usr/bin/env python3
"""v1.5.1 hardening: permission pass-through, CJK and noun-aware routing,
scout Bash allowlist, frontier detection, state paths, judge key transport,
MCP framing and setup-codex backups.  Run: python -m unittest discover tests"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eo = _load("enforce_orchestrator_hardening", "enforce-orchestrator.py")
mj = _load("mitosis_judge_hardening", "mitosis_judge.py")
mcp = _load("mitosis_mcp_server_hardening", "mitosis-mcp-server.py")
setup = _load("setup_codex_hardening", "setup-codex.py")

ENV_PREFIXES = ("AGY_", "CLAUDE_", "CODEX_", "MITOSIS_")


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(ENV_PREFIXES):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.payload = {
            "session_id": f"hardening-{id(self)}",
            "artifactDirectoryPath": str(Path(self.tmp.name) / "artifacts"),
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "permission_mode": "default",
        }

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def claude_tool(self, name, tool_input, **extra):
        p = dict(self.payload, tool_name=name, tool_input=tool_input)
        p.update(extra)
        return p


class TestClaudePermissionPassThrough(EnvCase):
    def test_tools_get_no_verdict_without_auto_allow(self):
        for name, args in (
            ("Bash", {"command": "rm -rf build"}),
            ("Write", {"file_path": str(self.project / "a.py")}),
            ("Edit", {"file_path": "/etc/hosts"}),
            ("Read", {"file_path": str(self.project / "a.py")}),
        ):
            with self.subTest(tool=name):
                self.assertEqual(eo.handle_pre_tool_use(self.claude_tool(name, args)), {})

    def test_auto_allow_still_allows_inside_workspace_only(self):
        os.environ["CLAUDE_MITOSIS_AUTO_ALLOW"] = "1"
        inside = eo.handle_pre_tool_use(self.claude_tool("Read", {"file_path": str(self.project / "a.py")}))
        self.assertEqual(inside["hookSpecificOutput"]["permissionDecision"], "allow")
        outside = eo.handle_pre_tool_use(self.claude_tool("Read", {"file_path": "/etc/shadow"}))
        self.assertEqual(outside["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_hook_crash_does_not_allow(self):
        payload = self.claude_tool("Bash", {"command": "ls"})
        original = eo.HANDLERS["pre-tool-use"]
        eo.HANDLERS["pre-tool-use"] = lambda _p: 1 / 0
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv, sys.stdin, sys.stdout, sys.stderr
        try:
            sys.argv = ["enforce-orchestrator.py", "pre-tool-use"]
            sys.stdin, sys.stdout, sys.stderr = io.StringIO(json.dumps(payload)), out, err
            eo.main()
        finally:
            sys.argv, sys.stdin, sys.stdout, sys.stderr = old
            eo.HANDLERS["pre-tool-use"] = original
        self.assertEqual(json.loads(out.getvalue()), {})
        self.assertIn("hook error", err.getvalue())

    def test_antigravity_pass_through_unchanged(self):
        res = eo._no_opinion({"conversationId": "x", "toolCall": {"name": "view_file", "args": {}}})
        self.assertEqual(res["decision"], "allow")


class TestRoutingVocabulary(unittest.TestCase):
    def tier(self, prompt, **extra):
        return eo.decide_subagent(dict({"prompt": prompt}, **extra))

    def test_chinese_and_everyday_mutations_reach_flash(self):
        for prompt in (
            "修改 config.py 並刪除舊的設定",
            "請重構 auth 模組並執行測試",
            "把 timeout 改成 30 秒",
            "Add a docstring to foo()",
            "git commit and push the changes",
            "- Add a retry to fetch()\n- Bump version",
            "Please update the README",
        ):
            with self.subTest(prompt=prompt):
                d = self.tier(prompt)
                self.assertEqual((d.tier, d.rule), ("flash", "mutation"))

    def test_nouns_do_not_count_as_work(self):
        for prompt in (
            "Tell me the patch version",
            "Find the bug report template",
            "Where is the test for parse_header?",
            "What does the migration guide say?",
            "List the test files",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.tier(prompt).tier, "flash_lite")

    def test_heavy_verbs_stay_heavy(self):
        for prompt in ("Run the tests in tests/", "Figure out why the build fails", "執行測試並回報結果"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.tier(prompt).tier, "flash")

    def test_chinese_negation_and_lookup(self):
        d = self.tier("不要修改任何檔案，找出 parse_header 在哪裡")
        self.assertEqual((d.tier, d.rule), ("flash_lite", "lite"))

    def test_scout_typed_chinese_mutation_upgrades(self):
        d = self.tier("刪除 legacy/ 目錄", subagent_type="mitosis-orchestrator:flash-lite-scout")
        self.assertEqual((d.tier, d.type_override), ("flash", "mitosis-orchestrator:flash-worker"))

    def test_later_imperative_beats_read_only_declaration(self):
        d = self.tier("Do not modify anything. Fix the typo in README")
        self.assertEqual((d.rule, d.confidence), ("mutation", eo.RULE_CONFIDENCE["mutation"]))
        d = self.tier("Read-only review: summarize fixes listed in CHANGELOG.md.")
        self.assertEqual(d.confidence, eo.READ_ONLY_CONFIDENCE)

    def test_callers_report_format_block_is_not_the_task(self):
        # Seen live on Codex: the primary's own "REPORT FORMAT ... TESTS:" made a lookup heavy.
        prompt = "Find where main is defined in this directory and report the file name.\n\nREPORT FORMAT\nSTATUS:\nSUMMARY:\nFILES:\nTESTS:"
        self.assertEqual(self.tier(prompt).tier, "flash_lite")
        self.assertEqual(self.tier("Run the tests. Report format: STATUS only.").tier, "flash")

    def test_send_message_to_scout_uses_new_vocabulary(self):
        self.assertEqual(eo.find_mutation("請順便修改 parser.py"), "修改")
        self.assertIsNone(eo.find_mutation("Also check the patch notes"))


class TestScoutBashAllowlist(EnvCase):
    SCOUT = "mitosis-orchestrator:flash-lite-scout"

    def test_read_only_commands_pass(self):
        for cmd in ("git log --oneline -5", "git status -sb 2>/dev/null", "rg -n foo src | head -20",
                    "ls -la && wc -l *.py", "git branch -a", "git config --get user.name", "tail -n 50 app.log"):
            with self.subTest(cmd=cmd):
                self.assertEqual(eo.scout_command_is_read_only(cmd), (True, ""))

    def test_writing_commands_fail(self):
        for cmd in ("git commit -m x", "git push", "rm -rf build", "echo hi > x", "cat $(ls)",
                    "find . -name '*.pyc' -delete", "sort -o out in", "git branch new", "git tag v1",
                    "git diff --output=x", "git -c core.pager=sh log", "python3 x.py", "sed -i s/a/b/ f",
                    "awk 'BEGIN{system(\"id\")}'"):
            with self.subTest(cmd=cmd):
                self.assertFalse(eo.scout_command_is_read_only(cmd)[0])

    def test_hook_denies_scout_write_but_not_worker(self):
        deny = eo.handle_pre_tool_use(self.claude_tool("Bash", {"command": "rm -rf build"},
                                                       agent_id="a1", agent_type=self.SCOUT))
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        ok = eo.handle_pre_tool_use(self.claude_tool("Bash", {"command": "git log -3"},
                                                     agent_id="a1", agent_type=self.SCOUT))
        self.assertEqual(ok, {})
        worker = eo.handle_pre_tool_use(self.claude_tool("Bash", {"command": "rm -rf build"}, agent_id="a2",
                                                         agent_type="mitosis-orchestrator:flash-worker"))
        self.assertEqual(worker, {})

    def test_scout_agent_file_lists_bash(self):
        text = (ROOT / "claude-agents" / "flash-lite-scout.md").read_text(encoding="utf-8")
        frontmatter = text.split("---")[1]
        self.assertIn("- Bash", frontmatter)
        self.assertNotIn("- Write", frontmatter)
        self.assertNotIn("- Edit", frontmatter)


class TestFrontierSegments(unittest.TestCase):
    def test_substrings_no_longer_match(self):
        for name in ("product-o1x", "deepseek-coder-1.3b", "qwen2.5-coder-0.5b", "improv-7b"):
            with self.subTest(name=name):
                self.assertFalse(eo.is_frontier_tier(name))

    def test_current_frontier_names(self):
        for name in ("claude-opus-5-5", "claude-fable-5-1", "gpt-6.1-sol", "qwen3-235b-a22b", "deepseek-v4", "fable"):
            with self.subTest(name=name):
                self.assertTrue(eo.is_frontier_tier(name))


class TestStateFiles(EnvCase):
    def test_session_id_cannot_escape_temp_dir(self):
        path = eo._state_file({"session_id": "../../etc/x"})
        base = Path(tempfile.gettempdir()) / "mitosis-orchestrator"
        self.assertEqual(path.parent.parent, base)
        self.assertNotIn("..", path.parent.name.replace("_", ""))

    def test_save_leaves_no_temp_files(self):
        eo.save_state(self.payload, {"subagents": {"x": {"model": "flash"}}})
        state_dir = eo._state_file(self.payload).parent
        self.assertEqual([p.name for p in state_dir.iterdir() if p.suffix == ".tmp"], [])
        self.assertIn("x", eo.load_state(self.payload)["subagents"])


class TestJudgeTransport(EnvCase):
    def test_key_only_over_https_or_lan(self):
        cases = {
            "https://judge.example.com/v1": True,
            "http://192.168.1.5:8000/v1": True,
            "http://127.0.0.1:9/v1": True,
            "http://localhost:9/v1": True,
            "http://judge.example.com/v1": False,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(mj._key_may_travel(urllib.parse.urlparse(url)), expected)
        os.environ["MITOSIS_JUDGE_ALLOW_HTTP_KEY"] = "1"
        self.assertTrue(mj._key_may_travel(urllib.parse.urlparse("http://judge.example.com/v1")))

    def test_non_http_scheme_is_refused(self):
        os.environ["MITOSIS_JUDGE_URL"] = "file:///etc/passwd"
        self.assertIsNone(mj.judge_tier("Locate x", opener=lambda *a, **k: self.fail("must not open")))

    def test_redaction_covers_jwt_aws_and_url_credentials(self):
        text = mj.redact("t eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijk k AKIAABCDEFGHIJKLMNOP https://bob:hunter2@h/x")
        self.assertNotIn("eyJ", text)
        self.assertNotIn("AKIA", text)
        self.assertNotIn("hunter2", text)


class TestMcpServerRobustness(unittest.TestCase):
    def run_server(self, stdin_text):
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "mitosis-mcp-server.py")],
                              input=stdin_text, capture_output=True, text=True, timeout=30)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_parse_and_shape_errors_get_replies(self):
        replies = self.run_server('not json\n[1]\n{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32600)
        self.assertEqual(replies[2]["id"], 7)
        self.assertIn("tools", replies[2]["result"])

    def test_explicit_model_cannot_escape_pinning(self):
        seen = []

        def runner(prompt, model, workspace="inherit", cwd=None):
            seen.append(model)
            return 0, "STATUS: COMPLETED\nFILES_CHANGED: NONE\nVERIFICATION: ok\nNOTES: ok", ""

        res = mcp.execute_subagent_task({"prompt": "Locate parse_token", "model": "gpt-6-astra"}, runner=runner)
        self.assertEqual((res["model"], res["decision_source"]), (mcp.DEFAULT_CODEX_SCOUT_MODEL, "rule"))
        self.assertIn("ignored", res["reason"])
        res = mcp.execute_subagent_task({"prompt": "Edit parser.py to fix the crash", "model": mcp.DEFAULT_CODEX_SCOUT_MODEL},
                                        runner=runner)
        self.assertEqual((res["model"], res["type"]), (mcp.DEFAULT_CODEX_WORKER_MODEL, mcp.WORKER_TYPE))
        self.assertEqual(seen, [mcp.DEFAULT_CODEX_SCOUT_MODEL, mcp.DEFAULT_CODEX_WORKER_MODEL])

    def test_dash_prompt_stays_positional(self):
        cmd = mcp.build_codex_cmd("--help me", "gpt-5.6-terra")
        self.assertEqual(cmd[-2:], ["--", "--help me"])

    def test_handler_exception_becomes_internal_error(self):
        server = mcp.MitosisMCPServer()
        server.handle_request = lambda req: 1 / 0
        out = io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        try:
            sys.stdin, sys.stdout = io.StringIO('{"jsonrpc":"2.0","id":3,"method":"tools/call"}\n'), out
            server.run_stdio()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        reply = json.loads(out.getvalue())
        self.assertEqual((reply["id"], reply["error"]["code"]), (3, -32603))


class TestSetupCodexBackups(unittest.TestCase):
    def test_second_run_keeps_first_backup_and_agents_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text('model = "original"\n', encoding="utf-8")
            (home / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                setup.setup_codex(codex_home=home, repo_root=ROOT, interpreter="python3")
            self.assertEqual((home / "config.toml.bak").read_text(encoding="utf-8"), 'model = "original"\n')
            agent_backups = list(home.glob("AGENTS.md.bak*"))
            self.assertEqual(len(agent_backups), 1)
            self.assertEqual(agent_backups[0].read_text(encoding="utf-8"), "my own rules\n")
            with redirect_stdout(io.StringIO()):
                setup.setup_codex(codex_home=home, repo_root=ROOT, interpreter="python3")
            self.assertEqual((home / "config.toml.bak").read_text(encoding="utf-8"), 'model = "original"\n')
            self.assertEqual(len(list(home.glob("config.toml.bak-*"))), 1)
            self.assertEqual(len(list(home.glob("AGENTS.md.bak*"))), 1)  # identical AGENTS.md is not re-backed up
            self.assertEqual(list(home.glob("*.mitosis-tmp")), [])


if __name__ == "__main__":
    unittest.main()
