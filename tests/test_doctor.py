#!/usr/bin/env python3
"""mitosis-doctor tests: healthy fixtures, fault injections (each must report non-OK with the named check),
exit codes and the no-secrets guarantee. Fixtures use --home and copies of the plugin tree under a temp dir.
Run: python -m unittest tests.test_doctor -v"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


LABELS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
          "PostToolUse": "post_tool_use", "SubagentStop": "subagent_stop", "Stop": "stop"}


def oracle_content_hash(root):
    """Independent re-statement of Codex's plugin bundle hash (recursive, written apart from the doctor)."""
    digest = hashlib.sha256(b"codex-plugin-bundle-v1")

    def visit(path, rel):
        name = rel.split(b"/")[-1] if rel else b""
        if rel:
            try:
                label = name.decode("utf-8")
            except UnicodeDecodeError:
                label = ""
            if label in ("__pycache__", ".venv") or label.endswith(".pyc") or label.endswith(".egg-info"):
                return
        digest.update(len(rel).to_bytes(8, "little") + rel)
        if os.path.islink(path):
            raise AssertionError("symlink in oracle tree")
        if os.path.isdir(path):
            digest.update(b"directory")
            for child in sorted(os.listdir(os.fsencode(path))):
                visit(os.path.join(os.fsencode(path), child), (rel + b"/" + child) if rel else child)
        else:
            data = Path(os.fsdecode(path)).read_bytes()
            digest.update(b"file" + len(data).to_bytes(8, "little") + data)

    visit(os.fsencode(str(root)), b"")
    return "sha256:" + digest.hexdigest()


