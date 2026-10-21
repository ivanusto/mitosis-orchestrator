# Plugin evals

Cases for `claude plugin eval`: scout lookup, worker fix, the `/orchestrate` skill, and edit requests aimed at the scout.

The orchestrate skill case asks for the skill in plain language instead of typing `/orchestrate`, because a typed slash command expands the skill directly and never makes a Skill tool call, so the `skill-fired` grader could never pass.

Each case has `prompt.md`, `case.yaml` (scaffold script), and `graders/`.

## Run

From the plugin root:

    claude plugin eval . --trust-plugin --scaffold --allow-tools Bash Write Edit --runs 1 --ablation none --max-cost-usd 3 --json eval-results.json

`--scaffold` is required to create the fixture files, and `--allow-tools` is required because Bash, Edit and Write are not granted by `allowed_tools` alone. Put the target (`.`) before `--allow-tools` and `--json`.

## Two-arm comparison

Drop `--ablation none` (default is `with-without`) to also run a no-plugin baseline and get a delta:

    claude plugin eval . --trust-plugin --scaffold --allow-tools Bash Write Edit --runs 1 --max-cost-usd 6 --json eval-results.json

Graders marked `arm: with-only` are reported as indicators, not scored.

In the baseline arm, plugin agents (`flash-lite-scout`, `flash-worker`) fail with "Agent type not found". This is by design: the plugin is not loaded there, so the delta measures what the plugin adds.
