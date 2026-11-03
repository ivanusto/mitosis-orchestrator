#!/usr/bin/env python3
"""Grade the fix task: python3 grade.py WORKDIR. Exit 0 only if visible and hidden tests pass.

The tests that run are the pristine copies kept next to this file, not the ones in WORKDIR,
so editing the tests in the working directory cannot help. The package under test is WORKDIR/billing.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_suite(label, start_dir, work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", start_dir],
        cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120,
    )
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]
    print("%s: exit %d %s" % (label, proc.returncode, tail[0] if tail else ""))
    return proc.returncode == 0


def main(argv):
    if len(argv) != 2:
        print("usage: grade.py WORKDIR", file=sys.stderr)
        return 2
    work = os.path.abspath(argv[1])
    if not os.path.isfile(os.path.join(work, "billing", "__init__.py")):
        print("billing package missing")
        return 1
    visible = run_suite("visible", os.path.join(HERE, "template", "tests"), work)
    hidden = run_suite("hidden", os.path.join(HERE, "hidden"), work)
    return 0 if visible and hidden else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
