"""Repair layer (shadow mode): the server side of a run's life.

The dispatcher registers each explore run of a switched-on project, the repair agent reviews it at most once in its
review window, and the dispatcher's /end, or failing that the sweep, ends it. Phase 2 records and never applies:
nothing here moves a deadline or changes how a run starts, runs or ends.

This module holds the constants, the clock, the window maths, liveness, the sweep, the not-reviewed rule, the
stamping of ended runs on their issues, the run views and the text cleaning. Only the repair endpoints call it,
each inside its own `immediate_transaction`, which logs what the transaction wrote only once it has committed (the
listing then reads its runs in a second, read-only transaction); no existing endpoint does. A run is deleted with
its intent through the foreign key of `repair_runs`, and its reviews with it, by today's project delete and reopen,
so the sweep has no orphan pass. Every read here joins a run with its intent and project, so a row whose intent is
gone (possible only after a delete run outside Cairn with foreign keys off) is never read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cairn.server import db
from cairn.server.models import IntentIssue
from cairn.server.services import get_intent_timeout, utcnow

if TYPE_CHECKING:
    from cairn.server.repair_models import ReviewRequest, RunEndRequest, RunRegisterRequest

LOG = logging.getLogger(__name__)

# A run, an intent or a project as read from the database (sqlite3.Row), or the same keys in a dict.
RowLike = Mapping[str, Any] | sqlite3.Row

# --------------------------------------------------------------------------- constants

# The kind of the intent issue that shows a review, or the server's not-reviewed record, on the intent's node.
REPAIR_REVIEW = "repair_review"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The review clock. Reported to the agent in GET /repair/runs and GET /repair/settings, so it hardcodes none.
REVIEW_OPEN_FRACTION = 0.75
REVIEW_CLOSE_FRACTION = 0.80
REVIEW_ACCEPT_MARGIN_SECONDS = 10
# A 60 s model call plus the accept margin must fit in the window's last 20%.
MIN_REVIEWABLE_WINDOW_SECONDS = 350
DIGEST_TIME_MARGIN_SECONDS = 5
# The CLI types whose runs have a session transcript to review (mock has none).
REVIEWABLE_CLI_TYPES: frozenset[str] = frozenset({"claudecode", "codex", "pi"})
# Used only to word extend decisions: nothing is extended in shadow mode.
SLICE_SECONDS = 600

# Liveness. Server-internal: reported nowhere.
LAPSE_MIN_SECONDS = 60
LAPSE_TIMEOUT_MULTIPLIER = 4
# How many changes of intent_timeout the sweeps' reads keep in memory (see _IntentTimeouts).
INTENT_TIMEOUT_CHANGES_KEPT = 32
# States in which an open run cannot lapse. Phase 4 adds {"detached", "stop_requested"}.
LAPSE_EXEMPT_STATES: frozenset[str] = frozenset()
# The states that the default GET /repair/runs listing shows. Every open run is "running" in Phase 2.
LISTED_STATES: frozenset[str] = frozenset({"running"})

# The shadow settings. Constants: no table holds them and no HTTP request can change them.
SHADOW = True
MAX_EXTENSIONS_PER_RUN = 0
MAX_EXTENDED_RUNS = 0
MAX_DETACHED_PER_PROJECT = 0
MAX_DETACHED_TOTAL = 0
MAX_DETACHED_SECONDS = 0

# The full review texts kept in repair_reviews. The request limits equal these, so nothing is cut silently.
REASON_MAX_CHARS = 2000
EVIDENCE_MAX_ITEMS = 10
EVIDENCE_MAX_CHARS = 500
NEXT_STEP_MAX_CHARS = 500
MODEL_MAX_CHARS = 100
DIGEST_MAX_CHARS = 65536
RAW_OUTPUT_MAX_CHARS = 16384
# The display copies in the repair_review issue, which every project read and heartbeat reply carries.
DISPLAY_REASON_MAX_CHARS = 400
DISPLAY_EVIDENCE_MAX_ITEMS = 4
DISPLAY_EVIDENCE_MAX_CHARS = 200
DISPLAY_NEXT_STEP_MAX_CHARS = 200
DISPLAY_MODEL_MAX_CHARS = 64

# The fields of the /end the dispatcher sends when the execute process exits (the other one carries the outcome).
EXIT_REPORT_FIELDS: frozenset[str] = frozenset({"timed_out", "cancelled", "returncode", "exited_seconds_ago"})


def review_rules() -> dict[str, Any]:
    """The review clock as GET /repair/runs (`review_rules`) and GET /repair/settings (`review`) report it."""
    return {
        "open_fraction": REVIEW_OPEN_FRACTION,
        "close_fraction": REVIEW_CLOSE_FRACTION,
        "accept_margin_seconds": REVIEW_ACCEPT_MARGIN_SECONDS,
        "min_window_seconds": MIN_REVIEWABLE_WINDOW_SECONDS,
        "digest_time_margin_seconds": DIGEST_TIME_MARGIN_SECONDS,
        "reviewable_cli_types": sorted(REVIEWABLE_CLI_TYPES),
    }


def settings_constants() -> dict[str, Any]:
    """The constant part of GET /repair/settings: shadow on, the slice, and every action limit at 0."""
    return {
        "shadow": SHADOW,
        "slice_seconds": SLICE_SECONDS,
        "max_extensions_per_run": MAX_EXTENSIONS_PER_RUN,
        "max_extended_runs": MAX_EXTENDED_RUNS,
        "max_detached_per_project": MAX_DETACHED_PER_PROJECT,
        "max_detached_total": MAX_DETACHED_TOTAL,
        "max_detached_seconds": MAX_DETACHED_SECONDS,
        "review": review_rules(),
    }


# --------------------------------------------------------------------------- clock


def parse_time(value: object) -> datetime | None:
    """A time as the server writes it (YYYY-MM-DDTHH:MM:SSZ), or None for NULL or anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime(TIME_FORMAT)


