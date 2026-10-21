# Changelog

## 1.5.1 — 2026-10-22

### Security
- **Claude Code permission prompts are no longer skipped.** With `*_MITOSIS_AUTO_ALLOW` unset (the default), the `PreToolUse` hook answered every `Bash`, `Read`, `Write`, `Edit` and `SendMessage` call with `permissionDecision: "allow"`, which Claude Code treats as "skip the permission prompt". It now returns `{}` (no verdict), so the user's own rules and prompts apply. A hook crash also returns `{}` instead of `allow`. `AUTO_ALLOW=1` keeps its documented in-workspace auto-approval. **Behaviour change:** sessions that ran without prompts only because of this bug will now ask.
- The Claude scout gains `Bash` for `git status` / `git log` checks, which `rules/AGENTS.md` already assigned to it. The hook denies any scout command outside a read-only allowlist (`git` read subcommands, `ls`, `rg`, `wc`, `tail`, ...), any `>` redirection and any `$(...)`.
- Judge: the bearer key (`MITOSIS_JUDGE_KEY`) is sent only over https or to loopback / private-network hosts (`MITOSIS_JUDGE_ALLOW_HTTP_KEY=1` lifts this); non-http(s) URLs are refused; redaction also masks JWTs, AWS access key ids and `user:pass@` in URLs.
- State files: the session id is reduced to `[A-Za-z0-9_-]` before it becomes a path, and each writer uses its own temp file, so parallel hooks can no longer rename each other's half-written state into place.

### Fixed (routing)
- Chinese change requests (修改, 刪除, 重構, 改名, 升到, 加上, 幫 X 加 ...) routed to the scout because the vocabulary was English-only; Chinese heavy work (執行測試, 調查, 除錯, 跨模組) and lookups (在哪裡, 列出, 有沒有) are now recognised, with negations (不要修改, 請勿刪除) and noun uses (被修改的, 修正版, 安裝說明) excluded.
- Everyday English imperatives at the start of a clause (add, bump, commit, push, install, set, change, update, write, ...) and `git commit/push/...` count as change requests; "run the test suite" upgrades a scout-typed call, since the scout cannot run it.
- Mutation and heavy keywords used as nouns or paths no longer count: "the patch version", "the bug report template", "where is the test for x", "refactor/".
- "Do not modify anything. Fix the typo" now keeps the fix at full confidence: an imperative after a read-only declaration wins. A keyword inside the declared scope ("Read-only: summarize fixes") still lowers confidence.
- `is_frontier_tier` matches whole name segments, so "proxy", "product-o1x" and sub-30B checkpoints such as `deepseek-coder-1.3b` are not frontier.

### Fixed (other)
- MCP server: invalid JSON gets a `-32700` reply and a non-object a `-32600` reply instead of silence; a handler exception returns `-32603` instead of ending the server; the prompt follows `--` so one starting with `-` is not read as a `codex exec -m gpt-5.6-luna` option.
- `setup-codex.py` no longer overwrites the first `config.toml.bak` (later runs write `.bak-YYYYmmdd-HHMMSS`), backs up a different `~/.codex/AGENTS.md` before replacing it, and writes `config.toml` atomically.
- `SKILL.md` and `rules/AGENTS.md` said v1.4.1.

### Added
- `tests/routing_cases.jsonl` (120 labelled prompts, half Chinese) and `tests/test_routing_eval.py`: no task that needs a worker may reach a scout, accuracy at least 95 %. `python3 tests/test_routing_eval.py` prints the confusion matrices. 1.5.0 rules: 73/120 with 35 worker tasks sent to the scout; 1.5.1: 119/120 with none. The rules were tuned on this set, so it is a regression check, not an accuracy figure.
- `tests/routing_holdout.jsonl`: 40 prompts written without seeing the rules or the first set. 1.5.0 rules: 21/40 with 11 worker tasks sent to the scout. First blind run of the 1.5.1 rules: 28/40 with 4. After adding soft requests ("should return None instead of raising", 改用, 整理成 docs/x.md): 32/40 with none; the remaining 8 errors send read-only questions that mention tests, builds or DEBUG to a worker, which costs more but does not fail. The test only requires zero worker tasks on the scout.
- 229 tests (was 199).

## 1.5.0 — 2026-10-20

### Added
- **`Decision` routing record**: `decide_subagent` returns `tier`, `type_override`, `effort`, `confidence`, `source`, `rule` and `reason`; `classify_subagent` / `classify_subagent_tier` keep their old return shapes. Each rule carries a prior confidence (`RULE_CONFIDENCE`): explicit types, workspace and models 0.95, mutation 0.95, heavy 0.9, lite 0.85, prompt-length fallbacks 0.5. The debug log records every decision under `decisions`, and the Codex MCP server reports `confidence` / `decision_source` per task.
- **Read-only declarations**: prompts that declare themselves read-only (`read-only`, `do not modify anything`, `唯讀`) drop mutation and heavy hits to 0.6.
- **Optional judge** (`scripts/mitosis_judge.py`, off by default): with `MITOSIS_JUDGE_URL` set, decisions below `MITOSIS_JUDGE_THRESHOLD` (0.9) go to a Jev-compatible endpoint (TypeSafe Jev, a self-hosted gateway, Ollaya). The prompt is cut to 2000 characters and secrets, e-mail and IP addresses and home directories are masked. An unsure, slow (`MITOSIS_JUDGE_TIMEOUT`, 1.5 s) or failing judge falls back to `flash`. Typed, workspace and explicit-model decisions are never judged; a judge may veto a scout's mutation upgrade.

### Fixed
- Negated mutation phrases with coordinated verbs or slash-joined words (`Do NOT git fetch/pull or modify`, `never delete or rename`) were read as edit requests. `don't X and fix Y` still counts as a fix.
- Codex `mitosis_scout` matched mutation verbs without stripping negations, so "Do not edit anything" upgraded the scout to a worker.
- The MCP server reported `SERVER_VERSION` 1.3.0.

### Notes
- Measured on 66 real subagent prompts from local sessions: with the judge off, no tier or type changes against 1.4.1; plugin-typed calls never reached the judge threshold.
- 199 tests (was 176).

## 1.4.1 — 2026-10-19

### Added
- **REPORT FORMAT auto-append**: when a subagent prompt has no `REPORT FORMAT` block, the hook appends the tier's four-header block (scout variant fixes `FILES_CHANGED: NONE`) on both agy `invoke_subagent` and Claude Code `Agent` calls, so a forgetful primary no longer costs a SubagentStop round trip. Prompts that already carry all four canonical headers are left untouched; a prompt whose primary invented its own `REPORT FORMAT:` headers (seen live: `STATUS: found` / `FILES` / `EVIDENCE`) gets the canonical block appended with a line saying it supersedes the earlier one. `*_MITOSIS_APPEND_REPORT_FORMAT=0` disables.
- `scripts/smoke-claude.py` now also asserts the block was appended and that SubagentStop never had to send the scout back.
- 176 tests (was 170).

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
