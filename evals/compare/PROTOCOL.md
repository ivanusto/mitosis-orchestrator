# Protocol: does explicit /orchestrate pay off, per task type?

Pre-registered. This file, the fixtures, the graders, the prompts, `run.py` and the schedule seed are hashed into `SEAL.json` before any scored run. `run.py run` refuses when any hash differs. The rule below may not change after the seal.

## Question

Does an explicit `/mitosis-orchestrator:orchestrate` request reduce cost without losing success, for each of three task types?

## Arms

Both arms use the same model and flags: `claude -p <prompt> --model opus --effort medium --permission-mode acceptEdits --allowedTools <per-task list> --max-budget-usd 5 --output-format stream-json --verbose`, stdin from `/dev/null`, a wall timeout per run, and `--settings '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}'`.

- off: nothing else. No plugin.
- on: the same settings, plus `--plugin-dir <frozen tree>` and the prompt prefix `/mitosis-orchestrator:orchestrate `.

The frozen tree is a clean git checkout; `run` refuses a dirty tree and, if the seal recorded a commit, any other commit. Prompts do not mention delegation, helpers, cost or this experiment. Working directories are fresh, outside any `/worktrees/` path.

## Tasks

| name | what it is | success |
| --- | --- | --- |
| lookup | 8 multi-hop questions over a pinned, read-only copy of four standard library packages (sha256 manifest, offline) | every answer in `answers.json` equals the key |
| fix | a 10 file synthetic package with one root cause that shows up in 4 modules; visible tests fail | pristine visible tests and hidden tests both pass |
| ops | 8 dependent local steps (config, sqlite migrations, CSV import, log rotation, summary, manifest) | the final state matches the asserted state exactly |

## Design

3 tasks x 2 arms x 3 repeats = 18 scored runs, sequential. The order inside each repeat block is a shuffle fixed by the sealed seed. One unscored warm-up per arm comes first, to absorb first-run cache writes; it is charged to the budget but not reported.

A run that hits the budget cap or the wall timeout counts as a failure at the cap cost (5.00 USD). A run that ends normally but fails the grader is a failure at its measured cost. The cost of a run is the larger of the CLI's reported total and the transcript total from `scripts/mitosis-cost-report.py`, because the CLI total may leave out subagents.

Budget: spend, including `--spent-before`, may not pass 60 USD. Before each run the harness stops cleanly if spent plus the run's cap would exceed 60.

There are no top-up runs. A (task, arm, repeat) cell that already has results is never run again, and a cell interrupted mid-run stays charged at its cap and stays empty.

## Rule, per task type

With n = 3 per arm. Let `S_on` and `S_off` be success counts, and let costs be the effective costs above.

- BENEFIT: `S_on >= S_off`, and every on-arm cost is below every off-arm cost (complete separation), and the median on-arm cost is at least 25% below the median off-arm cost.
- HARM: `S_on < S_off`, or complete separation with the on arm above the off arm and the median on-arm cost at least 10% above the median off-arm cost.
- INCONCLUSIVE: anything else, including a tie between an on-arm and an off-arm cost, and incomplete data.

With 3 runs per arm, complete separation is the strongest evidence this design can give: it happens by chance with probability 1/20 = 0.05 one-sided. The thresholds are compared exactly (25.0% passes, 24.9% does not; 10.0% is HARM, 9.9% is not).

## Actions

- Two or more BENEFIT and no HARM: 2.5 invests in dispatch rate.
- Any HARM: the skill excludes that task type, and the investment above does not follow.
- Anything else: the README stops claiming savings and only claims what was measured.

## Scope

The conclusion applies only to explicit `/orchestrate`, on Claude only, on one machine, with small n. It says nothing about automatic dispatch, other models, other task types or other machines. Peak context of the primary is reported next to every verdict but does not decide it. Success, cost and everything else in `report` come from the run files as written; `report` never tunes, drops or reweights a run.

## Files

`run.py seal | pilot | run | report`. The pilot is two runs of the fix task (one per arm, cap 1.5 USD) with checks printed as PASS, FAIL or UNKNOWN, and it stops at the first FAIL. It does not need the seal. Results go to `--out`: `runs/<task>__<arm>__r<k>/` with the raw stream, all result events, grader output and `result.json`; `ledger.jsonl`; `sessions.txt` (session ids and transcript project directories, so the transcripts can be removed later). Working directories under `--work-root` are deleted after grading unless `--keep`. Nothing outside `--work-root` and `--out` is touched.