def _required_time(value: object) -> datetime:
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("a repair run holds a time that is not in the server's format")
    return parsed


def server_now() -> datetime:
    """The server's clock, services.utcnow (whole seconds): every repair time derives from it."""
    return _required_time(utcnow())


def seconds_since(value: object, now: datetime) -> float:
    """Seconds from a stored time to now; infinite when the time is NULL or unreadable."""
    then = parse_time(value)
    if then is None:
        return math.inf
    return (now - then).total_seconds()


def _whole_seconds(delta: timedelta) -> int:
    return int(delta.total_seconds())


# --------------------------------------------------------------------------- the agent's polls


class _AgentPolls:
    """The time of the repair agent's last default poll of GET /repair/runs, in memory (one server process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: datetime | None = None

    def last(self) -> datetime | None:
        with self._lock:
            return self._last

    def record(self, at: datetime) -> None:
        with self._lock:
            self._last = at

    def reset(self) -> None:
        with self._lock:
            self._last = None


_AGENT_POLLS = _AgentPolls()


def agent_last_seen() -> datetime | None:
    """The agent's last default poll, or None if it has not polled since the server started."""
    return _AGENT_POLLS.last()


def record_agent_poll(at: datetime) -> None:
    """Called by the default GET /repair/runs after its sweep, so that the sweep saw the previous poll."""
    _AGENT_POLLS.record(at)


# --------------------------------------------------------------------------- the intent_timeouts the sweeps read


class _IntentTimeouts:
    """The intent_timeout values the sweeps have read, each with when a sweep first read it, in memory (one server
    process), as the agent's polls are.

    expire_workers clears a claim by the intent_timeout in force when it runs, and PUT /settings can change that
    value at any time, so today's value alone cannot tell a release from a heartbeat gap (run_end_reason). Only
    changes are kept, at most INTENT_TIMEOUT_CHANGES_KEPT: past that, the two oldest merge into one that keeps the
    smaller value, which can only make the release rule stricter. A value set and replaced between two sweeps is
    never read, and a server restart forgets the values read before it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changes: list[tuple[datetime, int]] = []

    def record(self, at: datetime, value: int) -> None:
        """A sweep read `value` at `at`; kept if it differs from the value read before it."""
        with self._lock:
            if self._changes and self._changes[-1][1] == value:
                return
            self._changes.append((at, value))
            if len(self._changes) > INTENT_TIMEOUT_CHANGES_KEPT:
                (first_at, first), (_second_at, second) = self._changes[0], self._changes[1]
                self._changes[:2] = [(first_at, min(first, second))]

    def shortest_since(self, since: datetime) -> int | None:
        """The smallest value that may have been in force at some moment from `since` on, or None before any read:
        the value read last before `since`, and every value read at or after it. Times are whole seconds, so a value
        read in the same second as `since` may have been set before or after it, and both values count.
        """
        with self._lock:
            changes = list(self._changes)
        before = [value for at, value in changes if at < since]
        return min(before[-1:] + [value for at, value in changes if at >= since], default=None)

    def reset(self) -> None:
        with self._lock:
            self._changes.clear()


_INTENT_TIMEOUTS = _IntentTimeouts()


def read_intent_timeout(conn: sqlite3.Connection, now: datetime) -> int:
    """Today's intent_timeout, as the sweep reads it; the value is kept with the time of this read."""
    value = get_intent_timeout(conn)
    _INTENT_TIMEOUTS.record(now, value)
    return value


def shortest_intent_timeout_since(heartbeat_at: object) -> int | None:
    """The smallest intent_timeout that may have been in force since the heartbeat, as far as the sweeps have read
    them; None for a NULL or unreadable heartbeat (whose age is infinite) or before any sweep has read one.
    """
    since = parse_time(heartbeat_at)
    return None if since is None else _INTENT_TIMEOUTS.shortest_since(since)


def reset_memory() -> None:
    """Forget what this server process keeps in memory, as a restart does: the agent's last poll and the
    intent_timeouts the sweeps read. For tests, which start each server on a new database.
    """
    _AGENT_POLLS.reset()
    _INTENT_TIMEOUTS.reset()


# --------------------------------------------------------------------------- transactions

# The INFO lines of the repair transaction in progress in this context, emitted once it has committed; None outside one.
_PENDING_LINES: ContextVar[list[tuple[str, tuple[Any, ...]]] | None] = ContextVar("repair_pending_lines", default=None)


def log_after_commit(message: str, *args: Any) -> None:
    """Log an INFO line about what the current repair transaction writes, once that transaction has committed.

    A transaction that rolls back, such as a request that fails, writes nothing and so logs nothing: no line names a
    record that does not exist, and the sweep that records it later does not log it a second time. Outside a repair
    transaction the line is logged at once.
    """
    pending = _PENDING_LINES.get()
    if pending is None:
        LOG.info(message, *args)
    else:
        pending.append((message, args))


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """A connection whose transaction starts with BEGIN IMMEDIATE, so that every check-then-write is atomic.

    get_conn commits it on success and rolls it back on an error. A second writer waits within the busy timeout.
    The lines given to log_after_commit inside the block are logged after the commit, and dropped with a rollback.
    So a request refused after its sweep raises its refusal after the block: the sweep's endings and records are
    then kept and logged, and a refusal can name a record that the sweep made (spec 6.5, check 4).
    """
    pending: list[tuple[str, tuple[Any, ...]]] = []
    outer = _PENDING_LINES.get()
    _PENDING_LINES.set(pending)
    try:
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
    finally:
        _PENDING_LINES.set(outer)
    for message, args in pending:
        LOG.info(message, *args)


