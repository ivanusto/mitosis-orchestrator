#!/usr/bin/env python3
"""Re-derive every answer from questions.json with ast and compare with key.json.

    python3 verify_key.py [TREE]   TREE is a directory holding asyncio/, email/, ... (default: a fresh scaffold of the
                                   local standard library, checked against the manifest first)

Exit 0 when every derived answer equals the key. Nothing here uses memory of the stdlib: each answer is found by
following imports, base classes and call targets through the source, which is what the questions ask a reader to do.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import questions  # noqa: E402
import scaffold  # noqa: E402
from treequery import Tree  # noqa: E402


def derive(tree, specs):
    return {s["id"]: questions.answer(tree, s) for s in specs}


def main(argv):
    with open(os.path.join(HERE, "questions.json"), encoding="utf-8") as fh:
        specs = json.load(fh)
    with open(os.path.join(HERE, "key.json"), encoding="utf-8") as fh:
        key = json.load(fh)
    tmp = None
    if len(argv) > 1:
        root = os.path.abspath(argv[1])
    else:
        tmp = tempfile.mkdtemp(prefix="lookup-verify-")
        if scaffold.main(["scaffold.py", os.path.join(tmp, "w")]) != 0:
            print("local stdlib does not match the manifest", file=sys.stderr)
            return 2
        root = os.path.join(tmp, "w", "stdlib")
    try:
        derived = derive(Tree(root), specs)
    finally:
        if tmp:
            for base, dirs, files in os.walk(tmp):
                os.chmod(base, 0o755)
            shutil.rmtree(tmp, ignore_errors=True)
    bad = [q for q in sorted(set(key) | set(derived)) if key.get(q) != derived.get(q)]
    for q in bad:
        print("MISMATCH %s: key=%r derived=%r" % (q, key.get(q), derived.get(q)))
    print("verify_key: %d questions, %d mismatches" % (len(derived), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
