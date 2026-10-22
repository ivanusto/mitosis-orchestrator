"""Routing rules, judge hook and subagent classification."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import ALLOWED_SUBAGENT_MODELS, Decision, SCOUT_TYPE, WORKER_TYPE, _get, _get_env
from .guards import SCOUT_GIT_SUBCOMMANDS, scout_command_is_read_only
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
    r"write|create|move|revert|roll(?: \w+){0,2} back|rollback|merge|rebase|format|deploy|publish|release|tag|adjust|tweak|convert|switch|port|lint|typecheck|type-check|reformat"
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
    r"(?:不要|不得|不可|不能|不准|不需要?|不用|不是要你|並非要你|并非要你|無須|无须|毋須|別|别|勿|請勿|请勿|禁止|切勿|避免)\s*"
    r"(?:任何|去|再|重新|重|來|来)?\s*"
    r"(?:" + CJK_MUTATION_RE.pattern + r"|[修改])"
)


# "Do not edit anything", "never modify", "without deleting" must not count as mutation requests.
# Up to four words (commas only in the first two) may sit between the negation and the verb, and "or"/"nor"
# chains it, so "Do NOT git fetch/pull or modify" and "never delete or rename"
# are prohibitions too. "and" is left out: "don't X and fix Y" asks for a fix.
_MUTATION_ALT = MUTATION_RE.pattern[3:-3] + "|" + IMPERATIVE_VERBS


NEGATED_MUTATION_RE = re.compile(
    r"\b(?:do not|don't|dont|never|must not|mustn't|should not|shouldn't|without|not|no)\s+"
    r"(?:(?!\b(?:" + _MUTATION_ALT + r"|and|but|then|so|instead|just|also|until|unless|when|if|because|since|while|before|after|once|stop)\b)[\w/.-]+,?\s+){0,2}"
    r"(?:(?!\b(?:" + _MUTATION_ALT + r"|and|but|then|so|instead|just|also|until|unless|when|if|because|since|while|before|after|once|stop)\b)[\w/.-]+\s+){0,2}"
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
    if before == "-" and (match.start() < 2 or text[match.start() - 2].isspace()):
        before = ""  # a command-line flag such as "-delete", not a hyphenated word
    return before in ("/", ".", "-", "_") or after.startswith("/") or bool(re.match(r"^\.\w", after))


# In a read-only question these words name a topic, not a job.
QUESTION_NOUNS = {"migration", "debug", "build", "test", "tests", "race condition"}
CJK_QUESTION_NOUN_RE = re.compile(r"除錯|偵錯|调试|建置|構建|构建|競態|竞态")


# Leading read-only verbs and question words, in either language.
_RO_LEAD_RE = re.compile(
    r"(?:^|[.;!\n])\s*(?:please\s+)?(?:show me|show|list|find|explain|describe|summari[sz]e|walk me through|"
    r"tell me|print|read|look at|check (?:whether|if)|which|what|where|why|how)\b|"
    r"解釋|解释|說明|说明|為什麼|为什么|哪裡|哪里|哪個|哪个|是什麼|是什么|怎麼運作|怎么运作|嗎|吗|有沒有|有没有|是否|"
    r"想了解|看 ?code 就好",
    re.IGNORECASE,
)


# Imperative action aimed at the thing: a question with one of these is a task.
_ACTION_EN_RE = re.compile(
    r"\b(?:run|rerun|re-run|execute|fix|create|write|add|apply|implement|rewrite|refactor|delete|remove|"
    r"rename|edit|modify|migrate|deploy|install|reproduce|repro|rebuild|recompile|make (?:it|them|that|this|the))\b",
    re.IGNORECASE,
)
# Also nouns ("the rollback", "a commit"): an action only when they open a clause or follow "you to", "and", "then".
_ACTION_AMBIGUOUS_EN_RE = re.compile(
    r"(?:(?:^|[.;:!?,\n])\s*|\b(?:please|then|and|also|to|you|just|can|could|would|will)\s+)"
    r"(?:roll ?back|patch|update|commit|push|change|trigger|benchmark|profile|measure|start|launch|revert|merge|tag)\b",
    re.IGNORECASE,
)
_ACTION_CJK_RE = re.compile(
    r"(?<![會会能可要用次前曾已被有])(?:跑|執行|执行|修|建立|新增|寫|写|加|刪|删|改|回滾|回滚|回退|套用|部署|重構|重构)(?![了過过的])"
)
_NEGATED_ACTION_CJK_RE = re.compile(
    r"(?:不要|不用|別|别|勿|請勿|请勿|不需要?)[^。，,！!？?]{0,6}?(?:跑|執行|执行|修|改|寫|写|加|刪|删|建立|新增)"
)


def _is_readonly_question(text: str) -> bool:
    """True for a question or lookup that asks for no action on the thing it names.

    "Can you run the migration on staging?" asks for a run and is not one.
    """
    stripped = _NEGATED_ACTION_CJK_RE.sub(_blank, _strip_negated_mutations(text))
    if not (QUESTION_RE.search(stripped) or _RO_LEAD_RE.search(stripped)):
        return False
    if EXECUTION_SIGNAL_RE.search(stripped) or TEST_RUNNER_RE.search(stripped) or _ACTION_CJK_RE.search(stripped):
        return False
    if _ACTION_AMBIGUOUS_EN_RE.search(stripped):
        return False
    for m in _ACTION_EN_RE.finditer(stripped):
        if not DETERMINER_BEFORE_RE.search(stripped[max(0, m.start() - 30):m.start()]):
            return False
    return True


def find_mutations(text: str, question_nouns: bool = True) -> List[Tuple[int, str]]:
    """Return (offset, phrase) for every mutation request in text, in order.

    Negated phrases ("do not edit", "不要修改") and noun uses ("the patch
    version") do not count. In a read-only question "migration" is a topic too
    (question_nouns=False turns that off).
    """
    stripped = _strip_negated_mutations(text)
    skip_migration = question_nouns and _is_readonly_question(text)
    found: List[Tuple[int, str]] = []
    for m in MUTATION_RE.finditer(stripped):
        if skip_migration and m.group(0).lower() == "migration":
            continue
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


def find_mutation(text: str, question_nouns: bool = True) -> Optional[str]:
    """First mutation phrase in text, or None."""
    found = find_mutations(text, question_nouns)
    return found[0][1] if found else None


# Shell mutations that hide inside a pipeline or a one-liner: "sed -i", "xargs rm", "perl -pi", "dd of=".
SHELL_MUTATION_RE = re.compile(
    r"\bsed\s+(?:-\S+\s+)*-(?:[A-Za-z]*i|-in-place)\S*|"
    r"\bperl\s+(?:-\S+\s+)*-[A-Za-z]*i\S*|"
    r"\bxargs\b[^|;&\n]*?\b(?:rm|mv|cp|chmod|chown|tee|truncate|git\s+rm|unlink|shred)\b|"
    r"\|\s*(?:sudo\s+)?tee\s+(?!/dev/null\b)[^\s|]|"
    r"-exec(?:dir)?\s+(?:sudo\s+)?(?:rm|mv|cp|sed|chmod|chown|truncate|unlink|shred)\b|"
    r"\bfind\b[^|;\n]*\s-delete\b|"
    r"\btruncate\s+-\w*s\b|"
    r"\bdd\b[^|;\n]*\bof=(?!/dev/null\b)\S|"
    r"(?:^|[|;&\n`]\s*)(?:sudo\s+)?(?:rm|mv|chmod|chown|unlink|shred)\s+\S",
    re.IGNORECASE,
)


def find_shell_mutation(text: str) -> Optional[str]:
    """First shell-level mutation (sed -i, xargs rm, perl -pi, dd of=...) in text, or None."""
    m = SHELL_MUTATION_RE.search(text)
    return m.group(0).strip(" |;&`\n") if m else None


# Trailing imperative: a question followed by a clause that asks for an action
# ("Why is the build broken? Please get it green."). Split at sentence ends and
# at ", then" style joins; the first question clause arms the check.
_SENTENCE_SPLIT_RE = re.compile(
    r"[?？！!。；;\n]+|\.(?=\s|$)|[,，]\s*(?=(?:and then|then|and|please|also|so)\b)|[,，]\s*(?=(?:請|请|然後|然后|再|並|并|順便|顺便))",
    re.IGNORECASE,
)
_ABBREV_RE = re.compile(r"\b(e)\.(g)\.|\b(i)\.(e)\.|\b(vs|etc|cf)\.", re.IGNORECASE)
_QUESTION_START_RE = re.compile(
    r"^\W*(?:(?:and|then|also|but|so|please)\s+)?(?:what|which|where|why|how|who|whom|whose|when)\b",
    re.IGNORECASE,
)
_QUESTION_CJK_RE = re.compile(r"嗎|吗|哪|什麼|什么|為什麼|为什么|為何|为何|怎麼|怎么|如何|是否|有沒有|有没有|多少|誰|谁|幾|几|是不是|有無|有无|呢")
_TRAILING_READONLY_RE = re.compile(
    r"^\W*(?:(?:just|only|simply|please|and|then|also|first|先|請|请|只要|只|麻煩|麻烦|順便|顺便)\s*)*"
    r"(?:tell me|show me|show|list|explain|describe|summari[sz]e|report(?: back)?|answer|point me|give me (?:the|a) (?:path|file|name|list)|"
    r"let me know|name|identify|locate|find (?:out|me)|"
    r"告訴我|告诉我|列出|說明|说明|解釋|解释|回報|回报|報告|报告|看看|看一下|看就好|看|查看|找出|指出|回答)|"
    r"\b(?:only|just) (?:report|tell|list|show|explain|look|read|check|answer)\b|"
    r"\b(?:do not|don't|dont|no need to|without) (?:change|modify|edit|touch|write|fix|run|alter)\b|"
    r"\bno (?:changes|edits|writes)\b|\bread[- ]?only\b|"
    r"只要看|只看|只要告訴|只要告诉|不用改|不用修|不要改|不要動|不要动|先不要動|先不要动|不需要改|不用動|不用动|不必改|無須修改|无须修改|唯讀|只讀|只读",
    re.IGNORECASE,
)
# Looser hints that a follow-up clause only says how to look ("Paths only.", "git show 看就好", "不要執行").
_READONLY_HINT_RE = re.compile(
    r"\bonly\b|\b(?:use|using|check|via|with|from)\s+(?:the\s+)?(?:`?(?:rg|grep|git|wc|cat|find|ls|head|tail|jq)\b|git log|blame)|"
    r"\b(?:is|are) fine\b|\bwill do\b|\bis enough\b|"
    r"就好|即可|就行|而已|不要\s*(?:build|執行|执行|跑|編譯|编译)|不用\s*(?:執行|执行|跑|build)|回報|回报|回答|說明|说明|摘要|簡單|简单|用\s*(?:rg|grep|git|wc|cat|find)\b",
    re.IGNORECASE,
)
_RO_COMPOUND_ACTION_RE = re.compile(
    r"\b(?:and|then)\s+(?:also\s+)?(?:fix|apply|make|change|update|edit|run|delete|remove|add|write|create|commit|push|"
    r"patch|rewrite|rename|get|set|turn|generate)\b|然後|然后",
    re.IGNORECASE,
)
_COURTESY_RE = re.compile(
    r"^\W*(?:thanks?(?: you| a lot| so much| in advance| again)*|thx|ty|cheers|no rush|no hurry|not urgent|take your time|"
    r"please|pls|appreciated|much appreciated|ok|okay|hi|hello|hey|sorry|any ideas|thoughts|asap|"
    r"謝謝|谢谢|感謝|感谢|多謝|多谢|麻煩了|麻烦了|辛苦了|不急|拜託|拜托|謝啦|謝囉|先謝謝|不好意思|請|请)"
    r"(?:\s*(?:you|你|您|了|啦|喔|哦|囉|唷|你們|你们|大家|!|~))*\W*$",
    re.IGNORECASE,
)
_PATHISH = r"[`'\"]?[\w@~-]*[/._#][\w./\\:#@~-]*[`'\"]?"
_REFERENCE_RE = re.compile(
    r"^\W*(?:see(?: also)?|refs?|cf|files?|paths?|repo|dir|located (?:at|in)|in|at|from|source|context|note|fyi|參考|参考|檔案|文件|路徑|路径|位置|見|见)"
    r"\s*[:：]?\s*" + _PATHISH + r"(?:[,\s]+" + _PATHISH + r"){0,3}\W*$|^\W*" + _PATHISH + r"\W*$",
    re.IGNORECASE,
)
# A statement that opens with a subject or a determiner is background, not an instruction.
_NON_IMPERATIVE_START_RE = re.compile(
    r"^\W*(?:(?:i|i'm|i've|i'd|we|we're|you|it|it's|its|the|this|that|these|those|my|our|your|there|here|he|she|they|a|an|"
    r"context|background|note|fyi|btw|for|in|on|at|with|by|because|since|currently|right now)\b|"
    r"這|这|那|我|我們|我们|它|他|她|該|该|背景|備註|备注|註|注|目前|現在|现在|專案|项目|因為|因为|由於|由于)",
    re.IGNORECASE,
)


def _is_question_clause(clause: str, terminator: str) -> bool:
    return (terminator.strip()[:1] in ("?", "？") or bool(_QUESTION_START_RE.search(clause))
            or bool(_QUESTION_CJK_RE.search(clause)))


def find_trailing_imperative(text: str) -> Optional[str]:
    """The first action clause that follows a question clause, or None.

    "Why is the build broken? Please get it green." asks for a fix after the
    question. Read-only follow-ups ("Just tell me which file."), courtesy
    ("Thanks.") and reference fragments ("See docs/x.md.") do not count.
    """
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", "․"), text)
    clauses: List[Tuple[str, str]] = []
    pos = 0
    for m in _SENTENCE_SPLIT_RE.finditer(protected):
        clauses.append((protected[pos:m.start()], m.group(0)))
        pos = m.end()
    clauses.append((protected[pos:], ""))
    seen_question = False
    for clause, terminator in clauses:
        clause = clause.strip()
        if not clause:
            continue
        if _is_question_clause(clause, terminator):
            seen_question = True
            continue
        if not seen_question:
            continue
        if _COURTESY_RE.match(clause) or _REFERENCE_RE.match(clause):
            continue
        if _TRAILING_READONLY_RE.search(clause) and not _RO_COMPOUND_ACTION_RE.search(clause):
            continue
        if (_READONLY_HINT_RE.search(clause) and not _RO_COMPOUND_ACTION_RE.search(clause)
                and not find_mutations(clause) and not _ACTION_EN_RE.search(_strip_negated_mutations(clause))
                and not _ACTION_AMBIGUOUS_EN_RE.search(clause)
                and not _ACTION_CJK_RE.search(_NEGATED_ACTION_CJK_RE.sub(_blank, clause))):
            continue
        if _NON_IMPERATIVE_START_RE.match(clause) and not re.match(r"^\W*(?:if|如果|要是)", clause, re.IGNORECASE):
            continue
        return clause.replace("․", ".")[:60]
    return None



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


def find_heavy_ex(text: str, lite_present: bool, question_nouns: bool = True) -> Tuple[Optional[str], bool]:
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
    topic = question_nouns and _is_readonly_question(text)
    for m in HEAVY_RE.finditer(text):
        if _before_label(text, m) or _is_path_part(text, m):
            continue
        if topic and m.group(0).lower() in QUESTION_NOUNS:
            continue
        nounish = m.group(0).lower() in NOUNISH_KEYWORDS
        if nounish and (lite_present or question) and _after_determiner(text, m, loose=question):
            continue
        if nounish and _after_determiner(text, m):
            noun_like = noun_like or m.group(0)
            continue
        return m.group(0), False
    for m in CJK_HEAVY_RE.finditer(text):
        if topic and CJK_QUESTION_NOUN_RE.fullmatch(m.group(0)) and not CLAUSE_START_RE.search(text[:m.start()]):
            continue
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
    r"\brun the\s+\w+|\btop to bottom\b|(?:^|\s)\d?>>?\s*(?!/dev/null)[\w./~-]+|"
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


# Commands that are never English prose; the rest need a flag or a path as first argument.
_PLAIN_COMMANDS = {"ls", "wc", "rg", "grep", "egrep", "fgrep", "du", "df", "pwd", "jq", "nl", "stat", "md5sum", "sha256sum"}
_PATHISH_ARG_RE = re.compile(r"^(?:-\S|[~./]|\S*[/.]\S*)")


def is_read_only_command(prompt: str) -> bool:
    """True when the whole prompt is one short read-only shell command line ("ls scripts/", "git log -5")."""
    text = prompt.strip().strip("`").strip()
    if not text or len(text) > 200 or not text.isascii():
        return False
    words = text.split()
    prog = words[0]
    if prog == "git":
        sub = next((w for w in words[1:] if not w.startswith("-")), "")
        if sub not in SCOUT_GIT_SUBCOMMANDS:
            return False
    elif prog in _PLAIN_COMMANDS:
        pass
    elif prog in ("cat", "head", "tail", "find", "tree", "diff"):
        if len(words) < 2 or not _PATHISH_ARG_RE.match(words[1]):
            return False
    else:
        return False
    return scout_command_is_read_only(text)[0]


# Polite request frames: "Would you mind bumping X and making sure Y?" asks for work in gerunds,
# which the imperative vocabulary above does not see. The -ing forms are built from the verb lists.
_ING_DOUBLE = {"commit", "rerun", "format", "reformat", "submit", "tag", "drop", "plan", "strip", "wrap", "ship",
               "stop", "map", "cut", "put", "set", "run", "pin", "log", "trim", "zip", "skip", "swap", "re-run", "reset", "spin", "split"}
_POLITE_BASE_VERBS = (
    "implement refactor rewrite edit modify patch fix delete remove rename replace insert append migrate "
    "add apply bump commit push install uninstall reinstall wire set change update upgrade downgrade write create move "
    "revert rollback merge rebase format reformat deploy publish release tag adjust tweak convert switch port lint "
    "typecheck run rerun re-run execute profile benchmark measure build rebuild recompile regenerate reproduce "
    "make_sure clean_up wire_up set_up roll_back double-check verify ensure test retest restart resolve "
    "sync pin drop strip split extract tidy bring_up spin_up clear reset re-add"
).split()


def _ing(word: str) -> str:
    head, sep, tail = word.partition("_")
    sep = " " if sep else ""
    if head.endswith("ie"):
        head = head[:-2] + "y"
    elif head.endswith("e") and not head.endswith("ee"):
        head = head[:-1]
    elif head in _ING_DOUBLE:
        head += head[-1]
    return head + "ing" + sep + tail


_POLITE_BASE_ALT = "|".join(v.replace("_", " ") for v in _POLITE_BASE_VERBS)
_POLITE_ING_ALT = "|".join(_ing(v) for v in _POLITE_BASE_VERBS)
_POLITE_ADV = r"(?:(?:also|just|first|then|please|now|again)\s+)?"

# Strong frames: the gerund or base verb after them is a request. "mind" keeps its force under a
# negation ("don't mind bumping"); "mind not bumping" never matches because "not" blocks the gerund.
_POLITE_STRONG_EN = (
    r"\b(?:would you mind|do you mind|did you mind|mind|appreciate(?: it if you| you)?|go ahead with)\s+"
    + _POLITE_ADV + r"(?:" + _POLITE_ING_ALT + r")\b|"
    r"\b(?:could you be so kind as to|can you go ahead and|could you go ahead and|go ahead and|feel free to|"
    r"it would be (?:great|nice|good|helpful) if you could|i'?d appreciate it if you could|"
    r"i'?d appreciate it if you(?: could| would)?|i would appreciate it if you(?: could| would)?)\s+"
    + _POLITE_ADV + r"(?:" + _POLITE_BASE_ALT + r")\b"
)
POLITE_STRONG_EN_RE = re.compile(_POLITE_STRONG_EN, re.IGNORECASE)
# Weaker frames also open lookups ("When does it start running?"); a wh- or auxiliary question opening
# the sentence, and any negation just before the frame, cancel them.
POLITE_WEAK_EN_RE = re.compile(
    r"\b(?:try|start|begin|keep|finish|continue|consider|proceed with)\s+(?:by\s+)?" + _POLITE_ADV
    + r"(?:" + _POLITE_ING_ALT + r")\b",
    re.IGNORECASE,
)
_POLITE_WEAK_OPEN_RE = re.compile(
    r"^\s*(?:what|which|where|when|why|how|who|does|did|do|is|are|was|were|will|can)\b", re.IGNORECASE)
_POLITE_WEAK_NEG_RE = re.compile(r"(?:n't|\bnot|\bnever|\bwithout|\bno)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_POLITE_CJK_FRAME = (r"(?:麻煩你|麻烦你|麻煩|麻烦|能不能幫我|能不能帮我|可以幫我|可以帮我|能不能幫忙|能不能帮忙|可以幫忙|可以帮忙|"
                     r"幫忙|帮忙|請你|请你|勞煩|劳烦|拜託你|拜托你)")
_POLITE_CJK_ACTION = (CJK_MUTATION_RE.pattern + r"|更新|清理|整理|調整|调整|重跑|重新|升|確保|确保|補|补|換|换|裝|装|"
                      r"設定|设定|設置|设置|處理|处理|優化|优化|搬|移動|移动|發布|发布|部署|測試|测试|驗證|验证|跑|執行|执行|運行|运行|寫|写|改|刪|删|修")
POLITE_CJK_RE = re.compile(
    _POLITE_CJK_FRAME + r"(?:(?!說明|说明|解釋|解释|了解|介紹|介绍|哪|查|看|找|列出|告訴|告诉|是什麼|是什么|[。？?！!，,]).){0,20}?"
    r"(?:" + _POLITE_CJK_ACTION + r")"
)


def find_polite_request(text: str) -> Optional[str]:
    """Return the polite action request in text ("mind bumping", "麻煩你升級"), or None."""
    stripped = _strip_negated_mutations(text)
    m = POLITE_STRONG_EN_RE.search(stripped)
    if m:
        return m.group(0)
    for m in POLITE_WEAK_EN_RE.finditer(stripped):
        before = stripped[:m.start()]
        sentence = before[max(before.rfind(c) for c in ".;!?\n") + 1:]
        if not _POLITE_WEAK_OPEN_RE.match(sentence) and not _POLITE_WEAK_NEG_RE.search(before[-30:]):
            return m.group(0)
    m = POLITE_CJK_RE.search(stripped)
    if m and not CJK_NEGATED_MUTATION_RE.search(stripped) and not _NEGATED_ACTION_CJK_RE.search(stripped):
        return m.group(0)
    return None


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
    "question-noun": 0.7,
    "readonly-command": 0.8,
    "shell-mutation": 0.95,
    "trailing-imperative": 0.85,
    "polite-request": 0.9,
    "default": 0.5,
}


# Rules a judge may overrule when their confidence is low. Type, workspace and
# explicit-model rules state the caller's intent and are never sent to a judge.
JUDGEABLE_RULES = {"mutation", "heavy", "heavy-noun", "lite", "long-prompt", "short-prompt",
                  "short-prompt-unknown", "docs-lookup", "default", "question-noun", "readonly-command"}


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
      2b. polite action request ("would you mind bumping...") -> flash
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
    # A read-only question that only passed because "migration", "debug", "build" or "tests" counted as a topic.
    topic_only = not mutation and not heavy and _is_readonly_question(text) and bool(
        find_mutation(text, False) or find_heavy_ex(text, lite is not None, False)[0])

    if bare_type == WORKER_TYPE:
        return done(_rule_decision("flash", None, "worker-type", "flash-worker type"))
    if mutation:
        if bare_type == SCOUT_TYPE:
            upgrade_type = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE
            return done(_rule_decision("flash", upgrade_type, "mutation",
                                  f"mutation verb '{mutation}' on read-only scout; upgraded to flash-worker"))
        return done(_rule_decision("flash", None, "mutation", f"mutation verb '{mutation}'"))
    shell_mutation = find_shell_mutation(text)
    if shell_mutation:
        upgrade_type = (f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE) if bare_type == SCOUT_TYPE else None
        return done(_rule_decision("flash", upgrade_type, "shell-mutation",
                                   f"shell mutation '{shell_mutation}'"))
    polite = find_polite_request(text)
    if polite:
        upgrade_type = (f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE) if bare_type == SCOUT_TYPE else None
        return done(_rule_decision("flash", upgrade_type, "polite-request", f"polite action request '{polite}'"))
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
    trailing = find_trailing_imperative(prompt)
    if trailing:
        return done(_rule_decision("flash", None, "trailing-imperative",
                                   f"action clause '{trailing}' follows a question"))
    run_task = find_run_task(text)
    if lite and len(prompt) <= 600 and not run_task:
        if topic_only:
            return done(_rule_decision("flash_lite", None, "question-noun", f"read-only question; lite signal '{lite}'"))
        return done(_rule_decision("flash_lite", None, "lite", f"lite signal '{lite}'"))
    if len(prompt) <= 320 and not run_task and is_read_only_command(prompt):
        return done(_rule_decision("flash_lite", None, "readonly-command", "prompt is a read-only shell command"))
    if len(prompt) > 450:
        return done(_rule_decision("flash", None, "long-prompt", "long prompt"))
    if len(prompt) <= 320:
        if (LOOKUP_SIGNAL_RE.search(text) or READ_ONLY_RE.search(text)) and not run_task:
            if topic_only:
                return done(_rule_decision("flash_lite", None, "question-noun",
                                           "read-only question; migration/debug/build/test named as a topic"))
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
