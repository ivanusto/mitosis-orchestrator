#!/usr/bin/env bash
set -euo pipefail
cat > util.py <<'PY'
def foo(x):
    return x * 2


def main():
    return foo(21)
PY
