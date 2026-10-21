#!/usr/bin/env python3
"""Mitosis Orchestrator lifecycle hooks for Antigravity (agy).

Zero-dependency (Python 3.8+ standard library only).

Modes (first CLI argument):

session-start
    Reads ~/.gemini/antigravity-cli/settings.json and injects the orchestration
    policy notice. By default it only WARNS when the configured primary model is
    not a frontier model. Set AGY_MITOSIS_PIN_SETTINGS=1 (or legacy AGY_ARGON_PIN_SETTINGS=1)
    to opt in to the behaviour that rewrites settings.json back to a preferred model.

pre-invocation
    Checks the live session model (payload.modelName) on every turn. If it is not
    a frontier model, injects a one-time ephemeral warning per invocation so the
    primary agent asks the user to switch models. Resets per-turn tracking
    (turn_edited_files, consecutive_failures).

pre-tool-use
    invoke_subagent   -> context-classifies every subagent into flash / flash_lite
                         (strong signals always win over lite keywords; mutation
                         prompts never stay on flash_lite) and records pending
                         spawns for lifecycle tracking.
    send_message      -> denies mutation or multi-step (>3 numbered rules) tasks
                         sent to a known flash_lite recipient (Claude Code: `SendMessage`,
                         recipients paired by agent id at post-tool-use / subagent-stop).
    run_workflow      -> denies scripts whose agent(...) calls omit
                         model="flash" / model="flash_lite".
    define_subagent   -> forces enable_subagent_tools=False (no nested spawning).
    manage_subagents  -> marks killed branch workers as cleaned up.
    circuit breaker   -> denies run_command and invoke_subagent when consecutive
                         failures reach >= 2.
    context hygiene   -> blocks direct inspection of .log and transcript.jsonl
                         files by the primary agent (delegates to flash-lite-scout).
    fast-path budget  -> tracks direct workspace edits (max 3 by default) and blocks
                         direct workspace edit + test-runner loops in the primary agent.
    workspace guard   -> "allow" when cwd, paths and command text stay inside
                         workspacePaths or artifactDirectoryPath; "ask" for
                         paths outside, privilege escalation, destructive or
                         system-altering commands, force pushes, or downloads
                         piped to a shell; fallback returns explicit "allow"
                         pass-through so tools are never denied by hook output.

post-tool-use
    Rolls back pending spawns when invoke_subagent fails; otherwise pairs pending
    spawns with the conversationIds that appear in the transcript. Tracks
    consecutive failures across run_command and invoke_subagent for the circuit breaker.

stop
    When the turn is fully idle and Workspace:"branch" workers are still active,
    asks the primary agent (once per set of live branch workers) to merge/discard and kill them
    before finishing. Never blocks user cancellations, errors, or budget caps.

Environment variables:
    AGY_MITOSIS_PIN_SETTINGS=1          opt in to rewriting settings.json at session start
    AGY_MITOSIS_PRIMARY_MODEL=<name>    target model when pinning (default: Gemini 4 Argon (High))
    AGY_MITOSIS_ALLOW_NON_PRIMARY=1     silence all primary-model warnings
    AGY_ALLOW_NON_MITOSIS_PRIMARY=1     alias to silence all primary-model warnings
    AGY_ANTIGRAVITY_SETTINGS=<path>     override the settings.json location (tests)
    AGY_MITOSIS_AUTO_ALLOW=1            enable the workspace scope guard (off by default)
    AGY_MITOSIS_MAX_DIRECT_EDITS=<int>  max distinct workspace file edits per turn (default: 3)
    AGY_MITOSIS_ALLOW_DIRECT_LOGS=1     allow primary agent to read raw logs and transcripts
    AGY_MITOSIS_ALLOW_DIRECT_FASTPATH=1 allow primary agent direct edit-test loops and unlimited edits
    AGY_MITOSIS_CIRCUIT_BREAKER=0       disable the 2-failure circuit breaker
    CLAUDE_MITOSIS_PIN_SUBAGENTS=0      (Claude Code) stop rewriting Agent calls entirely
    CLAUDE_MITOSIS_SCOUT_MODEL=<m>      (Claude Code) scout model (default: haiku; haiku|sonnet|opus|fable)
    CLAUDE_MITOSIS_WORKER_MODEL=<m>     (Claude Code) worker model (default: sonnet)
    CLAUDE_MITOSIS_PIN_EFFORT=0         (Claude Code) stop adding `effort` to pinned Agent calls
    CLAUDE_MITOSIS_SCOUT_EFFORT=<e>     (Claude Code) scout effort (default: low; low|medium|high|xhigh|max)
    CLAUDE_MITOSIS_WORKER_EFFORT=<e>    (Claude Code) worker effort (default: medium)
    CLAUDE_MITOSIS_ENFORCE_REPORT=0     (Claude Code) do not block a plugin subagent's stop when its
                                        final message is not the four-header report
    AGY_MITOSIS_APPEND_REPORT_FORMAT=0  do not append the REPORT FORMAT block to subagent prompts
                                        that lack one (agy invoke_subagent, Claude Code Agent)
    AGY_MITOSIS_DEBUG=1                 append one JSON line per hook call to
                                        <artifactDirectoryPath>/mitosis_orchestrator_debug.log
    MITOSIS_OTEL=1                      emit one OTel GenAI invoke_agent span per routing decision to
                                        MITOSIS_OTEL_FILE (JSONL) and/or an OTLP/HTTP endpoint
                                        (OTEL_EXPORTER_OTLP_[TRACES_]ENDPOINT); see scripts/mitosis_otel.py
    (All variables support legacy AGY_ARGON_* and AGY_ALLOW_NON_ARGON_PRIMARY fallbacks)
"""

import copy
import functools
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

def _load_sibling(name: str) -> Any:
    """Load scripts/<name>.py by file path (scripts/ is not on sys.path), cached in sys.modules."""
    import importlib.util
    key = "_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


_GEN = _load_sibling("mitosis_generated")

DEFAULT_PRIMARY_MODEL = _GEN.DEFAULT_PRIMARY_MODEL
DEFAULT_CODEX_PRIMARY_MODEL = _GEN.DEFAULT_CODEX_PRIMARY_MODEL
DEFAULT_CODEX_WORKER_MODEL = _GEN.DEFAULT_CODEX_WORKER_MODEL
DEFAULT_CODEX_SCOUT_MODEL = _GEN.DEFAULT_CODEX_SCOUT_MODEL
DEFAULT_CODEX_SCOUT_EFFORT = _GEN.DEFAULT_CODEX_SCOUT_EFFORT
DEFAULT_CODEX_WORKER_EFFORT = _GEN.DEFAULT_CODEX_WORKER_EFFORT
DEFAULT_CLAUDE_PRIMARY_MODEL = _GEN.DEFAULT_CLAUDE_PRIMARY_MODEL
DEFAULT_CLAUDE_WORKER_MODEL = _GEN.DEFAULT_CLAUDE_WORKER_MODEL
DEFAULT_CLAUDE_SCOUT_MODEL = _GEN.DEFAULT_CLAUDE_SCOUT_MODEL
DEFAULT_CLAUDE_SCOUT_EFFORT = _GEN.DEFAULT_CLAUDE_SCOUT_EFFORT
DEFAULT_CLAUDE_WORKER_EFFORT = _GEN.DEFAULT_CLAUDE_WORKER_EFFORT
CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
CLAUDE_PLUGIN_PREFIX = "mitosis-orchestrator"
# Built-in Claude Code subagent types that the pinning policy must never rewrite.
CLAUDE_BUILTIN_AGENT_TYPES = {
    "explore", "plan", "general-purpose", "claude", "claude-code-guide",
    "statusline-setup", "output-style-setup", "bash",
}
CLAUDE_SUBAGENT_MODELS = {"haiku", "sonnet", "opus", "fable", "best"}
CLAUDE_HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop", "Stop", "Notification", "PreCompact",
}
ALLOWED_PRIMARY_MODELS = {
    "Gemini 4 Argon (High)",
    "Gemini 4 Argon (Medium)",
}
ALLOWED_SUBAGENT_MODELS = {"flash", "flash_lite"}
SCOUT_TYPE = "flash-lite-scout"
CODEX_SCOUT_ROLE = "explorer"
CODEX_WORKER_ROLE = "worker"
WORKER_TYPE = "flash-worker"
STATE_FILENAME = "mitosis_orchestrator_state.json"


