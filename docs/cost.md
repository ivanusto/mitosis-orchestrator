# Choosing the Primary Model on Claude Code: Fable 5.2 vs Opus 5.5

Prices as of 2026-11-10 (Fable 5.2 released 2026-11-09), usage measurements of 2026-10-23 to 2026-11-02 (2.4.0, measured on Fable 5.1); check the official pricing page.

[繁體中文](./cost.zh-TW.md) · [Back to README](../README.md)

Mitosis pins only the daughter cells (scouts on `haiku`, workers on `sonnet`). The Master Nucleus is whatever `/model` is set to, and on Claude Code that is usually `fable` (Claude Fable 5.2) or `opus` (Claude Opus 5.5). The numbers below are Anthropic API list prices in USD per million tokens at the time of writing (2026-11); check the official pricing page before relying on them.

| Tier | Model | Input | Output | Cache read | Relative to Opus 5.5 | Default effort |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| Primary | `fable` (Claude Fable 5.2) | 8.00 | 40.00 | 0.20 | 2x | `high` |
| Primary | `opus` (Claude Opus 5.5) | 4.00 | 20.00 | 0.20 | 1x | `medium` |
| Worker | `sonnet` (Claude Sonnet 5.5) | 2.00 | 10.00 | 0.20 | 0.5x | `high` (Mitosis pins `medium`) |
| Scout | `haiku` (Claude Haiku 5.5) | 0.10 | 0.50 | about 0.01 | 0.025x | `medium` (Mitosis pins `low`) |

### What changes when you swap the primary

- **Price per token: 2x.** Fable 5.2 is 8 / 40 against 4 / 20 for Opus 5.5 (input / output), and a 1-hour cache write is 16 against 8, so the same transcript costs about 2x on Fable, assuming the same token counts. Under Mitosis that multiplier applies only to the primary's share (planning, decomposition, reviewing diffs, synthesis); scouts and workers cost the same under either primary. In a three-task check on 2026-11-10, Opus 5.5 was cheaper per task than Fable 5.2, while Fable 5.2 was 8% to 32% faster than Fable 5.1 (n=3 each, a small sample).
- **Wall-clock time.** Fable takes longer turns: at `high` effort a single request that gathers context, builds and self-verifies can run many minutes, and a 15-minute turn on a hard task is normal. Opus 5.5 defaults to `medium`, where it matches or beats Opus 5 at `high` in fewer steps and with roughly half the tokens. Both models get noticeably slower at `xhigh` / `max`. Effort is the main latency and cost lever on both: `/effort` in the session, `effortLevel` in `~/.claude/settings.json`, or `CLAUDE_CODE_EFFORT_LEVEL`.
- **Capability.** Fable pulls ahead on long-horizon autonomous runs, ambiguous specifications it must clarify itself, cross-cutting refactors with subtle invariants, deep code review, and sustained coordination of parallel subagents. Its `low` / `medium` settings still perform very well, often above previous models at `xhigh`. Opus 5.5 is the stronger value for multistep agentic coding and review on well-specified tasks: cheaper per solved task, not just per token.
- **Where the money goes.** The primary's cache reads and output dominate the bill. In the two sessions below (measured 2026-10-19 to 2026-10-22, on Fable 5.1) the primary was 70% to 85% of spend, and across the 2026-10-19 to 2026-10-22 sessions about 90%. An unpinned subagent inherits the primary model: built-in `Explore` / `general-purpose` agents and untyped `Agent` calls with an explicit `model` are deliberately left alone by the hook, and one inherited `fable` subagent in a measured session cost about 40x what thirteen `haiku` scouts did.

### What real use looked like (2026-10-23 to 2026-11-02)

One machine, interactive Claude Code, `mitosis-cost-report.py --fleet`, Fable 5.1 list prices: 40 sessions, 5,799 primary tool calls, 27 `Agent` calls (0.5%), Bash 77% of tool calls. The primary cost about 492 USD and subagents about 11.4 USD (2.3%). Of the subagent spend 23.8% was unpinned (2 `Explore` calls, 2.71 USD), and no subagent ran on haiku. In this use the plugin barely delegated. The limit is one user, one machine, 11 days and list prices; this does not say what a heavier delegator would see.

Earlier measurements, 2026-10-19 to 2026-10-22 (Fable 5.1): primary about 90% of spend; the report's all-primary counterfactual came out about 5.3% lower, and 15% to 17% lower in sessions with heavy delegation. That is a re-pricing, not a saving, and the comparison experiment below did not reproduce a cost benefit.

