# Changelog

## 2.1.0 - 2026-10-22

### Changed
- Routing (rule layer, judge off). Run phrases that only ask which command runs something are topics; a leftover "and run it" still upgrades. A `flash-lite-scout` typed call is upgraded to the worker when it needs web access (`scout-web`, 0.9), ends with an action after a question (`trailing-imperative`) or still asks for a run (`run-directive`, 0.9). A scout-typed call that declares read-only with its last word keeps the scout (`scout-read-only`, 0.7, judgeable).
- New soft-request frames: "would it be possible to", "appreciate it if you" + past tense, "it would be great if you" + verb. Questions without "?" (auxiliary inversion, "I wonder", "not sure", "curious", 不確定 / 不知道 / 想知道 / 好奇) now arm the trailing-imperative check.
- New Chinese verbs (弄成, 弄好, 搞定, 拉到, 換掉, 改掉, 調成, 調大, 調小) and lookups (弄清楚, 搞清楚, 釐清).
- Routing eval: new sealed blind holdout v6 (sha256 `69d3cec3e684453d510898557d13d7eb0abaad61aa09599c60bb688ef331daf9`, sealed in commit 1c5d524), scored once on 2026-10-22 in separate processes: 2.0.0 29/40, 8 cost, 3 dangerous; 2.1.0 32/40, 8 cost, 0 dangerous. Four of the cost errors are lookups typed `flash-worker`, which always routes to the worker. No tuning on v6 afterwards.
- Known gaps: lookups typed `flash-worker` (the type wins), scout prompts over 600 characters skip the trailing-imperative check, Chinese verbs outside the list.

### Added
- Codex `SubagentStop` blocks a bound scout/worker once when its final message is not the four-header report (the primary often dictates its own headings in the encrypted `spawn_agent` message). Never blocks when `stop_hook_active` is true, for unbound agents or non-mitosis roles; `CODEX_MITOSIS_ENFORCE_REPORT=0` disables. After the corrected reply `report_ok` becomes true.
- Agent definitions state that the four headers win over any layout in the task message; the Codex SessionStart notice tells the primary not to dictate report headings.
- Tests: 373 in total (361 in 2.0.0).

## 2.0.0 - 2026-10-22

### Changed
- `scripts/enforce-orchestrator.py` (3,667 lines) is split into the `scripts/mitosis_hook/` package: `common`, `models`, `report`, `routing`, `state`, `guards`, `handlers` and `platforms/{claude,codex,agy}`. The entry script keeps its path, `HANDLERS` and `main()`, and re-exports every name it defined before, so hook configs, the MCP server and by-path `import` users are unaffected. Verified by replaying 464 recorded and synthetic payloads through 1.8.1 and 2.0 (byte-identical stdout, exit codes, state and settings files) and identical `decide_subagent` output on 343 labelled prompts. CI `vermin` covers the package. The Python 3.8 floor is unchanged.

### Fixed
- Codex child uuid binding. Codex 0.163 child hooks carry `agent_id` while records are keyed by `task_name`; `SubagentStop` read agy's `subagent_id`, found nothing and cleared `active_branch` on every record. A child now binds through the first line of its rollout (`session_meta.agent_path`), else through the only unbound record of the same role. `SubagentStop` fires after every child turn, so it updates only that record (stops, `last_turn_id`, `report_ok`), never blocks and never marks it dead.
- Codex role field. Role-less spawns were recorded with role `flash-worker`; the record now holds the Codex role actually sent (`worker` / `explorer`).
- Routing (rule layer, judge off): read-only questions naming migration, debug, build, tests or race condition now go to the scout (rule `question-noun`, 0.7). A single read-only shell command (`ls`, `wc`, `rg`, `git log` ...) is a lookup (`readonly-command`, 0.8). Negation reaches four words and the Chinese `不用`, `不是要你` and `並非要你`.
- Routing: dangerous misroutes that already existed in 1.8.1 are closed. A question followed by an imperative or conditional clause ("Where are the tests? Make them pass.", `trailing-imperative`, 0.85), shell mutations in pipelines (`sed -i`, `xargs rm/mv`, `-exec rm`, `dd of=`, `tee` into a file; `shell-mutation`, 0.95), and polite requests with a gerund ("Would you mind bumping axios and making sure CI passes?", `polite-request`, 0.9; also `麻煩你` / `能不能幫我` + action). These three rules are not judgeable, so a judge cannot send them to the scout. Also: "roll it back" is imperative, a shell redirect is a run task, and a `-delete` flag is not a path part.
- Routing eval (correct out of 40, cost errors, dangerous), 1.8.1 -> 2.0.0: v1 39/40, 1, 0 (unchanged); v2 39/40, 1, 0 (unchanged); v3 36 -> 38 (cost errors 4 -> 2); v4 33 -> 40 (now a tuning set); `routing_cases` grew from 183 to 281 labelled prompts, all correct. New sealed blind holdout v5 (sha256 `160c44e16043fdfee4534dd5af8f4466a85ad2ce9f9ee10e4d2e74cac9cf42ad`, written without access to the scripts or earlier sets) was scored once against the 2.0 candidate rules: 37/40, 2 cost, 1 dangerous (`v5-en-19`, a polite gerund request); 1.8.1 scores 29/40, 10 cost, 1 dangerous on the same set. The `polite-request` rule was then written from new cases only; final 38/40, 2 cost, 0 dangerous. v5 is no longer blind; the next tuning round needs a v6 first.