# --------------------------------------------------------------------------- windows


@dataclass(frozen=True)
class Window:
    """One review window of a run. Phase 2 has one per run: the whole budget, number 1."""

    number: int
    start: datetime
    end: datetime
    seconds: int
    opens: datetime
    closes: datetime
    accept_until: datetime
    reviewable: bool


def is_reviewable(budget_seconds: int, cli_type: str) -> bool:
    """Whether a run is ever reviewed: a window of at least 350 s, of a CLI with a session transcript."""
    return budget_seconds >= MIN_REVIEWABLE_WINDOW_SECONDS and cli_type in REVIEWABLE_CLI_TYPES


def current_window(run: RowLike) -> Window:
    """The run's current window: [started_at, deadline_at] in Phase 2, whose deadline never moves."""
    start = _required_time(run["started_at"])
    end = _required_time(run["deadline_at"])
    seconds = int(run["budget_seconds"])
    return Window(
        number=1,
        start=start,
        end=end,
        seconds=seconds,
        opens=start + timedelta(seconds=math.floor(seconds * REVIEW_OPEN_FRACTION)),
        closes=start + timedelta(seconds=math.floor(seconds * REVIEW_CLOSE_FRACTION)),
        accept_until=end - timedelta(seconds=REVIEW_ACCEPT_MARGIN_SECONDS),
        reviewable=bool(run["reviewable"]) and seconds >= MIN_REVIEWABLE_WINDOW_SECONDS,
    )


def review_status(run: RowLike, window: Window, review_source: str | None, now: datetime) -> str:
    """window.review_status of the run view. review_source is the source of the window's review row, if any."""
    if review_source == "agent":
        return "reviewed"
    if review_source == "server":
        return "not_reviewed"
    if not window.reviewable:
        return "not_reviewable"
    if run["ended_at"] is not None:
        return "closed"
    if now < window.opens:
        return "pending"
    if now < window.closes:
        return "open"
    if now < window.accept_until:
        return "late"
    return "closed"  # past accept_until, until the next sweep records not_reviewed


# --------------------------------------------------------------------------- liveness


def repair_lapse_seconds(intent_timeout: int) -> int:
    """How long a run outlives a heartbeat gap: 60 s with today's default intent_timeout of 15."""
    return max(LAPSE_TIMEOUT_MULTIPLIER * intent_timeout, LAPSE_MIN_SECONDS)


def run_end_reason(
    run: RowLike,
    intent: RowLike,
    project: RowLike,
    now: datetime,
    intent_timeout: int,
    *,
    shortest_intent_timeout: int | None = None,
) -> tuple[str, str | None] | None:
    """None while the open run is live, else (end_reason, last_alive_at).

    Only release, stopping the project and expire_workers clear a claim, and expire_workers only once the heartbeat
    is older than the intent_timeout in force when it runs, or exactly that old: it compares julianday floats, and
    for some heartbeat times their rounding reads an age of exactly intent_timeout as older. PUT /settings can change
    intent_timeout at any time, so a claim cleared by a smaller value that has since been raised has a heartbeat
    younger than today's value. So a cleared claim was released only if its heartbeat is strictly younger than every
    intent_timeout in force since that heartbeat: shortest_intent_timeout, the smallest the sweeps have read since
    then (shortest_intent_timeout_since), or today's intent_timeout if not given. A value set and replaced between
    two sweeps, or read only before a server restart, is unknown here, so a claim that expire_workers cleared under
    it can still read as a release. The same worker re-claims on its next heartbeat, so a gap ends the run only past
    repair_lapse_seconds. The switch is not part of liveness.
    """
    if intent["to_fact_id"] is not None:
        return "concluded", intent["concluded_at"]
    if project["status"] != "active":
        return "project_inactive", intent["last_heartbeat_at"]
    if intent["worker"] is not None and intent["worker"] != run["worker"]:
        return "claim_taken", None
    if run["state"] in LAPSE_EXEMPT_STATES:
        return None
    age = seconds_since(intent["last_heartbeat_at"], now)  # infinite if NULL
    # Spec 5.1's code compares with today's value only, with <=. Strictly younger: a claim that expire_workers cleared
    # at an age of exactly intent_timeout is a heartbeat gap, which the same worker's next heartbeat closes, not a
    # release. And younger than every value in force since the heartbeat: a claim cleared past a smaller value, which
    # PUT /settings has raised since, is a gap too.
    release_limit = intent_timeout if shortest_intent_timeout is None else min(intent_timeout, shortest_intent_timeout)
    if intent["worker"] is None and age < release_limit:
        return "released", intent["last_heartbeat_at"]
    if age > repair_lapse_seconds(intent_timeout):
        return "claim_lost", intent["last_heartbeat_at"]
    return None


# --------------------------------------------------------------------------- reading runs