def oracle_decl_hash(label, matcher, command):
    """Compact sorted JSON written by hand: keys async, command, timeout, type; event_name, hooks, matcher."""
    handler = '{"async":false,"command":%s,"timeout":600,"type":"command"}' % json.dumps(command, ensure_ascii=False)
    text = '{"event_name":"%s","hooks":[%s]%s}' % (
        label, handler, (',"matcher":%s' % json.dumps(matcher, ensure_ascii=False)) if matcher is not None else "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def oracle_baseline_name(config_file, plugin_id):
    h = hashlib.sha256()
    for part in (str(config_file).encode("utf-8"), plugin_id.encode("utf-8")):
        h.update(len(part).to_bytes(8, "little") + part)
    return h.hexdigest()


def tree_digest(root):
    """Names, kinds and contents of everything under root."""
    h = hashlib.sha256()
    for path in sorted(Path(root).rglob("*"), key=str):
        h.update(str(path).encode("utf-8", "surrogateescape"))
        if path.is_symlink():
            h.update(b"L" + os.readlink(str(path)).encode("utf-8", "surrogateescape"))
        elif path.is_file():
            h.update(b"F" + path.read_bytes())
        else:
            h.update(b"D")
    return h.hexdigest()


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

    def hook_declarations(self):
        """[(event label, matcher or None, command)] straight from the cached manifest, via the oracle."""
        data = json.loads((self.codex_tree / "hooks" / "codex-hooks.json").read_text(encoding="utf-8"))
        return [(LABELS[ev], g.get("matcher") if LABELS[ev] in ("pre_tool_use", "post_tool_use", "session_start", "subagent_stop") else None, g["hooks"][0]["command"])
                for ev, groups in data["hooks"].items() for g in groups]

    def write_codex(self, hooks=True, drop_event=None, content=True, extra_state=None):
        d = self.home / ".codex"
        (d / "agents").mkdir(parents=True, exist_ok=True)
        m = models()
        decls = {label: oracle_decl_hash(label, matcher, command) for label, matcher, command in self.hook_declarations()}
        bundle = oracle_content_hash(self.codex_tree)
        lines = ["model = \"%s\"" % MARKER, "", "[features]"]
        if hooks:
            lines.append("hooks = true")
        lines += ["", "[hooks.state]"]
        for ev in EVENTS:
            if ev != drop_event:
                lines += ["", "[hooks.state.\"%s:hooks/codex-hooks.json:%s:0:0\"]" % (KEY, ev), "trusted_hash = \"%s\"" % decls[ev]]
                if content:
                    lines.append("trusted_content_hash = \"%s\"" % bundle)
                lines += (extra_state or {}).get(ev, [])
        lines += ["", "[mcp_servers.x]", "env = { TOKEN = \"%s\" }" % MARKER, "", "[plugins.\"%s\"]" % KEY, "enabled = true", ""]
        (d / "config.toml").write_text("\n".join(lines), encoding="utf-8")
        for role, model in (("explorer", m["codex"]["scout"]["model"]), ("worker", m["codex"]["worker"]["model"]),
                            ("flash-lite-scout", m["codex"]["scout"]["model"]), ("flash-worker", m["codex"]["worker"]["model"])):
            (d / "agents" / (role + ".toml")).write_text("name = \"%s\"\nmodel = \"%s\"\n" % (role, model), encoding="utf-8")

    def codex_version(self, version):
        """Make the `codex` shim report this version (the other shims stay at 9.9.9)."""
        if os.name == "nt":
            return
        path = self.bin / "codex"
        path.write_text("#!/bin/sh\necho 'codex-cli %s'\n" % version, encoding="ascii")

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

    def test_codex_healthy_trust_rows_are_ok(self):
        checks = self.fx.checks("--platform", "codex")
        self.assertNotIn("codex.trust_hash", checks)
        for event in EVENTS:
            self.assertEqual(checks["codex.trust." + event]["status"], "OK", checks["codex.trust." + event])
        self.assertEqual(checks["codex.trust.bundle"]["status"], "OK")
        self.assertEqual({k for k, v in checks.items() if v["status"] != "OK"}, set())
        self.assertEqual(self.fx.last_rc, 0)

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

    def test_report_rejection_row_is_informational_and_names_classes_only(self):
        self.fx.heartbeat("claude", report_bad_scout={"missing_status": 2, "not an id!": 5, "bad_value": "x"},
                          report_bad_worker={"no_log_uri": 1}, free_text={"secret": MARKER})
        rc, out, _ = self.fx.run("--platform", "claude")
        checks = self.fx.checks("--platform", "claude")
        row = checks["claude.heartbeat.report"]
        self.assertEqual(row["status"], "OK")
        self.assertIn("scout: missing_status=2", row["message"])
        self.assertIn("worker: no_log_uri=1", row["message"])
        self.assertNotIn("not an id", row["message"])
        self.assertNotIn("bad_value", row["message"])
        self.assertNotIn(MARKER, out + row["message"])
        self.assertEqual(self.fx.last_rc, 0, "a rejected report alone must not raise the exit code")

    def test_report_rejection_row_without_counts(self):
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(checks["claude.heartbeat.report"]["status"], "OK")
        self.assertIn("no rejected", checks["claude.heartbeat.report"]["message"])

    def test_report_rejection_row_ignores_junk_shapes(self):
        self.fx.heartbeat("claude", report_bad_scout=["x"], report_bad_worker={"a": -3, "b": True})
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(checks["claude.heartbeat.report"]["status"], "OK")
        self.assertIn("no rejected", checks["claude.heartbeat.report"]["message"])

    def test_builtin_row_is_informational_and_allowlisted(self):
        self.fx.heartbeat("claude", builtin_stops={"Explore": 4, "general-purpose": 1, "evil name": 9, "Plan": 0},
                          free_text={"secret": MARKER})
        rc, out, _ = self.fx.run("--platform", "claude")
        checks = self.fx.checks("--platform", "claude")
        row = checks["claude.heartbeat.builtin"]
        self.assertEqual(row["status"], "OK")
        self.assertIn("Explore=4", row["message"])
        self.assertIn("general-purpose=1", row["message"])
        self.assertNotIn("evil", row["message"])
        self.assertNotIn("Plan", row["message"])
        self.assertIn("CLAUDE_MITOSIS_PIN_BUILTINS", row["message"])
        self.assertNotIn(MARKER, out)
        self.assertEqual(self.fx.last_rc, 0)

    def test_builtin_row_without_counts_or_with_junk(self):
        for value in (None, ["x"], {"Explore": True, "Plan": -1}):
            extra = {} if value is None else {"builtin_stops": value}
            self.fx.heartbeat("claude", **extra)
            row = self.fx.checks("--platform", "claude")["claude.heartbeat.builtin"]
            self.assertEqual(row["status"], "OK")
            self.assertIn("no built-in agent stop recorded", row["message"])

    def test_old_heartbeat_is_unknown(self):
        self.fx.heartbeat("claude", last_event_at=int(time.time()) - 15 * 86400)
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(self.status(checks, "claude.heartbeat.age"), "UNKNOWN")

    def test_corrupt_heartbeat_is_unknown(self):
        (self.fx.hb_dir / "heartbeat-claude.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.status(self.fx.checks("--platform", "claude"), "claude.heartbeat"), "UNKNOWN")


class TestCodexScoutShellRules(DoctorCase):
    @unittest.skipIf(os.name == "nt", "PATH shims are POSIX here")
    def test_row_says_posix_without_pwsh_and_strict_with_it(self):
        only_bin = str(self.fx.bin)
        checks = self.fx.checks("--platform", "codex", path=only_bin)
        row = checks["codex.scout_shell_rules"]
        if os.path.exists("/usr/local/bin/pwsh"):
            self.skipTest("a real /usr/local/bin/pwsh exists on this host")
        self.assertEqual(row["status"], "OK")
        self.assertIn("posix", row["message"])
        shim = self.fx.bin / "pwsh"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        checks = self.fx.checks("--platform", "codex", path=only_bin)
        self.assertIn("strict", checks["codex.scout_shell_rules"]["message"])
        self.assertEqual(checks["codex.scout_shell_rules"]["status"], "OK")

    def test_claude_has_no_such_row(self):
        self.assertNotIn("claude.scout_shell_rules", self.fx.checks("--platform", "claude"))


class TestCliVerified(DoctorCase):
    """The <platform>.cli_verified info row: always OK, never changes the exit code."""

    ENTRY = {"verified": "0.166.0", "verified_on": "2026-11-04", "method": "m"}

    def msg(self, line):
        return doctor.cli_verified_message("codex", line, self.ENTRY)

    def test_newer_cli_points_at_the_fixtures_and_stays_info(self):
        text = doctor.cli_verified_message("agy", "9.9.9", {"verified": "1.5.0", "verified_on": "2026-11-04", "fixtures": "1.5.0"})
        self.assertTrue(text.startswith("info:"))
        self.assertIn("fixtures for 1.5.0", text)
        self.assertIn("payload-shape.py", text)
        self.assertNotIn("fixtures", doctor.cli_verified_message("agy", "9.9.9", {"verified": "1.5.0", "verified_on": "2026-11-04"}))
        self.assertNotIn("fixtures", doctor.cli_verified_message("agy", "1.5.0", {"verified": "1.5.0", "verified_on": "2026-11-04", "fixtures": "1.5.0"}))

    def test_older_equal_newer_unreadable_prerelease(self):
        self.assertIn("not newer than the version verified on 0.166.0 (2026-11-04)", self.msg("codex-cli 0.165.9"))
        self.assertIn("not newer than the version verified", self.msg("codex-cli 0.166.0"))
        self.assertIn("not newer", self.msg("0.99.0"))  # numeric, not textual: 0.99 < 0.166
        for line in ("codex-cli 0.167.0", "codex-cli 0.167.0-alpha.1", "1.0.0", "0.166.0.1"):
            text = self.msg(line)
            self.assertIn("is newer than the last version verified (0.166.0 on 2026-11-04)", text, line)
        self.assertIn("not newer", self.msg("codex-cli 0.166.0-alpha.1"))
        for line in (None, "", "codex-cli unknown"):
            self.assertIn("could not be read", self.msg(line))
        for text in (self.msg("0.1.0"), self.msg("9.9.9"), self.msg(None)):
            self.assertTrue(text.startswith("info:"), text)

    def test_missing_or_bad_entry(self):
        for entry in (None, {}, {"verified": 5}, {"verified": "abc"}):
            self.assertIn("no verified CLI version is recorded", doctor.cli_verified_message("agy", "1.4.6", entry))

    @unittest.skipIf(os.name == "nt", "PATH shims are POSIX here")
    def test_rows_per_platform_never_change_the_exit_code(self):
        compat = doctor.compat_table(ROOT)
        self.assertEqual(set(compat), {"claude", "codex", "agy"})
        for platform in ("claude", "codex", "agy"):
            verified = compat[platform]["verified"]
            parts = [int(x) for x in verified.split(".")]
            up = ".".join(str(x) for x in parts[:-1] + [parts[-1] + 1])
            down = ".".join(str(x) for x in parts[:-1] + [max(parts[-1] - 1, 0)])
            cases = ((down, "not newer"), (verified, "not newer"), (up, "is newer"), (up + "-alpha.1", "is newer"))
            base = {}
            for version, expect in cases:
                shim = self.fx.bin / platform
                shim.write_text("#!/bin/sh\necho '%s %s'\n" % (platform, version), encoding="ascii")
                checks = self.fx.checks("--platform", platform)
                row = checks[platform + ".cli_verified"]
                self.assertEqual(row["status"], "OK", row)
                self.assertTrue(row["message"].startswith("info:"), row)
                self.assertIn(expect, row["message"], (platform, version))
                base[version] = self.fx.last_rc
            self.assertEqual(len(set(base.values())), 1, base)
            shim.write_text("#!/bin/sh\necho 'no digits here'\n", encoding="ascii")
            checks = self.fx.checks("--platform", platform)
            self.assertEqual(checks[platform + ".cli_verified"]["status"], "OK")
            self.assertIn("could not be read", checks[platform + ".cli_verified"]["message"])
            self.assertEqual(self.fx.last_rc, base[verified])

    def test_exit_codes_with_the_9_9_9_shims(self):
        for platform, rc in (("claude", 0), ("codex", 0), ("agy", 1)):
            checks = self.fx.checks("--platform", platform)
            self.assertEqual(checks[platform + ".cli_verified"]["status"], "OK")
            self.assertIn("is newer", checks[platform + ".cli_verified"]["message"])
            self.assertEqual(self.fx.last_rc, rc, platform)

    def test_old_generated_module_without_compat_does_not_fail_doctor(self):
        gen = self.fx.agy_tree / "scripts" / "mitosis_generated.py"
        text = gen.read_text(encoding="utf-8")
        gen.write_text(text[:text.index("COMPAT = {")], encoding="utf-8")
        self.assertEqual(doctor.compat_table(self.fx.agy_tree).get("agy", {}).get("verified"),
                         doctor.compat_table(ROOT)["agy"]["verified"])  # falls back to this checkout
        gen.write_text("raise RuntimeError('broken')\n", encoding="utf-8")
        checks = self.fx.checks("--platform", "agy", "--root", str(self.fx.agy_tree))
        self.assertEqual(checks["agy.cli_verified"]["status"], "OK")

    def test_editing_compat_without_regenerating_is_caught_by_check(self):
        tree = Path(self._tmp.name) / "synctree"
        for name in ("scripts", "spec", "rules", "skills", "claude-agents", "codex-agents", "codex-config", "agents", ".agents", "README.md", "README.zh-TW.md"):
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(str(src), str(tree / name), ignore=shutil.ignore_patterns("__pycache__"))
            elif src.is_file():
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(tree / name))
        cmd = [sys.executable, str(tree / "scripts" / "sync-generated.py"), "--check"]
        self.assertEqual(subprocess.run(cmd, capture_output=True, text=True).returncode, 0)
        current = re.search(r'verified = "[^"]+"', (tree / "spec" / "compat.toml").read_text(encoding="utf-8")).group(0)
        edit(tree / "spec" / "compat.toml", current, 'verified = "9.9.999"')
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("scripts/mitosis_generated.py", proc.stdout)
        self.assertIn("README.md", proc.stdout)


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
        (self.fx.claude_tree / "hooks" / "claude-hooks.json").unlink()
        checks = self.fx.checks("--platform", "claude")
        self.assertFail(checks, "claude.files", "hooks/claude-hooks.json")
        self.assertEqual(self.status(checks, "claude.probe.scout"), "UNKNOWN")

    def test_claude_old_install_without_manifest_hooks_falls_back(self):
        tree = self.fx.claude_tree
        (tree / "hooks" / "claude-hooks.json").rename(tree / "hooks" / "hooks.json")
        manifest = tree / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.pop("hooks", None)
        manifest.write_text(json.dumps(data), encoding="utf-8")
        checks = self.fx.checks("--platform", "claude")
        self.assertEqual(self.status(checks, "claude.files"), "OK")
        self.assertEqual(self.status(checks, "claude.probe.scout"), "OK")

    def test_claude_manifest_hooks_path_too_long_for_the_os_falls_back(self):
        tree = self.fx.claude_tree
        (tree / "hooks" / "claude-hooks.json").rename(tree / "hooks" / "hooks.json")
        manifest = tree / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["hooks"] = "x" * 5000
        manifest.write_text(json.dumps(data), encoding="utf-8")
        rc, out, err = self.fx.run("--json", "--platform", "claude")
        self.assertNotIn("Traceback", err)
        checks = {c["id"]: c for c in json.loads(out)["checks"]}
        self.assertIn("claude.files", checks)  # Linux falls back and says OK; Windows may just call the path missing

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

    def test_codex_role_difference_names_the_fix(self):
        (self.fx.home / ".codex" / "agents" / "worker.toml").write_text('model = "older-model"\n', encoding="utf-8")
        row = self.fx.checks("--platform", "codex")["codex.role.worker"]
        self.assertEqual(row["status"], "WARN")
        self.assertIn("python3 scripts/setup-codex.py", row["message"])

    def _config_model(self, model):
        cfg = self.fx.home / ".codex" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace('model = "%s"' % MARKER, 'model = "%s"' % model, 1), encoding="utf-8")

    def test_codex_worker_same_as_primary_is_info_ok(self):
        self._config_model(models()["codex"]["worker"]["model"])
        checks = self.fx.checks("--platform", "codex", extra_env={"CODEX_HOME": str(self.fx.home / ".codex")})
        row = checks["codex.worker_same_as_primary"]
        self.assertEqual(row["status"], "OK")
        self.assertIn("only by reasoning effort", row["message"])
        self.assertNotRegex(row["message"], r"(?i)sav|cheap|faster|better")
        self.assertEqual(self.fx.last_rc, 0)  # an info row never changes the exit code

    def test_codex_worker_differs_from_primary_has_no_such_row(self):
        self.assertNotIn("codex.worker_same_as_primary", self.fx.checks("--platform", "codex"))

    def _models_cache(self, slugs):
        (self.fx.home / ".codex" / "models_cache.json").write_text(
            json.dumps({"fetched_at": "x", "models": [{"slug": s} for s in slugs]}), encoding="utf-8")

    def test_codex_pinned_models_listed_is_ok(self):
        m = models()["codex"]
        self._models_cache([m["scout"]["model"], m["worker"]["model"], "other"])
        self.assertEqual(self.fx.checks("--platform", "codex")["codex.pinned_models"]["status"], "OK")

    def test_codex_pinned_model_absent_warns_with_rollback(self):
        m = models()["codex"]
        self._models_cache([m["scout"]["model"]])
        checks = self.fx.checks("--platform", "codex")
        row = checks["codex.pinned_models"]
        self.assertEqual(row["status"], "WARN")
        self.assertIn(m["worker"]["model"], row["message"])
        self.assertIn("CODEX_MITOSIS_WORKER_MODEL=gpt-5.6-terra", row["message"])
        self.assertEqual(self.fx.last_rc, 1)  # a WARN, like every other warning
        (self.fx.home / ".codex" / "models_cache.json").unlink()
        self.fx.checks("--platform", "codex")
        self.assertEqual(self.fx.last_rc, 0)

    def test_codex_models_cache_missing_or_unreadable_adds_no_row(self):
        self.assertNotIn("codex.pinned_models", self.fx.checks("--platform", "codex"))
        (self.fx.home / ".codex" / "models_cache.json").write_text("{not json", encoding="utf-8")
        self.assertNotIn("codex.pinned_models", self.fx.checks("--platform", "codex"))
        (self.fx.home / ".codex" / "models_cache.json").write_text('{"models": "x"}', encoding="utf-8")
        self.assertNotIn("codex.pinned_models", self.fx.checks("--platform", "codex"))

    def _cache_text(self, text):
        (self.fx.home / ".codex" / "models_cache.json").write_text(text, encoding="utf-8")

    def test_codex_models_cache_with_unusable_slugs_adds_no_row_and_no_traceback(self):
        for text in ('{"models":[{"slug":["x"]}]}', '{"models":[]}', '{"models":[1,"a",null]}', '{"models":[{"slug":5},{"slug":""}]}'):
            self._cache_text(text)
            rc, out, err = self.fx.run("--json", "--platform", "codex")
            self.assertNotIn("Traceback", err, text)
            checks = {c["id"]: c for c in json.loads(out)["checks"]}
            self.assertNotIn("codex.pinned_models", checks, text)
            self.assertEqual(rc, 0, text)

    def test_codex_models_cache_unusable_slug_next_to_a_real_list_still_warns(self):
        m = models()["codex"]
        self._cache_text(json.dumps({"models": [{"slug": ["x"]}, {"slug": m["scout"]["model"]}]}))
        row = self.fx.checks("--platform", "codex")["codex.pinned_models"]
        self.assertEqual(row["status"], "WARN")
        self.assertIn(m["worker"]["model"], row["message"])

    def test_codex_nothing_pinned_adds_no_pinned_models_row(self):
        self._models_cache(["other"])
        (self.fx.codex_tree / "spec" / "models.toml").write_text("codex = 5\n", encoding="utf-8")
        self.assertNotIn("codex.pinned_models", self.fx.checks("--platform", "codex"))

    def test_pin_row_lists_only_what_it_checked(self):
        class Rows:
            def __init__(self):
                self.rows = []

            def add(self, *row):
                self.rows.append(row)

        rows = Rows()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models_cache.json").write_text(json.dumps({"models": [{"slug": "w1"}]}), encoding="utf-8")
            doctor.check_codex_pinned_models(rows, "codex", {}, Path(tmp), {"codex": {"worker": {"model": "w1"}}})
            self.assertEqual([(r[1], r[2]) for r in rows.rows], [("pinned_models", "OK")])
            self.assertIn("w1", rows.rows[0][3])
            self.assertNotIn("scout", rows.rows[0][3])
            rows.rows.clear()
            doctor.check_codex_pinned_models(rows, "codex", {}, Path(tmp), None)
            doctor.check_codex_pinned_models(rows, "codex", {}, Path(tmp), {"codex": 5})
            self.assertEqual(rows.rows, [])

    def test_pin_mismatch_text_covers_inherited_children_too(self):
        self.fx.heartbeat("codex", pin={"verified": 0, "mismatch": 2, "unknown": 0})
        row = self.fx.checks("--platform", "codex")["codex.heartbeat.pin"]
        self.assertEqual(row["status"], "WARN")
        self.assertIn("inherited", row["message"])
        self.assertNotIn("ran on another model", row["message"])

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
        path = self.fx.claude_tree / "hooks" / "claude-hooks.json"
        edit(path, "Agent|Task|Bash", "Task|Bash")
        self.assertFail(self.fx.checks("--platform", "claude"), "claude.probe.scout", "matcher")

    def test_probe_does_not_touch_installed_tree_or_heartbeat(self):
        before = sorted(str(p) for p in self.fx.home.rglob("*"))
        hb = (self.fx.hb_dir / "heartbeat-claude.json").read_text(encoding="utf-8")
        self.fx.checks()
        self.assertEqual(sorted(str(p) for p in self.fx.home.rglob("*")), before)
        self.assertEqual((self.fx.hb_dir / "heartbeat-claude.json").read_text(encoding="utf-8"), hb)


