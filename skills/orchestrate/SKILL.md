---
name: orchestrate
description: >-
  Structured 4-phase orchestration workflow that pins the primary agent (Mitosis Orchestrator frontier model:
  Gemini 4 Argon, Claude Opus/Sonnet, GPT, Grok, DeepSeek, Qwen) as architect/coordinator and dispatches subagents
  pinned to flash_lite (for fast reconnaissance, searches, log checks, and single-file inspection) or flash (for
  multi-file analysis, test runs, and scoped code edits). Activate when the user invokes /orchestrate or requests
  multi-agent task decomposition.
---

# Mitosis Orchestrator Workflow (`/orchestrate`)

Execute complex tasks using a 4-phase hierarchical orchestration loop with **Mitosis Orchestrator** (frontier model: Gemini 4 Argon, Claude Opus/Sonnet, GPT, Grok, DeepSeek, Qwen) as the primary agent and context-pinned **`flash_lite`** / **`flash`** subagents.

---

## Phase 1: Reconnaissance (`flash_lite` Fan-Out)

When context must be gathered across the workspace before planning:
1. Identify independent lookups (file discovery, symbol search, config inspection, git/log status).
2. Fan out concurrent **`flash_lite`** subagents in a single `invoke_subagent` call:
   - Prefer `TypeName: "flash-lite-scout"` (or `TypeName: "research"`) with `"Model": "flash_lite"`.
   - Keep each prompt narrowly scoped and end it with the four-header bounded REPORT FORMAT from `rules/AGENTS.md`.
   - Scouts are read-only and have no web tools; anything that needs `search_web` or an edit goes to Phase 3.
3. End your turn after the `invoke_subagent` call. Antigravity wakes you when the scouts report.

---

## Phase 2: Architecture & Decomposition (Primary Mitosis Orchestrator)

Perform high-reasoning work directly on the primary Mitosis Orchestrator agent:
1. Synthesize the `flash_lite` reconnaissance results.
2. Design the architecture, interfaces, invariants, and step-by-step execution plan.
3. Partition the implementation and verification into independent work units and classify each unit's tier:
   - **`flash_lite`**: Config/format validation, quick status checks, single-file read verification.
   - **`flash`**: Multi-file research, test authoring & execution, localized code changes, module debugging.
   - **Primary Mitosis Orchestrator**: Cross-cutting core refactors, subtle concurrency/security logic, final integration.

---

## Phase 3: Scoped Execution & Testing (`flash` Workers)

1. Dispatch independent implementation or test work units via `invoke_subagent`:
   - Use `TypeName: "flash-worker"` (or `TypeName: "self"` / `"research"`) with `"Model": "flash"`.
   - Use `Workspace: "share"` or `"branch"` when parallel `flash` workers modify separate branches/worktrees, or `"inherit"` when they operate on disjoint files.
   - Give every worker an explicit file allowlist, a verification command and the four-header bounded REPORT FORMAT.
   - Follow-up instructions to an existing worker go through `send_message`; never send edit tasks to a scout.
2. For large programmatic fan-out (e.g., processing a list of modules or test files in a pipeline), you may also use `run_workflow` with `agent(..., model="flash_lite")` for discovery and `agent(..., model="flash")` for per-module execution/review.

---

## Phase 4: Synthesis & Verification (Primary Mitosis Orchestrator)

1. Inspect the outputs and diffs produced by all `flash` workers.
2. Resolve any cross-module integration issues directly on the primary Mitosis Orchestrator agent.
3. Run final verification (or dispatch a `flash-worker` verifier) and present a concise summary with clickable file and symbol links.
4. Kill every `Workspace: "branch"` worker with `manage_subagents(Action="kill")` once its changes are merged or discarded. The Stop hook blocks completion once if any branch worker is still alive.
