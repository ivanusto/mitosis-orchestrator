# Protocol: what does pinning the built-in Explore agent do on a lookup task?

Pre-registered. This file, `run.py`, `evals/overhead/run.py` (loaded for helpers), `evals/compare/run.py`, `evals/compare/PROTOCOL.md` and every file of the lookup fixture are hashed into `SEAL.json` by `run.py seal`, which is the only thing that creates that file. `calibrate` and `run` refuse when any hash differs. Nothing below may change after the seal, and `seal` refuses to overwrite an existing `SEAL.json`.

## Question

On a lookup task, what does running the built-in `Explore` agent on the scout tier (haiku, effort low) do to answer quality and cost, compared with `Explore` inheriting the primary (opus, medium)?

`CLAUDE_MITOSIS_PIN_BUILTINS=1` makes the PreToolUse hook rewrite an `Explore` call to the scout model and the scout effort (`scripts/mitosis_hook/platforms/claude.py`, `_pin_explore`). This measures that shipped bundle. It does not separate model from effort: both change together.

## Arms

Both arms: the frozen plugin exported with `git archive` at the sealed commit (`evals/` left out) and loaded with `--plugin-dir`; `claude -p <prompt> --model opus --effort medium --permission-mode acceptEdits --allowedTools <lookup list> --max-budget-usd 1.5 --output-format stream-json --verbose --settings '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}' --strict-mcp-config`, stdin from `/dev/null`, a wall timeout, a fresh working directory outside any `/worktrees/` path. The command line is identical in both arms.

The ONLY difference is the environment variable `CLAUDE_MITOSIS_PIN_BUILTINS`:

- `pinned`: `CLAUDE_MITOSIS_PIN_BUILTINS=1`.
- `inherit`: the variable is removed from the child environment.

Nothing from the caller can leak in differently. In both arms every variable starting with `CLAUDE_MITOSIS_`, `AGY_MITOSIS_`, `CODEX_MITOSIS_` or `AGY_ARGON_` is removed from the child environment, as are `CLAUDE_CODE_SUBAGENT_MODEL` and `CLAUDE_CODE_SUBAGENT_EFFORT`; then `CLAUDE_MITOSIS_DEBUG=1` is set in both arms (the hook debug log is how a pin decision is seen), and the flag is set in `pinned` only. Settings files are read by Claude Code identically in both arms; `calibrate` and `run` read `settings.json`, `settings.local.json` under the Claude config directory and the managed settings file, refuse to start if `env` in any of them contains `CLAUDE_MITOSIS_PIN_BUILTINS` (it would override the process environment in both arms), and write any other mitosis variable found there to `frozen.json` and to every run's `result.json`. Each run's result records the flag as passed, as found in settings and as resolved.

## Tasks and schedule

The 30 gold questions of `evals/compare/tasks/lookup` (a read-only copy of eleven standard library packages). The sealed seed shuffles them into 6 batches of 5, each question in exactly one batch. A scored run is one batch, one arm, one round: 6 batches x 2 arms x 3 rounds = 36 runs, sequential, the order inside each round shuffled by the seed. One unscored warm-up per arm runs first (batch 1), in a seed-shuffled order. Nothing runs in parallel and nothing sleeps.

Prompt: the fixture's own preamble, the batch's five question lines, then exactly this instruction (it is in `run.py` as `PROMPT_TAIL`):

> Work in two steps. First dispatch exactly one subagent of type `Explore` and give it the full text of every question above, so that it finds all the answers in `stdlib/` in one pass. Do not read, search or list any file yourself, and do not dispatch any other subagent. Second, when it has reported, write `answers.json` as described above, with one entry per question id listed above and no others.

## Scoring

`grade.py` of the fixture cannot score a 5-question batch: it counts every key id missing from `answers.json` as wrong and exits 1. It is not changed. A batch is scored against `key.json` with the same normalisation (`grade.same`, loaded from the fixture): exact matches. The stdlib copy is verified against its sha256 manifest after the run; a changed copy makes the run invalid. 90 questions per arm: `A` is the pinned arm's correct count, `B` the inherit arm's.

## Cost

