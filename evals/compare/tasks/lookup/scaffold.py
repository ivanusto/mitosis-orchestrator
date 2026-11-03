#!/usr/bin/env python3
"""Create the lookup working directory: python3 scaffold.py WORKDIR.

Copies the *.py files of eleven packages (see PACKAGES) from this machine's Python standard
library into WORKDIR/stdlib, verifies every file against stdlib.manifest.json (sha256), and makes
the copy read-only. A mismatch (different Python build, missing or extra file) exits 2 and removes
the directory, so the harness refuses to run instead of asking questions about a different tree.

    python3 scaffold.py --make-manifest    rewrite stdlib.manifest.json from the local stdlib
"""

import hashlib
import json
import os
import shutil
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "stdlib.manifest.json")
PACKAGES = ("asyncio", "concurrent", "email", "http", "importlib", "json", "logging", "multiprocessing", "unittest", "urllib", "xml")


def stdlib_root():
    return os.path.dirname(os.path.abspath(os.__file__))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_files(root):
    """Relative posix paths of every *.py below the four packages in root."""
    out = []
    for pkg in PACKAGES:
        for base, dirs, files in os.walk(os.path.join(root, pkg)):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "test", "tests"))
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return sorted(out)


def digest_tree(root):
    return {rel: sha256_file(os.path.join(root, *rel.split("/"))) for rel in list_files(root)}


def load_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def verify_tree(root):
    """Return a list of problems comparing root/{packages} against the manifest (empty means identical)."""
    want = load_manifest()["files"]
    got = digest_tree(root)
    problems = ["missing: " + p for p in sorted(set(want) - set(got))]
    problems += ["extra: " + p for p in sorted(set(got) - set(want))]
    problems += ["changed: " + p for p in sorted(set(want) & set(got)) if want[p] != got[p]]
    return problems


def make_readonly(root):
    for base, dirs, files in os.walk(root):
        for name in files:
            os.chmod(os.path.join(base, name), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    for base, dirs, files in os.walk(root, topdown=False):
        os.chmod(base, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def make_writable(root):
    for base, dirs, files in os.walk(root):
        os.chmod(base, 0o755)
        for name in files:
            os.chmod(os.path.join(base, name), 0o644)


def main(argv):
    if argv[1:] == ["--make-manifest"]:
        data = {"python": sys.version.split()[0], "files": digest_tree(stdlib_root())}
        with open(MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print("manifest: %d files, python %s" % (len(data["files"]), data["python"]))
        return 0
    if len(argv) != 2:
        print("usage: scaffold.py WORKDIR", file=sys.stderr)
        return 2
    work = os.path.abspath(argv[1])
    if os.path.exists(work) and os.listdir(work):
        print("scaffold: %s exists and is not empty" % work, file=sys.stderr)
        return 2
    target = os.path.join(work, "stdlib")
    os.makedirs(target, exist_ok=True)
    root = stdlib_root()
    for rel in list_files(root):
        dst = os.path.join(target, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(root, *rel.split("/")), dst)
    problems = verify_tree(target)
    if problems:
        print("scaffold: the local standard library does not match stdlib.manifest.json (pinned python %s, running %s)"
              % (load_manifest().get("python"), sys.version.split()[0]), file=sys.stderr)
        for p in problems[:10]:
            print("  " + p, file=sys.stderr)
        make_writable(work)
        shutil.rmtree(work, ignore_errors=True)
        return 2
    make_readonly(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
