"""Raw hook payload capture for building test fixtures. Off unless MITOSIS_CAPTURE_PAYLOADS names a directory.

A capture holds prompts, paths and tool output exactly as the CLI sent them. It is raw material
for scripts/sanitize-payloads.py and must never be committed or shared as it is. The directory is
created 0700 and each file 0600. Nothing here may disturb the hook: every failure is swallowed.
"""

import copy
import json
import os
import stat
from typing import Any, Dict, Optional


def snapshot(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Copy the payload before a handler can change it. None when capture is off."""
    if not os.environ.get("MITOSIS_CAPTURE_PAYLOADS"):
        return None
    try:
        return copy.deepcopy(payload)
    except Exception:
        return None


def _directory() -> str:
    """The capture directory with ~ expanded, or "" when it is unset or relative (the hook's cwd is the workspace)."""
    raw = os.environ.get("MITOSIS_CAPTURE_PAYLOADS")
    if not raw:
        return ""
    directory = os.path.expanduser(raw)
    return directory if os.path.isabs(directory) else ""


def write(snap: Optional[Dict[str, Any]], mode: str, result: Any) -> None:
    """Append one JSON line per hook call; one file per process id would scatter a run, so use one file."""
    directory = _directory()
    if snap is None or not directory:
        return
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700, exist_ok=True)
            try:
                os.chmod(directory, 0o700)  # makedirs applies mode to the leaf only and through the umask
            except Exception:
                pass
        line = json.dumps({"mode": mode, "payload": snap, "result": result}, ensure_ascii=False) + "\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        for name in ("O_BINARY", "O_NOFOLLOW", "O_NONBLOCK"):
            flags |= getattr(os, name, 0)  # a FIFO would block the open; a symlink must not be followed
        fd = os.open(os.path.join(directory, "payloads.jsonl"), flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except Exception:
                    pass
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass
