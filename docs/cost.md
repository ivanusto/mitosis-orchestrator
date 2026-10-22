# Choosing the Primary Model on Claude Code: Fable 5.1 vs Opus 5.5

Prices and measurements as of 2026-10-22; check the official pricing page.

[繁體中文](./cost.zh-TW.md) · [Back to README](../README.md)

Mitosis pins only the daughter cells (scouts on `haiku`, workers on `sonnet`). The Master Nucleus is whatever `/model` is set to, and on Claude Code that is usually `fable` (Claude Fable 5.1) or `opus` (Claude Opus 5.5). The numbers below are Anthropic API list prices in USD per million tokens at the time of writing (2026-10); check the official pricing page before relying on them.

| Tier | Model | Input | Output | Cache read | Relative to Opus 5.5 | Default effort |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| Primary | `fable` (Claude Fable 5.1) | 10.00 | 50.00 | 0.25 | 2.5x | `high` |
| Primary | `opus` (Claude Opus 5.5) | 4.00 | 20.00 | 0.20 | 1x | `medium` |
| Worker | `sonnet` (Claude Sonnet 5.5) | 2.00 | 10.00 | 0.20 | 0.5x | `high` (Mitosis pins `medium`) |
| Scout | `haiku` (Claude Haiku 5.5) | 0.10 | 0.50 | about 0.01 | 0.025x | `medium` (Mitosis pins `low`) |

### What changes when you swap the primary

- **Price per token: 2.5x.** Fable 5.1 and Opus 5.5 share the same tokenizer family, so the same transcript costs about 2.5x on Fable. Under Mitosis that multiplier applies only to the primary's share (planning, decomposition, reviewing diffs, synthesis); scouts and workers cost the same under either primary.
- **Wall-clock time.** Fable 5.1 takes longer turns: at `high` effort a single request that gathers context, builds and self-verifies can run many minutes, and a 15-minute turn on a hard task is normal. Opus 5.5 defaults to `medium`, where it matches or beats Opus 5 at `high` in fewer steps and with roughly half the tokens. Both models get noticeably slower at `xhigh` / `max`. Effort is the main latency and cost lever on both: `/effort` in the session, `effortLevel` in `~/.claude/settings.json`, or `CLAUDE_CODE_EFFORT_LEVEL`.
- **Capability.** Fable 5.1 pulls ahead on long-horizon autonomous runs, ambiguous specifications it must clarify itself, cross-cutting refactors with subtle invariants, deep code review, and sustained coordination of parallel subagents. Its `low` / `medium` settings still perform very well, often above previous models at `xhigh`. Opus 5.5 is the stronger value for multistep agentic coding and review on well-specified tasks: cheaper per solved task, not just per token.
- **Where the money goes.** In orchestrated sessions the primary's cache reads and output dominate the bill even with every scout and worker pinned (70% to 85% of spend in the two measured sessions below). An unpinned subagent inherits the primary model: built-in `Explore` / `general-purpose` agents and untyped `Agent` calls with an explicit `model` are deliberately left alone by the hook, and one inherited `fable` subagent in a measured session cost about 40x what thirteen `haiku` scouts did.

### Recommendation

| Situation | Primary | Effort |
| :--- | :--- | :--- |
| Day-to-day orchestration, well-specified tasks (default) | `opus` | `medium`; `high` for architecture-heavy turns |
| Cost-sensitive batches of routine tasks | `opus` | `low` / `medium` |
| Multi-hour autonomous run you walk away from | `fable` | `medium`; `high` only for the hardest parts |
| Ambiguous specification the primary has to pin down itself | `fable` | `medium` |
| Cross-cutting refactor with invariants, concurrency or security implications | `fable` | `high` |
| Deep review of a large diff or a repository-history investigation | `fable` | `medium` |

The plugin's default Claude pin target is `opus` for the same reason. Rule of thumb: if the cost report shows the primary carrying more than about two thirds of the tokens, you are paying primary rates for work the daughter cells should have done. Fix delegation first (typed `mitosis-orchestrator:*` subagents, no inherited models), then revisit the model.

**Illustrative example** (assumptions, not a measurement): a primary that reads 300K input tokens per task (80% served from cache) and writes 30K output tokens.

| Primary | Input (60K) | Cache read (240K) | Output (30K) | Per task |
| :--- | ---: | ---: | ---: | ---: |
| `fable` | 0.60 | 0.06 | 1.50 | about 2.16 USD |
| `opus` | 0.24 | 0.05 | 0.60 | about 0.89 USD |

That is a 2.4x difference on the primary alone; the scout and worker bill is identical in both rows. Real sessions differ in turn count and effort, so measure your own.

**Subscription plans (Pro / Max).** These are not billed per token, but their usage limits are weighted by model tier in the same direction, so a `fable` primary drains the limit noticeably faster than `opus`. The decision rule above is unchanged; the report below still tells you which model is consuming the budget.

### Measure it: `scripts/mitosis-cost-report.py`

A standard-library script that reads Claude Code transcripts (`~/.claude/projects/<project>/<session>.jsonl` plus `<session>/subagents/agent-*.jsonl`), de-duplicates streamed usage records by message id, aggregates tokens per model, splits primary from subagents, and estimates USD from a built-in price table (`--prices file.json` overrides it). Cache writes are priced at the 1-hour rate for the primary and the 5-minute rate for subagents (`--primary-cache-ttl` / `--subagent-cache-ttl`), which reproduces Claude Code's own cost accounting (`claude -p --output-format json`) to the cent on the sessions checked:

```bash
python3 scripts/mitosis-cost-report.py                     # newest session of the current project
python3 scripts/mitosis-cost-report.py --last 2            # the two newest sessions, side by side
python3 scripts/mitosis-cost-report.py --session <id-A> --session <id-B> --json
python3 scripts/mitosis-cost-report.py --format ccusage    # shape of `ccusage session --json` (ccusage 20.x)
python3 scripts/mitosis-cost-report.py --debug-log /tmp/mitosis-orchestrator/<session>/mitosis_orchestrator_debug.log
```

Since 1.7.0 the report also prints a savings line: every subagent row is re-priced on the primary model (the all-frontier counterfactual) and compared with what the pinned scouts and workers cost. `--format text|json|ccusage` selects the output (`--json` stays as an alias). When a hook debug log exists (`*_MITOSIS_DEBUG=1`; found automatically per session or passed with `--debug-log`), a routing section shows how many decisions went to each tier and which rules fired.

Trimmed output from two real (different) tasks, one per primary:

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

### Tuning the daughter cells

Since 1.3.2 the Claude Code hook pins `effort` as well as `model` on every `Agent` call it rewrites: scouts run at `low`, workers at `medium` (the same split as the Codex roles). A valid `effort` passed by the caller is honoured. Override per tier or turn it off:

```bash
export CLAUDE_MITOSIS_WORKER_MODEL=opus      # heavier workers under a fable primary (haiku|sonnet|opus|fable)
export CLAUDE_MITOSIS_SCOUT_EFFORT=medium    # low|medium|high|xhigh|max
export CLAUDE_MITOSIS_PIN_EFFORT=0           # stop adding effort; models are still pinned
```
