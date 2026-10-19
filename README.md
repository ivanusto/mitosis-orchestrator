# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>Cross-Model Frontier Orchestrator with Unified 3-Platform Support</strong><br/>
  <em>Google Antigravity (<code>agy</code>) · Anthropic Claude Code · OpenAI Codex CLI</em><br/>
  <a href="./README.zh-TW.md">繁體中文說明 (README.zh-TW.md)</a> · <a href="./CHANGELOG.md">Changelog</a> · <a href="./rules/AGENTS.md">Orchestration Rules</a>
</p>

---

## Overview — Why Mitosis?

Inspired by biological **mitosis (cell division)**, **Mitosis Orchestrator** treats your chosen **frontier reasoning model** as the central **Master Nucleus (Primary Architect)**. Instead of spending high-cost frontier tokens and context window capacity on single-file searches, raw log reading, or repetitive edit-and-test cycles, Mitosis Orchestrator divides complex engineering work into specialized, context-bounded **daughter subagents**:

- **Master Nucleus (Primary Frontier Orchestrator)**: Retains full architectural context, designs API contracts, decomposes tasks, resolves cross-cutting invariants, and synthesizes final results.
- **Execution Daughter Cells (`flash-worker` · `worker`)**: Execute parallel multi-file analysis, documentation/web research, scoped code edits, and unit/integration test loops within an explicit file allowlist.
- **Reconnaissance Daughter Cells (`flash-lite-scout` · `explorer`)**: Perform high-fan-out, single-hop read-only lookups—locating symbols, running `grep`/`glob` searches, slicing `.log` or `transcript.jsonl` files, and inspecting single configs at minimal latency and cost.

---

## Supported Frontier Primary Models

Mitosis Orchestrator is vendor-agnostic at the primary tier across all 3 platforms. It preserves the frontier model you select while filtering out lightweight models from acting as the primary orchestrator:

| Provider | Supported Primary Orchestrator Models | Auto-Filtered Small Tiers (Blocked as Primary) |
| :--- | :--- | :--- |
| **OpenAI** (Codex CLI / API) | **`gpt-6.1-sol`**, **`gpt-5.6-sol`**, `gpt-5.6-terra`, `gpt-6-astra`, `o1`, `o3`, `o4` | `gpt-5.6-luna`, `gpt-5.4-mini`, `o4-mini`, `o3-mini`, `gpt-4.1-mini` |
| **Anthropic** (Claude Code) | **`fable`** (Claude Fable 5.1), **`opus`** (Claude Opus 5.5), `sonnet` (Claude Sonnet 5.5) | `haiku` (Claude Haiku 5.5, `claude-haiku-*`) |
| **Google Gemini** (Antigravity `agy`) | `Gemini 4 Argon (High / Medium)`, `Gemini Pro`, `Gemini Ultra` | `gemini-3-flash`, `gemini-flash-lite`, `gemini-nano` |
| **xAI** | `Grok 3`, `Grok 4`, `grok-3`, `grok-4`, `grok-3-thinking` | `grok-3-mini`, `grok-3-mini-fast` |
| **DeepSeek** | `DeepSeek-R1`, `DeepSeek-V3`, `deepseek-reasoner`, `deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`, `Qwen Plus`, `Qwen 2.5 / 3`, `QwQ` (`qwq-32b`) | `qwen-lite`, `qwen-vl-lite` |

> [!NOTE]
> Boundary-aware regex filtering (`(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])`) rejects lightweight variants across hyphen, space, or underscore naming conventions while never false-matching `gemini`.

---

## Unified 3-Platform Context Routing Matrix (v1.3.1)

| Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon (High/Medium)` / Frontier | `fable` / `opus` / `sonnet` | **`gpt-6.1-sol`** / `gpt-5.6-sol` / `gpt-6-astra` / `o3` | System architecture, multi-step planning, cross-cutting refactors, security/concurrency review, user clarification, and final synthesis. |
| **Execution Worker** | `flash` via [`flash-worker`](./agents/flash-worker/agent.md) | `sonnet` via [`mitosis-orchestrator:flash-worker`](./claude-agents/flash-worker.md) | **`gpt-5.6-terra`** via [`worker`](./codex-agents/worker.toml) / [`flash-worker`](./codex-agents/flash-worker.toml) or `--profile mitosis-worker` | Multi-file codebase exploration, call-graph tracing, web/documentation research, writing & running tests, and scoped code edits within an explicit allowlist (`workspace-write`). |
| **Reconnaissance Scout** | `flash_lite` via [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | `haiku` via [`mitosis-orchestrator:flash-lite-scout`](./claude-agents/flash-lite-scout.md) | **`gpt-5.6-luna`** via [`explorer`](./codex-agents/explorer.toml) / [`flash-lite-scout`](./codex-agents/flash-lite-scout.toml) or `--profile mitosis-scout` | Single-hop read-only lookups: file/symbol location, `grep`/`glob` searches, `git status`/`git log`, log tail slicing, and single-file inspection (`read-only`). |

---

## Installation & Setup

Requirements: Python 3.11+ (standard library only; `tomllib` is used by the Codex installer), `git`.

### 1. Google Antigravity (`agy` 1.4+)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
agy plugin list            # mitosis-orchestrator (enabled, 1.3.0)
```

Alternatively clone straight into the global plugin directory; `agy` loads every directory under it automatically:

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
agy plugin validate ~/.gemini/config/plugins/mitosis-orchestrator --strict
```

> [!WARNING]
> If the predecessor plugin `argon-orchestrator` is still installed, uninstall it first (`agy plugin uninstall argon-orchestrator`). Both ship a skill named `orchestrate` and agents named `flash-worker` / `flash-lite-scout`, so they would collide and run two hook chains.

### 2. Anthropic Claude Code (2.1.292+)

The repository doubles as a single-plugin marketplace (`.claude-plugin/marketplace.json`), so it installs straight from GitHub:

```bash
claude plugin marketplace add ivanusto/mitosis-orchestrator
claude plugin install mitosis-orchestrator@mitosis-orchestrator
claude plugin list         # mitosis-orchestrator@mitosis-orchestrator  ✔ enabled
```

Updating later: `claude plugin marketplace update mitosis-orchestrator && claude plugin update mitosis-orchestrator@mitosis-orchestrator`.

For a local checkout, add the directory as the marketplace instead (`claude plugin marketplace add /path/to/mitosis-orchestrator`), or load it for one session only:

```bash
claude --plugin-dir /path/to/mitosis-orchestrator
```

What gets loaded: `hooks/hooks.json` (SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SubagentStop / Stop), the `/orchestrate` skill, and two subagents reachable through the `Agent` tool as `subagent_type: "mitosis-orchestrator:flash-lite-scout"` (haiku) and `subagent_type: "mitosis-orchestrator:flash-worker"` (sonnet). Verify in a new session: the SessionStart banner `[Mitosis Orchestrator Active] Primary model is 'fable'.` appears and `/orchestrate` is listed.

### 3. OpenAI Codex CLI (0.134+; verified on 0.154.0 and 0.163.0)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
python3 ~/mitosis-orchestrator/scripts/setup-codex.py
codex exec --profile mitosis-scout "Locate where parse_token is defined"
```

The installer is idempotent and performs, in order:

1. **Backs up** `$CODEX_HOME/config.toml` to `config.toml.bak` (`$CODEX_HOME` defaults to `~/.codex`).
2. **Copies** `rules/AGENTS.md` to `$CODEX_HOME/AGENTS.md`.
3. **Installs** native agent role files into `$CODEX_HOME/agents/`: `explorer.toml` & `flash-lite-scout.toml` (`gpt-5.6-luna`, `low` effort) and `worker.toml` & `flash-worker.toml` (`gpt-5.6-terra`, `medium` effort). Roles are selectable by the primary agent through `spawn_agent(agent_type=...)`.
4. **Installs** profile files `mitosis-scout.config.toml` and `mitosis-worker.config.toml` into `$CODEX_HOME/` (Codex 0.134+ reads `--profile <name>` from `$CODEX_HOME/<name>.config.toml`; the legacy `[profiles.*]` table is no longer honoured).
5. **Configures** `[mcp_servers.mitosis]` in `config.toml` with the running interpreter and the absolute path to `scripts/mitosis-mcp-server.py`.
6. **Validates** every TOML file and verifies all mounted files exist.

