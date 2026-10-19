# Changelog

## 1.0.0 — 2026-10-19

Initial release of **Mitosis Orchestrator (mitosis-orchestrator)** for Google Antigravity (gy).

### Added
- **Mitotic Cross-Model Orchestration Architecture**: Treats any supported frontier reasoning model as the central **Master Nucleus (Primary Orchestrator)** and divides subagent workloads into lash (lash-worker for multi-file research, testing, and scoped code edits) and lash_lite (lash-lite-scout for fast, single-hop read-only reconnaissance).
- **Universal Frontier Primary Model Support**:
  - **xAI**: Grok 3, Grok 4, grok-3, grok-4, grok-3-thinking
  - **Anthropic**: Claude Opus, Claude Sonnet (Claude 3.5 / 3.7 / 4 Sonnet & Opus)
  - **Google Gemini**: Gemini 4 Argon (High / Medium), Gemini Pro, Gemini Ultra
  - **OpenAI**: GPT-4 (gpt-4o, gpt-4.1, gpt-4.5), GPT-5, o1, o3, o4
  - **DeepSeek**: DeepSeek-R1, DeepSeek-V3, deepseek-reasoner, deepseek-chat
  - **Alibaba Qwen**: Qwen Max, Qwen Plus, Qwen 2.5 / 3, QwQ (qwq-32b)
- **Strict Small-Tier Filtering**: Lookaround boundary regex ((?<![a-z0-9])(flash|lite|mini|nano|haiku)(?![a-z0-9])) prevents lightweight models (grok-3-mini, gpt-4o-mini, gpt-4.1-nano, o1-mini, o3-mini, o4-mini, claude-3-5-haiku, gemini-3-flash, lash_lite, qwen-lite) from acting as primary orchestrators while accurately preserving gemini.
- **Lifecycle Enforcement Hooks (hooks.json & scripts/enforce-orchestrator.py)**:
  - SessionStart & PreInvocation primary frontier model checks and per-turn state resets.
  - PreToolUse context-aware subagent classifier (invoke_subagent), send_message scout protection, un_workflow model enforcement, and define_subagent nesting prevention.
  - **Context Hygiene Guard**: Blocks primary agent from directly reading raw .log or 	ranscript.jsonl files, delegating log slicing to lash-lite-scout.
  - **Fast-Path Direct Edit Budget**: Caps direct primary-agent workspace edits at 3 distinct files per turn and blocks direct workspace edit + test-runner loops on the primary agent.
  - **Two-Failure Circuit Breaker**: Halts runaway retry loops after 2 consecutive failures across un_command or invoke_subagent.
  - **Workspace Scope Guard**: Opt-in (AGY_MITOSIS_AUTO_ALLOW=1) auto-approval for in-workspace file and command tools with explicit pass-through llow fallback.
  - PostToolUse subagent metadata pairing and Stop branch worktree cleanup reminders.
- **Bounded 4-Header Subagent Report Contract**: Enforces compact STATUS / FILES_CHANGED / VERIFICATION / NOTES reports ($\le 1,600$ chars, up to 10 bullets, mandatory ile://.../*.log URI on BLOCKED or FAILED).
- **Native AGY_MITOSIS_* Environment Variables**: Full support for AGY_MITOSIS_* configuration variables with 100% backwards-compatible fallback to AGY_ARGON_*.
- **Brand Assets & Test Suite**: Mitotic cell-division SVG logo (ssets/logo.svg), bilingual documentation (README.md, README.zh-TW.md), and 73 unit tests (	ests/test_enforce_orchestrator.py).
