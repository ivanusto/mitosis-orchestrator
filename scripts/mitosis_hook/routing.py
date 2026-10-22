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
    r"(?:nice|great|better|good|ideal|awesome|helpful|cool|useful|preferable)\s+(?:if(?!\s+you\b)|to)\b|"
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
    r"(?:" + IMPERATIVE_VERBS + r")\b(?![./]\w)",  # release.yml, format/ and tag.py name files, not verbs
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


# A run phrase that is the object of a "how / which command" question names a topic, not a job:
# "the command to run the test suite", "how do I run the linter", "跑測試的指令是什麼".
_RUN_CONJ = r"(?:and|then|but|or|so|if|with|against|on|in|using|via|after|before|until|once|when|while)"
_RUN_OBJ_EN = r"(?:(?!" + _RUN_CONJ + r"\b)(?:\.{1,2}/)?[\w/:-]+(?:\.[\w/:-]+)*[ \t]*){1,5}"
_RUN_OBJECT_EN_RE = re.compile(
    r"(?:\b(?:commands?|cmds?|scripts?|targets?|steps?|instructions?|ways?|recipes?|procedure)\b"
    r"(?:(?!\b(?:and|then)\b)[^.?!;\n,]){0,30}?\b(?:to|for)\s+"
    r"|\bhow\s+(?:to|(?:do|does|should|can|could|would)\s+(?:i|we|you|one|someone|people)(?:\s+\w+ly)?)\s+)"
    r"(?:re-?run|run|execute)\s+" + _RUN_OBJ_EN,
    re.IGNORECASE,
)
_CJK_RUN_VERB = r"(?:執行|执行|跑|運行|运行|重跑)"
_RUN_OBJECT_CJK_RE = re.compile(
    r"(?:怎麼|怎么|如何|要用什麼|要用什么)\s*(?:去|來|来)?\s*" + _CJK_RUN_VERB + r"[^。？?！!，,；;然並并再\n]{0,12}|"
    + _CJK_RUN_VERB + r"[^。？?！!，,；;然並并再\n]{0,12}?"
    r"(?=的\s*(?:指令|命令|方式|方法|步驟|步骤|腳本|脚本)|要用(?:哪個|什麼|哪个|什么))"
)


# Permission is not a request: "you may run `python3 -m py_compile`", "feel free to run", 可以執行, 允許跑.
_RUN_PERMISSION_RE = re.compile(
    r"\b(?:you\s+(?:may|can|could|might)|feel\s+free\s+to|it'?s\s+(?:ok|okay|fine)(?:\s+for\s+you)?\s+to|"
    r"(?:you\s+(?:are|'re)\s+|is\s+|are\s+)?(?:allowed|permitted)\s+to)\s+(?:also\s+)?(?:re-?run|run|execute|exec)\b|"
    r"(?:可以|允許|允许|可)(?:再|也)?(?:重跑|跑|執行|执行|運行|运行)",
    re.IGNORECASE,
)


def _blank_run_objects(text: str) -> str:
    """Blank the run phrases that only ask how or with which command to run, and permissions to run.

    Lengths are preserved.
    """
    return _CJK_NEGATED_RUN_RE.sub(_blank, _RUN_PERMISSION_RE.sub(_blank, _RUN_OBJECT_CJK_RE.sub(_blank, _RUN_OBJECT_EN_RE.sub(_blank, text))))


# CJK has no word boundaries, so these run without \b. Traditional and Simplified forms.
CJK_MUTATION_RE = re.compile(
    r"修改|改寫|改写|重構|重构|新增|刪除|删除|刪掉|删掉|移除|拿掉|去掉|實作|實現|实现|修正|修好|修復|修复|修掉|"
    r"改成|改為|改为|改名|換成|换成|寫入|写入|安裝|安装|提交|推送|更名|重新命名|替換|替换|取代|合併|合并|"
    r"更動|更改|改動|改动|升到|升級|升级|降到|設為|设为|建立|新建|創建|创建|補上|补上|"
    r"弄成|弄好|搞定|拉到|換掉|换掉|改掉|調成|调成|調大|調小|调大|调小|"
    r"加(?:上|入|一|個|个|進|进|到)|加\s+[A-Za-z_]|(?:幫|帮|給|给|替)[^。？?！!]{0,30}?加|"
    r"統一(?!處理|处理|管理|入口|由|的|化)|统一(?!處理|处理|管理|入口|由|的|化)|攤平|摊平|精簡|精简|收斂|收敛|封裝|封装|汰換|汰换|瘦身|包一層|包一层|對齊|对齐|重寫|重写|簡化|简化|"
    r"優化|优化|補齊|补齐|抽出|抽成|拆開|拆开|拆掉|補上|補充|补充"
)


# A Chinese verb used as a noun or adjective: 被修改的, 修正版, 安裝說明, 修復提交訊息.
# "說明修改 X 前要..." asks to explain the change, not to make it.
CJK_NOUN_BEFORE_RE = re.compile(r"(?:被|已|已經|已经|曾|曾經|曾经|說明|说明|解釋|解释|介紹|介绍|了解)\s*$")


CJK_MUTATION_NOUN_AFTER_RE = re.compile(
    r"^(?:的|過|过|版|紀錄|记录|訊息|信息|說明|说明|對照|对照|表|清單|清单|指南|流程|步驟|步骤|方式|方法|提交訊息|提交信息|原因|理由|目的|動機|动机)"
)


# Transitive "X掉" (關掉 砍掉 清掉 撤掉 停掉 拔掉). Intransitive descriptions (掛掉 當掉 壞掉 斷掉 跳掉 爛掉) and
# consumption verbs (吃掉 用掉 佔掉) are not requests; neither are particles in front of 掉.
_CJK_INTRANSITIVE = "掛挂當当壞坏死斷断跳爛烂漏忘輸输走跑逃用佔占花吃耗"
_CJK_NON_VERB = "會会被已不沒没有的得地而所"
_CJK_TRANS_DIAO = r"(?:(?![" + _CJK_INTRANSITIVE + _CJK_NON_VERB + r"])[一-鿿])掉"


# Negations ahead of a Chinese verb: 不要修改, 請勿刪除, 不需要任何改動.
CJK_NEGATED_MUTATION_RE = re.compile(
    r"(?:不要|不得|不可|不能|不准|不需要?|不用|不是要你|並非要你|并非要你|無須|无须|毋須|別|别|勿|請勿|请勿|禁止|切勿|避免)\s*"
    r"(?:任何|去|再|重新|重|來|来)?\s*"
    r"(?:" + CJK_MUTATION_RE.pattern + r"|[修改]|" + _CJK_TRANS_DIAO + r")"
)


# A mutation verb inside a "whether" frame (要不要整個拿掉 old 目錄, 是否修改 X) or inside a why-question clause
# (為什麼會把 scout 升級？) is the topic of the question, not a request. A "how" clause is not skipped:
# 如何實作 X 可以是研究任務的一部分。
_CJK_WHETHER_BEFORE_RE = re.compile(r"(?:要不要|是否|會不會|会不会|需不需要|該不該|该不该)[^把將将。？?！!，,；;\n]{0,4}$")
_CJK_WHY_CLAUSE_RE = re.compile(r"(?:為什麼|为什么|為何|为何|怎麼會|怎么会)[^。？?！!，,；;\n]*$")


# 在哪裡建立 / 哪個檔案 X / 什麼時候 X: a verb that closes a where/which/when question is the topic; with an
# object after it ("在哪裡建立一個 X") it stays a request.
_CJK_WHERE_CLAUSE_RE = re.compile(
    r"(?:在哪裡|在哪里|在哪|哪裡|哪里|哪個|哪个|哪些|什麼時候|什么时候|何時|何时|什麼時間|什么时间)[^。？?！!，,；;\n]*$")
_CJK_QUESTION_TAIL_RE = re.compile(r"^(?:的|了|過|过|嗎|吗|呢|\s)*(?:[。？?！!，,；;\n]|$)")


# "什麼時候從 3 改成 5 的？", "誰改的？", "是在這次 commit 改的": a cleft question about who/when a change was made.
_CJK_CLEFT_BEFORE_RE = re.compile(r"是|什麼時候|什么时候|誰|谁|何時|何时|哪")
_CJK_CLEFT_AFTER_RE = re.compile(r"的\s*[嗎吗呢]?\s*$")
# 看 ... 有沒有加 X: the verb inside a has-it/whether frame after a read verb is what is looked for.
_CJK_READ_WHETHER_RE = re.compile(
    r"(?:看|查|確認|确认|檢查|检查|找|問|问|瞧|驗證|验证)[^。？?！!，,；;\n]{0,40}?(?:有沒有|有没有|是否|有無|有无|會不會|会不会)[^把將将。？?！!，,；;\n]{0,8}$")


def _cjk_topic_frame(text: str, start: int, end: Optional[int] = None) -> bool:
    before = text[:start]
    if _CJK_WHETHER_BEFORE_RE.search(before) or _CJK_WHY_CLAUSE_RE.search(before) or _CJK_READ_WHETHER_RE.search(before):
        return True
    seg_start = max((before.rfind(c) for c in "。？?！!；;\n"), default=-1) + 1
    seg_end = min([i for i in (text.find(c, start) for c in "。？?！!；;\n") if i >= 0] or [len(text)])
    if _CJK_CLEFT_BEFORE_RE.search(text[seg_start:start]) and _CJK_CLEFT_AFTER_RE.search(text[start:seg_end]):
        return True
    return end is not None and bool(_CJK_WHERE_CLAUSE_RE.search(before) and _CJK_QUESTION_TAIL_RE.match(text[end:]))


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


# 別跑測試, 不用執行 build: a restriction on running, not a request.
_CJK_NEGATED_RUN_RE = re.compile(
    r"(?:不要|不用|不必|不需要?|別|别|勿|請勿|请勿|無須|无须|暫時不用|暂时不用|先不用|先不要)\s*(?:實際|实际|去|再)?"
    r"(?:執行|执行|跑|運行|运行|重跑)[^。？?！!，,；;]{0,8}?(?:測試|测试|建置|編譯|编译|基準|基准|build)")


def _blank(match: "re.Match[str]") -> str:
    return " " * len(match.group(0))


def _strip_negated_mutations(text: str) -> str:
    """Blank out negated mutation phrases so prohibitions do not read as tasks.

    Lengths are preserved, so match offsets still point into the original text.
    """
    return _CJK_NEGATED_RUN_RE.sub(_blank, CJK_NEGATED_MUTATION_RE.sub(_blank, NEGATED_MUTATION_RE.sub(_blank, text)))


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


# A test runner named in a question that asks about its state (needs a run) rather than its configuration.
_RUNNER_STATE_RE = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|fail(?:s|ed|ing)?|green|red|broken|succeed(?:s|ed)?|work(?:s|ing)?|"
    r"flak(?:y|es)|hang(?:s|ing)?|times? out|timed out|crash(?:es|ed)?|how (?:long|many (?:tests|cases)|fast)|"
    r"coverage|result|results|output)\b|通過|通过|失敗|失败|變紅|变红|轉綠|转绿|壞掉|多久|結果|结果|輸出|输出|覆蓋率|覆盖率",
    re.IGNORECASE,
)


_RUNNER_VERDICT_RE = re.compile(
    r"\b(?:pass(?:es|ing)?|fail(?:s|ing)?|green|red|broken|succeed(?:s)?|flak(?:y|es))\b|通過|通过|失敗|失败|變紅|变红|轉綠|转绿",
    re.IGNORECASE)


_RUN_TOOL_RE = re.compile(
    r"\b(?:pytest|unittest|jest|vitest|mypy|ruff|pylint|flake8|eslint|tsc|pyright|black|clippy|tests?|test suite|build|"
    r"lint(?:er)?|type[- ]?check(?:er)?|compiler?|ci|make|cargo test|go test|npm test)\b|測試|测试|建置|編譯|编译|型別檢查|类型检查",
    re.IGNORECASE)
_RUN_NOW_RE = re.compile(
    r"\b(?:right now|at the moment|currently|as of now|at present)\b|現在|现在|目前|此刻|當前|当前|眼下",
    re.IGNORECASE)
_RUN_RESULT_RE = re.compile(
    r"\b(?:report(?:s|ed)?|complain(?:s|ed)?|say|says|show(?:s)?|print(?:s)?|output|emit(?:s)?|flag(?:s)?|warn(?:s)?|fail(?:s|ing)?|"
    r"errors?|results?|status|how many|pass(?:es|ing)?|green|red)\b|結果|结果|報告|报告|輸出|输出|報錯|报错|失敗|失败|通過|通过|錯誤數|警告",
    re.IGNORECASE)


_RUN_HISTORY_RE = re.compile(
    r"\b(?:last|previous|prior|earlier|yesterday|logs?|history|artifacts?|according to|did|was|were|had)\b|ci-logs|\.(?:log|txt)\b|"
    r"#\d+|\brun\s+\d+|上次|上一次|昨天|日誌|日志|紀錄|记录|歷史|历史",
    re.IGNORECASE)
