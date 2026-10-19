# Mitosis Orchestrator (`mitosis-orchestrator`)

[繁體中文說明 (README.zh-TW.md)](./README.zh-TW.md)

An Antigravity (`agy`) plugin that pins supported frontier models — **Gemini 4 Argon (`High` or `Medium`)**, **Gemini Pro / Ultra**, **Claude (Opus / Sonnet)**, **xAI Grok**, **OpenAI (GPT-4 / GPT-5 / o1 / o3 / o4)**, **DeepSeek**, or **Qwen** — as the primary orchestrator agent (automatically preserving whichever thinking tier you select in the UI/CLI, while rejecting small tiers like mini / flash / lite / haiku / nano) and enforces context-aware model pinning (`flash` or `flash_lite`) for all subagents.

---

## Architecture & Context Routing Policy

| Tier | Model | Role & Responsibilities |
| :--- | :--- | :--- |
| **Primary Orchestrator** | `Gemini 4 Argon (High / Medium)`, `Gemini Pro / Ultra`, `Claude (Opus / Sonnet)`, `xAI Grok`, `OpenAI (GPT-4 / GPT-5 / o1 / o3 / o4)`, `DeepSeek`, or `Qwen` (rejects small tiers: mini / flash / lite / haiku / nano) | High-level architecture, multi-step planning, cross-cutting refactors, complex reasoning, security review, and final synthesis. Automatically follows your selected thinking tier. |
| **Tier 1 Subagent (`flash-worker`)** | `flash` | Multi-file codebase research, web/documentation synthesis, writing & running tests, and scoped code edits or debugging. |
| **Tier 2 Subagent (`flash-lite-scout`)** | `flash_lite` | Fast file/symbol lookups, grep/glob searches, git/log status checks, single-file inspection, and lightweight summarization. |

---

## Bundled Components

- **[`rules/AGENTS.md`](./rules/AGENTS.md)**: Always-on orchestration policy: the routing matrix, invocation rules, the bounded 4-header subagent report contract, and the branch-cleanup rule.
- **[`hooks.json`](./hooks.json) & [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py)** (Python 3.8+, standard library only):

  | Hook | What it does |
  | :--- | :--- |
  | `SessionStart` | Reads `~/.gemini/antigravity-cli/settings.json`, **warns** if the primary model is not a supported frontier model, and injects the policy notice. Rewrites the file only when `AGY_MITOSIS_PIN_SETTINGS=1` (or legacy `AGY_ARGON_PIN_SETTINGS=1`). |
  | `PreInvocation` | Checks the live session model every turn; warns once per invocation if it is not frontier-tier (catches UI model switches). Resets per-turn tracking (`turn_edited_files`, `consecutive_failures`). |
  | `PreToolUse` `invoke_subagent` | Classifies every subagent into `flash` / `flash_lite` from `TypeName`, `Workspace`, `Role` and `Prompt`. Strong signals (tests, debugging, tracing, mutation verbs) always beat lookup keywords. A `flash-lite-scout` given an edit task is upgraded to `flash-worker`. Denied when the circuit breaker is tripped. |
  | `PreToolUse` `send_message` | Denies code-change or multi-step (>3 numbered rules) follow-ups sent to a `flash_lite` recipient. |
  | `PreToolUse` `run_workflow` | Denies scripts whose `agent(...)` calls lack `model="flash"` / `model="flash_lite"`. |
  | `PreToolUse` `define_subagent` | Forces `enable_subagent_tools=false`. |
  | `PreToolUse` context hygiene & fast-path | Blocks direct primary-agent reads of `.log` or `transcript.jsonl` files (delegates to `flash-lite-scout`). Limits direct workspace file edits to 3 files per turn and blocks direct edit + test-runner loops in the primary agent (delegating to `flash-worker`). |
  | `PreToolUse` other tools & workspace guard | When `AGY_MITOSIS_AUTO_ALLOW=1` (or legacy `AGY_ARGON_AUTO_ALLOW=1`), in-workspace file/command tools are answered `allow`; paths outside, privilege escalation, recursive deletes of a root, system-altering commands, force pushes and downloads piped to a shell are answered `ask`. When unset or for tools without path arguments, returns explicit `allow` pass-through so tools are never denied by hook output. |
  | `PostToolUse` | Pairs spawned subagents with metadata; tracks command and subagent failure streaks for the circuit breaker. |
  | `Stop` | If `Workspace:"branch"` workers are still alive when the turn is fully idle, asks once per set of live workers to merge/discard and kill them. Never blocks cancellations, errors or budget caps. |