# A run with its intent and project. A run whose intent or project is gone is never read.
_RUN_SELECT = (
    "SELECT r.*, i.description AS intent_description, i.to_fact_id AS intent_to_fact_id,"
    " i.concluded_at AS intent_concluded_at, i.worker AS intent_worker,"
    " i.last_heartbeat_at AS intent_last_heartbeat_at, p.status AS project_status,"
    " p.repair_enabled AS project_repair_enabled"
    " FROM repair_runs AS r"
    " JOIN intents AS i ON i.id = r.intent_id AND i.project_id = r.project_id"
    " JOIN projects AS p ON p.id = r.project_id"
)
_FIND_RUN_SQL = _RUN_SELECT + " WHERE r.run_id = ?"
# The sweep reads only open or unsettled runs, through the partial index repair_runs_attention.
ATTENTION_SQL = _RUN_SELECT + " WHERE r.ended_at IS NULL OR r.settled_window = 0 ORDER BY r.started_at, r.run_id"
# GET /repair/runs: the open runs of active projects whose switch is on, oldest start first (live_runs keeps those in a
# listed state), and, for evaluation tools, every run, newest registration first, one page after a cursor.
_LIVE_RUNS_SQL = _RUN_SELECT + (
    " WHERE r.ended_at IS NULL AND p.status = 'active' AND p.repair_enabled != 0"
    " AND (:project_id IS NULL OR r.project_id = :project_id)"
    " ORDER BY r.started_at, r.run_id"
)
_RUNS_PAGE_SQL = _RUN_SELECT + (
    " WHERE (:project_id IS NULL OR r.project_id = :project_id)"
    " AND (:registered_at IS NULL OR r.registered_at < :registered_at"
    " OR (r.registered_at = :registered_at AND r.run_id < :run_id))"
    " ORDER BY r.registered_at DESC, r.run_id DESC LIMIT :limit"
)


def find_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """The run with its intent and project, or None: unknown, deleted, or its intent or project is missing."""
    return conn.execute(_FIND_RUN_SQL, (run_id,)).fetchone()


def live_runs(conn: sqlite3.Connection, project_id: str | None, limit: int) -> list[sqlite3.Row]:
    """The runs the agent reviews: open, of an active project whose switch is on, and in a state of LISTED_STATES, so
    that the detached runs of Phase 4, which stay open, are not listed. Oldest start first, at most `limit`.
    """
    rows = conn.execute(_LIVE_RUNS_SQL, {"project_id": project_id}).fetchall()
    return [row for row in rows if row["state"] in LISTED_STATES][:limit]


def runs_page(
    conn: sqlite3.Connection, project_id: str | None, limit: int, after: tuple[str, str] | None
) -> list[sqlite3.Row]:
    """Every run, ended ones included, whatever the switch, newest registration first: at most `limit` of them after
    the (registered_at, run_id) of the previous page's last run.
    """
    registered_at, run_id = after if after is not None else (None, None)
    return conn.execute(
        _RUNS_PAGE_SQL, {"project_id": project_id, "registered_at": registered_at, "run_id": run_id, "limit": limit}
    ).fetchall()


def _intent_of(row: RowLike) -> dict[str, Any]:
    return {
        "to_fact_id": row["intent_to_fact_id"],
        "concluded_at": row["intent_concluded_at"],
        "worker": row["intent_worker"],
        "last_heartbeat_at": row["intent_last_heartbeat_at"],
    }


def _project_of(row: RowLike) -> dict[str, Any]:
    return {"status": row["project_status"], "repair_enabled": row["project_repair_enabled"]}


# --------------------------------------------------------------------------- registering


def insert_run(conn: sqlite3.Connection, run_id: str, body: RunRegisterRequest, now: datetime) -> None:
    """The row of a new run. The caller has made the checks of a new register (its project, intent and claim).

    started_at is the receive time minus started_seconds_ago, rounded; the deadline never moves in Phase 2. A run
    that is never reviewed is settled at once, so it is never flagged.
    """
    started = now - timedelta(seconds=round(body.started_seconds_ago))
    deadline = started + timedelta(seconds=body.budget_seconds)
    reviewable = is_reviewable(body.budget_seconds, body.cli_type)
    registered_at = format_time(now)
    conn.execute(
        "INSERT INTO repair_runs (run_id, project_id, intent_id, worker, cli_type, execution, session_id,"
        " locator_json, budget_seconds, started_at, deadline_at, version, slices_used, state, reviewable,"
        " settled_window, registered_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'running', ?, ?, ?, ?)",
        (
            run_id,
            body.project_id,
            body.intent_id,
            body.worker,
            body.cli_type,
            body.execution,
            body.session_id,
            json.dumps(body.locator, ensure_ascii=False),
            body.budget_seconds,
            format_time(started),
            format_time(deadline),
            int(reviewable),
            0 if reviewable else 1,
            registered_at,
            registered_at,
        ),
    )


# --------------------------------------------------------------------------- endings and stamping


def end_stamp(run: RowLike) -> dict[str, Any]:
    """What an ended run adds to the detail of its repair_review issues: when and why it ended, and its outcome."""
    stamp: dict[str, Any] = {
        "run_ended_at": run["last_alive_at"] or run["ended_at"],
        "end_reason": run["end_reason"],
    }
    if run["outcome"] is not None:
        stamp["outcome"] = run["outcome"]
    return stamp


