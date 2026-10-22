"""Routing rules, judge hook and subagent classification."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import ALLOWED_SUBAGENT_MODELS, Decision, SCOUT_TYPE, WORKER_TYPE, _get, _get_env
from .report import strip_report_format
from .state import _RECENT_DECISIONS


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
    r"(?<![會会能可要用])(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?(?:測試|测试|建置|編譯|编译|基準|基准)|"
    r"\b(?:run|rerun|re-run|execute)\s+(?:[\w./:-]+\s+){0,3}?" + _CHECKER_ALT + r"\b|"
    r"(?<![會会能可要用])(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?" + _CHECKER_ALT + r"\b|"
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
    r"(?<![會会能可要用])(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,]{0,8}?(?:測試|测试)|調查|调查|上網查|上网查|綜合|综合|記憶體洩漏|内存泄漏|效能分析|性能分析|剖析|"
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


# The caller also declares that nothing is to be executed or built.
READ_ONLY_RE = re.compile(
    READ_ONLY_RE.pattern + r"|"
    r"不用(?:執行|执行|跑|build)|不要(?:執行|执行|跑|build|編譯|编译)|看檔案就好|看文件就好|用 ?git (?:show|log|blame) 看就好|"
    r"\bdig through the git history\b|\b(?:do not|don't|no need to) (?:run|execute|build)\b",
    re.IGNORECASE,
)


# Positive lookup signal: the short-prompt fallback sends a prompt to the
# scout only when it asks to find or list something. Unknown goes to the worker.
LOOKUP_SIGNAL_RE = re.compile(
    r"[?？]|"
    r"\b(?:where|which|what|find|locate|list|show me|look up|grep|search for|how many)\b|"
    r"(?:^|[.;!\n])\s*(?:does|do|did|is|are|was|were|can)\b|"
    r"(?:^|[.;!\n])\s*(?:show|view|print|open|看|查看)\b|(?:^|[.;!\n])\s*(?:看|查看)|"
    r"\b(?:tail|read|summari[sz]e|explain|walk me through|tell me|display|cat)\b|"
    r"哪裡|哪里|哪個|哪个|哪些|哪幾|哪几|在哪|列出|找出|查找|有沒有|有没有|是不是|什麼|什么|多少|幾個|几个|有哪些|"
    r"看一下|讀|读|摘要|總結|总结|解釋|解释|顯示|显示|告訴我|告诉我|說明|说明|了解|怎麼|怎么|如何",
    re.IGNORECASE,
)


# Execution signal: an imperative that starts a clause, "run the <noun>", a
# Chinese verb with an object, or a leading CLI command.
RUN_VERBS = r"run|rerun|re-run|execute|profile|benchmark|measure|build|deploy|apply"


RUN_TASK_RE = re.compile(
    r"(?:(?:^|[.;:!?\n]\s*|\b(?:then|and|please|also)\s+)(?:" + RUN_VERBS + r")\s+(?!(?:git|ls|cat|grep|rg|head|tail|wc|find)\b)\S)|"
    r"\brun the\s+\w+|\btop to bottom\b|"
    r"(?<![會会能可要用別别])(?:重跑|跑一次|跑一下|跑|執行|执行|量一下)(?:\s*[A-Za-z0-9_./`'\"-]|一次|一下|\s|$)",
    re.IGNORECASE,
)


CLI_START_RE = re.compile(
    r"^\s*`?(?:ruff|mypy|pytest|npm|pnpm|yarn|cargo|go|make|terraform|kubectl|docker|gradle|\./gradlew|tsc|eslint|"
    r"python3?|node|bash)\s+[^\s\u4e00-\u9fff]",
)


# Docs or web research: the scout has no web access.
DOCS_LOOKUP_RE = re.compile(
    r"(?:官方文件|文件|文檔|文档|\bdocs\b|\bdocumentation\b|\bchangelog\b|\brelease notes\b)"
    r"[^。\n]{0,40}?(?:查一下|查|最新|\blook up\b|\bcheck the latest\b)|"
    r"(?:查一下|查|最新|\blook up\b|\bcheck the latest\b)[^。\n]{0,40}?"
    r"(?:官方文件|文件|文檔|文档|\bdocs\b|\bdocumentation\b|\bchangelog\b|\brelease notes\b)",
    re.IGNORECASE,
)


def find_run_task(text: str) -> Optional[str]:
    """First execution signal in text (imperative run, CLI command, Chinese run verb), or None."""
    m = CLI_START_RE.search(text) or RUN_TASK_RE.search(text)
    return m.group(0).strip() if m else None


NUMBERED_RULE_RE = re.compile(r"(?m)^\s*[0-9]+[.)]\s+")


TEST_RUNNER_RE = re.compile(
    r"\b(pytest|python(?:\d+(?:\.\d+)?)?\s+-m\s+unittest|npm\s+test|pnpm\s+test|yarn\s+test|vitest|jest|cargo\s+test|go\s+test)\b",
    re.IGNORECASE,
)


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
    "short-prompt-unknown": 0.5,
    "docs-lookup": 0.85,
    "default": 0.5,
}


# Rules a judge may overrule when their confidence is low. Type, workspace and
# explicit-model rules state the caller's intent and are never sent to a judge.
JUDGEABLE_RULES = {"mutation", "heavy", "heavy-noun", "lite", "long-prompt", "short-prompt",
                  "short-prompt-unknown", "docs-lookup", "default"}


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
        path = Path(__file__).resolve().parent.parent / "mitosis_judge.py"
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
     10. short prompt (<= 320 chars)            -> flash_lite only with a lookup signal and no run task, else flash
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
    if DOCS_LOOKUP_RE.search(text):
        return done(_rule_decision("flash", None, "docs-lookup", "docs or web lookup needs web access"))
    run_task = find_run_task(text)
    if lite and len(prompt) <= 600 and not run_task:
        return done(_rule_decision("flash_lite", None, "lite", f"lite signal '{lite}'"))
    if len(prompt) > 450:
        return done(_rule_decision("flash", None, "long-prompt", "long prompt"))
    if len(prompt) <= 320:
        if (LOOKUP_SIGNAL_RE.search(text) or READ_ONLY_RE.search(text)) and not run_task:
            return done(_rule_decision("flash_lite", None, "short-prompt", "short lookup prompt without heavy signals"))
        return done(_rule_decision("flash", None, "short-prompt-unknown",
                                   f"short prompt without a lookup signal{'; run task' if run_task else ''}"))
    return done(_rule_decision("flash", None, "default", "default"))


def classify_subagent(subagent: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
    """Return (model, type_override_or_None, reason); see decide_subagent."""
    decision = decide_subagent(subagent)
    return decision.tier, decision.type_override, decision.reason


def classify_subagent_tier(subagent: Dict[str, Any]) -> str:
    """Backwards-compatible wrapper returning only the model tier."""
    return classify_subagent(subagent)[0]
