# Long-session calibration: does delegation protect the primary's context?

Branch `exp/long-session`. This directory holds a **calibration harness only**. Calibration is unscored and yields no verdict. There is no seal here and no decision rule in the code; the rule further down is a draft for a later, separate experiment and is **NOT SEALED**.

## Question

Over a long session, does a primary agent that hands reads, searches and test runs to subagents keep its own context smaller than one that does the same work itself, and what does that cost?

The short experiments (`evals/compare`, `evals/overhead`) cannot answer this: each ran one task of a few minutes, far from the point where a context fills, compacts or goes stale. The intuition behind delegation is about the long run, so it needs a long run.

## Why calibration first

A sealed experiment needs a fixture of the right size and a rule written before any data. Both are unknown. A fixture that is too short never grows the context; one that is too long burns the money. The rule needs numbers to be well formed (how large is G, how much of it is tool output at all, does the delegating arm delegate at all). Calibration answers those with one run per arm, two arms, at most 20 USD in total, and then stops. It tells the reader whether a sealed experiment is worth running. It does not say anything about the plugin.

## Fixture

One workspace, built by the existing scaffolds under `evals/compare/tasks` (read by path, never changed), in three subfolders:

| folder | source | content |
| --- | --- | --- |
| `lookup/` | `tasks/lookup` | a read-only copy of eleven stdlib packages and 30 questions |
| `fix/` | `tasks/fix` | the shop package (62 modules, 151 visible tests, six root causes) |
| `ops/` | `tasks/ops` | the three-site setup chain |
| `notes/` | empty | stage notes written by the agent |

One `claude -p` per stage, each later stage with `--resume` on the session of the stage before.

| stage | work | graded |
| --- | --- | --- |
| 1 to 3 | lookup batches q01-q10, q11-q20, q21-q30; answers merged into `lookup/answers.json`; a short note in `notes/stage-N.md` | per batch, with the existing key |
| 4 | fix round 1: the failing tests of `test_orders`, `test_discounts`, `test_invoicing`, `test_returns` | failing visible tests recorded |
| 5 | fix round 2: `test_fulfilment` and the renewal tests of `test_ext_services` | failing visible tests recorded |
| 6 | fix round 3: whatever still fails until the visible suite passes | existing grader, visible and hidden tests |
| 7 to 9 | ops steps 1-4 for north-7, south-2, east-5, one per stage | at stage 10 |
| 10 | ops steps 5-7, plus a change request touching earlier results (`x01` in `lookup/answers.json` from q08 and q19, and `fix/CHANGES.txt` from `fix/shop/money.py`) | existing ops grader, plus a small new check of the change |
| 11 | recall: six questions about the earlier stages, written to `recall.json` | key derived from the fixtures |

**Fix round split.** The split follows what the failing visible tests show, found by applying each reference fix alone and in sequence on a scratch copy. The money helpers and the shipping tiers account for every failure in the order, discount, invoice and refund tests, so round 1 is those four test modules. The stock reservation is masked by the shipping tiers and only shows in the fulfilment tests, and the calendar explains the renewal dates; round 2 is those. The receivables ledger and the currency conversion are what is left for round 3. The prompts name test modules, never modules of the package or root causes. An agent may fix more than a round asks for; the rounds only set what it is told to look at.

**Recall.** Six questions, chosen so that a primary that delegated cannot answer them from its own context: three lookup answers, one from each batch (q05, q14, q27, restated by their subject, not their id alone); the number of tests the visible suite runs (151); the BUILD_ID of south-2; the number of rows in the `devices` table of east-5. The agent may answer from what it knows or look the value up in the workspace. The key is computed from the fixtures (`key.json`, the unittest loader, the sha256 of `params/south-2.json`, the de-duplicated CSV) and `python3 evals/long/stages.py verify-key` cross-checks it against the lookup reference answers, the ops reference solution's `summary.json` and the count unittest prints. Recall exists so the design is not biased toward delegation: the delegating primary has to re-acquire what the other already holds.

**Stage count.** `--stages N` runs stages 1..N. Stages are cut from the end, in this order: 11 (recall), 10 (ops steps 5-7 and the change request; ops is then not graded), 9, 8, 7 (the other ops sites), 6 (the final fix grade is lost), 5, 4, then lookup batches 3, 2, 1.

Prompts never mention cost, budget or this experiment. In the delegate arm every stage prompt ends with:

> Delegate reading files, searching and running tests to subagents through the Agent tool (mitosis-orchestrator:flash-lite-scout for single lookups, mitosis-orchestrator:flash-worker for the rest), and keep only their short reports here.

## Arms

* `off`: no plugin. `--model opus --effort medium --permission-mode acceptEdits --allowedTools <union of the three tasks' lists> --max-budget-usd <stage cap> --output-format stream-json --verbose --strict-mcp-config --settings '{"enabledPlugins":{"mitosis-orchestrator@mitosis-orchestrator":false}}'`, stdin from `/dev/null`, `--resume <session>` from stage 2.
* `delegate`: the same flags plus `--plugin-dir <frozen export of a named commit, evals left out>` and the clause above. `Agent` is in the allowed tools of both arms. The plugin state in `init` is recorded in `result.json`.

Auto-compaction is never disabled. Compaction events and their `preTokens` are recorded when the stream shows them (and the number of boundaries in the transcript, as a cross-check).

## Metrics

Per stage and run: effective cost (the larger of the CLI total and the transcript total; whether the CLI total is per stage or cumulative is what `g0` finds out), primary and subagent USD by token class (price table of `scripts/mitosis-cost-report.py`), API calls (deduplicated by `message.id`, `calls.jsonl` keeps token counts, model, subagent flag and tool names, never content), peak context, **cumulative context growth G** (the sum of the positive differences between successive primary API calls, so a compaction reset costs nothing and growth after it counts from the lower level), the displaceable tool output estimate (results of `Read`, `Bash`, `Grep`, `Glob` and web tools of the primary, CJK-aware estimate of 1.5 tokens per CJK character and one per four others, shown as a share of G and nothing more), Agent calls, report tokens returned by them, grader outcome, permission denials, leak suspects, compactions, wall time.

`report` prints one table per run (stage rows), then an arm comparison over the stages both runs completed. It prints numbers and differences. It prints no verdict word.

## Commands

* `g0` mechanics check, at most 1 USD for four capped stages, prints PASS, FAIL or UNKNOWN with counts as evidence and no prompts or content:
  1. does the session id stay the same on `--resume` (FAIL means it changed; the harness then follows the newest id);
  2. does SessionStart fire again each stage (hook debug log with `CLAUDE_MITOSIS_DEBUG=1` under a private `TMPDIR`);
  3. is `total_cost_usd` cumulative across stages (writes `g0.json` with `cli_cost_mode`, which `calibrate` reads, or takes `--cost-mode`);
  4. are transcripts double-counted across stages by the harness's total (PASS: the total takes differences when a stage's transcript holds the stage before it);
  5. do Agent calls return synchronously in `-p` and does a stage wait for them;
  6. does the plugin's hook state stay keyed to one state file across stages.
* `calibrate --arm off|delegate`: one run. Stops cleanly before a stage if spent plus the stage cap would pass the run cap (default 15) or the total cap (default 20, counting `--spent-before` and everything in the same `--out` including g0); a partial run is recorded as `partial`. A stage that reaches its cap stops the run (`stage_cap_exceeded`). A ledger line is written per stage, a started stage is charged at its cap until its end line replaces that. After an infrastructure failure (`infra_failed`, or a runner that died) `--resume-run` continues from the last completed stage, once. One runner per output directory.
* `report`.

This is calibration only. The output sizes the fixture and answers whether a sealed experiment is worth running; it applies no rule.

## What calibration can and cannot show

It can show: whether the delegate arm delegates at all (Agent calls per stage); how large G gets and whether compaction occurs within 11 stages; how much of G is tool output that delegation could have displaced; what the stages cost and how cost splits between primary and subagents; whether the stages are gradable and the grader passes in the off arm; whether recall is answerable by both arms; the mechanics in `g0`; how long a run takes.

It cannot show: anything with n = 1 per arm about differences (run-to-run variation is unmeasured); whether cost or success differ; the product's natural behaviour (the instruction to delegate is explicit); effects of human pauses (there are none, so no cache expiry); other machines, models, efforts or task mixes.

## Draft rule (NOT SEALED)

For a later experiment, to be written, reviewed and sealed before any run; shown here so calibration collects what it needs. None of it is implemented.

* MANIPULATION FAILED: any delegate run with fewer Agent calls than stages/2, or displaceable output at least 50% of the off median.
* HARM: success lower, or cost separated upward with a median at least 10% higher.
* BENEFIT: success not lower, G separated with a median at least 15% lower, and cost separated with a median at least 10% lower.
* PROTECTS ONLY: the G condition without the others.
* INCONCLUSIVE: none of the above.

Scope limits that would apply: one machine; explicit delegation instruction, not the product's natural behaviour; no human gaps, so no cache expiry.
