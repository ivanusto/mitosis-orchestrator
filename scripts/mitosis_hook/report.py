"""Subagent REPORT FORMAT handling."""

import re
from typing import Any, Dict, Optional, Tuple

from .common import _GEN, _get_env


MAX_SUBAGENT_REPORT_CHARS = _GEN.REPORT_MAX_CHARS


MAX_REPORT_BULLET_LINES = _GEN.REPORT_MAX_BULLETS


REPORT_BULLET_RE = re.compile(r"^[-*]\s+\S+")


REPORT_LOG_URI_RE = re.compile(r"file://\S+\.log\b", re.IGNORECASE)


REPORT_FENCE_RE = re.compile(r"^\s*`{3,}[\w-]*\s*$")


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

# The four header lines in order, as the primary writes them itself under its own lead-in.
_HEADER_LINE = r"[ \t]*[-*`]*[ \t]*{}[*`]*:[^\n]*"
_REPORT_HEADERS_RE = re.compile(
    r"(?:^|\n)" + r"\n".join(_HEADER_LINE.format(h) for h in ("STATUS", "FILES_CHANGED", "VERIFICATION", "NOTES"))
    + r"(?:\n[ \t]*-[^\n]*)*"
)
# The sentence announcing that block ("End your reply with this report format:"). It must open with a
# format verb, so "Fix the bug and report in this format:" keeps its action.
_REPORT_LEAD_RE = re.compile(
    r"(?:^|(?<=[.!?。！？])\s*)\W*(?:(?:then|and|please|finally)\s+)*"
    r"(?:(?:end|finish|close|conclude|reply|respond|answer|return|format|structure|use|give)\b"
    r"[^.!?\n]*\b(?:report|reply|response|answer|format|headers?|message)\b[^.!?\n]*"
    r"|your\s+(?:final\s+)?(?:reply|response|report|answer|message)\b[^.!?\n]*"
    r"|(?:最後|最后|結尾|结尾|回覆|回复|回報|回报|請用|请用)[^。\n]*(?:格式|標頭|标题|回報|回报)[^。\n]*)"
    r"[:：]\s*\Z",
    re.IGNORECASE,
)


def strip_report_format(prompt: str) -> str:
    """Drop the report instructions before classification: they describe the answer, not the task.

    Covers a trailing REPORT FORMAT block and the four header lines under the primary's own lead-in.
    Text after the header lines is kept, so a task cannot hide behind them.
    """
    text = REPORT_FORMAT_TAIL_RE.sub("", prompt or "").strip()
    match = _REPORT_HEADERS_RE.search(text)
    if match:
        head = text[:match.start()].rstrip()
        lead = _REPORT_LEAD_RE.search(head.rsplit("\n", 1)[-1])
        if lead:
            head = head[:len(head) - len(head.rsplit("\n", 1)[-1]) + lead.start()].rstrip()
        text = (head + "\n" + text[match.end():]).strip()
    return text


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


REPORT_PROBLEM_CLASSES = (
    "empty", "too_long", "too_few_lines", "too_many_lines", "preamble", "status_line",
    "files_line", "verification_line", "notes_line", "bullet", "no_log_uri",
)

_HEADER_PROBLEM_CLASSES = ("status_line", "files_line", "verification_line", "notes_line")


