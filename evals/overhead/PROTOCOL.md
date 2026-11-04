# Protocol: where does the plugin's extra cost come from?

Pre-registered. This file, `run.py`, `evals/compare/run.py`, the lookup fixture files and the cost scripts are hashed into `SEAL.json` before any run. `run.py probe` and `run.py run` refuse when any hash differs. The rule below may not change after the seal.

## Question

The sealed on/off experiment in `evals/compare` found the lookup task 58% dearer with the plugin on (costs on 0.88, 1.11, 0.97 USD; off 0.59, 0.87, 0.61), with mostly no `Agent` calls. `RESULTS.md` and the README inferred that the extra cost comes from loading the skill and hook context. Measured sizes say that inference is probably wrong or incomplete: SessionStart output 761 bytes, normal tool-call hook output `{}`, `SKILL.md` 6,092 bytes (about 2K tokens, about 0.03 USD per run by arithmetic). The transcripts were deleted, so no input/output split exists. "Plugin loaded, no /orchestrate" is how the plugin is used every day and was never measured.

This experiment asks: for the same lookup task, how much of the cost difference is text the plugin adds to the prompt prefix (static), how much is extra output tokens, and how much is something else?

## Arms

Same model and flags as the compare experiment in every arm: `claude -p <prompt> --model opus --effort medium --permission-mode acceptEdits --allowedTools <lookup list> --max-budget-usd <cap> --output-format stream-json --verbose --settings '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}' --strict-mcp-config`, stdin from `/dev/null`, a wall timeout per run.

- off: nothing else. No plugin.
- loaded: `--plugin-dir <frozen export>`, prompt unchanged. The plugin is present and `/orchestrate` is not used.
- orchestrate: `--plugin-dir <frozen export>`, prompt prefix `/mitosis-orchestrator:orchestrate `.

The frozen export is `git archive` of the commit named at `seal --plugin-commit`, with `evals/` left out (as `export_frozen` in compare). The seal records the commit and the export's sha256; the runner refuses when either differs and checks the export's hash before every plugin-arm run. The task is the compare lookup fixture, used by path: `evals/compare/tasks/lookup` (scaffold, prompt, grader, allowed tools). Working directories are fresh, outside any `/worktrees/` path.

Arm state check: every run's `init` event is read. For a plugin arm the plugin `mitosis-orchestrator`, the agent `mitosis-orchestrator:flash-worker` and the skill or command `mitosis-orchestrator:orchestrate` must be listed; for off no item may mention mitosis. The three facts are written to `result.json`. A run whose arm is in the wrong state stops the runner (exit 3); it is not repeated, and the experiment is void until the cause is found.

## Static probe (unscored)

One run per arm with the prompt `Reply OK` (the orchestrate arm with its prefix), in an empty working directory, cap 0.50 USD. The first primary API call's prefix, input + cache read + cache write tokens, measures the text each arm puts in front of the model directly. The probe runs first, in an order shuffled by the sealed seed, and `run` refuses unless all three probe runs exist with a correct arm state. Probe cost is charged to the budget.

## Scored runs

3 arms x 3 repeats = 9 runs, sequential. Order inside each repeat block is a shuffle fixed by the sealed seed. Per-run cap 2.00 USD. There is no warm-up: three warm-ups on the lookup task would cost about 2.7 USD of a 10 USD budget, and the probe runs already write the shared system-prompt cache once per arm. Cache writes are measured as their own token class, not hidden; the shuffled order spreads any first-run effect across arms.

Budget: hard total 10.00 USD including the probe and `--spent-before`. Before each run the runner stops cleanly if spent plus that run's cap would exceed 10.00 (a started run is charged at its cap until its end line replaces it). There are no top-up runs: a cell that has results is never run again, and a cell interrupted mid-run stays charged at its cap and stays empty. If the budget stops the schedule early the data is incomplete, and incomplete data cannot satisfy any test below (NO TRIM).

Failure, leak and denial rules are those of compare: a run that hits the cap or the wall timeout counts as a failure at the cap cost; a run that ends normally but fails the grader (more than 1 wrong answer of 30) is a failure at its measured cost; a suspected leak (tool inputs naming `evals/compare`, `key.json`, `hidden/`, `test_hidden` or `reference/` outside the working directory) is a failure at its measured cost; permission denials are recorded and do not enter the verdict unless Bash could not run at all or a reason names the permission mode. The cost of a run is the larger of the CLI total and the transcript total from `scripts/mitosis-cost-report.py`.

