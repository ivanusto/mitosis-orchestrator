#!/usr/bin/env python3
"""mitosis-doctor tests: healthy fixtures, fault injections (each must report non-OK with the named check),
exit codes and the no-secrets guarantee. Fixtures use --home and copies of the plugin tree under a temp dir.
Run: python -m unittest tests.test_doctor -v"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "mitosis-doctor.py"
KEY = "mitosis-orchestrator@mitosis-orchestrator"
EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit", "subagent_stop", "stop")
MARKER = "ZZ-SECRET-MARKER-9137"
TREE_ITEMS = ("scripts", "hooks", "hooks.json", "plugin.json", "spec", "claude-agents", "codex-agents", "agents", ".claude-plugin", ".codex-plugin")

_spec = importlib.util.spec_from_file_location("mitosis_doctor_under_test", str(DOCTOR))
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def copy_tree(dest):
    """Copy the plugin tree, never .git, __pycache__ or the routing holdout file."""
    ignore = shutil.ignore_patterns(".git", "__pycache__", "routing_holdout_v9.jsonl")
    dest.mkdir(parents=True, exist_ok=True)
    for name in TREE_ITEMS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(str(src), str(dest / name), ignore=ignore)
        elif src.is_file():
            shutil.copy2(str(src), str(dest / name))
    return dest


def edit(path, old, new):
    text = Path(path).read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError("mutation target not found in %s: %r" % (path, old))
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")


def models():
    return doctor.spec_models(ROOT)


class Fixture:
    """A fake home with a healthy Claude, Codex and agy install plus fresh heartbeats."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.claude_tree = copy_tree(self.home / ".claude" / "plugins" / "cache" / "m" / "m" / self.version)
        self.codex_tree = copy_tree(self.home / ".codex" / "plugins" / "cache" / "mitosis-orchestrator" / "mitosis-orchestrator" / self.version)
        self.agy_tree = copy_tree(self.home / ".gemini" / "config" / "plugins" / "mitosis-orchestrator")
        self.bin = self.tmp / "bin"
        self.write_claude()
        self.write_codex()
        self.shims()
        for name in ("claude", "codex", "agy"):
            self.heartbeat(name)

    @property
    def hb_dir(self):
        base = self.home / "AppData" / "Local" if os.name == "nt" else self.home / ".cache"
        return base / "mitosis-orchestrator"

    def write_claude(self):
        d = self.home / ".claude"
        (d / "plugins").mkdir(parents=True, exist_ok=True)
        entry = {"scope": "user", "installPath": str(self.claude_tree), "version": self.version, "gitCommitSha": "abc123def456"}
        (d / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {KEY: [entry]}}), encoding="utf-8")
        (d / "settings.json").write_text(json.dumps({"enabledPlugins": {KEY: True}, "apiKeyHelper": MARKER}), encoding="utf-8")

    def write_codex(self, hooks=True, drop_event=None):
        d = self.home / ".codex"
        (d / "agents").mkdir(parents=True, exist_ok=True)
        m = models()
        lines = ["model = \"%s\"" % MARKER, "", "[features]"]
        if hooks:
            lines.append("hooks = true")
        lines += ["", "[hooks.state]"]
        for ev in EVENTS:
            if ev != drop_event:
                lines += ["", "[hooks.state.\"%s:hooks/codex-hooks.json:%s:0:0\"]" % (KEY, ev), "trusted_hash = \"sha256:%s\"" % MARKER]
        lines += ["", "[mcp_servers.x]", "env = { TOKEN = \"%s\" }" % MARKER, "", "[plugins.\"%s\"]" % KEY, "enabled = true", ""]
        (d / "config.toml").write_text("\n".join(lines), encoding="utf-8")
        for role, model in (("explorer", m["codex"]["scout"]["model"]), ("worker", m["codex"]["worker"]["model"]),
                            ("flash-lite-scout", m["codex"]["scout"]["model"]), ("flash-worker", m["codex"]["worker"]["model"])):
            (d / "agents" / (role + ".toml")).write_text("name = \"%s\"\nmodel = \"%s\"\n" % (role, model), encoding="utf-8")

    def shims(self):
        self.bin.mkdir(exist_ok=True)
        for name in ("claude", "codex", "agy"):
            if os.name == "nt":  # found through PATHEXT
                (self.bin / (name + ".cmd")).write_text("@echo %s 9.9.9\r\n" % name, encoding="ascii", newline="")
                continue
            path = self.bin / name
            path.write_text("#!/bin/sh\necho '%s 9.9.9'\n" % name, encoding="ascii")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def heartbeat(self, platform, **over):
        self.hb_dir.mkdir(parents=True, exist_ok=True)
        data = {"schema": 1, "plugin_version": self.version, "last_event": "pre-tool-use", "last_event_at": int(time.time()) - 30,
                "events": 5, "unhandled_spawn_tools": {}, "tool_failures": {}, "stdin_replacement_chars": 0,
                "pin": {"verified": 1, "mismatch": 0, "unknown": 0}, "report": {"ok": 0, "bad": 0}, "last_error": None}
        data.update(over)
        (self.hb_dir / ("heartbeat-%s.json" % platform)).write_text(json.dumps(data), encoding="utf-8")

    def run(self, *args, path=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if "MITOSIS" not in k}
        env["PATH"] = path if path is not None else os.pathsep.join([str(self.bin), os.environ.get("PATH", "")])
        env["DOCTOR_MARKER_ENV"] = MARKER
        env["XDG_CACHE_HOME"] = str(self.tmp / "other-cache")
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, str(DOCTOR), "--home", str(self.home)] + list(args),
                              capture_output=True, env=env, timeout=300)
        return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")

    def checks(self, *args, **kw):
        rc, out, _ = self.run("--json", *args, **kw)
        data = json.loads(out)
        self.last_rc = rc
        return {c["id"]: c for c in data["checks"]}


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(self._tmp.name)

    def status(self, checks, cid):
        self.assertIn(cid, checks, sorted(checks))
        return checks[cid]["status"]

    def assertFail(self, checks, cid, text=None):
        self.assertEqual(self.status(checks, cid), "FAIL", checks[cid])
        if text:
            self.assertIn(text, checks[cid]["message"])


