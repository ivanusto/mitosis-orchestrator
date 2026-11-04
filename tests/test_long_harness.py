#!/usr/bin/env python3
"""Tests for evals/long: commands, clause, context growth, dedupe, cost modes, caps, ledger, resume, recall key, g0.

No real CLI is ever started: a fake `claude` executable on PATH plays the part. Nothing here spends money.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
COMPARE = REPO / "evals" / "compare"
OVERHEAD = REPO / "evals" / "overhead"
LONG = REPO / "evals" / "long"


def snapshot(root):
    """Every file under root (bytecode included) -> sha256."""
    out = {}
    for base, dirs, files in os.walk(str(root)):
        dirs.sort()
        for name in sorted(files):
            full = Path(base) / name
            out[full.relative_to(root).as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


COMPARE_BEFORE, OVERHEAD_BEFORE = snapshot(COMPARE), snapshot(OVERHEAD)  # taken before this module imports anything from evals

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("long_run_under_test", str(LONG / "run.py"))
lr = importlib.util.module_from_spec(_spec)
sys.modules["long_run_under_test"] = lr
_spec.loader.exec_module(lr)
st = lr.stages

POSIX_ONLY = unittest.skipIf(os.name == "nt", "the harness and its fake claude are POSIX-only")


def rmtree(path):
    for base, dirs, files in os.walk(str(path)):
        try:
            os.chmod(base, 0o755)
        except OSError:
            pass
        for name in files:
            try:
                os.chmod(os.path.join(base, name), 0o644)
            except OSError:
                pass
    shutil.rmtree(str(path), ignore_errors=True)


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)


def assistant(mid, usage, blocks=None, parent=None, model="claude-opus-5-5"):
    u = {"input_tokens": usage[0], "cache_creation_input_tokens": usage[1], "cache_read_input_tokens": usage[2], "output_tokens": usage[3]}
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": mid, "model": model, "content": blocks or [], "usage": u}}


# ---------------------------------------------------------------------------------------------
# pure pieces
# ---------------------------------------------------------------------------------------------

class PureTests(unittest.TestCase):
    def test_growth_skips_the_drop_of_a_compaction_reset(self):
        self.assertEqual(lr.growth([100, 200, 50, 150]), 200)   # +100, drop skipped, +100
        self.assertEqual(lr.growth([100, 200, 300]), 200)
        self.assertEqual(lr.growth([300, 100]), 0)
        self.assertEqual(lr.growth([]), 0)

    def test_growth_carries_over_a_stage_boundary(self):
        self.assertEqual(lr.growth([150, 250], prev=200), 100)       # 150 is a drop (skipped), then +100
        self.assertEqual(lr.growth([50, 80], prev=200), 30)      # the reset happened between stages

    def test_dedupe_by_message_id_keeps_max_tokens_and_lists_tools_once(self):
        tool = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "SECRET-CONTENT"}}
        events = [
            assistant("m1", [10, 0, 100, 1], [{"type": "text", "text": "words"}]),
            assistant("m1", [10, 0, 100, 40], [tool]),
            assistant("m1", [10, 0, 100, 40], [tool]),
            assistant("m2", [5, 20, 100, 3], parent="agent-1"),
            assistant("m3", [0, 0, 0, 0], model="<synthetic>"),
            {"type": "result"},
        ]
        calls = lr.extract_calls(events)
        self.assertEqual([c["message_id"] for c in calls], ["m1", "m2"])
        self.assertEqual(calls[0]["output"], 40)
        self.assertEqual(calls[0]["tools"], ["Read"])
        self.assertTrue(calls[1]["sub"])
        self.assertNotIn("SECRET-CONTENT", json.dumps(calls))
        self.assertEqual(lr.primary_contexts(calls), [110])

    def test_cli_cost_handling_both_ways(self):
        self.assertAlmostEqual(lr.cli_stage_cost("per_stage", 0.3, 0.5), 0.3)
        self.assertAlmostEqual(lr.cli_stage_cost("cumulative", 0.8, 0.5), 0.3)
        self.assertEqual(lr.cli_stage_cost("cumulative", 0.2, 0.5), 0.0)       # never negative
        self.assertAlmostEqual(lr.cli_stage_cost("cumulative", 0.4, None), 0.4)
        self.assertIsNone(lr.cli_stage_cost("cumulative", None, 0.5))
        self.assertAlmostEqual(lr.transcript_stage_cost(0.9, 0.5, True), 0.4)    # the file holds the stage before: take it off
        self.assertAlmostEqual(lr.transcript_stage_cost(0.9, 0.5, False), 0.9)
        self.assertAlmostEqual(lr.effective_cost(0.2, 0.3, 4), 0.3)
        self.assertAlmostEqual(lr.effective_cost(None, None, 4), 4.0)

    def test_decide_cli_mode(self):
        self.assertEqual(lr.decide_cli_mode(0.30, 0.50, 0.20), "cumulative")
        self.assertEqual(lr.decide_cli_mode(0.30, 0.20, 0.20), "per_stage")
        self.assertEqual(lr.decide_cli_mode(0.30, 0.10, None), "per_stage")      # a fall rules out a running total
        self.assertIsNone(lr.decide_cli_mode(0.001, 0.20, 0.20))                 # cannot tell
        self.assertIsNone(lr.decide_cli_mode(None, 0.2, 0.2))

    def test_estimate_is_cjk_aware(self):
        self.assertEqual(lr.est_tokens("a" * 40), 10)
        self.assertEqual(lr.est_tokens("測試" * 10), 30)
        self.assertEqual(lr.est_tokens(""), 0)

    def test_tool_output_stats_counts_primary_displaceable_results_and_reports(self):
        events = [
            assistant("m1", [1, 0, 1, 1], [{"type": "tool_use", "id": "r1", "name": "Read", "input": {}}, {"type": "tool_use", "id": "a1", "name": "Agent", "input": {}},
                                          {"type": "tool_use", "id": "e1", "name": "Edit", "input": {}}]),
            assistant("m2", [1, 0, 1, 1], [{"type": "tool_use", "id": "s1", "name": "Bash", "input": {}}], parent="a1"),
            {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": "x" * 400},
                {"type": "tool_result", "tool_use_id": "a1", "content": [{"type": "text", "text": "y" * 80}]},
                {"type": "tool_result", "tool_use_id": "e1", "content": "z" * 4000}]}},
            {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [{"type": "tool_result", "tool_use_id": "s1", "content": "q" * 4000}]}},
        ]
        s = lr.tool_output_stats(events)
        self.assertEqual(s["displaceable_by_tool"], {"Read": 100})
        self.assertEqual(s["report_tokens"], 20)

    def test_agent_evidence_tells_synchronous_from_background(self):
        use = assistant("m1", [1, 0, 1, 1], [{"type": "tool_use", "id": "a1", "name": "Agent", "input": {}}])
        sub = assistant("m2", [1, 0, 1, 1], parent="a1")

        def back(text):
            return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a1", "content": text}]}}
        sync = lr.agent_evidence([use, sub, back("2 lines"), {"type": "result"}])
        self.assertEqual((sync["agent_calls"], sync["returned"], sync["returned_before_result"], sync["background_marked"], sync["subagent_events"]), (1, 1, 1, 0, 1))
        bg = lr.agent_evidence([use, back("Async agent launched successfully"), {"type": "result"}, sub])
        self.assertEqual(bg["background_marked"], 1)
        late = lr.agent_evidence([use, {"type": "result"}, back("late")])
        self.assertEqual(late["returned_before_result"], 0)

    def test_compaction_events_keep_trigger_and_pretokens_only(self):
        events = [{"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto", "pre_tokens": 170000, "summary": "SECRET"}},
                  {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "manual", "preTokens": 5}}]
        self.assertEqual(lr.compaction_events(events), [{"trigger": "auto", "pre_tokens": 170000}, {"trigger": "manual", "pre_tokens": 5}])

    def test_arm_state(self):
        base = {"plugins": [], "agents": ["general-purpose"], "skills": [], "slash_commands": []}
        self.assertTrue(lr.arm_state(base, "off")[1])
        loaded = dict(base, plugins=[{"name": "mitosis-orchestrator"}], agents=["mitosis-orchestrator:flash-worker", "mitosis-orchestrator:flash-lite-scout"])
        state, ok, _ = lr.arm_state(loaded, "delegate")
        self.assertTrue(ok)
        self.assertTrue(state["plugin_loaded"] and state["agents_loaded"] and state["scout_loaded"])
        self.assertFalse(lr.arm_state(loaded, "off")[1])
        self.assertFalse(lr.arm_state(base, "delegate")[1])
        self.assertFalse(lr.arm_state(None, "off")[1])

    def test_commands_per_arm_and_stage(self):
        off1 = lr.build_command("claude", "off", "P", "/frozen", 4)
        off2 = lr.build_command("claude", "off", "P", "/frozen", 4, resume="sess-1")
        dl1 = lr.build_command("claude", "delegate", "P", "/frozen", 4)
        dl2 = lr.build_command("claude", "delegate", "P", "/frozen", 4, resume="sess-1")
        for cmd in (off1, off2):
            self.assertNotIn("--plugin-dir", cmd)
        for cmd in (dl1, dl2):
            self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], "/frozen")
        self.assertNotIn("--resume", off1)
        self.assertNotIn("--resume", dl1)
        self.assertEqual(off2[off2.index("--resume") + 1], "sess-1")
        self.assertEqual(dl2[dl2.index("--resume") + 1], "sess-1")
        self.assertEqual(off1[:2], ["claude", "-p"])
        for flag, value in (("--model", "opus"), ("--effort", "medium"), ("--permission-mode", "acceptEdits"), ("--max-budget-usd", "4"),
                            ("--output-format", "stream-json"), ("--settings", lr.cmp.SETTINGS_JSON)):
            self.assertEqual(off1[off1.index(flag) + 1], value)
        for flag in ("--verbose", "--strict-mcp-config"):
            self.assertIn(flag, off1)
        tools = off1[off1.index("--allowedTools") + 1].split(",")
        for need in ("Agent", "Write", "Edit", "Read", "Bash(python3:*)", "Bash(mkdir:*)", "Bash(git:*)"):
            self.assertIn(need, tools)
        self.assertEqual(len(tools), len(set(tools)))
        # the arms differ only by --plugin-dir
        stripped = list(dl2)
        i = stripped.index("--plugin-dir")
        del stripped[i:i + 2]
        self.assertEqual(stripped, off2)


class PromptTests(unittest.TestCase):
    def test_clause_only_in_the_delegate_arm_and_always_last(self):
        for n in range(1, st.MAX_STAGES + 1):
            off, dl = st.stage_prompt(n, "off"), st.stage_prompt(n, "delegate")
            self.assertNotIn("Delegate reading files", off)
            self.assertNotIn("mitosis", off.lower())
            self.assertTrue(dl.rstrip("\n").endswith(st.DELEGATE_CLAUSE), n)
            self.assertEqual(dl.count(st.DELEGATE_CLAUSE), 1)
            self.assertEqual(dl.replace("\n\n" + st.DELEGATE_CLAUSE, "").rstrip("\n"), off.rstrip("\n"))
        self.assertEqual(st.DELEGATE_CLAUSE, "Delegate reading files, searching and running tests to subagents through the Agent tool "
                         "(mitosis-orchestrator:flash-lite-scout for single lookups, mitosis-orchestrator:flash-worker for the rest), "
                         "and keep only their short reports here.")

    def test_prompts_do_not_mention_cost_budget_or_the_experiment(self):
        bad = re.compile(r"\b(cost|costs|budget|experiment|calibration|price|cap)\b", re.I)
        for arm in lr.ARMS:
            for n in range(1, st.MAX_STAGES + 1):
                self.assertIsNone(bad.search(st.stage_prompt(n, arm)), (arm, n))

    def test_stage_table_and_removal_order(self):
        self.assertEqual(st.MAX_STAGES, 11)
        self.assertEqual([s["kind"] for s in st.STAGES], ["lookup"] * 3 + ["fix"] * 3 + ["ops_site"] * 3 + ["ops_final", "recall"])
        self.assertEqual(st.REMOVAL_ORDER, tuple(range(11, 0, -1)))
        text = (LONG / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("--stages", text)
        self.assertIn("NOT SEALED", text)

    def test_lookup_batches_cover_thirty_questions_in_order(self):
        ids = [q for n in (1, 2, 3) for q in st.batch_ids(n)]
        self.assertEqual(ids, ["q%02d" % i for i in range(1, 31)])
        for n in (1, 2, 3):
            for q in st.batch_ids(n):
                self.assertIn("- %s: " % q, st.stage_body(n))
            self.assertIn("notes/stage-%d.md" % n, st.stage_body(n))

    def test_fix_rounds_do_not_name_root_causes(self):
        modules = ("money.py", "shipping.py", "fulfilment.py", "calendar.py", "ledger.py", "currency.py", "allocate", "bisect", "round_div", "tier")
        for n in (4, 5, 6):
            body = st.stage_body(n)
            for word in modules:
                self.assertNotIn(word, body)

    def test_ops_sites_each_stage_one_site_in_order(self):
        for n, site in ((7, "north-7"), (8, "south-2"), (9, "east-5")):
            body = st.stage_body(n)
            self.assertIn("params/%s.json" % site, body)
            others = [s for s in st.SITES if s != site]
            for o in others:
                self.assertNotIn("params/%s.json" % o, body)
        self.assertIn("`none`", st.stage_body(7))
        self.assertIn("north-7's new log", st.stage_body(8))
        self.assertIn("`out/MANIFEST.sha256`", st.stage_body(10))
        self.assertIn("x01", st.stage_body(10))


class FixtureTests(unittest.TestCase):
    def test_recall_key_is_verified_from_the_fixtures(self):
        self.assertEqual(st.verify_recall_key(), [])
        key = st.recall_key()
        self.assertEqual(sorted(key), ["r1", "r2", "r3", "r4", "r5", "r6"])
        self.assertEqual(key["r4"], 151)
        self.assertRegex(key["r5"], r"^[0-9a-f]{12}$")
        self.assertEqual(len(st.RECALL_QUESTIONS), 6)

    def test_change_key_comes_from_the_fixtures(self):
        key = st.change_key()
        self.assertEqual(key["x01"], 40 + 65536)
        self.assertEqual(key["changes"], sorted(key["changes"]))
        self.assertIn("allocate", key["changes"])

    def test_workspace_has_three_subfolders_and_leaves_the_scaffolds_alone(self):
        before = snapshot(st.TASKS)
        tmp = Path(tempfile.mkdtemp(prefix="long-ws-"))
        self.addCleanup(rmtree, tmp)
        try:
            st.build_workspace(tmp / "ws")
        except RuntimeError as exc:
            self.skipTest("lookup scaffold cannot run with this Python: %s" % exc)
        for sub in ("lookup/stdlib", "fix/shop", "fix/tests", "ops/templates", "ops/migrations", "notes"):
            self.assertTrue((tmp / "ws" / sub).is_dir(), sub)
        self.assertEqual(snapshot(st.TASKS), before)

    def test_graders_on_reference_solutions(self):
        tmp = Path(tempfile.mkdtemp(prefix="long-grade-"))
        self.addCleanup(rmtree, tmp)
        ws = tmp / "ws"
        try:
            st.build_workspace(ws)
        except RuntimeError as exc:
            self.skipTest("lookup scaffold cannot run with this Python: %s" % exc)
        key = st.lookup_key()
        with open(str(ws / "lookup" / "answers.json"), "w") as fh:
            json.dump({q: key[q] for q in st.batch_ids(1)}, fh)
        self.assertEqual(st.grade_stage(ws, 1)["label"], "10/10")
        self.assertEqual(st.grade_stage(ws, 2)["label"], "0/10")
        self.assertFalse(st.grade_stage(ws, 2)["ok"])
        failing, ran = st.visible_failures(ws)
        self.assertEqual((failing, ran), (23, 151))
        self.assertEqual(st.grade_stage(ws, 4)["label"], "23 failing")
        self.assertFalse(st.grade_stage(ws, 6)["ok"])
        for letter, mod in (("A", "money"), ("B", "shipping"), ("C", "fulfilment"), ("D", "calendar"), ("E", "ledger"), ("F", "currency")):
            shutil.copy(str(st.FIX / "reference" / letter / (mod + ".py")), str(ws / "fix" / "shop" / (mod + ".py")))
        self.assertEqual(st.visible_failures(ws)[0], 0)
        self.assertTrue(st.grade_stage(ws, 6)["ok"])
        # ops: reference solution plus the change request
        subprocess.run([sys.executable, "-B", str(st.OPS / "reference" / "solve.py"), str(ws / "ops")], check=True, stdout=subprocess.DEVNULL)
        self.assertFalse(st.grade_stage(ws, 10)["ok"])        # change request not done yet
        self.assertTrue(st.grade_ops(ws)["ok"])
        answers = json.loads((ws / "lookup" / "answers.json").read_text())
        answers["x01"] = st.change_key()["x01"]
        (ws / "lookup" / "answers.json").write_text(json.dumps(answers))
        (ws / "fix" / "CHANGES.txt").write_text("\n".join(st.change_key()["changes"]) + "\n")
        self.assertTrue(st.grade_stage(ws, 10)["ok"])
        (ws / "fix" / "CHANGES.txt").write_text("allocate\n")
        self.assertEqual(st.check_change(ws)["problems"], ["CHANGES.txt"])
        # recall
        (ws / "recall.json").write_text(json.dumps(st.recall_key()))
        self.assertEqual(st.grade_stage(ws, 11)["label"], "6/6")
        bad = dict(st.recall_key(), r4=150)
        (ws / "recall.json").write_text(json.dumps(bad))
        self.assertEqual(st.grade_stage(ws, 11)["wrong_ids"], ["r4"])


# ---------------------------------------------------------------------------------------------
# fake claude and sandbox
# ---------------------------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys
cfg = json.load(open(os.environ["FAKE_CLAUDE_CFG"]))
argv = sys.argv[1:]
sys.stdin.read()
prompt = argv[argv.index("-p") + 1]
cwd = os.path.realpath(os.getcwd())
arm = "delegate" if "--plugin-dir" in argv else "off"
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
cap = float(argv[argv.index("--max-budget-usd") + 1])
log = cfg["log"]
prior = [json.loads(ln) for ln in open(log)] if os.path.exists(log) else []
mine = [p for p in prior if p["cwd"] == cwd]
call_no = len(mine) + 1
stage_no = len([p for p in mine if not p["died"]]) + 1
died = call_no in cfg.get("die_calls", [])
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": cwd, "arm": arm, "resume": resume, "call_no": call_no, "stage_no": stage_no, "died": died,
                         "prompt": prompt, "debug": os.environ.get("CLAUDE_MITOSIS_DEBUG"), "tmpdir": os.environ.get("TMPDIR"), "cap": cap}) + "\n")
if died:
    sys.exit(1)
if resume and not cfg.get("change_sid"):
    sid = resume
else:
    sid = "sess-%s-%d" % (arm, len(prior) + 1)
state_dir = os.path.join(os.path.dirname(log), "fake-state")
os.makedirs(state_dir, exist_ok=True)
sp = os.path.join(state_dir, sid + ".txt")
before = float(open(sp).read()) if os.path.exists(sp) else 0.0
costs = cfg.get("costs", {}).get(arm, [0.5])
cost = costs[min(stage_no, len(costs)) - 1]
total = before + cost if cfg.get("cumulative") else cost
open(sp, "w").write(str(before + cost))
usage = cfg.get("usage_by_stage", {}).get(str(stage_no)) or [[10, 500, 1000 * stage_no, 20], [10, 0, 1000 * stage_no + 400, 30]]
init = {"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "plugins": [], "agents": ["general-purpose"], "skills": ["update-config"], "slash_commands": ["compact"]}
if arm == "delegate":
    init["plugins"] = [{"name": "mitosis-orchestrator", "path": "/frozen"}]
    init["agents"] += ["mitosis-orchestrator:flash-worker", "mitosis-orchestrator:flash-lite-scout"]
    init["skills"].append("mitosis-orchestrator:orchestrate")
if cfg.get("wrong_state"):
    init["plugins"], init["agents"] = [], ["general-purpose"]
out = [init]
for i, u in enumerate(usage):
    uo = {"input_tokens": u[0], "cache_creation_input_tokens": u[1], "cache_read_input_tokens": u[2], "output_tokens": u[3]}
    tid = "tb%d-%d" % (stage_no, i)
    for blk in ({"type": "text", "text": "words"}, {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "ls"}}):
        out.append({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m%d-%d" % (stage_no, i), "model": "claude-opus-5-5", "content": [blk], "usage": uo}})
    out.append({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "x" * 400}]}})
if arm == "delegate" and cfg.get("agent", True):
    aid = "ag%d" % stage_no
    out.append({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "ma%d" % stage_no, "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "tool_use", "id": aid, "name": "Agent", "input": {"subagent_type": "mitosis-orchestrator:flash-lite-scout"}}]}})
    out.append({"type": "assistant", "parent_tool_use_id": aid, "message": {"id": "sub%d" % stage_no, "model": "claude-haiku-4-5", "content": [], "usage":
                {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
    text = "Async agent launched successfully" if cfg.get("background") else "STATUS: COMPLETED " * 5
    out.append({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result", "tool_use_id": aid, "content": text}]}})
if cfg.get("compact_stage") == stage_no:
    out.append({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto", "pre_tokens": 150000}})
sub = "error_max_budget_usd" if cost >= cap else "success"
out.append({"type": "result", "subtype": sub, "is_error": False, "session_id": sid, "total_cost_usd": total, "permission_denials": []})
if "lookup/answers.json" in prompt:
    ids = re.findall(r"^- (q\d\d):", prompt, re.M)
    ref = json.load(open(cfg["ref_answers"]))
    path = os.path.join("lookup", "answers.json")
    cur = json.load(open(path)) if os.path.exists(path) else {}
    cur.update({q: ("wrong" if q in cfg.get("wrong_ids", []) else ref[q]) for q in ids})
    json.dump(cur, open(path, "w"))
if "recall.json" in prompt and cfg.get("recall"):
    json.dump(cfg["recall"], open("recall.json", "w"))
tok = int(round(cost * 1e6 / cfg["transcript_price"])) if cfg.get("transcript_price") else cfg.get("transcript_output")
if tok:
    enc = "".join(c if c.isalnum() else "-" for c in cwd)
    proj = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", enc)
    os.makedirs(os.path.join(proj, sid, "subagents"), exist_ok=True)
    with open(os.path.join(proj, sid + ".jsonl"), "a") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-04T10:00:05.000Z", "message": {"id": "tp-%s-%d" % (sid, stage_no), "model": "claude-opus-5", "role": "assistant",
                 "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 0, "output_tokens": tok}}}) + "\n")
if arm == "delegate" and os.environ.get("CLAUDE_MITOSIS_DEBUG") == "1" and cfg.get("debug_log"):
    d = os.path.join(os.environ["TMPDIR"], "mitosis-orchestrator", cfg.get("state_key", sid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "mitosis_orchestrator_debug.log"), "a") as fh:
        if stage_no == 1 or cfg.get("sessionstart_each", True):
            fh.write(json.dumps({"event": "SessionStart"}) + "\n")
        fh.write(json.dumps({"event": "PreToolUse"}) + "\n")
for event in out:
    print(json.dumps(event), flush=True)
'''


class Sandbox:
    """A fake claude on PATH, a small git plugin repository (with an evals/ directory), and the options of the CLI."""

    def __init__(self, case, **cfg):
        self.tmp = Path(tempfile.mkdtemp(prefix="long-harness-"))
        case.addCleanup(rmtree, self.tmp)
        self.plugin = self.tmp / "plugin"
        (self.plugin / "agents").mkdir(parents=True)
        (self.plugin / "agents" / "flash-worker.md").write_text("---\nname: flash-worker\n---\n")
        (self.plugin / "evals" / "compare").mkdir(parents=True)
        (self.plugin / "evals" / "compare" / "key.json").write_text("{}\n")
        (self.plugin / "plugin.json").write_text('{"name": "mitosis-orchestrator"}\n')
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        for cmd in (git + ["init", "-q"], git + ["add", "-A"], git + ["commit", "-q", "-m", "x"]):
            subprocess.run(cmd, cwd=str(self.plugin), check=True, stdout=subprocess.DEVNULL)
        self.commit = sh(["git", "rev-parse", "HEAD"], cwd=str(self.plugin)).stdout.strip()
        (self.plugin / "later.txt").write_text("later\n")
        for cmd in (git + ["add", "-A"], git + ["commit", "-q", "-m", "later"]):
            subprocess.run(cmd, cwd=str(self.plugin), check=True, stdout=subprocess.DEVNULL)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        exe = self.bin / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "claude-calls.jsonl"
        self.cfg_path = self.tmp / "fake.json"
        self.config_dir = self.tmp / "claude-config"
        self.work = self.tmp / "work"
        self.out = self.tmp / "out"
        self.cfg = {"log": str(self.log), "ref_answers": str(st.LOOKUP / "reference" / "answers.json")}
        self.cfg.update(cfg)
        self.write_cfg()
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "FAKE_CLAUDE_CFG": str(self.cfg_path),
                                               "CLAUDE_CONFIG_DIR": str(self.config_dir)})
        patcher.start()
        case.addCleanup(patcher.stop)

    def write_cfg(self):
        self.cfg_path.write_text(json.dumps(self.cfg))

    def main(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = lr.main(list(args))
        return code, buf.getvalue(), err.getvalue()

    def calibrate(self, arm, *extra, **kw):
        args = ["calibrate", "--arm", arm, "--plugin-repo", str(self.plugin), "--plugin-commit", self.commit, "--work-root", str(self.work),
                "--out", str(self.out), "--cost-mode", kw.get("mode", "per_stage")] + list(extra)
        return self.main(*args)

    def g0(self, *extra):
        return self.main("g0", "--plugin-repo", str(self.plugin), "--plugin-commit", self.commit, "--work-root", str(self.work), "--out", str(self.out), *extra)

    def calls(self, arm=None):
        if not self.log.is_file():
            return []
        rows = [json.loads(ln) for ln in self.log.read_text().splitlines() if ln.strip()]
        return [r for r in rows if arm is None or r["arm"] == arm]

    def stage(self, arm, n):
        return json.loads((self.out / ("cal__" + arm) / ("stage-%02d" % n) / "result.json").read_text())

    def run_json(self, arm):
        return json.loads((self.out / ("cal__" + arm) / "run.json").read_text())

    def run_result(self, arm):
        return json.loads((self.out / ("cal__" + arm) / "result.json").read_text())


def needs_lookup_scaffold(case):
    tmp = Path(tempfile.mkdtemp(prefix="long-probe-"))
    case.addCleanup(rmtree, tmp)
    try:
        st.build_workspace(tmp / "ws")
    except RuntimeError as exc:
        case.skipTest("lookup scaffold cannot run with this Python: %s" % exc)


@POSIX_ONLY
class CalibrateTests(unittest.TestCase):
    def setUp(self):
        needs_lookup_scaffold(self)

    def test_commands_resume_and_clause_per_arm_and_stage(self):
        sb = Sandbox(self)
        for arm in lr.ARMS:
            code, out, err = sb.calibrate(arm, "--stages", "3")
            self.assertEqual(code, 0, out + err)
        for arm in lr.ARMS:
            calls = sb.calls(arm)
            self.assertEqual(len(calls), 3)
            self.assertEqual([c["resume"] for c in calls][0], None)
            sid = sb.stage(arm, 1)["session_id"]
            self.assertEqual([c["resume"] for c in calls][1:], [sid, sid])
            for n, c in enumerate(calls, 1):
                argv = c["argv"]
                self.assertEqual(("--plugin-dir" in argv), arm == "delegate")
                self.assertEqual(("--resume" in argv), n >= 2)
                self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "4")
                self.assertEqual(c["prompt"], st.stage_prompt(n, arm))
                self.assertEqual("Delegate reading files" in c["prompt"], arm == "delegate")
        self.assertEqual(sb.calls("delegate")[0]["argv"][sb.calls("delegate")[0]["argv"].index("--plugin-dir") + 1],
                         str(sb.work / "_frozen" / sb.commit[:12]))

    def test_export_leaves_evals_out_and_the_result_records_the_plugin_state(self):
        sb = Sandbox(self)
        self.assertEqual(sb.calibrate("delegate", "--stages", "1")[0], 0)
        frozen = sb.work / "_frozen" / sb.commit[:12]
        self.assertTrue((frozen / "agents" / "flash-worker.md").is_file())
        self.assertFalse((frozen / "evals").exists())
        self.assertFalse((frozen / "later.txt").exists())      # the named commit, not HEAD
        res = sb.run_result("delegate")
        self.assertEqual(res["plugin_state"], {"plugin_loaded": True, "agents_loaded": True, "scout_loaded": True, "skill_loaded": True})
        self.assertTrue(res["plugin_state_ok"])
        self.assertEqual(res["status"], "complete")
        self.assertEqual(sb.calibrate("off", "--stages", "1")[0], 0)
        self.assertEqual(sb.run_result("off")["plugin_state"], {"plugin_loaded": False, "agents_loaded": False, "scout_loaded": False, "skill_loaded": False})

    def test_wrong_arm_state_stops_the_run(self):
        sb = Sandbox(self, wrong_state=True)
        code, out, err = sb.calibrate("delegate", "--stages", "3")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(len(sb.calls()), 1)
        self.assertEqual(sb.run_json("delegate")["status"], "arm_state_failed")

    def test_calls_jsonl_is_deduplicated_and_holds_no_content(self):
        sb = Sandbox(self)
        sb.calibrate("delegate", "--stages", "1")
        lines = (sb.out / "cal__delegate" / "stage-01" / "calls.jsonl").read_text().splitlines()
        recs = [json.loads(x) for x in lines]
        self.assertEqual(len(recs), 4)          # two primary ids (each streamed twice), the Agent call, one subagent call
        self.assertEqual([r["sub"] for r in recs], [False, False, False, True])
        self.assertEqual(sorted(recs[0]), ["cache_read", "cache_write", "input", "message_id", "model", "n", "output", "sub", "tools"])
        self.assertNotIn("words", "\n".join(lines))
        r = sb.stage("delegate", 1)
        self.assertEqual((r["n_api_calls"], r["n_primary_api_calls"], r["n_subagent_api_calls"], r["agent_calls"]), (4, 3, 1, 1))
        self.assertGreater(r["usd_subagent"], 0)
        self.assertGreater(r["usd_primary"], 0)

    def test_growth_over_stages_with_a_compaction_reset(self):
        usage = {"1": [[100, 0, 0, 1], [200, 0, 0, 1]], "2": [[50, 0, 0, 1], [150, 0, 0, 1]]}
        sb = Sandbox(self, usage_by_stage=usage, compact_stage=2, agent=False)
        self.assertEqual(sb.calibrate("off", "--stages", "2")[0], 0)
        self.assertEqual(sb.stage("off", 1)["growth_g"], 100)
        self.assertEqual(sb.stage("off", 2)["growth_g"], 100)      # the drop from 200 to 50 is skipped
        self.assertEqual(sb.stage("off", 2)["compactions"], [{"trigger": "auto", "pre_tokens": 150000}])
        self.assertEqual(sb.run_result("off")["growth_g_total"], 200)
        self.assertEqual(sb.stage("off", 1)["peak_context_tokens"], 200)

    def test_displaceable_share_is_reported_next_to_g(self):
        sb = Sandbox(self, usage_by_stage={"1": [[100, 0, 0, 1], [300, 0, 0, 1]]}, agent=False)
        sb.calibrate("off", "--stages", "1")
        r = sb.stage("off", 1)
        self.assertEqual(r["displaceable_by_tool"], {"Bash": 200})   # two results of 400 characters
        self.assertAlmostEqual(r["displaceable_share_of_g"], 200 / 200.0)

    def test_cumulative_versus_per_stage_cli_totals(self):
        costs = {"off": [0.1, 0.2, 0.3]}
        cum = Sandbox(self, costs=costs, cumulative=True)
        self.assertEqual(cum.calibrate("off", "--stages", "3", mode="cumulative")[0], 0)
        self.assertEqual([round(cum.stage("off", n)["effective_cost"], 6) for n in (1, 2, 3)], [0.1, 0.2, 0.3])
        self.assertEqual([round(cum.stage("off", n)["cli_total"], 6) for n in (1, 2, 3)], [0.1, 0.3, 0.6])
        per = Sandbox(self, costs=costs, cumulative=False)
        self.assertEqual(per.calibrate("off", "--stages", "3", mode="per_stage")[0], 0)
        self.assertEqual([round(per.stage("off", n)["effective_cost"], 6) for n in (1, 2, 3)], [0.1, 0.2, 0.3])
        self.assertAlmostEqual(per.run_result("off")["effective_cost_total"], 0.6)

    def test_calibrate_needs_to_know_the_cost_mode(self):
        sb = Sandbox(self)
        code, out, err = sb.main("calibrate", "--arm", "off", "--plugin-repo", str(sb.plugin), "--plugin-commit", sb.commit, "--work-root", str(sb.work), "--out", str(sb.out))
        self.assertEqual(code, 2)
        self.assertIn("g0", err)
        (sb.out / "g0.json").write_text(json.dumps({"cli_cost_mode": "cumulative"}))
        code, out, err = sb.main("calibrate", "--arm", "off", "--plugin-repo", str(sb.plugin), "--plugin-commit", sb.commit, "--work-root", str(sb.work), "--out", str(sb.out),
                                 "--stages", "1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sb.run_json("off")["cost_mode"], "cumulative")

    def test_effective_cost_is_the_larger_of_cli_and_transcript(self):
        sb = Sandbox(self, costs={"off": [0.2]}, transcript_output=20000)
        sb.calibrate("off", "--stages", "1")
        r = sb.stage("off", 1)
        self.assertAlmostEqual(r["cli_stage"], 0.2)
        self.assertGreater(r["transcript_stage"], 0.3)
        self.assertAlmostEqual(r["effective_cost"], r["transcript_stage"])

    def test_transcript_totals_are_not_counted_twice_across_stages(self):
        sb = Sandbox(self, costs={"off": [0.01]}, transcript_output=20000)
        sb.calibrate("off", "--stages", "3")
        one = sb.stage("off", 1)["transcript_stage"]
        self.assertTrue(sb.stage("off", 2)["transcript_cumulative"])
        self.assertAlmostEqual(sb.stage("off", 2)["transcript_stage"], one, places=6)
        self.assertAlmostEqual(sb.stage("off", 3)["transcript_stage"], one, places=6)
        self.assertAlmostEqual(sb.stage("off", 3)["transcript_total"], 3 * one, places=6)

    def test_grades_per_stage(self):
        sb = Sandbox(self, wrong_ids=["q15"], recall=dict(st.recall_key()))
        code, out, err = sb.calibrate("off", "--stages", "11")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sb.stage("off", 1)["grade"]["label"], "10/10")
        self.assertEqual(sb.stage("off", 2)["grade"]["label"], "9/10")
        self.assertEqual(sb.stage("off", 2)["grade"]["wrong_ids"], ["q15"])
        self.assertEqual(sb.stage("off", 4)["grade"]["visible_failing"], 23)
        self.assertFalse(sb.stage("off", 6)["grade"]["ok"])
        self.assertEqual(sb.stage("off", 7)["grade"]["label"], "graded at stage 10")
        self.assertFalse(sb.stage("off", 10)["grade"]["ok"])
        self.assertEqual(sb.stage("off", 11)["grade"]["label"], "6/6")
        self.assertEqual(sb.run_result("off")["status"], "complete")
        self.assertEqual(len(sb.calls("off")), 11)

    def test_stages_option_cuts_from_the_end(self):
        sb = Sandbox(self)
        self.assertEqual(sb.calibrate("off", "--stages", "4")[0], 0)
        self.assertEqual(len(sb.calls()), 4)
        code, _out, err = sb.calibrate("delegate", "--stages", "12")
        self.assertEqual(code, 2)
        self.assertIn("--stages", err)

    def test_stage_cap_stops_the_run_before_a_stage_that_could_pass_the_run_cap(self):
        sb = Sandbox(self, costs={"off": [1.0]})
        code, out, err = sb.calibrate("off", "--stages", "6", "--stage-cap", "2", "--run-cap", "3")
        self.assertEqual(code, 4, out + err)
        self.assertEqual(len(sb.calls()), 2)          # after two stages 2 spent + 2 would pass 3
        run = sb.run_json("off")
        self.assertEqual(run["status"], "partial")
        self.assertIn("run cap", run["stop_reason"])
        res = sb.run_result("off")
        self.assertTrue(res["partial"])
        self.assertEqual(res["stages_completed"], [1, 2])
        self.assertEqual(lr.Ledger(sb.out, 0).spent(), 2.0)

    def test_total_cap_stops_a_second_run_through_the_ledger(self):
        sb = Sandbox(self, costs={"off": [1.0], "delegate": [1.0]})
        self.assertEqual(sb.calibrate("off", "--stages", "3", "--stage-cap", "2", "--total-cap", "5")[0], 0)
        self.assertEqual(lr.Ledger(sb.out, 0).spent(), 3.0)
        code, out, err = sb.calibrate("delegate", "--stages", "3", "--stage-cap", "2", "--total-cap", "5")
        self.assertEqual(code, 4, out + err)
        self.assertEqual(len(sb.calls("delegate")), 1)       # 3 + 2 is not above 5, so stage 1 runs; stage 2 would need 4 + 2 = 6

    def test_total_cap_stop_before_the_first_stage(self):
        sb = Sandbox(self)
        code, out, err = sb.calibrate("off", "--stages", "3", "--stage-cap", "2", "--total-cap", "5", "--spent-before", "4")
        self.assertEqual(code, 4, out + err)
        self.assertEqual(sb.calls(), [])
        self.assertEqual(sb.run_json("off")["status"], "partial")
        self.assertIn("total cap", sb.run_json("off")["stop_reason"])

    def test_a_stage_over_its_cap_stops_the_run_with_a_clear_status(self):
        sb = Sandbox(self, costs={"off": [0.5, 5.0, 0.5]})
        code, out, err = sb.calibrate("off", "--stages", "3")
        self.assertEqual(code, 5, out + err)
        self.assertEqual(len(sb.calls()), 2)
        run = sb.run_json("off")
        self.assertEqual(run["status"], "stage_cap_exceeded")
        self.assertIn("stage 2", run["stop_reason"])
        self.assertGreaterEqual(lr.Ledger(sb.out, 0).spent(), 4.5)

    def test_the_ledger_charges_a_started_stage_at_its_cap_until_it_ends(self):
        sb = Sandbox(self)
        ledger = lr.Ledger(sb.out, 0)
        sb.out.mkdir(parents=True)
        ledger.start("cal:off:s01", "cal", 4)
        self.assertEqual(ledger.spent(), 4.0)
        ledger.end("cal:off:s01", "cal", 0.7)
        self.assertEqual(ledger.spent(), 0.7)
        self.assertEqual(lr.Ledger(sb.out, 1.5).spent(), 2.2)
        self.assertEqual(ledger.spent_run("cal:off:"), 0.7)
        self.assertEqual(ledger.spent_run("cal:delegate:"), 0)

    def test_resume_after_a_crash_runs_only_the_remaining_stages_once(self):
        sb = Sandbox(self, die_calls=[3])
        code, out, err = sb.calibrate("off", "--stages", "5")
        self.assertEqual(code, 6, out + err)
        self.assertEqual(len(sb.calls()), 3)
        run = sb.run_json("off")
        self.assertEqual(run["status"], "infra_failed")
        self.assertEqual(sb.run_result("off")["stages_completed"], [1, 2])
        sid = sb.stage("off", 2)["session_id"]
        ledger = lr.Ledger(sb.out, 0)
        self.assertEqual(ledger.spent(), 1.0 + 4.0)          # two finished stages at 0.5, the dead one stays charged at its cap
        code, out, err = sb.calibrate("off", "--stages", "5", "--resume-run")
        self.assertEqual(code, 0, out + err)
        calls = sb.calls()
        self.assertEqual(len(calls), 3 + 3)                  # the dead call, then stages 3, 4, 5
        self.assertEqual([c["stage_no"] for c in calls[3:]], [3, 4, 5])
        self.assertEqual(calls[3]["resume"], sid)
        self.assertEqual(calls[3]["prompt"], st.stage_prompt(3, "off"))
        self.assertEqual(sb.run_result("off")["status"], "complete")
        self.assertEqual(sb.run_json("off")["resumes"], 1)
        self.assertEqual(sb.run_result("off")["stages_completed"], [1, 2, 3, 4, 5])
        code, _out, err = sb.calibrate("off", "--stages", "5", "--resume-run")
        self.assertEqual(code, 2)
        self.assertEqual(len(sb.calls()), 6)

    def test_a_second_crash_cannot_be_resumed_again(self):
        sb = Sandbox(self, die_calls=[2, 3])
        self.assertEqual(sb.calibrate("off", "--stages", "4")[0], 6)
        self.assertEqual(sb.calibrate("off", "--stages", "4", "--resume-run")[0], 6)
        code, _out, err = sb.calibrate("off", "--stages", "4", "--resume-run")
        self.assertEqual(code, 2)
        self.assertIn("already resumed once", err)
        self.assertEqual(len(sb.calls()), 3)

    def test_resume_refuses_a_run_that_stopped_for_another_reason_and_a_fresh_start_over_a_run(self):
        sb = Sandbox(self, costs={"off": [1.0]})
        sb.calibrate("off", "--stages", "6", "--stage-cap", "2", "--run-cap", "3")
        code, _out, err = sb.calibrate("off", "--stages", "6", "--resume-run")
        self.assertEqual(code, 2)
        self.assertIn("partial", err)
        code, _out, err = sb.calibrate("off", "--stages", "6")
        self.assertEqual(code, 2)
        self.assertIn("--resume-run", err)

    def test_a_second_runner_on_the_same_output_is_refused(self):
        sb = Sandbox(self)
        sb.out.mkdir(parents=True)
        with lr.RunLock(sb.out):
            code, _out, err = sb.calibrate("off", "--stages", "1")
        self.assertEqual(code, 2)
        self.assertIn("another runner", err)

    def test_report_prints_tables_and_an_arm_comparison_without_a_verdict_word(self):
        sb = Sandbox(self, costs={"off": [0.4], "delegate": [0.6]}, compact_stage=2)
        for arm in lr.ARMS:
            self.assertEqual(sb.calibrate(arm, "--stages", "3")[0], 0)
        code, out, err = sb.main("report", "--out", str(sb.out))
        self.assertEqual(code, 0, err)
        self.assertIn("== arm off", out)
        self.assertIn("== arm delegate", out)
        self.assertIn("plugin state in init", out)
        self.assertIn("arm comparison over the 3 stage(s)", out)
        self.assertIn("cumulative context growth G", out)
        self.assertIn("compactions (stage, trigger, preTokens)", out)
        self.assertIn("Calibration only", out)
        for word in ("verdict", "benefit", "harm", "inconclusive", "protects", "manipulation", "better", "worse", "wins"):
            self.assertNotIn(word, out.lower())
        data = json.loads((sb.out / "report.json").read_text())
        self.assertEqual(data["common_stages"], [1, 2, 3])
        self.assertAlmostEqual(data["arms"]["delegate"]["common"]["cost"] - data["arms"]["off"]["common"]["cost"], 0.6)

    def test_report_compares_only_stages_both_runs_completed(self):
        sb = Sandbox(self)
        sb.calibrate("off", "--stages", "3")
        sb.calibrate("delegate", "--stages", "2")
        code, out, _err = sb.main("report", "--out", str(sb.out))
        self.assertEqual(code, 0)
        self.assertIn("over the 2 stage(s)", out)

    def test_compare_and_overhead_are_byte_identical_after_running(self):
        sb = Sandbox(self)
        self.assertEqual(sb.calibrate("delegate", "--stages", "2")[0], 0)
        sb.main("report", "--out", str(sb.out))
        self.assertEqual(snapshot(COMPARE), COMPARE_BEFORE)
        self.assertEqual(snapshot(OVERHEAD), OVERHEAD_BEFORE)
        self.assertEqual(sorted(snapshot(COMPARE)), sorted(COMPARE_BEFORE))
        self.assertEqual([p for p in snapshot(LONG) if p.endswith(".pyc") or "__pycache__" in p], [])
        listing = sh(["git", "status", "--porcelain", "--", "evals/compare", "evals/overhead", "scripts"], cwd=str(REPO))
        if listing.returncode == 0:
            self.assertEqual([ln for ln in listing.stdout.splitlines() if ln.strip()], DIRTY_AT_IMPORT)


_dirty = sh(["git", "status", "--porcelain", "--", "evals/compare", "evals/overhead", "scripts"], cwd=str(REPO))
DIRTY_AT_IMPORT = [ln for ln in _dirty.stdout.splitlines() if ln.strip()] if _dirty.returncode == 0 else []


# ---------------------------------------------------------------------------------------------
# g0
# ---------------------------------------------------------------------------------------------

@POSIX_ONLY
class G0Tests(unittest.TestCase):
    def findings(self, out):
        return {f["name"]: f for f in json.loads((out / "g0.json").read_text())["findings"]}

    def test_all_mechanics_pass_and_the_cost_mode_is_written(self):
        sb = Sandbox(self, costs={"off": [0.2, 0.2], "delegate": [0.2, 0.2]}, cumulative=True, transcript_price=25, debug_log=True)
        code, out, err = sb.g0()
        self.assertEqual(code, 0, out + err)
        f = self.findings(sb.out)
        self.assertEqual({k: v["status"] for k, v in f.items()},
                         {"session_id_stable": "PASS", "sessionstart_each_stage": "PASS", "plugin_state_keyed": "PASS", "cli_total_cost_cumulative": "PASS",
                          "transcripts_not_double_counted": "PASS", "agent_calls_synchronous": "PASS"}, out)
        self.assertEqual(json.loads((sb.out / "g0.json").read_text())["cli_cost_mode"], "cumulative")
        for token in ("PASS", "session_id_stable"):
            self.assertIn(token, out)
        calls = sb.calls()
        self.assertEqual(len(calls), 4)
        self.assertEqual({c["debug"] for c in calls}, {"1"})
        self.assertTrue(all(c["tmpdir"] and c["tmpdir"].startswith(str(sb.work)) for c in calls))
        self.assertEqual([c["resume"] is not None for c in calls if c["arm"] == "delegate"], [False, True])
        self.assertLessEqual(lr.Ledger(sb.out, 0).spent(), 1.0)
        self.assertEqual({c["cap"] for c in calls}, {0.25})
        self.assertIn("Calibration only", out)
        self.assertNotIn("small.txt", out)         # no prompts or content

    def test_per_stage_totals_and_failures_are_reported_as_such(self):
        sb = Sandbox(self, costs={"off": [0.2, 0.2], "delegate": [0.2, 0.2]}, cumulative=False, change_sid=True, background=True, debug_log=True,
                     sessionstart_each=False, transcript_price=25)
        code, out, err = sb.g0()
        self.assertEqual(code, 0, out + err)
        f = self.findings(sb.out)
        self.assertEqual(f["session_id_stable"]["status"], "FAIL")
        self.assertEqual(f["sessionstart_each_stage"]["status"], "FAIL")
        self.assertEqual(f["agent_calls_synchronous"]["status"], "FAIL")
        self.assertEqual(json.loads((sb.out / "g0.json").read_text())["cli_cost_mode"], "per_stage")

    def test_unknown_when_nothing_shows_the_answer(self):
        sb = Sandbox(self, costs={"off": [0.05, 0.05], "delegate": [0.05, 0.05]}, agent=False)
        code, out, err = sb.g0()
        self.assertEqual(code, 0, out + err)
        f = self.findings(sb.out)
        self.assertEqual(f["sessionstart_each_stage"]["status"], "UNKNOWN")
        self.assertEqual(f["plugin_state_keyed"]["status"], "UNKNOWN")
        self.assertEqual(f["agent_calls_synchronous"]["status"], "UNKNOWN")
        self.assertEqual(f["cli_total_cost_cumulative"]["status"], "UNKNOWN")
        self.assertIsNone(json.loads((sb.out / "g0.json").read_text())["cli_cost_mode"])

    def test_g0_counts_against_the_total_cap(self):
        sb = Sandbox(self)
        code, out, err = sb.g0("--total-cap", "5", "--spent-before", "4.9")
        self.assertEqual(code, 4, out + err)
        self.assertEqual(sb.calls(), [])
        self.assertIn("stopped cleanly", out)

    def test_g0_findings_pure(self):
        stage = {"sid": "s", "cli_total": 0.1, "transcript_total": 0.1, "transcript_cumulative": False, "agent_evidence": {}}
        facts = {"off": {"stages": [stage, dict(stage)], "debug": None}}
        names = {n: s for n, s, _ in lr.g0_findings(facts)}
        self.assertEqual(names["session_id_stable"], "PASS")
        self.assertEqual(names["sessionstart_each_stage"], "UNKNOWN")
        self.assertEqual(lr.g0_findings({})[0][1], "UNKNOWN")


class LongDirectoryTests(unittest.TestCase):
    def test_protocol_and_readme_line(self):
        text = (LONG / "PROTOCOL.md").read_text(encoding="utf-8")
        for needle in ("NOT SEALED", "MANIPULATION FAILED", "HARM", "BENEFIT", "PROTECTS ONLY", "INCONCLUSIVE", "fewer Agent calls than stages/2"):
            self.assertIn(needle, text)
        self.assertIn("evals/long", (REPO / "evals" / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("SEAL.json", [p.name for p in LONG.iterdir()])


if __name__ == "__main__":
    unittest.main()