_RUN_CHECKER_RE = re.compile(
    r"\b(?:pytest|unittest|jest|vitest|mypy|ruff|pylint|flake8|eslint|tsc|pyright|black|clippy|tests?|test suite|build|"
    r"lint(?:er)?|type[- ]?check(?:er)?|compiler?|cargo test|go test|npm test|tox|make|image|suite|workflow|pipeline|"
    r"project|package|install(?:ation)?)\b|測試|测试|建置|編譯|编译|型別檢查|类型检查|映像",
    re.IGNORECASE)
# A tool's state asked as a yes/no question: Is/Does/Will + state word. Plain "build/run" verbs are left out here
# because "Does the build config ..." names the build as a topic.
_RUN_VERDICT_RE = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|fail(?:s|ed|ing)?|complain(?:s)?|green|red|clean|happy|ok|okay|broken|healthy|"
    r"succeed(?:s)?|compil(?:es|ing)|builds|installs|works)\b|"
    r"有沒有過|有没有过|過了沒|过了没|會不會報錯|会不会报错|有沒有報錯|有没有报错|過不過|过不过|有沒有通過|有没有通过|是否通過|是否通过|會不會失敗|会不会失败",
    re.IGNORECASE)
# "check whether the image builds": a request to find out the state, any state verb.
_RUN_WHETHER_RE = re.compile(
    r"\b(?:check|see|verify|confirm|find out|make sure|ensure|tell me|let me know|test)\s+(?:that\s+|whether\s+|if\s+)", re.IGNORECASE)
_RUN_WHETHER_VERB_RE = re.compile(
    r"\b(?:builds?|compiles?|installs?|works?|runs?|passes|pass|fails?|starts?|boots?|lints?|type-?checks?)\b", re.IGNORECASE)
_RUN_CJK_STATE_RE = re.compile(
    r"(?:看一下|看看|確認|确认|檢查|检查|驗證|验证|看)[^，。？?！!]{0,25}?(?:會不會|会不会|能不能|可不可以|有沒有|有没有|是否)\s*"
    r"(?:過|过|通過|通过|編|编|編譯|编译|壞|坏|報錯|报错|成功|失敗|失败|跑得過|跑得过)")
_RUN_YESNO_RE = re.compile(r"(?:^|[.?!;\n])\s*(?:and\s+)?(?:does|do|is|are|will|would|can|has|have)\b", re.IGNORECASE)


def _needs_current_run(text: str) -> Optional[str]:
    """A question about the state a runner, build or linter is in (now, or just "does it pass") needs a run.

    Short prompts only (long briefs mention tools as background). A log, an earlier run or a CI run number
    makes it a read.
    """
    stripped = _blank_run_objects(text)
    if len(stripped) > 240 or _RUN_HISTORY_RE.search(stripped):
        return None
    cjk = _RUN_CJK_STATE_RE.search(stripped)
    if cjk and _RUN_CHECKER_RE.search(stripped):
        return cjk.group(0)[:20]
    question = QUESTION_RE.search(stripped)
    tool, now = _RUN_TOOL_RE.search(stripped), _RUN_NOW_RE.search(stripped)
    if question and tool and now and _RUN_RESULT_RE.search(stripped):
        return now.group(0)
    verdict = _RUN_VERDICT_RE.search(stripped)
    if _RUN_WHETHER_RE.search(stripped):
        verb = _RUN_WHETHER_VERB_RE.search(stripped) or verdict
        if verb:
            return verb.group(0)
    if not _RUN_CHECKER_RE.search(stripped):
        return None
    if verdict and ((_RUN_YESNO_RE.search(stripped) and question) or not verdict.group(0).isascii()):
        return verdict.group(0)
    return None


def _runner_verdict_request(text: str) -> Optional[str]:
    """A test runner whose pass/fail state is asked for ("Does pytest pass on main?") needs a run, not a read."""
    stripped = _blank_run_objects(text)
    current = _needs_current_run(text)
    if current:
        return current
    if TEST_RUNNER_RE.search(stripped):
        m = _RUNNER_VERDICT_RE.search(stripped)
        if m:
            return m.group(0)
    return None


def _runner_is_topic(stripped: str) -> bool:
    """True when a test runner is named in a read-only question about its setup, not asked to run or judged.

    "Does ci.yml call pytest with -n auto?", "pytest 的 conftest.py 放在哪?" ask about files and settings;
    "Does pytest pass on main?" needs a run and is not covered.
    """
    return bool(TEST_RUNNER_RE.search(stripped)) and not _RUNNER_STATE_RE.search(stripped)


def _is_readonly_question(text: str) -> bool:
    """True for a question or lookup that asks for no action on the thing it names.

    "Can you run the migration on staging?" asks for a run and is not one. A test runner named only as a
    topic of a configuration question ("Which dirs hold a conftest.py for pytest?") does not count as a run.
    """
    stripped = _NEGATED_ACTION_CJK_RE.sub(_blank, _strip_negated_mutations(_blank_run_objects(text)))
    if not (QUESTION_RE.search(stripped) or _RO_LEAD_RE.search(stripped)):
        return False
    if EXECUTION_SIGNAL_RE.search(stripped) or _ACTION_CJK_RE.search(stripped):
        return False
    if TEST_RUNNER_RE.search(stripped) and not _runner_is_topic(stripped):
        return False
    if _ACTION_AMBIGUOUS_EN_RE.search(stripped):
        return False
    for m in _ACTION_EN_RE.finditer(stripped):
        if not DETERMINER_BEFORE_RE.search(stripped[max(0, m.start() - 30):m.start()]):
            return False
    return True


# Disposal construction: 把/將 + object + verb with a resultative complement (把 X 關掉, 搬到 Y, 弄乾淨, 調回, 清空).
_CJK_BA_RE = re.compile(r"[把將将]")
_CJK_COMPLEMENT_RE = re.compile(r"乾淨|干净|起來|起来|[掉好成到進进回上下開开空齊齐]")
_CJK_CLAUSE_BREAK = "。？?！!，,；;\n"
_CJK_NEG_PREFIX_RE = re.compile(
    r"(?:不要|不得|不可|不能|不准|不需要?|不用|不必|無須|无须|毋須|無需|无需|別|别|勿|請勿|请勿|禁止|切勿|避免|不是要你|並非要你|并非要你)"
)
# Looking verbs never dispose of anything; 列/印/貼 only read when they report back (列出來, 貼上來, ... 給我).
_CJK_READ_VERBS = "看讀读找查問问說说"
_CJK_REPORT_VERBS = "列印貼贴"
# In front of the complement these are not verbs: particles, locations (底下, 以上), prepositions, nouns (組成).
_CJK_NOT_VERB_BEFORE = (_CJK_NON_VERB + "之以最更很太都也還还就才又再底裡里中內内外前後后邊边面在於于從从向往朝對对跟和與与或及並并用為为讓让給给直至等來来去組组構构形達达造促")
_CJK_VIEW_VERBS = "當当視视算認认"
_CJK_SUMMARY_VERB_PAIRS = ("整理", "彙整", "汇整", "歸納", "归纳", "總結", "总结", "統計", "统计", "比較", "比较", "輸出", "输出", "顯示", "显示")
_CJK_PASSIVE_BEFORE_RE = re.compile(r"被[^。？?！!，,；;\n的]{0,12}$")
_CJK_CLAUSE_LEAD_RE = re.compile(
    r"^(?:(?:請|请|幫我|帮我|幫忙|帮忙|麻煩你?|麻烦你?|先|再|然後|然后|並|并|順便|顺便|你|可以|能不能)\s*)*$")
_CJK_READ_ANY_RE = re.compile(
    r"看|讀|读|找|列|查|印|貼|贴|告訴|告诉|回報|回报|給我|给我|說明|说明|顯示|显示|比對|比对|確認|确认|檢查|检查|統計|统计|"
    r"整理|翻|解釋|解释|總結|总结|比較|比较")
_CJK_NOUN_PHRASE_END_RE = re.compile(
    r"(?:在哪|在哪裡|在哪里|的那段|的地方|是哪|是哪個|是哪个|是什麼|是什么|的位置|的內容|的内容|的名字|的檔名|的路徑|的路径|[了過过])\s*$")
_CJK_REPORT_BACK_RE = re.compile(r"(?:給我|给我)\s*$")


# "改大一點", "調成..." style degree edits; 讓 X 打開/關閉; a clause that opens with a setting toggle (停用 X).
_CJK_DEGREE_EDIT_RE = re.compile(r"改[大小長短多少高低快慢寬窄嚴鬆久早晚]\s*(?:一點|一些|點|些)")
_CJK_LET_TOGGLE_RE = re.compile(
    r"[讓让使][^。？?！!，,；;\n]{1,20}?(?:打開|打开|開啟|开启|關閉|关闭|啟用|启用|停用|禁用|變成|变成)")
_CJK_TOGGLE_OPEN_RE = re.compile(
    r"(?:^|[。！；\n，,])\s*(?:(?:先|再|然後|然后|並|并|順便|顺便)\s*)*(?:關閉|关闭|啟用|启用|停用|禁用)(?!的|狀態|状态|後|后|時|时)")


# 替/給/幫/為 + object + a verb that is not a read verb opens an action clause: 替 api/users.py 補型別註記,
# 給 cli.py 包一層 try/except, 幫 Dockerfile 瘦身. The clause is short and the read verbs rule it out.
_CJK_FRAME_ACTION_RE = re.compile(
    r"(?:^|[。！；\n，,]|\s)(?:(?:請|请|先|再|然後|然后|直接|並|并)\s*)*([替給给幫帮為为])(?![我你他她它們们什麼么何了助忙自出予止主準准])"
    r"\s*([^。？?！!，,；;\n]{3,40})(?=[。？?！!，,；;\n]|$)")
# Nouns and descriptions after the frame: 給 X 的說明, 為 X 提供的.
_CJK_FRAME_TOPIC_RE = re.compile(r"[的之]\s*$|^[^\u4e00-\u9fff]*$|^\S+\s*[的是有在]")
CJK_NEGATED_FRAME_RE = re.compile(r"(?:不要|不用|不必|不需要?|別|别|勿|請勿|请勿|無須|无须)\s*$")


# 移到 utils/text.py, 改成 30, 存進 `cache`: verb + 到/成/進/回 + a code token, without 把, is an action.
_CJK_CODE_TOKEN = (r"(?:`[^`\n]+`|\d+(?![\d]*[個个次種种位條条檔份])|[A-Za-z_~.][\w.~-]*[/_.][\w./-]*|[A-Z][A-Z0-9_]{2,}|"
                   r"[A-Za-z_]\w*\(\))")
_CJK_RESULT_MOVE_RE = re.compile(
    r"([移搬挪拆存寫写丟丢放貼贴塞推拉改換换轉转調调切分降升])\s*(到|成|進|进|回|為|为)\s*(" + _CJK_CODE_TOKEN + r")")


# "pyproject.toml 版本改 2.3": 改/換 straight in front of a value or code token.
_CJK_BARE_SET_RE = re.compile(r"(?<![被已曾會会能可有])[改換换]\s*(?:成|為|为|到)?\s*(?:\d[\w.]*|`[^`\n]+`|[A-Za-z_][\w.-]*[_./][\w./-]*)")