### Recommendation

| Situation | Primary | Effort |
| :--- | :--- | :--- |
| Day-to-day orchestration, well-specified tasks (default) | `opus` | `medium`; `high` for architecture-heavy turns |
| Batches of routine tasks, when cost matters | `opus` | `low` / `medium` |
| Multi-hour autonomous run you walk away from | `fable` | `medium`; `high` only for the hardest parts |
| Ambiguous specification the primary has to pin down itself | `fable` | `medium` |
| Cross-cutting refactor with invariants, concurrency or security implications | `fable` | `high` |
| Deep review of a large diff or a repository-history investigation | `fable` | `medium` |

The plugin's default Claude pin target is `opus` for the same reason. The old rule of thumb (a primary above about two thirds of the tokens means wasted primary rates) is withdrawn: the data does not support it. In the 11-day measurement the primary carried almost all of the spend because most work is Bash on the primary and only 0.5% of tool calls were `Agent` calls. What the data does support is narrower: check `--fleet` for unpinned subagents (23.8% of subagent spend above) and for subagents that never run on `haiku`, and fix those before revisiting the model.

**Illustrative example** (assumptions, not a measurement): a primary that reads 300K input tokens per task (80% served from cache) and writes 30K output tokens.

| Primary | Input (60K) | Cache read (240K) | Output (30K) | Per task |
| :--- | ---: | ---: | ---: | ---: |
| `fable` (5.2) | 0.48 | 0.05 | 1.20 | about 1.73 USD |
| `opus` | 0.24 | 0.05 | 0.60 | about 0.89 USD |

That is about a 2x difference on the primary alone; the scout and worker bill is identical in both rows. Real sessions differ in turn count and effort, so measure your own.

**Fast mode.** On the models that support it (`claude-fable-5-2`, `claude-mythos-5-2`, `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`), fast mode bills every token category at 2x the standard price, cache reads and writes included. Claude Code transcripts mark such turns with `usage.speed = "fast"`; the cost report reads that field, prices those turns at 2x and prints a `fast mode:` line with their tokens and USD (already included in the totals).

**Subscription plans (Pro / Max).** These are not billed per token, but their usage limits are weighted by model tier in the same direction, so a `fable` primary drains the limit noticeably faster than `opus`. The decision rule above is unchanged; the report below still tells you which model is consuming the budget.

### Measure it: `scripts/mitosis-cost-report.py`

A standard-library script that reads Claude Code transcripts (`~/.claude/projects/<project>/<session>.jsonl` plus `<session>/subagents/agent-*.jsonl`), de-duplicates streamed usage records by message id, aggregates tokens per model, splits primary from subagents, and estimates USD from a built-in price table (`--prices file.json` overrides it). Cache writes are priced at the 1-hour rate for the primary and the 5-minute rate for subagents (`--primary-cache-ttl` / `--subagent-cache-ttl`), which reproduces Claude Code's own cost accounting (`claude -p --output-format json`) to the cent on the sessions checked:

```bash
python3 scripts/mitosis-cost-report.py                     # newest session of the current project
python3 scripts/mitosis-cost-report.py --last 2            # the two newest sessions, side by side
python3 scripts/mitosis-cost-report.py --session <id-A> --session <id-B> --json
python3 scripts/mitosis-cost-report.py --fleet --since 2026-10-23 --until 2026-11-02 --entrypoint cli   # dispatch rate, model per subagent, unpinned spend
python3 scripts/mitosis-cost-report.py --format ccusage    # shape of `ccusage session --json` (ccusage 20.x)
python3 scripts/mitosis-cost-report.py --debug-log /tmp/mitosis-orchestrator/<session>/mitosis_orchestrator_debug.log
```

Since 1.7.0 the report also prints a counterfactual line: every subagent row is re-priced on the primary model (the all-frontier counterfactual) and compared with what the pinned scouts and workers cost. `--format text|json|ccusage` selects the output (`--json` stays as an alias). When a hook debug log exists (`*_MITOSIS_DEBUG=1`; found automatically per session or passed with `--debug-log`), a routing section shows how many decisions went to each tier and which rules fired.

Trimmed output from two real (different) tasks, one per primary (measured on Fable 5.1, before 5.2 existed):

