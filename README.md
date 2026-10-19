# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>Next-Generation Cross-Model Frontier Orchestrator Plugin for Google Antigravity (<code>agy</code>)</strong><br/>
  <a href="./README.zh-TW.md">繁體中文說明 (README.zh-TW.md)</a> · <a href="./CHANGELOG.md">Changelog</a> · <a href="./rules/AGENTS.md">Orchestration Rules</a>
</p>

---

## Overview — Why Mitosis?

Inspired by biological **mitosis (cell division)**, **Mitosis Orchestrator** treats your chosen **Frontier Reasoning Model** as the central **Master Nucleus (Primary Architect)**. Instead of spending high-cost frontier tokens and context window capacity on single-file searches, raw log reading, or repetitive edit-and-test cycles, Mitosis Orchestrator divides complex engineering work into specialized, context-bounded **daughter subagents**:

- **Master Nucleus (Primary Frontier Orchestrator)**: Retains full architectural context, designs API contracts, decomposes tasks, resolves cross-cutting invariants, and synthesizes final results.
- **Execution Daughter Cells (`flash-worker` · `flash`)**: Execute parallel multi-file analysis, documentation/web research, scoped code edits, and unit/integration test loops within an explicit file allowlist.
- **Reconnaissance Daughter Cells (`flash-lite-scout` · `flash_lite`)**: Perform high-fan-out, single-hop read-only lookups—locating symbols, running `grep`/`glob` searches, slicing `.log` or `transcript.jsonl` files, and inspecting single configs at minimal latency and cost.

---

## Supported Frontier Primary Models

Mitosis Orchestrator is vendor-agnostic at the primary tier. It automatically preserves the thinking tier or frontier model you select in the Antigravity UI/CLI while strictly filtering out lightweight models from acting as the primary orchestrator:

| Provider | Supported Primary Orchestrator Models | Auto-Filtered Small Tiers (Blocked as Primary) |
| :--- | :--- | :--- |
| **xAI** | `Grok 3`, `Grok 4`, `grok-3`, `grok-4`, `grok-3-thinking` | `grok-3-mini`, `grok-3-mini-fast` |
| **Anthropic** | `Claude Opus`, `Claude Sonnet` (e.g., `Claude 3.7 Sonnet`, `Claude 4 Opus`) | `Claude Haiku` (`claude-3-5-haiku`, etc.) |
| **Google Gemini** | `Gemini 4 Argon (High / Medium)`, `Gemini Pro`, `Gemini Ultra` | `gemini-3-flash`, `gemini-flash-lite`, `gemini-nano` |
| **OpenAI** | `GPT-4` (`gpt-4o`, `gpt-4.1`, `gpt-4.5`), `GPT-5`, `o1`, `o3`, `o4` | `gpt-4o-mini`, `gpt-4.1-mini`, `gpt-4.1-nano`, `o1-mini`, `o3-mini`, `o4-mini` |
| **DeepSeek** | `DeepSeek-R1`, `DeepSeek-V3`, `deepseek-reasoner`, `deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`, `Qwen Plus`, `Qwen 2.5 / 3`, `QwQ` (`qwq-32b`) | `qwen-lite`, `qwen-vl-lite` |

> [!NOTE]
> Boundary-aware regex filtering (`(?<![a-z0-9])(flash|lite|mini|nano|haiku)(?![a-z0-9])`) rejects any `mini`, `flash`, `lite`, `haiku`, or `nano` variant across hyphen, space, or underscore naming conventions while never false-matching `gemini`.

---

## Mitotic Context Routing Matrix

