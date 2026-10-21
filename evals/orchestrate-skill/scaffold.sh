#!/usr/bin/env bash
set -euo pipefail
mkdir -p pkg
echo '"""pkg."""' > pkg/__init__.py
cat > pkg/core.py <<'PY'
def run():
    return 1
PY
