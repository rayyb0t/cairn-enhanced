"""Repair layer (shadow mode): what existing endpoints do with the repair layer installed (spec 2.1, 4.2, 6.8).

Existing endpoints never call repair code. The one effect they gain is the foreign key of `repair_runs` (your answer
to Q3): today's project delete and reopen's delete of the completion intent cascade into the repair tables inside
their own statement, so a deleted project's runs, reviews, digests and raw output go with it, also on a server rolled
back to ee0f227 (SV26, SV27). SV43 checks with a SQLite authorizer that nothing else of an existing endpoint reaches
a repair table; SV44 that a dropped repair table fails no existing endpoint; SV46 that the existing endpoints answer
as at the base while Phase 2 runs beside them; SV8 that the export workers read never shows anything of the repair
layer.

SV43, SV44 and SV46 replay the existing-endpoint script (test_repair_server_script.py, which the switch-off
differential replays too) with its clock, which moves one second at every reading; repair calls read that clock
without moving it, so they change none of the script's times.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_repair_server import ACCEPT_UNTIL, Clock, heartbeat, new_project, project_intents, traced_sql
from test_repair_server_script import SCRIPT, Call
from test_repair_server_support import (
    AGENT,
    AGENT_TOKEN,
    DISPATCHER,
    DISPATCHER_TOKEN,
    advance,
    coded,
    exit_end,
    first_call,
    freeze_clock,
    poll,
    post_review,
    pre_existing_rows,
    put_run,
    register,
    review,
    review_body,
    run_body,
    run_script,
    script_clock,
    serve,
    set_switch,
    table,
)

from cairn.server import db, repair, repair_auth
from cairn.server.app import app

REPAIR_TABLES = ("repair_runs", "repair_reviews")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return freeze_clock(monkeypatch)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    yield from serve(tmp_path, monkeypatch)


def _claims(prefix: int, count: int) -> tuple[tuple[str, str], ...]:
    return tuple((f"i{number:03d}", f"w{prefix + number}") for number in range(1, count + 1))


def _repair_rows(project_id: str) -> dict[str, list[dict[str, Any]]]:
    """A project's rows in the repair tables, and its repair_review issues."""
    return {
        "repair_runs": table("repair_runs", "WHERE project_id = ?", (project_id,)),
        "repair_reviews": table(
            "repair_reviews", "WHERE run_id IN (SELECT run_id FROM repair_runs WHERE project_id = ?)", (project_id,)
        ),
        "issues": table("intent_issues", "WHERE project_id = ? AND kind = 'repair_review'", (project_id,)),
    }


# --------------------------------------------------------------------------- SV8: the export (spec 6.8)


# SV8
def test_export_yaml_and_timeline_never_change(api: TestClient, clock: Clock) -> None:
    response = api.post(
        "/projects",
        json={
            "title": "export check",
            "origin": "a firmware image",
            "goal": "the root password",
            "bootstrap_enabled": False,
            "hints": [{"content": "look at the squashfs", "creator": "human"}],
        },
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["project"]["id"]
    for number in (1, 2):
        created = api.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": f"investigate {number}", "creator": "reasoner", "worker": None},
        )
        assert created.status_code == 201, created.text
    claims = (("i001", "w1"), ("i002", "w2"))
    for intent_id, worker in claims:
        heartbeat(api, project_id, intent_id, worker)

    def export() -> tuple[bytes, bytes]:
        yaml_answer = api.get(f"/projects/{project_id}/export", params={"format": "yaml"})
        timeline = api.get(f"/projects/{project_id}/export", params={"format": "timeline"})
        assert yaml_answer.status_code == timeline.status_code == 200
        return yaml_answer.content, timeline.content

    before = export()
    # The switch on, a register with a session id, an agent review and a not-reviewed record.
    set_switch(api, project_id, True)
    register(api, "run-0001", project_id, "i001", "w1", session_id="0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e44")
    register(api, "run-0002", project_id, "i002", "w2")
    advance(api, clock, project_id, 1352, claims, polls=True)
    review(api, "run-0001", 1352)
    advance(api, clock, project_id, ACCEPT_UNTIL, claims, polls=True)
    assert [row["source"] for row in table("repair_reviews")] == ["agent", "server"]
    assert {issue["kind"] for intent in project_intents(api, project_id) for issue in intent["issues"]} == {
        "repair_review"
    }

    after = export()
    assert after == before
    for text in after:
        lowered = text.decode("utf-8").lower()
        for word in ("repair", "run-0001", "run-0002", "review", "shadow", "not_reviewed", "0199a1b2"):
            assert word not in lowered, word


