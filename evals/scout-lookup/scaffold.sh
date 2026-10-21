#!/usr/bin/env bash
set -euo pipefail
mkdir -p src/app tests
touch src/app/__init__.py
cat > src/app/parser.py <<'PY'
def parse_token(text):
    """Split text into whitespace-separated tokens."""
    return text.split()
PY
cat > src/app/cli.py <<'PY'
import sys
from app.parser import parse_token


def main():
    print(parse_token(" ".join(sys.argv[1:])))
PY
cat > tests/test_parser.py <<'PY'
import unittest
from app.parser import parse_token


class T(unittest.TestCase):
    def test_split(self):
        self.assertEqual(parse_token("a b"), ["a", "b"])
PY
