# Mitosis Orchestrator (`mitosis-orchestrator`)

[![CI](https://github.com/ivanusto/mitosis-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanusto/mitosis-orchestrator/actions/workflows/ci.yml)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>Cross-Model Frontier Orchestrator with Unified 3-Platform Support</strong><br/>
  <em>Google Antigravity (<code>agy</code>) · Anthropic Claude Code · OpenAI Codex CLI</em><br/>
  <a href="./README.zh-TW.md">繁體中文說明 (README.zh-TW.md)</a> · <a href="./CHANGELOG.md">Changelog</a> · <a href="./rules/AGENTS.md">Orchestration Rules</a>
</p>

---

## Overview: Why Mitosis?

Inspired by biological **mitosis (cell division)**, **Mitosis Orchestrator** treats your chosen **frontier reasoning model** as the central **Master Nucleus (Primary Architect)**. Instead of spending high-cost frontier tokens and context window capacity on single-file searches, raw log reading, or repetitive edit-and-test cycles, Mitosis Orchestrator divides complex engineering work into specialized, context-bounded **daughter subagents**:

- **Master Nucleus (Primary Frontier Orchestrator)**: Retains full architectural context, designs API contracts, decomposes tasks, resolves cross-cutting invariants, and synthesizes final results.
- **Execution Daughter Cells (`flash-worker` · `worker`)**: Execute parallel multi-file analysis, documentation/web research, scoped code edits, and unit/integration test loops within an explicit file allowlist.
- **Reconnaissance Daughter Cells (`flash-lite-scout` · `explorer`)**: Perform high-fan-out, single-hop read-only lookups: locating symbols, running `grep`/`glob` searches, slicing `.log` or `transcript.jsonl` files, and inspecting single configs at minimal latency and cost.

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

## Unified 3-Platform Context Routing Matrix (v1.5.0)

| Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon (High/Medium)` / Frontier | `fable` / `opus` / `sonnet` | **`gpt-6.1-sol`** / `gpt-5.6-sol` / `gpt-6-astra` / `o3` | System architecture, multi-step planning, cross-cutting refactors, security/concurrency review, user clarification, and final synthesis. |
| **Execution Worker** | `flash` via [`flash-worker`](./agents/flash-worker/agent.md) | `sonnet` via [`mitosis-orchestrator:flash-worker`](./claude-agents/flash-worker.md) | **`gpt-5.6-terra`** via [`worker`](./codex-agents/worker.toml) / [`flash-worker`](./codex-agents/flash-worker.toml) or `--profile mitosis-worker` | Multi-file codebase exploration, call-graph tracing, web/documentation research, writing & running tests, and scoped code edits within an explicit allowlist (`workspace-write`). |
| **Reconnaissance Scout** | `flash_lite` via [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | `haiku` via [`mitosis-orchestrator:flash-lite-scout`](./claude-agents/flash-lite-scout.md) | **`gpt-5.6-luna`** via [`explorer`](./codex-agents/explorer.toml) / [`flash-lite-scout`](./codex-agents/flash-lite-scout.toml) or `--profile mitosis-scout` | Single-hop read-only lookups: file/symbol location, `grep`/`glob` searches, `git status`/`git log`, log tail slicing, and single-file inspection (`read-only`). |


### Confidence-graded routing and the optional judge (v1.5.0)

Every routing decision is a `Decision`: `tier`, `effort`, `confidence`, `source` (`rule`, `judge` or `default`), `rule` and `reason`. With `*_MITOSIS_DEBUG=1` each one is written to the debug log under `decisions`, so you can see which rule fired and how sure it was.

```
keyword rules ──► confidence ≥ 0.9 ──────────────────────────────► use the rule
      │
      └─ confidence < 0.9 and MITOSIS_JUDGE_URL set ──► judge ──► sure  ──► use the judge
                                                          └────► unsure / error ──► flash (cannot fail the task)
```

- **Rule confidence.** Explicit types, workspace and explicit models score 0.95 and are never judged. Mutation verbs score 0.95, heavy signals 0.9, lite signals 0.85, and the prompt-length fallbacks 0.5.
- **Read-only declarations.** A prompt that says it is read-only (`read-only`, `do not modify anything`, `唯讀`) drops mutation and heavy hits to 0.6, because in such prompts those words are usually the topic ("review the fixes", "plan the migration").
- **Wider negation.** `Do NOT git fetch/pull or modify` and `never delete or rename` now count as prohibitions; `don't X and fix Y` still asks for a fix.
- **The judge is off by default.** Setting `MITOSIS_JUDGE_URL` turns it on. It speaks the Jev `choice` request format, so TypeSafe Jev, a self-hosted Jev-compatible gateway and Ollaya are interchangeable. A judge may veto a scout's mutation upgrade, but never overrides explicit types, workspace modes or models. Without a URL, routing is identical to v1.4.1 apart from the wider negation.
- **Privacy.** The judge receives the first 2000 characters of the prompt after secrets, e-mail and IP addresses and home directories are masked ([`scripts/mitosis_judge.py`](./scripts/mitosis_judge.py)). Use a local gateway if prompts must not leave the machine.
- **When it matters.** On Claude Code the primary nearly always picks a typed agent, so the judge mostly sees untyped calls and Codex `spawn_agent` prompts. In a 66-prompt sample of real sessions, typed calls never reached it.

### Routing eval

Rule layer only, judge off. "Dangerous" means a worker task routed to the read-only scout.

| Set | Role in 1.7.0 | v1.6.0 | v1.7.0 |
| :--- | :--- | :--- | :--- |
| routing_cases (120 -> 183) | training | 119/120, 0 dangerous | 183/183, 0 dangerous |
| holdout v1 (40) | tuned in 1.5.1 and 1.7.0 | 32/40, 0 | 39/40, 0 |
| holdout v2 (40) | blind baseline, then training | 25/40, 4 | 39/40, 0 |
| holdout v3 (40) | blind first contact after the first tuning round, then training | 25/40, 9 | 29/40, 6 at first contact; 36/40, 0 after |
| holdout v4 (40) | blind, scored once, not tuned | 25/40, 8 | 33/40, 0 |

The 1.5.1 "zero dangerous" result did not generalise: three fresh blind sets each had 4 to 9 dangerous misroutes on v1.6.0. The first 1.7.0 round fitted the training sets but barely moved the blind score. All six remaining dangerous v3 misses came from the short-prompt fallback, so 1.7.0 changed it to send prompts with no lookup signal to the worker (a cost error instead of a failed scout run), and added run, CLI and docs-lookup signals. The 7 remaining v4 errors are lookups sent to the worker (cost, not failure). Known limitation: read-only questions that name debug, migration or build words still go to the worker without a judge; a judge (`MITOSIS_JUDGE_URL`) now gets a say on noun-use heavy hits (confidence 0.75).

---

## Choosing the Primary Model on Claude Code: Fable 5.1 vs Opus 5.5

Mitosis pins only the daughter cells (scouts on `haiku`, workers on `sonnet`). The Master Nucleus is whatever `/model` is set to, and on Claude Code that is usually `fable` (Claude Fable 5.1) or `opus` (Claude Opus 5.5). The numbers below are Anthropic API list prices in USD per million tokens at the time of writing (2026-10); check the official pricing page before relying on them.

| Tier | Model | Input | Output | Cache read | Relative to Opus 5.5 | Default effort |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| Primary | `fable` (Claude Fable 5.1) | 10.00 | 50.00 | 0.25 | 2.5x | `high` |
| Primary | `opus` (Claude Opus 5.5) | 4.00 | 20.00 | 0.20 | 1x | `medium` |
| Worker | `sonnet` (Claude Sonnet 5.5) | 2.00 | 10.00 | 0.20 | 0.5x | `high` (Mitosis pins `medium`) |
| Scout | `haiku` (Claude Haiku 5.5) | 0.10 | 0.50 | about 0.01 | 0.025x | `medium` (Mitosis pins `low`) |

### What changes when you swap the primary

- **Price per token: 2.5x.** Fable 5.1 and Opus 5.5 share the same tokenizer family, so the same transcript costs about 2.5x on Fable. Under Mitosis that multiplier applies only to the primary's share (planning, decomposition, reviewing diffs, synthesis); scouts and workers cost the same under either primary.
- **Wall-clock time.** Fable 5.1 takes longer turns: at `high` effort a single request that gathers context, builds and self-verifies can run many minutes, and a 15-minute turn on a hard task is normal. Opus 5.5 defaults to `medium`, where it matches or beats Opus 5 at `high` in fewer steps and with roughly half the tokens. Both models get noticeably slower at `xhigh` / `max`. Effort is the main latency and cost lever on both: `/effort` in the session, `effortLevel` in `~/.claude/settings.json`, or `CLAUDE_CODE_EFFORT_LEVEL`.
- **Capability.** Fable 5.1 pulls ahead on long-horizon autonomous runs, ambiguous specifications it must clarify itself, cross-cutting refactors with subtle invariants, deep code review, and sustained coordination of parallel subagents. Its `low` / `medium` settings still perform very well, often above previous models at `xhigh`. Opus 5.5 is the stronger value for multistep agentic coding and review on well-specified tasks: cheaper per solved task, not just per token.
- **Where the money goes.** In orchestrated sessions the primary's cache reads and output dominate the bill even with every scout and worker pinned (70% to 85% of spend in the two measured sessions below). An unpinned subagent inherits the primary model: built-in `Explore` / `general-purpose` agents and untyped `Agent` calls with an explicit `model` are deliberately left alone by the hook, and one inherited `fable` subagent in a measured session cost about 40x what thirteen `haiku` scouts did.

### Recommendation

| Situation | Primary | Effort |
| :--- | :--- | :--- |
| Day-to-day orchestration, well-specified tasks (default) | `opus` | `medium`; `high` for architecture-heavy turns |
| Cost-sensitive batches of routine tasks | `opus` | `low` / `medium` |
| Multi-hour autonomous run you walk away from | `fable` | `medium`; `high` only for the hardest parts |
| Ambiguous specification the primary has to pin down itself | `fable` | `medium` |
| Cross-cutting refactor with invariants, concurrency or security implications | `fable` | `high` |
| Deep review of a large diff or a repository-history investigation | `fable` | `medium` |

The plugin's default Claude pin target is `opus` for the same reason. Rule of thumb: if the cost report shows the primary carrying more than about two thirds of the tokens, you are paying primary rates for work the daughter cells should have done. Fix delegation first (typed `mitosis-orchestrator:*` subagents, no inherited models), then revisit the model.

**Illustrative example** (assumptions, not a measurement): a primary that reads 300K input tokens per task (80% served from cache) and writes 30K output tokens.

| Primary | Input (60K) | Cache read (240K) | Output (30K) | Per task |
| :--- | ---: | ---: | ---: | ---: |
| `fable` | 0.60 | 0.06 | 1.50 | about 2.16 USD |
| `opus` | 0.24 | 0.05 | 0.60 | about 0.89 USD |

That is a 2.4x difference on the primary alone; the scout and worker bill is identical in both rows. Real sessions differ in turn count and effort, so measure your own.

**Subscription plans (Pro / Max).** These are not billed per token, but their usage limits are weighted by model tier in the same direction, so a `fable` primary drains the limit noticeably faster than `opus`. The decision rule above is unchanged; the report below still tells you which model is consuming the budget.

### Measure it: `scripts/mitosis-cost-report.py`

A standard-library script that reads Claude Code transcripts (`~/.claude/projects/<project>/<session>.jsonl` plus `<session>/subagents/agent-*.jsonl`), de-duplicates streamed usage records by message id, aggregates tokens per model, splits primary from subagents, and estimates USD from a built-in price table (`--prices file.json` overrides it). Cache writes are priced at the 1-hour rate for the primary and the 5-minute rate for subagents (`--primary-cache-ttl` / `--subagent-cache-ttl`), which reproduces Claude Code's own cost accounting (`claude -p --output-format json`) to the cent on the sessions checked:

```bash
python3 scripts/mitosis-cost-report.py                     # newest session of the current project
python3 scripts/mitosis-cost-report.py --last 2            # the two newest sessions, side by side
python3 scripts/mitosis-cost-report.py --session <id-A> --session <id-B> --json
python3 scripts/mitosis-cost-report.py --format ccusage    # shape of `ccusage session --json` (ccusage 20.x)
python3 scripts/mitosis-cost-report.py --debug-log /tmp/mitosis-orchestrator/<session>/mitosis_orchestrator_debug.log
```

Since 1.7.0 the report also prints a savings line: every subagent row is re-priced on the primary model (the all-frontier counterfactual) and compared with what the pinned scouts and workers cost. `--format text|json|ccusage` selects the output (`--json` stays as an alias). When a hook debug log exists (`*_MITOSIS_DEBUG=1`; found automatically per session or passed with `--debug-log`), a routing section shows how many decisions went to each tier and which rules fired.

Trimmed output from two real (different) tasks, one per primary:

```text
  model                 role        turns      input    cache_w     cache_r    output  thinking       USD
  claude-fable-5-1      primary        60      1,740    252,164  10,664,346    99,292    22,559    12.691
  claude-fable-5-1      subagent       17        514    127,169   1,449,838     4,974       241     2.206
  claude-haiku-5-5      subagent       13         26    171,781   1,520,729    27,748    18,087     0.051
  TOTAL USD 14.948   primary share: 77% of tokens, 85% of USD

Comparison
  session       primary model(s)                    duration  primary USD  subagent USD  total USD
  e93f5d3f-0a4  claude-fable-5-1                    1h19m02s       12.691         2.256     14.948
  25dcc449-ba9  claude-opus-5-5                       58m29s        8.153         3.467     11.620
```

Run the same task under both primaries before drawing conclusions; the two sessions above were different jobs. The `claude-fable-5-1 subagent` row is the inherited-model trap described above.

### Tuning the daughter cells

Since 1.3.2 the Claude Code hook pins `effort` as well as `model` on every `Agent` call it rewrites: scouts run at `low`, workers at `medium` (the same split as the Codex roles). A valid `effort` passed by the caller is honoured. Override per tier or turn it off:

```bash
export CLAUDE_MITOSIS_WORKER_MODEL=opus      # heavier workers under a fable primary (haiku|sonnet|opus|fable)
export CLAUDE_MITOSIS_SCOUT_EFFORT=medium    # low|medium|high|xhigh|max
export CLAUDE_MITOSIS_PIN_EFFORT=0           # stop adding effort; models are still pinned
```

---

## Installation & Setup

Requirements: Python 3.11+ (standard library only; `tomllib` is used by the Codex installer), `git`.

### 1. Google Antigravity (`agy` 1.4+)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
agy plugin list            # mitosis-orchestrator (enabled, 1.7.0)
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

What gets loaded: `hooks/hooks.json` (SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SubagentStop / Stop), the `/orchestrate` skill, and two subagents reachable through the `Agent` tool as `subagent_type: "mitosis-orchestrator:flash-lite-scout"` (haiku, effort `low`) and `subagent_type: "mitosis-orchestrator:flash-worker"` (sonnet, effort `medium`). Verify in a new session: the SessionStart banner `[Mitosis Orchestrator Active] Primary model is 'fable'.` appears and `/orchestrate` is listed.

### 3. OpenAI Codex CLI (0.134+; verified on 0.154.0 and 0.163.0)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
python3 ~/mitosis-orchestrator/scripts/setup-codex.py
codex exec --profile mitosis-scout "Locate where parse_token is defined"
```

The installer is idempotent and performs, in order:

1. **Backs up** `$CODEX_HOME/config.toml` to `config.toml.bak` (`$CODEX_HOME` defaults to `~/.codex`).
2. **Copies** `rules/AGENTS.md` to `$CODEX_HOME/AGENTS.md`.
3. **Installs** native agent role files into `$CODEX_HOME/agents/`: `explorer.toml` & `flash-lite-scout.toml` (`gpt-5.6-luna`, `low` effort) and `worker.toml` & `flash-worker.toml` (`gpt-5.6-terra`, `medium` effort). Roles are selectable by the primary agent through `spawn_agent(agent_type=...)`; the `flash-*` role files exist only after this installer has run.
4. **Installs** profile files `mitosis-scout.config.toml` and `mitosis-worker.config.toml` into `$CODEX_HOME/` (Codex 0.134+ reads `--profile <name>` from `$CODEX_HOME/<name>.config.toml`; the legacy `[profiles.*]` table is no longer honoured).
5. **Configures** `[mcp_servers.mitosis]` in `config.toml` with the running interpreter and the absolute path to `scripts/mitosis-mcp-server.py`.
6. **Validates** every TOML file and verifies all mounted files exist.

The MCP server runs scouts with `--sandbox read-only` and workers with `--sandbox workspace-write`. Set `MITOSIS_CODEX_YOLO=1` only if you deliberately want `--dangerously-bypass-approvals-and-sandbox`.

Since 1.7.0 the plugin hook rewrites `spawn_agent` calls to the built-in `explorer` (scout) and `worker` roles, because Codex 0.163 rejects an unknown `agent_type` (`unknown agent_type '...'`) and the `flash-*` roles do not exist on plugin-only installs. Models and efforts come from `gpt-5.6-luna` / `gpt-5.6-terra` with `low` / `medium`; override them with `CODEX_MITOSIS_SCOUT_MODEL`, `CODEX_MITOSIS_WORKER_MODEL`, `CODEX_MITOSIS_SCOUT_EFFORT` and `CODEX_MITOSIS_WORKER_EFFORT`. The values are validated (the model is spliced into a `codex exec` command) and there is no `AGY_` form. `setup-codex.py` renders the role and profile TOMLs with the effective values and passes them to the MCP server through its `env` table.

### Plugin hooks (Codex 0.128+)

Codex can also load the hooks as a plugin:

```bash
codex plugin marketplace add <path-to-repo>
codex plugin add mitosis-orchestrator@mitosis-orchestrator
```

Enable the hooks feature in `config.toml`, then open Codex and approve the plugin hooks in `/hooks`; untrusted hooks are skipped silently. `setup-codex.py` is still needed for the MCP server, agent roles and profiles.

---

## Bundled Components & Guardrails

### 1. Lifecycle Enforcement Hooks ([`hooks.json`](./hooks.json) · [`hooks/hooks.json`](./hooks/hooks.json) · [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
Zero-dependency Python 3.8+ standard library hook engine enforcing orchestration discipline across every turn (~30 ms per call). On Claude Code the `PreToolUse` matcher is limited to the tools the engine actually inspects (`Agent`, `Task`, `Bash`, `Read`, `NotebookRead`, `Write`, `Edit`, `NotebookEdit`, `SendMessage`), so `Grep` / `Glob` / `WebFetch` / MCP calls no longer pay the hook start-up cost:

| Hook Event | Guardrail / Behavior |
| :--- | :--- |
| `SessionStart` | Checks settings, **warns** if the configured primary model is not a supported frontier model, and injects the Mitosis routing policy. Rewrites settings only when `*_MITOSIS_PIN_SETTINGS=1` is enabled. |
| `PreInvocation` (agy) · `UserPromptSubmit` (Claude Code / Codex) | Verifies the live session model on every turn; warns once per invocation if switched to a non-frontier model. Resets per-turn tracking (`turn_edited_files`, `consecutive_failures`). |
| `PreToolUse` · `invoke_subagent` (agy) | Classifies every spawned subagent into `flash` or `flash_lite` based on `TypeName`, `Workspace`, `Role`, and `Prompt`. Strong signals (testing, debugging, call-graph tracing, mutation verbs) always override lookup keywords. Automatically upgrades `flash-lite-scout` to `flash-worker` if given a mutation task, and appends the `REPORT FORMAT` block to prompts that lack one. |
| `PreToolUse` · `Agent` (Claude Code) | Pins **only** this plugin's own agent types (`flash-worker`, `flash-lite-scout`, with or without the `mitosis-orchestrator:` prefix) and untyped `Agent` calls that use `model: inherit`. Pins `effort` too (scout `low`, worker `medium`; caller values honoured; `CLAUDE_MITOSIS_PIN_EFFORT=0` disables) and appends the four-header `REPORT FORMAT` block to prompts that lack one (`CLAUDE_MITOSIS_APPEND_REPORT_FORMAT=0` disables). Other plugins' agents, built-in types (`Explore`, `Plan`, `general-purpose`, …), and untyped calls with an explicit model are passed through untouched. `CLAUDE_MITOSIS_PIN_SUBAGENTS=0` disables pinning entirely. |
| `PreToolUse` · `spawn_agent` / `codex exec` (Codex) | Pins subagents to `gpt-5.6-luna` (scout) or `gpt-5.6-terra` (worker) by prompt classification. |
| `PreToolUse` · `send_message` (agy) · `SendMessage` (Claude Code) | Blocks code-editing or multi-step (>3 numbered rules) follow-up instructions sent to a scout. Claude Code recipients are recognised by the agent id that `PostToolUse` / `SubagentStop` pair with the spawn. |
| `PreToolUse` · `run_workflow` | Blocks workflow scripts whose `agent(...)` calls omit model specification. |
| `PreToolUse` · `define_subagent` | Forces `enable_subagent_tools=false` to prevent recursive nested subagent spawning. |
| `PreToolUse` · **Context Hygiene** | Blocks the primary agent from directly reading raw `.log` or `transcript.jsonl` files, keeping noisy logs out of the primary context window by delegating log slicing to reconnaissance scouts. |
| `PreToolUse` · **Fast-Path Budget** | Caps direct workspace file mutations by the primary agent at `3` distinct files per turn (configurable via `*_MITOSIS_MAX_DIRECT_EDITS`) and blocks direct workspace edit + test-runner loops on the primary agent. |
| `PreToolUse` · **Circuit Breaker** | Trips after `2` consecutive failures across `run_command` / `Bash` / subagent spawns, halting runaway retry loops until the next turn. |
| `PreToolUse` · **Workspace Guard** | Opt-in (`*_MITOSIS_AUTO_ALLOW=1`): auto-approves file and command tools that stay inside the workspace, while prompting (`ask`; `deny` on Codex, which has no `ask`) for outside paths, privilege escalation, root deletes, force pushes, or shell-piped downloads. When disabled it gives no verdict: on Claude Code the hook returns `{}`, so your normal permission rules and prompts apply (1.5.0 and earlier returned `allow`, which skipped them). |
| `PostToolUse` | Pairs spawned subagents with platform metadata and updates failure streaks. |
| `SubagentStop` / `Stop` | When the turn is idle and branch worktrees remain alive, reminds the primary agent once per set of live workers to merge/discard changes and clean them up. On Claude Code, `SubagentStop` also checks that a `flash-lite-scout` / `flash-worker` ended with the four-header report and, once per agent, sends it back to rewrite its final message (`CLAUDE_MITOSIS_ENFORCE_REPORT=0` disables). |

### 2. Bounded 4-Header Subagent Report Contract
All reconnaissance scouts and execution workers return a strict, token-efficient 4-header report (≤ 1,600 characters, up to 10 compact bullet lines under `NOTES:`, and a mandatory `file://.../*.log` scratch log URI whenever `STATUS` is `BLOCKED` or `FAILED`):

<!-- BEGIN GENERATED: report-format -->
```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <comma-separated paths or NONE>
VERIFICATION: <one sentence stating the command run and its exit result>
NOTES: <one sentence of findings or up to 10 bullets; when BLOCKED or FAILED, include a file:// log URI>
```
<!-- END GENERATED: report-format -->

### 3. 4-Phase `/orchestrate` Skill ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
Provides the structured `/orchestrate` workflow:
1. **Phase 1: Reconnaissance (Scout / Explorer Fan-Out)**: Parallel read-only scouts gather file paths, symbols, and config layouts.
2. **Phase 2: Architecture & Decomposition (Primary Frontier Agent)**: Synthesize recon findings, design interfaces, and partition work units.
3. **Phase 3: Scoped Execution & Testing (Workers)**: Parallel workers implement changes within explicit file allowlists and run verification commands.
4. **Phase 4: Synthesis & Branch Cleanup (Primary Frontier Agent)**: Review diffs, verify integration, and terminate branch worktrees.

---

## Configuration & Environment Variables

Every variable is accepted with the `AGY_MITOSIS_*`, `CLAUDE_MITOSIS_*`, or `CODEX_MITOSIS_*` prefix (legacy `AGY_ARGON_*` names still work):

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `*_MITOSIS_PIN_SETTINGS` | `0` | Set to `1` to automatically rewrite settings to the target frontier model at session start. |
| `*_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` · `opus` · `gpt-6.1-sol` | Target frontier model name when pinning (per platform default shown). |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` / `*_MITOSIS_ALLOW_NON_PRIMARY` | `0` | Set to `1` to silence all primary-model warnings. |
| `CLAUDE_MITOSIS_PIN_SUBAGENTS` | `1` | Set to `0` to stop rewriting `Agent` calls in Claude Code entirely. |
| `CLAUDE_MITOSIS_SCOUT_MODEL` · `CLAUDE_MITOSIS_WORKER_MODEL` | `haiku` · `sonnet` | Claude Code model pinned per tier (`haiku`, `sonnet`, `opus`, `fable`); invalid values fall back to the default. |
| `CLAUDE_MITOSIS_PIN_EFFORT` | `1` | Set to `0` to stop adding `effort` to pinned `Agent` calls in Claude Code (models are still pinned). |
| `CLAUDE_MITOSIS_SCOUT_EFFORT` · `CLAUDE_MITOSIS_WORKER_EFFORT` | `low` · `medium` | Claude Code effort pinned per tier (`low`, `medium`, `high`, `xhigh`, `max`); a valid `effort` passed by the caller wins. |
| `CLAUDE_MITOSIS_ENFORCE_REPORT` | `1` | Set to `0` to stop `SubagentStop` from sending a plugin subagent back when its final message is not the four-header report. |
| `*_MITOSIS_APPEND_REPORT_FORMAT` | `1` | Set to `0` to stop appending the four-header `REPORT FORMAT` block to subagent prompts that lack one (agy `invoke_subagent`, Claude Code `Agent`). |
| `*_MITOSIS_AUTO_ALLOW` | `0` | Set to `1` to enable the in-workspace permission scope guard in interactive sessions. |
| `*_MITOSIS_MAX_DIRECT_EDITS` | `3` | Maximum distinct workspace files the primary agent may edit directly in a single turn. |
| `*_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | Set to `1` to allow the primary agent to inspect `.log` and `transcript.jsonl` files directly. |
| `*_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | Set to `1` to allow unlimited direct workspace edits and direct edit-then-test loops on the primary agent. |
| `*_MITOSIS_CIRCUIT_BREAKER` | `1` | Set to `0` to disable the 2-consecutive-failure circuit breaker. |
| `*_MITOSIS_DEBUG` | `0` | Set to `1` to append diagnostic JSON lines to `mitosis_orchestrator_debug.log` next to the state file. |
| `MITOSIS_JUDGE_URL` | unset | Jev-compatible endpoint for low-confidence routing decisions; unset disables the judge. |
| `MITOSIS_JUDGE_KEY` · `MITOSIS_JUDGE_MODEL` | unset · `jev-latest` | Bearer token and model name sent to the judge. The token is sent only over https or to a loopback / private-network host. |
| `MITOSIS_JUDGE_ALLOW_HTTP_KEY` | `0` | Set to `1` to also send the token over plain http to other hosts. |
| `MITOSIS_JUDGE_THRESHOLD` · `MITOSIS_JUDGE_MIN_CONFIDENCE` | `0.9` · `0.7` | Rule confidence below which the judge is asked; judge confidence below which it counts as unsure (falls back to `flash`). |
| `MITOSIS_JUDGE_TIMEOUT` | `1.5` | Seconds to wait for the judge before falling back to `flash`. |
| `CODEX_MITOSIS_SCOUT_MODEL` · `CODEX_MITOSIS_WORKER_MODEL` | `gpt-5.6-luna` · `gpt-5.6-terra` | Codex model pinned per tier. Read from the environment only (no `AGY_` form); invalid values fall back to the default. |
| `CODEX_MITOSIS_SCOUT_EFFORT` · `CODEX_MITOSIS_WORKER_EFFORT` | `low` · `medium` | Codex reasoning effort pinned per tier; invalid values fall back to the default. |
| `*_MITOSIS_LOCK_TIMEOUT` | `2` | Seconds to wait for the per-session state lock before failing open (`CLAUDE_`, `CODEX_` or `AGY_` prefix). |
| `MITOSIS_OTEL` | `0` | Set to `1` to emit an OpenTelemetry `invoke_agent` span per routing decision (needs a file or an endpoint below). |
| `MITOSIS_OTEL_FILE` | unset | Append one OTLP JSON body per line to this file. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` · `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` · `OTEL_EXPORTER_OTLP_HEADERS` | unset | Standard OTLP settings. `/v1/traces` is appended to the first; the second is used as-is; headers are `k=v,k2=v2`. |
| `MITOSIS_OTEL_TIMEOUT` | `0.5` | Seconds to wait on the OTLP endpoint; every telemetry failure is swallowed. |
| `MITOSIS_CODEX_YOLO` | `0` | Set to `1` to run Codex subagents with `--dangerously-bypass-approvals-and-sandbox` instead of tiered sandboxes. |

---

## Observability

**OpenTelemetry (opt-in).** With `MITOSIS_OTEL=1` and either `MITOSIS_OTEL_FILE` or an OTLP endpoint, every routing decision becomes one `invoke_agent {agent}` span following the OpenTelemetry GenAI conventions. Attributes: `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.conversation.id`, plus `mitosis.tier`, `mitosis.rule`, `mitosis.confidence`, `mitosis.source`, `mitosis.platform` and `mitosis.effort`. The span marks the routing moment (start equals end), because the subagent runs later inside the host CLI. Spans are flushed after the hook response, outside the state lock.

```bash
export MITOSIS_OTEL=1
export MITOSIS_OTEL_FILE=/tmp/mitosis-spans.jsonl                 # and/or:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

The debug log (`*_MITOSIS_DEBUG=1`) records the same decisions, now with the pinned model, effort and platform.

**Cost report.** `scripts/mitosis-cost-report.py` prints the savings against an all-frontier run, can emit `ccusage`-shaped JSON and summarises routing from the debug logs (see "Measure it" above).

**Statusline (optional).** Shows per-session scout and worker counts, for example `mitosis: scout 3 (1 live) | worker 2 | Opus | $1.23`. Add to `~/.claude/settings.json`:

```json
{"statusLine": {"type": "command", "command": "python3 <plugin path>/scripts/mitosis-statusline.py"}}
```

`MITOSIS_STATUSLINE_PREFIX` changes the leading label; any problem prints `mitosis: idle`.

---

## Development & Verification

```bash
python3 -m unittest discover -s tests -v     # 307 tests (hooks, judge, cost report, routing eval, packaging, Codex hooks)
python3 tests/test_routing_eval.py          # prints the routing confusion matrix
agy plugin validate . --strict
python3 scripts/smoke-claude.py                       # headless Claude Code run: asserts the hook pinned haiku + effort low (a few cents)
python3 scripts/mitosis-cost-report.py --last 1       # token / USD ledger of the newest Claude Code session
```

### Single source (`spec/`)

Contributors: do not edit generated files by hand. `spec/models.toml` (models, efforts, prices), `spec/report.toml` (the four-header report contract) and `spec/agents/*.md` (shared agent bodies) are the single source. `python3 scripts/sync-generated.py` renders the Claude Code and Antigravity agent files, the Codex role and profile TOMLs, the report block in `rules/AGENTS.md` and both READMEs, `.agents/skills/orchestrate/SKILL.md` (a byte copy of the skill, which the Codex manifest ships through its `skills` entry) and `scripts/mitosis_generated.py`. `python3 scripts/sync-generated.py --check` runs in the test suite and fails when anything is stale.

### Evals

```bash
claude plugin eval . --trust-plugin --scaffold --runs 1 --ablation none --max-cost-usd 3 --allow-tools Bash Write Edit
```

This costs real tokens (about 0.6 USD per run on opus) and needs `bubblewrap` and `socat` on Linux.

See [CHANGELOG.md](./CHANGELOG.md) for release history.

---

## License

[MIT](./LICENSE) © 2026 ivanusto