def _loads_object(text: object) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def stamp_run_issues(conn: sqlite3.Connection, run_id: str, now: datetime) -> int:
    """Add the ended run's stamp to every repair_review issue of the run; returns how many issues changed.

    Runs when a row ends, on any path, and again when a later /end corrects last_alive_at or stores the outcome.
    """
    run = conn.execute(
        "SELECT project_id, intent_id, ended_at, end_reason, last_alive_at, outcome FROM repair_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None or run["ended_at"] is None:
        return 0
    stamp = end_stamp(run)
    changed = 0
    issues = conn.execute(
        "SELECT id, detail_json FROM intent_issues"
        " WHERE project_id = ? AND intent_id = ? AND run_id = ? AND kind = ? ORDER BY id",
        (run["project_id"], run["intent_id"], run_id, REPAIR_REVIEW),
    ).fetchall()
    for issue in issues:
        detail = _loads_object(issue["detail_json"])
        if detail is None:
            continue
        stamped = {**detail, **stamp}
        if stamped == detail:
            continue
        conn.execute(
            "UPDATE intent_issues SET detail_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(stamped, ensure_ascii=False), format_time(now), issue["id"]),
        )
        changed += 1
    return changed


def end_open_run(
    conn: sqlite3.Connection,
    run_id: str,
    end_reason: str,
    *,
    last_alive_at: str | None,
    fact_id: str | None,
    now: datetime,
) -> bool:
    """End an open run and stamp its issues; False if it had already ended."""
    ended_at = format_time(now)
    cursor = conn.execute(
        "UPDATE repair_runs SET state = 'ended', ended_at = ?, end_reason = ?, last_alive_at = ?,"
        " fact_id = COALESCE(fact_id, ?), updated_at = ? WHERE run_id = ? AND ended_at IS NULL",
        (ended_at, end_reason, last_alive_at, fact_id, ended_at, run_id),
    )
    if cursor.rowcount == 0:
        return False
    stamp_run_issues(conn, run_id, now)
    return True


# --------------------------------------------------------------------------- the sweep


def sweep(conn: sqlite3.Connection, now: datetime, agent_seen_before: datetime | None) -> None:
    """End the open runs that have an end reason, then settle the current window of every open or unsettled run.

    Idempotent. Runs in the repair endpoints' BEGIN IMMEDIATE transaction: at the start of register, the listings
    and review posts, and after the ending in /end. agent_seen_before is the agent's poll before this request.
    """
    intent_timeout = read_intent_timeout(conn, now)
    for row in conn.execute(ATTENTION_SQL).fetchall():
        if row["ended_at"] is not None:
            continue
        ending = run_end_reason(
            row,
            _intent_of(row),
            _project_of(row),
            now,
            intent_timeout,
            shortest_intent_timeout=shortest_intent_timeout_since(row["intent_last_heartbeat_at"]),
        )
        if ending is None:
            continue
        end_reason, last_alive_at = ending
        fact_id = row["intent_to_fact_id"] if end_reason == "concluded" else None
        end_open_run(conn, row["run_id"], end_reason, last_alive_at=last_alive_at, fact_id=fact_id, now=now)
    for row in conn.execute(ATTENTION_SQL).fetchall():
        settle_window(conn, row, now, agent_seen_before)


# --------------------------------------------------------------------------- the not-reviewed rule


def settle_window(
    conn: sqlite3.Connection,
    run: RowLike,
    now: datetime,
    agent_seen_before: datetime | None,
    *,
    again: bool = False,
) -> bool:
    """Settle the run's current window once; True if this call recorded "not reviewed".

    `run` is read with find_run or ATTENTION_SQL. The rules, in order: the project's switch is off, or the window
    has a review, or the run is not reviewable: settled, nothing recorded. A live run at accept_until, or an ended
    run last alive at or after the window closed: not reviewed. Any other ended run: settled, nothing recorded. A
    live run before accept_until: not settled yet. `again` applies the rules once more to a settled window, after a
    late /end corrects when the run was last alive; the primary key of repair_reviews prevents a second record.
    """
    window = current_window(run)
    if not again and run["settled_window"] >= window.number:
        return False
    if not run["project_repair_enabled"]:
        _mark_settled(conn, run["run_id"], window.number, now)
        return False
    has_review = (
        conn.execute(
            "SELECT 1 FROM repair_reviews WHERE run_id = ? AND window_no = ?", (run["run_id"], window.number)
        ).fetchone()
        is not None
    )
    if has_review or not window.reviewable:
        _mark_settled(conn, run["run_id"], window.number, now)
        return False
    recorded = False
    if run["ended_at"] is None:
        if now < window.accept_until:
            return False
        recorded = _record_not_reviewed(conn, run, window, now, alive_at=now, agent_seen_before=agent_seen_before)
    else:
        last_alive = parse_time(run["last_alive_at"])
        if last_alive is not None and last_alive >= window.closes:
            recorded = _record_not_reviewed(
                conn, run, window, now, alive_at=last_alive, agent_seen_before=agent_seen_before
            )
    _mark_settled(conn, run["run_id"], window.number, now)
    return recorded


def _mark_settled(conn: sqlite3.Connection, run_id: str, window_number: int, now: datetime) -> None:
    conn.execute(
        "UPDATE repair_runs SET settled_window = ?, updated_at = ? WHERE run_id = ? AND settled_window < ?",
        (window_number, format_time(now), run_id, window_number),
    )


def _snapshot(run: RowLike, window: Window, at: datetime) -> dict[str, Any]:
    """The run's timing at a record: the window, the time used by `at`, and the run's budget and deadline."""
    started = _required_time(run["started_at"])
    return {
        "window": window.number,
        "run_started_at": run["started_at"],
        "elapsed_seconds": max(0, _whole_seconds(at - started)),
        "budget_seconds": run["budget_seconds"],
        "window_seconds": window.seconds,
        "deadline_at": run["deadline_at"],
        "slices_used": run["slices_used"],
    }


def _insert_issue(conn: sqlite3.Connection, run: RowLike, detail: dict[str, Any], created_at: str) -> int:
    cursor = conn.execute(
        "INSERT INTO intent_issues (project_id, intent_id, run_id, kind, detail_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run["project_id"],
            run["intent_id"],
            run["run_id"],
            REPAIR_REVIEW,
            json.dumps(detail, ensure_ascii=False),
            created_at,
            created_at,
        ),
    )
    issue_id = cursor.lastrowid
    if issue_id is None:
        raise sqlite3.DatabaseError("the repair_review issue was stored without an id")
    return issue_id