def _find_cjk_disposals(text: str) -> List[Tuple[int, str]]:
    """Out-of-list Chinese actions: 把 + object + resultative verb, and transitive X掉."""
    found: List[Tuple[int, str]] = []
    for pattern in (_CJK_DEGREE_EDIT_RE, _CJK_LET_TOGGLE_RE, _CJK_TOGGLE_OPEN_RE):
        for m in pattern.finditer(text):
            if not _cjk_topic_frame(text, m.start()) and not CJK_NOUN_BEFORE_RE.search(text[:m.start()]):
                found.append((m.start(), m.group(0).strip(" 。！；，,\n")))
    for m in _CJK_RESULT_MOVE_RE.finditer(text):
        clause = re.split(r"[。？?！!，,；;\n]", text[:m.start()])[-1] + re.split(r"[。？?！!，,；;\n]", text[m.start():])[0]
        if (not _QUESTION_CJK_RE.search(clause) and not _cjk_topic_frame(text, m.start())
                and not CJK_NOUN_BEFORE_RE.search(text[:m.start()]) and not CJK_NEGATED_FRAME_RE.search(text[:m.start()])
                and not _CJK_PASSIVE_BEFORE_RE.search(text[:m.start()])):
            found.append((m.start(), m.group(0)[:20]))
    for m in _CJK_BARE_SET_RE.finditer(text):
        clause = re.split(r"[。？?！!，,；;\n]", text[:m.start()])[-1] + re.split(r"[。？?！!，,；;\n]", text[m.start():])[0]
        if (not _QUESTION_CJK_RE.search(clause) and not _cjk_topic_frame(text, m.start())
                and not CJK_NOUN_BEFORE_RE.search(text[:m.start()]) and not _CJK_PASSIVE_BEFORE_RE.search(text[:m.start()])):
            found.append((m.start(), m.group(0)[:20]))
    for m in _CJK_FRAME_ACTION_RE.finditer(text):
        rest = m.group(2)
        if (not _QUESTION_CJK_RE.search(rest) and not _POLITE_CJK_READ_RE.search(rest) and not _CJK_FRAME_TOPIC_RE.search(rest)
                and not _cjk_topic_frame(text, m.start(1)) and not CJK_NEGATED_FRAME_RE.search(text[:m.start(1)])):
            found.append((m.start(1), (m.group(1) + rest)[:20].strip()))
    for m in re.finditer(_CJK_TRANS_DIAO, text):
        if text[m.end():m.end() + 1] in ("了", "的") or _cjk_topic_frame(text, m.start()):
            continue
        if _CJK_PASSIVE_BEFORE_RE.search(text[:m.start()]):
            continue  # 被 OOM 砍掉: a description
        if CJK_NOUN_BEFORE_RE.search(text[:m.start()]) or CJK_MUTATION_NOUN_AFTER_RE.match(text[m.end():]):
            continue
        found.append((m.start(), m.group(0)))
    for ba in _CJK_BA_RE.finditer(text):
        end = len(text)
        for i in range(ba.end(), len(text)):
            if text[i] in _CJK_CLAUSE_BREAK:
                end = i
                break
        clause = text[ba.start():end]
        clause_start = max([text.rfind(c, 0, ba.start()) for c in _CJK_CLAUSE_BREAK] + [-1]) + 1
        if (_CJK_REPORT_BACK_RE.search(clause) or _CJK_NEG_PREFIX_RE.search(text[clause_start:ba.start()])
                or _cjk_topic_frame(text, ba.start())):
            continue
        lead = text[clause_start:ba.start()].strip()
        if (_CJK_CLAUSE_LEAD_RE.match(lead) and len(clause) >= 4 and not _CJK_READ_ANY_RE.search(clause)
                and not _CJK_NOUN_PHRASE_END_RE.search(clause) and not _QUESTION_CJK_RE.search(clause)):
            found.append((ba.start(), clause))  # clause-initial 把 + object + any verb: 把舊的 API key 作廢
            continue
        for comp in _CJK_COMPLEMENT_RE.finditer(clause, 3, 36):
            idx = comp.start()
            if idx < 3 or not "一" <= clause[idx - 1] <= "鿿":
                continue
            verb, nxt = clause[idx - 1], clause[comp.end():comp.end() + 1]
            if verb == "一" or verb in _CJK_NOT_VERB_BEFORE or nxt in ("了", "的", "嗎", "吗"):
                continue
            if verb in _CJK_READ_VERBS or (verb in _CJK_REPORT_VERBS and nxt in ("來", "来")):
                continue
            if comp.group(0) == "成" and (verb in _CJK_VIEW_VERBS or clause[idx - 2:idx] in _CJK_SUMMARY_VERB_PAIRS):
                continue
            if comp.group(0) == "回" and nxt in ("報", "报", "信", "覆", "复", "答", "應", "应", "傳", "传", "來", "来"):
                continue
            found.append((ba.start(), clause[:comp.end()]))
            break
    return found


# "the cache layer refactor", "our auth migration": a mutation word that closes a determiner noun phrase names a
# topic. "do/perform/start the X refactor" and "work on the X migration" still ask for the work.
_NP_HEAD_WORDS = {"refactor", "rewrite", "patch", "fix", "fixes", "rename", "edit", "migration", "replace"}
_NP_DET_BEFORE_RE = re.compile(
    r"\b(?:the|a|an|this|that|these|those|its|my|your|our|their)\s+(?:[\w-]+\s+){1,3}$", re.IGNORECASE)
_NP_REQUEST_VERB_BEFORE_RE = re.compile(
    r"\b(?:do|perform|carry out|complete|finish|start|begin|make|handle|run|execute|implement|land|ship|apply|"
    r"work on|go ahead with|tackle|kick off|get|take care of|please|then|and)\s+(?:(?:the|a|an|this|that|our|my|your)\s+)?$",
    re.IGNORECASE)
_NP_AFTER_RE = re.compile(
    r"^\s*(?:[.,;:!?)]|$|\b(?:and|or|but|to|before|for|of|in|on|at|that|which|is|was|will|needs?|has|have|should|"
    r"from|by|so|because|since|with|first|next|later)\b)", re.IGNORECASE)


def _is_np_head(text: str, match: "re.Match[str]") -> bool:
    if match.group(0).lower() not in _NP_HEAD_WORDS:
        return False
    clause_start = max(text.rfind(c, 0, match.start()) for c in ".;:!?\n") + 1
    before = text[clause_start:match.start()]
    det = _NP_DET_BEFORE_RE.search(before)
    if not det or not _NP_AFTER_RE.match(text[match.end():]):
        return False
    return not _NP_REQUEST_VERB_BEFORE_RE.search(before[:det.start()])


# "Which call site should we migrate first?", "what should we rename it to", "where would you add": asking for a
# choice or an opinion. A yes/no "can you X?" is a request and stays one.
_WH_OPINION_BEFORE_RE = re.compile(
    r"\b(?:which|what|where|how|when)\b[^.?!;\n]{0,60}?\b(?:should|would|could|shall|might|ought to)\s+"
    r"(?:we|i|you|they|one|it|someone)\s+(?:(?:also|first|even|really|best|then)\s+)?$",
    re.IGNORECASE,
)


_CJK_WH_AFTER_RE = re.compile(r"哪|誰|谁|幾|几|多少|什麼|什么|是不是|有沒有|有没有|進度|进度|進展|进展")
_CJK_REQUEST_LEAD_RE = re.compile(r"(?:請|请|幫|帮|麻煩|麻烦|可以|能不能|能否|想要|要)[^。？?！!，,；;\n]*$")


def _cjk_modifier_in_wh_clause(text: str, start: int, end: int) -> bool:
    """A verb that modifies a noun inside a wh-question ("統一格式的 logger 在哪個模組", "X 的工作做到哪了") is a topic."""
    before = re.split(r"[。？?！!，,；;\n]", text[:start])[-1]
    after = re.split(r"[。？?！!，,；;\n]", text[end:])[0]
    if _CJK_REQUEST_LEAD_RE.search(before):
        return False
    m = _CJK_WH_AFTER_RE.search(after)
    return bool(m and "的" in after[:m.start()])


_CJK_WHETHER_INSIDE_RE = re.compile(r"有沒有|有没有|是否|有無|有无|會不會|会不会")
_CJK_THEN_RE = re.compile(r"然後|然后|並|并|再|之後|之后|接著|接着")


def _is_identifier_word(phrase: str) -> bool:
    """An ALL-CAPS token (PORT, MOVE_LIMIT, BUILD_DIR) names a constant; it is never an English verb."""
    first = phrase.split()[0] if phrase.split() else ""
    return len(first) >= 2 and first.isupper() and first.isascii()


def find_mutations(text: str, question_nouns: bool = True) -> List[Tuple[int, str]]:
    """Return (offset, phrase) for every mutation request in text, in order.

    Negated phrases ("do not edit", "不要修改") and noun uses ("the patch
    version") do not count. In a read-only question "migration" is a topic too
    (question_nouns=False turns that off). Chinese verbs inside a "whether" or
    why/how frame are topics as well, and the Chinese disposal construction
    (把 X 關掉, 砍掉, 搬到 Y) counts although the verb is not in the word list.
    """
    stripped = _strip_negated_mutations(_blank_run_objects(text))
    skip_migration = question_nouns and _is_readonly_question(text)
    found: List[Tuple[int, str]] = []
    for m in MUTATION_RE.finditer(stripped):
        if _is_identifier_word(m.group(0)):
            continue
        if skip_migration and m.group(0).lower() == "migration":
            continue
        if not _is_noun_use(stripped, m) and not _is_path_part(stripped, m) and not _is_np_head(stripped, m) \
                and not _WH_OPINION_BEFORE_RE.search(stripped[max(0, m.start() - 90):m.start()]):
            found.append((m.start(), m.group(0)))
    for pattern in (IMPERATIVE_MUTATION_RE, GIT_MUTATION_RE, EXECUTION_RE, SOFT_REQUEST_RE):
        for m in pattern.finditer(stripped):
            if _is_identifier_word(m.group(0).strip(" .;:!?\n")):
                continue
            found.append((m.start(), m.group(0).strip(" .;:!?\n")))
    for m in CJK_MUTATION_RE.finditer(stripped):
        if CJK_NOUN_BEFORE_RE.search(stripped[:m.start()]) or CJK_MUTATION_NOUN_AFTER_RE.match(stripped[m.end():]):
            continue
        if _cjk_topic_frame(stripped, m.start(), m.end()) or _cjk_modifier_in_wh_clause(stripped, m.start(), m.end()):
            continue
        if _CJK_WHETHER_INSIDE_RE.search(m.group(0)) and not _CJK_THEN_RE.search(m.group(0)):
            continue  # 幫我看有沒有加 X: the added thing is what is asked about
        found.append((m.start(), m.group(0).strip()))
    found.extend(_find_cjk_disposals(stripped))
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
    r"[?？！!。；;\n]+|\.(?=\s|$)|[,，]\s*(?=(?:and then|then|and|please|also|so)\b)|[,，]\s*(?=(?:請|请|然後|然后|再|並|并|順便|顺便|幫我|帮我|幫忙|帮忙))",
    re.IGNORECASE,
)
_ABBREV_RE = re.compile(r"\b(e)\.(g)\.|\b(i)\.(e)\.|\b(vs|etc|cf)\.", re.IGNORECASE)
_QUESTION_START_RE = re.compile(
    r"^\W*(?:(?:and|then|also|but|so|please)\s+)?(?:what|which|where|why|how|who|whom|whose|when)\b",
    re.IGNORECASE,
)
_QUESTION_CJK_RE = re.compile(r"嗎|吗|哪|什麼|什么|為什麼|为什么|為何|为何|怎麼|怎么|如何|是否|有沒有|有没有|多少|誰|谁|幾|几|是不是|有無|有无|呢")
_QUESTION_AUX_RE = re.compile(
    r"^\W*(?:(?:and|then|also|but|so)\s+)?(?:(?:is|are|does|did|was|were|can|could|should|has)\s+\w|"
    r"(?:do|have)\s+(?:we|you|i|they|any|all|these|those)\b)",
    re.IGNORECASE,
)
_QUESTION_HEDGE_RE = re.compile(
    r"^\W*(?:(?:and|then|also|but|so)\s+)?(?:i\s+(?:wonder|was wondering)\s+(?:if|whether)\b|"
    r"(?:i'?m\s+|i\s+am\s+)?(?:not sure|unsure|not certain)\s+(?:if|whether|where|which|what|how|why|the)\b|"
    r"(?:i'?m\s+|i\s+am\s+)?curious\s+(?:if|whether|which|what|how|why|where)\b)",
    re.IGNORECASE,
)
_QUESTION_CJK_HEDGE_RE = re.compile(r"不確定|不确定|不知道|想知道|好奇")
# Restriction phrases: "不用重現", "別跑", "不必實際執行", "不需要改".
_CJK_RESTRICT = r"(?:不用|不必|不需要|別|别)\s*(?:實際|实际)?(?:重現|重现|執行|执行|跑|改|修)"
_TRAILING_READONLY_RE = re.compile(
    r"^\W*(?:(?:just|only|simply|please|and|then|also|first|先|請|请|只要|只|麻煩|麻烦|順便|顺便|幫我|帮我|幫忙|帮忙|只需要|只需|僅|仅)\s*)*"
    r"(?:tell me|show me|show|list|explain|describe|summari[sz]e|report(?: back)?|answer|point me|give me (?:the|a) (?:path|file|name|list)|"
    r"let me know|name|identify|locate|find (?:out|me)|look (?:at|in|into|through)|inspect|open(?!\s+(?:(?:a|an|the)\s+)?(?:new\s+)?(?:pr|prs|pull request|mr|merge request|issue|ticket|branch)\b)|view|check(?!\s+(?:out|in)\b)|consult|read|"
    r"(?:the )?(?:path|paths|file|files|name|names|command|commands|line|lines|list|answer|result|location|function)s?\b|"
    r"告訴我|告诉我|列出|說明|说明|解釋|解释|回報|回报|報告|报告|看看|看一下|看就好|看|查看|找出|指出|回答|"
    r"paste (?:the |its |their )?(?:output|result|results|contents?|text|diff|lines?)|貼出來|貼上來|贴出来|贴上来|"
    r"讀|读|閱讀|阅读|查一下|查|找|搜尋|搜寻|確認|确认|檢查|检查|比對|比对|對照|对照|統計|统计|翻|列一下|列)|"
    r"\b(?:only|just) (?:report|tell|list|show|explain|look|read|check|answer)\b|"
    r"\b(?:do not|don't|dont|no need to|without) (?:change|modify|edit|touch|write|fix|run|alter)\b|"
    r"\bno (?:changes|edits|writes)\b|\bread[- ]?only\b|"
    + _CJK_RESTRICT +
    r"|只要看|只看|只要告訴|只要告诉|不用改|不用修|不要改|不要動|不要动|先不要動|先不要动|不需要改|不用動|不用动|不必改|無須修改|无须修改|唯讀|只讀|只读",
    re.IGNORECASE,
)
# Looser hints that a follow-up clause only says how to look ("Paths only.", "git show 看就好", "不要執行").
_READONLY_HINT_RE = re.compile(
    r"\bonly\b|\b(?:use|using|check|via|with|from)\s+(?:the\s+)?(?:`?(?:rg|grep|git|wc|cat|find|ls|head|tail|jq)\b|git log|blame)|"
    r"\b(?:is|are) fine\b|\bwill do\b|\bis enough\b|"
    + _CJK_RESTRICT + r"|"
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
            or bool(_QUESTION_CJK_RE.search(clause)) or bool(_QUESTION_AUX_RE.search(clause))
            or bool(_QUESTION_HEDGE_RE.search(clause)) or bool(_QUESTION_CJK_HEDGE_RE.search(clause)))


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
        before = protected[:m.start()]
        if before.count("(") > before.count(")"):
            continue  # a break inside a parenthetical is not a sentence end
        clauses.append((protected[pos:m.start()], m.group(0)))
        pos = m.end()
    clauses.append((protected[pos:], ""))
    seen_question = False
    for clause, terminator in clauses:
        clause = clause.strip()
        if not clause or not re.search(r"[A-Za-z]{2}|[\u4e00-\u9fff]", clause):
            continue  # empty, or a list marker or bracket left over from a split
        if _is_question_clause(clause, terminator):
            seen_question = True
            continue
        if not seen_question:
            continue
        if _COURTESY_RE.match(clause) or _REFERENCE_RE.match(clause):
            continue
        if _TRAILING_READONLY_RE.search(clause) and not _RO_COMPOUND_ACTION_RE.search(clause):
            continue
        if _opening_clause_action(clause.replace("․", ".")):
            return clause.replace("․", ".")[:60]  # a verb that is not a read verb + it/the/a code token
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
    r"race condition|(?:on|from) the web(?=\s*(?:$|[.,;:!?)\n]|\s(?:for|to|about|and|or|instead|first|too|as|if|when|then|using|with)\b))|search the web|web search|"
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


