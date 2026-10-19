---
name: orchestrate
description: >-
  Structured 4-phase orchestration workflow across 3 platforms (Google Antigravity agy, Anthropic Claude Code,
  OpenAI Codex CLI 0.163.0) that pins the primary agent (Mitosis Orchestrator frontier reasoning models:
  Gemini 4 Argon, Claude Opus/Sonnet, gpt-5.4, gpt-6.1-sol, gpt-5.6-sol, gpt-5.3-codex, o3, Grok, DeepSeek, Qwen)
  as architect/coordinator and dispatches subagents pinned to reconnaissance (flash_lite / haiku / gpt-5.6-luna)
  or execution (flash / sonnet / codex-mini-latest). Activate when the user invokes /orchestrate or requests
  multi-agent task decomposition.
---

# Mitosis Orchestrator Workflow (`/orchestrate` — v1.2.0)

Execute complex engineering tasks using a 4-phase hierarchical orchestration loop with **Mitosis Orchestrator** across **Google Antigravity (`agy`)**, **Anthropic Claude Code**, and **OpenAI Codex CLI (`0.163.0`)**.

---

## 3-Platform Tier Mapping

| Phase / Tier | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI (`0.163.0`) |
| :--- | :--- | :--- | :--- |
| **Primary Master Nucleus** | `Gemini 4 Argon` / Frontier | `Claude Opus` / `Sonnet 3.7+` | **`gpt-5.4`** / **`gpt-6.1-sol`** / **`gpt-5.6-sol`** / **`gpt-5.3-codex`** / **`o3`** |
| **Phase 1: Reconnaissance** | `flash_lite` (`flash-lite-scout`) | `haiku` | **`gpt-5.6-luna`** (`explorer` / `flash-lite-scout`, `model_reasoning_effort="low"`, `read-only`) |
| **Phase 3: Scoped Execution** | `flash` (`flash-worker`) | `sonnet` | **`codex-mini-latest`** (`worker` / `flash-worker`, `model_reasoning_effort="medium"`, `workspace-write`) |

---

## Phase 1: Reconnaissance (Scout / Explorer Fan-Out)

When context must be gathered across the workspace before planning:
1. Identify independent lookups (file discovery, symbol search, config inspection, git/log status).
2. Fan out concurrent read-only subagents in a single invocation:
   - **Antigravity (`agy`)**: `invoke_subagent` with `TypeName: "flash-lite-scout"` and `"Model": "flash_lite"`.
   - **Claude Code**: Dispatch reconnaissance queries to `haiku`.
   - **OpenAI Codex CLI**: Dispatch via `explorer` or `flash-lite-scout` (`model = "gpt-5.6-luna"`, `model_reasoning_effort = "low"`, `sandbox_mode = "read-only"`).
   - Keep each prompt narrowly scoped and end it with the four-header bounded REPORT FORMAT from `rules/AGENTS.md`.
   - Scouts are strictly read-only; mutation tasks or web searches are directed to Phase 3 workers.
3. End turn after dispatching. The platform wakes you when scouts report.

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
   - **Claude Code**: Dispatch execution tasks to `sonnet`.
   - **OpenAI Codex CLI**: Dispatch via `worker` or `flash-worker` (`model = "codex-mini-latest"`, `model_reasoning_effort = "medium"`, `sandbox_mode = "workspace-write"`).
   - Give every worker an explicit file allowlist, a verification command and the four-header bounded REPORT FORMAT.
   - Follow-up instructions go through `send_message`; never send edit tasks to a scout.
2. For large programmatic fan-out, workers operate in parallel across disjoint files or branch worktrees.

---

## Phase 4: Synthesis & Verification (Primary Mitosis Orchestrator)

1. Inspect outputs, diffs, and test outputs produced by all workers.
2. Resolve any cross-module integration issues directly on the primary Mitosis Orchestrator agent.
3. Run final verification (or dispatch a worker verifier) and present a concise summary with clickable file and symbol links.
4. Terminate branch workers and clean up temporary worktrees.