def _record_not_reviewed(
    conn: sqlite3.Connection,
    run: RowLike,
    window: Window,
    now: datetime,
    *,
    alive_at: datetime,
    agent_seen_before: datetime | None,
) -> bool:
    """Insert the server's not-reviewed record and, only if that row was new, its issue."""
    run_ended = run["ended_at"] is not None
    why = "agent_seen" if agent_seen_before is not None and agent_seen_before >= window.opens else "agent_not_seen"
    detail = {
        "decision": "not_reviewed",
        "mode": "shadow",
        "applied": False,
        **_snapshot(run, window, alive_at),
        "display_truncated": False,
        "why": why,
        "agent_last_seen_at": format_time(agent_seen_before) if agent_seen_before is not None else None,
        "run_ended": run_ended,
    }
    created_at = format_time(now)
    cursor = conn.execute(
        "INSERT INTO repair_reviews (run_id, window_no, review_id, source, decision, detail_json, created_at)"
        " VALUES (?, ?, 'server', 'server', 'not_reviewed', ?, ?) ON CONFLICT (run_id, window_no) DO NOTHING",
        (run["run_id"], window.number, json.dumps(detail, ensure_ascii=False), created_at),
    )
    if cursor.rowcount == 0:
        return False
    # The ending and its stamping came before this window step, so a record of an ended run is stamped here.
    issue_detail = {**detail, **end_stamp(run)} if run_ended else detail
    issue_id = _insert_issue(conn, run, issue_detail, created_at)
    conn.execute(
        "UPDATE repair_reviews SET issue_id = ? WHERE run_id = ? AND window_no = ?",
        (issue_id, run["run_id"], window.number),
    )
    log_after_commit(
        "repair window not reviewed run=%s project=%s intent=%s window=%s why=%s",
        run["run_id"],
        run["project_id"],
        run["intent_id"],
        window.number,
        why,
    )
    return True


# --------------------------------------------------------------------------- the dispatcher's /end


def end_run(
    conn: sqlite3.Connection,
    run_id: str,
    body: RunEndRequest,
    *,
    now: datetime,
    agent_seen_before: datetime | None,
) -> sqlite3.Row | None:
    """Apply one of the dispatcher's /end calls, then sweep. None if the run is not found (404 run_not_found).

    The first /end on an open run ends it: ended_at now, end_reason dispatcher, last_alive_at the reported exit
    time (not before started_at), the reported fields, and the intent's fact if it has concluded. On a run that the
    sweep or a supersede ended first, the first exit report keeps ended_at and end_reason but corrects
    last_alive_at, because the dispatcher's exit time is the truth, and applies the not-reviewed rule once more.
    Later calls fill only empty fields. Every change of last_alive_at or the outcome re-stamps the run's issues.
    /end does not check the switch.
    """
    run = find_run(conn, run_id)
    if run is None:
        return None
    exit_reported = bool(body.model_fields_set & EXIT_REPORT_FIELDS)
    started = _required_time(run["started_at"])
    exited_at = format_time(max(now - timedelta(seconds=round(body.exited_seconds_ago or 0)), started))
    report = (
        json.dumps({"timed_out": body.timed_out, "cancelled": body.cancelled, "returncode": body.returncode})
        if exit_reported
        else None
    )
    fact_id = run["intent_to_fact_id"]
    changed_at = format_time(now)
    if run["ended_at"] is None:
        conn.execute(
            "UPDATE repair_runs SET state = 'ended', ended_at = ?, end_reason = 'dispatcher', last_alive_at = ?,"
            " outcome = ?, outcome_json = ?, fact_id = COALESCE(fact_id, ?), updated_at = ?"
            " WHERE run_id = ? AND ended_at IS NULL",
            (changed_at, exited_at, body.outcome, report, fact_id, changed_at, run_id),
        )
        stamp_run_issues(conn, run_id, now)
    else:
        fills_report = report is not None and run["outcome_json"] is None
        # The dispatcher's first exit report on a run that the sweep or a supersede ended.
        corrects_exit = fills_report and run["end_reason"] != "dispatcher"
        stores_outcome = body.outcome is not None and run["outcome"] is None
        fills_fact = fact_id is not None and run["fact_id"] is None
        if fills_report or stores_outcome or fills_fact:
            conn.execute(
                "UPDATE repair_runs SET last_alive_at = ?, outcome = COALESCE(outcome, ?),"
                " outcome_json = COALESCE(outcome_json, ?), fact_id = COALESCE(fact_id, ?), updated_at = ?"
                " WHERE run_id = ?",
                (
                    exited_at if corrects_exit else run["last_alive_at"],
                    body.outcome,
                    report,
                    fact_id,
                    changed_at,
                    run_id,
                ),
            )
        if corrects_exit or stores_outcome:
            stamp_run_issues(conn, run_id, now)
        if corrects_exit:
            corrected = find_run(conn, run_id)
            if corrected is not None:
                settle_window(conn, corrected, now, agent_seen_before, again=True)
    ended = find_run(conn, run_id)
    if ended is not None:
        log_after_commit(
            "repair run ended run=%s project=%s intent=%s reason=%s outcome=%s",
            run_id,
            ended["project_id"],
            ended["intent_id"],
            ended["end_reason"],
            ended["outcome"],
        )
    sweep(conn, now, agent_seen_before)
    return find_run(conn, run_id)


