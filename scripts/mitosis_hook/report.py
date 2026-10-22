"""Subagent REPORT FORMAT handling."""

import re
from typing import Optional

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


REPORT_BLOCK_REASON = _GEN.REPORT_BLOCK_REASON
