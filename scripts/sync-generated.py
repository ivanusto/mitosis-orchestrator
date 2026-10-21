#!/usr/bin/env python3
"""Regenerate every file derived from spec/ (requires Python 3.11+ for tomllib).

    python3 scripts/sync-generated.py          write all outputs
    python3 scripts/sync-generated.py --check  write nothing, list stale paths, exit 1 if any
"""

import re
import sys
import tomllib
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"

BEGIN = "<!-- BEGIN GENERATED: report-format -->"
END = "<!-- END GENERATED: report-format -->"
NOTICE_MD = "<!-- Generated from spec/agents/{name}.md and spec/models.toml; edit the spec and run scripts/sync-generated.py. -->"
NOTICE_TOML = (
    "# Generated from spec/ ({src}); edit the spec and run scripts/sync-generated.py.\n"
)

AGENTS_PROMPT_HEAD = """TASK: <single-sentence objective>

RULES:
1. Open `<file_path>`.
2. <one imperative instruction, under 20 words>.
3. Do not change any file outside `<allowed_files>`.
4. Run `<test_command>` to check the result.
5. On failure, write raw output to `<scratch_dir>/<task_id>.log`.
"""

CLAUDE_SCOUT_TOOL_RULE = (
    "Use `Read`, `Grep`, and `Glob`. Read whole files only when the prompt asks for it.\n"
    "   `Bash` is for read-only checks only (`git status`, `git log`, `git diff`, `ls`, `rg`, `wc`, `tail`). "
    "The hook denies any other command, any `>` redirection and any `$(...)`."
)
AGY_SCOUT_TOOL_RULE = (
    "Use `find_by_name`, `grep_search`, `list_dir` and `view_file` with `StartLine`/`EndLine` slices. "
    "Read whole files only when the prompt asks for it."
)
CODEX_SCOUT_TOOL_RULE = (
    "Read files with slice bounds when inspecting large files. Shell is for read-only commands only "
    "(`rg`, `ls`, `git status`, `git log`, `git diff`, `wc`, `tail`); never run commands that write, install, or push."
)

CLAUDE_SCOUT_TOOLS = ["Read", "Grep", "Glob", "Bash"]
CLAUDE_WORKER_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "NotebookEdit"]
AGY_SCOUT_TOOLS = ["send_message", "view_file", "find_by_name", "grep_search", "list_dir"]
AGY_WORKER_TOOLS = [
    "send_message", "view_file", "find_by_name", "grep_search", "list_dir", "read_url_content",
    "search_web", "multi_replace_file_content", "replace_file_content", "write_to_file",
    "run_command", "manage_task", "notebook_edit",
]


