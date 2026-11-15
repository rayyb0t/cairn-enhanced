"""Repair layer (shadow mode): the repair endpoints.

The run endpoints need a role token (cairn.server.repair_auth): the dispatcher registers and ends runs, and the repair
agent lists the runs, reads one and posts its review. The Repair switch and the read-only repair settings are open,
like every endpoint on main. No existing endpoint is changed.

Each run endpoint works in one BEGIN IMMEDIATE transaction that starts with the sweep (for /end, the sweep follows the
ending), and raises a refusal only once that transaction has committed, so the sweep's endings and records stand
whatever the answer. Phase 2 records and never applies: no endpoint here moves a run's deadline, version, slices or
state on a review, whatever its decision.

Errors raised here have one shape, {"detail": {"code": ..., "message": ..., ...}}, so a client can tell a repair answer
from FastAPI's own {"detail": "Not Found"} for an unknown route. The switch, which the UI calls, keeps the plain string
"Project not found" that the UI shows. A body or parameter that fails its model gets FastAPI's own 422. No exception
handler is added: one on the app would change the 422 body of every existing endpoint. The repair routes alone use
RepairRoute, which checks a run route's token before FastAPI reads the body, so that a refused token is always the
coded 401 or 503, and makes FastAPI's 422 writable when the body holds NaN, Infinity or a lone surrogate, which
json.loads accepts and the models refuse, rather than a 500.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import sqlite3
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from cairn.server import db
from cairn.server.models import ProjectMeta
from cairn.server.repair import (
    DIGEST_TIME_MARGIN_SECONDS,
    SHADOW,
    SLICE_SECONDS,
    agent_last_seen,
    current_window,
    end_open_run,
    end_run,
    find_run,
    format_time,
    immediate_transaction,
    insert_run,
    live_runs,
    log_after_commit,
    loggable_name,
    parse_time,
    record_agent_poll,
    record_agent_review,
    repair_lapse_seconds,
    replace_lone_surrogates,
    review_body,
    review_record,
    review_rules,
    run_view,
    runs_page,
    seconds_since,
    server_now,
    settings_constants,
    settle_window,
    sweep,
)
from cairn.server.repair_auth import (
    require_agent,
    require_dispatcher,
    requirement_of,
    roles_configured,
    verify_request,
)
from cairn.server.repair_models import (
    RepairSettingsView,
    ReviewRequest,
    ReviewResponse,
    RunDetailView,
    RunEndRequest,
    RunEndResponse,
    RunListResponse,
    RunRegisterRequest,
    RunRegisterResponse,
    UpdateProjectRepairRequest,
    valid_run_id,
)
from cairn.server.services import (
    get_intent_timeout,
    get_project_or_404,
    log_repair_switch,
    project_meta_from_row,
    project_repair_enabled,
)

# The same logger as cairn.server.repair: every server repair line but the token lines goes through it.
LOG = logging.getLogger("cairn.server.repair")


def _json_safe(value: Any) -> Any:
    """`value` as a JSON answer can hold it: every number JSON cannot hold (NaN, Infinity) written as the literal the
    client sent, and every lone surrogate in a string or a key, which UTF-8 cannot encode, as U+FFFD.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, str):
        return replace_lone_surrogates(value)
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


