"""Repair layer (shadow mode): the run endpoints over HTTP, as the dispatcher and the repair agent call them.

SV16-SV22, SV25 (spec 6.1, 6.2, 5.2): registering a run, adding its session id later, the conflicts and refusals of a
register, the bootstrap intent, the start time, superseding, and the dispatcher's two /end calls, late ones included.
SV28-SV31 (spec 6.3, 6.4, 5.4): the agent's listing, the evaluation listing with its cursor, and the review clock.
SV32-SV36 (spec 6.5, 4.5): reviews, recorded as shadow repair_review issues and never applied.
Spec 6.0's body rules: a lone surrogate becomes U+FFFD, and an unknown field is ignored however deeply it is nested.

The server's clock is frozen and moves only when a test moves it; the intent's side (claims and heartbeats) goes
through today's endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_repair_server import (
    ACCEPT_UNTIL,
    BUDGET,
    CLOSES,
    OPENS,
    Clock,
    at,
    heartbeat,
    new_project,
    not_reviewed_detail,
    project_intents,
    traced_sql,
)
from test_repair_server_support import (
    AGENT,
    DISPATCHER,
    LOCATOR,
    REPAIR_LOGGER,
    REVIEW_RULES,
    RUN_VIEW_KEYS,
    WINDOW_KEYS,
    advance,
    capture_repair_logs,
    coded,
    exit_end,
    freeze_clock,
    get_run,
    issue_details,
    outcome_end,
    poll,
    post_review,
    put_run,
    records,
    register,
    repair_lines,
    review,
    review_body,
    run_body,
    serve,
    set_switch,
    table,
    validation_locs,
)

from cairn.server import db

RUN = "run-0001"
SESSION = "0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e44"
OTHER_SESSION = "0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e45"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return freeze_clock(monkeypatch)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    yield from serve(tmp_path, monkeypatch)


def _row(run_id: str) -> dict[str, Any]:
    rows = table("repair_runs", "WHERE run_id = ?", (run_id,))
    assert len(rows) == 1, run_id
    return rows[0]


def _claims(count: int) -> tuple[tuple[str, str], ...]:
    return tuple((f"i{number:03d}", f"w{number}") for number in range(1, count + 1))


def _claim(api: TestClient, project_id: str, claims: tuple[tuple[str, str], ...]) -> None:
    for intent_id, worker in claims:
        heartbeat(api, project_id, intent_id, worker)


def _conclude(api: TestClient, project_id: str, intent_id: str, worker: str) -> str:
    response = api.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude", json={"worker": worker, "description": "an answer"}
    )
    assert response.status_code == 200, response.text
    return response.json()["fact"]["id"]


def _raw_put(api: TestClient, run_id: str, body: dict[str, Any], field: str, literal: str) -> Any:
    """A register whose `field` is a JSON literal that json.dumps would not write, such as Infinity."""
    text = json.dumps({**body, field: "__literal__"}).replace('"__literal__"', literal)
    return api.put(f"/repair/runs/{run_id}", content=text, headers={**DISPATCHER, "Content-Type": "application/json"})


# --------------------------------------------------------------------------- SV16-SV21: register (spec 6.1)


# SV16
def test_register_creates_once_and_adds_the_session_id_later(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api, intents=3)
    claims = _claims(3)
    _claim(api, project_id, claims)
    caplog.clear()

    response = put_run(api, RUN, run_body(project_id))
    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    run = response.json()["run"]
    assert set(run) == RUN_VIEW_KEYS and set(run["window"]) == WINDOW_KEYS
    assert run == {
        "run_id": RUN,
        "project_id": project_id,
        "intent_id": "i001",
        "intent": {"description": "investigate 1", "from": ["origin"]},
        "worker": "w1",
        "cli_type": "codex",
        "execution": "container",
        "session_id": None,
        "locator": LOCATOR,
        "budget_seconds": BUDGET,
        "started_at": at(0),
        "deadline_at": at(BUDGET),
        "version": 1,
        "slices_used": 0,
        "state": "running",
        "elapsed_seconds": 0,
        "remaining_seconds": BUDGET,
        "window": {
            "number": 1,
            "start_at": at(0),
            "end_at": at(BUDGET),
            "seconds": BUDGET,
            "review_opens_at": at(OPENS),
            "review_closes_at": at(CLOSES),
            "review_accept_until": at(ACCEPT_UNTIL),
            "reviewable": True,
            "review_status": "pending",
        },
        "reviews": [],
        "registered_at": at(0),
        "updated_at": at(0),
        "ended_at": None,
        "end_reason": None,
        "last_alive_at": None,
        "outcome": None,
        "fact_id": None,
    }
    stored = _row(RUN)
    assert (stored["settled_window"], stored["reviewable"], json.loads(stored["locator_json"])) == (0, 1, LOCATOR)
    assert repair_lines(caplog) == [
        f"repair run registered run={RUN} project={project_id} intent=i001 worker=w1 cli=codex execution=container"
        " budget=1800s session=no"
    ]

    # A retried register, its started_seconds_ago computed afresh: 200 with the run as it is, nothing written.
    clock.to(1)
    caplog.clear()
    again = put_run(api, RUN, run_body(project_id, started_seconds_ago=1.004))
    assert (again.status_code, again.json()["created"]) == (200, False)
    assert again.json()["run"] == {**run, "elapsed_seconds": 1, "remaining_seconds": BUDGET - 1}
    assert _row(RUN) == stored
    assert repair_lines(caplog) == []

    # The session id, found later, comes with a second full PUT of the accepted body: filled, the start unchanged.
    clock.to(2)
    filled = put_run(api, RUN, run_body(project_id, session_id=SESSION, started_seconds_ago=2.004))
    assert (filled.status_code, filled.json()["created"]) == (200, False)
    assert filled.json()["run"] == {
        **run,
        "session_id": SESSION,
        "elapsed_seconds": 2,
        "remaining_seconds": BUDGET - 2,
        "updated_at": at(2),
    }
    assert repair_lines(caplog) == [f"repair run updated run={RUN} fields=session_id"]
    # The same id again fills nothing.
    clock.to(3)
    caplog.clear()
    same = put_run(api, RUN, run_body(project_id, session_id=SESSION, started_seconds_ago=3.004))
    assert (same.status_code, same.json()["run"]["updated_at"]) == (200, at(2))
    assert repair_lines(caplog) == []

    # A run registered without a locator gets one from a later PUT, and only then.
    second = register(api, "run-0002", project_id, "i002", "w2", locator={})
    assert (second["locator"], _row("run-0002")["locator_json"]) == ({}, "{}")
    clock.to(4)
    caplog.clear()
    located = put_run(api, "run-0002", run_body(project_id, "i002", "w2"))
    assert (located.status_code, located.json()["run"]["locator"]) == (200, LOCATOR)
    assert repair_lines(caplog) == ["repair run updated run=run-0002 fields=locator"]
    kept = put_run(api, "run-0002", run_body(project_id, "i002", "w2", session_id=OTHER_SESSION, locator={"a": "b"}))
    assert (kept.json()["run"]["locator"], kept.json()["run"]["session_id"]) == (LOCATOR, OTHER_SESSION)

    # The window follows 5.4 on the server's clock: a budget of 1001 s, registered 2.4 s after its start.
    clock.to(10)
    timed = register(api, "run-0003", project_id, "i003", "w3", budget_seconds=1001, started_seconds_ago=2.4)
    assert (timed["started_at"], timed["deadline_at"], timed["registered_at"]) == (at(8), at(1009), at(10))
    assert timed["window"] == {
        "number": 1,
        "start_at": at(8),
        "end_at": at(1009),
        "seconds": 1001,
        "review_opens_at": at(8 + 750),  # floor(0.75 * 1001)
        "review_closes_at": at(8 + 800),  # floor(0.80 * 1001)
        "review_accept_until": at(1009 - 10),
        "reviewable": True,
        "review_status": "pending",
    }
    assert (timed["elapsed_seconds"], timed["remaining_seconds"]) == (2, 999)

    # The session fill of a timed-out run: run-0003 times out with no session id, and the id, read only at the end
    # as in local mode, arrives after the budget has passed. started_seconds_ago is then beyond the budget, and ignored.
    advance(api, clock, project_id, 1009, claims)
    clock.to(1010)
    ended = exit_end(api, "run-0003", exited_seconds_ago=1.0, timed_out=True, returncode=124)
    assert (ended["state"], ended["ended_at"], ended["session_id"]) == ("ended", at(1010), None)
    clock.to(1015)
    late = put_run(
        api,
        "run-0003",
        run_body(project_id, "i003", "w3", budget_seconds=1001, session_id=SESSION, started_seconds_ago=1009.4),
    )
    assert late.status_code == 200, late.text
    filled_late = late.json()["run"]
    assert (filled_late["session_id"], filled_late["started_at"], filled_late["deadline_at"]) == (
        SESSION,
        at(8),
        at(1009),
    )
    assert (filled_late["state"], filled_late["ended_at"], filled_late["end_reason"]) == (
        "ended",
        at(1010),
        "dispatcher",
    )


# SV17
def test_register_conflicts(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    _claim(api, project_id, _claims(2))
    register(api, RUN, project_id, session_id=SESSION)
    register(api, "run-0002", project_id, "i002", "w2")
    before = table("repair_runs")

    # Every field of the run must be repeated: a second register can never move the run, nor raise its budget.
    for field, value in (
        ("project_id", "proj_999"),
        ("intent_id", "i002"),
        ("worker", "w2"),
        ("cli_type", "pi"),
        ("execution", "local"),
        ("budget_seconds", 3600),
    ):
        body = {**run_body(project_id, session_id=SESSION), field: value}
        detail = coded(put_run(api, RUN, body), 409, "run_conflict")
        assert detail["fields"] == [field], field
    detail = coded(
        put_run(api, RUN, run_body(project_id, "i002", "w2", budget_seconds=60, session_id=SESSION)),
        409,
        "run_conflict",
    )
    assert detail["fields"] == ["intent_id", "worker", "budget_seconds"]

    # Another session id than the stored one.
    coded(put_run(api, RUN, run_body(project_id, session_id=OTHER_SESSION)), 409, "session_conflict")
    # A refused register fills nothing: run-0002 has no session id yet, and keeps none.
    coded(
        put_run(api, "run-0002", run_body(project_id, "i002", "w2", budget_seconds=60, session_id=SESSION)),
        409,
        "run_conflict",
    )
    assert table("repair_runs") == before
    # A register with no session id never conflicts with a stored one.
    assert put_run(api, RUN, run_body(project_id)).status_code == 200
    assert _row(RUN)["session_id"] == SESSION


# SV18
def test_register_refusals(api: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = new_project(api, intents=6)
    heartbeat(api, project_id)  # i001 by w1

    # The switch is off.
    set_switch(api, project_id, False)
    coded(put_run(api, RUN, run_body(project_id)), 409, "repair_disabled")
    set_switch(api, project_id, True)
    # Another worker holds the claim; an intent never claimed.
    coded(put_run(api, RUN, run_body(project_id, worker="w2")), 409, "not_claimed")
    coded(put_run(api, RUN, run_body(project_id, "i002", "w2")), 409, "not_claimed")
    # A concluded intent.
    heartbeat(api, project_id, "i003", "w3")
    _conclude(api, project_id, "i003", "w3")
    coded(put_run(api, RUN, run_body(project_id, "i003", "w3")), 409, "intent_concluded")
    # No such project or intent.
    coded(put_run(api, RUN, run_body("proj_999")), 404, "project_not_found")
    coded(put_run(api, RUN, run_body(project_id, "i999")), 404, "intent_not_found")
    # A stopped or completed project.
    stopped = new_project(api)
    heartbeat(api, stopped)
    assert api.put(f"/projects/{stopped}/status", json={"status": "stopped"}).status_code == 200
    detail = coded(put_run(api, RUN, run_body(stopped)), 403, "project_inactive")
    assert detail["message"] == "Project is stopped"
    completed = new_project(api)
    response = api.post(
        f"/projects/{completed}/complete", json={"from": ["origin"], "description": "done", "worker": "reasoner"}
    )
    assert response.status_code == 200, response.text
    coded(put_run(api, RUN, run_body(completed)), 403, "project_inactive")

    # The run id rule, as a full match: a trailing newline is refused too.
    for bad in (
        "abcdefgh%0A",
        "abcdefg",
        "a" * 65,
        "-abcdefgh",
        ".abcdefgh",
        "abcd%20efgh",
        "abcdefgh%00",
        "%0Aabcdefgh",
    ):
        coded(put_run(api, bad, run_body(project_id)), 422, "bad_run_id")
    # A new run's started_seconds_ago beyond its budget.
    coded(put_run(api, RUN, run_body(project_id, started_seconds_ago=BUDGET + 0.5)), 422, "bad_started_seconds_ago")
    # Session ids that fail their rule, one ending in a newline included, and numbers that are not finite.
    for session_id in ("0199a1b2-6a3f\n", "a..b", "-abc", ".abc", "a" * 129, "a/b", "", "a b"):
        assert validation_locs(put_run(api, RUN, run_body(project_id, session_id=session_id))) == [
            ["body", "session_id"]
        ], session_id
    for literal in ("Infinity", "-Infinity", "NaN"):
        assert validation_locs(_raw_put(api, RUN, run_body(project_id), "started_seconds_ago", literal)) == [
            ["body", "started_seconds_ago"]
        ], literal
    assert validation_locs(_raw_put(api, RUN, run_body(project_id), "budget_seconds", "Infinity")) == [
        ["body", "budget_seconds"]
    ]
    assert validation_locs(put_run(api, RUN, run_body(project_id, started_seconds_ago=-1))) == [
        ["body", "started_seconds_ago"]
    ]
    # Nothing was registered by any refusal.
    assert table("repair_runs") == []

    # The claim may be NULL or the worker's, and its heartbeat at most repair_lapse_seconds old (60 s here).
    clock.to(100)
    heartbeat(api, project_id, "i004", "w4")
    clock.to(161)
    coded(put_run(api, RUN, run_body(project_id, "i004", "w4")), 409, "not_claimed")
    heartbeat(api, project_id, "i005", "w5")
    clock.to(221)
    assert put_run(api, RUN, run_body(project_id, "i005", "w5")).status_code == 201  # exactly 60 s old

    # Register only reads the claim. A heartbeat 20 s old: today's project read expires the claim (older than
    # intent_timeout), and a register then still passes, without expiring workers or writing the intent.
    heartbeat(api, project_id, "i006", "w6")
    clock.to(241)
    assert next(i for i in project_intents(api, project_id) if i["id"] == "i006")["worker"] is None
    with traced_sql(monkeypatch) as statements:
        assert put_run(api, "run-0002", run_body(project_id, "i006", "w6")).status_code == 201
    assert not any("intents" in statement and statement.startswith("UPDATE") for statement in statements)
    with db.get_conn() as conn:
        intent = conn.execute("SELECT worker, last_heartbeat_at FROM intents WHERE id = 'i006'").fetchone()
    assert (intent["worker"], intent["last_heartbeat_at"]) == (None, at(221))


# SV19
def test_the_bootstrap_intent_is_never_registered(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api)  # i001, an explore intent
    heartbeat(api, project_id)
    fact_id = _conclude(api, project_id, "i001", "w1")

    def intent(description: str, creator: str, sources: list[str]) -> str:
        response = api.post(
            f"/projects/{project_id}/intents",
            json={"from": sources, "description": description, "creator": creator, "worker": None},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    # The scheduler's bootstrap intent: description, creator, sources exactly ["origin"], not concluded.
    bootstrap = intent("bootstrap", "dispatcher.bootstrap", ["origin"])
    heartbeat(api, project_id, bootstrap, "w1")
    coded(put_run(api, "run-0001", run_body(project_id, bootstrap)), 409, "not_explore")
    # Explore intents that share only some of the four conditions are registered.
    partial = [
        intent("bootstrap", "dispatcher.bootstrap", [fact_id]),
        intent("bootstrap", "dispatcher.bootstrap", ["origin", fact_id]),
        intent("bootstrap", "reasoner", ["origin"]),
        intent("explore the origin", "dispatcher.bootstrap", ["origin"]),
        intent("Bootstrap", "dispatcher.bootstrap", ["origin"]),
        intent("bootstrap", "reasoner", [fact_id]),
    ]
    for number, intent_id in enumerate(partial, start=2):
        heartbeat(api, project_id, intent_id, f"w{number}")
        run = register(api, f"run-{number:04d}", project_id, intent_id, f"w{number}")
        assert run["intent_id"] == intent_id
    assert [row["intent_id"] for row in table("repair_runs")] == partial


# SV20
def test_started_seconds_ago_sets_the_start_once(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=4)
    clock.to(100)
    _claim(api, project_id, _claims(4))
    first = register(api, RUN, project_id, started_seconds_ago=2.4)
    assert (first["started_at"], first["deadline_at"], first["registered_at"]) == (at(98), at(98 + BUDGET), at(100))

    # A retry 5 s later carries a value computed afresh; the start stays where the first register put it.
    clock.to(105)
    retried = put_run(api, RUN, run_body(project_id, started_seconds_ago=7.4))
    assert (retried.status_code, retried.json()["run"]["started_at"]) == (200, at(98))
    # On a known run id the value is ignored, and so not range-checked.
    ignored = put_run(api, RUN, run_body(project_id, started_seconds_ago=10.0 * BUDGET))
    assert (ignored.status_code, ignored.json()["run"]["started_at"]) == (200, at(98))
    assert (_row(RUN)["started_at"], _row(RUN)["deadline_at"]) == (at(98), at(98 + BUDGET))

    # Rounded to whole seconds (Python's round), and at most the budget for a new run.
    for run_id, intent_id, worker, ago, start in (
        ("run-0002", "i002", "w2", 2.5, 103),
        ("run-0003", "i003", "w3", 3.6, 101),
        ("run-0004", "i004", "w4", float(BUDGET), 105 - BUDGET),
    ):
        run = register(api, run_id, project_id, intent_id, worker, started_seconds_ago=ago)
        assert (run["started_at"], run["deadline_at"]) == (at(start), at(start + BUDGET)), run_id
    assert get_run(api, "run-0004")["remaining_seconds"] == 0


# SV21
def test_a_new_run_supersedes_the_open_run_of_the_same_intent_and_worker(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api, intents=2)
    claims = (("i001", "w1"), ("i002", "w1"))  # one worker holding two intents
    _claim(api, project_id, claims)
    register(api, RUN, project_id, "i001", "w1")
    register(api, "run-0002", project_id, "i002", "w1")
    advance(api, clock, project_id, 600, claims)
    caplog.clear()

    newer = register(api, "run-0003", project_id, "i001", "w1")
    assert (newer["started_at"], newer["state"]) == (at(600), "running")
    old = get_run(api, RUN)
    assert (old["state"], old["ended_at"], old["end_reason"], old["last_alive_at"], old["fact_id"]) == (
        "ended",
        at(600),
        "superseded",
        None,  # when the old process stopped is unknown
        None,
    )
    assert (old["window"]["review_status"], _row(RUN)["settled_window"]) == ("closed", 1)
    assert repair_lines(caplog) == [
        f"repair run superseded run={RUN} by=run-0003 project={project_id} intent=i001 worker=w1",
        f"repair run registered run=run-0003 project={project_id} intent=i001 worker=w1 cli=codex"
        " execution=container budget=1800s session=no",
    ]
    # The same worker's run of its other intent goes on; the agent sees the two live runs.
    assert get_run(api, "run-0002")["ended_at"] is None
    assert [run["run_id"] for run in poll(api)["runs"]] == ["run-0002", "run-0003"]
    # The superseded run is history: a repeat of its register answers it as it is, ended.
    repeat = put_run(api, RUN, run_body(project_id))
    assert (repeat.status_code, repeat.json()["run"]["end_reason"]) == (200, "superseded")

    # The partial unique index: one open row per (project, intent, worker); ended rows are history.
    insert = (
        "INSERT INTO repair_runs (run_id, project_id, intent_id, worker, cli_type, execution, budget_seconds,"
        " started_at, deadline_at, registered_at, updated_at, ended_at)"
        " VALUES ('run-0009', ?, 'i001', 'w1', 'codex', 'local', 60, 'x', 'x', 'x', 'x', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.get_conn() as conn:
        conn.execute(insert, (project_id, None))
    with db.get_conn() as conn:
        conn.execute(insert, (project_id, "x"))
        conn.execute("DELETE FROM repair_runs WHERE run_id = 'run-0009'")
    with db.get_conn() as conn:
        indexes = {row["name"]: row["unique"] for row in conn.execute("PRAGMA index_list(repair_runs)")}
    assert indexes["repair_runs_open_key"] == 1


# --------------------------------------------------------------------------- SV22, SV25: /end (spec 6.2, 5.2)


# SV22
def test_end_records_the_outcome_once_and_is_idempotent(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api, intents=2)
    claims = _claims(2)
    _claim(api, project_id, claims)
    register(api, RUN, project_id)
    register(api, "run-0002", project_id, "i002", "w2")
    advance(api, clock, project_id, 600, claims)
    caplog.clear()

    clock.to(601)
    ended = exit_end(api, RUN, exited_seconds_ago=0.6)
    assert (ended["state"], ended["ended_at"], ended["end_reason"], ended["last_alive_at"]) == (
        "ended",
        at(601),
        "dispatcher",
        at(600),
    )
    assert (ended["outcome"], ended["fact_id"], ended["elapsed_seconds"], ended["remaining_seconds"]) == (
        None,
        None,
        None,
        None,
    )
    stored = _row(RUN)
    assert json.loads(stored["outcome_json"]) == {"timed_out": False, "cancelled": False, "returncode": 0}
    assert repair_lines(caplog, "repair run ended") == [
        f"repair run ended run={RUN} project={project_id} intent=i001 reason=dispatcher outcome=None"
    ]

    # A retried exit /end changes nothing.
    clock.to(605)
    assert exit_end(api, RUN, exited_seconds_ago=4.6) == ended
    assert _row(RUN) == stored

    # The outcome /end stores the outcome once; a later one fills only empty fields.
    clock.to(610)
    finished = outcome_end(api, RUN, "success")
    assert finished == {**ended, "outcome": "success", "updated_at": at(610)}
    clock.to(615)
    assert outcome_end(api, RUN, "failed") == finished
    assert exit_end(api, RUN, exited_seconds_ago=0.0, timed_out=True, returncode=124) == finished
    assert json.loads(_row(RUN)["outcome_json"]) == {"timed_out": False, "cancelled": False, "returncode": 0}

    # /end does not check the switch, and an outcome it does not know is stored as given.
    set_switch(api, project_id, False)
    other = outcome_end(api, "run-0002", "not_a_known_outcome")
    assert (other["end_reason"], other["outcome"], other["last_alive_at"]) == (
        "dispatcher",
        "not_a_known_outcome",
        at(615),
    )

    # Unknown runs and bad bodies.
    coded(api.post("/repair/runs/run-0009/end", json={"outcome": "success"}, headers=DISPATCHER), 404, "run_not_found")
    for body in ({"outcome": "Success"}, {"outcome": "a" * 33}, {"exited_seconds_ago": 604801}, {"returncode": 1.5}):
        assert validation_locs(api.post(f"/repair/runs/{RUN}/end", json=body, headers=DISPATCHER)) == [
            ["body", next(iter(body))]
        ], body
    infinite = api.post(
        f"/repair/runs/{RUN}/end",
        content=b'{"exited_seconds_ago": Infinity}',
        headers={**DISPATCHER, "Content-Type": "application/json"},
    )
    assert validation_locs(infinite) == [["body", "exited_seconds_ago"]]
    assert outcome_end(api, RUN, "failed")["outcome"] == "success"


# SV22
def test_end_at_process_exit_then_outcome_after_todays_ending(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, RUN, project_id)
    advance(api, clock, project_id, BUDGET)

    # The execute process times out: the exit /end ends the run; the intent has no fact yet.
    clock.to(BUDGET + 1)
    exited = exit_end(api, RUN, exited_seconds_ago=1.0, timed_out=True, returncode=124)
    assert (
        exited["end_reason"],
        exited["ended_at"],
        exited["last_alive_at"],
        exited["fact_id"],
        exited["outcome"],
    ) == (
        "dispatcher",
        at(BUDGET + 1),
        at(BUDGET),
        None,
        None,
    )
    assert json.loads(_row(RUN)["outcome_json"]) == {"timed_out": True, "cancelled": False, "returncode": 124}
    # Today's conclude fallback turns the run into a fact; the outcome /end then records it with the outcome.
    clock.to(BUDGET + 10)
    fact_id = _conclude(api, project_id, "i001", "w1")
    clock.to(BUDGET + 20)
    finished = outcome_end(api, RUN, "success")
    assert (finished["outcome"], finished["fact_id"], finished["updated_at"]) == ("success", fact_id, at(BUDGET + 20))
    assert (finished["end_reason"], finished["ended_at"], finished["last_alive_at"]) == (
        "dispatcher",
        at(BUDGET + 1),
        at(BUDGET),
    )
    single = get_run(api, RUN)
    assert {key: value for key, value in single.items() if key != "reviews"} == {
        key: value for key, value in finished.items() if key != "reviews"
    }


# SV25
def test_a_late_end_restores_last_alive_and_flags_once(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api, intents=2)
    claims = _claims(2)
    _claim(api, project_id, claims)
    register(api, RUN, project_id)
    register(api, "run-0002", project_id, "i002", "w2")
    advance(api, clock, project_id, 1400, claims)
    # run-0002's heartbeats stop at 1400 s, before its window closes at 1440 s.
    advance(api, clock, project_id, 1450, claims[:1])

    # run-0001's worker starts a new attempt after the window closed: the old run is superseded, at an unknown time,
    # so nothing is recorded for it.
    register(api, "run-0003", project_id, "i001", "w1")
    assert (_row(RUN)["end_reason"], _row(RUN)["last_alive_at"], _row(RUN)["settled_window"]) == (
        "superseded",
        None,
        1,
    )
    # run-0002 lapses: the sweep ends it as claim_lost, last alive at its heartbeat, before the window closed.
    advance(api, clock, project_id, 1461, claims[:1])
    poll(api)
    assert (_row("run-0002")["end_reason"], _row("run-0002")["last_alive_at"]) == ("claim_lost", at(1400))
    assert table("repair_reviews") == []

    # The late exit /end of each says it was alive after the window closed: last_alive_at is corrected to the
    # reported exit, and the run is flagged "not reviewed" once, with the time used at its exit.
    caplog.clear()
    clock.to(1470)
    superseded = exit_end(api, RUN, exited_seconds_ago=3.0)
    assert (superseded["end_reason"], superseded["ended_at"], superseded["last_alive_at"]) == (
        "superseded",
        at(1450),
        at(1467),
    )
    lost = exit_end(api, "run-0002", exited_seconds_ago=15.0)
    assert (lost["end_reason"], lost["ended_at"], lost["last_alive_at"]) == ("claim_lost", at(1461), at(1455))
    for run_id, alive, reason in ((RUN, 1467, "superseded"), ("run-0002", 1455, "claim_lost")):
        assert issue_details(run_id) == [
            not_reviewed_detail(
                elapsed_seconds=alive,
                why="agent_seen",
                agent_last_seen_at=at(1461),
                run_ended=True,
                run_ended_at=at(alive),
                end_reason=reason,
            )
        ], run_id
        assert get_run(api, run_id)["window"]["review_status"] == "not_reviewed"
    assert repair_lines(caplog, "repair window not reviewed") == [
        f"repair window not reviewed run={RUN} project={project_id} intent=i001 window=1 why=agent_seen",
        f"repair window not reviewed run=run-0002 project={project_id} intent=i002 window=1 why=agent_seen",
    ]

    # A retried late /end fills only empty fields: no second correction and no second record; the outcome stamps.
    clock.to(1480)
    assert exit_end(api, RUN, exited_seconds_ago=0.0)["last_alive_at"] == at(1467)
    assert outcome_end(api, RUN, "failed")["outcome"] == "failed"
    assert len(table("repair_reviews", "WHERE run_id = ?", (RUN,))) == 1
    assert issue_details(RUN)[0]["outcome"] == "failed"
    assert repair_lines(caplog, "repair window not reviewed")[2:] == []


# --------------------------------------------------------------------------- SV28-SV31: listings (spec 6.3, 6.4)


# SV28
def test_list_shows_only_live_runs_of_switched_on_projects_with_server_clock_timing(
    api: TestClient, clock: Clock
) -> None:
    on = new_project(api, intents=4)
    switched_off = new_project(api)
    stopped = new_project(api)
    on_claims = _claims(4)
    _claim(api, on, on_claims)
    heartbeat(api, switched_off, "i001", "w5")
    heartbeat(api, stopped, "i001", "w6")
    register(api, "run-0001", on, "i001", "w1", started_seconds_ago=30)
    register(api, "run-0002", on, "i002", "w2")
    register(api, "run-0003", on, "i003", "w3")
    register(api, "run-0004", on, "i004", "w4")
    register(api, "run-0005", switched_off, "i001", "w5")
    register(api, "run-0006", stopped, "i001", "w6")
    exit_end(api, "run-0003")
    set_switch(api, switched_off, False)
    assert api.put(f"/projects/{stopped}/status", json={"status": "stopped"}).status_code == 200
    # An open row in a state outside LISTED_STATES (as Phase 4's detached runs will be) is not listed.
    with db.get_conn() as conn:
        conn.execute("UPDATE repair_runs SET state = 'detached' WHERE run_id = 'run-0004'")

    clock.to(600)
    _claim(api, on, on_claims)
    heartbeat(api, switched_off, "i001", "w5")
    listed = poll(api)
    assert set(listed) == {"server_time", "shadow", "slice_seconds", "review_rules", "runs", "next_cursor"}
    assert (listed["server_time"], listed["shadow"], listed["slice_seconds"], listed["next_cursor"]) == (
        at(600),
        True,
        600,
        None,
    )
    assert listed["review_rules"] == REVIEW_RULES
    # Live runs of active, switched-on projects, oldest start first, timed on the server's clock.
    assert [run["run_id"] for run in listed["runs"]] == ["run-0001", "run-0002"]
    first, second = listed["runs"]
    assert set(first) == RUN_VIEW_KEYS
    assert (first["started_at"], first["elapsed_seconds"], first["remaining_seconds"], first["state"]) == (
        at(-30),
        630,
        BUDGET - 630,
        "running",
    )
    assert (second["started_at"], second["elapsed_seconds"], second["remaining_seconds"]) == (at(0), 600, BUDGET - 600)
    assert first == get_run(api, "run-0001") | {"reviews": []}
    # The others: ended by /end; live but on a switched-off project; ended by the sweep with its stopped project; and
    # open in another state.
    assert get_run(api, "run-0003")["end_reason"] == "dispatcher"
    assert (get_run(api, "run-0005")["ended_at"], get_run(api, "run-0005")["state"]) == (None, "running")
    assert get_run(api, "run-0006")["end_reason"] == "project_inactive"
    assert (_row("run-0004")["ended_at"], _row("run-0004")["state"]) == (None, "detached")

    # The project filter and the limit.
    assert [run["run_id"] for run in poll(api, project_id=on)["runs"]] == ["run-0001", "run-0002"]
    assert poll(api, project_id=switched_off)["runs"] == []
    assert poll(api, project_id="proj_999")["runs"] == []
    assert [run["run_id"] for run in poll(api, limit=1)["runs"]] == ["run-0001"]
    for limit in (0, 501, "x"):
        assert validation_locs(api.get("/repair/runs", params={"limit": limit}, headers=AGENT)) == [["query", "limit"]]

    # A live run past its deadline stays listed, over budget: today's timeout and conclude fallback are under way.
    while clock.elapsed < BUDGET:
        clock.to(clock.elapsed + 10)
        _claim(api, on, on_claims[:2])
        heartbeat(api, switched_off, "i001", "w5")
    over = poll(api)["runs"]
    assert [(run["run_id"], run["state"], run["elapsed_seconds"], run["remaining_seconds"]) for run in over] == [
        ("run-0001", "over_budget", BUDGET + 30, 0),
        ("run-0002", "running", BUDGET, 0),
    ]
    # Switched on again, a project's live runs are listed again.
    set_switch(api, switched_off, True)
    assert [run["run_id"] for run in poll(api)["runs"]] == ["run-0001", "run-0002", "run-0005"]


def _cursor(payload: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# SV29
def test_list_include_ended_pages_newest_first_with_a_cursor(api: TestClient, clock: Clock) -> None:
    first_project = new_project(api, intents=3)
    second_project = new_project(api, intents=2)
    _claim(api, first_project, _claims(3))
    heartbeat(api, second_project, "i001", "w4")
    heartbeat(api, second_project, "i002", "w5")
    register(api, "run-0001", first_project, "i001", "w1")
    register(api, "run-0002", first_project, "i002", "w2")  # the same second: the run id breaks the tie
    clock.to(10)
    register(api, "run-0003", second_project, "i001", "w4")
    clock.to(12)
    _claim(api, first_project, _claims(3))
    heartbeat(api, second_project, "i002", "w5")
    register(api, "run-0004", first_project, "i003", "w3")
    clock.to(14)
    register(api, "run-0005", second_project, "i002", "w5")
    exit_end(api, "run-0002")
    set_switch(api, second_project, False)  # every run is listed for evaluation, whatever the switch

    def pages(**params: Any) -> list[list[str]]:
        found = []
        cursor = None
        while True:
            page = poll(api, include_ended=True, **params, **({"cursor": cursor} if cursor else {}))
            assert (page["shadow"], page["review_rules"]) == (True, REVIEW_RULES)
            found.append([run["run_id"] for run in page["runs"]])
            cursor = page["next_cursor"]
            if cursor is None:
                return found
            assert isinstance(cursor, str) and len(cursor) <= 256

    assert pages(limit=2) == [["run-0005", "run-0004"], ["run-0003", "run-0002"], ["run-0001"]]
    assert pages(limit=1) == [["run-0005"], ["run-0004"], ["run-0003"], ["run-0002"], ["run-0001"]]
    assert pages(limit=5) == [["run-0005", "run-0004", "run-0003", "run-0002", "run-0001"]]
    assert pages() == [["run-0005", "run-0004", "run-0003", "run-0002", "run-0001"]]
    assert pages(limit=2, project_id=first_project) == [["run-0004", "run-0002"], ["run-0001"]]
    assert pages(limit=2, project_id=second_project) == [["run-0005", "run-0003"]]
    ended = poll(api, include_ended=True)["runs"][3]
    assert (ended["run_id"], ended["state"], ended["end_reason"]) == ("run-0002", "ended", "dispatcher")

    # A cursor is only for include_ended, and only one this server wrote.
    cursor = poll(api, include_ended=True, limit=2)["next_cursor"]
    coded(api.get("/repair/runs", params={"cursor": cursor}, headers=AGENT), 422, "bad_cursor")
    for bad in (
        "",
        "!!!!",
        "e30",
        _cursor({"registered_at": at(0), "run_id": "run-0001"}),
        _cursor(["2026-11-14T10:00:00Z"]),
        _cursor(["2026-11-14 10:00:00", "run-0001"]),
        _cursor(["2026-11-14T10:00:00Z", "bad id"]),
        _cursor(["2026-11-14T10:00:00Z", "run-0001\n"]),
        _cursor([1, "run-0001"]),
        "a" * 300,
        cursor + "é",
    ):
        coded(api.get("/repair/runs", params={"include_ended": True, "cursor": bad}, headers=AGENT), 422, "bad_cursor")
    assert [run["run_id"] for run in poll(api, include_ended=True, cursor=cursor)["runs"]] == [
        "run-0003",
        "run-0002",
        "run-0001",
    ]


# SV30
def test_evaluation_reads_do_not_record_the_agent_poll(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=3)
    claims = _claims(3)
    _claim(api, project_id, claims)
    register(api, RUN, project_id)
    register(api, "run-0002", project_id, "i002", "w2")
    advance(api, clock, project_id, 100, claims)
    poll(api)

    def last_seen() -> Any:
        return api.get("/repair/settings").json()["agent_last_seen_at"]

    assert last_seen() == at(100)
    advance(api, clock, project_id, 200, claims)
    register(api, "run-0003", project_id, "i003", "w3")  # its window opens at 1550 s and accepts until 1990 s

    # After the windows of run-0001 and run-0002 open, only evaluation reads come: the include_ended listing and the
    # single run. They sweep, and record no poll.
    while clock.elapsed < ACCEPT_UNTIL:
        clock.to(min(clock.elapsed + 30, ACCEPT_UNTIL))
        _claim(api, project_id, claims)
        poll(api, include_ended=True)
        get_run(api, RUN)
    assert last_seen() == at(100)
    # Their sweep at the accept limit recorded "not reviewed" with the agent's last real poll: before the windows.
    for run_id in (RUN, "run-0002"):
        assert issue_details(run_id) == [not_reviewed_detail(why="agent_not_seen", agent_last_seen_at=at(100))]

    # A default poll after run-0003's window opened counts; the evaluation reads after it do not change that.
    advance(api, clock, project_id, 1800, claims)
    poll(api)
    assert last_seen() == at(1800)
    while clock.elapsed < 1990:
        clock.to(min(clock.elapsed + 30, 1990))
        _claim(api, project_id, claims)
        poll(api, include_ended=True)
    assert last_seen() == at(1800)
    assert issue_details("run-0003") == [
        not_reviewed_detail(
            why="agent_seen",
            agent_last_seen_at=at(1800),
            elapsed_seconds=1790,
            run_started_at=at(200),
            deadline_at=at(2000),
        )
    ]


# Spec 5.5, 6.3: SV37's back_after_downtime case, through GET /repair/runs itself.
def test_a_poll_after_downtime_sweeps_with_the_previous_poll_time(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, RUN, project_id)
    # The agent polls every 10 s until 10:05:00 and is then down, while the run's task heartbeats on.
    advance(api, clock, project_id, 300, polls=True)
    advance(api, clock, project_id, ACCEPT_UNTIL)
    assert api.get("/repair/settings").json()["agent_last_seen_at"] == at(300)
    assert issue_details(RUN) == []

    # Its first poll after the downtime comes at the accept limit. The poll sweeps with the previous poll's time, so
    # the downtime does not look like agent_seen, and only then records its own.
    listed = poll(api)
    assert issue_details(RUN) == [not_reviewed_detail(why="agent_not_seen", agent_last_seen_at=at(300))]
    assert [run["window"]["review_status"] for run in listed["runs"]] == ["not_reviewed"]
    assert api.get("/repair/settings").json()["agent_last_seen_at"] == at(ACCEPT_UNTIL)


# SV31
def test_review_status_moves_pending_open_late_reviewed(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=5)
    claims = _claims(5)
    _claim(api, project_id, claims)
    register(api, RUN, project_id)  # reviewed late
    register(api, "run-0002", project_id, "i002", "w2")  # never reviewed
    register(api, "run-0003", project_id, "i003", "w3", cli_type="mock")  # no transcript
    register(api, "run-0004", project_id, "i004", "w4", budget_seconds=349)  # too short
    register(api, "run-0005", project_id, "i005", "w5")  # ends before its window closes

    def status(run_id: str) -> str:
        single = get_run(api, run_id)["window"]["review_status"]
        listed = {run["run_id"]: run["window"]["review_status"] for run in poll(api)["runs"]}
        if run_id in listed:
            assert listed[run_id] == single, run_id
        return single

    assert [status(run_id) for run_id in (RUN, "run-0002", "run-0003", "run-0004")] == [
        "pending",
        "pending",
        "not_reviewable",
        "not_reviewable",
    ]
    advance(api, clock, project_id, 1000, claims)
    exit_end(api, "run-0005")
    advance(api, clock, project_id, OPENS - 1, claims)
    assert [status(RUN), status("run-0005")] == ["pending", "closed"]
    advance(api, clock, project_id, OPENS, claims)
    assert status(RUN) == "open"
    advance(api, clock, project_id, CLOSES - 1, claims)
    assert status(RUN) == "open"
    clock.to(CLOSES)
    _claim(api, project_id, claims)
    assert [status(RUN), status("run-0002")] == ["late", "late"]
    # The agent's review, from a digest it took just after the window closed (within the 5 s margin), arrives late.
    clock.to(CLOSES + 20)
    _claim(api, project_id, claims)
    review(api, RUN, CLOSES + 3)
    assert status(RUN) == "reviewed"
    advance(api, clock, project_id, ACCEPT_UNTIL - 1, claims)
    assert status("run-0002") == "late"
    clock.to(ACCEPT_UNTIL)
    _claim(api, project_id, claims)
    assert [status(RUN), status("run-0002")] == ["reviewed", "not_reviewed"]
    assert status("run-0005") == "closed"
    assert [status("run-0003"), status("run-0004")] == ["not_reviewable", "not_reviewable"]


# --------------------------------------------------------------------------- SV32-SV36: reviews (spec 6.5, 4.5)

AGENT_ISSUE_KEYS = {
    "decision",
    "mode",
    "applied",
    "window",
    "run_started_at",
    "elapsed_seconds",
    "budget_seconds",
    "window_seconds",
    "deadline_at",
    "slices_used",
    "reason",
    "evidence",
    "next_step",
    "display_truncated",
    "fallback",
    "model",
}


# SV32
def test_a_review_becomes_a_shadow_repair_review_issue(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    claims = _claims(2)
    _claim(api, project_id, claims)
    # A first, short run of i001 is flagged "not reviewed", then its worker's next attempt is reviewed: the intent
    # gets two issues.
    register(api, RUN, project_id, budget_seconds=400)  # window: opens 300 s, closes 320 s, accepts until 390 s
    advance(api, clock, project_id, 390, claims, polls=True)
    assert [detail["decision"] for detail in issue_details(RUN)] == ["not_reviewed"]
    clock.to(400)
    _claim(api, project_id, claims)
    register(api, "run-0002", project_id, budget_seconds=400)  # supersedes run-0001
    advance(api, clock, project_id, 702, claims, polls=True)

    answer = review(
        api,
        "run-0002",
        302,
        reason="Steady\nprogress‮  on​ the extraction",
        evidence=["E14 t+21:10\tsecond partition found", "⁦E16⁩ extraction reports 70%", "​"],
        next_step="List /etc\r\nonce the extraction finishes",
        applied=True,  # the server sets mode and applied
        mode="live",
    )
    issue = answer["issue"]
    assert (answer["created"], answer["mode"], answer["applied"]) == (True, "shadow", False)
    assert answer["review"] == {
        "run_id": "run-0002",
        "window": 1,
        "review_id": "rv-8b1e2f30a9c4d5e6f7a8b9c0",
        "source": "agent",
        "decision": "extend",
        "issue_id": issue["id"],
        "created_at": at(702),
    }
    assert (issue["kind"], issue["run_id"], issue["created_at"], issue["updated_at"]) == (
        "repair_review",
        "run-0002",
        at(702),
        at(702),
    )
    assert set(issue["detail"]) == AGENT_ISSUE_KEYS | {"slice_seconds"}
    assert issue["detail"] == {
        "decision": "extend",
        "mode": "shadow",
        "applied": False,
        "window": 1,
        "run_started_at": at(400),
        "elapsed_seconds": 302,
        "budget_seconds": 400,
        "window_seconds": 400,
        "deadline_at": at(800),
        "slices_used": 0,
        "reason": "Steady progress on the extraction",
        "evidence": ["E14 t+21:10 second partition found", "E16 extraction reports 70%"],
        "next_step": "List /etc once the extraction finishes",
        "display_truncated": False,
        "fallback": None,
        "model": "test-model",
        "slice_seconds": 600,
    }
    # The full review, readable only with the agent token, holds the digest, its SHA-256 and the raw output.
    stored = get_run(api, "run-0002")["reviews"]
    assert len(stored) == 1
    assert (stored[0]["digest"], stored[0]["digest_sha256"], stored[0]["raw_output"]) == (
        "<the digest the model saw>",
        hashlib.sha256(b"<the digest the model saw>").hexdigest(),
        '{"decision": "extend"}',
    )
    assert (stored[0]["mode"], stored[0]["applied"], stored[0]["issue_id"]) == ("shadow", False, issue["id"])

    # GET /projects/{id} shows the intent's issues newest first: the review, then the earlier run's record.
    shown = next(intent for intent in project_intents(api, project_id) if intent["id"] == "i001")["issues"]
    assert [(item["kind"], item["run_id"], item["detail"]["decision"]) for item in shown] == [
        ("repair_review", "run-0002", "extend"),
        ("repair_review", RUN, "not_reviewed"),
    ]
    assert shown[0] == issue
    assert (shown[1]["detail"]["end_reason"], shown[1]["detail"]["run_ended_at"]) == ("superseded", at(400))
    # And the run's heartbeat reply carries that intent's issues, the worker under review included.
    reply = api.post(f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "w1"})
    assert reply.status_code == 200 and reply.json()["issues"] == shown
    other = api.post(f"/projects/{project_id}/intents/i002/heartbeat", json={"worker": "w2"})
    assert other.json()["issues"] == []


# SV33
def test_review_replay_and_conflict(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, RUN, project_id)
    advance(api, clock, project_id, 1352)
    first = review(api, RUN, 1352)

    # A retry with the same review_id gets the stored review and issue back, whatever it now says.
    clock.to(1360)
    heartbeat(api, project_id)
    replay = post_review(api, RUN, review_body(1360, decision="detach", reason="something else"))
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first, "created": False}
    # Another review_id: 409 with the existing summary, and nothing more is stored.
    conflict = coded(post_review(api, RUN, review_body(1360, review_id="rv-another-0001")), 409, "already_reviewed")
    assert conflict["review"] == first["review"]
    assert (len(table("repair_reviews")), len(table("intent_issues"))) == (1, 1)
    assert records(caplog, REPAIR_LOGGER)[-1] == (
        logging.INFO,
        f"repair review refused run={RUN} code=already_reviewed",
    )

    # After the run has ended, the replay still answers 200, with the issue as it is now (stamped with the end).
    clock.to(1400)
    exit_end(api, RUN, exited_seconds_ago=0.0)
    ended_replay = post_review(api, RUN, review_body(1352))
    assert ended_replay.status_code == 200
    assert ended_replay.json()["review"] == first["review"]
    assert ended_replay.json()["issue"]["detail"] == {
        **first["issue"]["detail"],
        "run_ended_at": at(1400),
        "end_reason": "dispatcher",
    }
    # A conflicting post is still a conflict, not "run ended": the review check comes first.
    coded(post_review(api, RUN, review_body(1352, review_id="rv-another-0001")), 409, "already_reviewed")


# SV34
def test_review_refusals(api: TestClient, clock: Clock, caplog) -> None:
    capture_repair_logs(caplog)
    project_id = new_project(api, intents=8)
    switched_off = new_project(api)
    toggled = new_project(api)
    claims = _claims(8)
    _claim(api, project_id, claims)
    heartbeat(api, switched_off, "i001", "w9")
    heartbeat(api, toggled, "i001", "w10")
    register(api, RUN, project_id)
    register(api, "run-0002", project_id, "i002", "w2")
    register(api, "run-0003", project_id, "i003", "w3", cli_type="mock")
    register(api, "run-0004", project_id, "i004", "w4", budget_seconds=349)
    register(api, "run-0005", switched_off, "i001", "w9")
    register(api, "run-0006", project_id, "i006", "w6")
    register(api, "run-0007", toggled, "i001", "w10")
    register(api, "run-0008", project_id, "i008", "w8")
    # run-0007's project is switched off for one sweep, which settles its window with nothing recorded, then on again.
    set_switch(api, toggled, False)
    poll(api)
    set_switch(api, toggled, True)
    set_switch(api, switched_off, False)
    exit_end(api, "run-0006")

    def every_claim(until: float) -> None:
        while clock.elapsed < until:
            clock.to(min(clock.elapsed + 10, until))
            _claim(api, project_id, claims)
            heartbeat(api, switched_off, "i001", "w9")
            heartbeat(api, toggled, "i001", "w10")

    refusals: list[str] = []

    def refused(run_id: str, body: dict[str, Any], code: str, status: int = 409) -> dict[str, Any]:
        detail = coded(post_review(api, run_id, body), status, code)
        if status == 409:
            refusals.append(f"repair review refused run={run_id} code={code}")
        return detail

    every_claim(OPENS - 1)
    detail = refused(RUN, review_body(OPENS - 1), "too_early")
    assert detail["review_opens_at"] == at(OPENS)
    every_claim(OPENS)
    refused(RUN, review_body(OPENS - 6), "digest_too_early")
    detail = refused(RUN, review_body(CLOSES + 6), "digest_too_late")
    assert detail["review_closes_at"] == at(CLOSES)
    detail = refused(RUN, review_body(OPENS, expected_version=2), "stale_version")
    assert detail["version"] == 1
    refused(RUN, review_body(OPENS, window=2), "wrong_window")
    refused("run-0003", review_body(OPENS), "not_reviewable")  # mock: no transcript
    refused("run-0004", review_body(OPENS), "not_reviewable")  # a 349 s window
    refused("run-0005", review_body(OPENS), "repair_disabled")
    detail = refused("run-0006", review_body(OPENS), "run_ended")
    assert detail["end_reason"] == "dispatcher"
    coded(post_review(api, "run-0009", review_body(OPENS)), 404, "run_not_found")
    # The limits of the margins are accepted: a digest from 5 s before the window opened, and from 5 s after it closed.
    assert post_review(api, RUN, review_body(OPENS - 5)).status_code == 201
    detail = refused(RUN, review_body(OPENS, review_id="rv-another-0001"), "already_reviewed")
    assert detail["review"]["review_id"] == "rv-8b1e2f30a9c4d5e6f7a8b9c0"
    every_claim(CLOSES + 20)
    assert post_review(api, "run-0008", review_body(CLOSES + 5)).status_code == 201

    # At the accept limit: run-0002's post finds the server's not-reviewed record, which the post's own sweep made;
    # run-0007, whose window was settled while its switch was off, gets the window's own limit.
    every_claim(ACCEPT_UNTIL)
    detail = refused("run-0002", review_body(CLOSES), "window_closed")
    assert (detail["review"]["source"], detail["review"]["decision"], detail["review"]["review_id"]) == (
        "server",
        "not_reviewed",
        "server",
    )
    detail = refused("run-0007", review_body(CLOSES), "window_closed")
    assert "review" not in detail
    assert table("repair_reviews", "WHERE run_id = 'run-0007'") == []

    # Only the two accepted posts and the not-reviewed record were stored.
    assert sorted((row["run_id"], row["source"]) for row in table("repair_reviews")) == [
        (RUN, "agent"),
        ("run-0002", "server"),
        ("run-0008", "agent"),
    ]
    assert repair_lines(caplog, "repair review refused") == refusals
    assert all(level == logging.INFO for level, message in records(caplog, REPAIR_LOGGER) if "refused" in message)

    # A body that fails its model: FastAPI's 422, before any check.
    body = review_body(OPENS)
    missing = {key: value for key, value in body.items() if key != "digest_elapsed_seconds"}
    assert validation_locs(post_review(api, RUN, missing)) == [["body", "digest_elapsed_seconds"]]
    for changes, field in (
        ({"digest_elapsed_seconds": 604801}, "digest_elapsed_seconds"),
        ({"digest_elapsed_seconds": -1}, "digest_elapsed_seconds"),
        ({"review_id": "rv-12345678\n"}, "review_id"),
        ({"review_id": "rv-1234"}, "review_id"),
        ({"reason": "r" * 2001}, "reason"),
        ({"reason": " ​\n "}, "reason"),
        ({"evidence": ["e" * 501]}, "evidence"),
        ({"evidence": ["e"] * 11}, "evidence"),
        ({"next_step": "n" * 501}, "next_step"),
        ({"model": "m" * 101}, "model"),
        ({"digest": "d" * 65537}, "digest"),
        ({"raw_output": "o" * 16385}, "raw_output"),
        ({"window": 0}, "window"),
        ({"decision": "stop"}, "decision"),
        ({"fallback": "model_timeout"}, None),  # a fallback requires decision continue
    ):
        locs = validation_locs(post_review(api, RUN, {**body, **changes}))
        assert locs[0][:2] == (["body", field] if field else ["body"]), changes
    for literal in ("Infinity", "NaN"):
        text = json.dumps({**body, "digest_elapsed_seconds": "__x__"}).replace('"__x__"', literal)
        response = api.post(
            f"/repair/runs/{RUN}/reviews", content=text, headers={**AGENT, "Content-Type": "application/json"}
        )
        assert validation_locs(response) == [["body", "digest_elapsed_seconds"]]


# SV35
def test_concurrent_reviews_record_exactly_one(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, RUN, project_id)
    advance(api, clock, project_id, 1352)

    start = threading.Barrier(8)
    answers: list[Any] = []
    lock = threading.Lock()

    def post(number: int) -> None:
        start.wait(timeout=10)
        response = post_review(api, RUN, review_body(1352, review_id=f"rv-concurrent-{number:04d}"))
        with lock:
            answers.append(response)

    threads = [threading.Thread(target=post, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(response.status_code for response in answers) == [201] + [409] * 7
    assert {response.json()["detail"]["code"] for response in answers if response.status_code == 409} == {
        "already_reviewed"
    }
    created = next(response.json() for response in answers if response.status_code == 201)
    assert [(row["review_id"], row["issue_id"]) for row in table("repair_reviews")] == [
        (created["review"]["review_id"], created["issue"]["id"])
    ]
    assert len(table("intent_issues")) == 1


# SV36
def test_extend_and_detach_reviews_change_no_run_state(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=3)
    claims = _claims(3)
    _claim(api, project_id, claims)
    for number, (intent_id, worker) in enumerate(claims, start=1):
        register(api, f"run-000{number}", project_id, intent_id, worker)
    advance(api, clock, project_id, 1352, claims, polls=True)

    for run_id, decision in ((RUN, "extend"), ("run-0002", "detach"), ("run-0003", "continue")):
        before_row = _row(run_id)
        before = get_run(api, run_id)
        assert before["window"]["review_status"] == "open"
        answer = review(api, run_id, 1352, decision=decision, review_id=f"rv-{decision}-00000001")
        assert (answer["mode"], answer["applied"], answer["issue"]["detail"]["applied"]) == ("shadow", False, False)
        after = get_run(api, run_id)
        # The run view differs only in its reviews, its updated_at and its review status.
        assert {key for key in before if before[key] != after[key]} == {"reviews", "updated_at", "window"}
        assert {**before["window"], "review_status": "reviewed"} == after["window"]
        assert [item["decision"] for item in after["reviews"]] == [decision]
        # Nothing is applied: deadline, version, slices and state stay as they were.
        after_row = _row(run_id)
        for key in ("deadline_at", "version", "slices_used", "state", "ended_at", "budget_seconds", "started_at"):
            assert after_row[key] == before_row[key], (run_id, key)
        assert ("slice_seconds" in answer["issue"]["detail"]) == (decision == "extend")

    # The answers the dispatcher gets later carry the same deadline and version.
    advance(api, clock, project_id, BUDGET, claims, polls=True)
    clock.to(BUDGET + 1)
    for run_id in (RUN, "run-0002", "run-0003"):
        ended = exit_end(api, run_id, exited_seconds_ago=1.0, timed_out=True, returncode=124)
        assert (ended["deadline_at"], ended["version"], ended["slices_used"]) == (at(BUDGET), 1, 0)
        assert ended["end_reason"] == "dispatcher"


# --------------------------------------------------------------------------- lone surrogates (spec 6.0)

# Half of a surrogate pair. JSON.stringify writes one as an escape, \ud83d, when a string was cut between the two
# halves, so a transcript excerpt in a digest can hold one. json.loads reads the escape back, but no UTF-8 text can
# hold the character, so it could be neither stored nor answered: every retry would get a 500.
LONE = "\ud83d"
REPLACED = "�"


def _escaped(api: TestClient, method: str, path: str, body: Any, headers: dict[str, str]) -> Any:
    """A request whose body json.dumps writes with its default escapes, so a lone surrogate goes as its escape."""
    content = json.dumps(body).encode("ascii")
    return api.request(method, path, content=content, headers={**headers, "Content-Type": "application/json"})


# Spec 6.0, 6.5: a lone surrogate in a request body is answered, never a 500.
def test_a_lone_surrogate_in_a_body_becomes_a_replacement_character(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    claims = _claims(2)
    _claim(api, project_id, claims)

    # A register: accepted, and stored and answered with U+FFFD in its place, one for one.
    locator = {"container": f"cairn-{LONE}", f"marker{LONE}": None}
    response = _escaped(api, "PUT", f"/repair/runs/{RUN}", run_body(project_id, locator=locator), DISPATCHER)
    assert response.status_code == 201, response.text
    assert response.json()["run"]["locator"] == {"container": f"cairn-{REPLACED}", f"marker{REPLACED}": None}
    assert json.loads(_row(RUN)["locator_json"]) == response.json()["run"]["locator"]
    # An id that holds one matches nothing, and the answer says so.
    for body, status, code in (
        (run_body(f"proj{LONE}"), 404, "project_not_found"),
        (run_body(project_id, f"i00{LONE}"), 404, "intent_not_found"),
        (run_body(project_id, "i002", f"w2{LONE}"), 409, "not_claimed"),
    ):
        coded(_escaped(api, "PUT", "/repair/runs/run-0002", body, DISPATCHER), status, code)
    changed = run_body(project_id, worker=f"w1{LONE}", locator=locator)
    assert coded(_escaped(api, "PUT", f"/repair/runs/{RUN}", changed, DISPATCHER), 409, "run_conflict")["fields"] == [
        "worker"
    ]

    # A value its model refuses gets FastAPI's own 422, which shows it with U+FFFD in its place: the open switch
    # included, whose model is today's plain bool.
    second = run_body(project_id, "i002", "w2")
    refused = [
        ("PUT", "/repair/runs/run-0002", {**second, "cli_type": f"codex{LONE}"}, DISPATCHER, "cli_type"),
        ("PUT", "/repair/runs/run-0002", {**second, "session_id": f"s{LONE}"}, DISPATCHER, "session_id"),
        ("POST", f"/repair/runs/{RUN}/end", {"outcome": f"success{LONE}"}, DISPATCHER, "outcome"),
        ("POST", f"/repair/runs/{RUN}/reviews", review_body(1352, review_id=f"rv-0000000{LONE}"), AGENT, "review_id"),
        ("POST", f"/repair/runs/{RUN}/reviews", review_body(1352, decision=LONE), AGENT, "decision"),
        ("PUT", f"/projects/{project_id}/repair", {"enabled": LONE}, {}, "enabled"),
    ]
    for method, path, body, headers, field in refused:
        response = _escaped(api, method, path, body, headers)
        assert validation_locs(response) == [["body", field]], field
        assert response.json()["detail"][0]["input"] == body[field].replace(LONE, REPLACED), field
    assert [row["run_id"] for row in table("repair_runs")] == [RUN]
    assert table("projects", "WHERE id = ?", (project_id,))[0]["repair_enabled"] == 1

    # A review: recorded, every text with U+FFFD in its place, and the digest hashed as stored.
    advance(api, clock, project_id, 1352, claims)
    texts = {
        "reason": f"Steady {LONE}progress",
        "evidence": [f"E14 t+21:10 second partition {LONE}"],
        "next_step": f"List /etc{LONE}",
        "model": f"test-model{LONE}",
        "digest": f"<the digest {LONE}the model saw>",
        "raw_output": f'{{"decision": "extend{LONE}"}}',
    }
    response = _escaped(api, "POST", f"/repair/runs/{RUN}/reviews", review_body(1352, **texts), AGENT)
    assert response.status_code == 201, response.text
    stored = get_run(api, RUN)["reviews"]
    assert len(stored) == 1
    assert {key: stored[0][key] for key in texts} == {
        "reason": f"Steady {REPLACED}progress",
        "evidence": [f"E14 t+21:10 second partition {REPLACED}"],
        "next_step": f"List /etc{REPLACED}",
        "model": f"test-model{REPLACED}",
        "digest": f"<the digest {REPLACED}the model saw>",
        "raw_output": f'{{"decision": "extend{REPLACED}"}}',
    }
    assert stored[0]["digest_sha256"] == hashlib.sha256(stored[0]["digest"].encode("utf-8")).hexdigest()
    assert issue_details(RUN)[0]["reason"] == f"Steady {REPLACED}progress"


# --------------------------------------------------------------------------- unknown fields (spec 6.0)

# Lists nested this deep: twice Python's default recursion limit, and far fewer levels than json.loads reads (about
# 10,000 on 3.12 and 3.13), so a body holding them reaches the request models.
DEEP = 2000


def _with_unknown(api: TestClient, method: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    """A request whose body is `body` plus a field that no model declares, as a newer client could add: lists nested
    DEEP levels deep, with half a surrogate pair at the bottom.
    """
    unknown = "[" * DEEP + '"\\ud83d"' + "]" * DEEP
    content = (json.dumps(body)[:-1] + ', "added_later": ' + unknown + "}").encode("ascii")
    return api.request(method, path, content=content, headers={**headers, "Content-Type": "application/json"})


# Spec 6.0: request models ignore unknown fields, so a newer client works with this server, whatever such a field
# holds. The lone-surrogate rule walks only the fields a model declares, so each body is answered as it would be
# without the field, as today's endpoints answer it, and nothing of the field is stored.
def test_an_unknown_field_is_ignored_however_deeply_it_is_nested(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=1)
    _claim(api, project_id, _claims(1))

    # A register (201), and the session id added later (200).
    response = _with_unknown(api, "PUT", f"/repair/runs/{RUN}", run_body(project_id), DISPATCHER)
    assert response.status_code == 201, response.text
    response = _with_unknown(api, "PUT", f"/repair/runs/{RUN}", run_body(project_id, session_id=SESSION), DISPATCHER)
    assert response.status_code == 200, response.text
    assert response.json()["run"]["session_id"] == SESSION

    # A review (201).
    advance(api, clock, project_id, 1352)
    response = _with_unknown(api, "POST", f"/repair/runs/{RUN}/reviews", review_body(1352), AGENT)
    assert response.status_code == 201, response.text
    assert response.json()["review"]["decision"] == "extend"

    # The dispatcher's two /end calls (200 each).
    exited = {"timed_out": False, "cancelled": False, "returncode": 0, "exited_seconds_ago": 0.2}
    for fields in (exited, {"outcome": "success"}):
        response = _with_unknown(api, "POST", f"/repair/runs/{RUN}/end", fields, DISPATCHER)
        assert response.status_code == 200, response.text
    assert (response.json()["run"]["end_reason"], response.json()["run"]["outcome"]) == ("dispatcher", "success")

    assert "added_later" not in json.dumps([table(name) for name in ("repair_runs", "repair_reviews", "intent_issues")])
    # Today's POST /projects ignores the same field.
    response = _with_unknown(api, "POST", "/projects", {"title": "t", "origin": "start", "goal": "finish"}, {})
    assert response.status_code == 201, response.text
