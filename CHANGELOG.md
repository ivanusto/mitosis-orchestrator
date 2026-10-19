# Changelog

## 1.0.0 — 2026-10-19

### Added
- **Mitosis Orchestrator Initial Launch**: Rebranded and evolved from `argon-orchestrator` v1.4.0 into `mitosis-orchestrator` as a universal multi-model frontier orchestrator plugin for Antigravity (`agy`).
- **Dual Environment Variable Support**: Introduced native `AGY_MITOSIS_*` configuration variables (`AGY_MITOSIS_PIN_SETTINGS`, `AGY_MITOSIS_PRIMARY_MODEL`, `AGY_ALLOW_NON_MITOSIS_PRIMARY`, `AGY_MITOSIS_AUTO_ALLOW`, `AGY_MITOSIS_MAX_DIRECT_EDITS`, `AGY_MITOSIS_ALLOW_DIRECT_LOGS`, `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH`, `AGY_MITOSIS_CIRCUIT_BREAKER`, `AGY_MITOSIS_DEBUG`) with full backwards compatibility for legacy `AGY_ARGON_*` environment variables.
- **Expanded Frontier Model Support**: Added full support for frontier primary orchestrator models across all major AI providers:
  - **Gemini**: Gemini 4 Argon (`High` / `Medium`), Gemini Pro, Gemini Ultra
  - **Claude**: Claude Opus, Claude Sonnet (e.g., Claude 3.5 Sonnet, Claude 3.7 Sonnet)
  - **xAI**: Grok (`grok-3`, `grok-4`)
  - **OpenAI**: GPT-4, GPT-5, reasoning series (`o1`, `o3`, `o4`)
  - **DeepSeek**: DeepSeek-V3, DeepSeek Reasoner (`deepseek-reasoner`, `deepseek-r1`)
  - **Qwen**: Qwen 2.5, Qwen Max (`qwen-max`, `qwen-plus`, `qwq-32b`)
- **Small-Tier Model Filtering**: Regex-based rejection (`\b(flash|lite|mini|nano|haiku)\b`) prevents small-tier models (such as `grok-3-mini`, `gpt-4o-mini`, `gpt-4.1-nano`, `o3-mini`, `o4-mini`, `claude-3-5-haiku-20241022`, `gemini-3-flash`, `gemini-flash-lite`, `flash_lite`, `qwen-lite`) from acting as primary orchestrators.
- **Diagnostic Logging**: Hook diagnostic log path updated to `<artifactDirectoryPath>/mitosis_orchestrator_debug.log`.

## 1.4.0 (argon-orchestrator) — 2026-10-19

### Added
- **Frontier Model Support for Primary Orchestrator**: Expanded `is_argon_tier` (aliased as `is_frontier_tier`) to support Claude (Opus / Sonnet), Gemini Pro / Ultra, OpenAI frontier/reasoning models (GPT-4, GPT-5, o1, o3, o4), DeepSeek, and Grok alongside Gemini 4 Argon (`High` / `Medium`).
- **Small-Tier Rejection Guard**: Word-boundary regex filtering (`\b(flash|lite|mini|nano|haiku)\b`) rejects small-tier models (such as Claude Haiku, GPT-4o mini, o3-mini) from being accidentally treated as primary orchestrators while correctly distinguishing `gemini`.
- **Flexible Primary Model Pinning**: `check_primary_model_settings()` now honors any valid Argon or frontier model specified via `AGY_ARGON_PRIMARY_MODEL` (e.g., `Claude 3.7 Sonnet`) when `AGY_ARGON_PIN_SETTINGS=1` is opted in.

## 1.3.0 — 2026-10-19

### Fixed
- **`PreToolUse` hook no longer returns empty `{}` (which Antigravity treated as an unapproved tool denial).** When `AGY_ARGON_AUTO_ALLOW` is unset, when a tool has no path arguments (`read_url_content`, `search_web`, browser/MCP tools), or on fallback/error, it now returns an explicit pass-through `{"decision": "allow", "reason": "Argon Orchestrator: pass-through."}`.