class RepairRoute(APIRoute):
    """The repair routes' class. Only the repair routes use it: the app gets no exception handler or middleware, and no
    existing endpoint changes.

    The role check first. A run route declares its token check as a dependency (spec 7), but FastAPI reads and parses
    a JSON body before it resolves dependencies, so a token-less request whose body is not JSON would get FastAPI's 422
    (or 400) and no `repair auth failed` line. The route runs its own check before FastAPI's handler, so the coded 401
    or 503 comes before anything else and the body of a refused request is never read. The dependency then checks
    again, and passes.

    FastAPI's own 422 for a body that fails its model, also when the body holds NaN, Infinity or a lone surrogate.
    json.loads accepts them and the models refuse them (allow_inf_nan=False; pydantic refuses a lone surrogate in a
    field with any rule), but FastAPI's 422 echoes each refused value as its `input`, and Starlette's JSONResponse can
    write neither a non-finite number nor a lone surrogate, so the 422 would become a 500. Here such a number is echoed
    as its JSON literal, a string, and a lone surrogate as U+FFFD; every other part of the 422 is FastAPI's.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        # The route's own dependencies, where each run route declares its check.
        requirements = [
            found
            for found in (requirement_of(depends.dependency) for depends in self.dependencies)
            if found is not None
        ]

        async def route_handler(request: Request) -> Response:
            for role, endpoint in requirements:
                verify_request(role, endpoint, request)
            try:
                return await handler(request)
            except RequestValidationError as error:
                errors = [_json_safe(item) for item in error.errors()]
                raise RequestValidationError(errors, body=error.body) from error

        return route_handler


router = APIRouter(tags=["repair"], route_class=RepairRoute)

# The scheduler's bootstrap intent (dispatcher/scheduler/loop.py, _is_bootstrap_intent): its run is not an explore.
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
# What a register of a known run_id must repeat: a second register can never change them, the budget included.
IMMUTABLE_RUN_FIELDS = ("project_id", "intent_id", "worker", "cli_type", "execution", "budget_seconds")
MAX_CURSOR_CHARS = 256


class RepairError(HTTPException):
    """An error of the repair endpoints: {"detail": {"code": code, "message": message, ...extra}}."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(status, {"code": code, "message": message, **extra})
        self.code = code


def repair_error(status: int, code: str, message: str, **extra: Any) -> RepairError:
    return RepairError(status, code, message, **extra)


def _run_not_found() -> RepairError:
    return repair_error(404, "run_not_found", "Run not found")


# --------------------------------------------------------------------------- PUT /repair/runs/{run_id}: register


@router.put(
    "/repair/runs/{run_id}",
    response_model=RunRegisterResponse,
    dependencies=[Depends(require_dispatcher("runs.register"))],
)
def put_run(run_id: str, body: RunRegisterRequest, response: Response) -> dict[str, Any]:
    """Register a run (201), or, for a known run_id, fill its session id and locator (200)."""
    if not valid_run_id(run_id):
        raise repair_error(
            422, "bad_run_id", "run_id must be 8 to 64 of A-Z a-z 0-9 _ . -, starting with a letter or digit"
        )
    with immediate_transaction() as conn:
        now = server_now()
        agent_seen = agent_last_seen()
        sweep(conn, now, agent_seen)
        known = find_run(conn, run_id)
        created = known is None
        if known is not None:
            refusal = _fill_known_run(conn, known, body, now)
        else:
            refusal = _new_run_refusal(conn, body, now)
            if refusal is None:
                _supersede_open_runs(conn, run_id, body, now, agent_seen)
                insert_run(conn, run_id, body, now)
                log_after_commit(
                    "repair run registered run=%s project=%s intent=%s worker=%s cli=%s execution=%s budget=%ss"
                    " session=%s",
                    run_id,
                    body.project_id,
                    body.intent_id,
                    loggable_name(body.worker),
                    body.cli_type,
                    body.execution,
                    body.budget_seconds,
                    "yes" if body.session_id is not None else "no",
                )
        run = find_run(conn, run_id) if refusal is None else None
        view = run_view(conn, run, now) if run is not None else None
    if refusal is not None:
        raise refusal
    if view is None:
        raise _run_not_found()
    response.status_code = 201 if created else 200
    return {"created": created, "run": view}


def _fill_known_run(
    conn: sqlite3.Connection, run: sqlite3.Row, body: RunRegisterRequest, now: datetime
) -> RepairError | None:
    """A register of a known run_id: it must repeat the run's fields, and it fills a missing session id and locator.
    This works on an ended run too, so a session id found only at the end, as in local mode, is still recorded.
    started_seconds_ago is ignored here: a timed-out run's session fill arrives after its budget has passed.
    """
    conflicts = [name for name in IMMUTABLE_RUN_FIELDS if run[name] != getattr(body, name)]
    if conflicts:
        return repair_error(409, "run_conflict", "this run_id is registered with other values", fields=conflicts)
    if body.session_id is not None and run["session_id"] is not None and body.session_id != run["session_id"]:
        return repair_error(409, "session_conflict", "this run already has another session id")
    filled = []
    if body.session_id is not None and run["session_id"] is None:
        filled.append("session_id")
    if body.locator and run["locator_json"] == "{}":
        filled.append("locator")
    if filled:
        conn.execute(
            "UPDATE repair_runs SET session_id = COALESCE(session_id, ?),"
            " locator_json = CASE WHEN locator_json = '{}' THEN ? ELSE locator_json END, updated_at = ?"
            " WHERE run_id = ?",
            (body.session_id, json.dumps(body.locator, ensure_ascii=False), format_time(now), run["run_id"]),
        )
        log_after_commit("repair run updated run=%s fields=%s", run["run_id"], ",".join(filled))
    return None