```text
  model                 role        turns      input    cache_w     cache_r    output  thinking       USD
  claude-fable-5-1      primary        60      1,740    252,164  10,664,346    99,292    22,559    12.691
  claude-fable-5-1      subagent       17        514    127,169   1,449,838     4,974       241     2.206
  claude-haiku-5-5      subagent       13         26    171,781   1,520,729    27,748    18,087     0.051
  TOTAL USD 14.948   primary share: 77% of tokens, 85% of USD

Comparison
  session       primary model(s)                    duration  primary USD  subagent USD  total USD
  e93f5d3f-0a4  claude-fable-5-1                    1h19m02s       12.691         2.256     14.948
  25dcc449-ba9  claude-opus-5-5                       58m29s        8.153         3.467     11.620
```

Run the same task under both primaries before drawing conclusions; the two sessions above were different jobs. The `claude-fable-5-1 subagent` row is the inherited-model trap described above.

`--fleet` (2.4.0) reads every session in the window and reports the share of `Agent` calls among tool calls, the model each subagent actually ran on, and unpinned subagent spend. `--entrypoint` filters by how the session was started. 2.4.0 also fixes the project-directory encoding: a working directory with an underscore failed in the default mode.

### Does explicit `/orchestrate` pay off? (comparison experiment)

Pre-registered in `evals/compare/PROTOCOL.md`, sealed by hash before any scored run. Three task types (lookup, six-cause fix, a 21-step sequential ops job as control) times two arms (plugin off; plugin on with `/mitosis-orchestrator:orchestrate`) times three repeats, 18 scored runs, same model and flags, budget 60 USD. A task type is BENEFIT, HARM or INCONCLUSIVE by a fixed rule on success and cost.

Scope: short tasks with a peak primary context of 39K to 86K tokens. The verdict says nothing about long sessions, and INCONCLUSIVE or HARM here is not evidence against using the plugin there.

Result (Claude Code, `opus` at `medium`, 18 scored runs, all 18 succeeded, none capped, timed out or flagged as a leak; 14.13 USD including 2 warm-ups). Plugin commit `afefb43`. Details: `evals/compare/RESULTS.md`.

| task | off median USD | on median USD | difference | verdict |
| :--- | ---: | ---: | :--- | :--- |
| lookup | 0.6117 | 0.9694 | on 58% higher | HARM |
| ops | 0.4367 | 0.4857 | on 11% higher | HARM |
| fix | 0.8240 | 0.8159 | on 1% lower | INCONCLUSIVE |

1. The verdict follows the sealed rule. By the pre-registered action, the skill now says when not to use it, and 2.5 does not invest in dispatch rate.
2. What it measures: 7 of 9 on-arm runs dispatched nothing even under explicit `/orchestrate`, and subagent cost was 0.05 USD in one run and 0.35 USD in another, so subagents do not account for the extra cost. An earlier version of this item inferred that the extra cost "probably comes from loading the skill and hook context". That was an inference from the `Agent` counts and the primary's cost, not a measurement. The 2.5 overhead run (`evals/overhead/RESULTS.md`; summary in the README) measured it and explains only about a quarter by its sealed formula, and output tokens about 45% (corrected 2026-11-04; see that file).
3. Limits: n=3 per arm, 3 uncorrected comparisons, and ops only just crosses the 10% line with a difference of about 0.05 USD. The tasks are short (peak context 40K to 90K against a 274K median in real sessions), so this says nothing about long sessions or context protection.
4. A first attempt stopped after one scored run because the tamper check counted hook-written bytecode as a change. The harness was fixed and resealed; that run (success, 0.70 USD, off arm) was discarded, not reused.

This plugin makes no savings claim. Parallelism for work that really is parallel is not measured here. One calibration run per arm on a long chained session (`evals/long/RESULTS.md`, not a verdict) lowered primary context growth by 30% and raised total cost by 71%.

### Tuning the daughter cells

Since 1.3.2 the Claude Code hook pins `effort` as well as `model` on every `Agent` call it rewrites: scouts run at `low`, workers at `medium` (the same split as the Codex roles). A valid `effort` passed by the caller is honoured. Override per tier or turn it off:

```bash
export CLAUDE_MITOSIS_WORKER_MODEL=opus      # heavier workers under a fable primary (haiku|sonnet|opus|fable)
export CLAUDE_MITOSIS_SCOUT_EFFORT=medium    # low|medium|high|xhigh|max
export CLAUDE_MITOSIS_PIN_EFFORT=0           # stop adding effort; models are still pinned
```
