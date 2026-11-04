"""Raw hook payload capture for building test fixtures. Off unless MITOSIS_CAPTURE_PAYLOADS names a directory.

A capture holds prompts, paths and tool output exactly as the CLI sent them. It is raw material
for scripts/sanitize-payloads.py and must never be committed or shared as it is. The directory is
created 0700 and each file 0600. Nothing here may disturb the hook: every failure is swallowed.
"""

import copy
import json
import os
from typing import Any, Dict, Optional


def snapshot(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Copy the payload before a handler can change it. None when capture is off."""
    if not os.environ.get("MITOSIS_CAPTURE_PAYLOADS"):
        return None
    try:
        return copy.deepcopy(payload)
    except Exception:
        return None


def write(snap: Optional[Dict[str, Any]], mode: str, result: Any) -> None:
    """Append one JSON line per hook call; one file per process id would scatter a run, so use one file."""
    directory = os.environ.get("MITOSIS_CAPTURE_PAYLOADS")
    if snap is None or not directory:
        return
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        line = json.dumps({"mode": mode, "payload": snap, "result": result}, ensure_ascii=False) + "\n"
        fd = os.open(
            os.path.join(directory, "payloads.jsonl"),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass
