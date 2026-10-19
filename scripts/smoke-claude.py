#!/usr/bin/env python3
"""smoke-claude: end-to-end check of the Mitosis hooks inside a real Claude Code headless run.

What it does (costs a few cents at `--model opus`, ~30 s):
  1. creates a throwaway git project with two Python files,
  2. runs `claude -p` asking the primary to spawn exactly one `mitosis-orchestrator:flash-lite-scout`,
     with CLAUDE_MITOSIS_DEBUG=1 so the hook writes its decisions to a debug log,
  3. asserts from the debug log that the Agent call was rewritten to model=haiku / effort=low
     with the REPORT FORMAT block appended, and that SubagentStop never had to send the scout
     back for a malformed report,
  4. asserts from Claude Code's own `--output-format json` usage that a haiku model actually ran,
  5. prints the session cost and (if present) the Mitosis cost report for the session.

Usage:
    python3 scripts/smoke-claude.py                 # primary = opus (cheapest frontier option)
    python3 scripts/smoke-claude.py --model fable   # test with your real primary
    python3 scripts/smoke-claude.py --keep          # keep the throwaway project directory

Requires the plugin to be installed in Claude Code (or pass --plugin-dir) and `claude` on PATH.
Exit code 0 on PASS, 1 on FAIL, 2 when `claude` could not be run.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROMPT = (
    "Use the Agent tool exactly once with subagent_type \"mitosis-orchestrator:flash-lite-scout\" "
    "to find which file defines main(). Then reply with one line naming the file and stop."
)


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Headless Claude Code smoke test for the Mitosis hooks.")
    ap.add_argument("--model", default="opus", help="primary model alias for `claude --model` (default opus)")
    ap.add_argument("--plugin-dir", help="pass `--plugin-dir` to claude instead of relying on the installed plugin")
    ap.add_argument("--max-turns", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=540, help="seconds before the claude run is killed")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway project directory")
    args = ap.parse_args(argv)

    if shutil.which("claude") is None:
        print("FAIL: `claude` not found on PATH", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="mitosis-smoke-"))
    try:
        (work / "app.py").write_text('def main():\n    print("hi")\n\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")
        (work / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        run(["git", "init", "-q"], cwd=work)
        run(["git", "add", "-A"], cwd=work)
        run(["git", "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid", "commit", "-qm", "init"], cwd=work)

        env = dict(os.environ, CLAUDE_MITOSIS_DEBUG="1")
        cmd = ["claude", "-p", PROMPT, "--model", args.model, "--max-turns", str(args.max_turns),
               "--permission-mode", "acceptEdits", "--output-format", "json"]
        if args.plugin_dir:
            cmd += ["--plugin-dir", args.plugin_dir]
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, text=True, capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"FAIL: claude did not finish within {args.timeout}s", file=sys.stderr)
            return 1
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print("FAIL: could not parse claude JSON output", file=sys.stderr)
            print(proc.stdout[-2000:], file=sys.stderr)
            print(proc.stderr[-2000:], file=sys.stderr)
            return 2

        session_id = str(result.get("session_id") or "")
        usage = result.get("modelUsage") or {}
        cost = result.get("total_cost_usd")
        print(f"session {session_id}  cost {cost} USD  turns {result.get('num_turns')}")
        print(f"result: {str(result.get('result'))[:200]}")

        failures = []
        haiku_models = [m for m in usage if "haiku" in m]
        if not haiku_models:
            failures.append(f"no haiku model in modelUsage (got {sorted(usage)})")

        log = Path(tempfile.gettempdir()) / "mitosis-orchestrator" / session_id / "mitosis_orchestrator_debug.log"
        if not log.is_file():
            failures.append(f"debug log not found at {log} (is the plugin loaded and CLAUDE_MITOSIS_DEBUG honoured?)")
        else:
            pinned = []
            report_blocks = 0
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("mode") == "pre-tool-use" and entry.get("tool") in ("Agent", "Task"):
                    out = (entry.get("result") or {}).get("hookSpecificOutput") or {}
                    ui = out.get("updatedInput") or {}
                    pinned.append((ui.get("model"), ui.get("effort"), ui.get("subagent_type"), out.get("permissionDecisionReason"), "REPORT FORMAT:" in str(ui.get("prompt") or "")))
                if entry.get("mode") == "subagent-stop" and (entry.get("result") or {}).get("decision") == "block":
                    report_blocks += 1
            print(f"hook: SubagentStop report rewrites requested: {report_blocks}")
            if report_blocks:
                failures.append(f"SubagentStop sent a subagent back {report_blocks} time(s) for a malformed report")
            if not pinned:
                failures.append("no Agent call passed through the PreToolUse hook")
            else:
                model, effort, stype, reason, has_format = pinned[0]
                print(f"hook: model={model} effort={effort} subagent_type={stype} report_format_in_prompt={has_format}")
                print(f"      {reason}")
                if model != "haiku":
                    failures.append(f"expected model=haiku, got {model}")
                if effort != "low":
                    failures.append(f"expected effort=low, got {effort}")
                if not has_format:
                    failures.append("REPORT FORMAT block was not appended to the scout prompt")

        report = Path(__file__).resolve().parent / "mitosis-cost-report.py"
        if report.is_file() and session_id:
            rep = run([sys.executable, str(report), "--project", str(_project_dir(work)), "--session", session_id])
            if rep.returncode == 0:
                print(rep.stdout.rstrip())

        if failures:
            print("FAIL:\n  - " + "\n  - ".join(failures), file=sys.stderr)
            return 1
        print("PASS")
        return 0
    finally:
        if args.keep:
            print(f"kept project at {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def _project_dir(cwd: Path) -> Path:
    import re
    return Path(os.path.expanduser("~")) / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9_-]", "-", str(cwd))


if __name__ == "__main__":
    sys.exit(main())