# Doctor never treats a Codex hash method as verified on Windows (codex_trust_verified), so a mismatch there is UNKNOWN.
MISMATCH = "UNKNOWN" if os.name == "nt" else "FAIL"


class TestCodexTrust(DoctorCase):
    def assertMismatch(self, checks, cid, text=None):
        self.assertEqual(self.status(checks, cid), MISMATCH, checks[cid])
        if text and MISMATCH == "FAIL":
            self.assertIn(text, checks[cid]["message"])

    def test_trust_verified_is_false_on_windows_and_follows_the_table_elsewhere(self):
        line = "codex-cli 0.166.0"
        with mock.patch.object(doctor.os, "name", "nt"):
            self.assertFalse(doctor.codex_trust_verified(line))
        with mock.patch.object(doctor.os, "name", "posix"):
            self.assertTrue(doctor.codex_trust_verified(line))
            self.assertFalse(doctor.codex_trust_verified("codex-cli 9.9.9"))
            self.assertFalse(doctor.codex_trust_verified(None))

    def setUp(self):
        super().setUp()
        self.fx.codex_version("0.166.0")

    def trust(self, **kw):
        return self.fx.checks("--platform", "codex", **kw)

    def assertOnlyTrustNotOk(self, checks, expected):
        bad = {k for k, v in checks.items() if k.startswith("codex.trust") and v["status"] != "OK"}
        self.assertEqual(bad, expected)

    # ---- golden vectors, computed apart from the doctor code
    def test_golden_vectors(self):
        root = Path(self._tmp.name) / "golden"
        (root / "sub").mkdir(parents=True)
        (root / "empty").mkdir()
        (root / "__pycache__").mkdir()
        (root / "a.txt").write_bytes(b"hello")
        (root / "sub" / "b.bin").write_bytes(b"\x00\x01")
        (root / "__pycache__" / "x.pyc").write_bytes(b"junk")
        self.assertEqual(oracle_content_hash(root), doctor.codex_bundle_hash(root))
        self.assertEqual(doctor.codex_bundle_hash(root), "sha256:8d6bacc7ca877b10495e2297cb644731332fec3f6a5903759dbca70b637afd67")
        handler = {"type": "command", "command": "echo ${PLUGIN_ROOT} \u00e9"}
        self.assertEqual(doctor.codex_declaration_hash("pre_tool_use", "Bash|Edit", handler),
                         oracle_decl_hash("pre_tool_use", "Bash|Edit", "echo ${PLUGIN_ROOT} \u00e9"))
        self.assertEqual(doctor.codex_declaration_hash("pre_tool_use", "Bash|Edit", handler), "sha256:8b58014e94c8e43f6f2bdb6026a5e771fb74dcda9bce53e8f877d08c48539ed5")
        self.assertEqual(doctor.codex_declaration_hash("stop", "ignored", handler), oracle_decl_hash("stop", None, "echo ${PLUGIN_ROOT} \u00e9"))

    def test_real_manifest_hashes_match_the_oracle(self):
        for label, matcher, command in self.fx.hook_declarations():
            data = {"type": "command", "command": command}
            self.assertEqual(doctor.codex_declaration_hash(label, matcher, data), oracle_decl_hash(label, matcher, command))

    def test_healthy_at_verified_version(self):
        checks = self.trust()
        self.assertOnlyTrustNotOk(checks, set())
        self.assertEqual(self.fx.last_rc, 0)

    # ---- bundle changes
    def test_one_byte_change_fails_bundle_only(self):
        target = self.fx.codex_tree / "scripts" / "enforce-orchestrator.py"
        target.write_bytes(target.read_bytes() + b" ")
        checks = self.trust()
        self.assertMismatch(checks, "codex.trust.bundle", "Completed")
        self.assertOnlyTrustNotOk(checks, {"codex.trust.bundle"})
        self.assertEqual(self.fx.last_rc, 1 if os.name == "nt" else 2)

    def test_new_file_fails(self):
        (self.fx.codex_tree / "extra.txt").write_text("x", encoding="utf-8")
        self.assertMismatch(self.trust(), "codex.trust.bundle")

    def test_new_empty_directory_fails(self):
        (self.fx.codex_tree / "emptydir").mkdir()
        self.assertMismatch(self.trust(), "codex.trust.bundle")

    def test_pyc_pycache_venv_egg_info_changes_stay_ok(self):
        t = self.fx.codex_tree
        (t / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (t / "scripts" / "__pycache__" / "a.cpython-38.pyc").write_bytes(b"1")
        (t / "scripts" / "loose.pyc").write_bytes(b"2")
        (t / ".venv").mkdir()
        (t / ".venv" / "bin").write_bytes(b"3")
        (t / "pkg.egg-info").mkdir()
        (t / "pkg.egg-info" / "PKG-INFO").write_bytes(b"4")
        checks = self.trust()
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "OK")

    def test_symlink_in_bundle(self):
        if os.name == "nt":
            self.skipTest("symlinks need privileges on Windows")
        os.symlink("scripts", str(self.fx.codex_tree / "link"))
        self.assertFail(self.trust(), "codex.trust.bundle", "symlink")
        self.fx.codex_version("9.9.9")
        self.assertEqual(self.status(self.trust(), "codex.trust.bundle"), "UNKNOWN")

    # ---- legacy baseline
    def baseline_file(self):
        cfg = self.fx.home / ".codex" / "config.toml"
        return cfg.parent / ".hook-trust" / oracle_baseline_name(cfg, KEY)

    def test_baseline_missing_warns_and_is_not_created(self):
        self.fx.write_codex(content=False)
        checks = self.trust()
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "UNKNOWN" if os.name == "nt" else "WARN")
        if os.name != "nt":
            self.assertIn("unreviewed", checks["codex.trust.bundle"]["message"])
        self.assertFalse((self.fx.home / ".codex" / ".hook-trust").exists())
        self.fx.codex_version("9.9.9")
        self.assertEqual(self.status(self.trust(), "codex.trust.bundle"), "UNKNOWN")
        self.assertFalse((self.fx.home / ".codex" / ".hook-trust").exists())

    def test_baseline_match_is_ok_and_differ_fails(self):
        self.fx.write_codex(content=False)
        bf = self.baseline_file()
        bf.parent.mkdir(parents=True)
        bf.write_text(oracle_content_hash(self.fx.codex_tree), encoding="utf-8")
        checks = self.trust()
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "OK")
        self.assertIn("legacy", checks["codex.trust.bundle"]["message"])
        bf.write_text("sha256:" + "0" * 64, encoding="utf-8")
        self.assertMismatch(self.trust(), "codex.trust.bundle")
        self.fx.codex_version("9.9.9")
        self.assertEqual(self.status(self.trust(), "codex.trust.bundle"), "UNKNOWN")

    def test_mixed_stored_and_absent_worst_wins(self):
        self.fx.write_codex(content=True)
        cfg = self.fx.home / ".codex" / "config.toml"
        text = cfg.read_text(encoding="utf-8")
        lines = text.splitlines()
        # drop the content approval of the last event only: it then relies on the (missing) baseline
        idx = max(i for i, ln in enumerate(lines) if ln.startswith("trusted_content_hash"))
        del lines[idx]
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.status(self.trust(), "codex.trust.bundle"), "UNKNOWN" if os.name == "nt" else "WARN")
        target = self.fx.codex_tree / "scripts" / "enforce-orchestrator.py"
        target.write_bytes(target.read_bytes() + b" ")
        self.assertMismatch(self.trust(), "codex.trust.bundle")

    # ---- declarations
    def test_matcher_edit_fails_only_pre_tool_use(self):
        edit(self.fx.codex_tree / "hooks" / "codex-hooks.json", "|Edit|Write|", "|Edit|")
        checks = self.trust()
        self.assertFail(checks, "codex.trust.pre_tool_use", "Completed")
        for event in EVENTS:
            if event != "pre_tool_use":
                self.assertEqual(self.status(checks, "codex.trust." + event), "OK")

    def test_enabled_false_fails(self):
        self.fx.write_codex(extra_state={"stop": ["enabled = false"]})
        checks = self.trust()
        self.assertFail(checks, "codex.trust.stop", "enabled = false")
        self.assertEqual(self.status(checks, "codex.trust.pre_tool_use"), "OK")

    def test_missing_entry_fails_both_ways(self):
        self.fx.write_codex(drop_event="stop")
        self.assertFail(self.trust(), "codex.trust.stop", "accept the hook review")
        self.fx.codex_version("9.9.9")
        self.assertFail(self.trust(), "codex.trust.stop")

    def test_verified_versus_unverified_shim(self):
        for event_file in ("codex-hooks.json",):
            path = self.fx.codex_tree / "hooks" / event_file
            path.write_text(path.read_text(encoding="utf-8").replace("|| exit 0", "|| exit 0 "), encoding="utf-8")
        checks = self.trust()
        for event in EVENTS:
            self.assertMismatch(checks, "codex.trust." + event)
        self.assertMismatch(checks, "codex.trust.bundle")
        self.fx.codex_version("9.9.9")
        checks = self.trust()
        for event in EVENTS:
            self.assertEqual(self.status(checks, "codex.trust." + event), "UNKNOWN")
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "UNKNOWN")
        self.assertEqual(self.fx.last_rc, 1)

    def test_unverified_single_mismatch_with_other_matches_is_fail(self):
        edit(self.fx.codex_tree / "hooks" / "codex-hooks.json", "|Edit|Write|", "|Edit|")
        self.fx.codex_version("9.9.9")
        checks = self.trust()
        self.assertFail(checks, "codex.trust.pre_tool_use")
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "UNKNOWN")

    def test_stale_hint_when_hook_ran_after_newest_file(self):
        edit(self.fx.codex_tree / "hooks" / "codex-hooks.json", "|Edit|Write|", "|Edit|")
        old = time.time() - 5000
        for path in [self.fx.codex_tree] + list(self.fx.codex_tree.rglob("*")):
            os.utime(str(path), (old, old))
        self.fx.codex_version("9.9.9")
        checks = self.trust()
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "UNKNOWN")
        self.assertIn("probably stale", checks["codex.trust.bundle"]["message"])
        newer = time.time()
        os.utime(str(self.fx.codex_tree / "plugin.json"), (newer, newer))
        self.assertNotIn("probably stale", self.trust()["codex.trust.bundle"]["message"])

    def test_no_codex_cli_mismatch_is_unknown(self):
        target = self.fx.codex_tree / "scripts" / "enforce-orchestrator.py"
        target.write_bytes(target.read_bytes() + b" ")
        checks = self.trust(path=str(self.fx.tmp / "nothing-here"))
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "UNKNOWN")
        self.assertEqual(self.status(checks, "codex.cli"), "WARN")

    def test_root_flag_does_not_change_what_is_hashed(self):
        other = copy_tree(Path(self._tmp.name) / "other-root")
        (other / "stray.txt").write_text("x", encoding="utf-8")
        checks = self.fx.checks("--platform", "codex", "--root", str(other))
        self.assertEqual(self.status(checks, "codex.trust.bundle"), "OK")

    def test_codex_home_env_is_honoured_without_home_flag(self):
        env = {k: v for k, v in os.environ.items() if "MITOSIS" not in k}
        env["PATH"] = str(self.fx.bin) + os.pathsep + env.get("PATH", "")
        env["CODEX_HOME"] = str(self.fx.home / ".codex")
        env["HOME"] = str(self.fx.tmp / "no-such-home")
        env["XDG_CACHE_HOME"] = str(self.fx.home / ".cache")
        proc = subprocess.run([sys.executable, str(DOCTOR), "--platform", "codex", "--json"], capture_output=True, env=env, timeout=300)
        checks = {c["id"]: c for c in json.loads(proc.stdout.decode("utf-8"))["checks"]}
        self.assertEqual(checks["codex.trust.bundle"]["status"], "OK")

    # ---- output hygiene
    def test_no_hash_value_in_any_output(self):
        for mutate in (False, True):
            if mutate:
                (self.fx.codex_tree / "extra.txt").write_text("x", encoding="utf-8")
                edit(self.fx.codex_tree / "hooks" / "codex-hooks.json", "|Edit|Write|", "|Edit|")
            for args in ((), ("--json",)):
                rc, out, err = self.fx.run("--platform", "codex", *args)
                self.assertNotIn("sha256:", out + err)
                self.assertIsNone(re.search(r"[0-9a-f]{64}", out + err))

    def test_home_tree_is_unchanged(self):
        self.fx.write_codex(content=False)  # missing baseline: the tempting case to write one
        (self.fx.codex_tree / "extra.txt").write_text("x", encoding="utf-8")
        before = tree_digest(self.fx.home)
        self.fx.checks("--platform", "codex")
        self.assertEqual(tree_digest(self.fx.home), before)


if __name__ == "__main__":
    unittest.main()
