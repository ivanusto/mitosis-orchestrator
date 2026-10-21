#!/usr/bin/env python3
"""Mitosis MCP Server for OpenAI Codex CLI.

Zero-dependency stdio JSON-RPC MCP server providing tools:
  - mitosis_invoke_subagent
  - mitosis_scout
  - mitosis_worker

Classifies subagent tasks into scout or worker tiers, executes 'codex exec'
in parallel via ThreadPoolExecutor, and validates the 4-header report.
"""

import concurrent.futures
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Load enforce-orchestrator dynamically
def _load_sibling(name: str) -> Any:
    """Load scripts/<name>.py by file path (scripts/ is not on sys.path), cached in sys.modules."""
    key = "_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


_eo = _load_sibling("enforce-orchestrator")

classify_subagent = _eo.classify_subagent
decide_subagent = _eo.decide_subagent
validate_subagent_report = _eo.validate_subagent_report
DEFAULT_CODEX_PRIMARY_MODEL = _eo.DEFAULT_CODEX_PRIMARY_MODEL
DEFAULT_CODEX_WORKER_MODEL = _eo.DEFAULT_CODEX_WORKER_MODEL
DEFAULT_CODEX_SCOUT_MODEL = _eo.DEFAULT_CODEX_SCOUT_MODEL
codex_tier_model = _eo.codex_tier_model
codex_tier_effort = _eo.codex_tier_effort
MUTATION_RE = _eo.MUTATION_RE
WORKER_TYPE = _eo.WORKER_TYPE
SCOUT_TYPE = _eo.SCOUT_TYPE

SERVER_NAME = "mitosis-mcp-server"
SERVER_VERSION = "1.7.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS_DEFINITIONS = [
    {
        "name": "mitosis_invoke_subagent",
        "description": f"Fan-out one or more subagent tasks in parallel via OpenAI Codex CLI, classifying each into scout ({codex_tier_model('flash_lite')}, read-only sandbox) or worker ({codex_tier_model('flash')}, workspace-write sandbox) and validating bounded 4-header reports.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of subagent tasks to execute in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "The task prompt following AGENTS.md contract"},
                            "role": {"type": "string", "description": "Optional subagent role or label"},
                            "workspace": {"type": "string", "description": "inherit or branch (uses --worktree)"},
                            "model": {"type": "string", "description": "Explicit model override"}
                        },
                        "required": ["prompt"]
                    }
                },
                "prompt": {"type": "string", "description": "Single task prompt shorthand"}
            }
        }
    },
    {
        "name": "mitosis_scout",
        "description": f"Execute a read-only single-hop reconnaissance subagent using {codex_tier_model('flash_lite')}. If mutation verbs are detected, automatically upgraded to worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Read-only lookup, grep, locate or inspection task"},
                "role": {"type": "string", "description": "Optional scout role description"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "mitosis_worker",
        "description": f"Execute a scoped execution subagent for multi-file research, test execution, or code edits using {codex_tier_model('flash')} (workspace-write sandbox).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Execution task prompt following AGENTS.md contract"},
                "workspace": {"type": "string", "description": "inherit or branch (uses --worktree)"},
                "role": {"type": "string", "description": "Optional worker role description"}
            },
            "required": ["prompt"]
        }
    }
]


def resolve_codex_bin() -> str:
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin and os.path.exists(env_bin):
        return env_bin
    found = shutil.which("codex")
    if found:
        return found
    return "codex"


def sandbox_for(role_type: str) -> str:
    """Scouts run read-only; workers get workspace-write."""
    return "read-only" if role_type == SCOUT_TYPE else "workspace-write"


def build_codex_cmd(prompt: str, model: str, workspace: str = "inherit", role_type: str = WORKER_TYPE) -> List[str]:
    """Build the 'codex exec' argv. Sandboxed by tier unless MITOSIS_CODEX_YOLO=1."""
    cmd = [resolve_codex_bin(), "exec", "-m", model]
    if workspace in ("branch", "worktree"):
        cmd.append("--worktree")
    if os.environ.get("MITOSIS_CODEX_YOLO", "").strip() == "1":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--sandbox", sandbox_for(role_type)])
    # "--" ends option parsing, so a prompt that starts with "-" stays a prompt.
    cmd.extend(["--", prompt])
    return cmd


def run_codex_exec(
    prompt: str,
    model: str,
    workspace: str = "inherit",
    cwd: Optional[str] = None,
    runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
    role_type: str = WORKER_TYPE,
) -> Tuple[int, str, str]:
    """Run a single subagent task via 'codex exec'."""
    if runner is not None:
        return runner(prompt=prompt, model=model, workspace=workspace, cwd=cwd)

    cmd = build_codex_cmd(prompt, model, workspace=workspace, role_type=role_type)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=int(os.environ.get("CODEX_EXEC_TIMEOUT", "300")),
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return proc.returncode, stdout, stderr
    except Exception as exc:
        return 1, "", str(exc)


def decide_task(task: Dict[str, Any]) -> Tuple[str, str, Any]:
    """Route a task dictionary to (model, role_type, Decision)."""
    decision = decide_subagent(task)
    if decision.tier == "flash_lite":
        model = codex_tier_model("flash_lite")
        role_type = decision.type_override or SCOUT_TYPE
    else:
        model = codex_tier_model("flash")
        role_type = decision.type_override or WORKER_TYPE
    return model, role_type, decision


def classify_task(task: Dict[str, Any]) -> Tuple[str, str, str]:
    """Classify a task dictionary into (model, role_type, reason)."""
    model, role_type, decision = decide_task(task)
    return model, role_type, decision.reason


