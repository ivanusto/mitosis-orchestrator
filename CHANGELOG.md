# Changelog

## 1.4.0 — 2026-10-19

### Added (Claude Code)
- **`SendMessage` guard**: follow-ups sent to a `flash-lite-scout` that contain mutation verbs or more than 3 numbered steps are denied, as `send_message` already was on agy. Scout recipients are recognised by agent id: `PostToolUse` pairs the `agentId` in the `Agent` tool result with the pending spawn, and `SubagentStop` pairs by `agent_id` / `agent_type` as a fallback.
- **Four-header report enforcement**: `SubagentStop` validates the final message of this plugin's own subagents and, once per agent, blocks the stop with the exact report shape so the agent rewrites it (`CLAUDE_MITOSIS_ENFORCE_REPORT=0` disables; `stop_hook_active` and third-party agent types are never blocked). The validator now accepts a bare `NOTES:` line followed by bullets and ignores code fences.
- **`scripts/smoke-claude.py`**: headless end-to-end check that runs `claude -p` on a throwaway project, asserts from the hook debug log that the scout was pinned to `haiku` / `effort low`, confirms a haiku model ran from Claude Code's own usage output, and prints the cost report for the session.

### Fixed
- Claude Code `Stop` and `SubagentStop` payloads carry `last_assistant_message`, which the Codex payload detector treated as a Codex marker, so both events ran the Codex code path on Claude Code (the branch-worktree Stop reminder never fired there). A Claude `hook_event_name` now settles platform detection first.

### Changed
- `hooks/hooks.json` `PreToolUse` matcher narrowed from `*` to `Agent|Task|Bash|Read|NotebookRead|Write|Edit|NotebookEdit|SendMessage`; other tools (`Grep`, `Glob`, `WebFetch`, MCP) no longer spawn the hook. The opt-in workspace guard therefore only auto-approves those tools.
- `skills/orchestrate/SKILL.md` and `rules/AGENTS.md`: "end the turn after dispatching" is agy behaviour; on Claude Code subagents run in the background, the primary keeps working on independent planning, and follow-ups reuse a worker's context through `SendMessage` instead of re-spawning.
- 170 tests (was 159).

## 1.3.2 — 2026-10-19

### Added
- **Effort pinning on Claude Code**: the `PreToolUse` hook now sets `effort` alongside `model` on every `Agent` call it rewrites (scouts `low`, workers `medium`, mirroring the Codex roles). A valid caller-supplied `effort` is honoured; `CLAUDE_MITOSIS_PIN_EFFORT=0` turns it off; `CLAUDE_MITOSIS_SCOUT_EFFORT` / `CLAUDE_MITOSIS_WORKER_EFFORT` override per tier.
- **Per-tier model overrides on Claude Code**: `CLAUDE_MITOSIS_SCOUT_MODEL` / `CLAUDE_MITOSIS_WORKER_MODEL` (`haiku`, `sonnet`, `opus`, `fable`), e.g. `opus` workers under a `fable` primary. The session banner, delegation hints and deny messages reflect the effective models and efforts.
- **`scripts/mitosis-cost-report.py`**: token and USD ledger for Claude Code sessions. Reads `<project>/<session>.jsonl` and `<session>/subagents/agent-*.jsonl`, de-duplicates streamed usage by message id, aggregates per model, splits primary from subagents, prints duration, primary share and a side-by-side comparison for several sessions (`--last N`, `--session`, `--json`, `--prices`, `--primary-cache-ttl` / `--subagent-cache-ttl`). With the defaults (primary 1-hour cache writes, subagents 5-minute) it reproduces Claude Code's own `--output-format json` cost figures to the cent.

### Fixed
- The edit-then-test deny message on Claude Code / Codex now names the platform's delegation primitive instead of agy's `TypeName=` syntax.

### Docs
- README / README.zh-TW: new section on choosing the primary model on Claude Code (Fable 5.1 vs Opus 5.5): list prices, latency and capability differences, a recommendation table, an illustrative cost example, subscription-plan note, and how to measure with the cost report.
- 159 tests (was 144): effort / model pinning and the cost report.

## 1.3.1 — 2026-10-19

### Fixed (found by an end-to-end `/orchestrate` run on Claude Code 2.1.303)
- **Negated mutation verbs no longer upgrade scouts**: prompts ending in "Do not edit anything" (the wording the orchestrator naturally produces) matched the mutation regex, so every `flash-lite-scout` was silently promoted to a `sonnet` worker. Negated phrases (`do not / never / without / no ... edit|modify|delete|...`) are stripped before classification; real mutation requests still upgrade.
- **Subagent tool calls are no longer treated as the primary agent**: Claude Code adds `agent_id` / `agent_type` to hook input for calls made inside a subagent. Those calls now bypass the primary-only guards (edit budget, edit-then-test block, log hygiene), so a worker can edit and then run the test suite; its edits no longer count against the primary agent's budget either.
- 144 tests.

