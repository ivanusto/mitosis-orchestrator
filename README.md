# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>Next-Generation Cross-Model Frontier Orchestrator Plugin for Google Antigravity (<code>agy</code>) and Anthropic Claude Code</strong><br/>
  <a href="./README.zh-TW.md">繁體中文說明 (README.zh-TW.md)</a> · <a href="./CHANGELOG.md">Changelog</a> · <a href="./rules/AGENTS.md">Orchestration Rules</a>
</p>

---

## Overview — Why Mitosis?

Inspired by biological **mitosis (cell division)**, **Mitosis Orchestrator** treats your chosen **Frontier Reasoning Model** as the central **Master Nucleus (Primary Architect)**. Instead of spending high-cost frontier tokens and context window capacity on single-file searches, raw log reading, or repetitive edit-and-test cycles, Mitosis Orchestrator divides complex engineering work into specialized, context-bounded **daughter subagents**:

- **Master Nucleus (Primary Frontier Orchestrator)**: Retains full architectural context, designs API contracts, decomposes tasks, resolves cross-cutting invariants, and synthesizes final results.
- **Execution Daughter Cells (`flash-worker` · `flash` on agy / `sonnet` on Claude Code)**: Execute parallel multi-file analysis, documentation/web research, scoped code edits, and unit/integration test loops within an explicit file allowlist.
- **Reconnaissance Daughter Cells (`flash-lite-scout` · `flash_lite` on agy / `haiku` on Claude Code)**: Perform high-fan-out, single-hop read-only lookups—locating symbols, running `grep`/`glob` searches, slicing `.log` or transcript files, and inspecting single configs at minimal latency and cost.

---

## Supported Frontier Primary Models

Mitosis Orchestrator is vendor-agnostic at the primary tier. It automatically preserves the thinking tier or frontier model you select in the Antigravity or Claude Code UI/CLI while strictly filtering out lightweight models from acting as the primary orchestrator:

| Provider | Supported Primary Orchestrator Models | Auto-Filtered Small Tiers (Blocked as Primary) |
| :--- | :--- | :--- |
| **Anthropic** | `Claude Opus`, `Claude Sonnet` (e.g., `Claude 3.7 Sonnet`, `Claude 4 Opus`) | `Claude Haiku` (`claude-3-5-haiku`, etc.) |
| **Google Gemini** | `Gemini 4 Argon (High / Medium)`, `Gemini Pro`, `Gemini Ultra` | `gemini-3-flash`, `gemini-flash-lite`, `gemini-nano` |
| **xAI** | `Grok 3`, `Grok 4`, `grok-3`, `grok-4`, `grok-3-thinking` | `grok-3-mini`, `grok-3-mini-fast` |
| **OpenAI** | `GPT-4` (`gpt-4o`, `gpt-4.1`, `gpt-4.5`), `GPT-5`, `o1`, `o3`, `o4` | `gpt-4o-mini`, `gpt-4.1-mini`, `gpt-4.1-nano`, `o1-mini`, `o3-mini`, `o4-mini` |
| **DeepSeek** | `DeepSeek-R1`, `DeepSeek-V3`, `deepseek-reasoner`, `deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`, `Qwen Plus`, `Qwen 2.5 / 3`, `QwQ` (`qwq-32b`) | `qwen-lite`, `qwen-vl-lite` |

> [!NOTE]
> Boundary-aware regex filtering (`(?<![a-z0-9])(flash|lite|mini|nano|haiku)(?![a-z0-9])`) rejects any `mini`, `flash`, `lite`, `haiku`, or `nano` variant across hyphen, space, or underscore naming conventions while never false-matching `gemini`.

---

## Mitotic Context Routing Matrix

| Tier | agy Model | Claude Code Model | Components | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Orchestrator** | Frontier Model (`Argon`, `Opus/Sonnet`, `GPT-4/5`, `o1/o3/o4`, `Grok`, `DeepSeek`, `Qwen`) | Frontier Model (`Claude Opus`, `Claude Sonnet 3.7`, `o1/o3`) | [`rules/AGENTS.md`](./rules/AGENTS.md) | System architecture, multi-step planning, cross-cutting refactors, security/concurrency review, user clarification, and final synthesis. |
| **Execution Tier (Worker)** | `flash` | `sonnet` | [`agents/flash-worker`](./agents/flash-worker/agent.md)<br/>[`claude-agents/flash-worker.md`](./claude-agents/flash-worker.md) | Multi-file codebase exploration, call-graph tracing, web/documentation research, writing & running tests, and scoped code edits within an explicit allowlist. |
| **Reconnaissance Tier (Scout)** | `flash_lite` | `haiku` | [`agents/flash-lite-scout`](./agents/flash-lite-scout/agent.md)<br/>[`claude-agents/flash-lite-scout.md`](./claude-agents/flash-lite-scout.md) | Single-hop read-only lookups: file/symbol location, `grep`/`glob` searches, `git status`/`git log`, log tail slicing, and single-file inspection. No web, no writes. |