## What is kept

For every API call, deduplicated by `message.id` (the stream repeats it once per content block): input, output, cache-read and cache-write tokens, the model, whether it was a subagent call, and the names of the tools it called. No content. Also: `Skill` calls (names), `Agent` calls, tool calls, peak primary context, the CLI and transcript costs. One `calls.jsonl` per run. The report is built from `calls.jsonl` and `result.json` only, so the transcripts may be removed afterwards (`sessions.txt` lists session ids and transcript directories).

## Report

Per arm: successes, median cost, median peak context, median API calls, Skill/Agent/tool call counts. A USD decomposition by token class (input, output, cache read, cache write): per run, tokens times list price from `scripts/mitosis-cost-report.py` (primary calls at the 1-hour cache-write rate, subagent calls at the 5-minute rate, as that script does), then the median over the arm's runs; each loaded and orchestrate class minus the same class of off. The decomposition is an estimate; the sum is printed next to the effective cost so the gap is visible. The probe's first-call prefix per arm and its difference from off are printed.

## Rule

Let `A` be loaded or orchestrate, with n = 3 each, `E` the effective costs, and median differences taken as median(A) minus median(off).

- compare labels: BENEFIT, HARM and INCONCLUSIVE are computed for A against off exactly as in `evals/compare/PROTOCOL.md` (A plays the on arm) and printed.
- DIFFERS(A): every A cost is above every off cost (complete separation, a tie fails) and the median difference is at least 10% of the off median. 10.0% passes, 9.9% does not. This is the cost branch of HARM; fewer successes alone do not make DIFFERS.
- STATIC SHARE(A): let `X` be the probe's first-call prefix tokens of A minus those of off (not below 0). The modelled static cost per run is `X * (cache_write_price + (k - 1) * cache_read_price)` where `k` is the median number of primary API calls of A's scored runs, prices for the primary model at the 1-hour rate. STATIC SHARE is that cost divided by the median USD difference. 50.0% passes, 49.9% does not.
- OUTPUT SHARE(A): the median output-token USD of A minus the same for off, divided by the median USD difference. 50.0% passes.

Outcomes, in this order:

1. TRIM TEXT, only if for loaded or orchestrate DIFFERS(A) and STATIC SHARE(A) >= 50%. A trim is verified with the probe alone: rerun `probe` on the trimmed commit and compare prefixes. No scored claim of improvement is made from a probe.
2. REWORD SKILL, if not 1, DIFFERS(orchestrate) and not DIFFERS(loaded) and OUTPUT SHARE(orchestrate) >= 50%. A claim that the reworded skill improved anything needs a rerun within the same 10 USD limit or a new budget.
3. NO TRIM otherwise, including incomplete data and a missing probe.

Whatever the outcome, the inference in `evals/compare/RESULTS.md`, the README and `docs/cost*.md` ("the extra cost probably comes from loading the skill and hook context") is corrected to what was measured here. The compare directory is sealed and is not edited by this experiment; the correction goes in the notes next to it and in the README and docs.

## Scope

One short task (peak context well under the 274K median of real sessions), one machine, `opus` at `medium`, n = 3 per arm. It explains where the overhead on a short task comes from. It does not measure the value of the plugin in a long session, and a NO TRIM or a REWORD SKILL here is not evidence against that use.

## Files

`run.py seal --plugin-commit SHA [--seed N] | probe | run | report`, with `--work-root`, `--out`, `--spent-before`. `probe` and `run` share `--out` and its `ledger.jsonl`, so spend outside that directory is passed as the same `--spent-before` to both. `run` is resumable (`--resume`) and holds a lock so two runners cannot share an output directory. Results: `probe/<arm>/` and `runs/<arm>__r<k>/` with `stream.jsonl`, `calls.jsonl`, grader output and `result.json`; `ledger.jsonl`; `sessions.txt`. Working directories under `--work-root` are deleted after grading unless `--keep`. Nothing outside `--work-root` and `--out` is written, and nothing under `evals/compare` is changed or given bytecode.