### Added
- Tests: 361 in total (338 in 1.8.1).

### Upgrade notes
- No hook config or matcher change, so Codex does not need re-approval in `/hooks`.
- Claude Code: update the plugin and restart the session. Codex: re-add the plugin (the cache is a snapshot). agy: picks up the repo.
- **Behaviour change:** more read-only questions go to the cheaper scout, and some prompts that 1.8.1 sent to the scout (question + imperative, polite gerund requests, `sed -i` pipelines) now go to the worker.
- Anyone importing private helpers from the package directly should import from the entry script or `mitosis_hook.*` (the internal layout may change).
- Not in this release (roadmap): embedding stage, escalate-and-retry on test failure, Agent Teams routing, `docs/cost.md` split.

## 1.8.1 - 2026-10-22

### Fixed
- Codex `PostToolUse` never ran for `spawn_agent`. Codex reads a matcher made only of letters, digits, `_` and `|` as a list of exact tool names, so `spawn_agent|Bash` did not match `collaborationspawn_agent` (the `PreToolUse` matcher contains `.*` and is a regex, which is why it did). The matcher is now `spawn_agent|collaborationspawn_agent|Bash`, so failed spawns are removed again and `agent_path` is stored. The follow-up guard itself already worked in 1.8.0, since spawns are registered at `PreToolUse`.
- The matcher test applies Codex's exact-list versus regex rule.

### Upgrade notes
- Codex: approve the hooks again in `/hooks` (the `PostToolUse` matcher changed). If 1.8.0 was never approved, one approval covers both.

## 1.8.0 - 2026-10-22

### Fixed
- Codex follow-up guard. Codex 0.163 (multi-agent v2) names its agent tools in hook payloads with the namespace glued on (`collaborationspawn_agent`, `collaborationfollowup_task`, `collaborationsend_message`, ...). The `spawn_agent` matcher still fired, but the handler compared the full name, so no Codex spawn was ever classified or recorded and the follow-up guard had nothing to check. The namespace is now stripped (`collaboration`, `collaboration.`, `collaboration__`).
- Codex spawns are registered at `PreToolUse` under their `task_name`, the address `followup_task` and `send_message` use in `target` (`probe` or `/root/probe`). `PostToolUse` stores the returned task path (`{"task_name":"/root/probe"}`) as `agent_path` and drops the record when the spawn fails. Subagent hooks, which share the primary's `session_id`, leave the record alone.
- Codex `spawn_agent` messages reach hooks as an encrypted token (`gAAAAA...`). The hook no longer classifies or edits them: the requested `agent_type` decides the tier, the message is left byte-identical (appending REPORT FORMAT would corrupt it) and a v2 call without a role gets `agent_type: "worker"` instead of inheriting the primary's model. v2 calls (`task_name` / `fork_turns`) no longer get a `model` field; the role file pins the model.

### Added
- `followup_task` and `send_message` to a Codex scout are guarded. Readable text goes through the mutation and numbered-step checks, and `followup_task` (which starts a new turn) is also routed like a new task. Text the hook cannot read is refused for scouts, with a hint to spawn a new `explorer` or a `worker`. Messages to workers and unknown recipients pass.
- Scout-side read-only layer: a Claude Code `flash-lite-scout` or Codex `explorer` cannot use `Write`, `Edit`, `MultiEdit`, `NotebookEdit` or `apply_patch`, and its shell commands go through the scout allowlist (previously Claude Code `Bash` only). This holds when a change request reaches a scout anyway.
- Debug log entries carry `event`, `rawTool` (the name before namespace stripping), `agentId`, `toolInputKeys`, `toolResponse` (first 500 characters) and a state summary (pending count, subagent keys). `CODEX_MITOSIS_DEBUG=1` enables the log in Codex.
- `tests/test_codex_hooks.py`: 13 tests built from payloads captured on Codex 0.163 (338 tests in total).

### Changed
- `hooks/codex-hooks.json` `PreToolUse` matcher adds `followup_task`, `send_message` and `apply_patch`.

### Upgrade notes
- **Codex: approve the hooks again in `/hooks`.** The matcher changed, so Codex treats the `PreToolUse` hook as untrusted and skips it silently until it is re-approved (`codex exec` runs need `--dangerously-bypass-hook-trust`).
- **Behaviour change (Codex):** the hook now sees `apply_patch`, so the primary's fast-path budget (direct edits to more than 3 files in one turn go to a worker) applies on Codex as it already did on Claude Code and agy. Set `CODEX_MITOSIS_ALLOW_DIRECT_FASTPATH=1` to keep the old behaviour.
- **Behaviour change (Codex):** follow-ups to an `explorer` are refused while Codex hides their text. Scouts take one lookup each; spawn another one.
- Codex spawns routed by role are logged with rule `codex-role`.

## 1.7.0 - 2026-10-22

