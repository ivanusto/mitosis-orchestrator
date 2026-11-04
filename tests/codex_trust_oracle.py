#!/usr/bin/env python3
"""MANUAL oracle: compare mitosis-doctor's Codex hook-trust hashes with what a real `codex app-server` computes.

Not collected by unittest discovery (no test_ prefix, no TestCase). Python 3.8, stdlib only.

    python3 tests/codex_trust_oracle.py [--codex PATH]

Builds a fixture plugin in a throwaway CODEX_HOME under a temp dir, starts `codex app-server` on stdio
with CODEX_HOME and HOME pointing at that temp dir, sends `initialize` then `hooks/list`, and compares
the returned `currentHash` ("<declaration>" or "<declaration>/<content>") with the doctor's functions.
Prints PASS or FAIL per hook and never a hash value. It never touches the real ~/.codex, copies no
credentials, and makes no model call (hooks/list is a local, read-only listing). Exit 0 all PASS,
1 any FAIL, 2 could not run.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mitosis_doctor_oracle", str(HERE.parent / "scripts" / "mitosis-doctor.py"))
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

PLUGIN_ID = "demo@test"
EVENT_NAMES = {"PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use", "SessionStart": "session_start",
               "UserPromptSubmit": "user_prompt_submit", "SubagentStop": "subagent_stop", "Stop": "stop"}

HOOKS = {"hooks": {
    "PreToolUse": [{"matcher": "Bash|apply_patch|mcp__x__.*", "hooks": [
        {"type": "command", "command": 'python3 "${PLUGIN_ROOT}/s.py" a || exit 0'},
        {"type": "command", "command": "echo second é", "timeout": 7, "statusMessage": "running"}]}],
    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo post"}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}],
    "UserPromptSubmit": [{"matcher": "ignored-for-this-event", "hooks": [{"type": "command", "command": "echo prompt"}]}],
    "SubagentStop": [{"matcher": "worker", "hooks": [{"type": "command", "command": "echo sub", "async": False}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
}}


def build_fixture(home):
    root = home / "plugins" / "cache" / "test" / "demo" / "local"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "empty").mkdir()
    (root / "sub").mkdir()
    (root / "__pycache__").mkdir()
    (root / ".codex-plugin" / "plugin.json").write_text('{"name":"demo"}', encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text(json.dumps(HOOKS), encoding="utf-8")
    (root / "s.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01\x02")
    (root / "__pycache__" / "s.cpython-38.pyc").write_bytes(b"junk")
    (home / "config.toml").write_text('[features]\nplugins = true\nhooks = true\n\n[plugins."%s"]\nenabled = true\n' % PLUGIN_ID,
                                      encoding="utf-8")
    return root


def expected_hooks():
    """{key: (decl hash)} the doctor would compute for the fixture manifest."""
    out = {}
    for event, groups in HOOKS["hooks"].items():
        label = EVENT_NAMES[event]
        for gi, group in enumerate(groups):
            for hi, handler in enumerate(group["hooks"]):
                key = "%s:hooks/hooks.json:%s:%d:%d" % (PLUGIN_ID, label, gi, hi)
                out[key] = doctor.codex_declaration_hash(label, group.get("matcher"), handler)
    return out


class Rpc:
    def __init__(self, argv, env, cwd):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.lines = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: self.proc.stderr.read(), daemon=True).start()

    def _pump(self):
        for raw in self.proc.stdout:
            self.lines.put(raw)
        self.lines.put(None)

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, method, params, timeout=60):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                raw = self.lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("timeout waiting for %s" % method)
            if raw is None:
                raise RuntimeError("app-server exited early")
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError("%s failed: %s" % (method, str(msg["error"])[:200]))
                return msg["result"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--codex", default=shutil.which("codex"), help="codex binary (default: from PATH)")
    args = ap.parse_args()
    if not args.codex:
        print("could not run: no codex binary on PATH")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="codex-trust-oracle-"))
    try:
        home = tmp / "codex-home"
        home.mkdir()
        cwd = tmp / "cwd"
        cwd.mkdir()
        root = build_fixture(home)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp), "CODEX_HOME": str(home), "TMPDIR": str(tmp)}
        rpc = Rpc([args.codex, "app-server"], env, str(cwd))
        try:
            rpc.call("initialize", {"clientInfo": {"name": "codex-trust-oracle", "version": "0"}})
            rpc.send({"jsonrpc": "2.0", "method": "initialized"})
            result = rpc.call("hooks/list", {"cwds": [str(cwd)]})
        except RuntimeError as exc:
            print("could not run: %s" % str(exc)[:200])
            return 2
        finally:
            rpc.proc.kill()
        hooks = [h for entry in result.get("data", []) for h in entry.get("hooks", []) if h.get("pluginId") == PLUGIN_ID]
        want = expected_hooks()
        content = doctor.codex_bundle_hash(root)
        seen_content = False
        failed = 0
        for key in sorted(want):
            got = next((h for h in hooks if h.get("key") == key), None)
            if got is None:
                print("FAIL %s: not listed by codex" % key)
                failed += 1
                continue
            current = got.get("currentHash") or ""
            decl, _, tail = current.partition("/")
            ok = decl == want[key]
            note = ""
            if tail:
                seen_content = True
                ok = ok and tail == content
                note = " (declaration+content)"
            print("%s %s%s" % ("PASS" if ok else "FAIL", key, note))
            failed += 0 if ok else 1
        if len(hooks) != len(want):
            print("FAIL: codex listed %d hooks, expected %d" % (len(hooks), len(want)))
            failed += 1
        if not seen_content:
            print("NOTE: this Codex lists only the declaration hash; the bundle content hash is not checked by hooks/list")
        print("%d failure(s)" % failed)
        return 1 if failed else 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