# --------------------------------------------------------------------------- agent reviews

_DROPPED_CATEGORIES = frozenset({"Cc", "Cf"})
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
REPLACEMENT_CHARACTER = "�"


def replace_lone_surrogates(value: Any) -> Any:
    """`value` with each lone surrogate in its strings, dict keys included, replaced by U+FFFD, one for one.

    A JSON body may hold the escape of half a surrogate pair, such as \\ud83d: JSON.stringify writes one for a string
    cut between the two halves, as a transcript excerpt can be. json.loads reads it back as a lone surrogate, which no
    UTF-8 text can hold, so storing, hashing or answering with it would fail. Lengths do not change, so no limit does.
    """
    if isinstance(value, str):
        return _LONE_SURROGATE.sub(REPLACEMENT_CHARACTER, value) if _LONE_SURROGATE.search(value) else value
    if isinstance(value, dict):
        return {replace_lone_surrogates(key): replace_lone_surrogates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_lone_surrogates(item) for item in value]
    return value


def clean_text(value: str | None) -> str:
    """Fold whitespace runs to one space and drop Unicode categories Cc and Cf (bidi overrides, zero-width marks).

    Whitespace is folded first, so a line break between two words leaves a space between them.
    """
    if not value:
        return ""
    kept = [
        " " if char.isspace() else char
        for char in value
        if char.isspace() or unicodedata.category(char) not in _DROPPED_CATEGORIES
    ]
    return " ".join("".join(kept).split())


# The id rule of spec 8.7, which a name must match in full to be shown in a log line.
LOGGABLE_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"


def loggable_name(value: object) -> str:
    """A name as a repair log line shows it: the name if it fully matches the id rule, else "-".

    For the worker of a register and of a supersede (spec 6.1, 12). A worker name has no rule of its own, so the
    request model takes any 1 to 128 characters, and register accepts it while the intent's claim is NULL, so it may
    match nothing in the database. Shown as sent, a line break or an escape sequence in it would write a forged line
    into the server log.
    """
    if isinstance(value, str) and re.fullmatch(LOGGABLE_NAME_PATTERN, value) is not None:
        return value
    return "-"


def record_agent_review(
    conn: sqlite3.Connection, run: RowLike, body: ReviewRequest, now: datetime
) -> tuple[sqlite3.Row, IntentIssue]:
    """Store an agent review that passed every check of its post, with its display issue, and settle the window.

    The full cleaned texts, the digest with its SHA-256 and the raw output go to repair_reviews; the issue gets the
    short display copies. Shadow mode: the decision is recorded and never applied, so the run's deadline, version,
    slices and state do not change, whatever the decision.
    """
    window = current_window(run)
    reason = clean_text(body.reason)
    evidence = [item for item in (clean_text(text) for text in body.evidence) if item]
    next_step = clean_text(body.next_step) or None
    model = clean_text(body.model) or None
    snapshot = _snapshot(run, window, now)
    stored: dict[str, Any] = {
        "decision": body.decision,
        "mode": "shadow",
        "applied": False,
        **snapshot,
        "reason": reason,
        "evidence": evidence,
        "next_step": next_step,
        "fallback": body.fallback,
        "model": model,
        "expected_version": body.expected_version,
        "digest_elapsed_seconds": body.digest_elapsed_seconds,
        "transcript_cut_seconds": body.transcript_cut_seconds,
    }
    shown_evidence = evidence[:DISPLAY_EVIDENCE_MAX_ITEMS]
    display_truncated = (
        len(reason) > DISPLAY_REASON_MAX_CHARS
        or len(evidence) > DISPLAY_EVIDENCE_MAX_ITEMS
        or any(len(item) > DISPLAY_EVIDENCE_MAX_CHARS for item in shown_evidence)
        or (next_step is not None and len(next_step) > DISPLAY_NEXT_STEP_MAX_CHARS)
    )
    display: dict[str, Any] = {
        "decision": body.decision,
        "mode": "shadow",
        "applied": False,
        **snapshot,
        "reason": reason[:DISPLAY_REASON_MAX_CHARS],
        "evidence": [item[:DISPLAY_EVIDENCE_MAX_CHARS] for item in shown_evidence],
        "next_step": next_step[:DISPLAY_NEXT_STEP_MAX_CHARS] if next_step is not None else None,
        "display_truncated": display_truncated,
        "fallback": body.fallback,
        "model": model[:DISPLAY_MODEL_MAX_CHARS] if model is not None else None,
    }
    if body.decision == "extend":
        stored["slice_seconds"] = SLICE_SECONDS
        display["slice_seconds"] = SLICE_SECONDS
    if run["ended_at"] is not None:
        display.update(end_stamp(run))
    created_at = format_time(now)
    issue_id = _insert_issue(conn, run, display, created_at)
    digest_sha256 = hashlib.sha256(body.digest.encode("utf-8")).hexdigest() if body.digest is not None else None
    conn.execute(
        "INSERT INTO repair_reviews (run_id, window_no, review_id, source, decision, issue_id, detail_json, digest,"
        " digest_sha256, raw_output, created_at) VALUES (?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?)",
        (
            run["run_id"],
            window.number,
            body.review_id,
            body.decision,
            issue_id,
            json.dumps(stored, ensure_ascii=False),
            body.digest,
            digest_sha256,
            body.raw_output,
            created_at,
        ),
    )
    _mark_settled(conn, run["run_id"], window.number, now)
    log_after_commit(
        "repair review recorded run=%s project=%s intent=%s window=%s decision=%s fallback=%s mode=shadow"
        " applied=false",
        run["run_id"],
        run["project_id"],
        run["intent_id"],
        window.number,
        body.decision,
        body.fallback,
    )
    review = conn.execute(
        "SELECT * FROM repair_reviews WHERE run_id = ? AND window_no = ?", (run["run_id"], window.number)
    ).fetchone()
    issue = IntentIssue(
        id=issue_id,
        kind=REPAIR_REVIEW,
        run_id=run["run_id"],
        detail=display,
        created_at=created_at,
        updated_at=created_at,
    )
    return review, issue