_RUNNER_TOPIC_WORDS = {"pytest", "unittest", "jest", "vitest", "go test", "cargo test", "npm test"}


def find_heavy_ex(text: str, lite_present: bool, question_nouns: bool = True) -> Tuple[Optional[str], bool]:
    """First heavy signal in text as (phrase, noun_like), or (None, False).

    A heavy keyword that names a path part ("tests/", "build.gradle"), a
    label ("the bug report template") or a topic in a lookup ("where is the
    test for x", "which tests import y?") is skipped. A question skips
    determiner uses only when nothing in it asks to run anything. A heavy hit
    that still follows a determiner is returned with noun_like=True; a plain
    hit is preferred over a noun-like one.
    """
    text = _CJK_NEGATED_RUN_RE.sub(_blank, _blank_run_objects(text))
    question = bool(QUESTION_RE.search(text)) and not EXECUTION_SIGNAL_RE.search(text) \
        and not TEST_RUNNER_RE.search(text)
    noun_like: Optional[str] = None
    topic = question_nouns and _is_readonly_question(text)
    runner_topic = topic and bool(TEST_RUNNER_RE.search(text))
    for m in HEAVY_RE.finditer(text):
        if _before_label(text, m) or _is_path_part(text, m):
            continue
        if topic and m.group(0).lower() in QUESTION_NOUNS:
            continue
        if runner_topic and m.group(0).lower() in _RUNNER_TOPIC_WORDS:
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
    if not noun_like and question_nouns:
        current = _needs_current_run(text)
        if current:
            return f"current result ({current})", False
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
    r"\bcheck(?:ed|ing)? (?:whether|if)\b|\b(?:tail|read|summari[sz]e|explain|walk me through|tell me|display|cat)\b|"
    r"哪裡|哪里|哪個|哪个|哪些|哪幾|哪几|在哪|列出|找出|查找|有沒有|有没有|是不是|什麼|什么|多少|幾個|几个|有哪些|"
    r"看一下|貼上來|贴上来|貼出來|贴出来|弄清楚|搞清楚|釐清|厘清|讀|读|摘要|總結|总结|解釋|解释|顯示|显示|告訴我|告诉我|說明|说明|了解|怎麼|怎么|如何",
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


# The scout has no web access: explicit web tools, vendor docs sites and release notes on a site need a worker.
WEB_NEED_RE = re.compile(
    r"\bweb ?fetch\b|\bweb ?search\b|\bsearch (?:the )?web\b|\b(?:on|from) the web(?=\s*(?:$|[.,;:!?)\n]|\s(?:for|to|about|and|or|instead|first|too|as|if|when|then|using|with)\b))|"
    r"\b(?:search|look(?:ing)? (?:it |this |that )?up|check|find|read|browse|google)\b[^.\n]{0,40}\bonline\b|"
    r"\bonline (?:docs?|documentation|search|sources?)\b|"
    r"\b(?:vendor|official|upstream|project)(?:'s)? (?:docs?|documentation)(?: (?:site|website|page|pages))?\b|"
    r"\b(?:docs?|documentation) (?:site|website)\b|"
    r"\brelease notes\b[^.\n]{0,40}\b(?:site|website|online|web)\b|"
    r"\b(?:site|website)\b[^.\n]{0,30}\brelease notes\b|上網查|上网查|網路上查|网路上查|官網|官网",
    re.IGNORECASE,
)


# A local file or path named like a doc ("CHANGELOG", "docs/guide.md", "CHANGELOG.md") is a read, not a web lookup.
_LOCAL_PATH_RE = re.compile(r"(?<![\w:/.-])(?:\./|\.\./|~/)?[\w.-]+(?:/[\w.-]*)+")
_DOMAINISH_RE = re.compile(r"^(?:www\.|[\w-]+\.(?:com|org|net|io|dev|app|ai|co|edu|gov|tw|jp)\b)", re.IGNORECASE)
_LOCAL_DOC_FILE_RE = re.compile(
    r"\b[\w-]*(?:changelog|release[-_]?notes|docs?|documentation|readme|history|news)[\w-]*\.(?:md|rst|txt|adoc)\b",
    re.IGNORECASE,
)
_LOCAL_DOC_BARE_RE = re.compile(r"\b(?:CHANGELOG|RELEASE[-_]?NOTES|HISTORY|NEWS)\b")
_LOCAL_DOC_DETERMINERS = {"the", "our", "this", "my", "local", "repo", "repo's", "project's"}


def _blank_local_docs(text: str) -> str:
    """Blank file names and paths, so a doc-like name does not read as a web lookup; lengths are kept."""
    text = _LOCAL_PATH_RE.sub(lambda m: m.group(0) if _DOMAINISH_RE.match(m.group(0)) else _blank(m), text)
    text = _LOCAL_DOC_FILE_RE.sub(_blank, text)
    out = text
    for m in _LOCAL_DOC_BARE_RE.finditer(text):
        prev = re.search(r"([A-Za-z0-9_'.-]+)\s+$", text[max(0, m.start() - 20):m.start()])
        if prev and prev.group(1).lower() not in _LOCAL_DOC_DETERMINERS:
            continue  # "the requests CHANGELOG" names a library's changelog
        out = out[:m.start()] + " " * len(m.group(0)) + out[m.end():]
    return out


def needs_web(text: str) -> bool:
    """True when text asks for web access or a docs lookup (a scout has no web)."""
    return bool(WEB_NEED_RE.search(text) or DOCS_LOOKUP_RE.search(_blank_local_docs(text)))


# A run that is still asked for after the how-to-run phrases are blanked: "...and run it", "execute them", 跑一次.
RUN_DIRECTIVE_RE = re.compile(
    r"\b(?:re-?run|run|execute)\s+(?:it|them|that|this|these)\b|"
    r"\b(?:and|then)\s+(?:also\s+|then\s+)?(?:re-?run|run|execute)\b(?!\s+[`'\"]?(?:git|ls|cat|grep|rg|head|tail|wc|find|sed|awk)\b)|"
    r"(?<![會会能可要用別别])(?:跑一次|跑一下|然後跑|然后跑|執行一次|执行一次|並執行|并执行|並跑|并跑|再跑|再執行|再执行)",
    re.IGNORECASE,
)


def find_run_directive(text: str) -> Optional[str]:
    """A run request left in text once the "command to run X" phrases are blanked, or None."""
    m = RUN_DIRECTIVE_RE.search(_blank_run_objects(text))
    return m.group(0).strip() if m else None


def find_run_task(text: str) -> Optional[str]:
    """First execution signal in text (imperative run, CLI command, Chinese run verb), or None."""
    text = _blank_run_objects(text)
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


_PAST_IRREGULAR = {"write": "wrote", "run": "ran", "rerun": "reran", "re-run": "re-ran", "build": "built",
                   "rebuild": "rebuilt", "make": "made", "set": "set", "reset": "reset", "split": "split",
                   "bring": "brought", "spin": "spun", "put": "put", "cut": "cut"}


def _ed(word: str) -> str:
    head, sep, tail = word.partition("_")
    sep = " " if sep else ""
    if head in _PAST_IRREGULAR:
        head = _PAST_IRREGULAR[head]
    elif head.endswith("e"):
        head += "d"
    elif head.endswith("y") and head[-2:-1] not in "aeiou":
        head = head[:-1] + "ied"
    else:
        if head in _ING_DOUBLE:
            head += head[-1]
        head += "ed"
    return head + sep + tail


_POLITE_BASE_ALT = "|".join(v.replace("_", " ") for v in _POLITE_BASE_VERBS)
_POLITE_PAST_ALT = "|".join(_ed(v) for v in _POLITE_BASE_VERBS)
_POLITE_ING_ALT = "|".join(_ing(v) for v in _POLITE_BASE_VERBS)
_POLITE_ADV = r"(?:(?:also|just|first|then|please|now|again)\s+)?"

# Strong frames: the gerund or base verb after them is a request. "mind" keeps its force under a
# negation ("don't mind bumping"); "mind not bumping" never matches because "not" blocks the gerund.
_POLITE_STRONG_EN = (
    r"\b(?:would you mind|do you mind|did you mind|mind|appreciate(?: it if you| you)?|go ahead with)\s+"
    + _POLITE_ADV + r"(?:" + _POLITE_ING_ALT + r")\b|"
    r"\b(?:could you be so kind as to|can you go ahead and|could you go ahead and|go ahead and|feel free to|"
    r"it would be (?:great|nice|good|helpful) if you could|i'?d appreciate it if you could|"
    r"i'?d appreciate it if you(?: could| would)?|i would appreciate it if you(?: could| would)?|"
    r"would it be possible to)\s+"
    + _POLITE_ADV + r"(?:" + _POLITE_BASE_ALT + r")\b|"
    r"\b(?:(?:i|we)(?:'d| would) appreciate it if you|it(?:'d| would) be (?:so |really |much )?"
    r"(?:great|nice|good|helpful|better|awesome|cool|useful) if you(?: could| would| can)?)\s+"
    + _POLITE_ADV + r"(?:" + _POLITE_PAST_ALT + r"|" + _POLITE_BASE_ALT + r")\b"
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


# Any polite frame, bare 請/幫我 included ("請問" and 申請/邀請 are not frames). The first verb after a frame is an
# action unless it is a read-only verb; 把/preposition openings are left to the windowed pattern above.
_POLITE_CJK_ANY_FRAME_RE = re.compile(
    r"(?<![申邀聘延敬約约催特])(?:" + _POLITE_CJK_FRAME + r"|請(?![問求假客教益示願])|请(?![问求假客教益示愿])|幫我|帮我)"
)
_POLITE_CJK_FILLER_RE = re.compile(
    r"(?:[你您先再也就]|順便|顺便|具體|具体|精確|精确|明確|明确|完整|清楚|準確|准确|確實|确实|一併|一并|一起|同時|同时|另外|順手|顺手|一下|幫我|帮我|幫忙|帮忙|直接|仔細|仔细|詳細|详细|簡單|简单|盡量|尽量|務必|务必|能不能|可以|不妨|麻煩|麻烦)*"
)
_POLITE_CJK_READ_RE = re.compile(
    r"看|查|找|讀|读|列|確認|确认|檢查|检查|比對|比对|對照|对照|統計|统计|說明|说明|解釋|解释|告訴|告诉|問|问|回報|回报|翻|搜尋|搜寻|閱讀|阅读|總結|总结|摘要|追蹤|追踪|追查|追|盤點|盘点|分析|研究|調查|调查|留意|注意|比較|比较|評估|评估|判斷|判断|推測|推测|確定|确定|了解|釐清|厘清|查證|查证|核對|核对|檢視|检视|審視|审视|掃|扫|瀏覽|浏览|計算|计算|數|数|估"
)
_POLITE_CJK_NOT_VERB = "把將将在於于從从對对用以依按針针不別别勿莫無无毋禁切避"


def _polite_cjk_action(stripped: str) -> Optional[str]:
    for m in _POLITE_CJK_ANY_FRAME_RE.finditer(stripped):
        rest = stripped[m.end():]
        rest = rest[_POLITE_CJK_FILLER_RE.match(rest).end():]
        if not rest or not "\u4e00" <= rest[0] <= "\u9fff" or rest[0] in _POLITE_CJK_NOT_VERB:
            continue
        if not _POLITE_CJK_READ_RE.match(rest):
            return m.group(0) + rest[:4]
    return None


_FEEL_FREE_RE = re.compile(r"\bfeel\s+free\s+to\s+(?:also\s+)?(?:re-?run|run|execute|exec)\b", re.IGNORECASE)


def find_polite_request(text: str) -> Optional[str]:
    """Return the polite action request in text ("mind bumping", "麻煩你升級"), or None."""
    stripped = _FEEL_FREE_RE.sub(_blank, _strip_negated_mutations(text))  # "feel free to run" is permission
    m = POLITE_STRONG_EN_RE.search(stripped)
    if m:
        return m.group(0)
    for m in POLITE_WEAK_EN_RE.finditer(stripped):
        before = stripped[:m.start()]
        sentence = before[max(before.rfind(c) for c in ".;!?\n") + 1:]
        if not _POLITE_WEAK_OPEN_RE.match(sentence) and not _POLITE_WEAK_NEG_RE.search(before[-30:]):
            return m.group(0)
    if CJK_NEGATED_MUTATION_RE.search(stripped) or _NEGATED_ACTION_CJK_RE.search(stripped):
        return None
    m = POLITE_CJK_RE.search(stripped)
    if m:
        return m.group(0)
    return _polite_cjk_action(stripped)


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
    "scout-web": 0.9,
    "scout-read-only": 0.7,
    "scout-imperative": 0.85,
    "scout-unproven": 0.6,
    "worker-lookup": 0.7,
    "run-directive": 0.9,
    "default": 0.5,
}