# --------------------------------------------------------------------------- SV26, SV27: the cascade (spec 4.2)


def _populate(api: TestClient, clock: Clock, projects: list[tuple[str, int]]) -> None:
    """In each project: run 1 reviewed by the agent and still open, run 2 flagged "not reviewed", run 3 ended early."""
    claims = {project_id: _claims(prefix, 3) for project_id, prefix in projects}
    for project_id, claim in claims.items():
        for intent_id, worker in claim:
            heartbeat(api, project_id, intent_id, worker)
    for project_id, prefix in projects:
        for number, (intent_id, worker) in enumerate(claims[project_id], start=1):
            register(api, f"run-{prefix + number:04d}", project_id, intent_id, worker)

    def keep_alive(until: float) -> None:
        while clock.elapsed < until:
            clock.to(min(clock.elapsed + 10, until))
            for project_id, claim in claims.items():
                for intent_id, worker in claim:
                    heartbeat(api, project_id, intent_id, worker)
            poll(api)

    keep_alive(500)
    for _project_id, prefix in projects:
        exit_end(api, f"run-{prefix + 3:04d}")
    keep_alive(1352)
    for _project_id, prefix in projects:
        review(api, f"run-{prefix + 1:04d}", 1352)
    keep_alive(ACCEPT_UNTIL)


# SV26
def test_deleting_a_project_deletes_its_repair_rows_in_the_same_transaction(
    api: TestClient, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = new_project(api, intents=3)
    second = new_project(api, intents=3)
    _populate(api, clock, [(first, 0), (second, 3)])
    rows = {project_id: _repair_rows(project_id) for project_id in (first, second)}
    for project_rows in rows.values():
        assert [run["ended_at"] is None for run in project_rows["repair_runs"]] == [True, True, False]
        assert sorted(review["source"] for review in project_rows["repair_reviews"]) == ["agent", "server"]
        assert len(project_rows["issues"]) == 2

    # Today's delete, with no repair endpoint called: one statement, whose cascade deletes the rows in its transaction.
    with traced_sql(monkeypatch) as statements:
        response = api.delete(f"/projects/{first}")
    assert response.status_code == 204
    assert not any("repair" in statement for statement in statements)
    assert f"DELETE FROM projects WHERE id = '{first}'" in statements  # noqa: S608 (an expected trace line)
    assert _repair_rows(first) == {"repair_runs": [], "repair_reviews": [], "issues": []}
    assert table("repair_reviews", "WHERE run_id IN ('run-0001', 'run-0002', 'run-0003')") == []
    assert _repair_rows(second) == rows[second]

    # A deleted run is gone for the repair endpoints too.
    for run_id in ("run-0001", "run-0002", "run-0003"):
        coded(api.get(f"/repair/runs/{run_id}", headers=AGENT), 404, "run_not_found")
        coded(
            api.post(f"/repair/runs/{run_id}/end", json={"outcome": "success"}, headers=DISPATCHER),
            404,
            "run_not_found",
        )
        coded(post_review(api, run_id, review_body(1352)), 404, "run_not_found")

    # A run whose intent was removed outside Cairn, with foreign keys off, is never read: 404, and in no listing.
    path = tmp_path / "cairn.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM intents WHERE id = 'i003' AND project_id = ?", (second,))
        conn.commit()
    assert len(table("repair_runs", "WHERE run_id = 'run-0006'")) == 1
    coded(api.get("/repair/runs/run-0006", headers=AGENT), 404, "run_not_found")
    coded(api.post("/repair/runs/run-0006/end", json={"outcome": "success"}, headers=DISPATCHER), 404, "run_not_found")
    coded(post_review(api, "run-0006", review_body(1352)), 404, "run_not_found")
    listed = [run["run_id"] for run in poll(api)["runs"]] + [
        run["run_id"] for run in poll(api, include_ended=True)["runs"]
    ]
    assert "run-0006" not in listed and "run-0004" in listed

    # A server rolled back to ee0f227 keeps the rule: its get_conn sets the same pragma, and the key is the table's.
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM projects WHERE id = ?", (second,))
        conn.commit()
    assert table("repair_runs", "WHERE run_id IN ('run-0004', 'run-0005')") == []
    assert table("repair_reviews", "WHERE run_id IN ('run-0004', 'run-0005')") == []
    assert table("intent_issues", "WHERE project_id = ?", (second,)) == []


