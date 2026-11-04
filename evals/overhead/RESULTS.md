# Results: where does the plugin's extra cost come from?

> **Note, 2026-11-04.** These runs used the earlier default, where the primary was denied after 3 direct edits or a test run after an edit. Since 2.6.0 the default is advice (`*_MITOSIS_FASTPATH_MODE=advise`), so these numbers do not describe the new default. They were not rerun.

Run under `PROTOCOL.md` (sealed). Plugin commit `368dc34` (`368dc3428e45ce834cd2db61c23e0c1d3039d82e`). Plugin tree sha256 `2e9090aa43131f8bc2b1d2fadd2a7827c928a5530436c1fad3b650277e4917fa` (from `SEAL.json`). Seed 20261104.

Lookup task, three arms x three repeats, Claude Code, `opus` at `medium`, one machine. All 9 scored runs succeeded. Ledger total 6.99 USD (including the three probe runs; budget 10.00 USD). Numbers below are from the runner's `report` output.

| arm | successes | median USD | costs | median peak context | median API calls |
| :--- | ---: | ---: | :--- | ---: | ---: |
| off | 3/3 | 0.6442 | 0.6442, 0.6061, 0.7626 | 58,575 | 7.0 |
| loaded | 3/3 | 0.6444 | 0.6431, 0.9067, 0.6444 | 59,021 | 8.0 |
| orchestrate | 3/3 | 0.7684 | 0.8622, 0.7684, 0.7614 | 66,860 | 8.0 |

`Skill` calls per run were 0 in every arm and `Agent` calls per run were 0 in every arm. Tool calls per run: off 30, 24, 35; loaded 25, 62, 30; orchestrate 26, 17, 45. Permission denials per run: off 2, 2, 2; loaded 2, 3, 3; orchestrate 2, 5, 3. No leak suspects.

## USD by token class

Median over runs, tokens times list price, then each arm minus off. The sum is printed next to the effective cost so the gap is visible.

| arm | input | output | cache read | cache write | sum | effective |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| off | 0.0001 | 0.0141 | 0.0538 | 0.3650 | 0.4329 | 0.6442 |
| loaded | 0.0001 | 0.0128 | 0.0596 | 0.3686 | 0.4355 | 0.6444 |
| orchestrate | 0.0001 | 0.0158 | 0.0721 | 0.4313 | 0.5196 | 0.7684 |
| loaded minus off | +0.0000 | -0.0012 | +0.0059 | +0.0036 | +0.0002 | |
| orchestrate minus off | +0.0000 | +0.0017 | +0.0183 | +0.0663 | +0.1242 | |

The decomposition is an estimate, and it leaves a visible gap to the effective cost in every arm (for example off: 0.4329 against 0.6442).

## Static probe

First primary API call's prefix (input + cache read + cache write tokens), prompt `Reply OK`, unscored:

| arm | tokens | against off |
| :--- | ---: | ---: |
| off | 27,464 | |
| loaded | 28,250 | +786 |
| orchestrate | 30,702 | +3,238 |

## Rule and outcome

- loaded against off: compare label INCONCLUSIVE, median difference 0.0002 USD (0.0%), DIFFERS no. The printed static share (4366.7%) and output share (-732.9%) are ratios to a near-zero difference and carry no meaning.
- orchestrate against off: compare label INCONCLUSIVE, median difference 0.1242 USD (19.3%), DIFFERS no (not every orchestrate cost is above every off cost), static share 24.5%, output share 1.4%.
- Outcome: **NO TRIM** (no arm met a trim or reword condition).

## What this says

Loading the plugin without using `/orchestrate` cost the same as no plugin on this task. `/orchestrate` cost 19.3% more at the median, and the sealed formula attributes 24.5% of that to the added prefix text and 1.4% to output tokens. About a quarter of the difference is explained; the remainder is not determined.

The inference in `evals/compare/RESULTS.md`, the README and `docs/cost*.md` that the extra cost "probably comes from loading the skill and hook context" is corrected to this. The +58% of the compare lookup arm was not reproduced at this size in this run. The compare HARM verdict stands under its own sealed rule.

Scope: one short task, one machine, `opus` at `medium`, n=3 per arm, so the label for each arm is INCONCLUSIVE and the medians have wide spread. This explains overhead on a short task. It does not measure the value of the plugin in a long session, and it makes no savings claim.

## Correction, 2026-11-04

The sealed report and the sections above use token classes taken from the stream's per-call usage, which undercounts output tokens: the off median is about 700 there and 11,267 in the CLI's `modelUsage`. That is the visible gap to the effective cost. So the output share of 1.4% is wrong, and "the remainder is not determined" no longer holds.

From `modelUsage` in the kept streams, medians over each arm's runs (off, loaded, orchestrate): output tokens 11,267, 11,366, 14,085; output USD 0.2253, 0.2273, 0.2817. `/orchestrate` against off: cache write +0.066 USD, output +0.056 USD (about 45% of the 0.1242 difference), cache read +0.018 USD. Medians of classes do not add up exactly.

The static share of 24.5% is unaffected. The outcome NO TRIM is unaffected, because neither arm's difference was completely separated. `run.py` is sealed and stays as it was; any later harness must take output tokens from `modelUsage` or the transcript.