---

## Dual-Platform Architecture (`agy` + Claude Code)

Mitosis Orchestrator v1.1.0 natively supports both runtime environments in a unified package:

```text
mitosis-orchestrator/
├── plugin.json                 # Antigravity native manifest
├── .claude-plugin/
│   └── plugin.json             # Claude Code extension manifest
├── hooks.json                  # Antigravity lifecycle hooks
├── hooks/
│   └── hooks.json              # Claude Code lifecycle hooks (${CLAUDE_PLUGIN_ROOT})
├── agents/                     # Antigravity subagents (flash / flash_lite)
│   ├── flash-worker/agent.md
│   └── flash-lite-scout/agent.md
├── claude-agents/              # Claude Code subagents (sonnet / haiku)
│   ├── flash-worker.md
│   └── flash-lite-scout.md
├── rules/
│   └── AGENTS.md               # Unified orchestration & routing policy
├── skills/
│   └── orchestrate/SKILL.md    # Portable /orchestrate workflow
└── scripts/
    └── enforce-orchestrator.py # Zero-dependency Python hook engine
```

### Lifecycle Hooks Comparison

| Hook Event | Antigravity (`agy`) | Claude Code | Engine Guardrail |
| :--- | :--- | :--- | :--- |
| **Session Initialization** | `SessionStart` | `SessionStart` | Verifies configured primary model against frontier allowlist; injects routing policy context. |
| **Turn / Prompt Start** | `PreInvocation` | `UserPromptSubmit` | Inspects live turn model; resets per-turn edit budget and failure counters. |
| **Tool Interception** | `PreToolUse` (`*`) | `PreToolUse` (`*`) | Classifies `invoke_subagent` / `Agent` / `Task` into scout/worker; enforces scout read-only protection; circuit breaker; fast-path budget. |
| **Post-Execution Cleanup** | `PostToolUse` | `PostToolUse` | Records subagent completion metadata; tracks consecutive failures for circuit breaker. |
| **Subagent Termination** | Handled in primary loop | `SubagentStop` | Validates subagent completion and outputs before handing control back. |
| **Session / Turn Stop** | `Stop` | `Stop` | Reminds orchestrator once if isolated branch/worktree workers remain active before finishing. |

---

## Configuration & Environment Variables

All configuration options are accessible via `AGY_MITOSIS_*` on Antigravity and `CLAUDE_MITOSIS_*` on Claude Code (with backwards compatibility for legacy `AGY_ARGON_*` variables):

| Antigravity Variable | Claude Code Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS` | — | `0` | Set to `1` to automatically rewrite `settings.json` to the target frontier model at session start. |
| `AGY_MITOSIS_PRIMARY_MODEL` | `CLAUDE_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` | Target frontier model name when pinning. |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` | `CLAUDE_MITOSIS_ALLOW_NON_PRIMARY` | `0` | Set to `1` to silence all non-frontier primary model warnings. |
| `AGY_MITOSIS_AUTO_ALLOW` | `CLAUDE_MITOSIS_AUTO_ALLOW` | `0` | Set to `1` to enable the in-workspace permission scope guard. |
| `AGY_MITOSIS_MAX_DIRECT_EDITS` | `CLAUDE_MITOSIS_MAX_DIRECT_EDITS` | `3` | Maximum distinct workspace files the primary agent may edit directly per turn. |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS` | `CLAUDE_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | Set to `1` to allow primary agent to inspect `.log` and transcripts directly. |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH` | `CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | Set to `1` to allow unlimited direct workspace edits and edit-then-test loops. |
| `AGY_MITOSIS_CIRCUIT_BREAKER` | `CLAUDE_MITOSIS_CIRCUIT_BREAKER` | `1` | Set to `0` to disable the 2-consecutive-failure circuit breaker. |
| `AGY_MITOSIS_DEBUG` | `CLAUDE_MITOSIS_DEBUG` | `0` | Set to `1` to append diagnostic JSON lines to `mitosis_orchestrator_debug.log`. |

---

## Installation

### A. For Google Antigravity (`agy`)

#### Option 1: Clone directly into global plugins
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

#### Option 2: Install via the `agy plugin` CLI
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

Restart `agy` after installation. You can toggle the plugin via `/plugin` or:
```bash
agy plugin enable mitosis-orchestrator
agy plugin disable mitosis-orchestrator
```

### B. For Anthropic Claude Code

#### Option 1: Load via CLI flag
```bash
claude --plugin-dir ~/.gemini/config/plugins/mitosis-orchestrator
```

#### Option 2: Install as a local plugin
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.claude/plugins/mitosis-orchestrator
```
Inside Claude Code, run `/reload-plugins` to activate.

---

## Development & Verification

Run the test suite and validate plugin manifests:

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

See [CHANGELOG.md](./CHANGELOG.md) for detailed release history.

---

## License

[MIT](./LICENSE) © 2026 ivanusto