## 1.3.0 — 2026-10-19

### Fixed
- **Claude Code install path**: the documented `claude plugin add` command does not exist. The repo now ships `.claude-plugin/marketplace.json`, so it installs with `claude plugin marketplace add ivanusto/mitosis-orchestrator` + `claude plugin install mitosis-orchestrator@mitosis-orchestrator` (or `claude --plugin-dir` for a single session).
- **Claude Code agents never loaded**: `.claude-plugin/plugin.json` pointed `agents` at a directory, which the manifest schema rejects; it now lists the two `.md` files explicitly. Removed the non-existent `LS` / `MultiEdit` tools from the agent frontmatter and narrowed the `PostToolUse` matcher to `Agent|Bash`.
- **Claude 5 family recognised as frontier**: `fable` / `mythos` were classified as non-frontier, so every session and prompt emitted a "switch model" warning. Default Claude pin target is now `opus` (was `Claude 3.7 Sonnet`).
- **Claude Code `Agent` rewrite scoped down**: only this plugin's `flash-worker` / `flash-lite-scout` types (with or without the `mitosis-orchestrator:` prefix) and untyped `model: inherit` calls are pinned. Other plugins' agents, built-in types (`Explore`, `Plan`, `general-purpose`, …) and untyped calls with an explicit model pass through unchanged. New `CLAUDE_MITOSIS_PIN_SUBAGENTS=0` switch. Worktree spawns are still tracked for the Stop reminder.
- **Platform-specific guidance text**: deny messages on Claude Code / Codex now name the right delegation primitive (`Agent` + `subagent_type`, `spawn_agent` + `agent_type`) instead of agy's `TypeName=` / `Model=` syntax.
- **Codex `ask` decision**: Codex hooks only honour allow/deny, so the workspace guard now returns `deny` instead of an unsupported `ask`.

