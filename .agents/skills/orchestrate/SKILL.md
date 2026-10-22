---
name: orchestrate
description: >-
  Structured 4-phase orchestration workflow across 3 platforms (Google Antigravity agy, Anthropic Claude Code,
  OpenAI Codex CLI) that keeps the primary agent on a frontier reasoning model (Gemini 4 Argon, Claude Fable/Opus/Sonnet,
  gpt-6.1-sol / gpt-5.6-sol, o3, Grok, DeepSeek, Qwen) as architect/coordinator and dispatches subagents pinned to
  reconnaissance (flash_lite / haiku / gpt-5.6-luna) or execution (flash / sonnet / gpt-5.6-terra). Activate when the
  user invokes /orchestrate or requests multi-agent task decomposition.
---

# Mitosis Orchestrator Workflow (`/orchestrate`, v2.0.0)

Execute complex engineering tasks using a 4-phase hierarchical orchestration loop with **Mitosis Orchestrator** across **Google Antigravity (`agy`)**, **Anthropic Claude Code**, and **OpenAI Codex CLI**.

---

## 3-Platform Tier Mapping

| Phase / Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI |
| :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon` / Frontier | `fable` / `opus` / `sonnet` | `gpt-6.1-sol` / `gpt-5.6-sol` / `gpt-6-astra` / `o3` |
| **Phase 1: Reconnaissance** | `flash_lite` (`flash-lite-scout`) | `haiku` (`Agent` with `subagent_type: "mitosis-orchestrator:flash-lite-scout"`) | `gpt-5.6-luna` (`spawn_agent` role `explorer` (`flash-lite-scout` accepted as alias), or `codex exec --profile mitosis-scout`) |
| **Phase 3: Scoped Execution** | `flash` (`flash-worker`) | `sonnet` (`Agent` with `subagent_type: "mitosis-orchestrator:flash-worker"`) | `gpt-5.6-terra` (`spawn_agent` role `worker` (`flash-worker` accepted as alias), or `codex exec --profile mitosis-worker`) |

---

## Phase 1: Reconnaissance (Scout / Explorer Fan-Out)

When context must be gathered across the workspace before planning:
1. Identify independent lookups (file discovery, symbol search, config inspection, git/log status).
2. Fan out concurrent read-only subagents in a single invocation:
   - **Antigravity (`agy`)**: `invoke_subagent` with `TypeName: "flash-lite-scout"` and `"Model": "flash_lite"`.
   - **Claude Code**: `Agent` with `subagent_type: "mitosis-orchestrator:flash-lite-scout"` (the hook pins `model: haiku`, `effort: low`). Untyped `Agent` calls with `model: inherit` are classified by prompt; built-in `Explore` is left as-is.
   - **OpenAI Codex CLI**: `spawn_agent(agent_type="explorer")` (`flash-lite-scout` is accepted as an alias when scripts/setup-codex.py installed it) or `codex exec --profile mitosis-scout "<lookup>"` (`gpt-5.6-luna`, low effort, `read-only` sandbox).
   - Keep each prompt narrowly scoped and end it with the four-header bounded REPORT FORMAT from `rules/AGENTS.md`.
   - Scouts are strictly read-only; mutation tasks or web searches are directed to Phase 3 workers.
3. Dispatch, then keep going:
   - **Antigravity (`agy`)**: end the turn right after dispatching; the platform wakes you when scouts report.
   - **Claude Code**: subagents run in the background and you are notified as each one finishes. Keep working on the parts of Phase 2 that do not depend on their reports; never poll or redo their lookups yourself. For follow-ups, reuse a finished worker's context with `SendMessage` (`to:` its agent id) instead of spawning a new one; the hook still blocks edit or multi-step instructions sent to a scout.
   - **OpenAI Codex CLI**: `spawn_agent` returns when the role finishes; batch independent lookups in one turn.

---

## Phase 2: Architecture & Decomposition (Primary Mitosis Orchestrator)

Perform high-reasoning work directly on the primary Mitosis Orchestrator agent:
1. Synthesize the reconnaissance results from scouts/explorers.
2. Design the architecture, interfaces, invariants, and step-by-step execution plan.
3. Partition the implementation and verification into independent work units:
   - **Recon / Check**: Config/format validation, quick status checks, single-file read verification.
   - **Execution Worker**: Multi-file research, test authoring & execution, localized code changes, module debugging.
   - **Primary Orchestrator**: Cross-cutting core refactors, subtle concurrency/security logic, final integration.

---

## Phase 3: Scoped Execution & Testing (Workers)

1. Dispatch independent implementation or test work units:
   - **Antigravity (`agy`)**: `invoke_subagent` with `TypeName: "flash-worker"` and `"Model": "flash"`.
   - **Claude Code**: `Agent` with `subagent_type: "mitosis-orchestrator:flash-worker"` (the hook pins `model: sonnet`, `effort: medium`); use `isolation: "worktree"` for parallel branch work.
   - **OpenAI Codex CLI**: `spawn_agent(agent_type="worker")` (`flash-worker` is accepted as an alias when scripts/setup-codex.py installed it) or `codex exec --profile mitosis-worker "<task>"` (`gpt-5.6-terra`, medium effort, `workspace-write` sandbox).
   - Give every worker an explicit file allowlist, a verification command and the four-header bounded REPORT FORMAT.
   - Follow-up instructions go through `send_message` (agy) or `SendMessage` (Claude Code) to a worker that already holds the context; never send edit tasks to a scout.
2. For large programmatic fan-out, workers operate in parallel across disjoint files or branch worktrees.

---

## Phase 4: Synthesis & Verification (Primary Mitosis Orchestrator)

1. Inspect outputs, diffs, and test outputs produced by all workers.
2. Resolve any cross-module integration issues directly on the primary Mitosis Orchestrator agent.
3. Run final verification (or dispatch a worker verifier) and present a concise summary with clickable file and symbol links.
4. Terminate branch workers and clean up temporary worktrees.
