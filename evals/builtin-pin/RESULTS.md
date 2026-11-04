# Results: what does pinning the built-in Explore agent do on a lookup task?

Run under `PROTOCOL.md` (sealed in `SEAL.json`, seed 20261105). Plugin commit `461d66a`. Claude Code 2.1.318, primary `opus` at `medium`, one machine. 36 scored runs, sequential. Ledger total 12.75 USD, against a budget of 20.00 USD. The medians, cost ratio, pair count and correct counts below were recomputed from the raw result events, separately from `run.py report`, and they agree.

| arm | valid runs | correct | median USD per run | median wall |
| :--- | ---: | ---: | ---: | ---: |
| `pinned` (Explore on haiku, low) | 18 of 18 | 90 of 90 | 0.2162 | 46 s |
| `inherit` (Explore on opus, medium) | 18 of 18 | 90 of 90 | 0.4213 | 53 s |

Median cost ratio 51.3%. Pinned was cheaper in 18 of 18 pairs. No run was invalid, and the primary made no `Read`, `Grep`, `Glob` or `Bash` call of its own in any run. Every Explore transcript shows the arm's model: haiku in all 18 pinned runs, opus in all 18 inherit runs.

Total by model across the 18 runs of each arm: pinned 3.80 USD opus and 0.13 USD haiku; inherit 7.56 USD opus.

**Outcome under the sealed rule: SUPPORTED** (`A = 90`, `B = 90`, ratio at most 70%, at least 14 of 18 pairs cheaper).

## What this does not show

- **Both arms scored 90 of 90, a ceiling.** These questions did not separate the two arms, so this says nothing about harder questions. A quality loss on harder work could exist and would not appear here.
- **The prompt forces delegation.** Every run dispatched exactly one Explore agent with five lookup questions, and the primary was told not to read files. A real session does this far less often, so its saving is smaller. On one machine from 2026-10-23 to 2026-11-04, unpinned built-ins were about 30% of subagent spend, and subagents are a minority of total spend.
- **Model and effort changed together.** This measures the bundle, not haiku or low effort alone.
- **Questions cluster within runs,** so this is not a formal non-inferiority test.
- **Short runs, one machine, one Claude Code version, one model pair.** No long session was run.

## Action

`CLAUDE_MITOSIS_PIN_BUILTINS` stays off by default in 2.7, as the protocol fixed before the runs. It is opt-in, with these numbers for anyone choosing it. This is a measurement on one task shape, not a general saving.

Raw data is not in the repository. The rule, schedule and harness are in `PROTOCOL.md` and `run.py`.