The MCP server runs scouts with `--sandbox read-only` and workers with `--sandbox workspace-write`. Set `MITOSIS_CODEX_YOLO=1` only if you deliberately want `--dangerously-bypass-approvals-and-sandbox`.

---

## Bundled Components & Guardrails

### 1. Lifecycle Enforcement Hooks ([`hooks.json`](./hooks.json) · [`hooks/hooks.json`](./hooks/hooks.json) · [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
Zero-dependency Python 3.8+ standard library hook engine enforcing orchestration discipline across every turn (~30 ms per call):

| Hook Event | Guardrail / Behavior |
| :--- | :--- |
| `SessionStart` | Checks settings, **warns** if the configured primary model is not a supported frontier model, and injects the Mitosis routing policy. Rewrites settings only when `*_MITOSIS_PIN_SETTINGS=1` is enabled. |
| `PreInvocation` (agy) · `UserPromptSubmit` (Claude Code / Codex) | Verifies the live session model on every turn; warns once per invocation if switched to a non-frontier model. Resets per-turn tracking (`turn_edited_files`, `consecutive_failures`). |
| `PreToolUse` · `invoke_subagent` (agy) | Classifies every spawned subagent into `flash` or `flash_lite` based on `TypeName`, `Workspace`, `Role`, and `Prompt`. Strong signals (testing, debugging, call-graph tracing, mutation verbs) always override lookup keywords. Automatically upgrades `flash-lite-scout` to `flash-worker` if given a mutation task. |
| `PreToolUse` · `Agent` (Claude Code) | Pins **only** this plugin's own agent types (`flash-worker`, `flash-lite-scout`, with or without the `mitosis-orchestrator:` prefix) and untyped `Agent` calls that use `model: inherit`. Other plugins' agents, built-in types (`Explore`, `Plan`, `general-purpose`, …), and untyped calls with an explicit model are passed through untouched. `CLAUDE_MITOSIS_PIN_SUBAGENTS=0` disables pinning entirely. |
| `PreToolUse` · `spawn_agent` / `codex exec` (Codex) | Pins subagents to `gpt-5.6-luna` (scout) or `gpt-5.6-terra` (worker) by prompt classification. |
| `PreToolUse` · `send_message` | Blocks code-editing or multi-step (>3 numbered rules) follow-up instructions sent to a scout. |
| `PreToolUse` · `run_workflow` | Blocks workflow scripts whose `agent(...)` calls omit model specification. |
| `PreToolUse` · `define_subagent` | Forces `enable_subagent_tools=false` to prevent recursive nested subagent spawning. |
| `PreToolUse` · **Context Hygiene** | Blocks the primary agent from directly reading raw `.log` or `transcript.jsonl` files, keeping noisy logs out of the primary context window by delegating log slicing to reconnaissance scouts. |
| `PreToolUse` · **Fast-Path Budget** | Caps direct workspace file mutations by the primary agent at `3` distinct files per turn (configurable via `*_MITOSIS_MAX_DIRECT_EDITS`) and blocks direct workspace edit + test-runner loops on the primary agent. |
| `PreToolUse` · **Circuit Breaker** | Trips after `2` consecutive failures across `run_command` / `Bash` / subagent spawns, halting runaway retry loops until the next turn. |
| `PreToolUse` · **Workspace Guard** | Opt-in (`*_MITOSIS_AUTO_ALLOW=1`): auto-approves file and command tools that stay inside the workspace, while prompting (`ask`; `deny` on Codex, which has no `ask`) for outside paths, privilege escalation, root deletes, force pushes, or shell-piped downloads. Always returns an explicit pass-through `allow` when disabled. |
| `PostToolUse` | Pairs spawned subagents with platform metadata and updates failure streaks. |
| `SubagentStop` / `Stop` | When the turn is idle and branch worktrees remain alive, reminds the primary agent once per set of live workers to merge/discard changes and clean them up. |

