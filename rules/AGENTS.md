# Mitosis Orchestrator & Subagent Model Pinning Policy (v1.8.1)

You are operating under the **Mitosis Orchestrator** multi-platform policy:
- **Primary Agent (Master Nucleus)**: Pinned to a supported frontier reasoning model, one of **Gemini 4 Argon (`High` or `Medium`)**, **Gemini Pro / Ultra**, **Claude (`fable` / `opus` / `sonnet`)**, **OpenAI (`gpt-6.1-sol`, `gpt-5.6-sol`, `gpt-6-astra`, `o3`, `o4`)**, **xAI Grok**, **DeepSeek**, or **Qwen**. You are the architect, planner and orchestrator.
- **Subagents (Daughter Cells)**: Always pinned to execution (`worker`) or reconnaissance (`scout` / `explorer`) tiers according to the 3-platform routing matrix below.

> [!IMPORTANT]
> - In **Antigravity (`agy`)**: Never pass `Model: "inherit"` or `Model: "pro"` to `invoke_subagent`, and never call `agent(...)` inside a `run_workflow` script without `model="flash"` or `model="flash_lite"`.
> - In **Anthropic Claude Code**: Delegate execution tasks to `Agent` with `subagent_type: "mitosis-orchestrator:flash-worker"` (sonnet, effort `medium`) and fast reconnaissance to `subagent_type: "mitosis-orchestrator:flash-lite-scout"` (haiku, effort `low`); the hook pins both model and effort. Other plugins' agents and built-in types are not rewritten by the policy.
> - In **OpenAI Codex CLI (0.134+)**: Pinned via role files in `$CODEX_HOME/agents/` (`explorer` / `flash-lite-scout` on `gpt-5.6-luna`, `worker` / `flash-worker` on `gpt-5.6-terra`) selected with `spawn_agent(agent_type=...)`, or via profile files `$CODEX_HOME/mitosis-scout.config.toml` (`read-only`) and `mitosis-worker.config.toml` (`workspace-write`) used with `codex exec --profile <name>`.

---

## 1. Unified 3-Platform Context Routing Matrix

| Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon (High/Medium)` / Frontier | `fable` / `opus` / `sonnet` | **`gpt-6.1-sol`** / `gpt-5.6-sol` / `gpt-6-astra` / `o3` | Architecture design, cross-cutting refactoring, API contracts, task decomposition, reviewing diffs, and final synthesis. |
| **Execution Worker** | `flash` via `flash-worker` | `sonnet` via `mitosis-orchestrator:flash-worker` | **`gpt-5.6-terra`** via `worker` / `flash-worker` / `--profile mitosis-worker` | Multi-file exploration, call-graph tracing, writing and running tests, scoped code edits within explicit file allowlist (`workspace-write`). |
| **Reconnaissance Scout** | `flash_lite` via `flash-lite-scout` | `haiku` via `mitosis-orchestrator:flash-lite-scout` | **`gpt-5.6-luna`** via `explorer` / `flash-lite-scout` / `--profile mitosis-scout` | High-fan-out single-hop read-only lookups: symbol searches, `grep`/`glob`, config inspection, git/log status, `.log` slicing (`read-only`). |

### A. Reconnaissance Scout (`flash_lite` / `haiku` / `gpt-5.6-luna`)
Single-hop, read-only lookups:
- Locating files, symbols, definitions or references
- Targeted grep/glob searches and directory listings
- Inspecting one file or config and summarizing its structure
- `git status` / `git log` / log tail checks
- High-fan-out reconnaissance where latency and cost matter

Scouts have no write tools and no web mutation tools. On Claude Code the scout has `Bash` for read-only checks; the hook allows only an allowlist of read-only commands (`git status/log/diff/show`, `ls`, `rg`, `wc`, `tail`, ...) and denies redirection. Prompts containing mutation verbs (implement, refactor, edit, fix, patch, delete, ...) are automatically upgraded to execution workers by the policy.

### B. Execution Worker (`flash` / `sonnet` / `gpt-5.6-terra`)
Moderate-complexity analysis or scoped execution:
- Multi-file exploration, call-graph tracing, root-cause analysis
- Web and documentation research (`search_web`, `read_url_content`, `WebFetch`, `WebSearch`)
- Writing and running unit, integration or regression tests
- Scoped code changes, bug fixes, module-level refactors
- Iterative verification of one component or test suite

### C. Stay on the Primary Mitosis Orchestrator Agent
- Architecture, API contract and trade-off decisions
- Task decomposition and orchestration planning
- Cross-cutting refactors with subtle invariants, concurrency or security implications
- Resolving ambiguous requirements with the user (`ask_question` / `AskUserQuestion`)
- Synthesizing subagent reports, reviewing diffs, delivering the final answer

---

## 2. Invocation Rules

1. **Set the model tier explicitly** on every subagent: `"flash_lite"` or `"flash"` in `agy`; the plugin's own `subagent_type` in Claude Code; a role or profile in Codex CLI.
2. **Prefer the bundled types**: `flash-lite-scout` / `explorer` for lookups, `flash-worker` / `worker` for everything that researches across files, runs tests or edits code.
3. **Fan out in one call**: launch independent subagents together in a single invocation. On agy end your turn right after dispatching; on Claude Code subagents run in the background, so keep working on independent planning and let their completion notifications wake you. Do not poll.
4. **`send_message` / `SendMessage` follow-ups**: reuse a long-lived worker's context for follow-ups instead of re-spawning (it keeps its cache), but never send code-editing or multi-step (more than 3 numbered steps) instructions to a scout. Invoke a worker instead.
5. **Branch cleanup**: after a branch worker reports, merge or discard its changes and terminate the worker.
6. **No nested spawning**: recursive nested subagent spawning is disallowed (`enable_subagent_tools: false`).

---

## 3. Subagent Prompt Contract

Write every subagent prompt and follow-up instruction in this shape. Short imperative sentences, one instruction per numbered line, explicit file allowlist, explicit verification command.

<!-- BEGIN GENERATED: report-format -->
```markdown
TASK: <single-sentence objective>

