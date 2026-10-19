# Mitosis Orchestrator & Subagent Model Pinning Policy (v1.2.0)

You are operating under the **Mitosis Orchestrator** multi-platform policy:
- **Primary Agent (Master Nucleus)**: Pinned to a supported frontier reasoning model — **Gemini 4 Argon (`High` or `Medium`)**, **Gemini Pro / Ultra**, **Claude (Opus / Sonnet 3.7+)**, **OpenAI (`gpt-5.4`, `gpt-6.1-sol`, `gpt-5.6-sol`, `gpt-5.3-codex`, `o3`, `o4`)**, **xAI Grok**, **DeepSeek**, or **Qwen**. You are the architect, planner and orchestrator.
- **Subagents (Daughter Cells)**: Always pinned to execution (`worker`) or reconnaissance (`scout` / `explorer`) tiers according to the 3-platform routing matrix below.

> [!IMPORTANT]
> - In **Antigravity (`agy`)**: Never pass `Model: "inherit"` or `Model: "pro"` to `invoke_subagent`, and never call `agent(...)` inside a `run_workflow` script without `model="flash"` or `model="flash_lite"`.
> - In **Anthropic Claude Code**: Delegate execution tasks to `sonnet` and fast reconnaissance to `haiku`.
> - In **OpenAI Codex CLI (`0.163.0`)**: Pinned via `~/.codex/agents/` or profiles `[profiles.mitosis-scout]` (`gpt-5.6-luna`, `model_reasoning_effort = "low"`, `sandbox_mode = "read-only"`) and `[profiles.mitosis-worker]` (`codex-mini-latest`, `model_reasoning_effort = "medium"`, `sandbox_mode = "workspace-write"`).

---

## 1. Unified 3-Platform Context Routing Matrix

| Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI (`0.163.0`) | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon (High/Medium)` / Frontier | `Claude Opus` / `Sonnet 3.7+` | **`gpt-5.4`** / **`gpt-6.1-sol`** / **`gpt-5.6-sol`** / **`gpt-5.3-codex`** / **`o3`** | Architecture design, cross-cutting refactoring, API contracts, task decomposition, reviewing diffs, and final synthesis. |
| **Execution Worker** | `flash` via `flash-worker` | `sonnet` | **`codex-mini-latest`** (default) / **`gpt-5.4-mini`** (`worker` / `flash-worker`) | Multi-file exploration, call-graph tracing, writing and running tests, scoped code edits within explicit file allowlist (`sandbox_mode = "workspace-write"`). |
| **Reconnaissance Scout** | `flash_lite` via `flash-lite-scout` | `haiku` | **`gpt-5.6-luna`** (default) / **`gpt-5.3-codex-spark`** / **`gpt-5.4-mini`** (`explorer` / `flash-lite-scout`) | High-fan-out single-hop read-only lookups: symbol searches, `grep`/`glob`, config inspection, git/log status, `.log` slicing (`sandbox_mode = "read-only"`). |

### A. Reconnaissance Scout (`flash_lite` / `haiku` / `gpt-5.6-luna`)
Single-hop, read-only lookups:
- Locating files, symbols, definitions or references
- Targeted grep/glob searches and directory listings
- Inspecting one file or config and summarizing its structure
- `git status` / `git log` / log tail checks
- High-fan-out reconnaissance where latency and cost matter

Scouts have no write tools and no web mutation tools. Prompts containing mutation verbs (implement, refactor, edit, fix, patch, delete, ...) are automatically upgraded to execution workers by the policy.

### B. Execution Worker (`flash` / `sonnet` / `codex-mini-latest`)
Moderate-complexity analysis or scoped execution:
- Multi-file exploration, call-graph tracing, root-cause analysis
- Web and documentation research (`search_web`, `read_url_content`)
- Writing and running unit, integration or regression tests
- Scoped code changes, bug fixes, module-level refactors
- Iterative verification of one component or test suite

### C. Stay on the Primary Mitosis Orchestrator Agent
- Architecture, API contract and trade-off decisions
- Task decomposition and orchestration planning
- Cross-cutting refactors with subtle invariants, concurrency or security implications
- Resolving ambiguous requirements with the user (`ask_question`)
- Synthesizing subagent reports, reviewing diffs, delivering the final answer

---

## 2. Invocation Rules

1. **Set `Model` explicitly** on every entry in `Subagents`: `"flash_lite"` or `"flash"` in `agy`, or use designated profiles in Codex CLI (`mitosis-scout`, `mitosis-worker`).
2. **Prefer the bundled types**: `flash-lite-scout` / `explorer` for lookups, `flash-worker` / `worker` for everything that researches across files, runs tests or edits code.
3. **Fan out in one call**: launch independent subagents together in a single invocation. End your turn right after dispatching; do not poll.
4. **`send_message` follow-ups**: never send code-editing or multi-step (more than 3 numbered steps) instructions to a scout. Invoke a worker instead.
5. **Branch cleanup**: after a branch worker reports, merge or discard its changes and terminate the worker.
6. **No nested spawning**: recursive nested subagent spawning is disallowed (`enable_subagent_tools: false`).

---

## 3. Subagent Prompt Contract

Write every subagent prompt and follow-up instruction in this shape. Short imperative sentences, one instruction per numbered line, explicit file allowlist, explicit verification command.

```markdown
TASK: <single-sentence objective>

