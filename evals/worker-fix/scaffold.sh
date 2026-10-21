#!/usr/bin/env bash
set -euo pipefail
mkdir -p tests
cat > calc.py <<'PY'
def add(a, b):
    return a - b
PY
cat > tests/test_calc.py <<'PY'
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calc import add


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
PY