RULES:
1. Open `<file_path>`.
2. <one imperative instruction, under 20 words>.
3. Do not change any file outside `<allowed_files>`.
4. Run `<test_command>` to check the result.
5. On failure, write raw output to `<scratch_dir>/<task_id>.log`.

REPORT FORMAT:
Return only these four headers (plus up to 10 `- ` bullet lines under NOTES if needed):
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <comma-separated paths or NONE>
VERIFICATION: <one sentence stating the command run and its exit result>
NOTES: <one sentence of findings or up to 10 bullets; when BLOCKED or FAILED, include a file:// log URI>
```
<!-- END GENERATED: report-format -->

Scouts omit rules 3 to 5 and always report `FILES_CHANGED: NONE`. Subagent reports must follow the bounded report format:
- Up to 10 compact bullet lines (`- ` or `* `) under `NOTES:` (total report length capped at ≤ 1,600 chars).
- When `STATUS` is `BLOCKED` or `FAILED`, `NOTES:` must include a `file://.../*.log` URI pointing to raw failure output in `<scratch_dir>/<task_id>.log` (`<artifactDirectoryPath>/scratch/` on agy).
The four-header bounded report keeps subagent output cheap to read back; do not ask subagents for unstructured prose summaries. If a prompt is dispatched without a `REPORT FORMAT` block, the hook appends the tier's block automatically (`*_MITOSIS_APPEND_REPORT_FORMAT=0` disables).

---

## 4. Platform Setup & Configuration

### A. OpenAI Codex CLI (0.134+) Auto-Mounting
```bash
python3 scripts/setup-codex.py
```
This script:
1. Non-destructively backs up `$CODEX_HOME/config.toml` to `config.toml.bak`.
2. Copies `rules/AGENTS.md` to `$CODEX_HOME/AGENTS.md`.
3. Installs `codex-agents/*.toml` role files into `$CODEX_HOME/agents/` (`explorer`, `flash-lite-scout`, `worker`, `flash-worker`).
4. Installs `codex-config/mitosis-scout.config.toml` and `mitosis-worker.config.toml` into `$CODEX_HOME/` for `codex exec --profile <name>`.
5. Writes `[mcp_servers.mitosis]` (this interpreter + absolute path to `scripts/mitosis-mcp-server.py`) into `config.toml`.

### B. Environment Variables & Controls
All options accept the `AGY_MITOSIS_*`, `CLAUDE_MITOSIS_*`, or `CODEX_MITOSIS_*` prefix, with backwards-compatible fallback to legacy `AGY_ARGON_*`:

- **`*_MITOSIS_PIN_SETTINGS=1`**: Opt in to rewriting settings back to the pinned frontier primary model at session start.
- **`*_MITOSIS_PRIMARY_MODEL=<name>`**: Target primary frontier model when pinning (defaults: `Gemini 4 Argon (High)`, `opus`, `gpt-6.1-sol`).
- **`AGY_ALLOW_NON_MITOSIS_PRIMARY=1`** / **`*_MITOSIS_ALLOW_NON_PRIMARY=1`**: Silence session-start and per-turn non-frontier primary model warnings.
- **`CLAUDE_MITOSIS_PIN_SUBAGENTS=0`**: Stop rewriting `Agent` calls in Claude Code entirely.
- **`CLAUDE_MITOSIS_SCOUT_MODEL` / `CLAUDE_MITOSIS_WORKER_MODEL`**: Claude Code model per tier (defaults `haiku` / `sonnet`; accepts `haiku`, `sonnet`, `opus`, `fable`).
- **`CLAUDE_MITOSIS_PIN_EFFORT=0`**: Stop adding `effort` to pinned Claude Code `Agent` calls.
- **`CLAUDE_MITOSIS_SCOUT_EFFORT` / `CLAUDE_MITOSIS_WORKER_EFFORT`**: Claude Code effort per tier (defaults `low` / `medium`).
- **`*_MITOSIS_AUTO_ALLOW=1`**: Enable workspace scope guard for automated in-workspace permissions.
- **`*_MITOSIS_MAX_DIRECT_EDITS=<int>`**: Cap direct primary agent file edits per turn (default: 3).
- **`*_MITOSIS_ALLOW_DIRECT_LOGS=1`**: Allow primary agent direct inspection of raw `.log` and transcripts.
- **`*_MITOSIS_ALLOW_DIRECT_FASTPATH=1`**: Bypass direct edit budget and edit-then-test restrictions.
- **`*_MITOSIS_CIRCUIT_BREAKER=0`**: Disable the 2-failure circuit breaker.
- **`*_MITOSIS_DEBUG=1`**: Enable verbose JSON diagnostic logging to `mitosis_orchestrator_debug.log`.
- **`MITOSIS_CODEX_YOLO=1`**: Run Codex subagents from the MCP server without sandboxing (default: scouts `read-only`, workers `workspace-write`).