# SV27
def test_reopen_leaves_runs_alone(api: TestClient, clock: Clock, tmp_path: Path) -> None:
    project_id = new_project(api, intents=3)
    _populate(api, clock, [(project_id, 0)])
    fact_id = api.post(
        f"/projects/{project_id}/intents/i001/conclude", json={"worker": "w1", "description": "the answer"}
    ).json()["fact"]["id"]
    clock.to(ACCEPT_UNTIL + 5)
    response = api.post(
        f"/projects/{project_id}/complete", json={"from": [fact_id], "description": "solved", "worker": "reasoner"}
    )
    assert response.status_code == 200, response.text
    completion_id = response.json()["id"]
    poll(api)  # the sweep ends the project's open runs
    before = _repair_rows(project_id)
    assert {run["end_reason"] for run in before["repair_runs"]} == {"concluded", "project_inactive", "dispatcher"}

    # The completion intent cannot get a run: while the project is completed, register refuses the project; and the
    # intent is concluded, so even an active project's register would refuse it.
    coded(put_run(api, "run-0009", run_body(project_id, completion_id, "reasoner")), 403, "project_inactive")
    path = tmp_path / "cairn.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
        conn.commit()
    coded(put_run(api, "run-0009", run_body(project_id, completion_id, "reasoner")), 409, "intent_concluded")
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?", (project_id,))
        conn.commit()

    # Reopen deletes the completion intent, and its cascade into the repair tables deletes nothing.
    response = api.post(f"/projects/{project_id}/reopen", json={"description": "not yet", "creator": "human"})
    assert response.status_code == 200, response.text
    assert completion_id not in [intent["id"] for intent in project_intents(api, project_id)]
    assert _repair_rows(project_id) == before
    # The reopen's new intent is concluded too.
    reopened = response.json()["intent"]["id"]
    coded(put_run(api, "run-0009", run_body(project_id, reopened, "human")), 409, "intent_concluded")
    assert _repair_rows(project_id) == before


# --------------------------------------------------------------------------- the existing-endpoint script

# Rows a project's earlier switched-on runs left: runs of proj_001 and proj_002, reviews and their issues. The one
# with settled_window 0 ended after its window closed and was never reviewed: with the switch on, a sweep would flag it.
_HISTORY_RUNS = [
    ("hist-0001", "proj_001", "i002", "dispatcher", "2026-11-13T09:28:00Z", 1),
    ("hist-0002", "proj_001", "i004", "claim_lost", "2026-11-13T09:27:00Z", 0),
    ("hist-0003", "proj_002", "i001", "dispatcher", "2026-11-13T09:29:00Z", 1),
    ("hist-0004", "proj_002", "i002", "superseded", None, 0),
]
_HISTORY_REVIEWS = [
    ("hist-0001", "rv-history-00000001", "agent", "extend", "proj_001", "i002"),
    ("hist-0003", "server", "server", "not_reviewed", "proj_002", "i001"),
]


def _seed_history(path: Path, *, repair_rows: bool) -> None:
    """The issues of earlier reviews, and, with repair_rows, the runs and reviews they belong to."""
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        issue_ids = {}
        for run_id, _review_id, _source, decision, project_id, intent_id in _HISTORY_REVIEWS:
            detail = {"decision": decision, "mode": "shadow", "applied": False, "window": 1, "run_ended": True}
            cursor = conn.execute(
                "INSERT INTO intent_issues (project_id, intent_id, run_id, kind, detail_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 'repair_review', ?, '2026-11-13T09:23:00Z', '2026-11-13T09:23:00Z')",
                (project_id, intent_id, run_id, json.dumps(detail)),
            )
            issue_ids[run_id] = cursor.lastrowid
        if repair_rows:
            for run_id, project_id, intent_id, end_reason, last_alive_at, settled in _HISTORY_RUNS:
                conn.execute(
                    "INSERT INTO repair_runs (run_id, project_id, intent_id, worker, cli_type, execution, session_id,"
                    " budget_seconds, started_at, deadline_at, state, settled_window, registered_at, updated_at,"
                    " ended_at, end_reason, last_alive_at) VALUES (?, ?, ?, 'w-old', 'codex', 'container',"
                    " 'old-session-0001', 1800, '2026-11-13T09:00:00Z', '2026-11-13T09:30:00Z', 'ended', ?,"
                    " '2026-11-13T09:00:00Z', '2026-11-13T09:30:00Z', '2026-11-13T09:30:00Z', ?, ?)",
                    (run_id, project_id, intent_id, settled, end_reason, last_alive_at),
                )
            for run_id, review_id, source, decision, _project_id, _intent_id in _HISTORY_REVIEWS:
                conn.execute(
                    "INSERT INTO repair_reviews (run_id, window_no, review_id, source, decision, issue_id, detail_json,"
                    " digest, digest_sha256, raw_output, created_at) VALUES (?, 1, ?, ?, ?, ?, '{}', 'an old digest',"
                    " NULL, 'old raw output', '2026-11-13T09:23:00Z')",
                    (run_id, review_id, source, decision, issue_ids[run_id]),
                )
        conn.commit()