### 2. Bounded 4-Header Subagent Report Contract
All reconnaissance scouts and execution workers return a strict, token-efficient 4-header report (≤ 1,600 characters, up to 10 compact bullet lines under `NOTES:`, and a mandatory `file://.../*.log` scratch log URI whenever `STATUS` is `BLOCKED` or `FAILED`):

```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <comma-separated paths or NONE>
VERIFICATION: <one sentence stating the command run and exit result>
NOTES: <concise summary (and up to 10 `- ` bullets), or file:// log URI if BLOCKED/FAILED>
```

### 3. 4-Phase `/orchestrate` Skill ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
Provides the structured `/orchestrate` workflow:
1. **Phase 1 — Reconnaissance (Scout / Explorer Fan-Out)**: Parallel read-only scouts gather file paths, symbols, and config layouts.
2. **Phase 2 — Architecture & Decomposition (Primary Frontier Agent)**: Synthesize recon findings, design interfaces, and partition work units.
3. **Phase 3 — Scoped Execution & Testing (Workers)**: Parallel workers implement changes within explicit file allowlists and run verification commands.
4. **Phase 4 — Synthesis & Branch Cleanup (Primary Frontier Agent)**: Review diffs, verify integration, and terminate branch worktrees.

---

## Configuration & Environment Variables

Every variable is accepted with the `AGY_MITOSIS_*`, `CLAUDE_MITOSIS_*`, or `CODEX_MITOSIS_*` prefix (legacy `AGY_ARGON_*` names still work):

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `*_MITOSIS_PIN_SETTINGS` | `0` | Set to `1` to automatically rewrite settings to the target frontier model at session start. |
| `*_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` · `opus` · `gpt-6.1-sol` | Target frontier model name when pinning (per platform default shown). |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` / `*_MITOSIS_ALLOW_NON_PRIMARY` | `0` | Set to `1` to silence all primary-model warnings. |
| `CLAUDE_MITOSIS_PIN_SUBAGENTS` | `1` | Set to `0` to stop rewriting `Agent` calls in Claude Code entirely. |
| `*_MITOSIS_AUTO_ALLOW` | `0` | Set to `1` to enable the in-workspace permission scope guard in interactive sessions. |
| `*_MITOSIS_MAX_DIRECT_EDITS` | `3` | Maximum distinct workspace files the primary agent may edit directly in a single turn. |
| `*_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | Set to `1` to allow the primary agent to inspect `.log` and `transcript.jsonl` files directly. |
| `*_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | Set to `1` to allow unlimited direct workspace edits and direct edit-then-test loops on the primary agent. |
| `*_MITOSIS_CIRCUIT_BREAKER` | `1` | Set to `0` to disable the 2-consecutive-failure circuit breaker. |
| `*_MITOSIS_DEBUG` | `0` | Set to `1` to append diagnostic JSON lines to `mitosis_orchestrator_debug.log` next to the state file. |
| `MITOSIS_CODEX_YOLO` | `0` | Set to `1` to run Codex subagents with `--dangerously-bypass-approvals-and-sandbox` instead of tiered sandboxes. |

---

## Development & Verification

```bash
python3 -m unittest discover -s tests -v     # 144 tests
agy plugin validate . --strict
claude --plugin-dir . -p "/orchestrate smoke test"   # optional Claude Code dry run
```

See [CHANGELOG.md](./CHANGELOG.md) for release history.

---

## License

[MIT](./LICENSE) © 2026 ivanusto