def _report_problem(text: str, require_log_uri: bool = True) -> Optional[Tuple[str, str]]:
    """Check a report against the 4-header bounded format.

    Returns None when valid, else (class, message). The class is one of REPORT_PROBLEM_CLASSES
    and carries no report content, so it is safe to count.
    """
    stripped = "\n".join(l for l in (text or "").splitlines() if not REPORT_FENCE_RE.match(l)).strip()
    if not stripped:
        return "empty", "Subagent report is empty."
    if len(stripped) > MAX_SUBAGENT_REPORT_CHARS:
        return "too_long", (
            f"Subagent report length ({len(stripped)} chars) exceeds maximum "
            f"{MAX_SUBAGENT_REPORT_CHARS} chars."
        )
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    max_lines = 4 + MAX_REPORT_BULLET_LINES
    if len(lines) < 4:
        return "too_few_lines", (
            f"Expected at least 4 header lines (STATUS, FILES_CHANGED, VERIFICATION, NOTES), got {len(lines)}."
        )
    if len(lines) > max_lines:
        return "too_many_lines", (
            f"Expected at most 4 headers plus {MAX_REPORT_BULLET_LINES} bullet lines "
            f"({max_lines} lines total), got {len(lines)}."
        )

    expected_prefixes = REPORT_FORMAT_HEADERS
    for idx, (pattern, prefix) in enumerate(zip(REPORT_LINE_PATTERNS, expected_prefixes), start=1):
        if not pattern.match(lines[idx - 1]):
            # `NOTES:` may be bare when bullet lines follow it.
            if prefix == "NOTES:" and re.match(r"^NOTES:\s*$", lines[idx - 1], re.IGNORECASE) and len(lines) > 4:
                continue
            cls = _HEADER_PROBLEM_CLASSES[idx - 1]
            if idx == 1 and any(REPORT_LINE_PATTERNS[0].match(l) for l in lines[1:]):
                cls = "preamble"  # the STATUS line exists but something precedes it
            return cls, f"Line {idx} must match '{prefix} <value>'."

    bullet_lines = lines[4:]
    for b_idx, b_line in enumerate(bullet_lines, start=5):
        if not REPORT_BULLET_RE.match(b_line):
            return "bullet", f"Line {b_idx} after NOTES must be a compact bullet starting with '- ' or '* '."

    if require_log_uri:
        status_match = re.match(r"^STATUS:\s*(\w+)", lines[0], re.IGNORECASE)
        status_val = status_match.group(1).upper() if status_match else ""
        if status_val in {"BLOCKED", "FAILED"}:
            notes_body = lines[3].split(":", 1)[1].strip() if ":" in lines[3] else ""
            combined_notes = " ".join([notes_body] + [b[1:].strip() for b in bullet_lines]).strip()
            if not REPORT_LOG_URI_RE.search(combined_notes):
                return "no_log_uri", f"When STATUS is {status_val}, NOTES must include a 'file://.../*.log' scratch log URI."

    return None


def report_problem_class(text: str, require_log_uri: bool = True) -> Optional[str]:
    """The reason class of an invalid report, or None when it is valid."""
    found = _report_problem(text, require_log_uri)
    return found[0] if found else None


def store_report_problem(record: Dict[str, Any], found: Optional[Tuple[str, str]]) -> None:
    """Keep only the reason class of the latest report (never its content) on a spawn record.

    Heartbeat counts are per stop, so a report blocked once and then fixed counts twice.
    """
    if found:
        record["report_problem"] = found[0]
    else:
        record.pop("report_problem", None)


def validate_subagent_report(text: str, require_log_uri: bool = True) -> Optional[str]:
    """Validate that a subagent report strictly follows the 4-header bounded format.

    Headers required:
      STATUS: COMPLETED | BLOCKED | FAILED
      FILES_CHANGED: <comma-separated paths or NONE>
      VERIFICATION: <description of verification command and result>
      NOTES: <single sentence summary or up to 10 bullet lines (- or *)>

    Constraints:
      - Total length <= 1600 characters.
      - At most 10 bullet lines following NOTES:.
      - If STATUS is BLOCKED or FAILED, NOTES must include a file:// URI to a .log file,
        unless require_log_uri is False (scouts are read-only and cannot write a log).

    Returns None if valid, or an error description string if invalid.
    """
    found = _report_problem(text, require_log_uri)
    return found[1] if found else None


REPORT_BLOCK_REASON = _GEN.REPORT_BLOCK_REASON