@dataclass
class ScriptRun:
    """One replay of the existing-endpoint script on its own database."""

    path: Path
    answers: list[tuple[int, bytes]] = field(default_factory=list)
    rows: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)


def _replay(
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tokens: bool = False,
    prepare: Callable[[Path], None] | None = None,
    before: Callable[[TestClient, int, Any], None] | None = None,
    after: Callable[[TestClient, int, Any], None] | None = None,
    extra: Callable[[TestClient], None] | None = None,
) -> ScriptRun:
    folder.mkdir()
    run = ScriptRun(folder / "cairn.db")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(run.path)
    if prepare is not None:
        prepare(run.path)
    repair.reset_memory()
    repair_auth.reset()
    if tokens:
        repair_auth.configure_tokens(AGENT_TOKEN, DISPATCHER_TOKEN)
    clock = script_clock(monkeypatch)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            run.answers = run_script(
                client,
                clock,
                before=None if before is None else lambda index, call: before(client, index, call),
                after=None if after is None else lambda index, call: after(client, index, call),
            )
            run.rows = pre_existing_rows(run.path)
            if extra is not None:
                extra(client)
    finally:
        repair.reset_memory()
        repair_auth.reset()
    return run


def _seed_point() -> int:
    """The script's call before which proj_001 has its intents i001-i004 and proj_002 its i001 and i002."""
    return first_call("/projects/proj_002/status", "PUT", {"status": "stopped"})


