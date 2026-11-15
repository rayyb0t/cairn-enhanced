"""start_repair_side: the repair side's housekeeping thread, started with the dispatcher (spec 8.3, At start).

DispatcherLoop.run calls start_repair_side once, through scheduler/loop.py's _start_repair_side, and only when
dispatch.yaml has a repair: section; without the section this module is never imported. start_repair_side starts
one daemon thread, repair-housekeeping, and returns at once. The thread, in this order:

1. reads the dispatcher token (repair/config.py; a reporter thread that needs it first reads it itself);
2. checks the reviewers' keys against the workers' keys (spec 8.3, Shared keys);
3. writes the repair agent's settings file (spec 8.3, The agent's settings file);
4. runs the copy housekeeping (spec 8.8, Removal) at once, and then every HOUSEKEEPING_INTERVAL_SECONDS for the
   dispatcher's lifetime.

Each step, and each housekeeping pass, runs inside its own guard: an Exception logs "repair shadow housekeeping
failed step=%s error=%s" once per process and step, with the exception's class name, and the thread goes on with
the next step or pass. A thread that cannot start logs the same line with step=start, and the dispatcher runs
without it. Nothing here raises into the scheduler.

Build step 6 adds the thread, its order and its guards. Steps 2, 3 and 4 are build step 7's (check_shared_keys,
write_agent_settings and remove_copies below): until then they do nothing, so the thread writes, removes and sends
nothing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair.client import RepairRunsClient
from cairn.dispatcher.repair.config import RepairState, load_state

LOG = logging.getLogger(__name__)

THREAD_NAME = "repair-housekeeping"
HOUSEKEEPING_INTERVAL_SECONDS = 60

# Waited on between passes. The dispatcher never sets it, so the thread lives as long as the process, as a daemon;
# tests set it to end the thread.
_STOP = threading.Event()

# The steps whose failure has been logged in this process: each is logged once.
_failed_steps: set[str] = set()
_failed_lock = threading.Lock()


def start_repair_side(config: DispatchConfig) -> None:
    """Start the housekeeping thread and return at once. Never raises."""
    try:
        threading.Thread(target=_housekeeping, args=(config,), name=THREAD_NAME, daemon=True).start()
    except Exception as exc:
        _log_failure("start", exc)


def _housekeeping(config: DispatchConfig) -> None:
    """The housekeeping thread: steps 1 to 3 once, then the copy housekeeping's passes."""
    state = _guarded("token", load_state, config)
    _guarded("shared_keys", check_shared_keys, config)
    _guarded("settings", write_agent_settings, config, state)
    client = RepairRunsClient(config.server)  # GET /projects and GET /projects/{id} only, without the token (8.6)
    try:
        while True:
            _guarded("removal", remove_copies, config, client)
            if _STOP.wait(HOUSEKEEPING_INTERVAL_SECONDS):
                return
    finally:
        client.close()


def check_shared_keys(config: DispatchConfig) -> None:
    """Step 2, spec 8.3 (Shared keys): one WARNING per reviewer whose REVIEW_API_KEY a worker also holds.

    Build step 7 fills this in; until then it checks nothing and logs nothing.
    """


def write_agent_settings(config: DispatchConfig, state: RepairState | None) -> None:
    """Step 3, spec 8.3 (The agent's settings file): <transcript_root>/agent-settings.json, written atomically.

    state is the token step's result (None if that step failed): a REVIEW_API_KEY equal to the dispatcher token is
    never written. Build step 7 fills this in; until then it writes nothing and creates no folder.
    """


def remove_copies(config: DispatchConfig, client: RepairRunsClient) -> None:
    """Step 4, one pass of spec 8.8's copy housekeeping (Removal), with the housekeeping thread's client.

    Build step 7 fills this in; until then a pass lists nothing, sends no request and removes nothing.
    """


def _guarded(step: str, function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except Exception as exc:
        _log_failure(step, exc)
        return None


def _log_failure(step: str, exc: Exception) -> None:
    with _failed_lock:
        first = step not in _failed_steps
        _failed_steps.add(step)
    if first:
        LOG.warning("repair shadow housekeeping failed step=%s error=%s", step, type(exc).__name__)
