#!/usr/bin/env python3
"""Fixture, stage prompts, graders and the recall key for the long-session calibration (see PROTOCOL.md).

Everything here reads the scaffolds under ../compare/tasks by path and never changes them. Python 3.8, standard
library only. Nothing in a stage prompt mentions the experiment, its price or any limit.

    stages.py verify-key        re-derive the recall key from the fixtures and report any mismatch
"""

import sys

sys.dont_write_bytecode = True  # the task directories belong to compare; importing from them must leave nothing behind

import ast
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent / "compare" / "tasks"
LOOKUP, FIX, OPS = TASKS / "lookup", TASKS / "fix", TASKS / "ops"
SITES = ("north-7", "south-2", "east-5")

DELEGATE_CLAUSE = ("Delegate reading files, searching and running tests to subagents through the Agent tool "
                   "(mitosis-orchestrator:flash-lite-scout for single lookups, mitosis-orchestrator:flash-worker for the rest), "
                   "and keep only their short reports here.")
FORBIDDEN_WORDS = re.compile(r"\b(cost|costs|price|budget|experiment|calibration)\b", re.I)

BATCHES = {1: ("q01", "q10"), 2: ("q11", "q20"), 3: ("q21", "q30")}

# Where the six root causes live in the visible suite (found by applying each reference fix alone, then in sequence):
# the money helpers and the shipping tiers explain every failure in the order, discount, invoice and refund tests;
# the stock reservation then shows up in the fulfilment tests (it is masked by the shipping tiers) and the calendar
# explains the renewal dates; the receivables ledger and the currency conversion are what is left.
FIX_ROUNDS = {
    4: ("Round 1 of 3. Start with the failing tests in `test_orders`, `test_discounts`, `test_invoicing` and `test_returns` "
        "(order pricing, discounts, invoices and refunds). Leave the other failing tests for the later rounds. "
        "When those four test modules pass, stop and say in a few lines what was wrong and what you changed."),
    5: ("Round 2 of 3. The first round is done. Now take the failing tests in `test_fulfilment` (reserving, shipping and "
        "cancelling orders) and the renewal and due-date tests in `test_ext_services` (class `SubscriptionTest`). Leave the "
        "rest for the last round. When those pass, stop and say in a few lines what was wrong and what you changed."),
    6: ("Round 3 of 3. Keep going on whatever still fails until the whole visible suite passes. "
        "Then stop and say in a few lines what was wrong in this round and what you changed."),
}
FIX_HEAD = ("The Python package in `fix/shop/` runs a small shop: orders, pricing, fulfilment, invoicing, returns, subscriptions and "
            "receivables. Its tests are in `fix/tests/`; run them with `python3 -m unittest discover -s tests` from the `fix/` directory. "
            "Find the root causes and fix them in the package source, not by special-casing results at the point where the failures show up. "
            "Do not edit anything under `fix/tests/`.\n\n")

OPS_SITE_STAGE = {7: "north-7", 8: "south-2", 9: "east-5"}
OPS_HEAD = ("The directory `ops/` holds the setup for three sites of a small service: north-7, south-2 and east-5. Every path below is "
            "relative to `ops/` (so `templates/app.conf.tmpl` is `ops/templates/app.conf.tmpl`). Use only files in `ops/`: no network, "
            "no sudo, nothing outside it. Use python3 for anything that needs SQLite (there is no sqlite3 command line tool). "
            "The sites are done one per stage, in the order north-7, south-2, east-5, and each site is finished before the next one starts.\n\n")
