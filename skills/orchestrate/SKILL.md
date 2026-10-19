---
name: orchestrate
description: >-
  Structured 4-phase orchestration workflow that pins the primary agent (Mitosis Orchestrator frontier model:
  Gemini 4 Argon, Claude Opus/Sonnet, GPT, Grok, DeepSeek, Qwen) as architect/coordinator and dispatches subagents
  pinned to flash_lite / haiku (for fast reconnaissance, searches, log checks, and single-file inspection) or flash / sonnet (for
  multi-file analysis, test runs, and scoped code edits). Activate when the user invokes /orchestrate or requests
  multi-agent task decomposition across Antigravity (agy) or Claude Code.
---

# Mitosis Orchestrator Workflow (`/orchestrate`)

Execute complex engineering tasks using a 4-phase hierarchical orchestration loop with **Mitosis Orchestrator** (frontier model: Gemini 4 Argon, Claude Opus/Sonnet, GPT, Grok, DeepSeek, Qwen) as the primary agent and context-pinned **`flash_lite` / `haiku`** (scout) and **`flash` / `sonnet`** (worker) subagents across **Antigravity (`agy`)** and **Claude Code**.

---

## Phase 1: Reconnaissance (`flash_lite` / `haiku` Fan-Out)

When context must be gathered across the codebase before planning:
1. Identify independent lookups (file discovery, symbol search, config inspection, git/log status).
2. Fan out concurrent reconnaissance subagents in a single turn:
   - **Antigravity (`agy`)**: `invoke_subagent` with `TypeName: "flash-lite-scout"` and `"Model": "flash_lite"`.
   - **Claude Code**: `Agent` or `Task` with `subagent_type: "flash-lite-scout"` and `model: "haiku"`.
   - Keep each prompt narrowly scoped and end it with the four-header bounded REPORT FORMAT from `rules/AGENTS.md`.
   - Scouts are read-only and have no write or web tools; any task requiring edits or external search goes to Phase 3.
3. End your turn after the subagent call. The platform wakes you when the scouts report.

---

## Phase 2: Architecture & Decomposition (Primary Mitosis Orchestrator)

Perform high-reasoning architectural work directly on the primary Mitosis Orchestrator agent:
1. Synthesize the reconnaissance results.
2. Design the architecture, interfaces, invariants, and step-by-step execution plan.
3. Partition the implementation and verification into independent work units and classify each unit's tier:
   - **`flash_lite` / `haiku`**: Config/format validation, quick status checks, single-file read verification.
   - **`flash` / `sonnet`**: Multi-file research, test authoring & execution, localized code changes, module debugging.
   - **Primary Mitosis Orchestrator**: Cross-cutting core refactors, subtle concurrency/security logic, final integration.

---

## Phase 3: Scoped Execution & Testing (`flash` / `sonnet` Workers)

1. Dispatch independent implementation or test work units:
   - **Antigravity (`agy`)**: `invoke_subagent` with `TypeName: "flash-worker"` and `"Model": "flash"`. Use `Workspace: "branch"` or `"share"` for parallel branch isolation.
   - **Claude Code**: `Agent` / `Task` with `subagent_type: "flash-worker"`, `model: "sonnet"`, and optional `isolation: "worktree"`.
   - Give every worker an explicit file allowlist, a verification command and the four-header bounded REPORT FORMAT.
   - Follow-up instructions to an existing worker go through `send_message`; never send edit tasks to a scout.
2. For large programmatic fan-out, you may also use automated workflows (`run_workflow` on agy) ensuring child agents are pinned to `model="flash_lite"` or `model="flash"`.

---

## Phase 4: Synthesis & Verification (Primary Mitosis Orchestrator)

1. Inspect the outputs, reports, and diffs produced by all workers.
2. Resolve any cross-module integration issues directly on the primary Mitosis Orchestrator agent.
3. Run final verification (or dispatch a `flash-worker` verifier) and present a concise summary with clickable file and symbol links.
4. Clean up worker environments: on `agy`, kill any `Workspace: "branch"` worker with `manage_subagents(Action="kill")`. On Claude Code, commit or merge changes from temporary worktrees.
