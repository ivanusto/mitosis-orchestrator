#!/usr/bin/env python3
"""Create the fresh working directory for the ops task: python3 scaffold.py WORKDIR."""

import os
import shutil
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
    for name in sorted(os.listdir(TEMPLATE)):
        src = os.path.join(TEMPLATE, name)
        dst = os.path.join(work, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
