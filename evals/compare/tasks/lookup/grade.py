#!/usr/bin/env python3
"""Grade the lookup task: python3 grade.py WORKDIR. Exit 0 only if every answer is right.

The key lives next to this file (outside the working directory). Output names only the ids of wrong
answers, never the expected values. The stdlib copy must still match the sha256 manifest.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scaffold  # noqa: E402


def same(answer, expected):
    if isinstance(expected, bool) or isinstance(answer, bool):
        return answer is expected
    if isinstance(expected, (int, float)):
        if isinstance(answer, str):
            try:
                answer = float(answer.strip())
            except ValueError:
                return False
        return isinstance(answer, (int, float)) and float(answer) == float(expected)
    if isinstance(expected, str):
        return isinstance(answer, str) and answer.strip() == expected
    if isinstance(expected, list):
        return isinstance(answer, list) and len(answer) == len(expected) and all(same(a, e) for a, e in zip(answer, expected))
    return answer == expected


def main(argv):
    if len(argv) != 2:
        print("usage: grade.py WORKDIR", file=sys.stderr)
        return 2
    work = os.path.abspath(argv[1])
    with open(os.path.join(HERE, "key.json"), encoding="utf-8") as fh:
        key = json.load(fh)
    problems = scaffold.verify_tree(os.path.join(work, "stdlib")) if os.path.isdir(os.path.join(work, "stdlib")) else ["stdlib missing"]
    if problems:
        print("stdlib copy no longer matches the manifest (%d problems)" % len(problems))
        return 1
    path = os.path.join(work, "answers.json")
    if not os.path.isfile(path):
        print("answers.json missing")
        return 1
    try:
        with open(path, encoding="utf-8") as fh:
            answers = json.load(fh)
    except ValueError:
        print("answers.json is not valid JSON")
        return 1
    if not isinstance(answers, dict):
        print("answers.json is not an object")
        return 1
    wrong = [q for q in sorted(key) if q not in answers or not same(answers[q], key[q])]
    for q in wrong:
        print("%s wrong or missing" % q)
    print("lookup: %d of %d correct" % (len(key) - len(wrong), len(key)))
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