# Rules a judge may overrule when their confidence is low. Type, workspace and
# explicit-model rules state the caller's intent and are never sent to a judge.
JUDGEABLE_RULES = {"mutation", "heavy", "heavy-noun", "lite", "long-prompt", "short-prompt",
                  "short-prompt-unknown", "docs-lookup", "default", "question-noun", "readonly-command",
                  "scout-read-only", "worker-lookup", "scout-unproven"}


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
        type_override = None if decision.rule == "worker-lookup" else decision.type_override
        return Decision("flash", type_override, _tier_effort("flash"), decision.confidence,
                        "default", decision.rule, f"{decision.reason}; judge unavailable, conservative flash")
    tier, confidence = verdict
    type_override = decision.type_override
    if decision.rule == "worker-lookup":
        # A worker-typed lookup was downgraded to the scout: the judge confirms it, or vetoes it and the worker stays.
        type_override = None if tier == "flash" else type_override
    elif scout_typed:
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


_SCOUT_IMPERATIVE_START_RE = re.compile(
    r"^\W*(?:(?:please|then|also|and|just)\s+)*(?:get|make|clear|kill|bring|take|put|turn|ensure|use|go|try|do|let|start|stop|"
    r"restart|reset|clean|tidy|squash|generate|build|fix|skip|disable|enable|mark|unpin|raise|lower|increase|decrease|"
    r"speed up|mute|silence|comment out|uncomment|open(?: a| an| the)? (?:new )?(?:pr|pull request|mr|issue|ticket|branch)|"
    r"check (?:out|in)|quarantine|xfail|" + _POLITE_BASE_ALT + r")\b|[\u4e00-\u9fff]",
    re.IGNORECASE,
)


_LIST_LINE_RE = re.compile(r"^\s*(?:[-*\u2022]\s+|\d+[.)\u3001]\s*)")


_SEQUENCED_RE = re.compile(
    r"^\W*(?:and\s+then|then|after\s+that|afterwards?|next|once\s+(?:that'?s\s+)?(?:done|finished)|following\s+that)\b[,\s]*"
    r"(?:(?:please|also|just)\s+)*([a-z][\w-]*)",
    re.IGNORECASE,
)
_SEQUENCED_CJK_RE = re.compile(r"^\W*(?:然後|然后|接著|接着|之後|之后|再)\s*(?:請|请)?\s*(\S)")
_SEQUENCED_READ_WORDS = {
    "read", "look", "check", "list", "report", "tell", "show", "summarize", "summarise", "explain", "describe", "return",
    "answer", "paste", "print", "note", "compare", "confirm", "verify", "find", "locate", "identify", "count", "trace",
    "grep", "search", "inspect", "review", "see", "give", "name", "mention", "quote", "cite", "display", "point",
    "i", "we", "you", "it", "they", "the", "a", "an", "this", "that", "there", "these", "those", "my", "our", "your",
}


def _sequenced_action(clause: str) -> bool:
    """A clause that opens with then/next/然後/再 plus a verb that is not a read verb is an action."""
    m = _SEQUENCED_RE.match(clause)
    if m:
        return m.group(1).lower() not in _SEQUENCED_READ_WORDS
    m = _SEQUENCED_CJK_RE.match(clause)
    return bool(m and "\u4e00" <= m.group(1) <= "\u9fff" and not _POLITE_CJK_READ_RE.match(clause[m.start(1):]))


_OPENING_SKIP = {
    # read and research verbs
    *(_SEQUENCED_READ_WORDS - {"point"}), "run", "execute", "exec", "rerun", "invoke", "launch", "research", "investigate", "analyze", "analyse", "examine", "explore", "study", "survey",
    "audit", "assess", "evaluate", "diagnose", "debug", "understand", "determine", "gather", "collect", "compile",
    "enumerate", "scan", "map", "outline", "walk", "go", "dig", "scout", "browse", "fetch", "get", "grab", "learn",
    "figure", "work", "tracing", "survey", "catalog", "catalogue", "inventory", "diff", "view", "open", "cat",
    "state", "provide", "specify", "spell", "extract", "highlight", "flag", "capture", "record", "calculate",
    "ls", "rg", "git", "sed", "awk", "wc", "head", "tail", "do", "use", "keep", "remember", "ensure", "be", "put",
    "take", "set", "try", "include", "attach", "output", "respond", "reply", "return", "say", "mention", "limit",
    "restrict", "prefer", "avoid", "stay", "stick", "ignore", "focus", "consider", "assume", "treat", "count",
    "rank", "prioritize", "prioritise", "order", "group", "keep",
    # openers that are not verbs
    "context", "background", "task", "goal", "objective", "question", "summary", "overview", "project", "repo",
    "repository", "current", "currently", "all", "any", "each", "every", "some", "no", "not", "if", "when", "while",
    "for", "in", "on", "at", "with", "by", "from", "of", "as", "so", "but", "or", "and", "because", "since", "after",
    "before", "once", "until", "unless", "which", "what", "who", "whom", "whose", "why", "how", "where", "is", "are",
    "was", "were", "does", "did", "can", "could", "would", "should", "will", "may", "might", "been", "being", "have",
    "has", "had", "my", "its", "their", "here", "last", "latest", "recent", "top", "newest", "oldest", "previous",
    "unused", "stale", "pending", "unresolved", "few", "many", "several", "total", "step", "part", "phase", "plan", "note", "notes", "important",
}
_OPENING_RE = re.compile(r"^\W*(?:(?:please|just|also|kindly|and|then)\s+)*([A-Za-z][\w-]*)\s+([^\s,.;:!?]+)")
_OPENING_OBJECT_RE = re.compile(
    r"^(?:the|a|an|this|that|these|those|all|every|each|any|both|its|their|our|my|your|it|them|`.*|.*[_./\d`'\"-].*|[A-Z][A-Z_]{2,})$")
_OPENING_READ_PHRASE_RE = re.compile(
    r"^\W*(?:(?:please|just|also|kindly)\s+)*(?:(?:make|build|generate)\s+(?:a|an|the|some)\s+(?:\w+\s+)?(?:list|note|notes|summary|table|"
    r"inventory|map|overview|report|diagram|outline|comparison|count|tally|checklist)\b|set\s+aside\b|"
    r"take\s+(?:a|an|the)\s+(?:look|peek|glance|note)\b|put\s+together\b)",
    re.IGNORECASE)


# Verbs of _SCOUT_IMPERATIVE_START_RE that are never read verbs: they fire without an object check.
_OPENING_FORCE_RE = re.compile(
    r"^\W*(?:(?:please|then|also|and|just)\s+)*(?:comment\s+out|uncomment|speed\s+up|clear|kill|bring|turn|restart|reset|"
    r"clean|tidy|squash|generate|skip|disable|enable|mark|unpin|raise|lower|increase|decrease|mute|silence|"
    r"(?:stub|rip|factor|strip|swap|carve|break|pull|split|cut|pare|spin|wipe|tear|knock|blank|zero)\s+out|"
    r"quarantine|xfail|stop|start(?!\s+(?:by|with|from|at)\b)|check\s+(?:out|in)|"
    r"open(?:\s+(?:a|an|the))?\s+(?:new\s+)?(?:pr|pull request|mr|issue|ticket|branch))\b",
    re.IGNORECASE,
)


# Chinese opening verbs that change something; a read verb (看 查 找 列出 確認 檢查 告訴 ...) is not in the list.
_CJK_OPENING_VERBS = (
    "取消註解|取消注释|取消|註解掉|注釋掉|注释掉|解除|停用|啟用|启用|關閉|关闭|開啟|开启|打開|打开|重設|重设|重置|重啟|重启|重開|"
    "啟動|启动|停止|暫停|暂停|恢復|恢复|回滾|回滚|回退|還原|还原|清除|清空|清理|搬移|移動|移动|複製|复制|匯入|导入|匯出|导出|"
    "產生|产生|生成|部署|發佈|发布|標記|标记|跳過|跳过|忽略|鎖定|锁定|固定|套用|封存|歸檔|归档|合併|合并|拆分|拆成|調整|调整|"
    "調高|调高|調低|调低|放寬|放宽|收緊|收紧|縮短|缩短|延長|延长|加大|加長|加长|降低|提高|升|降|"
    "補上|补上|補齊|补齐|補|补|統一|统一|攤平|摊平|精簡|精简|收斂|收敛|封裝|封装|抽出|抽成|抽|汰換|汰换|替換|替换|瘦身|"
    "包一層|包一层|對齊|对齐|改寫|改写|重寫|重写|簡化|简化|優化|优化|移除|刪除|删除|刪|删|拿掉|搬|塞|換|换|改|加|寫|写|"
    "新增|修正|修復|修复|修|拆開|拆开|拆掉|重構|重构|抽離|抽离|內嵌|内嵌|攔截|拦截|限流|節流|节流|防抖|去重|排序|"
    "升級|升级|降級|降级|遷移|迁移|轉換|转换|改用|改成|改為|改为")
_CJK_OPENING_RE = re.compile(
    r"^\W*(?:(?:請|請你|请|请你|幫我|帮我|幫忙|帮忙|麻煩你?|麻烦你?|先|直接|再|然後|然后)\s*)*(?:" + _CJK_OPENING_VERBS
    + r")(?!的|法|充|查|樣|样|檢|检|密|版|訊|讯|過|过|好的)")


# Code-edit verbs that are plainly actions at the head of an imperative. The second group also names things
# ("a cache", "the gate", "a patch"), so it needs more than a plain next word (see _opening_verb_action).
_OPENING_EDIT_VERBS = {
    "pin", "swap", "inline", "hoist", "memoize", "dedupe", "dedup", "clamp", "tighten", "loosen", "retire", "debounce",
    "throttle", "vendor", "bump", "reorder", "normalize", "normalise", "unify", "consolidate", "flatten", "deprecate",
    "decouple", "streamline", "harden", "rename", "rewrite", "refactor", "migrate", "replace", "convert", "move",
    "merge", "reroute", "relocate", "deduplicate", "parameterize", "parameterise", "vectorize", "vectorise",
    "sort", "wrap", "split", "cap", "drop", "trim", "slim", "shrink",
}
_OPENING_EDIT_NOUN_VERBS = {
    "port", "patch", "switch", "cache", "thread", "gate", "stub", "wire", "shim", "mock", "fork", "pipe", "route", "flag",
    "toggle", "stage", "lock", "log", "queue", "index", "map", "test", "build",
}
# Words that cannot be the plain object of an opening verb: prepositions, auxiliaries, pronouns, conjunctions.
_OPENING_NOT_OBJECT = {
    "of", "for", "in", "on", "at", "by", "from", "about", "across", "under", "inside", "within", "between", "among",
    "per", "via", "with", "without", "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "has", "have",
    "had", "can", "could", "will", "would", "should", "shall", "may", "might", "must", "that", "which", "who", "whom",
    "whose", "what", "where", "when", "why", "how", "and", "or", "but", "not", "no", "as", "if", "than", "then", "so",
    "i", "we", "you", "they", "he", "she", "there", "here", "to", "into", "free", "sure", "up", "out", "covering", "covers", "using", "named", "called", "mentioning",
}
# After the second word these keep a noun phrase a lookup ("Retry logic in clients/", "Config values for X").
_OPENING_NOT_ACTION_LEAD = {
    "without", "point",
    "the", "a", "an", "this", "that", "these", "those", "its", "their", "our", "my", "your", "it", "they", "we", "some",
    "any", "each", "every", "all", "both", "another", "one", "two", "three", "there", "he", "she", "you",
    "feel", "let", "want", "need", "wish", "hope", "think", "know", "ask", "wonder", "wait", "assume", "suppose",
    "consider", "imagine", "believe", "mean", "seem", "look", "looks", "sounds", "sound", "pls", "thanks", "thank",
    "sorry", "hey", "hi", "hello", "ok", "okay", "yes", "yep", "sure", "good", "great", "nice", "also", "now",
    "next", "first", "recent", "last", "latest", "previous", "unused", "stale", "dead", "old", "new", "current", "existing",
    "pending", "unresolved", "failing", "broken", "top", "few", "many", "several", "other", "oldest", "newest", "total",
    "finally", "lastly", "meanwhile", "afterwards", "tip", "hint", "example", "e", "i", "ps",
}
# Mostly verbs: stub, mock, wire... A preposition after the object ("Stub payments in tests/x.py") stays an action.
_OPENING_VERBISH = {"stub", "mock", "shim", "wire", "port", "switch", "toggle", "fork", "pipe"}
_OPENING_NP_TAIL_STRONG_RE = re.compile(
    r"^\s*(?:\w+ing\b|used|stored|kept|defined|called|imported|triggered|loaded|written|read|owned|configured|referenced|"
    r"of|for|that|which|who|where|covering|covers|using|named|is|are|was|were|has|have)\b", re.IGNORECASE)
