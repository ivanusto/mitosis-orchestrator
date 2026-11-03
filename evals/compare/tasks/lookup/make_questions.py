#!/usr/bin/env python3
"""Pick the lookup questions from the pinned tree and write questions.json, key.json, prompt.md.

    python3 make_questions.py            (needs the local stdlib to match the manifest)

Selection is deterministic: candidates are ranked by sha256 of their description, filtered for uniqueness and
for needing at least two files, and spread over different modules. Run it once; the outputs are committed
and sealed. verify_key.py re-derives every answer from questions.json and compares it with key.json.
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import questions  # noqa: E402
import scaffold  # noqa: E402
from treequery import Tree, Unresolved  # noqa: E402

PLAN = [("imported_const", 3), ("default_chain", 2), ("method_origin", 12), ("caller_callee", 8), ("raise_class", 6)]
SKIP_PARTS = ("idlelib", "/test", "__main__", "/tests")


def rank(desc):
    return hashlib.sha256(desc.encode("utf-8")).hexdigest()


def guessable(value):
    """Strings that are just an identifier (FIRST_COMPLETED = 'FIRST_COMPLETED') need no lookup to guess."""
    return isinstance(value, str) and value.replace("_", "").isalnum()


def usable(path):
    return not any(part in "/" + path for part in SKIP_PARTS)


def functions(tree, path):
    for st in tree.module(path).body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield None, st
        elif isinstance(st, ast.ClassDef):
            for sub in st.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield st.name, sub


def candidates(tree, kind):
    out = []
    for path in tree.files:
        if not usable(path):
            continue
        if kind == "const_chain":
            for name in sorted(tree.assigns(path)):
                if name.startswith("__"):
                    continue
                try:
                    value, touched = tree.const_value(path, name)
                except (Unresolved, ZeroDivisionError, OverflowError):
                    continue
                if len(touched) < 2 or isinstance(value, bool):
                    continue
                if isinstance(value, int) and abs(value) < 2:
                    continue
                if isinstance(value, str) and (not value or len(value) > 40 or not value.isprintable()):
                    continue
                out.append({"type": kind, "path": path, "name": name, "answer_kind": "int" if isinstance(value, int) else "str"})
        elif kind == "imported_const":
            for name, imp in sorted(tree.imports(path).items()):
                if imp[0] != "name" or name.startswith("__"):
                    continue
                spec = {"type": kind, "path": path, "name": name, "answer_kind": "int"}
                try:
                    value = questions.answer(tree, spec)
                except (Unresolved, ZeroDivisionError, OverflowError):
                    continue
                if isinstance(value, bool) or guessable(value) or (isinstance(value, int) and abs(value) < 2) or (isinstance(value, str) and (not value or len(value) > 40 or not value.isprintable())):
                    continue
                spec["answer_kind"] = "int" if isinstance(value, int) else "str"
                out.append(spec)
        elif kind == "default_chain":
            for cls, fn in functions(tree, path):
                for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
                    spec = {"type": kind, "path": path, "func": fn.name, "param": a.arg}
                    if cls:
                        spec["cls"] = cls
                    try:
                        value = questions.answer(tree, dict(spec, answer_kind="int"))
                    except (Unresolved, ZeroDivisionError, OverflowError):
                        continue
                    if isinstance(value, bool) or guessable(value) or (isinstance(value, int) and abs(value) < 2) or (isinstance(value, str) and (not value or len(value) > 40)):
                        continue
                    spec["answer_kind"] = "int" if isinstance(value, int) else "str"
                    out.append(spec)
        elif kind == "method_origin":
            for st in tree.module(path).body:
                if not isinstance(st, ast.ClassDef) or tree.find_def(path, st.name, (ast.ClassDef,)) is None:
                    continue
                try:
                    chain = tree.mro(path, st)[1:]
                except Unresolved:
                    continue
                own = tree.methods(path, st)
                for owner_path, owner_name in chain:
                    if owner_path == path:
                        continue
                    owner = tree.find_def(owner_path, owner_name, (ast.ClassDef,))
                    for name in sorted(tree.methods(owner_path, owner)):
                        if name.startswith("__") or name in own:
                            continue
                        spec = {"type": kind, "path": path, "cls": st.name, "method": name}
                        try:
                            questions.answer(tree, spec)
                        except Unresolved:
                            continue
                        out.append(spec)
        else:
            for cls, fn in functions(tree, path):
                spec = {"type": kind, "path": path, "func": fn.name}
                if cls:
                    spec["cls"] = cls
                try:
                    questions.answer(tree, spec)
                except (Unresolved, StopIteration):
                    continue
                out.append(spec)
    return out


def describe(spec):
    return json.dumps(spec, sort_keys=True)


def pick(cands, n, used_modules, used_keys):
    chosen = []
    for spec in sorted(cands, key=lambda s: rank(describe(s))):
        top = spec["path"].split("/")[0]
        key = (spec["type"], spec["path"], spec.get("cls"), spec.get("func") or spec.get("name") or spec.get("method"))
        if spec["path"] in used_modules or key in used_keys or sum(1 for c in chosen if c["path"].split("/")[0] == top) >= 2:
            continue
        chosen.append(spec)
        used_modules.add(spec["path"])
        used_keys.add(key)
        if len(chosen) == n:
            break
    return chosen


def main():
    tmp = tempfile.mkdtemp(prefix="lookup-q-")
    try:
        if scaffold.main(["scaffold.py", os.path.join(tmp, "w")]) != 0:
            return 2
        tree = Tree(os.path.join(tmp, "w", "stdlib"))
        used_modules, used_keys, chosen = set(), set(), []
        for kind, n in PLAN:
            cands = candidates(tree, kind)
            got = pick(cands, n, used_modules, used_keys)
            print("%-14s %4d candidates, %d chosen" % (kind, len(cands), len(got)))
            chosen.extend(got)
        # interleave the types so that the list does not read in blocks
        chosen.sort(key=lambda s: rank("order" + describe(s)))
        for i, spec in enumerate(chosen, 1):
            spec["id"] = "q%02d" % i
        key = {s["id"]: questions.answer(tree, s) for s in chosen}
        with open(os.path.join(HERE, "questions.json"), "w", encoding="utf-8") as fh:
            json.dump(chosen, fh, indent=1, sort_keys=True)
            fh.write("\n")
        with open(os.path.join(HERE, "key.json"), "w", encoding="utf-8") as fh:
            json.dump(key, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.makedirs(os.path.join(HERE, "reference"), exist_ok=True)
        with open(os.path.join(HERE, "reference", "answers.json"), "w", encoding="utf-8") as fh:
            json.dump(key, fh, indent=1, sort_keys=True)
            fh.write("\n")
        head = (
            "The directory `stdlib/` in the current working directory is a read-only copy of eleven packages of a Python standard library: "
            "`asyncio`, `concurrent`, `email`, `http`, `importlib`, `json`, `logging`, `multiprocessing`, `unittest`, `urllib` and `xml`. "
            "Answer the questions below from the files in that tree only. Do not answer from memory: the copy is the reference and exact values matter.\n\n"
            "Write your answers to `answers.json` in the current working directory, as one JSON object that maps each question id to its answer. "
            "A location is written as `path:line`, with the path relative to `stdlib/` (for example `http/server.py:123`) and a 1-based line number. "
            "A line number refers to the line that holds the `def` or `class` keyword.\n\n")
        body = "\n".join("- %s: %s" % (s["id"], questions.text(s)) for s in chosen)
        with open(os.path.join(HERE, "prompt.md"), "w", encoding="utf-8") as fh:
            fh.write(head + body + "\n")
        print("%d questions" % len(chosen))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