| Tier | Pinned Model | Component | Role & Responsibilities |
| :--- | :--- | :--- | :--- |
| **Primary Orchestrator** | Frontier Model (`Grok`, `Claude Opus/Sonnet`, `Gemini Argon/Pro/Ultra`, `GPT-4/5`, `o1/o3/o4`, `DeepSeek`, `Qwen`) | [`rules/AGENTS.md`](./rules/AGENTS.md) | System architecture, multi-step planning, cross-cutting refactors, security/concurrency review, user clarification (`ask_question`), and final synthesis. |
| **Tier 1 Subagent** | `flash` | [`flash-worker`](./agents/flash-worker/agent.md) | Multi-file codebase exploration, call-graph tracing, web/documentation research, writing & running tests, and scoped code edits within an explicit allowlist. |
| **Tier 2 Subagent** | `flash_lite` | [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | Single-hop read-only lookups: file/symbol location, `grep`/`glob` searches, `git status`/`git log`, log tail slicing, and single-file inspection. No web, no writes. |

---

## Bundled Components & Guardrails

### 1. Lifecycle Enforcement Hooks ([`hooks.json`](./hooks.json) & [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
Zero-dependency Python 3.8+ standard library hook engine enforcing orchestration discipline across every turn:

| Hook Event | Guardrail / Behavior |
| :--- | :--- |
| `SessionStart` | Checks `~/.gemini/antigravity-cli/settings.json`, **warns** if the configured primary model is not a supported frontier model, and injects the Mitosis routing policy. Rewrites `settings.json` only when `AGY_MITOSIS_PIN_SETTINGS=1` is enabled. |
| `PreInvocation` | Verifies the live session model (`payload.modelName`) on every turn; warns once per invocation if switched to a non-frontier model in the UI. Resets per-turn tracking (`turn_edited_files`, `consecutive_failures`). |
| `PreToolUse` · `invoke_subagent` | Classifies every spawned subagent into `flash` or `flash_lite` based on `TypeName`, `Workspace`, `Role`, and `Prompt`. Strong signals (testing, debugging, call-graph tracing, mutation verbs) always override lookup keywords. Automatically upgrades `flash-lite-scout` to `flash-worker` if given a mutation task. |
| `PreToolUse` · `send_message` | Blocks code-editing or multi-step (>3 numbered rules) follow-up instructions sent to a `flash_lite` scout. |
| `PreToolUse` · `run_workflow` | Blocks workflow scripts whose `agent(...)` calls omit `model="flash"` or `model="flash_lite"`. |
| `PreToolUse` · `define_subagent` | Forces `enable_subagent_tools=false` to prevent recursive nested subagent spawning. |
| `PreToolUse` · **Context Hygiene** | Blocks the primary agent from directly reading raw `.log` or `transcript.jsonl` files, keeping noisy logs out of the primary context window by delegating log slicing to `flash-lite-scout`. |
| `PreToolUse` · **Fast-Path Budget** | Caps direct workspace file mutations by the primary agent at `3` distinct files per turn (configurable via `AGY_MITOSIS_MAX_DIRECT_EDITS`) and blocks direct workspace edit + test-runner loops on the primary agent. |
| `PreToolUse` · **Circuit Breaker** | Trips after `2` consecutive failures across `run_command` or `invoke_subagent`, halting runaway retry loops until the next turn. |
| `PreToolUse` · **Workspace Guard** | Opt-in (`AGY_MITOSIS_AUTO_ALLOW=1`): auto-approves file and command tools that stay inside `workspacePaths` / `artifactDirectoryPath`, while prompting (`ask`) for outside paths, privilege escalation, root deletes, force pushes, or shell-piped downloads. Always returns an explicit pass-through `allow` when disabled. |
| `PostToolUse` | Pairs spawned subagents with Antigravity's `.system_generated/subagents/<cid>.json` metadata (or transcript fallback) and updates failure streaks. |
| `Stop` | When the turn is idle and `Workspace: "branch"` worktrees remain alive, reminds the primary agent once per set of live workers to merge/discard changes and call `manage_subagents(Action="kill")`. |

### 2. Bounded 4-Header Subagent Report Contract
Both [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) and [`flash-worker`](./agents/flash-worker/agent.md) return a strict, token-efficient 4-header report ($\le 1,600$ characters, up to 10 compact bullet lines under `NOTES:`, and a mandatory `file://.../*.log` scratch log URI whenever `STATUS` is `BLOCKED` or `FAILED`):

```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <comma-separated paths or NONE>
VERIFICATION: <one sentence stating the command run and exit result>
NOTES: <concise summary (and up to 10 `- ` bullets), or file:// log URI if BLOCKED/FAILED>
```

### 3. 4-Phase `/orchestrate` Skill ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
Provides the structured `/orchestrate` workflow:
1. **Phase 1 — Reconnaissance (`flash_lite` Fan-Out)**: Parallel read-only scouts gather file paths, symbols, and config layouts.
2. **Phase 2 — Architecture & Decomposition (Primary Frontier Agent)**: Synthesize recon findings, design interfaces, and partition work units.
3. **Phase 3 — Scoped Execution & Testing (`flash` Workers)**: Parallel workers implement changes within explicit file allowlists and run verification commands.
4. **Phase 4 — Synthesis & Branch Cleanup (Primary Frontier Agent)**: Review diffs, verify integration, and terminate branch worktrees.

---

## Configuration & Environment Variables

Mitosis Orchestrator uses `AGY_MITOSIS_*` environment variables (and maintains full backwards compatibility with legacy `AGY_ARGON_*` variables):

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS` | `0` | Set to `1` to automatically rewrite `~/.gemini/antigravity-cli/settings.json` to the target frontier model at session start. |
| `AGY_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` | Target frontier model name when `AGY_MITOSIS_PIN_SETTINGS=1` (e.g., `Claude 3.7 Sonnet`, `grok-4`, `o3`, `qwen-max`). |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` | `0` | Set to `1` (or `AGY_MITOSIS_ALLOW_NON_PRIMARY=1`) to silence all primary-model warnings. |
| `AGY_MITOSIS_AUTO_ALLOW` | `0` | Set to `1` to enable the in-workspace permission scope guard in interactive sessions. |
| `AGY_MITOSIS_MAX_DIRECT_EDITS` | `3` | Maximum distinct workspace files the primary agent may edit directly in a single turn. |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | Set to `1` to allow the primary agent to inspect `.log` and `transcript.jsonl` files directly. |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | Set to `1` to allow unlimited direct workspace edits and direct edit-then-test loops on the primary agent. |
| `AGY_MITOSIS_CIRCUIT_BREAKER` | `1` | Set to `0` to disable the 2-consecutive-failure circuit breaker. |
| `AGY_MITOSIS_DEBUG` | `0` | Set to `1` to append diagnostic JSON lines to `<artifactDirectoryPath>/mitosis_orchestrator_debug.log`. |

---

## Installation

### Option 1: Clone directly into Antigravity's global plugins directory

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

### Option 2: Install via the `agy plugin` CLI

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

Restart `agy` after installation. You can inspect or toggle the plugin at any time via `/plugin` or:

```bash
agy plugin enable mitosis-orchestrator
agy plugin disable mitosis-orchestrator
```

---

## Development & Verification

Run the 73-test unit suite and strict plugin schema validation:

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

See [CHANGELOG.md](./CHANGELOG.md) for release history.

---

## License

[MIT](./LICENSE) © 2026 ivanusto