Per run, the per-model `costUSD` and token counts come from the result event's `modelUsage`. The stream-json per-call usage is not used: it undercounts output tokens about tenfold. The effective cost of a run is the larger of `total_cost_usd` and the sum of `modelUsage` `costUSD` (the CLI total may leave out subagents); a disagreement of more than 5% and 0.005 USD between the two is flagged in the report. The report also gives the median cost and output tokens by model family.

## Validity

A run is invalid if any of these holds. Invalid runs are never repeated (no top-up runs): a cell that has results never reruns, and a cell interrupted mid-run stays charged at its cap and stays empty.

- The stream shows no `Explore` call, or the subagent records under `<project>/<session>/subagents/` (`agent-*.meta.json`, `agent-*.jsonl`) show no `Explore` agent.
- An `Explore` transcript does not show the arm's model: every model it used must contain `haiku` in `pinned` and `opus` in `inherit`; a meta file that names another family, or a transcript with no model, is invalid too.
- `pinned`: the hook debug log (`<temp>/mitosis-orchestrator/<session>/mitosis_orchestrator_debug.log`) is missing or shows no `builtin-pin` decision. `inherit`: it shows one.
- The flag resolves to something other than the arm's intended value.
- The run hit its 1.50 USD cap (charged at the cap), timed out, produced no result event, changed the stdlib copy, or a tool input names the answer key or fixtures (the compare leak check).

Compensation: the primary's own `Read`, `Grep`, `Glob` and `Bash` calls (not the subagent's) are counted for every run and reported. Other subagent types the primary dispatches are listed per run and do not invalidate it.

A run whose init event does not list the plugin stops the runner (exit 3).

## Decision rule

Costs are the effective cost per run, paired by (batch, round). Only pairs in which both runs are valid are used for `A`, `B`, the medians and the sign count; a dropped pair counts as not cheaper. Checked in this order:

1. INCONCLUSIVE, before anything: fewer than 36 scored results, or more than 20% of the 36 runs invalid (8 or more; 7 is 19.4%).
2. QUALITY LOSS: `A <= B - 5`, or more than 20% of the valid pinned runs have compensation reads.
3. SUPPORTED: `A >= B - 2`, AND the pinned median cost is at most 70% of the inherit median cost (exactly 70% passes), AND pinned is strictly cheaper in at least 14 of the 18 pairs (one-sided sign test, about 0.015).
4. INCONCLUSIVE: anything else.

Questions cluster within runs, so this is not a formal non-inferiority test. The default of `CLAUDE_MITOSIS_PIN_BUILTINS` stays off in 2.7 whatever the result. Results are published as numbers (counts, medians, pairs), not as a claim of saving.

## Budget

Hard cap 20.00 USD in total including `--spent-before`, and 1.50 USD per run (`--max-budget-usd 1.5`). Before each run the runner stops cleanly (exit 4, `STOPPED.json`) if spent plus 1.50 would exceed 20.00; a started run is charged at its cap until its end line replaces it. `calibrate` runs one unscored batch (batch 1) per arm, prints a projected total for the 36 scored and 2 warm-up runs (spent so far plus 19 times the sum of the two calibrated costs per run), and exits 5 with a message if the projection exceeds 20.00 or a calibration run is not valid; `run` refuses without a valid calibration for each arm. A calibration run is charged to the ledger and is never scored; `calibrate` repeats only an arm whose latest run is invalid, and each repeat is charged.

## Files

`run.py seal --plugin-commit SHA [--seed N] | calibrate | run | report`. `--work-root` and `--out` are required for `calibrate` and `run`; they share `--out` and its `ledger.jsonl`, so spend outside it is passed as the same `--spent-before` to both. Results: `calibration/<arm>__<n>/`, `warmup/<arm>/`, `runs/r<round>__b<batch>__<arm>/` with `stream.jsonl`, `hook_debug.log`, `result.json`; `ledger.jsonl`, `frozen.json`, `sessions.txt`, `projection.json`. The report is built from `result.json` files only. Working directories under `--work-root` are deleted after scoring unless `--keep`. `RESULTS.md` is written later by a human from the counts.