# "Lock file entries for urllib3": a second plain noun, plural or abstract, then a preposition.
_OPENING_COMPOUND_RE = re.compile(
    r"^\s*[a-z][a-z-]*(?:s|tion|ment|ing|ity)\s+(?:of|for|in|on|at|from|about|with|used|stored|defined|that|which)\b")
_OPENING_DETERMINERS = {"the", "a", "an", "this", "that", "these", "those", "all", "every", "each", "any", "both", "its",
                        "their", "our", "my", "your", "it", "them"}
_OPENING_NP_TAIL_RE = re.compile(
    r"^\s*(?:\w+ing\b|used|stored|kept|defined|called|imported|triggered|loaded|written|read|owned|configured|referenced|"
    r"set|of|for|in|on|at|from|about|across|under|inside|within|between|per|with|without|that|which|who|where|"
    r"covering|covers|using|named|called|is|are|was|were|has|have)\b", re.IGNORECASE)
_OPENING_TARGET_RE = re.compile(
    r"[`'\"]|\b\w*[_./\\]\w+|\d|\b(?:to|into|onto|for|in|with|via|from)\s+\S", re.IGNORECASE)
_OPENING_NOUNISH_SUFFIX_RE = re.compile(r"(?:tion|sion|ment|ness|ity|age|ance|ence|ing|ure|ist|ism|ics)$")
_OPENING_SENTENCE_RE = re.compile(r"(.*?)(?:([.?!;])(?=\s|$)|(:)\s|\n|$)", re.DOTALL)
# "No need to look further, just bump X": the read restriction is not the request, what follows is.
_OPENING_RESTRICTION_RE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|so|well|please|just)[,\s]+)*(?:(?:there'?s\s+)?no\s+need\s+(?:to|for)|you\s+don'?t\s+need\s+to|"
    r"don'?t\s+bother(?:\s+(?:to|with))?|no\s+need|skip|forget\s+about)\s+"
    r"(?:look|check|read|grep|search|dig|investigate|inspect|explore|open|trace|verify|review|browse|scan|go|bother|dive|"
    r"research|examin|analy[sz])[^,;:]{0,60}[,;:]\s*(?:(?:and|then|just|simply|instead)\s+)*", re.IGNORECASE)


def _opening_verb_action(clause: str, question: bool, colon: bool = False) -> bool:
    """True for "<edit verb> <plain object> ..." and for an unlisted verb-like word with a plain object and a target."""
    m = _OPENING_RE.match(clause)
    if not m or question:
        return False
    verb, obj = m.group(1), m.group(2)
    low = verb.lower()
    if low in _OPENING_SKIP or low in _OPENING_NOT_ACTION_LEAD or not (verb.islower() or verb.istitle()):
        return False
    obj_low = obj.lower()
    if obj_low in _OPENING_NOT_OBJECT or _QUESTION_START_RE.search(clause):
        return False
    rest = clause[m.end():]
    plain = bool(re.match(r"^[a-z][a-z-]*$", obj))
    if low in _OPENING_EDIT_VERBS:
        return True
    if low in _OPENING_EDIT_NOUN_VERBS:
        if obj_low in _OPENING_DETERMINERS or (_OPENING_OBJECT_RE.match(obj) and not plain):
            return True
        if not plain or colon:
            return False
        if low in _OPENING_VERBISH:
            # mostly used as verbs: only a clear noun-phrase tail makes it a lookup ("Patch notes for 3.2")
            return not _OPENING_NP_TAIL_STRONG_RE.match(rest)
        return not _OPENING_NP_TAIL_RE.match(rest) and not _OPENING_COMPOUND_RE.match(rest)
    # An unlisted verb-like word: not a plural or abstract noun, a plain object, and a concrete target.
    if (low.endswith("s") and not low.endswith("ss")) or _OPENING_NOUNISH_SUFFIX_RE.search(low) or not plain:
        return False
    if _OPENING_NP_TAIL_RE.match(rest):
        return False
    return bool(_OPENING_TARGET_RE.search(rest))


# A one or two character verb straight in front of a code token: 加密 config/secrets.yaml, 拆 utils/misc.py, 包 `main()`.
_CJK_VERB_OBJECT_RE = re.compile(
    r"^\W*(?:(?:請|请|幫我|帮我|幫忙|帮忙|麻煩你?|麻烦你?|先|直接|再|然後|然后)\s*)*([\u4e00-\u9fff]{1,2}(?<![這那哪每各某此其該该本])(?<![的了])(?![的])\s*)"
    + _CJK_CODE_TOKEN)


_BRIEF_LABEL_RE = re.compile(
    r"^\W*(?:context|background|task|goal|objective|output|format|report|return|rules?|constraints?|notes?|steps?|scope|"
    r"deliverables?|answer|expected|requirements?|instructions?|focus|summary|example|e\.g|options?|question|todo|"
    r"important|reminder|warning|tip|hint|format|reply|response)\b", re.IGNORECASE)
_TOPIC_FRAME_COMMA_RE = re.compile(r"^\W*(?:in|for|on|within|inside|regarding|about|at|under|across)\s+\S", re.IGNORECASE)
_CJK_TAIL_ADVERB_RE = re.compile(r"^(?:(?:順手|顺手|順便|顺便|順道|顺道|麻煩你?|麻烦你?|請|请|幫忙|帮忙|直接|再|然後|然后|並|并|先|也)\s*)+")


_ASSIGN_VALUE = r"(?:\d[\w.%-]*|['\"`][^'\"`]+['\"`]|true|false|none|null|[A-Z][A-Z0-9_]{2,}|\S*[/_.]\S*)"
_ASSIGN_SUBJECT = r"(?:the\s+)?(?:`[^`]+`|[\w./-]+)(?:\s+(?:in|of|for|on)\s+(?:the\s+)?[\w./`-]+)?"
# "Version in pyproject.toml goes to 2.3", "SESSION_TTL to 3600 in settings.py": an elliptical assignment.
_ASSIGN_EN_RE = re.compile(
    r"^\W*" + _ASSIGN_SUBJECT + r"\s+(?:goes\s+to|go\s+to|becomes?|should\s+become|->|=>|:=|=)\s+" + _ASSIGN_VALUE
    + r"|^\W*(?:`[^`]+`|[A-Z][A-Z0-9_]{2,}|\w*[_.]\w+)(?:\s+(?:in|of|for|on)\s+(?:the\s+)?[\w./`-]+)?\s+to\s+"
    + _ASSIGN_VALUE, re.IGNORECASE)


def _opening_clause_action(clause: str, question: bool = False, colon: bool = False) -> bool:
    """True when an English clause opens with an action: a force verb, an edit verb or a verb-like word + object."""
    clause = clause.strip()
    if not clause or _OPENING_READ_PHRASE_RE.match(clause):
        return False
    if question or _is_question_clause(clause, ""):
        return False
    if _ASSIGN_EN_RE.match(clause):
        return True
    if _OPENING_FORCE_RE.match(clause):
        return True
    if _opening_verb_action(clause, False, colon):
        return True
    m = _OPENING_RE.match(clause)
    return bool(m and m.group(1).lower() not in _OPENING_SKIP and m.group(1).lower() not in _OPENING_NOT_ACTION_LEAD
                and (m.group(1).islower() or m.group(1).istitle()) and _OPENING_OBJECT_RE.match(m.group(2)))


_CJK_MODIFIER_HEADS = {"含", "包含", "附", "附上", "帶", "带", "附帶", "附带", "需要", "想要", "要", "得", "應該", "应该", "有"}
_CJK_INFO_NOUNS = (r"(?:那一行|那一段|那段|那行|那個|值|路徑|路径|清單|清单|列表|名稱|名称|定義|定义|簽名|签名|輸出|输出|"
                   r"擁有者|负责人|負責人|位置|內容|内容|版本號|版本号|預設值|默认值)")
_CJK_NEED_INFO_RE = re.compile(r"(?:需要|想要|要看|要知道|想知道)[^，。？?！!]{0,30}?" + _CJK_INFO_NOUNS + r"\s*[。]?\s*$")


def _cjk_clause_action(clause: str, after_separator: bool = False) -> bool:
    """True when a Chinese clause opens with a verb outside the read list (list verb, verb + code token, adverb + verb)."""
    clause = clause.strip()
    if not clause or _QUESTION_CJK_RE.search(clause) or re.search(r"[?？]", clause):
        return False
    if _CJK_OPENING_RE.match(clause):
        return True
    vo = _CJK_VERB_OBJECT_RE.match(clause)
    if _CJK_NEED_INFO_RE.search(clause):
        return False
    if vo and not _POLITE_CJK_READ_RE.match(vo.group(1)) and vo.group(1)[0] not in _POLITE_CJK_NOT_VERB \
            and not re.search(r"[我你他她它給给]", vo.group(1)) and vo.group(1).strip() not in _CJK_MODIFIER_HEADS:
        return True
    if after_separator:
        lead = _CJK_TAIL_ADVERB_RE.match(clause)
        rest = clause[lead.end():] if lead else clause
        if lead and re.search(r"順|顺|麻煩|麻烦|直接|幫忙|帮忙", lead.group(0)) and rest and "\u4e00" <= rest[0] <= "\u9fff" \
                and rest[0] not in _POLITE_CJK_NOT_VERB and not _POLITE_CJK_READ_RE.match(rest) \
                and not re.search(r"[我你他她它給给]", rest[:2]):
            return True
    return False


def _action_sentence(prompt: str) -> Optional[str]:
    """Any sentence (not only the first) that opens with a non-read verb + it/the/a/this/that or a code token.

    "Reproduce it with act locally and tell me why." is an action although it ends in a request to report.
    """
    if len(prompt) > 600:
        return None  # long briefs: their sentences are background and list items (see _scout_trailing_imperative)
    for sentence, term in _split_sentences(prompt):
        sentence = re.sub(r"^\W*(?:(?:and|then|also|please|just|now)\s+)+", "", sentence, flags=re.IGNORECASE)
        if (_TRAILING_READONLY_RE.match(sentence) or _WORKER_READ_START_RE.match(sentence)
                or re.match(r"\W*(?:without|point|do not|don't|dont|never|avoid|only)\b", sentence, re.IGNORECASE)):
            continue
        if _opening_clause_action(sentence, term == "?"):
            return sentence[:60]
    return None


def _topic_prefix(prefix: str, cjk: bool) -> bool:
    """A part before a separator that names a topic (a noun phrase, a path) and carries no verb."""
    prefix = prefix.strip()
    if not prefix or len(prefix) > 100 or re.search(r"[?？]", prefix):
        return False
    if cjk:
        return bool(len(prefix) <= 40 and re.search(_CJK_CODE_TOKEN, prefix) and not re.search(r"(?:清單|清单|列表|名單|名单)$", prefix) and not _QUESTION_CJK_RE.search(prefix)
                    and not _POLITE_CJK_READ_RE.search(prefix) and not CJK_MUTATION_RE.search(prefix))
    if _BRIEF_LABEL_RE.match(prefix) or _opening_clause_action(prefix):
        return False
    first = re.match(r"\W*([A-Za-z][\w-]*)", prefix)
    return not (first and first.group(1).lower() in (_SEQUENCED_READ_WORDS - _OPENING_NOT_ACTION_LEAD) | {"run", "execute", "open"})


def _scout_opening_action(prompt: str) -> Optional[str]:
    """First clause of a scout prompt that opens with a verb outside the read list ("Make the timeout 30 seconds").

    The clause after a topic separator counts too: "The retry helper in net/retry.py: cap the backoff at 30s.",
    "utils/text.py 的 slugify，順手處理一下中文。". A tail that is a question stays a lookup.
    """
    text = prompt.strip()
    cjk_clause = re.split(r"[。？！；\n]", text, 1)[0]
    if _cjk_clause_action(cjk_clause):
        return cjk_clause[:60]
    sep = re.search(r"[：:，,]", cjk_clause) if len(text) <= 300 else None
    if sep and re.search(r"[\u4e00-\u9fff]", cjk_clause) and not (sep.group(0) in ":," and cjk_clause[sep.end():sep.end() + 1] not in (" ", "")):
        if _topic_prefix(cjk_clause[:sep.start()], True) and _cjk_clause_action(cjk_clause[sep.end():], True):
            return cjk_clause[sep.end():][:60]
    clause = re.split(r"[.?!;\n]|:\s", text, 1)[0]
    if _OPENING_READ_PHRASE_RE.match(clause):
        return None
    if _OPENING_FORCE_RE.match(clause) and not _is_question_clause(clause, ""):
        return clause[:60]
    sent = _OPENING_SENTENCE_RE.match(text)
    full = sent.group(1) if sent else clause
    restricted = _OPENING_RESTRICTION_RE.match(full)
    if restricted:
        full = full[restricted.end():]
    if full and not _OPENING_READ_PHRASE_RE.match(full):
        question = bool(sent and sent.group(2) == "?") or _is_question_clause(full, "")
        if _opening_verb_action(full, question, bool(sent and sent.group(3))):
            return full[:60]
    if _opening_clause_action(clause, bool(sent and sent.group(2) == "?")):
        return clause[:60]
    # A tail after a topic separator: "<topic>: <action>" or "In <path>, <action>".
    if len(text) > 300:
        return None  # separators in a long brief open sections, not a one-line "<topic>: <action>"
    if sent and sent.group(3) and _topic_prefix(sent.group(1), False):
        tail = _OPENING_SENTENCE_RE.match(text[sent.end():].lstrip())
        if tail and _opening_clause_action(tail.group(1), tail.group(2) == "?"):
            return tail.group(1)[:60]
    comma = re.match(r"([^,]{1,100}),\s+(.+)", full, re.DOTALL)
    if comma and _TOPIC_FRAME_COMMA_RE.match(comma.group(1)) and _topic_prefix(comma.group(1), False):
        if _opening_clause_action(comma.group(2), bool(sent and sent.group(2) == "?")):
            return comma.group(2)[:60]
    return None


# Programs a scout's Bash cannot run, named after a run verb: "Run `npm run build`", "Try docker compose up", 用 terraform apply.
_RUN_PROGRAMS = (r"pip3?|npm|npx|pnpm|yarn|uv|poetry|cargo|docker|docker-compose|kubectl|curl|wget|pytest|tox|terraform|helm|"
                 r"gh|aws|gcloud|python3?|node|brew|apt(?:-get)?|systemctl|ansible|gradle|mvn|bundle|gem|composer|dotnet|"
                 r"make|alembic|rake|flask|django-admin|manage\.py|go|sh|bash|zsh|sudo|\./\S+|\S+\.sh|\S+\.py|psql|mysql|redis-cli")
_RUN_COMMAND_RE = re.compile(
    r"(?:\b(?:run|execute|exec|try|invoke|launch)\b|跑一下|跑|執行|执行|運行|运行|(?<!不)(?<!不需)(?<!不需要)(?<!不必)(?<!無須)(?<!无须)(?<!別)(?<!别)用)"
    r"\s*[:：]?\s*`?\$?\s*(?:" + _RUN_PROGRAMS + r")(?![\w-])",
    re.IGNORECASE,
)


def find_run_command(text: str) -> Optional[str]:
    """A run verb followed by a program a read-only scout cannot run ("Run `npm run build`", 用 terraform apply 套一次)."""
    m = _RUN_COMMAND_RE.search(_blank_run_objects(text))
    return m.group(0).strip() if m else None


# Positive read-only evidence for a scout-typed prompt (rule "scout-unproven" when none is found).
_DESIRE_EN_RE = re.compile(
    r"(?<!\bno\s)(?<!n't\s)\b(?:should|must|needs?(?:\s+to)?|wants?|wanted|let'?s|we'?ll|we\s+will|gotta|has\s+to|have\s+to|ought|"
    r"would\s+like|i'?d\s+like|going\s+to|supposed\s+to)\b", re.IGNORECASE)
_DESIRE_CJK_RE = re.compile(r"應該|应该|要有|需要|不要再|別再|别再|才對|才对|得(?=[\u4e00-\u9fff]{1,2}[^的])|(?:^|[，,。\s])來\s*[A-Za-z\u4e00-\u9fff]|要(?![不求])")
# "We need to know whether...", "Let's start by listing...", "I want to understand how...": a desire for information.
_DESIRE_READ_RE = re.compile(
    r"\b(?:want|wants|need|needs|wish|plan|would\s+like|i'?d\s+like|let'?s|let\s+us|trying|going)\s+(?:to\s+|us\s+to\s+)?"
    r"(?:(?:start|begin|first)\s+(?:by\s+|with\s+)?)?(?:know|understand|see|find|check|list|read|look|learn|figure|confirm|verify|review|"
    r"listing|reading|checking|looking|reviewing|finding|grepping|searching|locating|inspecting|tracing|"
    r"get\s+a\s+(?:list|view|picture)|get\s+(?:an?\s+)?overview|make\s+sure)\b", re.IGNORECASE)
_READ_OPEN_CJK_RE = re.compile(
    r"^\W*(?:(?:請|请|幫我|帮我|幫忙|帮忙|先|只要|麻煩你?|麻烦你?|直接|再|然後|然后|想|想要)\s*)*"
    r"(?:看|查|找|讀|读|列|確認|确认|檢查|检查|比對|比对|統計|统计|告訴|告诉|說明|说明|解釋|解释|回報|回报|了解|瞭解|搜尋|搜寻|數|数|"
    r"比較|比较|摘要|盤點|盘点|釐清|厘清|弄清楚|搞清楚)")
_REQUEST_MODAL_RE = re.compile(
    r"^\W*(?:(?:and|then|also|so|but)\s+)?(?:can|could|would|will)\s+(?:you|we)\s+(?!(?:please\s+)?(?:just\s+)?"
    r"(?:tell|show|list|find|check|read|look|explain|describe|give|point|identify|name|summari[sz]e|report|answer|confirm|"
    r"verify|see|let\s+me\s+know|help\s+me\s+(?:find|understand|see|locate))\b)", re.IGNORECASE)
_NP_LOOKUP_EN_RE = re.compile(
    r"^\W*(?:[\w'./-]+\s+){0,5}?(?:of|in|for|from|touching|used\s+by|defined\s+in|across|under|older\s+than|"
    r"containing|matching|referencing|covering|covers|calling|importing|using|raising|returning|handling|mentioning|"
    r"that|which)\s+\S", re.IGNORECASE)
_NP_LOOKUP_CJK_RE = re.compile(r"(?:清單|清单|列表|名單|名单|位置|路徑|路径|定義|定义|用法)\s*$")
_RETURN_RE = re.compile(
    r"\b(?:tell|show|give|point(?:ed)?|send|get)\s+me\b|找出|查出|列出|貼出|贴出|印出|挑出|(?:給我|给我|告訴我|告诉我)", re.IGNORECASE)
_NP_LEAD = {"last", "latest", "recent", "top", "newest", "oldest", "previous", "unused", "stale", "dead", "old", "new",
            "current", "existing", "pending", "unresolved", "failing", "broken", "few", "many", "several", "total", "first",
            "all", "any", "every", "each", "other", "open", "closed", "open"}
_RUN_READ_RE = re.compile(r"^\W*(?:please\s+)?(?:run|execute|use|try)\s+(.+?)(?:\s+(?:and|then)\s+.*)?$", re.IGNORECASE)
_DESIRE_INFO_RE = re.compile(
    r"\b(?:need|needs|want|wants|looking\s+for|require|requires)\s+(?:to\s+see\s+|to\s+know\s+)?(?:the|a|an|its|their)?\s*(?:[\w-]+\s+){0,2}?"
    r"(?:lines?|values?|paths?|names?|lists?|signatures?|outputs?|definitions?|owners?|locations?|snippets?|contents?)\b",
    re.IGNORECASE)
_CODE_WORD = r"(?:`[^`]+`|[A-Z][A-Z0-9_]{2,}|[A-Za-z_]\w*[_.][\w.]*|[\w.-]*/[\w./-]*|[a-z]+[A-Z]\w*)"
_VALUE_TOKEN_RE = re.compile(r"(?<![\w./-])\d+(?:\.\d+)*(?![\w./-])|['\"][^'\"]+['\"]|\b(?:true|false|none|null)\b", re.IGNORECASE)
_RELATIVE_VERBS = ("reads?|uses?|calls?|imports?|sets?|defines?|loads?|returns?|raises?|writes?|exports?|requires?|"
                   "touche[sd]|references?|accepts?|expects?|emits?|logs?|checks?|handles?|parses?|takes?|registers?|exposes?|supports?|consumes?|produces?")
_TERSE_EN_RE = re.compile(
    r"^\W*(?:" + _CODE_WORD + r"|(?:the|all|every|each|any)\s+)", re.IGNORECASE)
_TERSE_RELATIVE_RE = re.compile(
    r"^\W*(?:the|all|every|each|any)\s+[\w\s./-]{1,40}?\s+(?:" + _RELATIVE_VERBS + r")\W*$", re.IGNORECASE)
_TERSE_CJK_RE = re.compile(r"^\W*" + _CODE_WORD + r"\s*(?:裡的|里的|裏的|底下的|中的|內的|内的|下的|的)\s*\S")
_CJK_EDIT_HINT_RE = re.compile(r"[改換换調调刪删補补修加]|設為|設成|设为|设成|應該|应该|要有|需要")
_FINITE_RE = re.compile(
    r"\b(?:is|are|was|were|will|shall|has|have|had|does|do|did|goes|go|becomes?|gets?|turns?|stays?|remains?)\b|->|=>|\s=\s",
    re.IGNORECASE)


def _split_sentences(text: str) -> List[Tuple[str, str]]:
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", "\u2024"), text)
    parts = re.findall(r"([^.?!;。？！；\n]*(?:\.(?=[\w/])[^.?!;。？！；\n]*)*)([.?!;。？！；]?)", protected)
    return [(a.replace("\u2024", ".").strip(), b) for a, b in parts if a.strip()]


def _scout_read_evidence(text: str, prompt: str) -> Optional[str]:
    """Why a scout-typed prompt that no action rule caught is still plainly read-only, or None.

    Evidence: a question; a read-verb opening (also after "I want to understand..."); a lite or lookup signal in a
    sentence with no modal or desire marker; a read-only declaration or command; a verbless noun-phrase lookup;
    every clause read-only. Modal and desire statements ("X should be 60", "we need a flag", 應該/要有) and plain
    declaratives about a target value are not evidence.
    """
    subject = prompt
    if len(prompt) > 600:
        paragraphs = re.split(r"\n[ \t]*\n", prompt.strip())
        subject = paragraphs[-1].strip()
    if READ_ONLY_RE.search(text):
        return "read-only declaration"
    if is_read_only_command(prompt):
        return "read-only command"
    sentences = _split_sentences(subject)
    any_request = False
    if len(sentences) == 1 and re.match(r"\W*(?:context|background|note|fyi|for context)\s*[:：]", sentences[0][0], re.IGNORECASE):
        return "background statement"
    for i, (sentence, term) in enumerate(sentences):
        desire = bool(_DESIRE_EN_RE.search(sentence) or _DESIRE_CJK_RE.search(sentence))
        request = bool(_REQUEST_MODAL_RE.match(sentence))
        any_request = any_request or request or desire
        if _DESIRE_READ_RE.search(sentence):
            return "desire to read"
        if not request and (term in ("?", "？") or any(_is_question_clause(part, "") for part in re.split(r"[:：]\s*", sentence))):
            return "question"
        run_read = _RUN_READ_RE.match(sentence)
        if run_read and is_read_only_command(run_read.group(1).strip().strip("`")):
            return "read-only command"
        if _WORKER_READ_START_RE.match(sentence) or _READ_OPEN_CJK_RE.match(sentence) or _opens_with_read_verb(sentence):
            return "read-verb opening"
        if not desire and not request and (find_lite(sentence) or LOOKUP_SIGNAL_RE.search(sentence) or _RETURN_RE.search(sentence)):
            return "lookup signal"
        if _DESIRE_INFO_RE.search(sentence) or _CJK_NEED_INFO_RE.search(sentence):
            return "need for information"
        if i == 0 and not desire and not request and not _FINITE_RE.search(sentence):
            clause = re.split(r"[,，:：]", sentence, 1)[0]
            has_value = bool(_VALUE_TOKEN_RE.search(_CODE_SPAN_RE.sub(" ", clause)))
            if not has_value and not _CJK_EDIT_HINT_RE.search(clause) and (
                    _TERSE_RELATIVE_RE.match(clause) or _TERSE_CJK_RE.match(clause)
                    or (_TERSE_EN_RE.match(clause) and len(clause.split()) <= 6 and not _OPENING_SKIP & {clause.split()[0].lower()})):
                return "terse code-token lookup"
            lead = re.match(r"\W*([A-Za-z]+)", clause)
            if (_NP_LOOKUP_EN_RE.match(clause) or _NP_LOOKUP_CJK_RE.search(clause)
                    or (lead and lead.group(1).lower() in _NP_LEAD)):
                return "noun-phrase lookup"
    if not any_request and _all_clauses_read_only(subject):
        return "all clauses read-only"
    return None


def _scout_trailing_imperative(text: str, prompt: str) -> Optional[str]:
    """Trailing imperative for a scout-typed prompt, with stricter guards than for untyped ones.

    Scout prompts are often long structured briefs whose later list items are noun phrases, so the clause
    must open with a verb, and a read-only declaration after it rules the check out. Past 600 chars only the
    final paragraph (at most 400 chars, not a list) is examined.
    """
    subject = prompt
    if len(prompt) > 600:
        # A long brief: only its closing paragraph can carry the ask, and only when it is prose, not a list.
        paragraphs = re.split(r"\n[ \t]*\n", prompt.strip())
        subject = paragraphs[-1].strip()
        if len(paragraphs) < 2 or len(subject) > 400 or any(_LIST_LINE_RE.match(ln) for ln in subject.splitlines()):
            return None
    clause = find_trailing_imperative(subject)
    if not clause or not (_SCOUT_IMPERATIVE_START_RE.match(clause) or _sequenced_action(clause)):
        return None
    at = max(text.find(clause), 0)
    if any(m.start() >= at for m in READ_ONLY_RE.finditer(text)):
        return None  # a read-only declaration at or after the clause has the last word
    return clause


# A verb outside the word lists that opens a clause and takes an object ("...and raise it to 30s", "make it faster").
_WORKER_ACTION_EN_RE = re.compile(
    r"(?:^|[.;!?\n]\s*|\b(?:and|then|also|please)\s+(?:(?:then|also|please)\s+)*)"
    r"([a-z][\w-]*)\s+(?:it|them|that|this|those|these|its|the|a|an|to)\b",
    re.IGNORECASE,
)
_WORKER_READ_WORDS = {
    "tell", "show", "list", "give", "report", "print", "explain", "return", "say", "answer", "name", "describe",
    "summarize", "summarise", "count", "find", "check", "read", "look", "see", "identify", "point", "include",
    "note", "mention", "paste", "quote", "cite", "display", "let", "is", "are", "was", "were", "does", "do", "did",
    "has", "have", "can", "could", "which", "what", "where", "who", "how", "why", "whether", "if", "the", "a", "an",
    "to", "of", "in", "on", "for", "with", "it", "this", "that", "these", "those", "there", "i", "we", "you", "so",
    "but", "or", "not", "no", "yes", "just", "only", "please", "also", "then", "and",
}
_WORKER_ACTION_CJK_RE = re.compile(
    r"(?:並|并|然後|然后|再|順便|顺便|接著|接着)(?!\s*(?:告訴|告诉|說明|说明|回報|回报|列|查|看|找|確認|确认|比對|比对|統計|统计|整理|報告|报告|回答))"
)


# Research that spans the codebase is a worker job even when it only reads.
_WORKER_BROAD_RE = re.compile(r"\bacross\b|\bend[- ]to[- ]end\b|\bmap out\b|\bflows? (?:from|through)\b|\btrace\b", re.IGNORECASE)


def _opens_with_read_verb(prompt: str) -> bool:
    return bool(_TRAILING_READONLY_RE.search(re.split(r"[。.;；，,\n]", prompt.strip(), 1)[0].strip()))


# Commands the scout's Bash cannot run: any CLI tool outside the read-only set, in a code span, a "$ " line or bare.
_CLI_WORDS = ("pip3?|npm|pnpm|yarn|uv|poetry|cargo|docker|kubectl|curl|wget|pytest|tox|terraform|helm|gh|aws|gcloud|"
              "python3?|node|npx|brew|apt(?:-get)?|systemctl|ansible|gradle|mvn|bundle|gem|composer|dotnet|"
              "go (?:build|test|run|mod|vet|get|install|env|list)|make\\s+(?:-\\w+|test|build|lint|install|clean|all|check|deploy|run)")
_CLI_BARE_RE = re.compile(r"(?<![\w./-])(?:" + _CLI_WORDS + r")\s+\S", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_SHELL_LINE_RE = re.compile(r"(?m)^\s*[$>]\s+(\S.*)$")


def _names_non_readonly_command(prompt: str) -> bool:
    for span in _CODE_SPAN_RE.findall(prompt) + _SHELL_LINE_RE.findall(prompt):
        span = span.strip()
        if re.match(r"(?:" + _CLI_WORDS + r")\b", span, re.IGNORECASE) and not is_read_only_command(span):
            return True
    return bool(_CLI_BARE_RE.search(_CODE_SPAN_RE.sub(" ", prompt)))


_WORKER_READ_START_RE = re.compile(
    r"^\W*(?:(?:please|just|also|and|then|first)\s+)*(?:find|list|show|tell|give|print|read|look|check|explain|describe|"
    r"summari[sz]e|count|identify|name|display|locate|grep|search|cat|report|answer|return|point|open|view)\b|"
    r"^\W*(?:用\s*\S+\s*)?(?:列出|找出|查|看|讀|读|告訴|告诉|說明|说明|解釋|解释|比對|比对|確認|确认|檢查|检查|統計|统计|搜尋|搜寻)")


def _all_clauses_read_only(prompt: str) -> bool:
    """True when every non-question clause is a read-only instruction, courtesy or reference.

    "I need you to wire the retry wrapper" opens with a subject and is no read-only clause here.
    """
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", "\u2024"), prompt)
    pos, clauses = 0, []
    for m in _SENTENCE_SPLIT_RE.finditer(protected):
        before = protected[:m.start()]
        if before.count("(") > before.count(")"):
            continue
        clauses.append((protected[pos:m.start()], m.group(0)))
        pos = m.end()
    clauses.append((protected[pos:], ""))
    for clause, terminator in clauses:
        clause = clause.strip()
        if not clause or not re.search(r"[A-Za-z]{2}|[\u4e00-\u9fff]", clause):
            continue
        if _is_question_clause(clause, terminator) or _COURTESY_RE.match(clause) or _REFERENCE_RE.match(clause):
            continue
        if not _RO_COMPOUND_ACTION_RE.search(clause) and (
                _TRAILING_READONLY_RE.search(clause) or _READONLY_HINT_RE.search(clause)
                or READ_ONLY_RE.search(clause) or _WORKER_READ_START_RE.match(clause)):
            continue
        return False
    return True


def _worker_lookup(text: str, prompt: str, workspace: str, requested: str, lite: Optional[str]) -> bool:
    """True when a flash-worker typed call is a plain read-only lookup the scout can answer.

    Conservative: a short prompt, no sign of an edit, a run, a shell write, web access, heavy research or a
    trailing action clause, and positive evidence of a lookup that asks for no action on what it names.
    """
    if len(prompt) > 600 or workspace in ("branch", "share") or requested not in ("", "inherit", "flash_lite"):
        return False  # an explicit stronger model (flash, sonnet, ...) is the caller's choice
    if (find_mutation(text, False) or find_heavy_ex(text, lite is not None, False)[0] or find_shell_mutation(text)
            or find_polite_request(text) or find_run_task(text) or find_run_directive(text)
            or EXECUTION_RE.search(_blank_run_objects(text)) or needs_web(text) or find_trailing_imperative(prompt)
            or _action_sentence(prompt)):
        return False
    if _WORKER_BROAD_RE.search(prompt) or _WORKER_ACTION_CJK_RE.search(prompt) or any(
            m.group(1).lower() not in _WORKER_READ_WORDS for m in _WORKER_ACTION_EN_RE.finditer(prompt)):
        return False
    read_command = is_read_only_command(prompt)
    if not read_command and (_names_non_readonly_command(prompt) or not _all_clauses_read_only(prompt)):
        return False
    if not (lite or LOOKUP_SIGNAL_RE.search(text) or read_command):
        return False
    stripped = prompt.strip()
    question = _is_question_clause(stripped, "?" if stripped[-1:] in ("?", "？") else "")
    return bool(_is_readonly_question(text) or READ_ONLY_RE.search(text) or read_command or question
                or _opens_with_read_verb(prompt))


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
      1. flash-worker type                      -> flash, except a plain read-only lookup (short, no edit, run,
         web, heavy or trailing-action signal, positive lookup evidence): flash_lite with the scout type
         (worker-lookup; AGY_MITOSIS_WORKER_DOWNGRADE=0 turns that off; a judge may veto it)
      2. mutation verbs in prompt               -> flash  (scout type is upgraded to worker); includes the Chinese
         disposal construction (把 X 關掉/搬到 Y) and transitive X掉; a verb inside a 要不要/是否 frame or a
         why-question clause is a topic
         Chinese code-edit verbs (統一 攤平 封裝 ...) and the 替/給/幫/為 + object + non-read-verb frame count too;
         a test runner named in a read-only configuration question (not its pass/fail state) is a topic
      2b. polite action request ("would you mind bumping...", 請/幫我 + any verb that is not a read verb) -> flash
      2c. run directive left after "command to run X" phrases are blanked, scout type -> flash (upgraded)
      3. flash-lite-scout type                  -> flash_lite, unless it needs web access (scout-web), ends
         in an action clause after a question (trailing-imperative; for a prompt over 600 chars only the final
         paragraph counts) or still asks for a run (run-directive);
         an opening edit verb (pin, swap, hoist...) with a plain object, or an unlisted verb-like word with a plain
         object and a concrete target (noun-phrase lookups such as "Callers of X" excluded), is scout-imperative;
         the clause after a topic separator ("<path>: <action>", "<path> 的 X，順手 <action>") is checked the same way;
         a yes/no or "check whether" question about a tool's state (passes, happy, builds) needs a run (run-directive);
         a scout type that has no positive read-only evidence (question, read-verb opening, lookup signal outside a
         modal or desire sentence, read-only declaration or command, noun-phrase or terse code-token lookup with no value or finite verb, "need the <line/value/path>", all clauses
         read-only; elliptical assignments such as "X goes to 2.3" are actions) is
         upgraded (scout-unproven, confidence 0.6, a judge may send it back);
         a scout type that declares read-only with the last word keeps the scout (scout-read-only)
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
        if _get_env("AGY_MITOSIS_WORKER_DOWNGRADE", default="1") != "0" and _worker_lookup(
                text, prompt, workspace, requested, lite):
            scout_type = f"{prefix}{SCOUT_TYPE}" if prefix else SCOUT_TYPE
            return done(_rule_decision("flash_lite", scout_type, "worker-lookup",
                                       "flash-worker type on a plain read-only lookup; downgraded to the scout"))
        return done(_rule_decision("flash", None, "worker-type", "flash-worker type"))
    worker_upgrade = f"{prefix}{WORKER_TYPE}" if prefix else WORKER_TYPE
    if mutation:
        if (bare_type == SCOUT_TYPE and _read_only_has_last_word(text, "mutation") and not needs_web(text)
                and not find_run_directive(text) and not find_run_command(text) and not find_run_task(text) and not EXECUTION_RE.search(_blank_run_objects(text))
                and not find_shell_mutation(text) and not find_polite_request(text)):
            return done(_rule_decision("flash_lite", None, "scout-read-only",
                                       f"scout declares read-only; '{mutation}' is the topic"))
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
        if needs_web(text):
            return done(_rule_decision("flash", worker_upgrade, "scout-web",
                                       "scout has no web access; upgraded to flash-worker"))
        opening = _scout_opening_action(prompt)
        if opening:
            return done(_rule_decision("flash", worker_upgrade, "scout-imperative",
                                       f"opening clause '{opening}' is an action; upgraded to flash-worker"))
        any_action = _action_sentence(prompt)
        if any_action:
            return done(_rule_decision("flash", worker_upgrade, "scout-imperative",
                                       f"sentence '{any_action}' is an action; upgraded to flash-worker"))
        trailing = _scout_trailing_imperative(text, prompt)
        if trailing:
            return done(_rule_decision("flash", worker_upgrade, "trailing-imperative",
                                       f"action clause '{trailing}' follows a question; upgraded to flash-worker"))
        directive = find_run_directive(text) or find_run_command(text) or _runner_verdict_request(text)
        if directive:
            return done(_rule_decision("flash", worker_upgrade, "run-directive",
                                       f"run request '{directive}' on read-only scout; upgraded to flash-worker"))
        evidence = _scout_read_evidence(text, prompt)
        if evidence is None:
            return done(_rule_decision("flash", worker_upgrade, "scout-unproven",
                                       "scout type without positive read-only evidence; upgraded to flash-worker"))
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
    if DOCS_LOOKUP_RE.search(_blank_local_docs(text)):
        return done(_rule_decision("flash", None, "docs-lookup", "docs or web lookup needs web access"))
    trailing = find_trailing_imperative(prompt) or _action_sentence(prompt)
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