class _Authorizations:
    """SQLite authorizer events of every connection opened while installed: (call, statement, action, arg1, arg2),
    each with the script call that was running and the statement whose execute() prepared it. The authorizer reports
    a foreign key's cascade as part of the deleting statement.
    """

    def __init__(self) -> None:
        self.call: int | None = None
        self.events: list[tuple[int, str, int, str | None, str | None]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self
        connect = sqlite3.connect

        class Connection(sqlite3.Connection):
            statement = ""

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                self.statement = sql
                return super().execute(sql, *args)

            def authorize(self, action: int, arg1: str | None, arg2: str | None, *_rest: Any) -> int:
                if recorder.call is not None:
                    recorder.events.append((recorder.call, self.statement, action, arg1, arg2))
                return sqlite3.SQLITE_OK

        def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = connect(*args, factory=Connection, **kwargs)
            conn.set_authorizer(conn.authorize)
            return conn

        monkeypatch.setattr(sqlite3, "connect", recording_connect)


PROJECT_DELETE = "DELETE FROM projects WHERE id = ?"
REOPEN_DELETE = "DELETE FROM intents WHERE id = ? AND project_id = ?"
# What the cascade's code names, as SQLite 3.53.1 reports it: the deletes, and the lookups by the keys.
CASCADE_EVENTS = {
    (sqlite3.SQLITE_DELETE, "repair_runs", None),
    (sqlite3.SQLITE_DELETE, "repair_reviews", None),
    (sqlite3.SQLITE_READ, "repair_runs", "intent_id"),
    (sqlite3.SQLITE_READ, "repair_runs", "project_id"),
    (sqlite3.SQLITE_READ, "repair_runs", "run_id"),
    (sqlite3.SQLITE_READ, "repair_reviews", "run_id"),
}


# SV43
@pytest.mark.parametrize("switch", ["off", "on"])
def test_existing_endpoints_touch_repair_tables_only_through_the_delete_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    seed_point = _seed_point()
    authorizations = _Authorizations()
    created: list[int] = []

    def before(client: TestClient, index: int, call: Any) -> None:
        authorizations.call = None
        if index == seed_point:
            _seed_history(run_path, repair_rows=True)
        if switch == "on":  # every project switched on, outside the script's calls
            with closing(sqlite3.connect(str(run_path))) as conn:
                conn.execute("UPDATE projects SET repair_enabled = 1")
                conn.commit()
        authorizations.call = index

    def after(client: TestClient, index: int, call: Any) -> None:
        authorizations.call = None

    def create_with_the_switch_on(client: TestClient) -> None:
        # The one existing endpoint that writes the switch: a project created with it on.
        authorizations.call = len(SCRIPT)
        response = client.post("/projects", json={"title": "z", "origin": "o", "goal": "g", "repair_enabled": True})
        authorizations.call = None
        assert response.status_code == 201
        created.append(len(SCRIPT))

    run_path = tmp_path / switch / "cairn.db"
    authorizations.install(monkeypatch)
    run = _replay(
        tmp_path / switch,
        monkeypatch,
        before=before,
        after=after,
        extra=create_with_the_switch_on if switch == "on" else None,
    )
    assert all(status < 500 for status, _body in run.answers)
    assert authorizations.events, "the authorizer saw nothing"

    naming = [event for event in authorizations.events if {event[3], event[4]} & set(REPAIR_TABLES)]
    # Only the two deleting statements name a repair table, and only as the cascade does.
    assert {statement for _call, statement, *_rest in naming} == {PROJECT_DELETE, REOPEN_DELETE}
    for statement in (PROJECT_DELETE, REOPEN_DELETE):
        assert {(action, arg1, arg2) for _call, text, action, arg1, arg2 in naming if text == statement} == (
            CASCADE_EVENTS
        ), statement
    # Those are the script's project deletes that ran their DELETE (a project with intents and repair rows, and one
    # with neither) and the reopen that succeeded: SQLite compiles the cascade with the statement, rows or not.
    calls = {(SCRIPT[call].method, SCRIPT[call].path) for call, *_rest in naming}
    assert calls == {
        ("DELETE", "/projects/proj_002"),
        ("DELETE", "/projects/proj_006"),
        ("POST", "/projects/proj_001/reopen"),
    }
    # None inserts into or updates a repair table.
    assert not [
        event
        for event in authorizations.events
        if event[2] in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and event[3] in REPAIR_TABLES
    ]
    # And only create_project with the switch on writes projects.repair_enabled.
    writes = [
        (call, statement)
        for call, statement, action, arg1, arg2 in authorizations.events
        if action == sqlite3.SQLITE_UPDATE and (arg1, arg2) == ("projects", "repair_enabled")
    ]
    assert writes == [(call, "UPDATE projects SET repair_enabled = 1 WHERE id = ?") for call in created]

    # The cascade did its part: proj_002's earlier runs went with it, and reopen deleted none of proj_001's.
    with closing(sqlite3.connect(str(run_path))) as conn:
        assert conn.execute("SELECT run_id FROM repair_runs ORDER BY run_id").fetchall() == [
            ("hist-0001",),
            ("hist-0002",),
        ]
        assert conn.execute("SELECT run_id FROM repair_reviews").fetchall() == [("hist-0001",)]


def _drop(tables: tuple[str, ...]) -> Callable[[Path], None]:
    def prepare(path: Path) -> None:
        with closing(sqlite3.connect(str(path))) as conn:
            for name in tables:
                conn.execute(f"DROP TABLE {name}")
            conn.commit()

    return prepare


# SV44
@pytest.mark.parametrize(
    "dropped",
    [("repair_reviews",), ("repair_runs",), ("repair_reviews", "repair_runs")],
    ids=["repair_reviews", "repair_runs", "both"],
)
def test_a_dropped_repair_table_never_fails_an_existing_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dropped: tuple[str, ...]
) -> None:
    base = _replay(tmp_path / "base", monkeypatch)
    assert all(status < 500 for status, _body in base.answers)

    def repair_endpoints(client: TestClient) -> None:
        # The run endpoints need their tables: 500, as before. A run is there for them to find if its table is.
        project = client.post("/projects", json={"title": "r", "origin": "o", "goal": "g", "repair_enabled": True})
        project_id = project.json()["project"]["id"]
        client.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": "d", "creator": "c", "worker": "c"},
        )
        if "repair_runs" not in dropped:
            with closing(sqlite3.connect(str(path))) as conn:
                conn.execute(
                    "INSERT INTO repair_runs (run_id, project_id, intent_id, worker, cli_type, execution,"
                    " budget_seconds, started_at, deadline_at, registered_at, updated_at) VALUES ('run-0001', ?,"
                    " 'i001', 'c', 'codex', 'local', 1800, '2026-11-14T10:00:00Z', '2026-11-14T10:30:00Z',"
                    " '2026-11-14T10:00:00Z', '2026-11-14T10:00:00Z')",
                    (project_id,),
                )
                conn.commit()
        answers = [
            client.put("/repair/runs/run-0002", json=run_body(project_id, "i001", "c"), headers=DISPATCHER),
            client.get("/repair/runs", headers=AGENT),
            client.get("/repair/runs", params={"include_ended": True}, headers=AGENT),
            client.get("/repair/runs/run-0001", headers=AGENT),
            client.post("/repair/runs/run-0001/end", json={"outcome": "success"}, headers=DISPATCHER),
            client.post("/repair/runs/run-0001/reviews", json=review_body(1352), headers=AGENT),
        ]
        statuses.extend(response.status_code for response in answers)
        # The open endpoints do not use them.
        statuses.append(client.get("/repair/settings").status_code)
        statuses.append(client.put(f"/projects/{project_id}/repair", json={"enabled": False}).status_code)

    statuses: list[int] = []
    path = tmp_path / "dropped" / "cairn.db"
    run = _replay(tmp_path / "dropped", monkeypatch, tokens=True, prepare=_drop(dropped), extra=repair_endpoints)

    # The existing-endpoint script, project delete and reopen included, answers as on an intact database, and leaves
    # the same rows.
    assert run.answers == base.answers
    assert run.rows == base.rows
    assert statuses == [500] * 6 + [200, 200]
    # A dropped repair_runs takes its key with it, so today's deletes cannot reach it.
    with closing(sqlite3.connect(str(run.path))) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not set(dropped) & names
        if "repair_runs" in dropped:
            assert conn.execute("PRAGMA foreign_key_list(repair_runs)").fetchall() == []