def load_toml(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def report_lines(report: dict, role: str, zh: bool = False) -> List[str]:
    src = report["zh"][role] if zh else report[role]
    return [
        "STATUS: " + " | ".join(report["status_values"]),
        "FILES_CHANGED: " + src["files_changed"],
        "VERIFICATION: " + src["verification"],
        "NOTES: " + src["notes"],
    ]


def hook_format(report: dict, role: str) -> str:
    return "REPORT FORMAT:\n" + report["hook_lead"] + "\n" + "\n".join(report_lines(report, role))


def render_body(template: str, tier: str, tool_rule: str, report: dict, role: str, fenced: bool,
                scratch_dir: str = "<artifactDirectoryPath>/scratch") -> str:
    block = "\n".join(report_lines(report, role))
    if fenced:
        block = "```\n" + block + "\n```"
    out = template.replace("{{tier}}", tier).replace("{{tool_rule}}", tool_rule).replace("{{scratch_dir}}", scratch_dir)
    out = out.replace("{{report_lead}}", report["agent_lead"]).replace("{{report_block}}", block)
    return out


def yaml_list(items: List[str]) -> str:
    return "".join("  - " + i + "\n" for i in items)


def md_agent(name: str, front: str, body: str) -> str:
    return "---\n" + front + "---\n\n" + NOTICE_MD.format(name=name) + "\n\n" + body


def toml_agent(name: str, description: str, model: str, effort: str, body: str, src: str) -> str:
    if "'''" in body:
        raise SystemExit("agent body must not contain triple single quotes")
    return (
        NOTICE_TOML.format(src=src)
        + 'name = "' + name + '"\n'
        + 'description = "' + description + '"\n'
        + 'model = "' + model + '"\n'
        + 'model_reasoning_effort = "' + effort + '"\n'
        + "developer_instructions = '''\n" + body.rstrip("\n") + "\n'''\n"
    )


def build_agent_outputs(models: dict, report: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    tpl = {
        "scout": (SPEC / "agents" / "flash-lite-scout.md").read_text(encoding="utf-8"),
        "worker": (SPEC / "agents" / "flash-worker.md").read_text(encoding="utf-8"),
    }
    cl, cx, ag = models["claude"], models["codex"], models["agy"]

    # Claude Code
    cs = render_body(tpl["scout"], cl["scout"]["model"], CLAUDE_SCOUT_TOOL_RULE, report, "scout", True)
    cw = render_body(tpl["worker"], cl["worker"]["model"], "", report, "worker", True)
    cs_front = (
        "name: flash-lite-scout\n"
        "description: Fast read-only reconnaissance subagent pinned to the " + cl["scout"]["model"] + " model tier for single-hop "
        "file/symbol lookups, grep/glob searches, git/log checks, and single-file inspection. No web access, no edits.\n"
        "model: " + cl["scout"]["model"] + "\ncolor: cyan\ntools:\n" + yaml_list(CLAUDE_SCOUT_TOOLS)
    )
    cw_front = (
        "name: flash-worker\n"
        "description: Execution and research subagent pinned to the " + cl["worker"]["model"] + " model tier for multi-file "
        "codebase research, web/documentation synthesis, writing and running tests, and scoped code edits or debugging "
        "within an explicit file allowlist.\n"
        "model: " + cl["worker"]["model"] + "\ncolor: green\ntools:\n" + yaml_list(CLAUDE_WORKER_TOOLS)
    )
    out["claude-agents/flash-lite-scout.md"] = md_agent("flash-lite-scout", cs_front, cs)
    out["claude-agents/flash-worker.md"] = md_agent("flash-worker", cw_front, cw)

    # agy
    asb = render_body(tpl["scout"], ag["scout"]["model"], AGY_SCOUT_TOOL_RULE, report, "scout", True)
    awb = render_body(tpl["worker"], ag["worker"]["model"], "", report, "worker", True)
    as_front = (
        "name: flash-lite-scout\n"
        "description: Fast read-only subagent pinned to the " + ag["scout"]["model"] + " model tier for single-hop "
        "file/symbol lookups, grep/glob searches, git/log checks and single-file inspection. No web access, no writes.\n"
        "model: " + ag["scout"]["model"] + "\nsubagent: true\ntools:\n" + yaml_list(AGY_SCOUT_TOOLS)
        + "inheritCustomizations: false\ninheritMcp: false\n"
    )
    aw_front = (
        "name: flash-worker\n"
        "description: Execution subagent pinned to the " + ag["worker"]["model"] + " model tier for multi-file codebase "
        "research, web/documentation synthesis, writing and running tests, and scoped code edits or debugging within an "
        "explicit file allowlist.\n"
        "model: " + ag["worker"]["model"] + "\nsubagent: true\ntools:\n" + yaml_list(AGY_WORKER_TOOLS)
        + "inheritCustomizations: false\ninheritMcp: true\n"
    )
    out["agents/flash-lite-scout/agent.md"] = md_agent("flash-lite-scout", as_front, asb)
    out["agents/flash-worker/agent.md"] = md_agent("flash-worker", aw_front, awb)

    # Codex (unfenced report block)
    xs = render_body(tpl["scout"], cx["scout"]["model"], CODEX_SCOUT_TOOL_RULE, report, "scout", False)
    xw = render_body(tpl["worker"], cx["worker"]["model"], "", report, "worker", False, "<workspace>/scratch")
    xs_desc = (
        "Fast read-only reconnaissance subagent (" + cx["scout"]["model"] + ", " + cx["scout"]["effort"]
        + " reasoning effort) for single-hop file/symbol lookups, grep/glob searches, git/log checks, "
        "and single-file inspection. Never edits files."
    )
    xw_desc = (
        "Execution and testing subagent (" + cx["worker"]["model"] + ", " + cx["worker"]["effort"]
        + " reasoning effort) for multi-file codebase research, writing and running tests, "
        "and scoped code edits within an explicit file allowlist."
    )
    for fname, name in (("flash-lite-scout", "flash-lite-scout"), ("explorer", "explorer")):
        out["codex-agents/" + fname + ".toml"] = toml_agent(
            name, xs_desc, cx["scout"]["model"], cx["scout"]["effort"], xs, "spec/agents/flash-lite-scout.md")
    for fname, name in (("flash-worker", "flash-worker"), ("worker", "worker")):
        out["codex-agents/" + fname + ".toml"] = toml_agent(
            name, xw_desc, cx["worker"]["model"], cx["worker"]["effort"], xw, "spec/agents/flash-worker.md")

    # Codex profiles
    for role, cmd, what in (("scout", "<read-only lookup task>", "scout"), ("worker", "<scoped edit / test task>", "worker")):
        p = cx[role]
        out["codex-config/mitosis-" + role + ".config.toml"] = (
            "# Mitosis Orchestrator " + what + " profile for OpenAI Codex CLI (>= 0.134).\n"
            "# Usage: codex exec -m gpt-5.6-luna --profile mitosis-" + role + ' "' + cmd + '"\n'
            "# Installed to $CODEX_HOME/mitosis-" + role + ".config.toml by scripts/setup-codex.py.\n"
            "# Generated from spec/models.toml; edit the spec and run scripts/sync-generated.py.\n"
            'model = "' + p["model"] + '"\n'
            'model_reasoning_effort = "' + p["effort"] + '"\n'
            'sandbox_mode = "' + p["sandbox"] + '"\n'
        )
    return out


def build_module(models: dict, report: dict) -> str:
    cl, cx, ag = models["claude"], models["codex"], models["agy"]
    prices = {k: tuple(float(x) for x in v) for k, v in sorted(models["prices"].items())}
    aliases = dict(sorted(models["price_aliases"].items()))
    items = [
        ("DEFAULT_CLAUDE_PRIMARY_MODEL", cl["primary"]),
        ("DEFAULT_CLAUDE_SCOUT_MODEL", cl["scout"]["model"]),
        ("DEFAULT_CLAUDE_WORKER_MODEL", cl["worker"]["model"]),
        ("DEFAULT_CLAUDE_SCOUT_EFFORT", cl["scout"]["effort"]),
        ("DEFAULT_CLAUDE_WORKER_EFFORT", cl["worker"]["effort"]),
        ("DEFAULT_CODEX_PRIMARY_MODEL", cx["primary"]),
        ("DEFAULT_CODEX_SCOUT_MODEL", cx["scout"]["model"]),
        ("DEFAULT_CODEX_WORKER_MODEL", cx["worker"]["model"]),
        ("DEFAULT_CODEX_SCOUT_EFFORT", cx["scout"]["effort"]),
        ("DEFAULT_CODEX_WORKER_EFFORT", cx["worker"]["effort"]),
        ("DEFAULT_PRIMARY_MODEL", ag["primary"]),
        ("AGY_SCOUT_MODEL", ag["scout"]["model"]),
        ("AGY_WORKER_MODEL", ag["worker"]["model"]),
        ("PRICES", prices),
        ("PRICE_ALIASES", aliases),
        ("CACHE_1H_WRITE_MULTIPLIER", float(models["cache"]["one_hour_write_multiplier"])),
        ("REPORT_HEADERS", tuple(report["headers"])),
        ("REPORT_STATUS_VALUES", tuple(report["status_values"])),
        ("REPORT_MAX_CHARS", int(report["max_chars"])),
        ("REPORT_MAX_BULLETS", int(report["max_bullets"])),
        ("REPORT_FORMAT_SCOUT", hook_format(report, "scout")),
        ("REPORT_FORMAT_WORKER", hook_format(report, "worker")),
        ("REPORT_BLOCK_REASON", report["block_reason"]),
    ]
    lines = ["# Generated by scripts/sync-generated.py from spec/. Do not edit.", ""]
    for name, value in items:
        if isinstance(value, dict):
            lines.append(name + " = {")
            for k, v in value.items():
                lines.append("    " + repr(k) + ": " + repr(v) + ",")
            lines.append("}")
        else:
            lines.append(name + " = " + repr(value))
    return "\n".join(lines) + "\n"


def with_region(text: str, block: str, fence_start: str) -> str:
    """Replace the marked region, or insert markers around the first fence that begins with fence_start."""
    region = BEGIN + "\n" + block + "\n" + END
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return head + region + tail
    m = re.search(r"^```markdown\n" + re.escape(fence_start) + r".*?^```$", text, re.S | re.M)
    if not m:
        raise SystemExit("cannot find report block to wrap (fence starting with %r)" % fence_start)
    return text[:m.start()] + region + text[m.end():]


def build_doc_outputs(report: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    agents_block = (
        "```markdown\n" + AGENTS_PROMPT_HEAD + "\n" + hook_format(report, "worker") + "\n```"
    )
    en_block = "```markdown\n" + "\n".join(report_lines(report, "worker")) + "\n```"
    zh_block = "```markdown\n" + "\n".join(report_lines(report, "worker", zh=True)) + "\n```"
    for rel, block, start in (
        ("rules/AGENTS.md", agents_block, "TASK:"),
        ("README.md", en_block, "STATUS:"),
        ("README.zh-TW.md", zh_block, "STATUS:"),
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        out[rel] = with_region(text, block, start)
    out[".agents/skills/orchestrate/SKILL.md"] = (ROOT / "skills/orchestrate/SKILL.md").read_text(encoding="utf-8")
    return out


def build_all() -> Dict[str, str]:
    models = load_toml(SPEC / "models.toml")
    report = load_toml(SPEC / "report.toml")
    out = build_agent_outputs(models, report)
    out["scripts/mitosis_generated.py"] = build_module(models, report)
    out.update(build_doc_outputs(report))
    return out


def main(argv: List[str]) -> int:
    check = "--check" in argv
    stale = []
    for rel, content in sorted(build_all().items()):
        path = ROOT / rel
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        stale.append(rel)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if check:
        for rel in stale:
            print("out of date: " + rel)
        if stale:
            print("run: python3 scripts/sync-generated.py")
        return 1 if stale else 0
    for rel in stale:
        print("wrote " + rel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