### Added
- `spec/` is the single source: `spec/models.toml` (models, efforts, prices), `spec/report.toml` (the four-header report contract) and `spec/agents/*.md` (shared agent bodies). `scripts/sync-generated.py` renders the Claude Code and Antigravity agent files, the Codex role and profile TOMLs, the report block in `rules/AGENTS.md` and both READMEs, `.agents/skills/orchestrate/SKILL.md` (a byte copy of the skill) and `scripts/mitosis_generated.py`, which the hook, MCP server and cost report load by file path. `--check` runs in the test suite and fails on stale output. The Codex plugin manifest now ships the orchestrate skill through its `skills` entry.
- `CODEX_MITOSIS_{SCOUT,WORKER}_{MODEL,EFFORT}` override the Codex scout and worker models and efforts. They are read from the environment only (no `AGY_` form) and validated, because the model is spliced into the `codex exec -m` command. The hook, MCP server, policy notice and `setup-codex.py` (role and profile TOMLs, MCP `env` table) all use the effective values.
- Per-session state lock (`fcntl` on POSIX, `msvcrt` on Windows): a hidden sidecar lock file, re-entrant, released after every handler, failing open after 2 s (`CLAUDE_`/`CODEX_`/`AGY_MITOSIS_LOCK_TIMEOUT`).
- OpenTelemetry GenAI spans, opt-in with `MITOSIS_OTEL=1` plus `MITOSIS_OTEL_FILE` (JSONL, one OTLP body per line) and/or `OTEL_EXPORTER_OTLP_[TRACES_]ENDPOINT` with `OTEL_EXPORTER_OTLP_HEADERS` and `MITOSIS_OTEL_TIMEOUT` (default 0.5 s). Each routing decision becomes one `invoke_agent {agent}` span with `gen_ai.*` attributes (operation, provider, agent name, request model, conversation id) and `mitosis.*` attributes (tier, rule, confidence, source, platform, effort). Spans are flushed after the hook response and outside the lock; the MCP server flushes from a daemon thread.
- Cost report (`scripts/mitosis-cost-report.py`): savings against an all-frontier counterfactual (subagent rows re-priced on the primary model), `--format text|json|ccusage` (`--json` stays as an alias; the ccusage shape matches `ccusage session --json` from ccusage 20.x) and a routing section built from the hook debug logs (`--debug-log`, auto-detected per session).
- Optional Claude Code statusline, `scripts/mitosis-statusline.py`, with per-session scout and worker counts: `{"statusLine":{"type":"command","command":"python3 <plugin path>/scripts/mitosis-statusline.py"}}`.
- `tests/routing_holdout_v2.jsonl`, `v3` and `v4`: 40 prompts each (10 per language and expected tier), written blind by subagents. Only v1 to v3 are used for tuning; v4 was scored once and not tuned.

### Changed
- Codex `spawn_agent` is now rewritten to the built-in `explorer` and `worker` roles. Codex 0.163 rejects an unknown `agent_type` with "unknown agent_type '...'", so the 1.6.0 rewrite to `flash-lite-scout` / `flash-worker` broke plugin-only installs (those roles exist only after `setup-codex.py`).
- Routing: linter and type-checker runs, "reproduce by running", wish-style requests ("it would be nice if") and Chinese indirect requests count as change or execution requests. Heavy keywords in paths, label nouns and read-only questions are skipped; remaining noun-like heavy hits use rule `heavy-noun` (confidence 0.75) so a judge can overrule them. Run and CLI execution signals and a docs-lookup rule (the scout has no web access) were added, and read-only declarations are recognised more widely.
- The short-prompt fallback now sends a prompt to the scout only with a lookup signal and no run task; otherwise it goes to the worker (rule `short-prompt-unknown`). Long-prompt behaviour is unchanged.
- Report-format wording is normalised across agents, rules, skill and READMEs (rendered from `spec/report.toml`).
- Debug log `decisions` carry the pinned model, effort and platform actually written into `updatedInput`, not the tier defaults.

### Fixed
- Lost updates in per-session state: parallel `Agent` spawns dropping `pending_spawns` entries, agent id pairing in `_claude_register_agent` (which made the SendMessage guard fail open), `report_nagged` flags (`SubagentStop` saves once), `consecutive_failures` and the `turn_edited_files` budget. The judge HTTP call runs outside the lock.
- Subagent tool calls (`PostToolUse` with an `agent_id`) no longer load or save the primary's state, so a subagent's failing tests no longer trip the primary's circuit breaker.
- Spawn records carry a `tier` field, and the SendMessage guard reads it, so records that store a Codex model id (or come from old state files) are recognised as scouts. This does not yet guard Codex: Codex spawn records are never paired with an agent id, so they stay in `pending_spawns` and follow-up messages to a Codex scout are not checked (see Known limitations).
- The `codex exec` Bash rewrite (since 1.5.1) matched the phrase anywhere in a command, so it rewrote unrelated `-m` flags (e.g. `python3 -m unittest`), inserted `-m <model>` into heredoc text, grep patterns and echo strings, and classified the task from the whole command. It now rewrites only a parsed `codex exec` invocation (split at unquoted `;`, `&`, `|`, newlines; heredoc bodies and comments skipped; `VAR=x`, `env`, `nohup`, `time`, `sudo`, `exec` and `npx` prefixes accepted), leaves the rest of the command byte-identical, and classifies from its prompt. Text corrupted by the old rewrite is repaired in `scripts/enforce-orchestrator.py`, `scripts/sync-generated.py`, the two `codex-config` profile TOMLs, `README.md` and the 1.6.0 and 1.5.1 entries below.
- Debug log lines are written with a single `O_APPEND` write.

### Routing eval
Rule layer only, judge off; dangerous = a worker task routed to the read-only scout.

| Set | Role in 1.7.0 | v1.6.0 | v1.7.0 |
| :--- | :--- | :--- | :--- |
| routing_cases (120 -> 183) | training | 119/120, 0 dangerous | 183/183, 0 dangerous |
| holdout v1 (40) | tuned in 1.5.1 and 1.7.0 | 32/40, 0 | 39/40, 0 |
| holdout v2 (40) | blind baseline, then training | 25/40, 4 | 39/40, 0 |
| holdout v3 (40) | blind first contact after the first tuning round, then training | 25/40, 9 | 29/40, 6 at first contact; 36/40, 0 after |
| holdout v4 (40) | blind, scored once, not tuned | 25/40, 8 | 33/40, 0 |