### Changed (OpenAI Codex CLI, verified against 0.154.0 docs/binary and the 0.163.0 docs)
- `[profiles.*]` tables are no longer honoured by Codex (removed in 0.134.0). `codex-config/config.toml.snippet` is replaced by `codex-config/mitosis-scout.config.toml` and `mitosis-worker.config.toml`, installed to `$CODEX_HOME/<name>.config.toml` for `codex exec --profile <name>`.
- Agent role TOMLs gained the required `name` field and dropped `sandbox_mode` (ignored in role files; sandboxing is set by the parent session or profile).
- Default Codex models: primary `gpt-6.1-sol`, worker `gpt-5.6-terra`, scout `gpt-5.6-luna`. `codex-mini-latest`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.3-codex-spark` are retired or not shipped by current Codex builds.
- `scripts/setup-codex.py` uses the running interpreter (`sys.executable`) and this repo's own `scripts/mitosis-mcp-server.py` path for `[mcp_servers.mitosis]` instead of a hard-coded `python` + `~/.gemini/...` / Windows path; it also installs the profile files and validates role `name`s.
- `scripts/mitosis-mcp-server.py` runs scouts with `--sandbox read-only` and workers with `--sandbox workspace-write`; `--dangerously-bypass-approvals-and-sandbox` is opt-in via `MITOSIS_CODEX_YOLO=1`. Dropped the hard-coded Windows `codex.exe` fallback path.

### Docs
- README / README.zh-TW rewritten: per-platform install + verification steps, agy conflict note for the predecessor `argon-orchestrator`, Claude 5 and current Codex model tables, unified `*_MITOSIS_*` env table. Chinese docs use 尖端模型 for "frontier model".
- 141 unit tests (was 127): fable tier, third-party / built-in agent pass-through, explicit model honouring, pin switch, Claude-specific deny wording, Codex sandbox flags, `setup-codex.py` end-to-end into a temp `$CODEX_HOME`.

## 1.2.0 — 2026-10-19

### Added
- **Unified 3-Platform Architecture**: Full cross-platform support across **Google Antigravity (`agy`)**, **Anthropic Claude Code**, and **OpenAI Codex CLI (`0.163.0`)**.
- **OpenAI Codex CLI Integration & Verified Models**:
  - Frontier Primary Orchestrator models: `gpt-5.4`, `gpt-6.1-sol`, `gpt-5.6-sol`, `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.5`, `o1`, `o3`, `o4`.
  - Execution Workers (`worker` / `flash-worker`): `codex-mini-latest` (default), `gpt-5.4-mini` (`sandbox_mode = "workspace-write"`).
  - Reconnaissance Scouts (`explorer` / `flash-lite-scout`): `gpt-5.6-luna` (default), `gpt-5.3-codex-spark`, `gpt-5.4-mini` (`sandbox_mode = "read-only"`, `model_reasoning_effort = "low"`).
- **Codex Native Agent TOML Roles (`codex-agents/`)**:
  - `codex-agents/explorer.toml` & `codex-agents/flash-lite-scout.toml` for low-latency read-only reconnaissance.
  - `codex-agents/worker.toml` & `codex-agents/flash-worker.toml` for scoped execution, test authoring/running, and code edits.
  - Both role definitions strictly enforce the bounded 4-header report contract (`STATUS`, `FILES_CHANGED`, `VERIFICATION`, `NOTES`).
- **Codex CLI Configuration Snippet & Auto-Mount Tooling**:
  - `codex-config/config.toml.snippet`: Standalone configuration profiles (`[profiles.mitosis-scout]`, `[profiles.mitosis-worker]`) and MCP server (`[mcp_servers.mitosis]`).
  - `scripts/setup-codex.py`: Automated idempotent installer that non-destructively backs up `~/.codex/config.toml` to `config.toml.bak`, synchronizes `rules/AGENTS.md` to `~/.codex/AGENTS.md`, installs agent roles into `~/.codex/agents/`, and appends profiles to `~/.codex/config.toml`.
- **Claude Code Plugin Manifest**:
  - Added `.claude-plugin/plugin.json` for Claude Code plugin compatibility.
- **Documentation & Manifests**:
  - Bumped `plugin.json` to `v1.2.0`.
  - Updated `rules/AGENTS.md`, `skills/orchestrate/SKILL.md`, `README.md`, and `README.zh-TW.md` with complete 3-platform routing matrices and configuration guides.

## 1.0.0 — 2026-10-19

Initial release of **Mitosis Orchestrator (mitosis-orchestrator)** for Google Antigravity (agy).

### Added
- **Mitotic Cross-Model Orchestration Architecture**: Treats any supported frontier reasoning model as the central **Master Nucleus (Primary Orchestrator)** and divides subagent workloads into flash (flash-worker for multi-file research, testing, and scoped code edits) and flash_lite (flash-lite-scout for fast, single-hop read-only reconnaissance).
- **Universal Frontier Primary Model Support**:
  - **xAI**: Grok 3, Grok 4, grok-3, grok-4, grok-3-thinking
  - **Anthropic**: Claude Opus, Claude Sonnet (Claude 3.5 / 3.7 / 4 Sonnet & Opus)
  - **Google Gemini**: Gemini 4 Argon (High / Medium), Gemini Pro, Gemini Ultra
  - **OpenAI**: GPT-4 (gpt-4o, gpt-4.1, gpt-4.5), GPT-5, o1, o3, o4
  - **DeepSeek**: DeepSeek-R1, DeepSeek-V3, deepseek-reasoner, deepseek-chat
  - **Alibaba Qwen**: Qwen Max, Qwen Plus, Qwen 2.5 / 3, QwQ (qwq-32b)
- **Strict Small-Tier Filtering**: Lookaround boundary regex prevents lightweight models (grok-3-mini, gpt-4o-mini, gpt-4.1-nano, o1-mini, o3-mini, o4-mini, claude-3-5-haiku, gemini-3-flash, flash_lite, qwen-lite) from acting as primary orchestrators while accurately preserving gemini.
- **Lifecycle Enforcement Hooks (hooks.json & scripts/enforce-orchestrator.py)**:
  - SessionStart & PreInvocation primary frontier model checks and per-turn state resets.
  - PreToolUse context-aware subagent classifier (invoke_subagent), send_message scout protection, run_workflow model enforcement, and define_subagent nesting prevention.
  - **Context Hygiene Guard**: Blocks primary agent from directly reading raw .log or transcript.jsonl files, delegating log slicing to flash-lite-scout.
  - **Fast-Path Direct Edit Budget**: Caps direct primary-agent workspace edits at 3 distinct files per turn and blocks direct workspace edit + test-runner loops on the primary agent.
  - **Two-Failure Circuit Breaker**: Halts runaway retry loops after 2 consecutive failures across run_command or invoke_subagent.
  - **Workspace Scope Guard**: Opt-in (AGY_MITOSIS_AUTO_ALLOW=1) auto-approval for in-workspace file and command tools with explicit pass-through allow fallback.
  - PostToolUse subagent metadata pairing and Stop branch worktree cleanup reminders.
- **Bounded 4-Header Subagent Report Contract**: Enforces compact STATUS / FILES_CHANGED / VERIFICATION / NOTES reports (<= 1,600 chars, up to 10 bullets, mandatory file://.../*.log URI on BLOCKED or FAILED).
- **Native AGY_MITOSIS_* Environment Variables**: Full support for AGY_MITOSIS_* configuration variables with 100% backwards-compatible fallback to AGY_ARGON_*.
- **Brand Assets & Test Suite**: Mitotic cell-division SVG logo (assets/logo.svg), bilingual documentation (README.md, README.zh-TW.md), and 73 unit tests (tests/test_enforce_orchestrator.py).