OPS_SITE_STEPS = """For site `{site}`:

1. Render `templates/app.conf.tmpl` into `conf/{site}.conf`. Replace each `@@KEY@@` placeholder: SITE, ENV, RETENTION_DAYS, DB_PATH and LOG_PATH come from the matching keys of `params/{site}.json`; BUILD_ID is the first 12 hex characters of the sha256 of the `params/{site}.json` file; UPSTREAM_BUILD is {upstream}. Leave every other line as it is.
2. Create the SQLite database at the `db_path` named in `conf/{site}.conf` (create parent directories as needed). In it create `schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`, then apply every file in `migrations/` in numeric order. After each file, insert its numeric prefix (for example `001`) as `version`, with the current UTC time in ISO 8601 form as `applied_at`.
3. Import `data/{site}.csv` into the `devices` table (columns serial, model, site, qty; the first line of the file names its columns). If a serial appears more than once in the file, keep only the row with the largest qty (the earlier row if they are equal). Load all rows or none.
4. Rotate the log named by `log_path` in `conf/{site}.conf`: move it to the same name with `.1` added; create a new log at `log_path` whose only line is `rotated build=<BUILD_ID> devices=<N>`, where BUILD_ID is the build id in `conf/{site}.conf` and N is the number of rows now in that site's `devices`; write `<log_path>.1.sha256` holding the sha256 of the rotated file in sha256sum format (`<hex>  <file name without directory>`).

When the site is done, reply with its build id and device count.
"""
OPS_UPSTREAM = {
    "north-7": "`none`",
    "south-2": "the build id printed in the single line of north-7's new log (step 4)",
    "east-5": "the build id printed in the single line of south-2's new log (step 4)",
}
OPS_FINAL = """The three sites are set up. Now, in `ops/`:

5. Write `out/summary.json`, a JSON object with `sites` (for each site name an object with `build_id`, `upstream_build`, `migrations` as the applied versions in order, `device_count`, `total_qty` as the sum of qty over that site's devices, and `log_sha256` as the hex digest from step 4), `total_devices` and `total_qty` over all sites.
6. Write `out/MANIFEST.sha256` with one sha256sum-format line (`<hex>  <path>`) for each of: `out/summary.json` and, for every site, `conf/<site>.conf`, the database file, the new log, the rotated log and its `.sha256` file. Paths are relative to `ops/` and the lines are sorted by path.
7. Run `sha256sum -c out/MANIFEST.sha256` from `ops/` and confirm every line says OK. Do not change any listed file afterwards.

"""
CHANGE_REQUEST = """Then a change request that touches earlier work. Do not redo the earlier stages; make exactly these two changes.

- In `lookup/answers.json` add one more key, `x01`, holding the integer sum of your answers to q08 (the default of `flushLevel` in `MemoryHandler.__init__`) and q19 (the default of `limit` in `create_subprocess_shell`). Keep every other entry as it is.
- Create `fix/CHANGES.txt` listing the names of the public functions defined at module level in `fix/shop/money.py` (names that do not start with an underscore), one per line, sorted alphabetically.

Reply with the value of `x01` and the number of lines in `fix/CHANGES.txt`.
"""

RECALL_QUESTIONS = (
    ("r1", "q05", "Lookup question q05 asked where the `ident` that instances of class `Process` in `multiprocessing/context.py` use is defined. "
                  "Give the `path:line` of that `def`."),
    ("r2", "q14", "Lookup question q14 asked about the plain calls in `StreamReaderProtocol.connection_made` in `asyncio/streams.py`: "
                  "exactly one goes to a function defined in another file of the tree. Give the `path:line` of that function's `def`."),
    ("r3", "q27", "Lookup question q27 asked about the plain calls in `Pool._handle_results` in `multiprocessing/pool.py`: "
                  "exactly one goes to a function defined in another file of the tree. Give the `path:line` of that function's `def`."),
    ("r4", None, "How many tests does the visible suite in `fix/tests/` run? Give an integer."),
    ("r5", None, "What is the BUILD_ID of site south-2? Give the 12 hex characters."),
    ("r6", None, "How many rows does the `devices` table of site east-5 hold after the import? Give an integer."),
)
RECALL_HEAD = ("A few questions about the work done earlier in this session (the lookup questions, the shop package and the three sites). "
               "Answer from what you already know from the earlier stages; where you do not know a value, look it up in the files in the "
               "current working directory. Do not change any file other than the one named below.\n\n"
               "Write `recall.json` in the current working directory, one JSON object mapping each id below to its answer.\n\n")


# ---------------------------------------------------------------------------------------------
# stage table and prompts
# ---------------------------------------------------------------------------------------------

def _stage_table():
    rows = []
    for n in (1, 2, 3):
        rows.append({"n": n, "name": "lookup batch %d" % n, "kind": "lookup"})
    for n in (4, 5, 6):
        rows.append({"n": n, "name": "fix round %d" % (n - 3), "kind": "fix"})
    for n in (7, 8, 9):
        rows.append({"n": n, "name": "ops " + OPS_SITE_STAGE[n], "kind": "ops_site"})
    rows.append({"n": 10, "name": "ops steps 5-7 and change request", "kind": "ops_final"})
    rows.append({"n": 11, "name": "recall", "kind": "recall"})
    return rows


STAGES = _stage_table()
MAX_STAGES = len(STAGES)
# `--stages N` runs stages 1..N. Cutting removes from the end: 11 recall, 10 ops steps 5-7 plus the change request
# (ops is then not graded), 9, 8, 7 (the other ops sites), 6, 5, 4 (fix rounds; the final fix grade is lost with 6),
# then lookup batches 3, 2, 1.
REMOVAL_ORDER = tuple(range(MAX_STAGES, 0, -1))