The 1.5.1 "zero dangerous" result did not generalise: three fresh blind sets each had 4 to 9 dangerous misroutes on v1.6.0. The first 1.7.0 round fitted the training sets but barely moved the blind score. All six remaining dangerous v3 misses came from the short-prompt fallback, so 1.7.0 changed it to send prompts with no lookup signal to the worker (a cost error instead of a failed scout run), and added run, CLI and docs-lookup signals. The 7 remaining v4 errors are lookups sent to the worker (cost, not failure).

### Known limitations
- Codex follow-up messages to a scout are not guarded: the hook does not pair Codex `spawn_agent` results with the spawned agent id, so the SendMessage guard has no Codex record to check. Verified on Codex 0.163 after release.
- Read-only questions that name debug, migration or build words still go to the worker without a judge. A judge (`MITOSIS_JUDGE_URL`) now gets a say on noun-use heavy hits (confidence 0.75).
- The v1 to v3 holdout sets were used for tuning, so their scores are regression checks, not accuracy figures. Only v4 is an unbiased measurement, and it has 40 prompts.

### Upgrade notes
- **Behaviour change:** untyped short prompts without a lookup signal now go to the worker (`sonnet` / `gpt-5.6-terra` / `flash`) instead of the scout. Expect somewhat more worker spawns and no more failed scout runs from this path.
- Codex: `spawn_agent` calls are pinned to the built-in `explorer` / `worker` roles; role names in debug logs and state change accordingly.
- State writes now wait up to 2 s for a per-session lock (`*_MITOSIS_LOCK_TIMEOUT`); on timeout the hook proceeds without it, as before.
- `SKILL.md` heading is now "(`/orchestrate`, v1.7.0)"; the report-format wording in agent prompts and READMEs is regenerated from `spec/report.toml`.
- 307 tests (was 246).

## 1.6.0 — 2026-10-22

### Added
- Codex plugin hooks: `.codex-plugin/plugin.json` points Codex 0.128+ at `hooks/codex-hooks.json` (SessionStart, UserPromptSubmit, PreToolUse on `spawn_agent|Bash|Edit|Write|mcp__mitosis__.*`, PostToolUse, SubagentStop, Stop), and `.agents/plugins/marketplace.json` lets `codex plugin marketplace add <repo>` + `codex plugin add mitosis-orchestrator@mitosis-orchestrator` install it. The manifest's `hooks` entry replaces Codex's default `hooks/hooks.json`, so Codex no longer picks up the Claude Code hook file. Codex requires hook trust: untrusted plugin hooks are skipped silently; review them in `/hooks` once, or pass `--dangerously-bypass-hook-trust` to `codex exec` for throwaway runs.
- Codex `spawn_agent` calls are pinned like Claude `Agent` calls: allow + `updatedInput.model` (`gpt-5.6-luna` scouts, `gpt-5.6-terra` workers), `agent_type` rewritten to `flash-lite-scout` / `flash-worker` when present, and the four-header REPORT FORMAT appended to the message.
- CI (`.github/workflows/ci.yml`): unit tests on Python 3.11, 3.12 and 3.13 with the routing confusion matrix in the job summary; ruff (correctness rules) and vermin (hook scripts stay Python 3.8 compatible); a Python 3.8 job that pipes sample hook payloads through the scripts.
- `pyproject.toml` (tooling config only, no runtime dependencies) and `tests/test_packaging.py`: every manifest, `SERVER_VERSION`, `SKILL.md`, `rules/AGENTS.md`, `pyproject.toml` and the top CHANGELOG entry must carry the same version, agent paths keep the `./` prefix Claude Code needs, and every hook command points at an existing script and a known mode.
- `evals/`: four `claude plugin eval` cases (scout lookup, worker fix, orchestrate skill, edit request aimed at the scout). First run on opus: 3 of 4 passed for 0.62 USD; the failing grader expected a Skill tool call that a typed `/orchestrate` never makes, fixed in the case prompt, after which that case passed all three graders (0.20 USD). Needs `bubblewrap` and `socat` on Linux because the cases grant Bash.

### Fixed
- Codex plugin hook payloads carry `hook_event_name: "PreToolUse"` like Claude Code, so the script treated them as Claude Code and answered in the wrong shape. A `turn_id` (or `--platform=codex`) now marks them as Codex, and they are answered with Claude-style `hookSpecificOutput`: deny for blocks and for `ask` (Codex fails a hook that answers ask), allow only with `updatedInput`, `{}` otherwise.
- Codex SessionStart judged the primary from `~/.codex/config.toml` even when the hook payload names the live `model`; it now uses the payload. A subagent's own UserPromptSubmit (Codex sends `agent_id`) no longer resets the primary's per-turn edit budget or warns about the subagent's model.
- MCP server: an explicit `model` on a task bypassed routing entirely, so any model (including a frontier one) ran unpinned. The router now always decides; an explicit model may only raise a task to the worker model, otherwise it is ignored and the reason says so.
- ruff: unused imports and variables in the hook, MCP server and cost report.

