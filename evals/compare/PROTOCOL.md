# Protocol: does explicit /orchestrate pay off, per task type?

Pre-registered. This file, the fixtures, the graders, the prompts, `run.py` and the schedule seed are hashed into `SEAL.json` before any scored run. `run.py run` refuses when any hash differs. The rule below may not change after the seal.

## Question

Does an explicit `/mitosis-orchestrator:orchestrate` request reduce cost without losing success, for each of three task types?

## Arms

Both arms use the same model and flags: `claude -p <prompt> --model opus --effort medium --permission-mode acceptEdits --allowedTools <per-task list> --max-budget-usd 5 --output-format stream-json --verbose --strict-mcp-config`, stdin from `/dev/null`, a wall timeout per run, and `--settings '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}'`.

- off: nothing else. No plugin.
- on: the same settings, plus `--plugin-dir <frozen tree>` and the prompt prefix `/mitosis-orchestrator:orchestrate `.

The on arm sees a frozen export, not the working checkout: `run` exports a commit of the plugin repository with `git archive`, leaves out `evals/` altogether (answer keys, hidden tests and reference solutions live there), and points `--plugin-dir` at the export. The seal records the commit and the sha256 of the export; `run` refuses when either differs, and checks the export's hash again before every on-arm run. Uncommitted edits in the checkout are reported and ignored. The off arm loads nothing from the plugin repository. Prompts do not mention delegation, helpers, cost or this experiment. Working directories are fresh, outside any `/worktrees/` path.

## Tasks

| name | what it is | success |
| --- | --- | --- |
| lookup | 8 multi-hop questions over a pinned, read-only copy of four standard library packages (70 files, 1.4 MB, sha256 manifest, offline) | every answer in `answers.json` equals the key |
| fix | a synthetic order-to-cash package (31 modules, about 1,600 lines, 94 visible tests) with three independent root causes at three layers; one is masked until another is fixed; the visible tests do not catch a work-around at the symptoms | pristine visible tests and hidden tests both pass |
| ops | 8 dependent local steps (config, sqlite migrations, CSV import, log rotation, summary, manifest); mostly sequential, so it is expected to favour the off arm and is kept as that control | the final state matches the asserted state exactly |

## Design

3 tasks x 2 arms x 3 repeats = 18 scored runs, sequential. The order inside each repeat block is a shuffle fixed by the sealed seed. One unscored warm-up per arm comes first, to absorb first-run cache writes; it is charged to the budget but not reported.

A run that hits the budget cap or the wall timeout counts as a failure at the cap cost (5.00 USD). A run that ends normally but fails the grader is a failure at its measured cost. The cost of a run is the larger of the CLI's reported total and the transcript total from `scripts/mitosis-cost-report.py`, because the CLI total may leave out subagents.

Permission denials: Claude Code sometimes refuses a model-written compound command on its own ("a variable in this command can't be checked before it runs"). That is not a misconfiguration. Denial counts and reasons are recorded for every run and printed in the report, and they do not enter the verdict. The pilot fails only when Bash could not run at all (every Bash call denied) or a denial reason names the permission mode. A denial for a command the allow list does not cover, or Claude Code's own "requires approval" and "can't be checked" limits, is recorded and passes.

Leak check: after each run the harness scans the tool inputs of the primary and of its subagents for `evals/compare`, `key.json`, `hidden/` or `test_hidden`, and `reference/`, ignoring paths inside the working directory. A hit marks the run `leak_suspect` and a suspected leak counts as a failure of that run, at its measured cost. The report lists suspects.

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

The conclusion applies only to explicit `/orchestrate`, on Claude only, on one machine, with small n. It says nothing about automatic dispatch, other models, other task types or other machines. Peak context of the primary is reported next to every verdict but does not decide it. Success (including the leak rule), cost and everything else in `report` come from the run files as written; `report` never tunes, drops or reweights a run.

## Files

`run.py seal | pilot | calibrate | run | report`. `--strict-mcp-config` keeps account-level connectors out of both arms, whose tool lists would otherwise differ from run to run. `calibrate` is one unscored run of one task and arm before the seal, to size the fixtures; it refuses once SEAL.json exists, is charged to the ledger, and is never part of the scored data. The pilot is two runs of the fix task (one per arm, cap 1.5 USD) with checks printed as PASS, FAIL or UNKNOWN, and it stops at the first FAIL. It does not need the seal. Results go to `--out`: `runs/<task>__<arm>__r<k>/` with the raw stream, all result events, grader output and `result.json`; `ledger.jsonl`; `sessions.txt` (session ids and transcript project directories, so the transcripts can be removed later). Working directories under `--work-root` are deleted after grading unless `--keep`. Nothing outside `--work-root` and `--out` is touched.
