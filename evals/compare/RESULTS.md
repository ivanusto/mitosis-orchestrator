# Results: explicit /orchestrate, per task type

Run under `PROTOCOL.md` (sealed). Plugin commit `afefb43` (`afefb432b15d73c6cf93640e26f7924602a5e7b0`); export sha256 `6fb7c063533e504203958f3918912b8b653fb4b28039853af35beb5d588b7711` (from `SEAL.json`).

Claude Code, `opus` at `medium`, 18 scored runs, all 18 succeeded, no capped run, no timeout, no leak suspect. Spend 14.13 USD including 2 warm-ups.

| task | off median USD | on median USD | difference | costs off | costs on | verdict |
| :--- | ---: | ---: | :--- | :--- | :--- | :--- |
| lookup | 0.6117 | 0.9694 | on 58% higher | 0.5906, 0.8660, 0.6117 | 0.8837, 1.1056, 0.9694 | HARM |
| ops | 0.4367 | 0.4857 | on 11% higher | 0.4079, 0.4399, 0.4367 | 0.5303, 0.4857, 0.4460 | HARM |
| fix | 0.8240 | 0.8159 | on 1% lower | 0.8240, 0.7418, 0.9825 | 0.7958, 0.8240, 0.8159 | INCONCLUSIVE |

Median peak primary context, thousands of tokens, off then on: lookup 57 and 69, ops 42 and 46, fix 84 and 81.

`Agent` calls per on-arm run: lookup 0, 0, 5; fix 0, 1, 0; ops 0, 0, 0. Seven of nine on-arm runs dispatched nothing even under explicit `/orchestrate`; subagent cost was 0.05 USD in one run and 0.35 USD in another.

1. The verdict follows the sealed rule.
2. What it measures: since the on arm rarely dispatched, the extra cost probably comes from loading the skill and hook context rather than from subagents. This is inferred from the `Agent` counts and the primary's cost. It is not a measurement of delegation itself.
3. Limits: n=3 per arm, 3 uncorrected comparisons, and ops only just crosses the 10% line with a small absolute difference (about 0.05 USD). Tasks are short (peak context 40K to 90K against a 274K median in real sessions), so the result says nothing about long sessions or context protection.
4. A first attempt stopped after one scored run because the tamper check counted hook-written bytecode as a change. The harness was fixed and resealed; that run (success, 0.70 USD, off arm) was discarded rather than reused.

Action under the protocol: the skill excludes the lookup and ops shapes ("When not to use"), and the investment in dispatch rate does not follow, so 2.5 does not work on it.

## Note, 2026-11-04 (added after the experiment; the text above is unchanged)

Item 2 above inferred that the extra cost "probably comes from loading the skill and hook context". That was an inference, not a measurement. The 2.5 overhead experiment (`evals/overhead/RESULTS.md`, plugin commit `368dc34`, lookup task, 3 arms x 3 repeats, n=3) measured it: a loaded plugin without `/orchestrate` cost the same as no plugin (median 0.6444 against 0.6442 USD, 0.0%), and `/orchestrate` cost 19.3% more (0.7684 USD), not completely separated. By the sealed formula the static prefix explains 24.5% of that difference and output tokens 1.4%; the remainder is not determined. The +58% above was not reproduced at that size in that run. The HARM verdict above stands under its own sealed rule.

This file is outside the seal's frozen set: `seal_problems` lists `RESULTS.md` as a new file.