def is_codex_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches OpenAI Codex CLI hook or MCP input schemas."""
    if not isinstance(payload, dict):
        return False
    if payload.get("client") == "codex" or payload.get("platform") == "codex":
        return True
    # Codex >= 0.128 plugin hooks speak Claude Code's protocol (same event names
    # and keys) but add turn_id, which Claude Code never sends.
    if payload.get("turn_id") is not None:
        return True
    # Claude Code Stop / SubagentStop payloads also carry `last_assistant_message`;
    # a Claude hook event name settles it before the key-based heuristics below.
    if str(payload.get("hook_event_name") or "") in CLAUDE_HOOK_EVENTS:
        return False
    if any(k in payload for k in ("turn_id", "turn-id", "thread_id", "thread-id", "call_id", "last-assistant-message", "last_assistant_message", "input-messages", "input_messages")):
        return True
    if payload.get("type") in ("agent-turn-complete", "approval-requested", "turn-ended") or \
       payload.get("event") in ("agent-turn-complete", "approval-requested", "turn-ended"):
        return True
    if str(payload.get("hook_event_name") or "").lower() in ("codex-notify", "turn-ended"):
        return True
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if not tool_name and isinstance(payload.get("toolCall"), dict):
        tool_name = str(payload["toolCall"].get("name") or "").strip()
    if not tool_name and isinstance(payload.get("tool_call"), dict):
        tool_name = str(payload["tool_call"].get("name") or "").strip()
    if tool_name in (
        "apply_patch", "exec_command", "local_shell", "shell",
        "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
    ):
        return True
    return False


def is_codex_hook_payload(payload: Dict[str, Any]) -> bool:
    """Codex plugin hook (hooks/codex-hooks.json): Claude Code's protocol, Codex's tools."""
    return is_codex_payload(payload) and str(payload.get("hook_event_name") or "") in CLAUDE_HOOK_EVENTS


def is_claude_code_payload(payload: Dict[str, Any]) -> bool:
    """Return True if payload matches Claude Code hook input schemas."""
    if not isinstance(payload, dict):
        return False
    if is_codex_payload(payload):
        return False
    return any(k in payload for k in ("hook_event_name", "session_id", "tool_name", "tool_input"))

# ---------------------------------------------------------------------------
# Classification vocabularies
# ---------------------------------------------------------------------------

# Prompts that ask the subagent to change the workspace. These NEVER run on
# flash_lite, whatever the Workspace mode or the requested Model.
MUTATION_RE = re.compile(
    r"\b("
    r"implement|refactor|rewrite|edit|modify|patch|fix(?:es|ing)?|"
    r"write (?:a |the |new )?(?:file|test|tests|code|module|function|class)|"
    r"create (?:a |the |new )?(?:file|test|tests|module|function|class|script)|"
    r"update (?:the )?(?:file|files|config|test|tests|code)|"
    r"delete|remove|rename|replace|insert|append|"
    r"migrate|migration|add (?:a |the |new )?(?:test|tests|file|function|method|check)"
    r")\b",
    re.IGNORECASE,
)

# Everyday verbs that only ask for a change when they open a clause ("Add a
# docstring", "...and push"); elsewhere they are usually nouns or prose.
IMPERATIVE_VERBS = (
    r"add|apply|bump|commit|push|install|uninstall|wire(?: up)?|set|change|update|upgrade|downgrade|"
    r"write|create|move|revert|merge|rebase|format|deploy|publish|release|tag|adjust|tweak|convert|switch|port|lint|typecheck|type-check|reformat"
)
# Soft requests: "X should return None instead of raising", "could you make X use Y".
SOFT_REQUEST_RE = re.compile(
    r"\bshould (?:now )?(?:return|raise|accept|use|handle|default to)\b[^.\n]{0,80}\binstead\b|"
    r"\b(?:could|can|would) you (?:please )?make\b|"
    r"\bcan we (?:please )?make\b|\bmake it so\b|\bi wish\b|"
    r"(?:\bit|\bthat|\bthis)?(?:'d|\s+would)\s+be\s+(?:so\s+|really\s+|much\s+)?"
    r"(?:nice|great|better|good|ideal|awesome|helpful|cool|useful|preferable)\s+(?:if|to)\b|"
    r"(?:^|[.;!\n])\s*(?:also,?\s+|and\s+)?(?:the|this|that|our|my|your|each|every)\s+[\w ./-]{1,40}?"
    r"\bshould (?:now |also |really )?(?:handle|return|accept|support|reject|retry|validate|log|raise|throw|use)\b|"
    r"(?:如果|要是|若能)[^。？?！!]{0,60}?就好了|"
    r"(?:能不能|可不可以|可以|能否|是否可以|可否|能)(?:(?!怎麼|怎么|如何|哪)[^。？?！!]){0,30}?"
    r"(?:改|處理|处理|修|加|支援|支持|調整|调整|優化|优化)|"
    r"(?:能不能|可不可以|能否|可否)\s*(?:讓|让|把|將|将|幫|帮|多|增加|提供|開放|开放)|"
    r"(?:幫忙|帮忙|麻煩你?|麻烦你?|幫我|帮我)(?:處理|处理)|"
    r"(?:應該|应该|應|改為|改为)(?:回傳|返回|丟出|抛出|使用)|不要再(?:丟|抛|拋)|改用|"
    r"(?:更新|調整|调整)(?!紀錄|记录|日誌|日志|說明|说明|內容是|了什麼|了什么)|"
    r"(?:整理|寫|写|輸出|输出)(?:成|到|進|进)\s*[\w./-]+\.(?:md|txt|json|ya?ml|csv|py)",
    re.IGNORECASE,
)
IMPERATIVE_MUTATION_RE = re.compile(
    r"(?:(?:^|[.;:!?\n])\s*(?:[-*]\s+|\d+[.)]\s+)?|\b(?:please|then|and|also|must|should|but|do)\s+)"
    r"(?:" + IMPERATIVE_VERBS + r")\b",
    re.IGNORECASE,
)
# Text ending at the start of a clause: an imperative may follow.
CLAUSE_START_RE = re.compile(
    r"(?:^|[.;:!?\n。；：！？，,])\s*(?:[-*]\s+|\d+[.)]\s+)?(?:(?:please|then|also|and|請|请|然後|然后|再)\s*)?$",
    re.IGNORECASE,
)
GIT_MUTATION_RE = re.compile(
    r"\bgit\s+(?:commit|push|add|rm|mv|reset|merge|rebase|cherry-pick|stash|tag|checkout\s+-b|switch\s+-c)\b",
    re.IGNORECASE,
)
# Linters, type checkers and formatters: running them needs a shell.
_CHECKER_ALT = (r"(?:eslint|ruff|flake8|pylint|mypy|pyright|tsc|prettier|black|golangci-lint|clippy|"
                r"shellcheck|rubocop|stylelint|lint(?:ing|er)?|type-?check(?:ing|er)?)")
# Running tests, builds or benchmarks needs a worker shell, even on a scout-typed call.
EXECUTION_RE = re.compile(
    r"\b(?:run|rerun|re-run|execute)\s+(?:[\w./:-]+\s+){0,3}?"
    r"(?:tests?|test suite|pytest|unittest|jest|vitest|builds?|benchmarks?|make|npm test|cargo test|go test)\b|"
    r"(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?(?:測試|测试|建置|編譯|编译|基準|基准)|"
    r"\b(?:run|rerun|re-run|execute)\s+(?:[\w./:-]+\s+){0,3}?" + _CHECKER_ALT + r"\b|"
    r"(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?" + _CHECKER_ALT + r"\b|"
    r"\b(?:reproduce|repro)\b[^.?!\n]{0,60}?\b(?:running|run|executing|execute)\b|"
    r"(?:重現|重现|復現|复现)[^。？?！!]{0,20}?(?:跑|執行|执行|運行|运行)|"
    r"(?:^|[.;:!?\n]|\b(?:please|then|and|also)\s)\s*(?:re-?run|run|execute)\s+(?:(?:the|this|that|our|it|your|a)\s+)?"
    r"(?:[\w./-]+\s+){0,2}?(?:pipeline|scripts?|jobs?|workflow|etl|cron|binary|program)\b",
    re.IGNORECASE,
)

# CJK has no word boundaries, so these run without \b. Traditional and Simplified forms.
CJK_MUTATION_RE = re.compile(
    r"修改|改寫|改写|重構|重构|新增|刪除|删除|刪掉|删掉|移除|拿掉|去掉|實作|實現|实现|修正|修好|修復|修复|修掉|"
    r"改成|改為|改为|改名|換成|换成|寫入|写入|安裝|安装|提交|推送|更名|重新命名|替換|替换|取代|合併|合并|"
    r"更動|更改|改動|改动|升到|升級|升级|降到|設為|设为|建立|新建|創建|创建|補上|补上|"
    r"加(?:上|入|一|個|个|進|进|到)|加\s+[A-Za-z_]|(?:幫|帮|給|给|替)[^。？?！!]{0,30}?加"
)
# A Chinese verb used as a noun or adjective: 被修改的, 修正版, 安裝說明, 修復提交訊息.
# "說明修改 X 前要..." asks to explain the change, not to make it.
CJK_NOUN_BEFORE_RE = re.compile(r"(?:被|已|已經|已经|曾|曾經|曾经|說明|说明|解釋|解释|介紹|介绍|了解)\s*$")
CJK_MUTATION_NOUN_AFTER_RE = re.compile(
    r"^(?:的|過|过|版|紀錄|记录|訊息|信息|說明|说明|對照|对照|表|清單|清单|指南|流程|步驟|步骤|方式|方法|提交訊息|提交信息|原因|理由|目的|動機|动机)"
)
# Negations ahead of a Chinese verb: 不要修改, 請勿刪除, 不需要任何改動.
CJK_NEGATED_MUTATION_RE = re.compile(
    r"(?:不要|不得|不可|不能|不准|不需要?|無須|无须|毋須|別|别|勿|請勿|请勿|禁止|切勿|避免)\s*(?:任何|去)?\s*"
    r"(?:" + CJK_MUTATION_RE.pattern + r")"
)

# "Do not edit anything", "never modify", "without deleting" must not count as mutation requests.
# Up to two words may sit between the negation and the verb, and "or"/"nor"
# chains it, so "Do NOT git fetch/pull or modify" and "never delete or rename"
# are prohibitions too. "and" is left out: "don't X and fix Y" asks for a fix.
_MUTATION_ALT = MUTATION_RE.pattern[3:-3] + "|" + IMPERATIVE_VERBS
NEGATED_MUTATION_RE = re.compile(
    r"\b(?:do not|don't|dont|never|must not|mustn't|should not|shouldn't|without|not|no)\s+"
    r"(?:(?!\b(?:" + _MUTATION_ALT + r")\b)[\w/.-]+,?\s+){0,2}"
    r"(?:(?:or|nor)\s+)?"
    r"(?:" + _MUTATION_ALT + r")\b"
    r"(?:,?\s+(?:or|nor)\s+(?:" + _MUTATION_ALT + r")\b)*",
    re.IGNORECASE,
)


def _blank(match: "re.Match[str]") -> str:
    return " " * len(match.group(0))


def _strip_negated_mutations(text: str) -> str:
    """Blank out negated mutation phrases so prohibitions do not read as tasks.

    Lengths are preserved, so match offsets still point into the original text.
    """
    return CJK_NEGATED_MUTATION_RE.sub(_blank, NEGATED_MUTATION_RE.sub(_blank, text))


# A determiner turns a mutation or heavy keyword into a noun: "the patch
# version", "review the fixes", "the migration guide", "the bug report".
_ADJ = (r"(?:later|earlier|previous|next|last|first|latest|recent|same|old|new|original|upstream|final|"
        r"actual|proposed|suggested|relevant|flaky|failing|broken|existing)")
DETERMINER_BEFORE_RE = re.compile(
    r"\b(?:the|a|an|this|that|these|those|its|my|your|our|their|which|what)\s+(?:" + _ADJ + r"\s+)*$",
    re.IGNORECASE,
)
# In a question the determiner may sit up to two words back: "does the Android release build use".
LOOSE_DETERMINER_BEFORE_RE = re.compile(
    r"\b(?:the|a|an|this|that|these|those|its|my|your|our|their|which|what)\s+(?:[\w-]+\s+){0,2}$", re.IGNORECASE
)
# Chinese determiners before an English noun: "那個 patch", "PR #812 的 fix", "哪些 migration".
CJK_DETERMINER_BEFORE_RE = re.compile(r"(?:的|那個|那个|這個|这个|該|该|此|這些|这些|那些|哪個|哪个|哪些|哪幾個|哪几个)\s*$")
# Questions: a leading question word in any clause, or a question mark.
QUESTION_RE = re.compile(
    r"[?？]|(?:^|[.;!\n])\s*(?:which|what|how many|how much|why|where|does|do|is|are|can|did)\b|"
    r"哪些|哪個|哪个|什麼|什么|為什麼|为什么|多少|有沒有|有没有|是否|嗎|吗",
    re.IGNORECASE,
)
# Running something is an execution signal: a noun-use skip must not hide it.
EXECUTION_SIGNAL_RE = re.compile(
    r"\b(?:run|rerun|re-run|running|execute|executing)\b|actually run|you'll need to run|跑|執行|执行|運行|运行",
    re.IGNORECASE,
)
# Nouns that make the preceding keyword a label: "bug report", "test file".
LABEL_NOUN_AFTER_RE = re.compile(
    r"^\s+(?:reports?|templates?|guides?|notes?|versions?|numbers?|names?|files?|folders?|"
    r"director(?:y|ies)|docs?|ids?|labels?|logs?|loggers?|tickets?|issues?|config(?:uration)?s?|settings|"
    r"scripts?|modules?)\b|"
    r"^\s*(?:回報|回报|報告|报告|範本|模板|版本|檔案|文件|目錄|目录|名稱|名称|設定值|设定值|設定|设置|說明|说明|紀錄|记录|"
    r"預設值|默认值|參數|参数)",
    re.IGNORECASE,
)


# Keywords that double as nouns; only these may be read as labels by what follows.
NOUNISH_KEYWORDS = {
    "patch", "fix", "fixes", "migration", "test", "tests", "bug", "build", "integration",
    "benchmark", "dependency", "dependencies", "trace", "documentation", "debug",
}


def _after_determiner(text: str, match: "re.Match[str]", loose: bool = False) -> bool:
    before = text[max(0, match.start() - 30):match.start()]
    if DETERMINER_BEFORE_RE.search(before) or (loose and LOOSE_DETERMINER_BEFORE_RE.search(before)):
        return True
    return match.group(0).lower() in NOUNISH_KEYWORDS and bool(CJK_DETERMINER_BEFORE_RE.search(before))


def _before_label(text: str, match: "re.Match[str]") -> bool:
    return match.group(0).lower() in NOUNISH_KEYWORDS and bool(LABEL_NOUN_AFTER_RE.match(text[match.end():]))


def _is_noun_use(text: str, match: "re.Match[str]") -> bool:
    return _after_determiner(text, match) or _before_label(text, match)


def _is_path_part(text: str, match: "re.Match[str]") -> bool:
    """'refactor/' or 'migrate.py' names a path, not a request."""
    before = text[match.start() - 1:match.start()] if match.start() else ""
    after = text[match.end():match.end() + 2]
    return before in ("/", ".", "-", "_") or after.startswith("/") or bool(re.match(r"^\.\w", after))


def find_mutations(text: str) -> List[Tuple[int, str]]:
    """Return (offset, phrase) for every mutation request in text, in order.

    Negated phrases ("do not edit", "不要修改") and noun uses ("the patch
    version") do not count.
    """
    stripped = _strip_negated_mutations(text)
    found: List[Tuple[int, str]] = []
    for m in MUTATION_RE.finditer(stripped):
        if not _is_noun_use(stripped, m) and not _is_path_part(stripped, m):
            found.append((m.start(), m.group(0)))
    for pattern in (IMPERATIVE_MUTATION_RE, GIT_MUTATION_RE, EXECUTION_RE, SOFT_REQUEST_RE):
        for m in pattern.finditer(stripped):
            found.append((m.start(), m.group(0).strip(" .;:!?\n")))
    for m in CJK_MUTATION_RE.finditer(stripped):
        if CJK_NOUN_BEFORE_RE.search(stripped[:m.start()]) or CJK_MUTATION_NOUN_AFTER_RE.match(stripped[m.end():]):
            continue
        found.append((m.start(), m.group(0).strip()))
    found.sort()
    return found


def find_mutation(text: str) -> Optional[str]:
    """First mutation phrase in text, or None."""
    found = find_mutations(text)
    return found[0][1] if found else None


# Moderate-complexity work that needs flash even when read-only.
HEAVY_RE = re.compile(
    r"\b("
    r"tests?|pytest|unittest|jest|vitest|go test|cargo test|npm test|"
    r"debug|bug|root[- ]cause|multi-file|across (?:files|modules|packages)|"
    r"call[- ]?graph|dependency|dependencies|trace|synthesi[sz]e|"
    r"benchmark|build|compile|integration|deep[- ]dive|investigate|diagnose|profil(?:e|ing)|memory leak|"
    r"race condition|on the web|from the web|search the web|web search|"
    r"search_web|read_url_content|documentation|docs? (?:for|on|about)"
    r")\b",
    re.IGNORECASE,
)

# Bounded single-hop lookups. Deliberately narrow: bare words such as
# "find", "ls", "count", "status" or "version" are too common in English and
# produced false flash_lite routing in 1.1.0.
LITE_RE = re.compile(
    r"\b("
    r"scout|look ?up|locate|where is|where are|"
    r"grep|ripgrep|glob|search for|find_by_name|grep_search|list_dir|view_file|"
    r"list (?:the |all )?files|list (?:the )?directory|"
    r"git status|git log|git blame|check (?:the )?logs?|tail (?:the )?logs?|"
    r"line numbers?|single file|one file|"
    r"summari[sz]e (?:this |the )?(?:file|readme|config)|"
    r"inspect (?:this |the )?(?:file|config)|"
    r"(?:file|path|symbol|function|class) exists?|does .{1,40} exist"
    r")\b",
    re.IGNORECASE,
)

CJK_HEAVY_RE = re.compile(
    r"(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?(?:測試|测试)|調查|调查|上網查|上网查|綜合|综合|記憶體洩漏|内存泄漏|效能分析|性能分析|剖析|"
    r"除錯|偵錯|调试|根因|根本原因|編譯|编译|建置|構建|构建|基準測試|基准测试|效能測試|性能测试|"
    r"跨(?:檔案|文件|模組|模块|套件|包)|多(?:檔案|文件)|呼叫圖|调用图|相依|依賴|依赖|追蹤|追踪|深入(?:分析|研究)|彙整|汇总|綜整|文件研究"
)
# 建置設定, 除錯用的 logger: the heavy word labels a thing to look up.
CJK_HEAVY_NOUN_AFTER_RE = re.compile(
    r"^(?:設定|设置|配置|用|的設定|腳本|脚本|範本|模板|紀錄|记录|報告|报告|檔|文件|目錄|目录)"
)
CJK_LITE_RE = re.compile(
    r"在哪(?:裡|里|個|个)?|位於|位于|哪(?:個|个)(?:檔案|文件|函式|函数)|列出(?:檔案|文件|目錄|目录)|"
    r"搜尋|搜索|查找|是否存在|有沒有|有没有|行號|行号|單一檔案|单一文件"
)


def find_heavy_ex(text: str, lite_present: bool) -> Tuple[Optional[str], bool]:
    """First heavy signal in text as (phrase, noun_like), or (None, False).

    A heavy keyword that names a path part ("tests/", "build.gradle"), a
    label ("the bug report template") or a topic in a lookup ("where is the
    test for x", "which tests import y?") is skipped. A question skips
    determiner uses only when nothing in it asks to run anything. A heavy hit
    that still follows a determiner is returned with noun_like=True; a plain
    hit is preferred over a noun-like one.
    """
    question = bool(QUESTION_RE.search(text)) and not EXECUTION_SIGNAL_RE.search(text) \
        and not TEST_RUNNER_RE.search(text)
    noun_like: Optional[str] = None
    for m in HEAVY_RE.finditer(text):
        if _before_label(text, m) or _is_path_part(text, m):
            continue
        nounish = m.group(0).lower() in NOUNISH_KEYWORDS
        if nounish and (lite_present or question) and _after_determiner(text, m, loose=question):
            continue
        if nounish and _after_determiner(text, m):
            noun_like = noun_like or m.group(0)
            continue
        return m.group(0), False
    for m in CJK_HEAVY_RE.finditer(text):
        if not CJK_HEAVY_NOUN_AFTER_RE.match(text[m.end():]):
            return m.group(0), False
    return (noun_like, True) if noun_like else (None, False)


def find_heavy(text: str, lite_present: bool) -> Optional[str]:
    """First heavy signal in text, or None (see find_heavy_ex)."""
    return find_heavy_ex(text, lite_present)[0]


def find_lite(text: str) -> Optional[str]:
    """First lite signal in text, or None."""
    m = LITE_RE.search(text) or CJK_LITE_RE.search(text)
    return m.group(0) if m else None


# The caller declares the task read-only. Mutation and heavy keywords in such
# prompts are usually the topic ("review the fixes", "plan the migration"),
# so those rules lose their confidence and a judge may take over.
READ_ONLY_RE = re.compile(
    r"\bread[- ]?only\b|"
    r"\bdo(?: not|n't) (?:modify|edit|change|write|touch) (?:anything|any files?|the (?:repo|code|workspace))\b|"
    r"\bno (?:edits|writes|changes)\b|"
    r"唯讀|只讀|只要讀取|只需讀取|僅讀取|不需修改|不要(?:修改|改動|更動|寫入|動手|动手)|先不要改|只读|只要读取|不要(?:修改|改动|写入)",
    re.IGNORECASE,
)
READ_ONLY_CONFIDENCE = 0.6

NUMBERED_RULE_RE = re.compile(r"(?m)^\s*[0-9]+[.)]\s+")
CONVERSATION_ID_RE = re.compile(
    r'\\*"conversationId\\*"\s*:\s*\\*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\*"',
    re.I,
)
AGENT_CALL_RE = re.compile(r"\bagent\s*\(")
MODEL_KWARG_RE = re.compile(r"""\bmodel\s*=\s*(?:["']([^"']*)["']|([A-Za-z_][A-Za-z0-9_.]*))""")

SAFE_TERMINATION_REASONS = {
    "USER_CANCELED",
    "ERROR",
    "MAX_INVOCATIONS",
    "MAX_FORCED_INVOCATIONS",
    "MAX_TOKEN_BUDGET_EXCEEDED",
}

MAX_SUBAGENT_REPORT_CHARS = _GEN.REPORT_MAX_CHARS
MAX_REPORT_BULLET_LINES = _GEN.REPORT_MAX_BULLETS
REPORT_BULLET_RE = re.compile(r"^[-*]\s+\S+")
REPORT_LOG_URI_RE = re.compile(r"file://\S+\.log\b", re.IGNORECASE)
REPORT_FENCE_RE = re.compile(r"^\s*`{3,}[\w-]*\s*$")
CLAUDE_AGENT_ID_RE = re.compile(r"agentId\"?\s*[:=]\s*\"?([0-9a-f]{8,})", re.IGNORECASE)
_REPORT_STATUS_ALT = "|".join(_GEN.REPORT_STATUS_VALUES)
REPORT_LINE_PATTERNS = (
    re.compile(r"^%s:\s*(?:%s)\b" % (_GEN.REPORT_HEADERS[0], _REPORT_STATUS_ALT), re.IGNORECASE),
    re.compile(r"^%s:\s*\S+" % _GEN.REPORT_HEADERS[1]),
    re.compile(r"^%s:\s*\S+" % _GEN.REPORT_HEADERS[2]),
    re.compile(r"^%s:\s*\S+" % _GEN.REPORT_HEADERS[3]),
)

REPORT_FORMAT_HEADER = "REPORT FORMAT:"
REPORT_FORMAT_HEADERS = tuple(h + ":" for h in _GEN.REPORT_HEADERS)
REPORT_FORMAT_PHRASE_RE = re.compile(r"REPORT FORMAT", re.IGNORECASE)
REPORT_FORMAT_SUPERSEDE = (
    "REPORT FORMAT (required by the orchestrator hook; this supersedes any other report format above):"
)
REPORT_FORMAT_SCOUT = _GEN.REPORT_FORMAT_SCOUT
REPORT_FORMAT_WORKER = _GEN.REPORT_FORMAT_WORKER


# The caller's own report instructions ("REPORT FORMAT: STATUS/FILES/TESTS")
# describe the answer, not the task, so routing ignores them.
REPORT_FORMAT_TAIL_RE = re.compile(r"(?:^|\n|[.!?]\s+)[ \t]*(?:#+\s*|\*\*)?REPORT FORMAT\b.*\Z", re.IGNORECASE | re.DOTALL)


def strip_report_format(prompt: str) -> str:
    """Drop a trailing REPORT FORMAT block, if any, before classification."""
    return REPORT_FORMAT_TAIL_RE.sub("", prompt or "").strip()


def prompt_has_report_format(prompt: str) -> bool:
    """True only when the prompt carries all four canonical headers; an ad-hoc
    'REPORT FORMAT:' with other headers does not count (SubagentStop would reject it)."""
    text = prompt or ""
    return all(h in text for h in REPORT_FORMAT_HEADERS)


def append_report_format(prompt: str, tier: str) -> str:
    """Append the bounded four-header REPORT FORMAT block when the prompt lacks one.

    If the prompt already names a different report format, the canonical block is
    still appended, headed by a line saying it supersedes the earlier one."""
    if prompt_has_report_format(prompt):
        return prompt
    block = REPORT_FORMAT_SCOUT if tier == "flash_lite" else REPORT_FORMAT_WORKER
    if REPORT_FORMAT_PHRASE_RE.search(prompt or ""):
        block = block.replace(REPORT_FORMAT_HEADER, REPORT_FORMAT_SUPERSEDE, 1)
    return (prompt or "").rstrip() + "\n\n" + block


def _append_report_format_enabled() -> bool:
    return _get_env("AGY_MITOSIS_APPEND_REPORT_FORMAT", default="1") != "0"


TEST_RUNNER_RE = re.compile(
    r"\b(pytest|python(?:\d+(?:\.\d+)?)?\s+-m\s+unittest|npm\s+test|pnpm\s+test|yarn\s+test|vitest|jest|cargo\s+test|go\s+test)\b",
    re.IGNORECASE,
)
LOG_OR_TRANSCRIPT_RE = re.compile(
    r"(?:^|[/\\])(?:transcript(?:_full)?\.jsonl|[^/\\]+\.log)$",
    re.IGNORECASE,
)
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "Read", "NotebookRead", "read_file", "read"}
MUTATION_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "write_file",
    "apply_patch",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _extract_apply_patch_files(patch_text_or_args: Any) -> List[str]:
    """Extract list of target file paths from apply_patch tool arguments or raw diff text."""
    files: List[str] = []
    text = ""
    if isinstance(patch_text_or_args, dict):
        for k in ("file_path", "filePath", "path", "target_file", "targetFile", "file"):
            val = patch_text_or_args.get(k)
            if isinstance(val, str) and val.strip():
                files.append(val.strip())
        list_val = patch_text_or_args.get("files") or patch_text_or_args.get("file_paths")
        if isinstance(list_val, list):
            for v in list_val:
                if isinstance(v, str) and v.strip():
                    files.append(v.strip())
        for k in ("patch", "diff", "content", "patch_text"):
            val = patch_text_or_args.get(k)
            if isinstance(val, str):
                text += "\n" + val
    elif isinstance(patch_text_or_args, str):
        text = patch_text_or_args

    if text:
        for m in re.finditer(r"^\s*diff\s+--git\s+[a-z]/(\S+)\s+[a-z]/(\S+)", text, re.MULTILINE):
            files.append(m.group(2))
        for m in re.finditer(r"^\s*Index:\s+([^\r\n\t]+)", text, re.MULTILINE):
            files.append(m.group(1).strip())
        for m in re.finditer(r"^\s*\*\*\*\s+([^\r\n\t]+)", text, re.MULTILINE):
            path_str = m.group(1).strip()
            path_str = re.split(r"\s{2,}|\t", path_str)[0].strip()
            if path_str.startswith(("a/", "b/")):
                path_str = path_str[2:]
            files.append(path_str)
        for m in re.finditer(r"^\s*(?:---|\+\+\+)\s+([^\r\n\t]+)", text, re.MULTILINE):
            path_str = m.group(1).strip()
            path_str = re.split(r"\s{2,}|\t", path_str)[0].strip()
            if path_str.startswith(("a/", "b/")):
                path_str = path_str[2:]
            files.append(path_str)

    cleaned: List[str] = []
    seen = set()
    for f in files:
        f = f.strip().strip("'\"")
        if not f or f in ("/dev/null", "dev/null"):
            continue
        if f not in seen:
            seen.add(f)
            cleaned.append(f)
    return cleaned


def _extract_tool_call(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if is_codex_payload(payload):
        name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
        inp = payload.get("tool_input") or payload.get("args") or payload.get("arguments") or payload.get("input")
        if not name:
            tc = payload.get("tool_call") or payload.get("toolCall")
            if isinstance(tc, dict):
                name = str(tc.get("name") or "").strip()
                inp = tc.get("args") or tc.get("arguments") or tc.get("input")
        args = inp if isinstance(inp, dict) else {}
        return name, args
    if is_claude_code_payload(payload):
        name = str(payload.get("tool_name") or "").strip()
        inp = payload.get("tool_input")
        args = inp if isinstance(inp, dict) else {}
        return name, args
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or "").strip()
        args = tool_call.get("args")
        return name, args if isinstance(args, dict) else {}
    return "", {}


def _allow(
    reason: str = "Mitosis Orchestrator: pass-through.",
    overrides: Optional[List[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    updated_input: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if payload and is_claude_code_payload(payload):
        out: Dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            out["updatedInput"] = updated_input
        return {"hookSpecificOutput": out}
    if payload and is_codex_hook_payload(payload):
        # Codex applies updatedInput only alongside "allow". A bare allow would
        # skip Codex's own approval, so pass-through stays silent.
        if updated_input is None and not overrides:
            return {}
        out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": reason}
        if updated_input is not None:
            out["updatedInput"] = updated_input
        return {"hookSpecificOutput": out}
    if payload and is_codex_payload(payload):
        res: Dict[str, Any] = {
            "decision": "allow",
            "reason": reason,
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            res["updatedInput"] = updated_input
            res["overwrite"] = updated_input
        if overrides:
            res["permissionOverrides"] = overrides
        return res
    res: Dict[str, Any] = {"decision": "allow", "reason": reason}
    if updated_input is not None:
        res["overwrite"] = updated_input
    if overrides:
        res["permissionOverrides"] = overrides
    return res


def _no_opinion(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pass-through without a permission verdict.

    Claude Code treats permissionDecision "allow" as "skip the permission
    prompt", so a hook with nothing to say must return {} there. Antigravity
    and Codex read a bare "allow" as no objection.
    """
    if payload and is_claude_code_payload(payload):
        return {}
    return _allow("Mitosis Orchestrator: pass-through.", payload=payload)


def _deny(reason: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and (is_claude_code_payload(payload) or is_codex_hook_payload(payload)):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if payload and is_codex_payload(payload):
        return {
            "decision": "deny",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    return {"decision": "deny", "reason": reason}


def _ask(reason: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if payload and is_claude_code_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }
    if payload and is_codex_payload(payload):
        # Codex hooks honour allow/deny only; "ask" is treated as a failed hook.
        if is_codex_hook_payload(payload):
            return _deny(reason, payload)
        return {
            "decision": "deny",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    return {"decision": "ask", "reason": reason}


def _delegation_hint(payload: Optional[Dict[str, Any]], tier: str) -> str:
    """Platform-specific wording that tells the primary agent how to delegate."""
    if payload and is_claude_code_payload(payload):
        if tier == "flash_lite":
            return (
                f"`Agent` with `subagent_type: \"{CLAUDE_PLUGIN_PREFIX}:{SCOUT_TYPE}\"` "
                f"(model: {claude_tier_model('flash_lite')})"
            )
        return (
            f"`Agent` with `subagent_type: \"{CLAUDE_PLUGIN_PREFIX}:{WORKER_TYPE}\"` "
            f"(model: {claude_tier_model('flash')})"
        )
    if payload and is_codex_payload(payload):
        if tier == "flash_lite":
            return f"`spawn_agent` with `agent_type: \"explorer\"` ({codex_tier_model('flash_lite')})"
        return f"`spawn_agent` with `agent_type: \"worker\"` ({codex_tier_model('flash')})"
    if tier == "flash_lite":
        return f"`{SCOUT_TYPE}` (TypeName='{SCOUT_TYPE}', Model='flash_lite')"
    return f"`{WORKER_TYPE}` (TypeName='{WORKER_TYPE}', Model='flash')"


def is_primary_agent(payload: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
    """Return True if caller is the primary orchestrator agent (not a subagent)."""
    if payload.get("isSubagent") is True or payload.get("is_subagent") is True:
        return False
    # Claude Code adds agent_id / agent_type to hook input when a subagent makes the call.
    if payload.get("agent_id") or payload.get("agent_type"):
        return False
    cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or "").strip().lower()
    if state and cid and cid in state.get("subagents", {}):
        return False
    for wp in payload.get("workspacePaths") or []:
        if "/worktrees/" in str(wp).replace("\\", "/"):
            return False
    cwd = str(payload.get("cwd") or "").replace("\\", "/")
    if "/worktrees/" in cwd:
        return False
    return True


def validate_subagent_report(text: str) -> Optional[str]:
    """Validate that a subagent report strictly follows the 4-header bounded format.

    Headers required:
      STATUS: COMPLETED | BLOCKED | FAILED
      FILES_CHANGED: <comma-separated paths or NONE>
      VERIFICATION: <description of verification command and result>
      NOTES: <single sentence summary or up to 10 bullet lines (- or *)>

    Constraints:
      - Total length <= 1600 characters.
      - At most 10 bullet lines following NOTES:.
      - If STATUS is BLOCKED or FAILED, NOTES must include a file:// URI to a .log file.

    Returns None if valid, or an error description string if invalid.
    """
    stripped = "\n".join(l for l in (text or "").splitlines() if not REPORT_FENCE_RE.match(l)).strip()
    if not stripped:
        return "Subagent report is empty."
    if len(stripped) > MAX_SUBAGENT_REPORT_CHARS:
        return (
            f"Subagent report length ({len(stripped)} chars) exceeds maximum "
            f"{MAX_SUBAGENT_REPORT_CHARS} chars."
        )
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    max_lines = 4 + MAX_REPORT_BULLET_LINES
    if len(lines) < 4:
        return f"Expected at least 4 header lines (STATUS, FILES_CHANGED, VERIFICATION, NOTES), got {len(lines)}."
    if len(lines) > max_lines:
        return (
            f"Expected at most 4 headers plus {MAX_REPORT_BULLET_LINES} bullet lines "
            f"({max_lines} lines total), got {len(lines)}."
        )

    expected_prefixes = REPORT_FORMAT_HEADERS
    for idx, (pattern, prefix) in enumerate(zip(REPORT_LINE_PATTERNS, expected_prefixes), start=1):
        if not pattern.match(lines[idx - 1]):
            # `NOTES:` may be bare when bullet lines follow it.
            if prefix == "NOTES:" and re.match(r"^NOTES:\s*$", lines[idx - 1], re.IGNORECASE) and len(lines) > 4:
                continue
            return f"Line {idx} must match '{prefix} <value>'."

    bullet_lines = lines[4:]
    for b_idx, b_line in enumerate(bullet_lines, start=5):
        if not REPORT_BULLET_RE.match(b_line):
            return f"Line {b_idx} after NOTES must be a compact bullet starting with '- ' or '* '."

    status_match = re.match(r"^STATUS:\s*(\w+)", lines[0], re.IGNORECASE)
    status_val = status_match.group(1).upper() if status_match else ""
    if status_val in {"BLOCKED", "FAILED"}:
        notes_body = lines[3].split(":", 1)[1].strip() if ":" in lines[3] else ""
        combined_notes = " ".join([notes_body] + [b[1:].strip() for b in bullet_lines]).strip()
        if not REPORT_LOG_URI_RE.search(combined_notes):
            return f"When STATUS is {status_val}, NOTES must include a 'file://.../*.log' scratch log URI."

    return None


SMALL_TIER_RE = re.compile(r"(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])", re.IGNORECASE)
# Matched against whole name segments, so "pro" does not match "proxy" and
# "o1" does not match "product-o1x". Family names match as segment prefixes
# ("qwen3", "deepseek-v4").
FRONTIER_SEGMENTS = {
    "argon", "pro", "ultra", "claude", "opus", "sonnet", "fable", "mythos", "grok", "o1", "o3", "o4",
}
FRONTIER_FAMILY_PREFIXES = ("deepseek", "qwen", "qwq")
FRONTIER_GPT_RE = re.compile(r"(?<![a-z0-9])(?:chat)?gpt-?[4-9]")
# Open-weight checkpoints below this size are not frontier, whatever the family.
PARAM_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")
MIN_FRONTIER_PARAMS_B = 30.0


def is_frontier_tier(model_name: str) -> bool:
    """True for Argon / Pro / Ultra / Claude / frontier models; False for Flash, Lite, Mini, Nano, Haiku, Auto or empty."""
    normalized = (model_name or "").strip().lower()
    if not normalized or normalized in {"auto", "none", "inherit"}:
        return False
    if SMALL_TIER_RE.search(normalized):
        return False
    sizes = [float(m.group(1)) for m in PARAM_SIZE_RE.finditer(normalized)]
    if sizes and max(sizes) < MIN_FRONTIER_PARAMS_B:
        return False
    if FRONTIER_GPT_RE.search(normalized):
        return True
    segments = [seg for seg in re.split(r"[^a-z0-9]+", normalized) if seg]
    return any(seg in FRONTIER_SEGMENTS or seg.startswith(FRONTIER_FAMILY_PREFIXES) for seg in segments)


is_mitosis_tier = is_frontier_tier
is_argon_tier = is_frontier_tier


def _get_env(*names: str, default: str = "") -> str:
    expanded_names: List[str] = []
    for name in names:
        if name.startswith("AGY_MITOSIS_"):
            suffix = name[len("AGY_MITOSIS_"):]
            expanded_names.append("CODEX_MITOSIS_" + suffix)
            expanded_names.append("CLAUDE_MITOSIS_" + suffix)
        expanded_names.append(name)
    for name in expanded_names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return default


def _env_flag(*names: str) -> bool:
    expanded_names: List[str] = []
    for name in names:
        if name.startswith("AGY_MITOSIS_"):
            suffix = name[len("AGY_MITOSIS_"):]
            expanded_names.append("CODEX_MITOSIS_" + suffix)
            expanded_names.append("CLAUDE_MITOSIS_" + suffix)
        expanded_names.append(name)
    for name in expanded_names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip() == "1"
    return False


def claude_tier_model(tier: str) -> str:
    """Claude Code model for a subagent tier, honouring CLAUDE_MITOSIS_{SCOUT,WORKER}_MODEL."""
    if tier == "flash_lite":
        default, env_name = DEFAULT_CLAUDE_SCOUT_MODEL, "CLAUDE_MITOSIS_SCOUT_MODEL"
    else:
        default, env_name = DEFAULT_CLAUDE_WORKER_MODEL, "CLAUDE_MITOSIS_WORKER_MODEL"
    override = _get_env(env_name).lower()
    return override if override in CLAUDE_SUBAGENT_MODELS else default


def claude_tier_effort(tier: str) -> Optional[str]:
    """Claude Code effort for a subagent tier (None when CLAUDE_MITOSIS_PIN_EFFORT=0).

    Scouts default to `low`, workers to `medium`; CLAUDE_MITOSIS_{SCOUT,WORKER}_EFFORT
    override them. Invalid values fall back to the default.
    """
    if _get_env("CLAUDE_MITOSIS_PIN_EFFORT", default="1") == "0":
        return None
    if tier == "flash_lite":
        default, env_name = DEFAULT_CLAUDE_SCOUT_EFFORT, "CLAUDE_MITOSIS_SCOUT_EFFORT"
    else:
        default, env_name = DEFAULT_CLAUDE_WORKER_EFFORT, "CLAUDE_MITOSIS_WORKER_EFFORT"
    override = _get_env(env_name).lower()
    return override if override in CLAUDE_EFFORT_LEVELS else default


CODEX_EFFORT_LEVELS = CLAUDE_EFFORT_LEVELS | {"minimal"}
CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def valid_codex_model(value: Optional[str]) -> bool:
    """True when value is safe to embed in a shell command and a TOML string."""
    return bool(value) and CODEX_MODEL_RE.match(value or "") is not None


# These CODEX_MITOSIS_* names are read straight from os.environ on purpose and have no
# AGY_ form: _get_env expands AGY_MITOSIS_X into CLAUDE_MITOSIS_X as well, so an
# AGY_MITOSIS_SCOUT_MODEL would become CLAUDE_MITOSIS_SCOUT_MODEL=haiku and leak into Codex.
def codex_tier_model(tier: str) -> str:
    """Codex model for a subagent tier, honouring CODEX_MITOSIS_{SCOUT,WORKER}_MODEL.

    Values that do not match CODEX_MODEL_RE are ignored, because the model is later
    spliced into a shell command by the codex exec -m gpt-5.6-luna pinning path.
    """
    if tier == "flash_lite":
        default, env_name = DEFAULT_CODEX_SCOUT_MODEL, "CODEX_MITOSIS_SCOUT_MODEL"
    else:
        default, env_name = DEFAULT_CODEX_WORKER_MODEL, "CODEX_MITOSIS_WORKER_MODEL"
    override = os.environ.get(env_name, "").strip()
    return override if valid_codex_model(override) else default


def codex_tier_effort(tier: str) -> str:
    """Codex reasoning effort for a subagent tier, honouring CODEX_MITOSIS_{SCOUT,WORKER}_EFFORT."""
    if tier == "flash_lite":
        default, env_name = DEFAULT_CODEX_SCOUT_EFFORT, "CODEX_MITOSIS_SCOUT_EFFORT"
    else:
        default, env_name = DEFAULT_CODEX_WORKER_EFFORT, "CODEX_MITOSIS_WORKER_EFFORT"
    override = os.environ.get(env_name, "").strip().lower()
    return override if override in CODEX_EFFORT_LEVELS else default


def codex_config_path(payload: Optional[Dict[str, Any]] = None) -> str:
    codex_override = os.environ.get("CODEX_CONFIG_PATH", "").strip()
    if codex_override:
        return os.path.expanduser(codex_override)
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return str(Path(os.path.expanduser(codex_home)) / "config.toml")
    return os.path.expanduser("~/.codex/config.toml")


def settings_path(payload: Optional[Dict[str, Any]] = None) -> str:
    codex_override = os.environ.get("CODEX_CONFIG_PATH", "").strip()
    if codex_override:
        return os.path.expanduser(codex_override)
    if payload and is_codex_payload(payload):
        return codex_config_path(payload)
    claude_override = os.environ.get("CLAUDE_SETTINGS_PATH", "").strip()
    if claude_override:
        return os.path.expanduser(claude_override)
    if payload and is_claude_code_payload(payload):
        return os.path.expanduser("~/.claude/settings.json")
    agy_override = os.environ.get("AGY_ANTIGRAVITY_SETTINGS", "").strip()
    if agy_override:
        return os.path.expanduser(agy_override)
    return os.path.expanduser("~/.gemini/antigravity-cli/settings.json")


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-tolerant lookup: _get(item, "Model", "model")."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


# ---------------------------------------------------------------------------
# State (per conversation, lives in the artifact directory)
# ---------------------------------------------------------------------------

def _empty_state() -> Dict[str, Any]:
    return {
        "warned_invocations": [],
        "pending_spawns": [],
        "subagents": {},
        "stop_reminded_sets": [],
        "transcript_offset": 0,
        "turn_edited_files": [],
        "consecutive_failures": 0,
    }


def _state_file(payload: Dict[str, Any]) -> Optional[Path]:
    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        base = Path(artifact_dir)
    else:
        cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or payload.get("turn_id") or "default")
        # The id comes from the payload; keep it a single safe path segment.
        cid = re.sub(r"[^A-Za-z0-9_-]", "_", cid)[:128] or "default"
        base = Path(tempfile.gettempdir()) / "mitosis-orchestrator" / cid
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / STATE_FILENAME


LOCK_FILENAME = ".mitosis_orchestrator_state.lock"

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore

# Held lock fds keyed by lock path. flock locks belong to the open file description, so a
# second open of the same file in this process would block against the first; keeping the
# fd here makes re-entrant acquisition (nested handler calls) a no-op.
_HELD_LOCKS: Dict[str, int] = {}


def _lock_timeout() -> float:
    try:
        return max(0.0, float(_get_env("AGY_MITOSIS_LOCK_TIMEOUT", "CLAUDE_MITOSIS_LOCK_TIMEOUT", "CODEX_MITOSIS_LOCK_TIMEOUT", default="2")))
    except ValueError:
        return 2.0


def _try_lock_fd(fd: int) -> bool:
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _msvcrt is not None:  # pragma: no cover - Windows
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True


def _acquire_state_lock(state_path: Path) -> None:
    """Take the per-session lock (idempotent). Fails open after the timeout; never raises."""
    if _fcntl is None and _msvcrt is None:
        return
    lock_path = str(state_path.with_name(LOCK_FILENAME))
    if lock_path in _HELD_LOCKS:
        return
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return
    deadline = time.monotonic() + _lock_timeout()
    while True:
        if _try_lock_fd(fd):
            _HELD_LOCKS[lock_path] = fd
            return
        if time.monotonic() >= deadline:
            os.close(fd)  # proceed unlocked
            return
        time.sleep(0.02)


def _release_state_locks() -> None:
    for lock_path, fd in list(_HELD_LOCKS.items()):
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _HELD_LOCKS.pop(lock_path, None)


_HANDLER_DEPTH = [0]


def _releases_state_locks(func):
    """Decorator: the outermost public handler call always drops the state locks."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        _HANDLER_DEPTH[0] += 1
        try:
            return func(*args, **kwargs)
        finally:
            _HANDLER_DEPTH[0] -= 1
            if _HANDLER_DEPTH[0] == 0:
                _release_state_locks()
    return wrapper


def _read_state_file(path: Optional[Path]) -> Dict[str, Any]:
    state = _empty_state()
    if path and path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                state.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return state


def _peek_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Lock-free read for decisions made before any network call. Never save this copy."""
    return _read_state_file(_state_file(payload))


def load_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Read the session state and take its lock (held until the handler returns)."""
    path = _state_file(payload)
    if path:
        _acquire_state_lock(path)
    return _read_state_file(path)


def save_state(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = _state_file(payload)
    if not path:
        return
    # A unique temp name per writer: parallel hooks sharing one ".tmp" could
    # rename each other's half-written file into place.
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def sync_subagents_from_transcript(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Pair pending spawns (FIFO) with new subagent conversationIds in the transcript.

    Only lines mentioning invoke_subagent are inspected, so unrelated UUIDs in
    the transcript (artifacts, task ids, the parent conversation) are ignored.
    Reading resumes from the last byte offset to stay cheap on long sessions.
    """
    transcript_path = payload.get("transcriptPath")
    if not transcript_path or not os.path.exists(transcript_path):
        return
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    parent_cid = str(payload.get("conversationId") or "").lower()
    offset = int(state.get("transcript_offset") or 0)
    try:
        size = os.path.getsize(transcript_path)
        if offset > size:
            offset = 0
        with open(transcript_path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                offset += len(raw)
                if not pending:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if "invoke_subagent" not in line and "Created the following subagents" not in line:
                    continue
                for match in CONVERSATION_ID_RE.finditer(line):
                    cid = match.group(1).lower()
                    if cid == parent_cid or cid in subagents:
                        continue
                    if not pending:
                        break
                    subagents[cid] = pending.pop(0)
        state["transcript_offset"] = offset
    except OSError:
        return


SUBAGENT_ALIVE_STATE = "SUBAGENT_STATE_ALIVE"


def _subagent_metadata_dir(payload: Dict[str, Any]) -> Optional[Path]:
    artifact_dir = payload.get("artifactDirectoryPath")
    if not artifact_dir:
        return None
    path = Path(artifact_dir) / ".system_generated" / "subagents"
    return path if path.is_dir() else None


def sync_subagents_from_artifacts(payload: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Pair pending spawns with <artifactDirectoryPath>/.system_generated/subagents/<cid>.json.

    Antigravity writes one file per spawned subagent with its typeName, role,
    live state and workspace URIs. This is authoritative, so it runs before the
    transcript fallback. Returns True when the directory exists.
    """
    meta_dir = _subagent_metadata_dir(payload)
    if meta_dir is None:
        return False
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    try:
        files = sorted(meta_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return True
    for file in files:
        try:
            with open(file, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        cid = str(meta.get("conversationId") or file.stem).strip().lower()
        descriptor = meta.get("subagentDescriptor") or {}
        type_name = str(descriptor.get("typeName") or "").strip()
        role = str(descriptor.get("role") or "").strip()
        alive = str(meta.get("state") or "") == SUBAGENT_ALIVE_STATE
        uris = meta.get("workspaceUris") or []
        in_worktree = any("/worktrees/" in str(u) for u in uris)

        record = subagents.get(cid)
        if record is None:
            # Prefer an exact (type, role) match, then a type match, then FIFO.
            idx = next((i for i, p in enumerate(pending)
                        if p.get("type", "").lower() == type_name.lower() and p.get("role") == role), None)
            if idx is None:
                idx = next((i for i, p in enumerate(pending)
                            if p.get("type", "").lower() == type_name.lower()), None)
            if idx is None and pending:
                idx = 0
            if idx is not None:
                record = pending.pop(idx)
            else:
                record = {
                    "role": role or type_name,
                    "type": type_name,
                    "model": "flash_lite" if type_name.lower() == SCOUT_TYPE else "flash",
                    "tier": "flash_lite" if type_name.lower() == SCOUT_TYPE else "flash",
                    "workspace": "branch" if in_worktree else "inherit",
                }
            subagents[cid] = record
        if in_worktree:
            record["workspace"] = "branch"
        record["alive"] = alive
        record["active_branch"] = bool(record.get("workspace") == "branch" and alive)
    return True


def sync_subagents(payload: Dict[str, Any], state: Dict[str, Any]) -> None:
    sync_subagents_from_artifacts(payload, state)
    if state.get("pending_spawns"):
        sync_subagents_from_transcript(payload, state)


# ---------------------------------------------------------------------------
# session-start
# ---------------------------------------------------------------------------

def _read_settings(path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    target_path = path or settings_path()
    if not os.path.exists(target_path):
        return None, target_path
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else None), target_path
    except (OSError, json.JSONDecodeError):
        return None, target_path


def _write_settings_atomic(path: str, data: Dict[str, Any]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            json.dump(data, tmp_f, indent=2)
            tmp_f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _read_codex_config_toml(path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    target_path = path or codex_config_path()
    if not os.path.exists(target_path):
        return None, target_path
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'^\s*model\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        model = m.group(1).strip() if m else None
        data = {"model": model} if model else {}
        return data, target_path
    except OSError:
        return None, target_path


def _write_codex_config_model(path: str, model: str) -> None:
    dir_name = os.path.dirname(path) or "."
    try:
        os.makedirs(dir_name, exist_ok=True)
    except OSError:
        pass
    content = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    if re.search(r'^\s*model\s*=.*$', content, flags=re.MULTILINE):
        new_content = re.sub(r'^\s*model\s*=.*$', f'model = "{model}"', content, flags=re.MULTILINE)
    else:
        new_content = f'model = "{model}"\n' + content

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(new_content)
            if not new_content.endswith("\n"):
                tmp_f.write("\n")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def check_primary_model_settings(payload: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Return (status, model) where status is one of:
    'ok' (frontier/mitosis), 'warn' (non-frontier, not changed), 'pinned' (rewritten, opt-in),
    'unknown' (settings file missing/unreadable), 'silenced'.
    """
    path = settings_path(payload)
    is_codex = (payload and is_codex_payload(payload)) or "CODEX_CONFIG_PATH" in os.environ
    is_claude = (payload and is_claude_code_payload(payload)) or "CLAUDE_SETTINGS_PATH" in os.environ
    preferred = _get_env("CODEX_MITOSIS_PRIMARY_MODEL", "CLAUDE_MITOSIS_PRIMARY_MODEL", "AGY_MITOSIS_PRIMARY_MODEL", "AGY_ARGON_PRIMARY_MODEL")
    default_model = DEFAULT_CODEX_PRIMARY_MODEL if is_codex else (DEFAULT_CLAUDE_PRIMARY_MODEL if is_claude else DEFAULT_PRIMARY_MODEL)
    target = preferred if (preferred in ALLOWED_PRIMARY_MODELS or is_frontier_tier(preferred)) else default_model
    if is_codex:
        data, path = _read_codex_config_toml(path)
    else:
        data, path = _read_settings(path)
    if data is None:
        return "unknown", target
    current = str(data.get("model") or "").strip()

    if _env_flag(
        "CODEX_MITOSIS_ALLOW_NON_PRIMARY",
        "CLAUDE_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_ALLOW_NON_MITOSIS_PRIMARY",
        "AGY_ALLOW_NON_ARGON_PRIMARY",
        "AGY_ARGON_ALLOW_NON_PRIMARY",
    ):
        return "silenced", current or target
    if is_frontier_tier(current) and (not preferred or current == preferred):
        return "ok", current
    if not _env_flag("CODEX_MITOSIS_PIN_SETTINGS", "CLAUDE_MITOSIS_PIN_SETTINGS", "AGY_MITOSIS_PIN_SETTINGS", "AGY_ARGON_PIN_SETTINGS"):
        return "warn", current
    try:
        if is_codex:
            _write_codex_config_model(path, target)
        else:
            data["model"] = target
            _write_settings_atomic(path, data)
        return "pinned", target
    except Exception:
        return "warn", current


POLICY_NOTICE = (
    "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
    "location, git/log checks, single-file inspection) to `flash_lite` "
    "(`TypeName: \"flash-lite-scout\"`) and multi-file research, test runs, web/docs "
    "research and scoped code edits to `flash` (`TypeName: \"flash-worker\"`). "
    "Never use `Model: \"inherit\"` or `Model: \"pro\"` on subagents. "
    "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
)

def claude_policy_notice() -> str:
    scout_model, worker_model = claude_tier_model("flash_lite"), claude_tier_model("flash")
    scout_effort, worker_effort = claude_tier_effort("flash_lite"), claude_tier_effort("flash")
    scout_tag = f"{scout_model}, effort {scout_effort}" if scout_effort else scout_model
    worker_tag = f"{worker_model}, effort {worker_effort}" if worker_effort else worker_model
    return (
        "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
        "location, git/log checks, single-file inspection) to the `Agent` tool with "
        f"`subagent_type: \"mitosis-orchestrator:flash-lite-scout\"` ({scout_tag}) and multi-file research, "
        "test runs, web/docs research and scoped code edits to "
        f"`subagent_type: \"mitosis-orchestrator:flash-worker\"` ({worker_tag}). "
        "Untyped `Agent` calls are classified by prompt; other plugins' agents and built-in types "
        "(Explore, Plan, general-purpose) are left untouched. "
        "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
    )


# Backwards-compatible constant (default configuration).
CLAUDE_POLICY_NOTICE = claude_policy_notice()

def codex_policy_notice() -> str:
    return (
        "Subagent pinning is active: delegate single-hop lookups (grep/glob, file or symbol "
        f"location, git/log checks, single-file inspection) to `{codex_tier_model('flash_lite')}` "
        "(`agent_type: \"explorer\"`) and multi-file research, test runs, web/docs "
        f"research and scoped code edits to `{codex_tier_model('flash')}` (`agent_type: \"worker\"`). "
        "Never use `model: \"inherit\"` on subagents. "
        "Every subagent prompt must end with the four-line REPORT FORMAT from rules/AGENTS.md."
    )


# Backwards-compatible constant (default configuration).
CODEX_POLICY_NOTICE = codex_policy_notice()


@_releases_state_locks
def handle_session_start(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = payload or {}
    target_file = "config.toml" if (is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ) else "settings.json"
    if is_codex_hook_payload(p) and p.get("model"):
        # Codex plugin hooks report the live model; config.toml may name another.
        model, target_file = str(p["model"]), "the session"
        if is_frontier_tier(model):
            status = "ok"
        elif _env_flag("CODEX_MITOSIS_ALLOW_NON_PRIMARY", "AGY_MITOSIS_ALLOW_NON_PRIMARY"):
            status = "silenced"
        else:
            status = "warn"
    else:
        status, model = check_primary_model_settings(p)
    if status == "ok":
        note = f"Primary model is '{model}'."
    elif status == "pinned":
        note = f"Restored primary model pin to '{model}' in {target_file} (pin settings opted-in)."
    elif status == "warn":
        note = (
            f"WARNING: {target_file} primary model is '{model or 'unset'}', not a frontier model. "
            "Ask the user to switch to a supported frontier model (Gemini Argon/Pro/Ultra, Claude Opus/Sonnet, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before orchestrating, "
            "or set CODEX_MITOSIS_ALLOW_NON_PRIMARY=1 / AGY_MITOSIS_ALLOW_NON_PRIMARY=1 / CLAUDE_MITOSIS_ALLOW_NON_PRIMARY=1 to silence this."
        )
    elif status == "silenced":
        note = f"Primary model is '{model}' (non-frontier warnings silenced)."
    else:
        note = f"{target_file} not found; the live model is checked on every turn instead."

    if is_codex_hook_payload(p):
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            },
        }
    if is_codex_payload(p) or "CODEX_CONFIG_PATH" in os.environ:
        return {
            "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            "systemMessage": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {codex_policy_notice()}",
            },
        }
    if is_claude_code_payload(p):
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[Mitosis Orchestrator Active] {note} {claude_policy_notice()}",
            }
        }
    return {
        "injectSteps": [
            {"systemMessage": {"systemMessage": f"[Mitosis Orchestrator Active] {note} {POLICY_NOTICE}"}}
        ]
    }


# ---------------------------------------------------------------------------
# pre-invocation / user-prompt-submit
# ---------------------------------------------------------------------------

@_releases_state_locks
def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    # A subagent's own prompt (Codex sends UserPromptSubmit with agent_id) is
    # neither a new primary turn nor a primary model to check.
    if payload.get("agent_id"):
        return {}
    state = load_state(payload)
    state["turn_edited_files"] = []
    state["consecutive_failures"] = 0
    save_state(payload, state)

    model_name = str(payload.get("modelName") or payload.get("model_name") or payload.get("model") or "")
    if not model_name and (is_codex_payload(payload) or "CODEX_CONFIG_PATH" in os.environ):
        data, _ = _read_codex_config_toml(codex_config_path(payload))
        if data and isinstance(data, dict):
            model_name = str(data.get("model") or "")
    elif not model_name and is_claude_code_payload(payload):
        data, _ = _read_settings(settings_path(payload))
        if data and isinstance(data, dict):
            model_name = str(data.get("model") or "")

    if not model_name or is_frontier_tier(model_name) or _env_flag(
        "CODEX_MITOSIS_ALLOW_NON_PRIMARY",
        "CLAUDE_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_MITOSIS_ALLOW_NON_PRIMARY",
        "AGY_ALLOW_NON_MITOSIS_PRIMARY",
        "AGY_ALLOW_NON_ARGON_PRIMARY",
        "AGY_ARGON_ALLOW_NON_PRIMARY",
    ):
        return {}
    cid = str(payload.get("conversationId") or payload.get("session_id") or payload.get("thread_id") or payload.get("thread-id") or "")
    inv_num = payload.get("invocationNum", 0)
    key = f"{cid}:{inv_num}"
    warned: List[str] = state.setdefault("warned_invocations", [])
    if key in warned:
        return {}
    warned.append(key)
    save_state(payload, state)

    warn_text = (
        f"[Mitosis Orchestrator] The active session model is '{model_name}', which is "
        "not a frontier model. Tell the user to switch Model Selection to "
        "a supported frontier model (Claude Opus/Sonnet, Gemini Argon/Pro/Ultra, xAI Grok, OpenAI GPT/o-series, DeepSeek, Qwen) before continuing orchestration work."
    )
    if is_codex_hook_payload(payload):
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": warn_text}}
    if is_codex_payload(payload):
        return {
            "additionalContext": warn_text,
            "systemMessage": warn_text,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn_text,
            },
        }
    if is_claude_code_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn_text,
            }
        }
    return {
        "injectSteps": [
            {
                "ephemeralMessage": warn_text
            }
        ]
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class Decision(NamedTuple):
    """One routing decision with the evidence behind it.

    `confidence` is a prior per rule for now (see RULE_CONFIDENCE); `source`
    says which layer decided: rule, model, judge or default.
    """
    tier: str
    type_override: Optional[str]
    effort: Optional[str]
    confidence: float
    source: str
    rule: str
    reason: str
    model: Optional[str] = None
    platform: str = ""

    def as_log(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "tier": self.tier,
            "type_override": self.type_override,
            "effort": self.effort,
            "confidence": self.confidence,
            "source": self.source,
            "rule": self.rule,
            "reason": self.reason,
        }
        if self.model:
            out["model"] = self.model
        if self.platform:
            out["platform"] = self.platform
        return out


# Prior confidence per rule. Explicit signals are near certain; the prompt
# length fallbacks are guesses and are the cases a judge should take over.
RULE_CONFIDENCE = {
    "worker-type": 0.95,
    "mutation": 0.95,
    "scout-type": 0.95,
    "workspace": 0.95,
    "heavy": 0.9,
    "heavy-noun": 0.75,
    "self-type": 0.95,
    "explicit-model": 0.95,
    "lite": 0.85,
    "long-prompt": 0.5,
    "short-prompt": 0.5,
    "default": 0.5,
}

# Rules a judge may overrule when their confidence is low. Type, workspace and
# explicit-model rules state the caller's intent and are never sent to a judge.
JUDGEABLE_RULES = {"mutation", "heavy", "heavy-noun", "lite", "long-prompt", "short-prompt", "default"}

# Decisions made during this process, read by _debug_log. Hooks are one-shot
# processes; the cap keeps the long-running MCP server from growing it.
_RECENT_DECISIONS: List[Decision] = []

_JUDGE_MODULE: Any = None


def _tier_effort(tier: str) -> str:
    return "low" if tier == "flash_lite" else "medium"


def _rule_decision(tier: str, type_override: Optional[str], rule: str, reason: str) -> Decision:
    return Decision(tier, type_override, _tier_effort(tier), RULE_CONFIDENCE[rule], "rule", rule, reason)


def _load_judge() -> Any:
    """Import scripts/mitosis_judge.py lazily, only when a judge is configured."""
    global _JUDGE_MODULE
    if _JUDGE_MODULE is None:
        import importlib.util
        path = Path(__file__).resolve().with_name("mitosis_judge.py")
        spec = importlib.util.spec_from_file_location("mitosis_judge", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _JUDGE_MODULE = module
    return _JUDGE_MODULE


def _apply_judge(decision: Decision, prompt: str, scout_typed: bool, worker_type_name: str) -> Decision:
    """Hand a low-confidence rule decision to the configured judge (MITOSIS_JUDGE_URL)."""
    if decision.rule not in JUDGEABLE_RULES or not _get_env("MITOSIS_JUDGE_URL"):
        return decision
    try:
        threshold = float(_get_env("MITOSIS_JUDGE_THRESHOLD", default="0.9"))
    except ValueError:
        threshold = 0.9
    if decision.confidence >= threshold:
        return decision
    try:
        verdict = _load_judge().judge_tier(prompt)
    except Exception as exc:  # the judge must never break routing
        verdict = None
        decision = decision._replace(reason=f"{decision.reason}; judge error {type(exc).__name__}")
    if verdict is None:
        # Unreachable or unsure judge: take the tier that cannot fail the task.
        return Decision("flash", decision.type_override, _tier_effort("flash"), decision.confidence,
                        "default", decision.rule, f"{decision.reason}; judge unavailable, conservative flash")
    tier, confidence = verdict
    type_override = decision.type_override
    if scout_typed:
        # Only a mutation verb reaches here for a scout: the judge confirms or vetoes the upgrade.
        type_override = worker_type_name if tier == "flash" else None
    return Decision(tier, type_override, _tier_effort(tier), round(confidence, 3), "judge", decision.rule,
                    f"{decision.reason}; judge chose {tier} ({confidence:.2f})")


def _read_only_has_last_word(text: str, rule: str) -> bool:
    """True when the prompt declares itself read-only and no change request follows.

    "Do not modify anything. Fix the typo" ends on an imperative, so the later
    instruction wins and the mutation keeps its confidence. A keyword inside a
    clause ("Read-only: summarize fixes") is the topic and does not count.
    """
    declarations = list(READ_ONLY_RE.finditer(text))
    if not declarations:
        return False
    if rule != "mutation":
        return True
    last = declarations[-1].end()
    return not any(offset >= last and CLAUSE_START_RE.search(text[:offset]) for offset, _ in find_mutations(text))


def decide_subagent(subagent: Dict[str, Any]) -> Decision:
    """Return the routing Decision for a subagent spec: rules first, then the optional judge."""
    decision, prompt, scout_typed, worker_name = _decide_by_rules(subagent)
    if decision.rule in ("mutation", "heavy", "heavy-noun") and _read_only_has_last_word(prompt, decision.rule):
        decision = decision._replace(confidence=min(decision.confidence, READ_ONLY_CONFIDENCE),
                                     reason=f"{decision.reason}; prompt declares read-only")
    decision = _apply_judge(decision, prompt, scout_typed, worker_name)
    _RECENT_DECISIONS.append(decision)
    del _RECENT_DECISIONS[:-16]
    return decision


def _decide_by_rules(subagent: Dict[str, Any]) -> Tuple[Decision, str, bool, str]:
    """Keyword rules. Returns (decision, role+prompt text, scout_typed, worker type name).

    Precedence (first match wins):
      1. flash-worker type                      -> flash
      2. mutation verbs in prompt               -> flash  (scout type is upgraded to worker)
      3. flash-lite-scout type                  -> flash_lite
      4. Workspace branch/share                 -> flash
      5. heavy signals (tests, debug, trace...) -> flash  (beats lite keywords)
      6. TypeName self (full tool set)          -> flash
      7. explicit Model flash/flash_lite        -> honoured
      8. lite signals, prompt <= 600 chars      -> flash_lite
      9. prompt > 450 chars                     -> flash
     10. short prompt (<= 320 chars)            -> flash_lite
     11. otherwise                              -> flash
    """
    raw_type_name = str(_get(subagent, "TypeName", "typeName", "subagent_type", "subagentType", default="")).strip()
    prefix = ""
    bare_type = raw_type_name.lower()
    if ":" in bare_type:
        prefix_part, bare_type = raw_type_name.split(":", 1)
        prefix = f"{prefix_part}:"
        bare_type = bare_type.strip().lower()

    role = str(_get(subagent, "Role", "role", default="")).strip()
    prompt = strip_report_format(str(_get(subagent, "Prompt", "prompt", default="")).strip())
    workspace = str(_get(subagent, "Workspace", "workspace", default="inherit")).strip().lower()
    requested = str(_get(subagent, "Model", "model", default="")).strip().lower()
    text = f"{role}\n{prompt}"
    scout_typed = bare_type == SCOUT_TYPE
    worker_name = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE

    def done(decision: Decision) -> Tuple[Decision, str, bool, str]:
        return decision, text, scout_typed, worker_name

    mutation = find_mutation(text)
    lite = find_lite(text)
    heavy, heavy_noun = find_heavy_ex(text, lite is not None)

    if bare_type == WORKER_TYPE:
        return done(_rule_decision("flash", None, "worker-type", "flash-worker type"))
    if mutation:
        if bare_type == SCOUT_TYPE:
            upgrade_type = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE
            return done(_rule_decision("flash", upgrade_type, "mutation",
                                  f"mutation verb '{mutation}' on read-only scout; upgraded to flash-worker"))
        return done(_rule_decision("flash", None, "mutation", f"mutation verb '{mutation}'"))
    if bare_type == SCOUT_TYPE:
        return done(_rule_decision("flash_lite", None, "scout-type", "flash-lite-scout type"))
    if workspace in ("branch", "share"):
        return done(_rule_decision("flash", None, "workspace", f"Workspace '{workspace}' implies writes"))
    if heavy:
        if heavy_noun:
            return done(_rule_decision("flash", None, "heavy-noun", f"heavy signal '{heavy}' used like a noun"))
        return done(_rule_decision("flash", None, "heavy", f"heavy signal '{heavy}'"))
    if bare_type == "self":
        return done(_rule_decision("flash", None, "self-type", "self type carries the full tool set"))
    if requested in ALLOWED_SUBAGENT_MODELS:
        return done(_rule_decision(requested, None, "explicit-model", "explicit model honoured"))
    if lite and len(prompt) <= 600:
        return done(_rule_decision("flash_lite", None, "lite", f"lite signal '{lite}'"))
    if len(prompt) > 450:
        return done(_rule_decision("flash", None, "long-prompt", "long prompt"))
    if len(prompt) <= 320:
        return done(_rule_decision("flash_lite", None, "short-prompt", "short prompt without heavy signals"))
    return done(_rule_decision("flash", None, "default", "default"))


def _payload_session_id(payload: Dict[str, Any]) -> str:
    for key in ("conversationId", "session_id", "thread_id", "thread-id", "turn_id"):
        if payload.get(key):
            return str(payload[key])
    return ""


def _record_routing(payload: Dict[str, Any], decision: Decision, model: Optional[str],
                    effort: Optional[str], platform: str, agent_name: str) -> None:
    """Record what was actually pinned (model, effort, platform) for the debug log and OTel.

    Replaces `decision` in _RECENT_DECISIONS (matched by identity, appended when absent)
    so the log shows the effort written into updatedInput, not the tier default.
    """
    final = decision._replace(model=model, effort=effort or decision.effort, platform=platform)
    for i, d in enumerate(_RECENT_DECISIONS):
        if d is decision:
            _RECENT_DECISIONS[i] = final
            break
    else:
        _RECENT_DECISIONS.append(final)
        del _RECENT_DECISIONS[:-16]
    try:
        if _otel_enabled():
            _load_sibling("mitosis_otel").queue({
                "agent_name": agent_name,
                "platform": platform,
                "tier": final.tier,
                "rule": final.rule,
                "confidence": final.confidence,
                "source": final.source,
                "model": model,
                "effort": effort,
                "session_id": _payload_session_id(payload),
            })
    except Exception:
        pass


def _otel_enabled() -> bool:
    """Cheap env check so mitosis_otel is only imported when telemetry is on."""
    if not _env_flag("CLAUDE_MITOSIS_OTEL", "AGY_MITOSIS_OTEL", "CODEX_MITOSIS_OTEL", "MITOSIS_OTEL"):
        return False
    return bool(os.environ.get("MITOSIS_OTEL_FILE", "").strip()
                or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def _flush_otel() -> None:
    if _otel_enabled():
        try:
            _load_sibling("mitosis_otel").flush()
        except Exception:
            pass


def classify_subagent(subagent: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
    """Return (model, type_override_or_None, reason); see decide_subagent."""
    decision = decide_subagent(subagent)
    return decision.tier, decision.type_override, decision.reason


def classify_subagent_tier(subagent: Dict[str, Any]) -> str:
    """Backwards-compatible wrapper returning only the model tier."""
    return classify_subagent(subagent)[0]


# ---------------------------------------------------------------------------
# pre-tool-use handlers
# ---------------------------------------------------------------------------

def _handle_invoke_subagent(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    key = "Subagents" if "Subagents" in args else ("subagents" if "subagents" in args else None)
    if key is None or not isinstance(args.get(key), list):
        return {"decision": "allow"}
    if not args[key]:
        return {"decision": "deny", "reason": "Subagents list cannot be empty."}

    updated: List[Any] = copy.deepcopy(args[key])
    changes: List[str] = []
    spawns: List[Dict[str, Any]] = []

    for idx, item in enumerate(updated):
        if not isinstance(item, dict):
            continue
        current_model = str(_get(item, "Model", "model", default="inherit")).strip()
        current_type = str(_get(item, "TypeName", "typeName", default="")).strip()
        label = _get(item, "Role", "role", default=None) or current_type or f"subagent[{idx}]"
        prompt = str(_get(item, "Prompt", "prompt", default="")).strip()
        if not prompt:
            return {"decision": "deny", "reason": f"Subagent '{label}' has an empty Prompt."}

        decision = decide_subagent(item)
        target_model, type_override, reason = decision.tier, decision.type_override, decision.reason
        _record_routing(payload, decision, target_model, None, "agy",
                        current_type or type_override or (SCOUT_TYPE if target_model == "flash_lite" else WORKER_TYPE))
        if current_model != target_model or "model" in item:
            item.pop("model", None)
            item["Model"] = target_model
            changes.append(f"{label}: {current_model} -> {target_model} ({reason})")
        if type_override and current_type.lower() != type_override.lower():
            item.pop("typeName", None)
            item["TypeName"] = type_override
            changes.append(f"{label}: TypeName {current_type} -> {type_override}")
        if _append_report_format_enabled() and not prompt_has_report_format(prompt):
            item.pop("prompt", None)
            item["Prompt"] = append_report_format(prompt, target_model)
            changes.append(f"{label}: appended REPORT FORMAT")

        workspace = str(_get(item, "Workspace", "workspace", default="inherit")).strip().lower()
        spawns.append(
            {
                "role": str(label),
                "type": str(_get(item, "TypeName", "typeName", default="")),
                "model": target_model,
                "tier": "flash_lite" if target_model == "flash_lite" else "flash",
                "workspace": workspace,
                "active_branch": workspace == "branch",
            }
        )

    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    if not changes:
        return {"decision": "allow"}
    return {
        "decision": "allow",
        "reason": "Mitosis Orchestrator pinned subagent model(s): " + "; ".join(changes),
        "overwrite": {key: updated},
    }


def _claude_subagent_in_scope(subagent_type: str) -> Tuple[bool, str]:
    """Return (in_scope, reason). Only this plugin's own agent types and untyped calls are pinned."""
    raw = (subagent_type or "").strip()
    if not raw:
        return True, "untyped Agent call"
    prefix, bare = "", raw.lower()
    if ":" in bare:
        prefix, bare = bare.split(":", 1)
        prefix, bare = prefix.strip(), bare.strip()
    if bare in (WORKER_TYPE, SCOUT_TYPE) and prefix in ("", CLAUDE_PLUGIN_PREFIX):
        return True, f"{bare} type"
    if prefix and prefix != CLAUDE_PLUGIN_PREFIX:
        return False, f"agent '{raw}' belongs to plugin '{prefix}'"
    if bare in CLAUDE_BUILTIN_AGENT_TYPES:
        return False, f"built-in agent type '{raw}'"
    return False, f"unknown agent type '{raw}' left untouched"


def _passthrough_tier(subagent_type: str, model_req: str) -> str:
    bare = subagent_type.split(":")[-1].strip().lower()
    if bare == SCOUT_TYPE:
        return "flash_lite"
    if bare == WORKER_TYPE:
        return "flash"
    if model_req in ("haiku", "flash_lite"):
        return "flash_lite"
    return "flash"


def _handle_claude_task_or_agent(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _deny(f"{tool_name} prompt cannot be empty.", payload)

    subagent_type = str(args.get("subagent_type") or args.get("subagentType") or "").strip()
    description = str(args.get("description") or args.get("role") or "").strip()
    isolation = str(args.get("isolation") or args.get("workspace") or "inherit").strip().lower()
    workspace = "branch" if isolation == "worktree" else isolation
    model_req = str(args.get("model") or "inherit").strip().lower()

    def _passthrough(reason: str) -> Dict[str, Any]:
        # Still track worktree spawns so the Stop reminder can nag about live branches.
        if workspace == "branch":
            state = load_state(payload)
            state.setdefault("pending_spawns", []).append({
                "role": description or subagent_type or tool_name.lower(),
                "type": subagent_type,
                "model": model_req,
                "tier": _passthrough_tier(subagent_type, model_req),
                "workspace": workspace,
                "active_branch": True,
            })
            save_state(payload, state)
        return _allow(f"Mitosis Orchestrator: {reason}; not pinned.", payload=payload)

    if _get_env("CLAUDE_MITOSIS_PIN_SUBAGENTS", default="1") == "0":
        return _passthrough("subagent pinning disabled (CLAUDE_MITOSIS_PIN_SUBAGENTS=0)")

    in_scope, scope_reason = _claude_subagent_in_scope(subagent_type)
    if not in_scope:
        return _passthrough(scope_reason)

    # Untyped call with an explicit, valid Claude model: honour the caller's choice.
    if not subagent_type and model_req in CLAUDE_SUBAGENT_MODELS:
        return _passthrough(f"explicit model '{model_req}' honoured")

    prefix = ""
    bare_type = subagent_type
    if ":" in bare_type:
        prefix_part, bare_type = bare_type.split(":", 1)
        prefix = f"{prefix_part}:"
        bare_type = bare_type.strip()

    # Map Claude Code model strings for classification
    internal_model = "flash_lite" if model_req in ("haiku", "flash_lite") else ("flash" if model_req in ("sonnet", "flash") else model_req)

    item = {
        "TypeName": subagent_type,
        "Role": description,
        "Prompt": prompt,
        "Workspace": workspace,
        "Model": internal_model,
    }

    decision = decide_subagent(item)
    target_tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    claude_model = claude_tier_model(target_tier)

    updated_input = copy.deepcopy(args)
    updated_input["model"] = claude_model
    # Effort: honour an explicit, valid caller value; otherwise pin per tier
    # (scouts low, workers medium) unless CLAUDE_MITOSIS_PIN_EFFORT=0.
    caller_effort = str(args.get("effort") or "").strip().lower()
    if caller_effort in CLAUDE_EFFORT_LEVELS:
        claude_effort: Optional[str] = caller_effort
        effort_note = f"effort {caller_effort} (caller)"
    else:
        claude_effort = claude_tier_effort(target_tier)
        if claude_effort:
            updated_input["effort"] = claude_effort
            effort_note = f"effort {claude_effort}"
        else:
            if caller_effort:
                updated_input.pop("effort", None)  # drop an invalid caller value
            effort_note = "effort unpinned"
    if type_override:
        if prefix and not type_override.startswith(prefix):
            type_override = f"{prefix}{type_override}"
        if "subagentType" in updated_input and "subagent_type" not in updated_input:
            updated_input["subagentType"] = type_override
        else:
            updated_input["subagent_type"] = type_override

    if _append_report_format_enabled() and not prompt_has_report_format(prompt):
        updated_input["prompt"] = append_report_format(prompt, target_tier)
        effort_note += ", REPORT FORMAT appended"

    target_type = type_override or subagent_type
    _record_routing(payload, decision, claude_model, updated_input.get("effort") or claude_effort, "claude",
                    target_type or (SCOUT_TYPE if target_tier == "flash_lite" else WORKER_TYPE))
    spawns = [{
        "role": description or target_type or tool_name.lower(),
        "type": target_type,
        "model": target_tier,
        "tier": target_tier,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    reason_msg = f"Mitosis Orchestrator pinned subagent model: {claude_model}, {effort_note} ({reason})"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason_msg,
            "updatedInput": updated_input,
        }
    }


def _handle_send_message(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    is_claude = is_claude_code_payload(payload)
    recipient = str(_get(args, "Recipient", "recipient", "to", "To", default="")).strip().lower()
    message = str(_get(args, "Message", "message", default=""))
    tool_label = "SendMessage.message" if is_claude else "send_message.Message"
    if not message.strip():
        return _deny(f"{tool_label} cannot be empty.", payload) if is_claude else {"decision": "deny", "reason": f"{tool_label} cannot be empty."}

    state = load_state(payload)
    sync_subagents(payload, state)
    save_state(payload, state)

    info = state.get("subagents", {}).get(recipient)
    if not info or _record_tier(info) != "flash_lite":
        return _no_opinion(payload) if is_claude else {"decision": "allow"}

    mutation = find_mutation(message)
    rule_count = len(NUMBERED_RULE_RE.findall(message))
    if mutation or rule_count > 3:
        why = f"mutation verb '{mutation}'" if mutation else f"{rule_count} numbered steps"
        reason = (
            f"Recipient '{recipient}' is a flash_lite '{info.get('type') or SCOUT_TYPE}' "
            f"subagent ({why}). Do not send code changes or multi-step (>3 rules) tasks to "
            f"a scout. Use {_delegation_hint(payload, 'flash')} instead."
        )
        return _deny(reason, payload) if is_claude else {"decision": "deny", "reason": reason}
    return _no_opinion(payload) if is_claude else {"decision": "allow"}


def _record_tier(info: Dict[str, Any]) -> str:
    """Tier of a spawn record: its `tier` field, else inferred from `model` (old state files)."""
    tier = info.get("tier")
    if tier in ("flash_lite", "flash"):
        return str(tier)
    model = str(info.get("model") or "").strip().lower()
    if model in ("flash_lite", "haiku", DEFAULT_CODEX_SCOUT_MODEL.lower(), codex_tier_model("flash_lite").lower()):
        return "flash_lite"
    return "flash"


def _claude_register_agent(state: Dict[str, Any], agent_id: str, agent_type_hint: str = "") -> Optional[Dict[str, Any]]:
    """Pair a Claude Code agent id with the oldest pending spawn (or infer a record from the type)."""
    agent_id = (agent_id or "").strip().lower()
    if not agent_id:
        return None
    subagents: Dict[str, Dict[str, Any]] = state.setdefault("subagents", {})
    if agent_id in subagents:
        return subagents[agent_id]
    pending: List[Dict[str, Any]] = state.setdefault("pending_spawns", [])
    hint = (agent_type_hint or "").strip()
    bare_hint = hint.split(":", 1)[1].lower() if ":" in hint else hint.lower()
    idx = next((i for i, p in enumerate(pending) if bare_hint and str(p.get("type", "")).split(":")[-1].lower() == bare_hint), None)
    if idx is None and pending:
        idx = 0
    if idx is not None:
        record = pending.pop(idx)
    elif bare_hint:
        record = {
            "role": hint, "type": hint,
            "model": "flash_lite" if bare_hint == SCOUT_TYPE else "flash",
            "tier": "flash_lite" if bare_hint == SCOUT_TYPE else "flash",
            "workspace": "inherit", "active_branch": False,
        }
    else:
        return None
    record["agent_id"] = agent_id
    record["alive"] = True
    subagents[agent_id] = record
    return record


def _claude_agent_ids_from_response(payload: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for text in _iter_strings(payload.get("tool_response")):
        for m in CLAUDE_AGENT_ID_RE.finditer(text):
            aid = m.group(1).lower()
            if aid not in ids:
                ids.append(aid)
    return ids


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _agent_call_bodies(script: str) -> List[str]:
    """Return the argument text of every top-level agent(...) call (balanced parens)."""
    bodies = []
    for m in AGENT_CALL_RE.finditer(script):
        depth, i, start = 1, m.end(), m.end()
        while i < len(script) and depth:
            ch = script[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        bodies.append(script[start : i - 1] if depth == 0 else script[start:])
    return bodies


def workflow_model_violations(script: str) -> List[str]:
    violations = []
    for n, body in enumerate(_agent_call_bodies(script), 1):
        m = MODEL_KWARG_RE.search(body)
        if not m:
            violations.append(f"agent() call #{n} has no model= argument")
            continue
        literal = m.group(1)
        if literal is not None and literal.strip().lower() not in ALLOWED_SUBAGENT_MODELS:
            violations.append(f"agent() call #{n} uses model={literal!r}")
        # A bare identifier (variable) is accepted; it cannot be checked statically.
    return violations


def _handle_run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    violations: List[str] = []
    for text in _iter_strings(args):
        if "agent(" in text or "agent (" in text:
            violations.extend(workflow_model_violations(text))
    if not violations:
        return {"decision": "allow"}
    return {
        "decision": "deny",
        "reason": (
            "run_workflow script must pass model=\"flash\" or model=\"flash_lite\" to every "
            "agent(...) call: " + "; ".join(violations[:5])
        ),
    }


def _handle_define_subagent(args: Dict[str, Any]) -> Dict[str, Any]:
    if args.get("enable_subagent_tools") is True:
        return {
            "decision": "allow",
            "reason": "Mitosis Orchestrator: subagents may not spawn subagents (enable_subagent_tools forced to false).",
            "overwrite": {"enable_subagent_tools": False},
        }
    return {"decision": "allow"}


def _handle_manage_subagents(payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(_get(args, "Action", "action", default="")).strip().lower()
    if action not in {"kill", "kill_all"}:
        return {"decision": "allow"}
    state = load_state(payload)
    sync_subagents(payload, state)
    subagents = state.setdefault("subagents", {})
    pending = state.setdefault("pending_spawns", [])
    if action == "kill_all":
        for meta in subagents.values():
            meta["active_branch"] = False
        for meta in pending:
            meta["active_branch"] = False
    else:
        targets = {str(c).strip().lower() for c in (_get(args, "ConversationIds", "conversationIds", default=[]) or [])}
        unmatched = 0
        for cid in targets:
            if cid in subagents:
                subagents[cid]["active_branch"] = False
            else:
                unmatched += 1
        # Kills that target spawns we never paired with a conversationId.
        for meta in pending:
            if unmatched <= 0:
                break
            if meta.get("active_branch"):
                meta["active_branch"] = False
                unmatched -= 1
    save_state(payload, state)
    return {"decision": "allow"}


# ---------------------------------------------------------------------------
# Workspace scope guard
# ---------------------------------------------------------------------------
# Project-scoped file and command tools are answered "allow" so the primary
# agent is not interrupted for routine work inside its own workspace.
# Interactive sessions only: headless `agy -p` soft-denies every tool that
# would need a prompt regardless of hook output (verified on agy 1.4.0). Anything
# that reaches outside the workspace, looks destructive, or is a tool this
# guard does not understand is left to Antigravity's normal permission flow
# ("ask" or no opinion).

PATH_ARG_KEYS = (
    "TargetFile", "AbsolutePath", "SearchPath", "DirectoryPath", "FilePath",
    "NotebookPath", "Path", "SearchDirectory",
)
PATH_LIST_ARG_KEYS = ("Paths", "Files", "TargetFiles", "Includes")
WRITE_TOOL_RE = re.compile(r"write|replace|edit|create|delete|remove|rename|move|notebook", re.IGNORECASE)

# Absolute prefixes a command may mention without leaving the workspace
# (interpreters, system binaries, scratch space).
SYSTEM_PATH_PREFIXES = (
    "/dev/", "/usr/", "/bin/", "/sbin/", "/lib/", "/opt/", "/proc/",
    "/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/",
    "/System/", "/Library/", "/Applications/",
)

# Commands that must always go through the human permission prompt.
ESCALATION_RE = re.compile(
    r"(?:^|[\s;&|(])(?:sudo|doas|su)\b",
    re.IGNORECASE,
)
RECURSIVE_DELETE_OF_ROOT_RE = re.compile(
    r"(?:^|[\s;&|(])rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/|~|\$HOME)(?:\s|$)",
    re.IGNORECASE,
)
SYSTEM_ALTERING_RE = re.compile(
    r"(?:^|[\s;&|(])(?:mkfs(?:\.\w+)?|diskutil\s+erase\w*|shutdown|reboot|"
    r"launchctl\s+(?:unload|remove|bootout)|chown\s+-R|chmod\s+-R\s+\d+\s+/)",
    re.IGNORECASE,
)
FORCE_PUSH_RE = re.compile(r"\bgit\s+push\b[^|;&]*(?:--force\b|\s-f\b)", re.IGNORECASE)
PIPE_TO_SHELL_RE = re.compile(
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", re.IGNORECASE
)
ABS_PATH_TOKEN_RE = re.compile(r"""(?:^|[\s=:"'(])((?:/|~/?|\$HOME/?)[A-Za-z0-9._/\-]*)""")
DOTDOT_TOKEN_RE = re.compile(r"""(?:^|[\s=:"'(])([A-Za-z0-9._\-/]*\.\.[A-Za-z0-9._\-/]*)""")


def allowed_roots(payload: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for wp in payload.get("workspacePaths") or []:
        try:
            roots.append(Path(os.path.expanduser(str(wp))).resolve())
        except OSError:
            pass
    cwd = payload.get("cwd")
    if cwd:
        try:
            roots.append(Path(os.path.expanduser(str(cwd))).resolve())
        except OSError:
            pass
    proj_dir = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("CODEX_PROJECT_DIR")
        or payload.get("project_dir")
        or payload.get("workspace_dir")
    )
    if proj_dir:
        try:
            roots.append(Path(os.path.expanduser(str(proj_dir))).resolve())
        except OSError:
            pass
    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        try:
            roots.append(Path(str(artifact_dir)).resolve())
        except OSError:
            pass
    return roots


def _within_roots(target: Any, roots: List[Path]) -> bool:
    if not target or not roots:
        return False
    try:
        text = str(target)
        if text.startswith("file://"):
            text = text[len("file://"):]
        resolved = Path(os.path.expanduser(text)).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# Commands a Claude Code scout may run through Bash. Anything else (and any
# output redirection) is denied, so a read-only scout stays read-only.
SCOUT_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "rg", "grep", "egrep", "fgrep", "find", "stat", "file",
    "du", "df", "pwd", "which", "tree", "sort", "cut", "jq", "basename",
    "dirname", "realpath", "readlink", "echo", "date", "uname", "nl", "diff", "cmp", "md5sum",
    "sha256sum",
}
SCOUT_GIT_SUBCOMMANDS = {
    "status", "log", "show", "diff", "blame", "branch", "rev-parse", "ls-files", "ls-tree",
    "grep", "describe", "shortlog", "reflog", "remote", "tag", "config",
}
SCOUT_FORBIDDEN_ARGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
    "tree": {"-o"},
    "diff": {"--output"},
    "log": {"--output"},
    "show": {"--output"},
    "branch": {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"},
    "tag": {"-d", "--delete", "-a", "-s", "-f"},
    "remote": {"add", "remove", "rm", "rename", "set-url", "prune", "set-head"},
    "config": {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--global", "--system"},
    "reflog": {"delete", "expire"},
}
_SCOUT_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;\n]")
_SCOUT_HARMLESS_REDIRECT_RE = re.compile(r"\d?>\s*/dev/null|2>&1")


def scout_command_is_read_only(command: str) -> Tuple[bool, str]:
    """Check a scout's Bash command against the read-only allowlist."""
    cleaned = _SCOUT_HARMLESS_REDIRECT_RE.sub(" ", command)
    if re.search(r"[<>`]|\$\(", cleaned):
        return False, "redirection or command substitution"
    for segment in _SCOUT_SEGMENT_SPLIT_RE.split(cleaned):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False, "unparseable command"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in SCOUT_GIT_SUBCOMMANDS:
                return False, f"git {sub or '(none)'} is not read-only"
            if sub == "config" and not any(w in ("--get", "--get-all", "--list", "-l", "--get-regexp") for w in words):
                return False, "git config without --get/--list"
            bad = {w for w in words[2:] for f in SCOUT_FORBIDDEN_ARGS.get(sub, set())
                   if w == f or (f.startswith("--") and w.startswith(f + "="))}
            if sub == "branch" and len([w for w in words[2:] if not w.startswith("-")]) > 0 and not bad:
                bad = {"<create>"}
            if sub == "tag" and len([w for w in words[2:] if not w.startswith("-")]) > 0 and "-l" not in words and "--list" not in words:
                bad = bad or {"<create>"}
            if bad:
                return False, f"git {sub} {sorted(bad)[0]} changes the repository"
            continue
        if prog not in SCOUT_READ_ONLY_COMMANDS:
            return False, f"'{prog}' is not on the scout allowlist"
        forbidden = SCOUT_FORBIDDEN_ARGS.get(prog, set())
        if any(w in forbidden or any(w.startswith(f) for f in forbidden if f.startswith("--")) for w in words[1:]):
            return False, f"'{prog}' with a writing option"
    return True, ""


def command_stays_in_workspace(command: str, cwd: str, roots: List[Path]) -> Tuple[bool, str]:
    """Return (ok, reason). ok is False when the command names a path outside the
    roots or matches a pattern that should always be confirmed by a human."""
    for pattern, why in (
        (ESCALATION_RE, "privilege escalation"),
        (RECURSIVE_DELETE_OF_ROOT_RE, "recursive delete of a root directory"),
        (SYSTEM_ALTERING_RE, "system-altering command"),
        (FORCE_PUSH_RE, "force push"),
        (PIPE_TO_SHELL_RE, "download piped to a shell"),
    ):
        if pattern.search(command):
            return False, why
    home = os.path.expanduser("~")
    for m in ABS_PATH_TOKEN_RE.finditer(command):
        token = m.group(1)
        if token in ("/", "~", "~/", "$HOME", "$HOME/"):
            return False, f"path token {token!r} is outside the workspace"
        expanded = os.path.expanduser(token.replace("$HOME", home))
        if expanded.startswith(SYSTEM_PATH_PREFIXES):
            continue
        if not _within_roots(expanded, roots):
            return False, f"path {token!r} is outside the workspace"
    base = Path(os.path.expanduser(cwd)).resolve() if cwd else (roots[0] if roots else Path.cwd())
    for m in DOTDOT_TOKEN_RE.finditer(command):
        candidate = (base / m.group(1)).resolve()
        if not _within_roots(str(candidate), roots):
            return False, f"relative path {m.group(1)!r} escapes the workspace"
    return True, ""


def _handle_codex_exec_command(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any], cmd: str) -> Dict[str, Any]:
    matches = re.findall(r"""(?:"([^"]*)"|'([^']*)')""", cmd)
    prompt = ""
    for m in reversed(matches):
        candidate = (m[0] or m[1]).strip()
        if candidate and len(candidate) > 2 and not candidate.startswith("-"):
            prompt = candidate
            break
    if not prompt:
        cleaned = re.sub(r"\bcodex\s+exec\b", "", cmd)
        cleaned = re.sub(r"-(?:m|c|i|s|p|C|-model|-config|-sandbox|-cd)\s+\S+", "", cleaned)
        cleaned = re.sub(r"--\S+", "", cleaned)
        prompt = cleaned.strip() or cmd

    item = {"Prompt": prompt, "Role": "codex-subagent"}
    decision = decide_subagent(item)
    tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    target_model = codex_tier_model(tier)
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
    _record_routing(payload, decision, target_model, codex_tier_effort(tier), "codex", target_type)

    if re.search(r"(?:-m|--model)\s+(\S+)", cmd):
        new_cmd = re.sub(r"(-m|--model)\s+\S+", f"-m {target_model}", cmd, count=1)
    else:
        new_cmd = re.sub(r"\bcodex\s+exec\b", f"codex exec -m {target_model}", cmd, count=1)

    active_branch = "--worktree" in cmd
    spawns = [{
        "role": target_type,
        "type": target_type,
        "model": target_model,
        "tier": tier,
        "workspace": "branch" if active_branch else "inherit",
        "active_branch": active_branch,
    }]
    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    updated_args = copy.deepcopy(args)
    for key in ("command", "CommandLine", "commandLine", "cmd"):
        if key in updated_args:
            updated_args[key] = new_cmd

    return _allow(
        f"Mitosis Orchestrator pinned codex exec model to '{target_model}' ({reason})",
        updated_input=updated_args,
        payload=payload,
    )


def _handle_spawn_agent(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or args.get("instructions") or args.get("message") or "").strip()
    if not prompt:
        return _deny(f"{tool_name} prompt cannot be empty.", payload)
    role = str(args.get("role") or args.get("agent_type") or args.get("type") or args.get("name") or "").strip()
    workspace = str(args.get("workspace") or args.get("isolation") or "inherit").strip().lower()
    if workspace == "worktree":
        workspace = "branch"
    item = {
        "Prompt": prompt,
        "Role": role,
        "Workspace": workspace,
        "TypeName": role,
    }
    decision = decide_subagent(item)
    tier, type_override, reason = decision.tier, decision.type_override, decision.reason
    target_model = codex_tier_model(tier)
    target_type = type_override or (SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)

    updated_args = copy.deepcopy(args)
    updated_args["model"] = target_model
    # Built-in Codex roles always exist; flash-lite-scout / flash-worker only exist after
    # scripts/setup-codex.py, and an unknown role makes Codex fail with "unknown agent_type".
    # Incoming flash-* names are accepted as aliases and rewritten to the built-in role.
    codex_role = CODEX_SCOUT_ROLE if tier == "flash_lite" else CODEX_WORKER_ROLE
    _record_routing(payload, decision, target_model, codex_tier_effort(tier), "codex", codex_role)
    for key in ("type", "agent_type"):
        if key in updated_args:
            updated_args[key] = codex_role
    if _append_report_format_enabled() and not prompt_has_report_format(prompt):
        prompt_key = next(k for k in ("prompt", "instructions", "message") if str(args.get(k) or "").strip())
        updated_args[prompt_key] = append_report_format(prompt, tier)

    spawns = [{
        "role": role or target_type,
        "type": target_type,
        "model": target_model,
        "tier": tier,
        "workspace": workspace,
        "active_branch": workspace == "branch",
    }]
    state = load_state(payload)
    state.setdefault("pending_spawns", []).extend(spawns)
    save_state(payload, state)

    return _allow(
        f"Mitosis Orchestrator pinned subagent model: {target_model} ({reason})",
        updated_input=updated_args,
        payload=payload,
    )


def _handle_mitosis_tools(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Classification (which may call the judge over HTTP) always runs before load_state
    # takes the lock, so the lock is never held across network I/O.
    def _record(state_spawns: List[Dict[str, Any]]) -> None:
        state = load_state(payload)
        state.setdefault("pending_spawns", []).extend(state_spawns)
        save_state(payload, state)

    if tool_name == "mitosis_scout":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_scout prompt cannot be empty.", payload)
        mutation = find_mutation(prompt)
        if mutation:
            tier = "flash"
            target_model = codex_tier_model("flash")
            target_type = WORKER_TYPE
            reason = f"mutation verb '{mutation}' upgraded scout to worker"
        else:
            tier = "flash_lite"
            target_model = codex_tier_model("flash_lite")
            target_type = SCOUT_TYPE
            reason = "read-only scout"
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        _record_routing(payload, Decision(tier, None, _tier_effort(tier), 1.0, "rule", "scout-tool", reason),
                        target_model, codex_tier_effort(tier), "codex", target_type)
        _record([{
            "role": str(args.get("role") or target_type),
            "type": target_type,
            "model": target_model,
            "tier": tier,
            "workspace": "inherit",
            "active_branch": False,
        }])
        return _allow(f"Mitosis Orchestrator pinned scout: {target_model} ({reason})", updated_input=updated_args, payload=payload)

    if tool_name == "mitosis_worker":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _deny("mitosis_worker prompt cannot be empty.", payload)
        target_model = codex_tier_model("flash")
        workspace = str(args.get("workspace") or "inherit").strip().lower()
        active_branch = workspace in ("branch", "worktree")
        updated_args = copy.deepcopy(args)
        updated_args["model"] = target_model
        _record_routing(payload, Decision("flash", None, _tier_effort("flash"), 1.0, "rule", "worker-tool",
                                          "mitosis_worker tool"),
                        target_model, codex_tier_effort("flash"), "codex", WORKER_TYPE)
        _record([{
            "role": str(args.get("role") or WORKER_TYPE),
            "type": WORKER_TYPE,
            "model": target_model,
            "tier": "flash",
            "workspace": "branch" if active_branch else workspace,
            "active_branch": active_branch,
        }])
        return _allow(f"Mitosis Orchestrator pinned worker: {target_model}", updated_input=updated_args, payload=payload)

    if tool_name == "mitosis_invoke_subagent":
        if "Subagents" in args or "subagents" in args:
            return _handle_invoke_subagent(payload, args)
        tasks = args.get("tasks")
        if isinstance(tasks, list):
            updated_tasks = copy.deepcopy(tasks)
            spawns = []
            for t in updated_tasks:
                if not isinstance(t, dict):
                    continue
                decision = decide_subagent(t)
                tier, reason = decision.tier, decision.reason
                model = codex_tier_model(tier)
                _record_routing(payload, decision, model, codex_tier_effort(tier), "codex",
                                SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
                t["model"] = model
                ws = str(t.get("workspace") or "inherit").strip().lower()
                spawns.append({
                    "role": str(t.get("role") or tier),
                    "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                    "model": model,
                    "tier": tier,
                    "workspace": ws,
                    "active_branch": ws in ("branch", "worktree"),
                })
            _record(spawns)
            updated_args = copy.deepcopy(args)
            updated_args["tasks"] = updated_tasks
            return _allow("Mitosis Orchestrator pinned tasks", updated_input=updated_args, payload=payload)
        prompt = str(args.get("prompt") or "").strip()
        if prompt:
            decision = decide_subagent(args)
            tier, reason = decision.tier, decision.reason
            model = codex_tier_model(tier)
            _record_routing(payload, decision, model, codex_tier_effort(tier), "codex",
                            SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE)
            updated_args = copy.deepcopy(args)
            updated_args["model"] = model
            _record([{
                "role": str(args.get("role") or tier),
                "type": SCOUT_TYPE if tier == "flash_lite" else WORKER_TYPE,
                "model": model,
                "tier": tier,
                "workspace": "inherit",
                "active_branch": False,
            }])
            return _allow(f"Mitosis Orchestrator pinned subagent: {model} ({reason})", updated_input=updated_args, payload=payload)
        return _deny("mitosis_invoke_subagent requires tasks or prompt.", payload)

    return _no_opinion(payload)


@_releases_state_locks
def handle_codex_notify(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state(payload)
    sync_subagents(payload, state)

    last_msg = str(payload.get("last-assistant-message") or payload.get("last_assistant_message") or "").strip()
    report_error = None
    if last_msg and ("STATUS:" in last_msg or "VERIFICATION:" in last_msg):
        report_error = validate_subagent_report(last_msg)

    active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
    active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]

    result: Dict[str, Any] = {"status": "ok", "decision": "allow"}
    if report_error:
        result["report_validation"] = "failed"
        result["report_error"] = report_error
    elif last_msg and ("STATUS:" in last_msg):
        result["report_validation"] = "passed"

    if active:
        key = ",".join(sorted(active))
        reminded: List[str] = state.setdefault("stop_reminded_sets", [])
        if key not in reminded:
            reminded.append(key)
            save_state(payload, state)
            result["decision"] = "continue"
            result["reason"] = (
                f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
                "Merge or discard their changes, then clean them up before finishing."
            )
            return result

    save_state(payload, state)
    return result


def _handle_workspace_guard(payload: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Opt-in: the plugin ships with the scope guard off. Set AGY_MITOSIS_AUTO_ALLOW=1 (or CLAUDE_MITOSIS_AUTO_ALLOW=1 / CODEX_MITOSIS_AUTO_ALLOW=1)
    # to let project-scoped file and command tools skip the permission prompt.
    # When unset or for tools without path arguments, pass through without a verdict.
    if not _env_flag("CODEX_MITOSIS_AUTO_ALLOW", "CLAUDE_MITOSIS_AUTO_ALLOW", "AGY_MITOSIS_AUTO_ALLOW", "AGY_ARGON_AUTO_ALLOW"):
        return _no_opinion(payload)
    roots = allowed_roots(payload)
    if not roots:
        return _no_opinion(payload)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cwd = str(_get(args, "cwd", "Cwd", default="") or payload.get("cwd") or "")
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "")
        if not cmd:
            return _no_opinion(payload)
        if cwd and not _within_roots(cwd, roots):
            return _ask("Mitosis Orchestrator: Cwd is outside the workspace.", payload=payload)
        ok, why = command_stays_in_workspace(cmd, cwd, roots)
        if ok:
            return _allow("Mitosis Orchestrator: command stays inside the workspace.", overrides=[f"command({cmd})"], payload=payload)
        return _ask(f"Mitosis Orchestrator: {why}.", payload=payload)

    if tool_name == "apply_patch":
        targets = _extract_apply_patch_files(args)
        if not targets:
            return _no_opinion(payload)
        outside = [t for t in targets if not _within_roots(t, roots)]
        if outside:
            return _ask(f"Mitosis Orchestrator: {outside[0]!r} is outside the workspace.", payload=payload)
        return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"write_file({t})" for t in targets], payload=payload)

    targets: List[Any] = []
    for key in ("file_path", "notebook_path") + PATH_ARG_KEYS:
        value = _get(args, key, key[0].lower() + key[1:], default=None)
        if isinstance(value, str) and value:
            targets.append(value)
    for key in PATH_LIST_ARG_KEYS:
        value = _get(args, key, key[0].lower() + key[1:], default=None)
        if isinstance(value, list):
            targets.extend(v for v in value if isinstance(v, str) and v)
    if not targets:
        return _no_opinion(payload)
    outside = [t for t in targets if not _within_roots(t, roots)]
    if outside:
        return _ask(f"Mitosis Orchestrator: {outside[0]!r} is outside the workspace.", payload=payload)
    # "allow" alone does not satisfy Antigravity's permission system (headless
    # runs still auto-deny), so grant the matching read_file/write_file rule
    # for exactly these in-workspace paths.
    action = "write_file" if (WRITE_TOOL_RE.search(tool_name) or tool_name in MUTATION_TOOLS) else "read_file"
    return _allow("Mitosis Orchestrator: path is inside the workspace.", overrides=[f"{action}({t})" for t in targets], payload=payload)


@_releases_state_locks
def handle_pre_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, args = _extract_tool_call(payload)
    if not tool_name:
        return _no_opinion(payload)
    # Codex plugin hooks name MCP tools mcp__<server>__<tool>.
    if tool_name.startswith("mcp__mitosis__"):
        tool_name = tool_name[len("mcp__mitosis__"):]

    # Lock-free read: the dispatch below may call the judge over HTTP (classify_subagent),
    # which must never happen while the state lock is held. Handlers that mutate state call
    # load_state (locking) themselves after classification.
    state = _peek_state(payload)

    # Circuit breaker: if consecutive failures >= 2 and breaker is not disabled ("0"),
    # block retry loops on commands and subagents.
    if _get_env("CODEX_MITOSIS_CIRCUIT_BREAKER", "CLAUDE_MITOSIS_CIRCUIT_BREAKER", "AGY_MITOSIS_CIRCUIT_BREAKER", "AGY_ARGON_CIRCUIT_BREAKER", default="1") != "0":
        fails = int(state.get("consecutive_failures") or 0)
        if fails >= 2 and tool_name in (
            "run_command", "invoke_subagent", "Bash", "Task", "Agent",
            "shell", "exec_command", "local_shell", "spawn_agent",
            "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
        ):
            return _deny(
                f"[Mitosis Orchestrator] Circuit breaker tripped after {fails} consecutive failure(s). "
                "Stop retrying and report the blocker to the user.",
                payload,
            )

    if tool_name == "invoke_subagent":
        return _handle_invoke_subagent(payload, args)
    if tool_name in ("Task", "Agent"):
        return _handle_claude_task_or_agent(payload, tool_name, args)
    if tool_name == "spawn_agent":
        return _handle_spawn_agent(payload, tool_name, args)
    if tool_name in ("mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
        return _handle_mitosis_tools(payload, tool_name, args)
    if tool_name in ("send_message", "SendMessage"):
        return _handle_send_message(payload, args)
    if tool_name == "run_workflow":
        return _handle_run_workflow(args)
    if tool_name == "define_subagent":
        return _handle_define_subagent(args)
    if tool_name == "manage_subagents":
        return _handle_manage_subagents(payload, args)

    if tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
        cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
        if re.search(r"\bcodex\s+exec\b", cmd):
            return _handle_codex_exec_command(payload, tool_name, args, cmd)

    # A Claude Code scout has Bash for git/log checks only.
    if tool_name == "Bash" and is_claude_code_payload(payload):
        agent_type = str(payload.get("agent_type") or "").split(":")[-1].strip().lower()
        if agent_type == SCOUT_TYPE:
            cmd = str(args.get("command") or "")
            ok, why = scout_command_is_read_only(cmd)
            if not ok:
                return _deny(
                    f"[Mitosis Orchestrator] {SCOUT_TYPE} runs read-only commands only ({why}). "
                    "Report the finding and let the orchestrator hand changes to flash-worker.",
                    payload,
                )

    # Primary agent guards (context hygiene and fast-path budget)
    if is_primary_agent(payload, state):
        # Context hygiene: block direct reads of raw logs or transcripts by primary agent
        if not _env_flag("CODEX_MITOSIS_ALLOW_DIRECT_LOGS", "CLAUDE_MITOSIS_ALLOW_DIRECT_LOGS", "AGY_MITOSIS_ALLOW_DIRECT_LOGS", "AGY_ARGON_ALLOW_DIRECT_LOGS") and tool_name in READ_TOOLS:
            target = str(
                args.get("file_path")
                or args.get("notebook_path")
                or args.get("AbsolutePath")
                or args.get("TargetFile")
                or args.get("FilePath")
                or args.get("Path")
                or args.get("File")
                or ""
            )
            if target and LOG_OR_TRANSCRIPT_RE.search(target.replace("\\", "/")):
                return _deny(
                    f"[Mitosis Orchestrator] Direct inspection of log/transcript file '{Path(target).name}' "
                    "by the primary agent is blocked for context hygiene. "
                    f"Delegate log slicing to {_delegation_hint(payload, 'flash_lite')}.",
                    payload,
                )

        # Fast-path budget: direct workspace file mutations and test-runner loops
        if not _env_flag("CODEX_MITOSIS_ALLOW_DIRECT_FASTPATH", "CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_MITOSIS_ALLOW_DIRECT_FASTPATH", "AGY_ARGON_ALLOW_DIRECT_FASTPATH"):
            if tool_name in MUTATION_TOOLS or tool_name == "apply_patch":
                if tool_name == "apply_patch":
                    patch_targets = _extract_apply_patch_files(args)
                    norm_targets = []
                    for t in patch_targets:
                        try:
                            norm_targets.append(str(Path(os.path.expanduser(t)).resolve()))
                        except Exception:
                            norm_targets.append(t)
                    state = load_state(payload)  # locked read-modify-write of the edit budget
                    edited: List[str] = state.setdefault("turn_edited_files", [])
                    max_edits_str = _get_env("CODEX_MITOSIS_MAX_DIRECT_EDITS", "CLAUDE_MITOSIS_MAX_DIRECT_EDITS", "AGY_MITOSIS_MAX_DIRECT_EDITS", "AGY_ARGON_MAX_DIRECT_EDITS", default="3")
                    try:
                        max_edits = int(max_edits_str)
                    except ValueError:
                        max_edits = 3
                    new_targets = [nt for nt in norm_targets if nt not in edited]
                    if len(edited) + len(new_targets) > max_edits:
                        return _deny(
                            f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                            f"when editing via apply_patch. Delegate multi-file modifications to "
                            f"{_delegation_hint(payload, 'flash')}.",
                            payload,
                        )
                    for nt in new_targets:
                        edited.append(nt)
                    if new_targets:
                        save_state(payload, state)
                else:
                    target = str(
                        args.get("file_path")
                        or args.get("notebook_path")
                        or args.get("TargetFile")
                        or args.get("AbsolutePath")
                        or args.get("FilePath")
                        or ""
                    )
                    artifact_dir = payload.get("artifactDirectoryPath")
                    is_artifact = False
                    if artifact_dir and target:
                        try:
                            is_artifact = _within_roots(target, [Path(artifact_dir)])
                        except Exception:
                            is_artifact = False
                    if target and not is_artifact:
                        try:
                            norm_target = str(Path(os.path.expanduser(target)).resolve())
                        except Exception:
                            norm_target = target
                        state = load_state(payload)  # locked read-modify-write of the edit budget
                        edited: List[str] = state.setdefault("turn_edited_files", [])
                        max_edits_str = _get_env("CODEX_MITOSIS_MAX_DIRECT_EDITS", "CLAUDE_MITOSIS_MAX_DIRECT_EDITS", "AGY_MITOSIS_MAX_DIRECT_EDITS", "AGY_ARGON_MAX_DIRECT_EDITS", default="3")
                        try:
                            max_edits = int(max_edits_str)
                        except ValueError:
                            max_edits = 3
                        if norm_target not in edited and len(edited) >= max_edits:
                            return _deny(
                                f"[Mitosis Orchestrator] Direct workspace edit limit ({max_edits} files per turn) exceeded "
                                f"when editing '{Path(target).name}'. Delegate multi-file modifications to "
                                f"{_delegation_hint(payload, 'flash')}.",
                                payload,
                            )
                        if norm_target not in edited:
                            edited.append(norm_target)
                            save_state(payload, state)

            elif tool_name in ("run_command", "Bash", "shell", "exec_command", "local_shell"):
                cmd = str(_get(args, "command", "CommandLine", "commandLine", "cmd", default="") or "").strip()
                edited = state.get("turn_edited_files", [])
                if edited and TEST_RUNNER_RE.search(cmd):
                    return _deny(
                        "[Mitosis Orchestrator] Direct workspace edit followed by test runner execution detected "
                        f"in the primary agent ('{cmd}'). Delegate code editing and test execution loops "
                        f"to {_delegation_hint(payload, 'flash')}.",
                        payload,
                    )

    return _handle_workspace_guard(payload, tool_name, args)


# ---------------------------------------------------------------------------
# post-tool-use / stop
# ---------------------------------------------------------------------------

def _tool_failed(tool_name: str, payload: Dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    tool_result = payload.get("toolResult") or payload.get("tool_result") or payload.get("result")
    if isinstance(tool_result, dict):
        exit_code = tool_result.get("exitCode")
        if exit_code is None:
            exit_code = tool_result.get("exit_code")
        if exit_code is None:
            exit_code = tool_result.get("returncode")
        if exit_code is not None and exit_code != 0:
            return True
        if tool_result.get("error") or tool_result.get("status") == "error":
            return True
        output = str(tool_result.get("output") or "")
        if re.search(r"\bexited with code [1-9]", output):
            return True
        if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
            res_str = json.dumps(tool_result)
            if re.search(r"STATUS:\s*(?:FAILED|BLOCKED)\b", res_str):
                return True
    elif isinstance(tool_result, str):
        if re.search(r"\bexited with code [1-9]", tool_result):
            return True
        if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"):
            if re.search(r"STATUS:\s*(?:FAILED|BLOCKED)\b", tool_result):
                return True
    return False


@_releases_state_locks
def handle_post_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_name, _ = _extract_tool_call(payload)
    # A subagent's own tool call (Claude and Codex both send agent_id) must not touch the
    # primary's state: its failures are not the primary's failures, and skipping the
    # load/save avoids needless lock contention.
    if payload.get("agent_id") and tool_name not in ("Agent", "Task", "spawn_agent"):
        return {}
    state = load_state(payload)
    failed = _tool_failed(tool_name, payload)
    if tool_name in ("invoke_subagent", "Task", "Agent", "spawn_agent", "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker") and (payload.get("error") or failed):
        pending = state.get("pending_spawns", [])
        if tool_name in ("invoke_subagent", "mitosis_invoke_subagent"):
            tool_call = payload.get("toolCall") or payload.get("tool_call") or {}
            args = tool_call.get("args") or tool_call.get("input") or {}
            tasks = _get(args, "Subagents", "subagents", "tasks", default=[]) or []
            n = len(tasks) if isinstance(tasks, list) else 1
            if n and len(pending) >= n:
                del pending[-n:]
            else:
                state["pending_spawns"] = []
        else:
            if pending:
                pending.pop()
    else:
        if is_claude_code_payload(payload) and tool_name in ("Task", "Agent"):
            hint = str((payload.get("tool_input") or {}).get("subagent_type") or "")
            for aid in _claude_agent_ids_from_response(payload):
                _claude_register_agent(state, aid, hint)
        sync_subagents(payload, state)

    # Track consecutive failures for the circuit breaker
    if tool_name in (
        "run_command", "invoke_subagent", "Bash", "Task", "Agent",
        "shell", "exec_command", "local_shell", "spawn_agent",
        "mitosis_invoke_subagent", "mitosis_scout", "mitosis_worker"
    ):
        if failed:
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        else:
            state["consecutive_failures"] = 0

    save_state(payload, state)
    return {}


@_releases_state_locks
def handle_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload) or is_codex_payload(payload):
        if payload.get("stop_hook_active") is True:
            return {}
        state = load_state(payload)
        sync_subagents(payload, state)
        active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
        active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]
        if not active:
            save_state(payload, state)
            return {}

        key = ",".join(sorted(active))
        reminded: List[str] = state.setdefault("stop_reminded_sets", [])
        if key in reminded:
            save_state(payload, state)
            return {}
        reminded.append(key)
        save_state(payload, state)
        return {
            "decision": "block",
            "reason": (
                f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
                "Merge or discard their changes, then clean them up before finishing."
            ),
        }

    # Antigravity handling
    if str(payload.get("terminationReason") or "") in SAFE_TERMINATION_REASONS:
        return {"decision": "allow"}
    if not payload.get("fullyIdle", True):
        return {"decision": "allow"}

    state = load_state(payload)
    sync_subagents(payload, state)
    active = [cid for cid, m in state.get("subagents", {}).items() if m.get("active_branch")]
    active += [m.get("role", "worker") for m in state.get("pending_spawns", []) if m.get("active_branch")]
    if not active:
        save_state(payload, state)
        return {"decision": "allow"}

    # agy increments executionNum after every "continue", so keying on it would
    # nag on every attempt. Remind once per distinct set of live branch workers:
    # a new branch worker earns a new reminder, ignoring the same one does not.
    key = ",".join(sorted(active))
    reminded: List[str] = state.setdefault("stop_reminded_sets", [])
    if key in reminded:
        save_state(payload, state)
        return {"decision": "allow"}
    reminded.append(key)
    save_state(payload, state)
    return {
        "decision": "continue",
        "reason": (
            f"[Mitosis Orchestrator] Branch worktrees are still active ({', '.join(active)}). "
            "Merge or discard their changes, then call manage_subagents(Action='kill') before finishing."
        ),
    }


REPORT_BLOCK_REASON = _GEN.REPORT_BLOCK_REASON


def _is_plugin_agent_type(type_name: str) -> bool:
    bare = (type_name or "").strip().lower()
    if ":" in bare:
        prefix, bare = bare.split(":", 1)
        if prefix.strip() not in ("", CLAUDE_PLUGIN_PREFIX):
            return False
    return bare.strip() in (SCOUT_TYPE, WORKER_TYPE)


def _handle_claude_subagent_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state(payload)
    agent_id = str(payload.get("agent_id") or "").strip().lower()
    agent_type = str(payload.get("agent_type") or "").strip()
    record = _claude_register_agent(state, agent_id, agent_type)
    cleared = False
    if record is not None:
        record["alive"] = False
        record["active_branch"] = False
        cleared = True
    if not cleared:
        for meta in state.setdefault("pending_spawns", []):
            meta["active_branch"] = False
        for meta in state.setdefault("subagents", {}).values():
            meta["active_branch"] = False

    # One save for every outcome (the report_nagged flag is part of the same write).
    result: Dict[str, Any] = {}
    if payload.get("stop_hook_active") is not True and _get_env("CLAUDE_MITOSIS_ENFORCE_REPORT", default="1") != "0":
        type_name = agent_type or (str(record.get("type") or "") if record else "")
        if _is_plugin_agent_type(type_name):
            error = validate_subagent_report(str(payload.get("last_assistant_message") or ""))
            if error is not None:
                nagged: List[str] = state.setdefault("report_nagged", [])
                key = agent_id or type_name
                if key not in nagged:
                    nagged.append(key)
                    result = {"decision": "block", "reason": REPORT_BLOCK_REASON.format(error=error)}
    save_state(payload, state)
    return result


@_releases_state_locks
def handle_subagent_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    if is_claude_code_payload(payload):
        return _handle_claude_subagent_stop(payload)
    state = load_state(payload)
    sync_subagents(payload, state)
    subagents = state.setdefault("subagents", {})
    pending = state.setdefault("pending_spawns", [])

    subagent_id = str(payload.get("subagent_id") or "").strip().lower()
    cleared = False
    if subagent_id and subagent_id in subagents:
        subagents[subagent_id]["active_branch"] = False
        cleared = True
    for meta in pending:
        if subagent_id and (meta.get("role", "").lower() == subagent_id or meta.get("type", "").lower() == subagent_id):
            meta["active_branch"] = False
            cleared = True
    if not cleared:
        for meta in pending:
            if meta.get("active_branch"):
                meta["active_branch"] = False
        for meta in subagents.values():
            if meta.get("active_branch"):
                meta["active_branch"] = False
    save_state(payload, state)
    return {}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _debug_log(payload: Dict[str, Any], mode: str, result: Dict[str, Any]) -> None:
    """With AGY_MITOSIS_DEBUG=1 / CLAUDE_MITOSIS_DEBUG=1 (or legacy AGY_ARGON_DEBUG=1), append one JSON line per hook call to the artifact directory."""
    if not _env_flag("CLAUDE_MITOSIS_DEBUG", "AGY_MITOSIS_DEBUG", "AGY_ARGON_DEBUG"):
        return
    path = _state_file(payload)
    if not path:
        return
    tool_name, _ = _extract_tool_call(payload)
    entry = {
        "mode": mode,
        "tool": tool_name,
        "modelName": payload.get("modelName") or payload.get("model_name") or payload.get("model"),
        "invocationNum": payload.get("invocationNum"),
        "executionNum": payload.get("executionNum"),
        "fullyIdle": payload.get("fullyIdle"),
        "terminationReason": payload.get("terminationReason"),
        "payloadKeys": sorted(payload.keys()),
        "result": result,
    }
    if _RECENT_DECISIONS:
        entry["decisions"] = [d.as_log() for d in _RECENT_DECISIONS]
        _RECENT_DECISIONS.clear()
    try:
        # One O_APPEND write per entry so concurrent hooks never interleave lines.
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(
            str(path.with_name("mitosis_orchestrator_debug.log")),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


HANDLERS = {
    "session-start": handle_session_start,
    "pre-invocation": handle_pre_invocation,
    "user-prompt-submit": handle_pre_invocation,
    "pre-tool-use": handle_pre_tool_use,
    "post-tool-use": handle_post_tool_use,
    "stop": handle_stop,
    "subagent-stop": handle_subagent_stop,
    "codex-notify": handle_codex_notify,
}

EVENT_NAME_TO_MODE = {
    "SessionStart": "session-start",
    "PreInvocation": "pre-invocation",
    "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use",
    "PostToolUseFailure": "post-tool-use",
    "Stop": "stop",
    "SubagentStop": "subagent-stop",
    "CodexNotify": "codex-notify",
    "codex-notify": "codex-notify",
    "agent-turn-complete": "codex-notify",
    "turn-ended": "codex-notify",
}


def main() -> None:
    # hooks/codex-hooks.json passes --platform=codex: Codex SessionStart carries no turn_id.
    platform = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--platform=")), "")
    argv = [a for a in sys.argv if not a.startswith("--platform=")]
    mode = argv[1] if len(argv) > 1 and not argv[1].startswith("{") else None
    try:
        raw = sys.stdin.read()
        if not raw.strip() and len(sys.argv) > 1 and sys.argv[-1].startswith("{"):
            raw = sys.argv[-1]
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    if platform:
        payload["platform"] = platform

    if not mode:
        ev = payload.get("hook_event_name") or payload.get("event") or payload.get("type")
        if ev:
            mode = EVENT_NAME_TO_MODE.get(ev)
    if not mode:
        mode = "pre-tool-use"

    handler = HANDLERS.get(mode)
    try:
        result = handler(payload) if handler else {}
    except Exception as exc:  # never break the agent loop because of the guard
        result = _no_opinion(payload) if mode in ("stop", "pre-tool-use") else {}
        sys.stderr.write(f"[mitosis-orchestrator] {mode} hook error: {exc!r}\n")
    finally:
        _release_state_locks()
    _debug_log(payload, mode, result)

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    _flush_otel()  # after the response and after the locks are released


if __name__ == "__main__":
    main()