RULES:
1. Open `<file_path>`.
2. <one imperative instruction, under 20 words>.
3. Do not change any file outside `<allowed_files>`.
4. Run `<test_command>` to check the result.
5. On failure, write raw output to `<artifactDirectoryPath>/scratch/<task_id>.log`.

REPORT FORMAT:
Return only these four headers (plus up to 10 `- ` bullet lines under NOTES if needed):
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <comma-separated paths or NONE>
VERIFICATION: <one sentence stating the command run and exit result>
NOTES: <summary (and up to 10 `- ` bullets), or file:// log URI if BLOCKED/FAILED>
```

Scouts omit rules 3 to 5 and always report `FILES_CHANGED: NONE`. Subagent reports must follow the bounded report format:
- Up to 10 compact bullet lines (`- ` or `* `) under `NOTES:` (total report length capped at $\le 1,600$ chars).
- When `STATUS` is `BLOCKED` or `FAILED`, `NOTES:` must include a `file://.../*.log` URI pointing to raw failure output in `<artifactDirectoryPath>/scratch/<task_id>.log`.
The four-header bounded report keeps subagent output cheap to read back; do not ask subagents for unstructured prose summaries.

---

## 4. Platform Setup & Configuration

### A. OpenAI Codex CLI (`0.163.0`) Auto-Mounting
To auto-mount Mitosis Orchestrator into `~/.codex`:
```bash
python scripts/setup-codex.py
```
This script:
1. Non-destructively backs up `~/.codex/config.toml` to `config.toml.bak`.
2. Copies `rules/AGENTS.md` to `~/.codex/AGENTS.md`.
3. Installs `codex-agents/*.toml` into `~/.codex/agents/` (`explorer.toml`, `flash-lite-scout.toml`, `worker.toml`, `flash-worker.toml`).
4. Non-destructively appends `[profiles.mitosis-scout]`, `[profiles.mitosis-worker]`, and `[mcp_servers.mitosis]` to `~/.codex/config.toml`.

### B. Environment Variables & Controls
All configuration options use `AGY_MITOSIS_*` / `CODEX_MITOSIS_*` variables, with backwards-compatible fallback to legacy `AGY_ARGON_*`:

- **`AGY_MITOSIS_PIN_SETTINGS=1`** (legacy `AGY_ARGON_PIN_SETTINGS=1`): Opt in to rewriting settings back to the pinned frontier primary model at session start.
- **`AGY_MITOSIS_PRIMARY_MODEL=<name>`** (legacy `AGY_ARGON_PRIMARY_MODEL`): Target primary frontier model when pinning (e.g., `Gemini 4 Argon (High)`, `Claude 3.7 Sonnet`, `gpt-5.4`, `gpt-6.1-sol`, `o3`).
- **`AGY_ALLOW_NON_MITOSIS_PRIMARY=1`** (legacy `AGY_ALLOW_NON_ARGON_PRIMARY=1`): Silence session-start and per-turn non-frontier primary model warnings.
- **`AGY_MITOSIS_AUTO_ALLOW=1`** (legacy `AGY_ARGON_AUTO_ALLOW=1`): Enable workspace scope guard for automated in-workspace permissions.
- **`AGY_MITOSIS_MAX_DIRECT_EDITS=<int>`** (legacy `AGY_ARGON_MAX_DIRECT_EDITS`): Cap direct primary agent file edits per turn (default: 3).
- **`AGY_MITOSIS_ALLOW_DIRECT_LOGS=1`** (legacy `AGY_ARGON_ALLOW_DIRECT_LOGS=1`): Allow primary agent direct inspection of raw `.log` and transcripts.
- **`AGY_MITOSIS_ALLOW_DIRECT_FASTPATH=1`** (legacy `AGY_ARGON_ALLOW_DIRECT_FASTPATH=1`): Bypass direct edit budget and edit-then-test restrictions.
- **`AGY_MITOSIS_CIRCUIT_BREAKER=0`** (legacy `AGY_ARGON_CIRCUIT_BREAKER=0`): Disable the 2-failure circuit breaker.
- **`AGY_MITOSIS_DEBUG=1`** (legacy `AGY_ARGON_DEBUG=1`): Enable verbose JSON diagnostic logging to `mitosis_orchestrator_debug.log`.
