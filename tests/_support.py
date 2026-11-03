"""Shared test helpers: private temp dir for the whole run, state-lock release.

Import this first in every test module (it is imported for its side effect):

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _support  # noqa: E402

The hook writes per-session state to <system temp>/mitosis-orchestrator/<id>.
Without isolation that lands in the real temp dir, and on Windows TMPDIR is
ignored (TEMP/TMP apply), so every variable is set together.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import MutableMapping

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = str(ROOT / "scripts")
TEMP_VARS = ("TMPDIR", "TEMP", "TMP")


def set_temp_env(env: MutableMapping, path: str) -> None:
    """Point every temp-dir variable at path (TMPDIR for POSIX, TEMP/TMP for Windows)."""
    for key in TEMP_VARS:
        env[key] = str(path)
    if env is os.environ:
        tempfile.tempdir = None  # force tempfile.gettempdir() to re-read the environment


def release_state_locks() -> None:
    """Close lock fds that direct load_state() calls leave in _HELD_LOCKS.

    Call before TemporaryDirectory.cleanup(): Windows cannot delete an open file (WinError 32).
    """
    state = sys.modules.get("mitosis_hook.state")
    if state is not None:
        state._release_state_locks()


_PROCESS_TMP = tempfile.mkdtemp(prefix="mitosis-tests-")
set_temp_env(os.environ, _PROCESS_TMP)

# Keep the per-user cache (heartbeat-*.json) inside the run's temp dir. HOME/USERPROFILE stay
# real: a fake home under /tmp would count as system scratch space in the path guard tests.
# Tests that clear MITOSIS_* variables cannot undo this: none of these carry that prefix.
_FAKE_HOME = os.path.join(_PROCESS_TMP, "home")
os.makedirs(os.path.join(_FAKE_HOME, "AppData", "Local"), exist_ok=True)
os.makedirs(os.path.join(_FAKE_HOME, ".cache"), exist_ok=True)
os.environ["XDG_CACHE_HOME"] = os.path.join(_FAKE_HOME, ".cache")
os.environ["LOCALAPPDATA"] = os.path.join(_FAKE_HOME, "AppData", "Local")


def _cleanup_process_tmp() -> None:
    release_state_locks()
    shutil.rmtree(_PROCESS_TMP, ignore_errors=True)


atexit.register(_cleanup_process_tmp)
