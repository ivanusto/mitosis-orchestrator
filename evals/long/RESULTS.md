# Calibration: delegation over a chained session (not a verdict)

One run per arm on 2026-11-04, Claude Code 2.1.317, `opus` at `medium`, plugin v2.5.0, 11 chained stages. **This is calibration: n=1, unsealed, no decision rule applied.** It sizes the fixture and shows a direction; it does not establish an effect. The delegate arm has an explicit delegation instruction in every stage, so it tests the mechanism, not what the plugin does by itself.

Cost: mechanics check 0.65 USD, off arm 3.11 USD, delegate arm 4.76 USD, plus about 0.25 USD for a budget flag test.

## Only stages 1 to 6 compare

In the delegate arm, stages 7 to 10 did not do the ops work: Claude Code's permission system denied a worker's `python3` heredoc write in stage 7, the primary stopped and asked for authorisation, and no one could answer in a headless run. The ops grade failed for that reason, and later stages were cheap because they did little. So the totals for all 11 stages are not comparable, and this file uses stages 1 to 6 (lookup and fix).

| stages 1 to 6 | off | delegate | difference |
| :--- | ---: | ---: | :--- |
| total USD (`modelUsage` in the CLI result) | 1.95 | 3.33 | +71% |
| primary `opus` USD | 1.95 | 1.89 | -3% |
| `sonnet` USD | 0 | 1.43 | |
| `haiku` USD | 0 | 0.006 | |
| cumulative context growth, tokens | 101,841 | 71,556 | -30% |
| peak context, tokens | 130,423 | 101,007 | -23% |
| lookup correct | 30/30 | 30/30 | |
| fix (visible and hidden tests) | pass | pass | |

Both arms also scored 6/6 on the recall stage, 11. Over all 11 stages the off arm took 412 s and the delegate arm 1,015 s. No compaction occurred in either run.

## Reading

- Delegation kept the primary's context smaller. It did not lower the total cost in this run: the workers cost more than the primary saved.
- The primary's saving grows with the number of calls that follow. A rough estimate from this run: about 30K tokens displaced for about 1.4 USD of workers needs about 200 further primary calls to break even at Opus 5.5 list prices (8 USD/M cache write, 0.20 USD/M cache read). One machine's real sessions have a median of 66 to 85 remaining calls at the point such output enters, so this is not a saving there either. This is arithmetic on one run.
- A smaller context can still have value near the context limit, or by postponing compaction. That was not measured: the off arm peaked at 160K, below the 200K to 340K target, with no compaction.

## Limits and what a sealed run would need

- n=1 per arm, one fixture, one machine, and no human gaps between stages.
- `allowedTools` must let a worker write in `ops/`, or the ops stages must not stop on a denial.
- Costs must come from `modelUsage` or the transcript: the `prim$` and `sub$` columns of `run.py report` and `calls.jsonl` use the stream's per-call usage, which undercounts output tokens (off arm 5,069 in the stream, 43,736 in `modelUsage`). Effective USD and context growth are not affected.
- The stages would need to reach the target band.

The draft rule in `PROTOCOL.md` was not sealed or applied, and no sealed run is planned.