def _new_run_refusal(conn: sqlite3.Connection, body: RunRegisterRequest, now: datetime) -> RepairError | None:
    """Why a new run is refused, or None. Register only reads the claim: it never expires workers or writes the
    intent. The scheduler claims before it submits the task, so a normal register passes.
    """
    if body.started_seconds_ago > body.budget_seconds:
        # Checked before anything is computed from it.
        return repair_error(
            422, "bad_started_seconds_ago", "started_seconds_ago must not exceed budget_seconds for a new run"
        )
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (body.project_id,)).fetchone()
    if project is None:
        return repair_error(404, "project_not_found", "Project not found")
    if project["status"] != "active":
        return repair_error(403, "project_inactive", f"Project is {project['status']}")
    if not project_repair_enabled(project):
        return repair_error(409, "repair_disabled", "Repair is off for this project")
    intent = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?", (body.intent_id, body.project_id)
    ).fetchone()
    if intent is None:
        return repair_error(404, "intent_not_found", "Intent not found")
    if intent["to_fact_id"] is not None:
        return repair_error(409, "intent_concluded", "Intent already concluded")
    if _is_bootstrap_intent(conn, intent):
        return repair_error(409, "not_explore", "the bootstrap intent's run is not an explore")
    lapse = repair_lapse_seconds(get_intent_timeout(conn))
    if intent["worker"] not in (None, body.worker) or seconds_since(intent["last_heartbeat_at"], now) > lapse:
        return repair_error(409, "not_claimed", "the intent is not claimed by this worker")
    return None


def _is_bootstrap_intent(conn: sqlite3.Connection, intent: sqlite3.Row) -> bool:
    """The scheduler's four-condition test: description and creator, sources exactly ["origin"], not concluded."""
    if (
        intent["description"] != BOOTSTRAP_INTENT_DESCRIPTION
        or intent["creator"] != BOOTSTRAP_INTENT_CREATOR
        or intent["to_fact_id"] is not None
    ):
        return False
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (intent["id"], intent["project_id"]),
    ).fetchall()
    return [source["fact_id"] for source in sources] == ["origin"]


def _supersede_open_runs(
    conn: sqlite3.Connection, run_id: str, body: RunRegisterRequest, now: datetime, agent_seen: datetime | None
) -> None:
    """End the open run of the same project, intent and worker, if any: the worker has started a new attempt. When it
    stopped is unknown, so its window is settled with nothing recorded, unless a late /end says it was alive later.
    """
    rows = conn.execute(
        "SELECT run_id FROM repair_runs WHERE project_id = ? AND intent_id = ? AND worker = ? AND ended_at IS NULL",
        (body.project_id, body.intent_id, body.worker),
    ).fetchall()
    for row in rows:
        if not end_open_run(conn, row["run_id"], "superseded", last_alive_at=None, fact_id=None, now=now):
            continue
        log_after_commit(
            "repair run superseded run=%s by=%s project=%s intent=%s worker=%s",
            row["run_id"],
            run_id,
            body.project_id,
            body.intent_id,
            loggable_name(body.worker),
        )
        superseded = find_run(conn, row["run_id"])
        if superseded is not None:
            settle_window(conn, superseded, now, agent_seen)


# --------------------------------------------------------------------------- POST /repair/runs/{run_id}/end