### Notes
- End-to-end on Codex 0.163 (throwaway `CODEX_HOME`, plugin installed through `.agents/plugins/marketplace.json`, `--dangerously-bypass-hook-trust`): every hook fired without errors; with a gpt-5.6-luna primary, the `spawn_agent` rewrite to gpt-5.6-terra took effect (the subagent's SubagentStop payload and rollout both report terra, effort low), so Codex honours `updatedInput.model`; the subagent answered in the four-header format. That run also showed a lookup routed to the worker because the primary's own `REPORT FORMAT ... TESTS:` block tripped the heavy rule: routing now ignores a trailing REPORT FORMAT block (`strip_report_format`).
- 246 tests (was 229).

## 1.5.1 — 2026-10-22

### Security
- **Claude Code permission prompts are no longer skipped.** With `*_MITOSIS_AUTO_ALLOW` unset (the default), the `PreToolUse` hook answered every `Bash`, `Read`, `Write`, `Edit` and `SendMessage` call with `permissionDecision: "allow"`, which Claude Code treats as "skip the permission prompt". It now returns `{}` (no verdict), so the user's own rules and prompts apply. A hook crash also returns `{}` instead of `allow`. `AUTO_ALLOW=1` keeps its documented in-workspace auto-approval. **Behaviour change:** sessions that ran without prompts only because of this bug will now ask.
- The Claude scout gains `Bash` for `git status` / `git log` checks, which `rules/AGENTS.md` already assigned to it. The hook denies any scout command outside a read-only allowlist (`git` read subcommands, `ls`, `rg`, `wc`, `tail`, ...), any `>` redirection and any `$(...)`.
- Judge: the bearer key (`MITOSIS_JUDGE_KEY`) is sent only over https or to loopback / private-network hosts (`MITOSIS_JUDGE_ALLOW_HTTP_KEY=1` lifts this); non-http(s) URLs are refused; redaction also masks JWTs, AWS access key ids and `user:pass@` in URLs.
- State files: the session id is reduced to `[A-Za-z0-9_-]` before it becomes a path, and each writer uses its own temp file, so parallel hooks can no longer rename each other's half-written state into place.

### Fixed (routing)
- Chinese change requests (修改, 刪除, 重構, 改名, 升到, 加上, 幫 X 加 ...) routed to the scout because the vocabulary was English-only; Chinese heavy work (執行測試, 調查, 除錯, 跨模組) and lookups (在哪裡, 列出, 有沒有) are now recognised, with negations (不要修改, 請勿刪除) and noun uses (被修改的, 修正版, 安裝說明) excluded.
- Everyday English imperatives at the start of a clause (add, bump, commit, push, install, set, change, update, write, ...) and `git commit/push/...` count as change requests; "run the test suite" upgrades a scout-typed call, since the scout cannot run it.
- Mutation and heavy keywords used as nouns or paths no longer count: "the patch version", "the bug report template", "where is the test for x", "refactor/".
- "Do not modify anything. Fix the typo" now keeps the fix at full confidence: an imperative after a read-only declaration wins. A keyword inside the declared scope ("Read-only: summarize fixes") still lowers confidence.
- `is_frontier_tier` matches whole name segments, so "proxy", "product-o1x" and sub-30B checkpoints such as `deepseek-coder-1.3b` are not frontier.

### Fixed (other)
- MCP server: invalid JSON gets a `-32700` reply and a non-object a `-32600` reply instead of silence; a handler exception returns `-32603` instead of ending the server; the prompt follows `--` so one starting with `-` is not read as a `codex exec` option.
- `setup-codex.py` no longer overwrites the first `config.toml.bak` (later runs write `.bak-YYYYmmdd-HHMMSS`), backs up a different `~/.codex/AGENTS.md` before replacing it, and writes `config.toml` atomically.
- `SKILL.md` and `rules/AGENTS.md` said v1.4.1.

### Added
- `tests/routing_cases.jsonl` (120 labelled prompts, half Chinese) and `tests/test_routing_eval.py`: no task that needs a worker may reach a scout, accuracy at least 95 %. `python3 tests/test_routing_eval.py` prints the confusion matrices. 1.5.0 rules: 73/120 with 35 worker tasks sent to the scout; 1.5.1: 119/120 with none. The rules were tuned on this set, so it is a regression check, not an accuracy figure.
- `tests/routing_holdout.jsonl`: 40 prompts written without seeing the rules or the first set. 1.5.0 rules: 21/40 with 11 worker tasks sent to the scout. First blind run of the 1.5.1 rules: 28/40 with 4. After adding soft requests ("should return None instead of raising", 改用, 整理成 docs/x.md): 32/40 with none; the remaining 8 errors send read-only questions that mention tests, builds or DEBUG to a worker, which costs more but does not fail. The test only requires zero worker tasks on the scout.
- 229 tests (was 199).

## 1.5.0 — 2026-10-20

### Added
- **`Decision` routing record**: `decide_subagent` returns `tier`, `type_override`, `effort`, `confidence`, `source`, `rule` and `reason`; `classify_subagent` / `classify_subagent_tier` keep their old return shapes. Each rule carries a prior confidence (`RULE_CONFIDENCE`): explicit types, workspace and models 0.95, mutation 0.95, heavy 0.9, lite 0.85, prompt-length fallbacks 0.5. The debug log records every decision under `decisions`, and the Codex MCP server reports `confidence` / `decision_source` per task.
- **Read-only declarations**: prompts that declare themselves read-only (`read-only`, `do not modify anything`, `唯讀`) drop mutation and heavy hits to 0.6.
- **Optional judge** (`scripts/mitosis_judge.py`, off by default): with `MITOSIS_JUDGE_URL` set, decisions below `MITOSIS_JUDGE_THRESHOLD` (0.9) go to a Jev-compatible endpoint (TypeSafe Jev, a self-hosted gateway, Ollaya). The prompt is cut to 2000 characters and secrets, e-mail and IP addresses and home directories are masked. An unsure, slow (`MITOSIS_JUDGE_TIMEOUT`, 1.5 s) or failing judge falls back to `flash`. Typed, workspace and explicit-model decisions are never judged; a judge may veto a scout's mutation upgrade.

### Fixed
- Negated mutation phrases with coordinated verbs or slash-joined words (`Do NOT git fetch/pull or modify`, `never delete or rename`) were read as edit requests. `don't X and fix Y` still counts as a fix.
- Codex `mitosis_scout` matched mutation verbs without stripping negations, so "Do not edit anything" upgraded the scout to a worker.
- The MCP server reported `SERVER_VERSION` 1.3.0.

### Notes
- Measured on 66 real subagent prompts from local sessions: with the judge off, no tier or type changes against 1.4.1; plugin-typed calls never reached the judge threshold.
- 199 tests (was 176).

## 1.4.1 — 2026-10-19

### Added
- **REPORT FORMAT auto-append**: when a subagent prompt has no `REPORT FORMAT` block, the hook appends the tier's four-header block (scout variant fixes `FILES_CHANGED: NONE`) on both agy `invoke_subagent` and Claude Code `Agent` calls, so a forgetful primary no longer costs a SubagentStop round trip. Prompts that already carry all four canonical headers are left untouched; a prompt whose primary invented its own `REPORT FORMAT:` headers (seen live: `STATUS: found` / `FILES` / `EVIDENCE`) gets the canonical block appended with a line saying it supersedes the earlier one. `*_MITOSIS_APPEND_REPORT_FORMAT=0` disables.
- `scripts/smoke-claude.py` now also asserts the block was appended and that SubagentStop never had to send the scout back.
- 176 tests (was 170).

## 1.4.0 — 2026-10-19

### Added (Claude Code)
- **`SendMessage` guard**: follow-ups sent to a `flash-lite-scout` that contain mutation verbs or more than 3 numbered steps are denied, as `send_message` already was on agy. Scout recipients are recognised by agent id: `PostToolUse` pairs the `agentId` in the `Agent` tool result with the pending spawn, and `SubagentStop` pairs by `agent_id` / `agent_type` as a fallback.
- **Four-header report enforcement**: `SubagentStop` validates the final message of this plugin's own subagents and, once per agent, blocks the stop with the exact report shape so the agent rewrites it (`CLAUDE_MITOSIS_ENFORCE_REPORT=0` disables; `stop_hook_active` and third-party agent types are never blocked). The validator now accepts a bare `NOTES:` line followed by bullets and ignores code fences.
- **`scripts/smoke-claude.py`**: headless end-to-end check that runs `claude -p` on a throwaway project, asserts from the hook debug log that the scout was pinned to `haiku` / `effort low`, confirms a haiku model ran from Claude Code's own usage output, and prints the cost report for the session.

### Fixed
- Claude Code `Stop` and `SubagentStop` payloads carry `last_assistant_message`, which the Codex payload detector treated as a Codex marker, so both events ran the Codex code path on Claude Code (the branch-worktree Stop reminder never fired there). A Claude `hook_event_name` now settles platform detection first.

### Changed
- `hooks/hooks.json` `PreToolUse` matcher narrowed from `*` to `Agent|Task|Bash|Read|NotebookRead|Write|Edit|NotebookEdit|SendMessage`; other tools (`Grep`, `Glob`, `WebFetch`, MCP) no longer spawn the hook. The opt-in workspace guard therefore only auto-approves those tools.
- `skills/orchestrate/SKILL.md` and `rules/AGENTS.md`: "end the turn after dispatching" is agy behaviour; on Claude Code subagents run in the background, the primary keeps working on independent planning, and follow-ups reuse a worker's context through `SendMessage` instead of re-spawning.
- 170 tests (was 159).

## 1.3.2 — 2026-10-19

### Added
- **Effort pinning on Claude Code**: the `PreToolUse` hook now sets `effort` alongside `model` on every `Agent` call it rewrites (scouts `low`, workers `medium`, mirroring the Codex roles). A valid caller-supplied `effort` is honoured; `CLAUDE_MITOSIS_PIN_EFFORT=0` turns it off; `CLAUDE_MITOSIS_SCOUT_EFFORT` / `CLAUDE_MITOSIS_WORKER_EFFORT` override per tier.
- **Per-tier model overrides on Claude Code**: `CLAUDE_MITOSIS_SCOUT_MODEL` / `CLAUDE_MITOSIS_WORKER_MODEL` (`haiku`, `sonnet`, `opus`, `fable`), e.g. `opus` workers under a `fable` primary. The session banner, delegation hints and deny messages reflect the effective models and efforts.
- **`scripts/mitosis-cost-report.py`**: token and USD ledger for Claude Code sessions. Reads `<project>/<session>.jsonl` and `<session>/subagents/agent-*.jsonl`, de-duplicates streamed usage by message id, aggregates per model, splits primary from subagents, prints duration, primary share and a side-by-side comparison for several sessions (`--last N`, `--session`, `--json`, `--prices`, `--primary-cache-ttl` / `--subagent-cache-ttl`). With the defaults (primary 1-hour cache writes, subagents 5-minute) it reproduces Claude Code's own `--output-format json` cost figures to the cent.

### Fixed
- The edit-then-test deny message on Claude Code / Codex now names the platform's delegation primitive instead of agy's `TypeName=` syntax.

### Docs
- README / README.zh-TW: new section on choosing the primary model on Claude Code (Fable 5.1 vs Opus 5.5): list prices, latency and capability differences, a recommendation table, an illustrative cost example, subscription-plan note, and how to measure with the cost report.
- 159 tests (was 144): effort / model pinning and the cost report.

## 1.3.1 — 2026-10-19

### Fixed (found by an end-to-end `/orchestrate` run on Claude Code 2.1.303)
- **Negated mutation verbs no longer upgrade scouts**: prompts ending in "Do not edit anything" (the wording the orchestrator naturally produces) matched the mutation regex, so every `flash-lite-scout` was silently promoted to a `sonnet` worker. Negated phrases (`do not / never / without / no ... edit|modify|delete|...`) are stripped before classification; real mutation requests still upgrade.
- **Subagent tool calls are no longer treated as the primary agent**: Claude Code adds `agent_id` / `agent_type` to hook input for calls made inside a subagent. Those calls now bypass the primary-only guards (edit budget, edit-then-test block, log hygiene), so a worker can edit and then run the test suite; its edits no longer count against the primary agent's budget either.
- 144 tests.

## 1.3.0 — 2026-10-19

### Fixed
- **Claude Code install path**: the documented `claude plugin add` command does not exist. The repo now ships `.claude-plugin/marketplace.json`, so it installs with `claude plugin marketplace add ivanusto/mitosis-orchestrator` + `claude plugin install mitosis-orchestrator@mitosis-orchestrator` (or `claude --plugin-dir` for a single session).
- **Claude Code agents never loaded**: `.claude-plugin/plugin.json` pointed `agents` at a directory, which the manifest schema rejects; it now lists the two `.md` files explicitly. Removed the non-existent `LS` / `MultiEdit` tools from the agent frontmatter and narrowed the `PostToolUse` matcher to `Agent|Bash`.
- **Claude 5 family recognised as frontier**: `fable` / `mythos` were classified as non-frontier, so every session and prompt emitted a "switch model" warning. Default Claude pin target is now `opus` (was `Claude 3.7 Sonnet`).
- **Claude Code `Agent` rewrite scoped down**: only this plugin's `flash-worker` / `flash-lite-scout` types (with or without the `mitosis-orchestrator:` prefix) and untyped `model: inherit` calls are pinned. Other plugins' agents, built-in types (`Explore`, `Plan`, `general-purpose`, …) and untyped calls with an explicit model pass through unchanged. New `CLAUDE_MITOSIS_PIN_SUBAGENTS=0` switch. Worktree spawns are still tracked for the Stop reminder.
- **Platform-specific guidance text**: deny messages on Claude Code / Codex now name the right delegation primitive (`Agent` + `subagent_type`, `spawn_agent` + `agent_type`) instead of agy's `TypeName=` / `Model=` syntax.
- **Codex `ask` decision**: Codex hooks only honour allow/deny, so the workspace guard now returns `deny` instead of an unsupported `ask`.

### Changed (OpenAI Codex CLI, verified against 0.154.0 docs/binary and the 0.163.0 docs)
- `[profiles.*]` tables are no longer honoured by Codex (removed in 0.134.0). `codex-config/config.toml.snippet` is replaced by `codex-config/mitosis-scout.config.toml` and `mitosis-worker.config.toml`, installed to `$CODEX_HOME/<name>.config.toml` for `codex exec --profile <name>`.
- Agent role TOMLs gained the required `name` field and dropped `sandbox_mode` (ignored in role files; sandboxing is set by the parent session or profile).
- Default Codex models: primary `gpt-6.1-sol`, worker `gpt-5.6-terra`, scout `gpt-5.6-luna`. `codex-mini-latest`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.3-codex-spark` are retired or not shipped by current Codex builds.
- `scripts/setup-codex.py` uses the running interpreter (`sys.executable`) and this repo's own `scripts/mitosis-mcp-server.py` path for `[mcp_servers.mitosis]` instead of a hard-coded `python` + `~/.gemini/...` / Windows path; it also installs the profile files and validates role `name`s.
- `scripts/mitosis-mcp-server.py` runs scouts with `--sandbox read-only` and workers with `--sandbox workspace-write`; `--dangerously-bypass-approvals-and-sandbox` is opt-in via `MITOSIS_CODEX_YOLO=1`. Dropped the hard-coded Windows `codex.exe` fallback path.

### Docs
- README / README.zh-TW rewritten: per-platform install + verification steps, agy conflict note for the predecessor `argon-orchestrator`, Claude 5 and current Codex model tables, unified `*_MITOSIS_*` env table. Chinese docs use 尖端模型 for "frontier model".
- 141 unit tests (was 127): fable tier, third-party / built-in agent pass-through, explicit model honouring, pin switch, Claude-specific deny wording, Codex sandbox flags, `setup-codex.py` end-to-end into a temp `$CODEX_HOME`.

## 1.2.0 — 2026-10-19

### Added
- **Unified 3-Platform Architecture**: Full cross-platform support across **Google Antigravity (`agy`)**, **Anthropic Claude Code**, and **OpenAI Codex CLI (`0.163.0`)**.
- **OpenAI Codex CLI Integration & Verified Models**:
  - Frontier Primary Orchestrator models: `gpt-5.4`, `gpt-6.1-sol`, `gpt-5.6-sol`, `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.5`, `o1`, `o3`, `o4`.
  - Execution Workers (`worker` / `flash-worker`): `codex-mini-latest` (default), `gpt-5.4-mini` (`sandbox_mode = "workspace-write"`).
  - Reconnaissance Scouts (`explorer` / `flash-lite-scout`): `gpt-5.6-luna` (default), `gpt-5.3-codex-spark`, `gpt-5.4-mini` (`sandbox_mode = "read-only"`, `model_reasoning_effort = "low"`).
- **Codex Native Agent TOML Roles (`codex-agents/`)**:
  - `codex-agents/explorer.toml` & `codex-agents/flash-lite-scout.toml` for low-latency read-only reconnaissance.
  - `codex-agents/worker.toml` & `codex-agents/flash-worker.toml` for scoped execution, test authoring/running, and code edits.
  - Both role definitions strictly enforce the bounded 4-header report contract (`STATUS`, `FILES_CHANGED`, `VERIFICATION`, `NOTES`).
- **Codex CLI Configuration Snippet & Auto-Mount Tooling**:
  - `codex-config/config.toml.snippet`: Standalone configuration profiles (`[profiles.mitosis-scout]`, `[profiles.mitosis-worker]`) and MCP server (`[mcp_servers.mitosis]`).
  - `scripts/setup-codex.py`: Automated idempotent installer that non-destructively backs up `~/.codex/config.toml` to `config.toml.bak`, synchronizes `rules/AGENTS.md` to `~/.codex/AGENTS.md`, installs agent roles into `~/.codex/agents/`, and appends profiles to `~/.codex/config.toml`.
- **Claude Code Plugin Manifest**:
  - Added `.claude-plugin/plugin.json` for Claude Code plugin compatibility.
- **Documentation & Manifests**:
  - Bumped `plugin.json` to `v1.2.0`.
  - Updated `rules/AGENTS.md`, `skills/orchestrate/SKILL.md`, `README.md`, and `README.zh-TW.md` with complete 3-platform routing matrices and configuration guides.

## 1.0.0 — 2026-10-19

Initial release of **Mitosis Orchestrator (mitosis-orchestrator)** for Google Antigravity (agy).

### Added
- **Mitotic Cross-Model Orchestration Architecture**: Treats any supported frontier reasoning model as the central **Master Nucleus (Primary Orchestrator)** and divides subagent workloads into flash (flash-worker for multi-file research, testing, and scoped code edits) and flash_lite (flash-lite-scout for fast, single-hop read-only reconnaissance).
- **Universal Frontier Primary Model Support**:
  - **xAI**: Grok 3, Grok 4, grok-3, grok-4, grok-3-thinking
  - **Anthropic**: Claude Opus, Claude Sonnet (Claude 3.5 / 3.7 / 4 Sonnet & Opus)
  - **Google Gemini**: Gemini 4 Argon (High / Medium), Gemini Pro, Gemini Ultra
  - **OpenAI**: GPT-4 (gpt-4o, gpt-4.1, gpt-4.5), GPT-5, o1, o3, o4
  - **DeepSeek**: DeepSeek-R1, DeepSeek-V3, deepseek-reasoner, deepseek-chat
  - **Alibaba Qwen**: Qwen Max, Qwen Plus, Qwen 2.5 / 3, QwQ (qwq-32b)
- **Strict Small-Tier Filtering**: Lookaround boundary regex prevents lightweight models (grok-3-mini, gpt-4o-mini, gpt-4.1-nano, o1-mini, o3-mini, o4-mini, claude-3-5-haiku, gemini-3-flash, flash_lite, qwen-lite) from acting as primary orchestrators while accurately preserving gemini.
- **Lifecycle Enforcement Hooks (hooks.json & scripts/enforce-orchestrator.py)**:
  - SessionStart & PreInvocation primary frontier model checks and per-turn state resets.
  - PreToolUse context-aware subagent classifier (invoke_subagent), send_message scout protection, run_workflow model enforcement, and define_subagent nesting prevention.
  - **Context Hygiene Guard**: Blocks primary agent from directly reading raw .log or transcript.jsonl files, delegating log slicing to flash-lite-scout.
  - **Fast-Path Direct Edit Budget**: Caps direct primary-agent workspace edits at 3 distinct files per turn and blocks direct workspace edit + test-runner loops on the primary agent.
  - **Two-Failure Circuit Breaker**: Halts runaway retry loops after 2 consecutive failures across run_command or invoke_subagent.
  - **Workspace Scope Guard**: Opt-in (AGY_MITOSIS_AUTO_ALLOW=1) auto-approval for in-workspace file and command tools with explicit pass-through allow fallback.
  - PostToolUse subagent metadata pairing and Stop branch worktree cleanup reminders.
- **Bounded 4-Header Subagent Report Contract**: Enforces compact STATUS / FILES_CHANGED / VERIFICATION / NOTES reports (<= 1,600 chars, up to 10 bullets, mandatory file://.../*.log URI on BLOCKED or FAILED).
- **Native AGY_MITOSIS_* Environment Variables**: Full support for AGY_MITOSIS_* configuration variables with 100% backwards-compatible fallback to AGY_ARGON_*.
- **Brand Assets & Test Suite**: Mitotic cell-division SVG logo (assets/logo.svg), bilingual documentation (README.md, README.zh-TW.md), and 73 unit tests (tests/test_enforce_orchestrator.py).