### Added
- **Context Hygiene Guard**: Blocks direct inspection of `.log` or `transcript.jsonl` / `transcript_full.jsonl` files by the primary agent in `PreToolUse` (unless `AGY_ARGON_ALLOW_DIRECT_LOGS=1`), delegating log slicing to `flash-lite-scout`.
- **Fast-Path Direct Execution Budget**: Tracks direct workspace file mutations in `turn_edited_files`, capping the primary agent at `AGY_ARGON_MAX_DIRECT_EDITS` (default 3 distinct workspace files in one turn) and blocking direct workspace edit followed by test runner execution in the same turn (unless `AGY_ARGON_ALLOW_DIRECT_FASTPATH=1`), delegating code editing and test execution loops to `flash-worker`.
- **Two-Failure Circuit Breaker**: Tracks consecutive failures across `run_command` and `invoke_subagent` in `PostToolUse` (matcher updated to `invoke_subagent|manage_subagents|run_command`). Trips after 2 consecutive failures and denies further commands or subagent invocations in `PreToolUse` (unless `AGY_ARGON_CIRCUIT_BREAKER=0`).
- **Per-Turn Tracking Reset**: `PreInvocation` automatically resets `turn_edited_files` and `consecutive_failures` on each turn.
- **Bounded 4-Header Subagent Report Contract**: Expanded the subagent report format across rules and agents to support up to 10 compact bullet lines (`- ` or `* `) under `NOTES:` (total length $\le 1,600$ characters) and mandatory `file://` scratch log URI on `BLOCKED` or `FAILED`. Added `validate_subagent_report(text)` validation helper in `enforce-orchestrator.py`.

## 1.2.0 — 2026-10-16

### Fixed
- **Classifier misrouted heavy work to `flash_lite`.** A single lite keyword such as `find` could override strong signals (`test`, `trace`, `call graph`, `refactor`). Strong signals now always win, and mutation prompts never stay on `flash_lite` regardless of `Workspace`.
- **Over-broad `flash_lite` vocabulary.** Bare words `find`, `ls`, `rg`, `count`, `version`, `format`, `status`, `check if` were removed; only concrete lookup phrases remain.

### Changed
- **`SessionStart` no longer rewrites `settings.json` by default.** It warns when the primary model is not Argon. Set `AGY_ARGON_PIN_SETTINGS=1` to restore the 1.1.x rewrite behaviour.
- `flash-lite-scout` lost `search_web`, `read_url_content` and `schedule`; web research is `flash-worker` work per the routing matrix. Both agents now return a fixed four-line report (`STATUS / FILES_CHANGED / VERIFICATION / NOTES`).
- `rules/AGENTS.md` gained the subagent prompt contract, `send_message` rules and branch-cleanup rules.

### Added
- **`PreInvocation` hook**: checks the live session model every turn and warns once per invocation if it is not Argon/Pro-tier. Catches model switches made in the UI after session start.
- **`send_message` guard**: denies mutation or multi-step (>3 numbered rules) instructions sent to a known `flash_lite` recipient.
- **`run_workflow` guard**: denies scripts whose `agent(...)` calls omit `model="flash"` / `model="flash_lite"`.
- **`define_subagent` guard**: forces `enable_subagent_tools=false` (no nested spawning).
- **Branch worktree lifecycle tracking** via `PostToolUse` + `Stop`: pending spawns are paired with the per-subagent metadata agy writes under `<artifactDirectoryPath>/.system_generated/subagents/` (type, role, live state, worktree URI), with the transcript as fallback; the `Stop` hook asks once per set of live `Workspace:"branch"` workers to merge/discard and kill them (agy bumps `executionNum` after every `continue`, so per-execution dedup would nag on every attempt).
- A `flash-lite-scout` invoked with a mutation prompt is upgraded to `flash-worker` instead of failing for lack of write tools.
- **Workspace scope guard (opt-in, `AGY_ARGON_AUTO_ALLOW=1`)**: the `PreToolUse` matcher is now `*`. `run_command` and file tools that stay inside `workspacePaths` / `artifactDirectoryPath` are answered `allow` (with a `permissionOverrides` entry for the exact command); paths outside the workspace, privilege escalation, recursive deletes of a root, system-altering commands, force pushes and downloads piped to a shell are answered `ask`; tools without path arguments get no opinion. Unknown tools and hook errors now return `{}` instead of `allow`. File tools carry a matching `read_file(<path>)` / `write_file(<path>)` override. Interactive sessions only (verified on agy 1.4.0: no prompts for in-workspace edits and commands); headless `agy -p` soft-denies prompt-requiring tools regardless of hook output.
- `AGY_ARGON_DEBUG=1` writes one JSON line per hook call for diagnosing payloads.
- `tests/test_enforce_orchestrator.py`: 27 unit tests, including a table of prompt → tier cases (the first three rows are the 1.1.0 misroutes).

## 1.1.0

- Support both Gemini 4 Argon (High) and (Medium) automatically as primary orchestrator.

## 1.0.0

- Initial release: Argon primary + flash/flash_lite subagents.
