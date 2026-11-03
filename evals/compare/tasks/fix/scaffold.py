#!/usr/bin/env python3
"""Create the fresh working directory for the fix task: python3 scaffold.py WORKDIR."""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template")


def main(argv):
    if len(argv) != 2:
        print("usage: scaffold.py WORKDIR", file=sys.stderr)
        return 2
    work = os.path.abspath(argv[1])
    if os.path.exists(work) and os.listdir(work):
        print("scaffold: %s exists and is not empty" % work, file=sys.stderr)
        return 2
    os.makedirs(work, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("billing", "tests"):
        shutil.copytree(os.path.join(TEMPLATE, name), os.path.join(work, name), ignore=ignore)
    git = ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
    for cmd in (git + ["init", "-q"], git + ["add", "-A"], git + ["commit", "-q", "-m", "initial state"]):
        subprocess.run(cmd, cwd=work, check=True, stdout=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
