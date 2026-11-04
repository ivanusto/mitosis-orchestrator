"""Did the subagent really run on the pinned model? Read it from the transcript the CLI wrote.

The verdict is three-state: True (every recorded model matches), False (at least one does not) and
None (no evidence). None must never be reported as a pass.
"""

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

_TAIL_BYTES = 1024 * 1024
_SYNTHETIC = "<synthetic>"  # Claude Code's marker for messages no model produced


def _tail_records(path: Any) -> Iterator[Dict[str, Any]]:
    """JSON objects from the last _TAIL_BYTES of a JSONL file, oldest first. Empty on any problem."""
    if not isinstance(path, str) or not path:
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            data = fh.read(_TAIL_BYTES)
    except OSError:
        return
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line was cut by the seek
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def claude_transcript_models(path: Any) -> List[str]:
    """Distinct models of the assistant messages in a Claude Code subagent transcript."""
    models: List[str] = []
    for record in _tail_records(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != _SYNTHETIC and model not in models:
            models.append(model)
    return models


def claude_handback_report(path: Any) -> str:
    """The report a subagent delivered through the SubagentHandback tool ("" when it did not).

    Under auto mode the final report is a tool call, so SubagentStop has no last_assistant_message.
    """
    report = ""
    for record in _tail_records(path):
        message = record.get("message") if record.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                tool_input = block.get("input")
                text = tool_input.get("message") if isinstance(tool_input, dict) else None
                if isinstance(text, str) and text.strip():
                    report = text
    return report


def codex_rollout_models(path: Any) -> List[str]:
    """The child's model from a Codex rollout: the LAST turn_context.

    A child rollout starts with a copy of the parent's context, so its first turn_context names the
    parent's model and only the last one is the child's own.
    """
    model = ""
    for record in _tail_records(path):
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        value = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(value, str) and value:
            model = value
    return [model] if model else []


def codex_rollout_turns(path: Any) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]], int]:
    """(first, last, count) of the turn_context records of a Codex rollout; each is (model, effort).

    A child rollout starts with a copy of its parent's turn_context, so with two or more records the
    first one is the parent's and the last is the child's. Effort is "" when the record has none
    (field name `effort`), and a record without a model is skipped.
    """
    first: Optional[Tuple[str, str]] = None
    last: Optional[Tuple[str, str]] = None
    count = 0
    for record in _tail_records(path):
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            continue
        effort = payload.get("effort")
        turn = (model, effort.strip().lower() if isinstance(effort, str) else "")
        if first is None:
            first = turn
        last = turn
        count += 1
    return first, last, count


def model_matches(expected: str, actual: str) -> bool:
    """A tier alias matches the full id it resolves to ("haiku" and "claude-haiku-5-5")."""
    want, got = expected.strip().lower(), actual.strip().lower()
    if not want or not got:
        return False
    return want == got or want in got


def pin_verdict(expected: Any, models: List[str]) -> Tuple[Optional[bool], str]:
    """Return (verdict, actual). A model the pin did not set or a transcript without models gives None."""
    want = str(expected or "").strip()
    if not want or want.lower() == "inherit" or not models:
        return None, ", ".join(models)
    return all(model_matches(want, m) for m in models), ", ".join(models)


def record_pin_verdict(record: Dict[str, Any], expected: Any, models: List[str]) -> Optional[bool]:
    """Store the verdict on a subagent record. Evidence already gathered is not replaced by None."""
    verdict, actual = pin_verdict(expected, models)
    if verdict is not None or "pin_verified" not in record:
        record["pin_verified"] = verdict
        if actual:
            record["actual_model"] = actual
    return verdict


def record_codex_pin_verdict(record: Dict[str, Any], expected_model: Any, expected_effort: Any,
                             turns: Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]], int]) -> Optional[bool]:
    """Codex verdict that tells a working pin from inheritance when parent and child share a model.

    An empty role inherits the parent's model and effort, so a matching model alone proves nothing
    when the primary runs the pinned model too. With a known parent (two or more turn_contexts) and
    an effort on both sides, a child that differs from the parent in (model, effort) was set by the
    pin; one equal to the parent and not to the expected effort inherited; one equal to both cannot
    be told apart (None). Effort never turns a model match into a mismatch except in that inherited
    case. Without a known parent or without effort the verdict is the model-only one.
    """
    first, last, count = turns
    models = [last[0]] if last else []
    verdict, _ = pin_verdict(expected_model, models)
    if verdict is not True or last is None:
        return record_pin_verdict(record, expected_model, models)
    note = ""
    if last[1]:
        record["actual_effort"] = last[1]
    want_effort = str(expected_effort or "").strip().lower()
    if count >= 2 and first is not None and first[1] and last[1] and want_effort:
        if last != first:
            verdict = True
        elif last[1] != want_effort:
            verdict, note = False, "looks inherited: model and effort equal the parent's, effort is not the pinned %s" % want_effort
        else:
            verdict = None
    if verdict is not None or "pin_verified" not in record:
        record["pin_verified"] = verdict
        record["actual_model"] = last[0]
        if note:
            record["pin_note"] = note
        else:
            record.pop("pin_note", None)
    return verdict
