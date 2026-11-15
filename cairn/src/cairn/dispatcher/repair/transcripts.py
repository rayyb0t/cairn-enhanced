"""The transcript copies of reported runs (spec 8.8), and the count of runs being reported per project.

A run's reporter thread (repair/shadow.py, spec 8.5) uses this module in three places: it joins its project's
count of runs being reported when it starts (step 1) and leaves it however it ends (step 9), and once its run is
registered it drives the run's CopySchedule while the run goes on (step 3) and for the final copy after the task
has returned (step 8). The copy housekeeping of the housekeeping thread (repair/housekeeping.py) never removes the
copy folder of a project whose count is above 0.

Build step 6 adds these interfaces. The count is complete. The copy itself is build step 7's (spec 8.8: the
schedule's ticks, locate, the container and local copies, the redaction, the writes and meta.json): until then a
CopySchedule has no tick and makes no copy, so nothing is written.
"""

from __future__ import annotations

import threading
from typing import Any

# The count of runs being reported in this process, per project id (spec 8.8, Removal). Reporter threads join and
# leave it under this lock, and the copy housekeeping holds the same lock while it checks the count and removes a
# project's folder, so that no reporter thread writes into a folder that is being removed. It is reentrant, so that
# a thread that holds it may also call reporting_count.
REPORTING_LOCK = threading.RLock()
_reporting: dict[str, int] = {}


def join_reporting(project_id: str) -> None:
    """A reporter thread of project_id has started (spec 8.5 step 1)."""
    with REPORTING_LOCK:
        _reporting[project_id] = _reporting.get(project_id, 0) + 1


def leave_reporting(project_id: str) -> None:
    """A reporter thread of project_id that joined has ended (spec 8.5 step 9)."""
    with REPORTING_LOCK:
        count = _reporting.get(project_id, 0) - 1
        if count > 0:
            _reporting[project_id] = count
        else:
            _reporting.pop(project_id, None)


def reporting_count(project_id: str) -> int:
    """How many reporter threads of project_id are running in this process."""
    with REPORTING_LOCK:
        return _reporting.get(project_id, 0)


class CopySchedule:
    """The transcript copies of one registered run (spec 8.8, Schedule), driven by its reporter thread.

    run is the run's ShadowRun, from which the copy reads what it needs (its ids, worker, execution, budget,
    container or work folder, prompt marker, session id once known, start time and cancellation); token is the
    dispatcher token, which the copy's redaction removes by exact value.

    Its methods handle their own failures (spec 8.8 records them in meta.json's status). Should one raise anyway,
    or the constructor, the reporter logs it once and stops the run's copies, and its requests go on.

    Build step 7 fills this in. Until then next_due() is None, so the reporter never calls tick(), and
    final_copy() makes no copy.
    """

    def __init__(self, run: Any, token: str) -> None:
        self.run = run
        self._token = token

    def next_due(self) -> float | None:
        """The time.monotonic() value at which the next tick is due, or None when no tick is left."""
        return None

    def tick(self) -> None:
        """One tick of the schedule, called once it is due: a copy, a checked-time rewrite of meta.json, or nothing."""
        return None

    def final_copy(self) -> None:
        """The final copy, after the task has returned and only if it was not cancelled (spec 8.5 step 8).

        The schedule itself checks the rest of step 8's condition: the run reached 70% of its budget and is reviewable.
        """
        return None