class TestHealthy(DoctorCase):
    def test_claude_all_ok_exit_0(self):
        checks = self.fx.checks("--platform", "claude")
        bad = {k: v["status"] for k, v in checks.items() if v["status"] != "OK"}
        self.assertEqual(bad, {})
        self.assertEqual(self.fx.last_rc, 0)

    def test_codex_only_trust_hash_is_unknown(self):
        checks = self.fx.checks("--platform", "codex")
        self.assertEqual({k for k, v in checks.items() if v["status"] != "OK"}, {"codex.trust_hash"})
        self.assertEqual(checks["codex.trust_hash"]["status"], "UNKNOWN")
        self.assertIn("/hooks", checks["codex.trust_hash"]["message"])
        self.assertEqual(self.fx.last_rc, 1)

    def test_agy_only_pin_verification_is_unknown(self):
        checks = self.fx.checks("--platform", "agy")
        self.assertEqual({k for k, v in checks.items() if v["status"] != "OK"}, {"agy.pin_verify"})
        self.assertEqual(self.fx.last_rc, 1)

    def test_text_output_and_summary(self):
        rc, out, _ = self.fx.run("--platform", "claude")
        self.assertEqual(rc, 0)
        self.assertIn("claude.probe.scout", out)
        self.assertIn("exit code 0", out)

    def test_no_secret_in_text_or_json_output(self):
        for args in ((), ("--json",)):
            rc, out, err = self.fx.run(*args)
            self.assertNotIn(MARKER, out + err, args)
            self.assertNotIn("DOCTOR_MARKER_ENV", out + err)


class TestHeartbeat(DoctorCase):
    def test_missing_heartbeat_is_unknown_and_exit_1(self):
        (self.fx.hb_dir / "heartbeat-claude.json").unlink()
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(self.status(checks, "claude.heartbeat"), "UNKNOWN")
        self.assertIn("no hook has fired", checks["claude.heartbeat"]["message"])
        self.assertEqual(self.fx.last_rc, 1)

    def test_heartbeat_dir_env_is_honoured(self):
        other = Path(self._tmp.name) / "hb-elsewhere"
        other.mkdir()
        (other / "heartbeat-claude.json").write_text(json.dumps({"plugin_version": self.fx.version, "last_event_at": int(time.time())}), encoding="utf-8")
        checks = self.fx.checks("--platform", "claude", extra_env={"MITOSIS_HEARTBEAT_DIR": str(other)})
        self.assertEqual(self.status(checks, "claude.heartbeat.age"), "OK")

    def test_unhandled_spawn_tool_fails_naming_it(self):
        self.fx.heartbeat("codex", unhandled_spawn_tools={"xyzspawn_agent": 3})
        checks = self.fx.checks("--platform", "codex")
        self.assertFail(checks, "codex.heartbeat.unhandled", "xyzspawn_agent")
        self.assertEqual(self.fx.last_rc, 2)

    def test_warnings(self):
        self.fx.heartbeat("claude", last_error={"class": "KeyError", "event": "pre-tool-use", "at": int(time.time()) - 7200},
                          stdin_replacement_chars=4, pin={"verified": 0, "mismatch": 2, "unknown": 0}, plugin_version="0.0.1")
        checks = self.fx.checks("--platform", "claude")
        for cid in ("heartbeat.error", "heartbeat.stdin", "heartbeat.pin", "heartbeat.version"):
            self.assertEqual(self.status(checks, "claude." + cid), "WARN", cid)
        self.assertIn("KeyError", checks["claude.heartbeat.error"]["message"])
        self.assertIn("2h", checks["claude.heartbeat.error"]["message"])
        self.assertEqual(self.fx.last_rc, 1)

    def test_old_heartbeat_is_unknown(self):
        self.fx.heartbeat("claude", last_event_at=int(time.time()) - 15 * 86400)
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(self.status(checks, "claude.heartbeat.age"), "UNKNOWN")

    def test_corrupt_heartbeat_is_unknown(self):
        (self.fx.hb_dir / "heartbeat-claude.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.status(self.fx.checks("--platform", "claude"), "claude.heartbeat"), "UNKNOWN")


class TestStatic(DoctorCase):
    def test_platform_not_installed_is_skip(self):
        shutil.rmtree(str(self.fx.home / ".codex"))
        checks = self.fx.checks("--platform", "codex")
        self.assertEqual(self.status(checks, "codex.installed"), "SKIP")
        self.assertEqual(self.fx.last_rc, 0)

    def test_empty_home_is_all_skip_exit_0(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        proc = subprocess.run([sys.executable, str(DOCTOR), "--home", str(empty), "--json"], capture_output=True, timeout=60)
        data = json.loads(proc.stdout.decode())
        self.assertEqual({c["status"] for c in data["checks"]}, {"SKIP"})
        self.assertEqual(proc.returncode, 0)

    def test_claude_disabled_is_fail(self):
        (self.fx.home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {KEY: False}}), encoding="utf-8")
        self.assertFail(self.fx.checks("--platform", "claude"), "claude.enabled")
        self.assertEqual(self.fx.last_rc, 2)

    def platform_rows(self, checks, platform):
        return [c for c in checks.values() if c["platform"] == platform]

    def assertOnlySkip(self, checks, platform, text=None):
        rows = self.platform_rows(checks, platform)
        self.assertEqual([r["status"] for r in rows], ["SKIP"], rows)
        if text:
            self.assertIn(text, rows[0]["message"])

    def test_claude_without_entry_is_one_skip_row_even_with_cli(self):
        (self.fx.home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {}}), encoding="utf-8")
        checks = self.fx.checks("--platform", "claude")
        self.assertOnlySkip(checks, "claude", "installed_plugins.json")
        self.assertEqual(self.fx.last_rc, 0)

    def test_claude_missing_installed_plugins_file_is_skip(self):
        (self.fx.home / ".claude" / "plugins" / "installed_plugins.json").unlink()
        self.assertOnlySkip(self.fx.checks("--platform", "claude"), "claude")

    def test_claude_malformed_installed_plugins_is_unknown_not_skip(self):
        path = self.fx.home / ".claude" / "plugins" / "installed_plugins.json"
        for text in ("{not json", json.dumps(["x"])):
            path.write_text(text, encoding="utf-8")
            checks = self.fx.checks("--platform", "claude")
            self.assertEqual(self.status(checks, "claude.plugin"), "UNKNOWN", text)
            self.assertNotIn("SKIP", {c["status"] for c in checks.values()})
            self.assertEqual(self.fx.last_rc, 1)

    def test_claude_not_installed_with_root_probes_the_tree_only(self):
        (self.fx.home / ".claude" / "plugins" / "installed_plugins.json").unlink()
        checks = self.fx.checks("--platform", "claude", "--root", str(self.fx.claude_tree))
        self.assertEqual(self.status(checks, "claude.installed"), "SKIP")
        self.assertEqual(self.status(checks, "claude.probe.scout"), "OK")
        self.assertEqual({c["status"] for c in checks.values()}, {"SKIP", "OK"})
        self.assertFalse([k for k in checks if "heartbeat" in k or k.endswith(".cli")])

    def test_codex_without_entry_or_cache_is_one_skip_row(self):
        shutil.rmtree(str(self.fx.home / ".codex" / "plugins"))
        (self.fx.home / ".codex" / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        checks = self.fx.checks("--platform", "codex")
        self.assertOnlySkip(checks, "codex", "config.toml")
        self.assertEqual(self.fx.last_rc, 0)

    def test_codex_dir_without_config_or_cache_is_skip_and_root_still_probes(self):
        shutil.rmtree(str(self.fx.home / ".codex" / "plugins"))
        (self.fx.home / ".codex" / "config.toml").unlink()
        self.assertOnlySkip(self.fx.checks("--platform", "codex"), "codex")
        checks = self.fx.checks("--platform", "codex", "--root", str(self.fx.claude_tree))
        self.assertEqual(self.status(checks, "codex.installed"), "SKIP")
        self.assertEqual(self.status(checks, "codex.probe.opaque"), "OK")
        self.assertFalse([k for k in checks if "trust" in k or "hooks_feature" in k or "heartbeat" in k])

    def test_broken_codex_config_with_cache_is_never_skip(self):
        (self.fx.home / ".codex" / "config.toml").write_text("[features\nhooks = = true\n", encoding="utf-8")
        checks = self.fx.checks("--platform", "codex")
        self.assertNotIn("SKIP", {c["status"] for c in checks.values()})
        self.assertEqual(self.status(checks, "codex.hooks_feature"), "UNKNOWN")
        self.assertEqual(self.status(checks, "codex.trust.stop"), "UNKNOWN")
        self.assertEqual(self.fx.last_rc, 1)

    def test_broken_codex_config_without_cache_is_not_skip_either(self):
        shutil.rmtree(str(self.fx.home / ".codex" / "plugins"))
        (self.fx.home / ".codex" / "config.toml").write_text("[features\nhooks = = true\n", encoding="utf-8")
        checks = self.fx.checks("--platform", "codex")
        self.assertNotIn("SKIP", {c["status"] for c in checks.values()})
        self.assertNotEqual(self.fx.last_rc, 0)

    def test_codex_cache_without_config_is_fail_not_skip(self):
        (self.fx.home / ".codex" / "config.toml").unlink()
        checks = self.fx.checks("--platform", "codex")
        self.assertFail(checks, "codex.hooks_feature")
        self.assertEqual(self.fx.last_rc, 2)

    def test_agy_without_plugin_dir_is_one_skip_row_and_root_probes(self):
        shutil.rmtree(str(self.fx.agy_tree))
        self.assertOnlySkip(self.fx.checks("--platform", "agy"), "agy", "plugin directory")
        checks = self.fx.checks("--platform", "agy", "--root", str(self.fx.codex_tree))
        self.assertEqual(self.status(checks, "agy.installed"), "SKIP")
        self.assertEqual(self.status(checks, "agy.probe.flat"), "OK")
        self.assertFalse([k for k in checks if "heartbeat" in k or "pin_verify" in k])

    def test_claude_missing_hooks_file_is_fail(self):
        (self.fx.claude_tree / "hooks" / "hooks.json").unlink()
        checks = self.fx.checks("--platform", "claude")
        self.assertFail(checks, "claude.files", "hooks/hooks.json")
        self.assertEqual(self.status(checks, "claude.probe.scout"), "UNKNOWN")

    def test_stale_version_warns(self):
        p = self.fx.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["plugins"][KEY][0]["version"] = "0.0.1"
        p.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.status(self.fx.checks("--platform", "claude"), "claude.version"), "WARN")

    def test_missing_cli_warns(self):
        checks = self.fx.checks("--platform", "claude", path=str(self.fx.tmp / "nothing-here"))
        self.assertEqual(self.status(checks, "claude.cli"), "WARN")

    def test_codex_missing_trust_key_is_fail(self):
        self.fx.write_codex(drop_event="session_start")
        checks = self.fx.checks("--platform", "codex")
        self.assertFail(checks, "codex.trust.session_start", "session_start")
        self.assertEqual(self.status(checks, "codex.trust.stop"), "OK")
        self.assertEqual(self.fx.last_rc, 2)

    def test_codex_hooks_feature_missing_is_fail(self):
        self.fx.write_codex(hooks=False)
        self.assertFail(self.fx.checks("--platform", "codex"), "codex.hooks_feature", "silently")

    def test_codex_role_model_difference_warns_without_echo(self):
        (self.fx.home / ".codex" / "agents" / "explorer.toml").write_text('model = "%s"\n' % MARKER, encoding="utf-8")
        rc, out, _ = self.fx.run("--platform", "codex", "--json")
        data = {c["id"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(data["codex.role.explorer"]["status"], "WARN")
        self.assertNotIn(MARKER, out)

    def test_codex_missing_role_file(self):
        (self.fx.home / ".codex" / "agents" / "worker.toml").unlink()
        self.assertFail(self.fx.checks("--platform", "codex"), "codex.role.worker")

    def test_agy_missing_hooks_json_is_fail(self):
        (self.fx.agy_tree / "hooks.json").unlink()
        self.assertFail(self.fx.checks("--platform", "agy"), "agy.files")

    def test_live_flag_prints_cost_warning_first_and_skips_without_claude(self):
        rc, out, _ = self.fx.run("--live", "--platform", "codex")
        self.assertTrue(out.startswith("WARNING"), out[:80])
        self.assertIn("costs money", out)
        self.assertIn("claude.live", out)

    def test_fallback_toml_parser_agrees_with_tomllib(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("no tomllib")
        text = (self.fx.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(doctor.parse_toml_fallback(text)["hooks"]["state"].keys(), tomllib.loads(text)["hooks"]["state"].keys())
        self.assertIs(doctor.parse_toml_fallback(text)["features"]["hooks"], True)
        self.assertIs(doctor.parse_toml_fallback(text)["plugins"][KEY]["enabled"], True)


class TestFaultInjection(DoctorCase):
    def test_1_no_python_in_chain_fails_with_empty_stdout(self):
        empty = self.fx.tmp / "empty-bin"
        empty.mkdir()
        for platform in ("claude", "codex", "agy"):
            with self.subTest(platform=platform):
                checks = self.fx.checks("--platform", platform, path=str(empty))
                cid = {"claude": "claude.probe.scout", "codex": "codex.probe.opaque", "agy": "agy.probe.flat"}[platform]
                self.assertFail(checks, cid, "empty stdout")
                self.assertEqual(self.fx.last_rc, 2)

    def test_2_text_mode_stdin_fails_the_cp950_probe(self):
        edit(self.fx.claude_tree / "scripts" / "enforce-orchestrator.py",
             'buffer = getattr(sys.stdin, "buffer", None)', "buffer = None")
        checks = self.fx.checks("--platform", "claude")
        self.assertFail(checks, "claude.probe.scout.cp950")
        self.assertEqual(self.fx.last_rc, 2)

    def test_3_trust_key_and_feature_removal(self):
        self.fx.write_codex(drop_event="user_prompt_submit", hooks=False)
        checks = self.fx.checks("--platform", "codex")
        self.assertFail(checks, "codex.trust.user_prompt_submit")
        self.assertFail(checks, "codex.hooks_feature")

    def test_4_codex_handler_misses_collaboration_prefix(self):
        edit(self.fx.codex_tree / "scripts" / "mitosis_hook" / "platforms" / "codex.py",
             'r"^collaboration(?:__|[./:])?("', 'r"^nevermatchcollab("')
        checks = self.fx.checks("--platform", "codex")
        self.assertFail(checks, "codex.probe.opaque")
        self.assertEqual(self.fx.last_rc, 2)

    def test_5_agy_handler_reads_only_subagents_list(self):
        edit(self.fx.agy_tree / "scripts" / "mitosis_hook" / "platforms" / "agy.py",
             'flat = any(k in args for k in ("Prompt", "prompt", "TypeName", "typeName"))', "flat = False")
        checks = self.fx.checks("--platform", "agy")
        self.assertFail(checks, "agy.probe.flat", "flat")
        self.assertEqual(self.fx.last_rc, 2)

    def test_agy_empty_object_is_a_denial(self):
        verdict = doctor.validate_agy({}, models())
        self.assertEqual(verdict[0], "FAIL")
        self.assertIn("DENIAL", verdict[1])

    def test_matcher_that_skips_the_tool_fails(self):
        path = self.fx.claude_tree / "hooks" / "hooks.json"
        edit(path, "Agent|Task|Bash", "Task|Bash")
        self.assertFail(self.fx.checks("--platform", "claude"), "claude.probe.scout", "matcher")

    def test_probe_does_not_touch_installed_tree_or_heartbeat(self):
        before = sorted(str(p) for p in self.fx.home.rglob("*"))
        hb = (self.fx.hb_dir / "heartbeat-claude.json").read_text(encoding="utf-8")
        self.fx.checks()
        self.assertEqual(sorted(str(p) for p in self.fx.home.rglob("*")), before)
        self.assertEqual((self.fx.hb_dir / "heartbeat-claude.json").read_text(encoding="utf-8"), hb)


if __name__ == "__main__":
    unittest.main()