def execute_subagent_task(
    task: Dict[str, Any],
    runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    """Classify, execute, and validate a single subagent task."""
    prompt = str(task.get("prompt") or "").strip()
    if not prompt:
        return {
            "role": str(task.get("role") or "subagent"),
            "model": "unknown",
            "returncode": 1,
            "output": "",
            "error": "Task prompt cannot be empty.",
            "report_validation": "Prompt empty",
            "is_error": True,
        }

    workspace = str(task.get("workspace") or task.get("isolation") or "inherit").strip().lower()
    explicit_model = str(task.get("model") or "").strip()

    # The router always decides. An explicit model may only move a task up to
    # the worker tier: a scout model cannot take a task that needs writes, and
    # any other model (a frontier one, a typo) would escape the pinning.
    model, role_type, decision = decide_task(task)
    reason = decision.reason
    explicit = False
    if explicit_model and explicit_model != model:
        if explicit_model == codex_tier_model("flash"):
            model, role_type, explicit = explicit_model, WORKER_TYPE, True
            reason = f"explicit model '{explicit_model}' honoured"
        else:
            reason = f"{reason}; explicit model '{explicit_model}' ignored"
    elif explicit_model:
        explicit = True
        reason = f"explicit model '{explicit_model}' matches the decision"

    role = str(task.get("role") or role_type)
    cwd = task.get("cwd")
    _eo._record_routing({}, decision, model, codex_tier_effort(decision.tier), "codex-mcp", role_type)

    rc, stdout, stderr = run_codex_exec(prompt, model, workspace=workspace, cwd=cwd, runner=runner, role_type=role_type)
    output = stdout.strip() if stdout else stderr.strip()

    val_error = validate_subagent_report(output)
    is_error = rc != 0 or bool(val_error)

    return {
        "role": role,
        "model": model,
        "type": role_type,
        "reason": reason,
        "confidence": 1.0 if explicit else decision.confidence,
        "decision_source": "explicit" if explicit else decision.source,
        "workspace": workspace,
        "returncode": rc,
        "output": output,
        "stderr": stderr,
        "report_validation": "valid" if not val_error else val_error,
        "is_error": is_error,
    }


def execute_tasks_parallel(
    tasks: List[Dict[str, Any]],
    max_workers: Optional[int] = None,
    runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Execute multiple subagent tasks in parallel via ThreadPoolExecutor."""
    if not tasks:
        return []
    workers = max_workers or min(len(tasks), 8) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(execute_subagent_task, t, runner) for t in tasks]
        results = [f.result() for f in futures]
    return results


class MitosisMCPServer:
    def __init__(self, runner: Optional[Callable[..., Tuple[int, str, str]]] = None):
        self.runner = runner

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            }

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS_DEFINITIONS},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            return self.call_tool(req_id, tool_name, arguments)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def call_tool(self, req_id: Any, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "mitosis_scout":
            prompt = str(args.get("prompt") or "").strip()
            task = {
                "prompt": prompt,
                "role": args.get("role") or "scout",
                "workspace": "inherit",
                "TypeName": SCOUT_TYPE,
            }
            results = execute_tasks_parallel([task], runner=self.runner)
        elif tool_name == "mitosis_worker":
            prompt = str(args.get("prompt") or "").strip()
            task = {
                "prompt": prompt,
                "role": args.get("role") or "worker",
                "workspace": args.get("workspace") or "inherit",
                "TypeName": WORKER_TYPE,
            }
            results = execute_tasks_parallel([task], runner=self.runner)
        elif tool_name == "mitosis_invoke_subagent":
            raw_tasks = args.get("tasks")
            if isinstance(raw_tasks, list) and raw_tasks:
                task_list = raw_tasks
            else:
                prompt = str(args.get("prompt") or "").strip()
                task_list = [{"prompt": prompt, "role": args.get("role") or "subagent"}]
            results = execute_tasks_parallel(task_list, runner=self.runner)
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        texts = []
        any_error = False
        for r in results:
            role = r.get("role", "subagent")
            model = r.get("model", "")
            out = r.get("output", "")
            val = r.get("report_validation", "")
            if r.get("is_error"):
                any_error = True
            header = f"=== Subagent: {role} (Model: {model}) ==="
            val_notice = f"[Validation: {val}]" if val != "valid" else "[Validation: OK]"
            texts.append(f"{header}\n{val_notice}\n{out}")

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": "\n\n".join(texts)}],
                "isError": any_error,
            },
        }

    def run_stdio(self) -> None:
        """Run the stdio JSON-RPC loop."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line.startswith("Content-Length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                    sys.stdin.readline()
                    body = sys.stdin.read(length)
                    req = json.loads(body)
                except Exception:
                    self._write(_rpc_error(None, -32700, "Parse error"))
                    continue
            else:
                try:
                    req = json.loads(line)
                except Exception:
                    self._write(_rpc_error(None, -32700, "Parse error"))
                    continue

            if not isinstance(req, dict):
                self._write(_rpc_error(None, -32600, "Invalid Request"))
                continue
            try:
                resp = self.handle_request(req)
            except Exception as exc:  # one bad call must not take the server down
                resp = _rpc_error(req.get("id"), -32603, f"Internal error: {type(exc).__name__}: {exc}")
            if resp is not None:
                self._write(resp)
                if req.get("method") == "tools/call":
                    _flush_otel_async()

    @staticmethod
    def _write(resp: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def _flush_otel_async() -> None:
    """Send queued OTel spans from a daemon thread so the response is never delayed."""
    try:
        if _eo._otel_enabled():
            threading.Thread(target=_eo._flush_otel, daemon=True).start()
    except Exception:
        pass


def _rpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    server = MitosisMCPServer()
    server.run_stdio()


if __name__ == "__main__":
    main()