def lookup_questions():
    """qid -> question line, as in the lookup task's own prompt."""
    out = {}
    with open(str(LOOKUP / "prompt.md"), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^- (q\d\d): (.*)$", line.rstrip("\n"))
            if m:
                out[m.group(1)] = m.group(2)
    return out


def lookup_key():
    with open(str(LOOKUP / "key.json"), encoding="utf-8") as fh:
        return json.load(fh)


def batch_ids(n):
    lo, hi = BATCHES[n]
    return ["q%02d" % i for i in range(int(lo[1:]), int(hi[1:]) + 1)]


def stage_body(n):
    """The stage prompt without the delegation clause."""
    if n in BATCHES:
        qs = lookup_questions()
        head = ("The directory `lookup/stdlib/` is a read-only copy of eleven packages of a Python standard library: `asyncio`, "
                "`concurrent`, `email`, `http`, `importlib`, `json`, `logging`, `multiprocessing`, `unittest`, `urllib` and `xml`. "
                "Answer the questions below from the files in that tree only. Do not answer from memory: the copy is the reference "
                "and exact values matter.\n\n"
                "Write your answers to `lookup/answers.json`, as one JSON object that maps each question id to its answer. If the file "
                "already exists, keep the entries that are in it and add the new ones. A location is written as `path:line`, with the path "
                "relative to `lookup/stdlib/` (for example `http/server.py:123`) and a 1-based line number. A line number refers to the line "
                "that holds the `def` or `class` keyword.\n\n")
        body = "\n".join("- %s: %s" % (q, qs[q]) for q in batch_ids(n))
        tail = ("\n\nWhen the answers are written, add `notes/stage-%d.md` (a few lines) saying what you found and how you found it." % n)
        return head + body + tail + "\n"
    if n in FIX_ROUNDS:
        return FIX_HEAD + FIX_ROUNDS[n] + "\n"
    if n in OPS_SITE_STAGE:
        site = OPS_SITE_STAGE[n]
        return OPS_HEAD + OPS_SITE_STEPS.format(site=site, upstream=OPS_UPSTREAM[site])
    if n == 10:
        return OPS_HEAD + OPS_FINAL + CHANGE_REQUEST
    if n == 11:
        lines = ["- %s: %s" % (rid, text) for rid, _q, text in RECALL_QUESTIONS]
        return RECALL_HEAD + "\n".join(lines) + "\n"
    raise ValueError("no stage %s" % n)


def stage_prompt(n, arm):
    body = stage_body(n)
    if arm == "delegate":
        return body.rstrip("\n") + "\n\n" + DELEGATE_CLAUSE + "\n"
    if arm == "off":
        return body
    raise ValueError("unknown arm " + str(arm))


def allowed_tools():
    """Union of the three tasks' lists in order (the notes need Write, which every list has)."""
    out = []
    for task in ("lookup", "fix", "ops"):
        with open(str(TASKS / task / "allowed_tools.txt"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and line not in out:
                    out.append(line)
    return out


# ---------------------------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------------------------

def build_workspace(ws):
    """One workspace with lookup/, fix/ and ops/ made by the existing scaffolds, and an empty notes/."""
    ws = Path(ws)
    ws.mkdir(parents=True, exist_ok=True)
    for task in ("lookup", "fix", "ops"):
        proc = subprocess.run([sys.executable, "-B", str(TASKS / task / "scaffold.py"), str(ws / task)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if proc.returncode != 0:
            raise RuntimeError("scaffold for %s failed (exit %d): %s" % (task, proc.returncode, proc.stderr.strip()[:600]))
    (ws / "notes").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------------------------
# graders
# ---------------------------------------------------------------------------------------------

_LOOKUP_GRADE = []


def _lookup_grade_module():
    if not _LOOKUP_GRADE:
        sys.path.insert(0, str(LOOKUP))
        try:
            spec = importlib.util.spec_from_file_location("long_lookup_grade", str(LOOKUP / "grade.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(LOOKUP))
        _LOOKUP_GRADE.append(mod)
    return _LOOKUP_GRADE[0]


def grade_lookup_batch(ws, n):
    """Existing key and comparison, restricted to the ten questions of batch n."""
    mod = _lookup_grade_module()
    key = lookup_key()
    ids = batch_ids(n)
    work = Path(ws) / "lookup"
    intact = (work / "stdlib").is_dir() and not mod.scaffold.verify_tree(str(work / "stdlib"))
    answers = {}
    path = work / "answers.json"
    if path.is_file():
        try:
            with open(str(path), encoding="utf-8") as fh:
                answers = json.load(fh)
        except ValueError:
            answers = {}
    if not isinstance(answers, dict):
        answers = {}
    wrong = [q for q in ids if q not in answers or not mod.same(answers[q], key[q])]
    return {"kind": "lookup", "correct": len(ids) - len(wrong), "total": len(ids), "wrong_ids": wrong, "stdlib_intact": bool(intact),
            "ok": (not wrong) and bool(intact), "label": "%d/%d" % (len(ids) - len(wrong), len(ids))}


def visible_failures(ws):
    """(failing, ran) of the pristine visible tests against ws/fix/shop; failing is None if the run could not be parsed."""
    work = str(Path(ws) / "fix")
    env = dict(os.environ, PYTHONPATH=work, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(FIX / "template" / "tests")], cwd=work, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=180)
    ran = re.search(r"^Ran (\d+) tests?", proc.stdout, re.M)
    if not ran:
        return None, None
    bad = re.search(r"^FAILED \((.*)\)", proc.stdout, re.M)
    failing = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", bad.group(1))) if bad else 0
    return failing, int(ran.group(1))


def grade_fix(ws, n):
    failing, ran = visible_failures(ws)
    res = {"kind": "fix", "visible_failing": failing, "visible_ran": ran}
    if n == 6:
        proc = subprocess.run([sys.executable, "-B", str(FIX / "grade.py"), str(Path(ws) / "fix")], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        res.update({"graded": True, "ok": proc.returncode == 0, "label": "hidden+visible " + ("pass" if proc.returncode == 0 else "fail")})
    else:
        res.update({"graded": False, "ok": None, "label": "%s failing" % ("?" if failing is None else failing)})
    return res


def grade_ops(ws):
    proc = subprocess.run([sys.executable, "-B", str(OPS / "grade.py"), str(Path(ws) / "ops")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    m = re.search(r"ops: (\d+) problem", proc.stdout)
    return {"ok": proc.returncode == 0, "problems": int(m.group(1)) if m else None}


def money_public_names(text=None):
    """Names of module-level functions in shop/money.py that do not start with an underscore, sorted."""
    if text is None:
        with open(str(FIX / "template" / "shop" / "money.py"), encoding="utf-8") as fh:
            text = fh.read()
    return sorted(node.name for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"))


def change_key():
    key = lookup_key()
    return {"x01": int(key["q08"]) + int(key["q19"]), "changes": money_public_names()}


def check_change(ws):
    """The small new check of stage 10: x01 in lookup/answers.json and fix/CHANGES.txt."""
    want = change_key()
    problems = []
    try:
        with open(str(Path(ws) / "lookup" / "answers.json"), encoding="utf-8") as fh:
            got = json.load(fh).get("x01")
        if isinstance(got, str):
            got = int(got.strip())
        if got != want["x01"] or isinstance(got, bool):
            problems.append("x01")
    except (OSError, ValueError, AttributeError):
        problems.append("x01")
    try:
        with open(str(Path(ws) / "fix" / "CHANGES.txt"), encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        if lines != want["changes"]:
            problems.append("CHANGES.txt")
    except OSError:
        problems.append("CHANGES.txt")
    return {"ok": not problems, "problems": problems}


def grade_ops_final(ws):
    ops, change = grade_ops(ws), check_change(ws)
    return {"kind": "ops_final", "ops_ok": ops["ok"], "ops_problems": ops["problems"], "change_ok": change["ok"], "change_problems": change["problems"],
            "ok": ops["ok"] and change["ok"], "label": "ops %s, change %s" % ("ok" if ops["ok"] else "fail", "ok" if change["ok"] else "fail")}


def grade_recall(ws):
    key = recall_key()
    mod = _lookup_grade_module()
    answers = {}
    path = Path(ws) / "recall.json"
    if path.is_file():
        try:
            with open(str(path), encoding="utf-8") as fh:
                answers = json.load(fh)
        except ValueError:
            answers = {}
    if not isinstance(answers, dict):
        answers = {}
    wrong = [r for r in sorted(key) if r not in answers or not mod.same(answers[r], key[r])]
    ok_n = len(key) - len(wrong)
    return {"kind": "recall", "correct": ok_n, "total": len(key), "wrong_ids": wrong, "ok": not wrong, "label": "%d/%d" % (ok_n, len(key))}


def grade_stage(ws, n):
    """Grade stage n against its fixture. Ops sites are graded at stage 10, fix rounds 1 and 2 only count failing visible tests."""
    kind = STAGES[n - 1]["kind"]
    if kind == "lookup":
        return grade_lookup_batch(ws, n)
    if kind == "fix":
        return grade_fix(ws, n)
    if kind == "ops_site":
        return {"kind": "ops_site", "ok": None, "label": "graded at stage 10"}
    if kind == "ops_final":
        return grade_ops_final(ws)
    return grade_recall(ws)


# ---------------------------------------------------------------------------------------------
# recall key, derived from the fixtures
# ---------------------------------------------------------------------------------------------

def ops_facts():
    """site -> {"build_id", "device_count"} from the pristine ops inputs (sha256 of the params file, de-duplicated CSV)."""
    out = {}
    for site in SITES:
        raw = (OPS / "template" / "params" / (site + ".json")).read_bytes()
        best = {}
        with open(str(OPS / "template" / "data" / (site + ".csv")), newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                q = int(row["qty"])
                if row["serial"] not in best or q > best[row["serial"]]:
                    best[row["serial"]] = q
        out[site] = {"build_id": hashlib.sha256(raw).hexdigest()[:12], "device_count": len(best)}
    return out


def visible_test_count():
    """Number of tests of the visible suite, counted by the unittest loader in a child process."""
    code = ("import sys,unittest;sys.dont_write_bytecode=True;sys.path.insert(0,%r);"
            "print(unittest.defaultTestLoader.discover(%r).countTestCases())" % (str(FIX / "template"), str(FIX / "template" / "tests")))
    proc = subprocess.run([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return int(proc.stdout.strip())


def recall_key():
    key = lookup_key()
    facts = ops_facts()
    out = {}
    for rid, qid, _text in RECALL_QUESTIONS:
        if qid:
            out[rid] = key[qid]
    out["r4"] = visible_test_count()
    out["r5"] = facts["south-2"]["build_id"]
    out["r6"] = facts["east-5"]["device_count"]
    return out


def verify_recall_key():
    """Cross-check the key against independent sources. Returns a list of problems (empty means verified)."""
    problems = []
    key = recall_key()
    with open(str(LOOKUP / "reference" / "answers.json"), encoding="utf-8") as fh:
        reference = json.load(fh)
    if reference != lookup_key():
        problems.append("lookup reference answers differ from key.json")
    questions = lookup_questions()
    anchors = {"q05": ("`Process`", "multiprocessing/context.py", "ident"), "q14": ("StreamReaderProtocol.connection_made", "asyncio/streams.py"),
               "q27": ("Pool._handle_results", "multiprocessing/pool.py")}
    for rid, qid, text in RECALL_QUESTIONS:
        if qid:
            for a in anchors[qid]:
                if a not in text:
                    problems.append("%s text lacks %s" % (rid, a))
                if a.strip("`") not in questions[qid] and a.strip("`").split(".")[-1] not in questions[qid]:
                    problems.append("%s: %s is not in the lookup prompt line of %s" % (rid, a, qid))
            if not re.match(r"^[\w/]+\.py:\d+$", str(key[rid])):
                problems.append("%s is not a path:line" % rid)
    scratch = Path(tempfile.mkdtemp(prefix="long-verify-"))
    try:
        work = scratch / "ops"
        subprocess.run([sys.executable, "-B", str(OPS / "scaffold.py"), str(work)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "-B", str(OPS / "reference" / "solve.py"), str(work)], check=True, stdout=subprocess.DEVNULL,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        with open(str(work / "out" / "summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        if summary["sites"]["south-2"]["build_id"] != key["r5"]:
            problems.append("r5 differs from the reference solution's summary")
        if summary["sites"]["east-5"]["device_count"] != key["r6"]:
            problems.append("r6 differs from the reference solution's summary")
        fix_work = scratch / "fix"
        shutil.copytree(str(FIX / "template" / "shop"), str(fix_work / "shop"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        proc = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(FIX / "template" / "tests")], cwd=str(fix_work),
                              env=dict(os.environ, PYTHONPATH=str(fix_work), PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
        m = re.search(r"^Ran (\d+) tests?", proc.stdout, re.M)
        if not m or int(m.group(1)) != key["r4"]:
            problems.append("r4 differs from the count printed by unittest")
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    with open(str(FIX / "reference" / "A" / "money.py"), encoding="utf-8") as fh:
        if money_public_names(fh.read()) != money_public_names():
            problems.append("the reference money.py has other public functions than the template (change check)")
    return problems


def main(argv):
    if argv[1:] == ["verify-key"]:
        problems = verify_recall_key()
        for p in problems:
            print("PROBLEM " + p)
        print("recall key: %s" % ("verified" if not problems else "%d problem(s)" % len(problems)))
        return 1 if problems else 0
    print("usage: stages.py verify-key", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