- **[`agents/`](./agents/)**:
  - **[`flash-lite-scout`](./agents/flash-lite-scout/agent.md)**: read-only scout pinned to `model: flash_lite`; local lookup tools only, no web, no writes.
  - **[`flash-worker`](./agents/flash-worker/agent.md)**: research & execution worker pinned to `model: flash`; edits only within the prompt's file allowlist and stops after two failed attempts.
  - Both return a bounded four-header report (`STATUS / FILES_CHANGED / VERIFICATION / NOTES`, supporting up to 10 bullet lines under `NOTES`, $\le 1,600$ chars, and requiring a `file://` log URI when `BLOCKED` or `FAILED`) so the orchestrator reads back minimal tokens.
- **[`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md)**: the `/orchestrate` four-phase workflow (`flash_lite` recon → frontier architect plan → `flash` execution → verification and branch cleanup).

### Environment variables

All configuration options support native `AGY_MITOSIS_*` environment variables with backwards-compatible fallback to legacy `AGY_ARGON_*` variables:

| Variable | Effect |
| :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS=1` | Opt in to rewriting `settings.json` back to a pinned frontier primary model at session start (legacy `AGY_ARGON_PIN_SETTINGS=1` supported). |
| `AGY_MITOSIS_PRIMARY_MODEL` | Target model when pinning: `Gemini 4 Argon (High)` (default), `Gemini 4 Argon (Medium)`, `Claude 3.7 Sonnet`, or any supported frontier model (legacy `AGY_ARGON_PRIMARY_MODEL` supported). |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY=1` | Silence all primary-model warnings (legacy `AGY_ALLOW_NON_ARGON_PRIMARY=1` supported). |
| `AGY_MITOSIS_AUTO_ALLOW=1` | Enable the workspace scope guard (off by default; legacy `AGY_ARGON_AUTO_ALLOW=1` supported). |
| `AGY_MITOSIS_MAX_DIRECT_EDITS=<int>` | Max distinct workspace file edits per turn by the primary agent (default: 3; legacy `AGY_ARGON_MAX_DIRECT_EDITS` supported). |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS=1` | Allow the primary agent to read raw `.log` and `transcript.jsonl` files directly (legacy `AGY_ARGON_ALLOW_DIRECT_LOGS=1` supported). |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH=1` | Allow the primary agent to bypass direct edit limits and edit-then-test runner loops (legacy `AGY_ARGON_ALLOW_DIRECT_FASTPATH=1` supported). |
| `AGY_MITOSIS_CIRCUIT_BREAKER=0` | Disable the 2-failure circuit breaker (legacy `AGY_ARGON_CIRCUIT_BREAKER=0` supported). |
| `AGY_MITOSIS_DEBUG=1` | Append one JSON line per hook call to `<artifactDirectoryPath>/mitosis_orchestrator_debug.log` (mode, tool, payload keys, decision; legacy `AGY_ARGON_DEBUG=1` supported). |

---

## Installation

### Option 1: Clone directly into Antigravity global plugins directory

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

### Option 2: Install from a local checkout via `agy plugin`

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

Restart `agy` after installing. You can manage or toggle the plugin at any time with `/plugin` or `agy plugin enable mitosis-orchestrator` / `agy plugin disable mitosis-orchestrator`.

---

## Development

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

**Headless note.** In `agy -p` (print) mode Antigravity soft-denies every tool that would need a permission prompt, regardless of hook decisions or `permissionOverrides`; only `settings.json` allow-rules or `--dangerously-skip-permissions` apply there. The workspace scope guard therefore only removes prompts in interactive sessions (verified interactively on agy 1.4.0: in-workspace edits and test commands ran without a prompt).

Verified live on `agy` 1.4.0 (2026-10-16): `SessionStart` and `PreInvocation` notices reach the model; the live model id arrives as e.g. `gemini-4-argon-high` / `gemini-3.8-flash-high`; `Stop` receives `fullyIdle`, `executionNum` and `terminationReason` (`NO_TOOL_CALL` on a normal end of turn); subagent metadata files flip from `SUBAGENT_STATE_ALIVE` to `SUBAGENT_STATE_KILLED` after `manage_subagents(Action="kill")`.

See [CHANGELOG.md](./CHANGELOG.md) for release notes.

---

## License

[MIT](./LICENSE)