# SV46
def test_switch_off_with_phase2_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_point = _seed_point()
    activity: list[tuple[str, int]] = []

    def base_before(client: TestClient, index: int, call: Any) -> None:
        if index == seed_point:
            _seed_history(base_path, repair_rows=False)

    def running_before(client: TestClient, index: int, call: Any) -> None:
        if index == seed_point:
            _seed_history(running_path, repair_rows=True)
        # Between the calls: the agent polls, and the dispatcher's register is refused (every switch is off).
        listed = client.get("/repair/runs", headers=AGENT)
        assert (listed.status_code, listed.json()["runs"]) == (200, [])
        refused = client.put("/repair/runs/run-0001", json=run_body("proj_001", "i002", "w1"), headers=DISPATCHER)
        activity.append((refused.json()["detail"]["code"], refused.status_code))

    base_path = tmp_path / "base" / "cairn.db"
    running_path = tmp_path / "running" / "cairn.db"
    base = _replay(tmp_path / "base", monkeypatch, before=base_before)
    running = _replay(tmp_path / "running", monkeypatch, tokens=True, before=running_before)

    # Every response and every row of the pre-existing tables, intent_issues included, as without Phase 2 running.
    assert running.answers == base.answers
    assert running.rows == base.rows
    calls = sum(1 for step in SCRIPT if isinstance(step, Call))
    assert len(activity) == calls
    # Refused by the project's state at that moment: missing, then off, then stopped or completed.
    assert {(code, status) for code, status in activity} == {
        ("project_not_found", 404),
        ("repair_disabled", 409),
        ("project_inactive", 403),
    }
    # The script's delete of proj_002 took its earlier repair rows with it; proj_001's stay, and nothing new was
    # recorded for the run that ended after its window closed, because the switch is off.
    with closing(sqlite3.connect(str(running_path))) as conn:
        assert conn.execute("SELECT run_id, settled_window FROM repair_runs ORDER BY run_id").fetchall() == [
            ("hist-0001", 1),
            ("hist-0002", 1),
        ]
        assert conn.execute("SELECT run_id, source FROM repair_reviews").fetchall() == [("hist-0001", "agent")]
        assert conn.execute("SELECT count(*) FROM intent_issues WHERE run_id LIKE 'hist-%'").fetchone() == (1,)
