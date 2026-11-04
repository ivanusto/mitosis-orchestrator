# Contributing

## Setup

- Python 3.11+ for the tooling (tests, generators, installers).
- The hooks themselves run on the Python 3.8+ standard library only. Do not add dependencies or newer syntax to `scripts/`.
- Install `ruff` and `vermin` for the lint checks, and `agy` for plugin validation.

## Checks CI runs

```bash
python3 -m unittest discover -s tests
python3 scripts/sync-generated.py --check
python3 scripts/mitosis-doctor.py   # after changing hooks or install files (a healthy Codex can exit 0 once its hook review is accepted; agy stays UNKNOWN, exit 1)
ruff check scripts tests
vermin -t=3.8- --no-tips scripts
agy plugin validate . --strict
```

## Single source

Models, prices, report wording and agent files are generated from `spec/` by `scripts/sync-generated.py`. Edit `spec/` and run `python3 scripts/sync-generated.py`; never edit the generated files by hand. `--check` fails when anything is stale.

## Code layout

See "Code layout" in [README.md](./README.md#code-layout).

## Routing changes

- Add labelled cases to `tests/routing_cases.jsonl`, in both directions: prompts that must reach the scout and prompts that must reach the worker.
- Never tune rules on a sealed holdout.

### Blind holdout protocol

1. A new holdout `tests/routing_holdout_vN.jsonl` is written by someone who has not read `scripts/` or earlier datasets.
2. It is sealed in its own commit, together with its sha256, before any rule change.
3. It is scored once per version, in separate processes.
4. After scoring it becomes a tuning set; the next blind check needs a new holdout. v9 is scored, so the next routing round writes `routing_holdout_v10.jsonl` first.

## When a CLI updates

Fake payloads have hidden real bugs: 2.6 found two edit budgets whose tests passed while the real count was 0. So after a CLI update, compare what it really sends.

1. Run a canned prompt in an empty folder with `MITOSIS_CAPTURE_PAYLOADS=<dir>`. That file is raw and private: see `SECURITY.md`.
2. `python3 scripts/sanitize-payloads.py <dir>/payloads.jsonl tests/fixtures/payloads/<platform>/<version>/<scenario>.jsonl --platform <platform> --cli-version <version> --scenario <scenario>`
3. `python3 scripts/payload-shape.py shape tests/fixtures/payloads/<platform>/<version>`
4. `python3 scripts/payload-shape.py diff tests/fixtures/payloads/<platform>/<old> tests/fixtures/payloads/<platform>/<version>` shows added, removed and retyped keys.
5. Update `spec/compat.toml` after a real run on the new version, and read the fixture diff yourself before committing it. `tests/test_fixture_privacy.py` is a net, not a review.

On Codex, every file change in the plugin needs a new hook review, so record Codex fixtures last and do not record them from a run with edited hook code. The same goes for a temporarily edited handler on any platform, since a fixture stores the hook's result too.

## Commit style

Conventional prefixes: `feat`, `fix(routing)`, `test`, `docs` (see `git log` for examples). One logical change per commit.

## Documentation

[README.md](./README.md) and [README.zh-TW.md](./README.zh-TW.md) change together; the same applies to [docs/cost.md](./docs/cost.md) and [docs/cost.zh-TW.md](./docs/cost.zh-TW.md). Do not use em dashes.

## Security

Report vulnerabilities privately, see [SECURITY.md](./SECURITY.md).
