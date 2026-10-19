# Changelog

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