@router.post(
    "/repair/runs/{run_id}/end",
    response_model=RunEndResponse,
    dependencies=[Depends(require_dispatcher("runs.end"))],
)
def post_run_end(run_id: str, body: RunEndRequest) -> dict[str, Any]:
    """The dispatcher's /end: once when the execute process exits, once with the task's outcome. It does not check the
    switch, and it sweeps after the ending.
    """
    with immediate_transaction() as conn:
        now = server_now()
        run = end_run(conn, run_id, body, now=now, agent_seen_before=agent_last_seen())
        view = run_view(conn, run, now) if run is not None else None
    if view is None:
        raise _run_not_found()
    return {"run": view}


# --------------------------------------------------------------------------- GET /repair/runs


@router.get(
    "/repair/runs",
    response_model=RunListResponse,
    dependencies=[Depends(require_agent("runs.list"))],
)
def list_runs(
    project_id: str | None = None,
    include_ended: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The agent's poll: the live runs of active, switched-on projects, oldest start first. With include_ended, every
    run, newest registration first, a page at a time, for evaluation tools, which record no poll.

    The sweep commits first, with the previous poll's time; the default listing then records this poll, and the list
    is read in a second, read-only transaction, so the write lock is never held while views are built.
    """
    after = None
    if cursor is not None:
        if not include_ended:
            raise repair_error(422, "bad_cursor", "a cursor is used only with include_ended=true")
        after = _decode_cursor(cursor)
        if after is None:
            raise repair_error(422, "bad_cursor", "the cursor cannot be read; use a next_cursor of this server")
    with immediate_transaction() as conn:
        now = server_now()
        sweep(conn, now, agent_last_seen())
    if not include_ended:
        record_agent_poll(now)
    with db.get_conn() as conn:
        conn.execute("BEGIN")  # one snapshot for the list and its views
        if include_ended:
            rows = runs_page(conn, project_id, limit + 1, after)
        else:
            rows = live_runs(conn, project_id, limit)
        views = [run_view(conn, row, now) for row in rows[:limit]]
    next_cursor = _encode_cursor(rows[limit - 1]) if include_ended and len(rows) > limit else None
    return {
        "server_time": format_time(now),
        "shadow": SHADOW,
        "slice_seconds": SLICE_SECONDS,
        "review_rules": review_rules(),
        "runs": views,
        "next_cursor": next_cursor,
    }


def _encode_cursor(row: sqlite3.Row) -> str:
    raw = json.dumps([row["registered_at"], row["run_id"]], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    """The (registered_at, run_id) a next_cursor of this server holds, or None for anything else."""
    if not 0 < len(cursor) <= MAX_CURSOR_CHARS or not cursor.isascii():
        return None
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(item, str) for item in value)):
        return None
    registered_at, run_id = value
    parsed = parse_time(registered_at)
    if parsed is None or format_time(parsed) != registered_at or not valid_run_id(run_id):
        return None
    return registered_at, run_id


# --------------------------------------------------------------------------- GET /repair/runs/{run_id}


@router.get(
    "/repair/runs/{run_id}",
    response_model=RunDetailView,
    dependencies=[Depends(require_agent("runs.get"))],
)
def get_run(run_id: str) -> dict[str, Any]:
    """One run, ended or not, with its full stored reviews: digests and raw output included. Records no poll."""
    with immediate_transaction() as conn:
        now = server_now()
        sweep(conn, now, agent_last_seen())
        run = find_run(conn, run_id)
        view = run_view(conn, run, now, full_reviews=True) if run is not None else None
    if view is None:
        raise _run_not_found()
    return view


# --------------------------------------------------------------------------- POST /repair/runs/{run_id}/reviews


@router.post(
    "/repair/runs/{run_id}/reviews",
    response_model=ReviewResponse,
    status_code=201,
    dependencies=[Depends(require_agent("runs.review"))],
)
def post_review(run_id: str, body: ReviewRequest, response: Response) -> dict[str, Any]:
    """Record the agent's review of the run's window as a shadow repair_review issue (201), or answer a retry of a
    recorded review with it (200). Recorded, never applied.
    """
    refusal: RepairError | None = None
    answer: dict[str, Any] | None = None
    with immediate_transaction() as conn:
        now = server_now()
        sweep(conn, now, agent_last_seen())
        run = find_run(conn, run_id)
        if run is None:
            refusal = _run_not_found()
        else:
            stored = conn.execute(
                "SELECT * FROM repair_reviews WHERE run_id = ? AND window_no = ?", (run_id, body.window)
            ).fetchone()
            if stored is not None and stored["source"] == "agent" and stored["review_id"] == body.review_id:
                answer = review_body(conn, stored, created=False)
            else:
                refusal = _review_refusal(run, stored, body, now)
                if refusal is None:
                    review, _issue = record_agent_review(conn, run, body, now)
                    answer = review_body(conn, review, created=True)
    if refusal is not None:
        if refusal.status_code == 409:
            LOG.info("repair review refused run=%s code=%s", run_id, refusal.code)
        raise refusal
    if answer is None:
        raise _run_not_found()
    response.status_code = 201 if answer["created"] else 200
    return answer


def _review_refusal(
    run: sqlite3.Row, stored: sqlite3.Row | None, body: ReviewRequest, now: datetime
) -> RepairError | None:
    """Checks 3 to 13 of a review post, in order (1, an unknown run, and 2, a replay, come before)."""
    if stored is not None:
        if stored["source"] == "agent":
            return repair_error(
                409, "already_reviewed", "this window already has another review", review=review_record(stored)
            )
        return repair_error(
            409, "window_closed", "this window was recorded as not reviewed", review=review_record(stored)
        )
    if not run["project_repair_enabled"]:
        return repair_error(409, "repair_disabled", "Repair is off for this project")
    if run["ended_at"] is not None:
        return repair_error(409, "run_ended", "the run has ended", end_reason=run["end_reason"])
    window = current_window(run)
    if body.window != window.number:
        return repair_error(409, "wrong_window", "this is not the run's current window")
    if not window.reviewable:
        return repair_error(409, "not_reviewable", "this run is never reviewed")
    if now < window.opens:
        return repair_error(
            409, "too_early", "the review window has not opened", review_opens_at=format_time(window.opens)
        )
    if now >= window.accept_until:
        return repair_error(409, "window_closed", "the review window has closed")
    started = parse_time(run["started_at"])
    if started is None:
        raise ValueError("a repair run holds a start time that is not in the server's format")
    digest_at = started + timedelta(seconds=body.digest_elapsed_seconds)
    margin = timedelta(seconds=DIGEST_TIME_MARGIN_SECONDS)
    if digest_at < window.opens - margin:
        return repair_error(409, "digest_too_early", "the digest was taken before the review window opened")
    if digest_at > window.closes + margin:
        return repair_error(
            409,
            "digest_too_late",
            "the digest was taken after the review window closed",
            review_closes_at=format_time(window.closes),
        )
    if body.expected_version is not None and body.expected_version != run["version"]:
        return repair_error(409, "stale_version", "the run's version has changed", version=run["version"])
    return None


# --------------------------------------------------------------------------- the switch and the settings (open)


@router.put("/projects/{project_id}/repair", response_model=ProjectMeta)
def put_project_repair(project_id: str, body: UpdateProjectRepairRequest, request: Request) -> ProjectMeta:
    """Turn the project's Repair switch on or off, in any project status. Writes, and logs at WARNING, only a change;
    registered runs go on and end normally.
    """
    with immediate_transaction() as conn:
        row = get_project_or_404(conn, project_id)
        changed = project_repair_enabled(row) != body.enabled
        if changed:
            conn.execute("UPDATE projects SET repair_enabled = ? WHERE id = ?", (int(body.enabled), project_id))
            row = get_project_or_404(conn, project_id)
        meta = project_meta_from_row(row)
    # Logged once the change is committed.
    if changed:
        log_repair_switch(project_id, body.enabled, request)
    return meta


@router.get("/repair/settings", response_model=RepairSettingsView)
def get_repair_settings() -> dict[str, Any]:
    """The shadow settings, read-only constants, with which roles have a token and when the agent last polled."""
    seen = agent_last_seen()
    return {
        **settings_constants(),
        "roles_configured": roles_configured(),
        "agent_last_seen_at": format_time(seen) if seen is not None else None,
    }