# --------------------------------------------------------------------------- views


def review_record(row: RowLike) -> dict[str, Any]:
    """A stored review as a review post answers it (its 201, its 200 replay, and its 409s)."""
    return {
        "run_id": row["run_id"],
        "window": row["window_no"],
        "review_id": row["review_id"],
        "source": row["source"],
        "decision": row["decision"],
        "issue_id": row["issue_id"],
        "created_at": row["created_at"],
    }


def load_issue(conn: sqlite3.Connection, issue_id: int | None) -> IntentIssue | None:
    if issue_id is None:
        return None
    row = conn.execute("SELECT * FROM intent_issues WHERE id = ?", (issue_id,)).fetchone()
    if row is None:
        return None
    return IntentIssue(
        id=row["id"],
        kind=row["kind"],
        run_id=row["run_id"],
        detail=_loads_object(row["detail_json"]) or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def review_body(conn: sqlite3.Connection, review: RowLike, *, created: bool) -> dict[str, Any]:
    """The answer to a review post: the stored review and its issue, in shadow mode, never applied."""
    issue = load_issue(conn, review["issue_id"])
    return {
        "created": created,
        "mode": "shadow",
        "applied": False,
        "review": review_record(review),
        "issue": issue.model_dump() if issue is not None else None,
    }


def _review_summary(row: RowLike) -> dict[str, Any]:
    return {
        "window": row["window_no"],
        "review_id": row["review_id"],
        "source": row["source"],
        "decision": row["decision"],
        "issue_id": row["issue_id"],
        "created_at": row["created_at"],
    }


def _stored_review(row: RowLike) -> dict[str, Any]:
    return {
        **(_loads_object(row["detail_json"]) or {}),
        "window": row["window_no"],
        "review_id": row["review_id"],
        "source": row["source"],
        "decision": row["decision"],
        "issue_id": row["issue_id"],
        "created_at": row["created_at"],
        "digest": row["digest"],
        "digest_sha256": row["digest_sha256"],
        "raw_output": row["raw_output"],
    }


def _locator(text: object) -> dict[str, Any]:
    return _loads_object(text) or {}


def _view_state(run: RowLike, deadline: datetime, now: datetime) -> str:
    if run["ended_at"] is not None:
        return "ended"
    if run["state"] != "running":
        return run["state"]
    # Live but past its deadline: today's timeout and conclude fallback are under way.
    return "over_budget" if now > deadline else "running"


def run_view(conn: sqlite3.Connection, run: RowLike, now: datetime, *, full_reviews: bool = False) -> dict[str, Any]:
    """The RunView of a run read with find_run. Times are the server's clock; elapsed and remaining are null once
    the run has ended. full_reviews gives the stored reviews (GET /repair/runs/{run_id}) instead of summaries.
    """
    window = current_window(run)
    started = _required_time(run["started_at"])
    deadline = _required_time(run["deadline_at"])
    live = run["ended_at"] is None
    reviews = conn.execute(
        "SELECT * FROM repair_reviews WHERE run_id = ? ORDER BY window_no", (run["run_id"],)
    ).fetchall()
    current = next((review for review in reviews if review["window_no"] == window.number), None)
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (run["intent_id"], run["project_id"]),
    ).fetchall()
    return {
        "run_id": run["run_id"],
        "project_id": run["project_id"],
        "intent_id": run["intent_id"],
        "intent": {"description": run["intent_description"], "from": [source["fact_id"] for source in sources]},
        "worker": run["worker"],
        "cli_type": run["cli_type"],
        "execution": run["execution"],
        "session_id": run["session_id"],
        "locator": _locator(run["locator_json"]),
        "budget_seconds": run["budget_seconds"],
        "started_at": run["started_at"],
        "deadline_at": run["deadline_at"],
        "version": run["version"],
        "slices_used": run["slices_used"],
        "state": _view_state(run, deadline, now),
        "elapsed_seconds": max(0, _whole_seconds(now - started)) if live else None,
        "remaining_seconds": max(0, _whole_seconds(deadline - now)) if live else None,
        "window": {
            "number": window.number,
            "start_at": format_time(window.start),
            "end_at": format_time(window.end),
            "seconds": window.seconds,
            "review_opens_at": format_time(window.opens),
            "review_closes_at": format_time(window.closes),
            "review_accept_until": format_time(window.accept_until),
            "reviewable": window.reviewable,
            "review_status": review_status(run, window, current["source"] if current is not None else None, now),
        },
        "reviews": [_stored_review(review) if full_reviews else _review_summary(review) for review in reviews],
        "registered_at": run["registered_at"],
        "updated_at": run["updated_at"],
        "ended_at": run["ended_at"],
        "end_reason": run["end_reason"],
        "last_alive_at": run["last_alive_at"],
        "outcome": run["outcome"],
        "fact_id": run["fact_id"],
    }
