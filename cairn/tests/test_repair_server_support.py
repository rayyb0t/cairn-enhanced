"""Repair layer (shadow mode): what the repair endpoint tests share. No tests here.

The endpoint tests (test_repair_server_auth.py, test_repair_server_runs.py, test_repair_server_existing.py and
test_repair_server_logs.py) drive the repair endpoints over HTTP, as the dispatcher and the repair agent call them,
with the role tokens configured and the server's clock frozen (spec 16). Each test file defines its own small
fixtures from `freeze_clock` and `serve` below, because a fixture imported from another module reads as an unused
import to the linter.

The intent's side (projects, intents, heartbeats) and the frozen clock come from test_repair_server.py, the tests of
the switch column and the run lifecycle. The existing-endpoint script of SV43, SV44 and SV46 is in
test_repair_server_script.py, which the switch-off differential (tools/repair-phase2/switch_off_differential.py)
replays too, so those tests and the differential cover the same calls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_repair_server import BUDGET, ONE_CLAIM, Clock, heartbeat
from test_repair_server_script import SCRIPT, SCRIPT_CLOCK_START, Advance, Call, FakeClock

from cairn.server import db, repair, repair_auth, services
from cairn.server.app import app

# Obviously fake role tokens: at least 32 printable ASCII characters each, and different.
AGENT_TOKEN = "test-agent-token-not-a-secret-0123456789"  # noqa: S105 (a fake token for tests)
DISPATCHER_TOKEN = "test-dispatcher-token-not-a-secret-01234"  # noqa: S105 (a fake token for tests)
AGENT = {repair_auth.AUTH_HEADER: AGENT_TOKEN}
DISPATCHER = {repair_auth.AUTH_HEADER: DISPATCHER_TOKEN}

REPAIR_LOGGER = "cairn.server.repair"
AUTH_LOGGER = "cairn.server.repair_auth"
LOCATOR = {
    "container": "cairn-dispatch-proj_027",
    "prompt_marker": "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml",
    "dispatcher_started_at": "2026-11-14T10:00:00.123Z",
}
# The keys of a RunView (spec 6.3) and of its window.
RUN_VIEW_KEYS = {
    "run_id",
    "project_id",
    "intent_id",
    "intent",
    "worker",
    "cli_type",
    "execution",
    "session_id",
    "locator",
    "budget_seconds",
    "started_at",
    "deadline_at",
    "version",
    "slices_used",
    "state",
    "elapsed_seconds",
    "remaining_seconds",
    "window",
    "reviews",
    "registered_at",
    "updated_at",
    "ended_at",
    "end_reason",
    "last_alive_at",
    "outcome",
    "fact_id",
}
WINDOW_KEYS = {
    "number",
    "start_at",
    "end_at",
    "seconds",
    "review_opens_at",
    "review_closes_at",
    "review_accept_until",
    "reviewable",
    "review_status",
}
REVIEW_RULES = {
    "open_fraction": 0.75,
    "close_fraction": 0.8,
    "accept_margin_seconds": 10,
    "min_window_seconds": 350,
    "digest_time_margin_seconds": 5,
    "reviewable_cli_types": ["claudecode", "codex", "pi"],
}


# --------------------------------------------------------------------------- the server


def freeze_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """services.utcnow, frozen at START, in every server module that reads it (spec 16)."""
    from cairn.server.routers import hints as hints_router
    from cairn.server.routers import intents as intents_router
    from cairn.server.routers import projects as projects_router

    frozen = Clock()
    for module in (services, intents_router, projects_router, hints_router, repair):
        monkeypatch.setattr(module, "utcnow", frozen)
    return frozen


def serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tokens: bool = True,
    client: tuple[str, int] = ("testclient", 50000),
    raise_server_exceptions: bool = True,
) -> Iterator[TestClient]:
    """The app on a fresh database, as test_server_api.py's fixture, with both role tokens configured. The server's
    in-memory state (tokens, the agent's last poll, the failure log) starts empty, as at a start.
    """
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    repair.reset_memory()
    repair_auth.reset()
    if tokens:
        repair_auth.configure_tokens(AGENT_TOKEN, DISPATCHER_TOKEN)
    try:
        with TestClient(app, client=client, raise_server_exceptions=raise_server_exceptions) as test_client:
            yield test_client
    finally:
        repair.reset_memory()
        repair_auth.reset()


# --------------------------------------------------------------------------- the dispatcher's calls


def run_body(project_id: str, intent_id: str = "i001", worker: str = "w1", **changes: Any) -> dict[str, Any]:
    """A register body as the dispatcher sends it (spec 8.6)."""
    return {
        "project_id": project_id,
        "intent_id": intent_id,
        "worker": worker,
        "cli_type": "codex",
        "execution": "container",
        "budget_seconds": BUDGET,
        "session_id": None,
        "started_seconds_ago": 0.004,
        "locator": dict(LOCATOR),
        **changes,
    }


def put_run(client: TestClient, run_id: str, body: dict[str, Any], headers: Any = None) -> httpx.Response:
    return client.put(f"/repair/runs/{run_id}", json=body, headers=DISPATCHER if headers is None else headers)


def register(
    client: TestClient, run_id: str, project_id: str, intent_id: str = "i001", worker: str = "w1", **changes: Any
) -> dict[str, Any]:
    """A new run, registered (201); its RunView."""
    response = put_run(client, run_id, run_body(project_id, intent_id, worker, **changes))
    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    return response.json()["run"]


def end(client: TestClient, run_id: str, **fields: Any) -> dict[str, Any]:
    """One of the dispatcher's two /end calls (200); the RunView."""
    response = client.post(f"/repair/runs/{run_id}/end", json=fields, headers=DISPATCHER)
    assert response.status_code == 200, response.text
    return response.json()["run"]


def exit_end(
    client: TestClient, run_id: str, *, exited_seconds_ago: float = 0.2, timed_out: bool = False, returncode: int = 0
) -> dict[str, Any]:
    """The /end the dispatcher sends when the execute process exits."""
    return end(
        client,
        run_id,
        timed_out=timed_out,
        cancelled=False,
        returncode=returncode,
        exited_seconds_ago=exited_seconds_ago,
    )


def outcome_end(client: TestClient, run_id: str, outcome: str = "success") -> dict[str, Any]:
    """The /end the dispatcher sends after the task returns."""
    return end(client, run_id, outcome=outcome)


# --------------------------------------------------------------------------- the agent's calls


def poll(client: TestClient, **params: Any) -> dict[str, Any]:
    """GET /repair/runs with the agent token (200)."""
    response = client.get("/repair/runs", params=params, headers=AGENT)
    assert response.status_code == 200, response.text
    return response.json()


def get_run(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/repair/runs/{run_id}", headers=AGENT)
    assert response.status_code == 200, response.text
    return response.json()


def review_body(digest_elapsed_seconds: float, **changes: Any) -> dict[str, Any]:
    """A review post as the agent sends it (spec 6.5)."""
    return {
        "review_id": "rv-8b1e2f30a9c4d5e6f7a8b9c0",
        "window": 1,
        "decision": "extend",
        "reason": "The squashfs extraction is advancing steadily and the next step depends on its result.",
        "evidence": ["E14 t+21:10 second partition found", "E16 t+22:10 extraction reports 70%"],
        "next_step": "List /etc once the extraction finishes",
        "fallback": None,
        "expected_version": 1,
        "digest_elapsed_seconds": digest_elapsed_seconds,
        "transcript_cut_seconds": max(0.0, digest_elapsed_seconds - 12),
        "model": "test-model",
        "digest": "<the digest the model saw>",
        "raw_output": '{"decision": "extend"}',
        **changes,
    }


def post_review(client: TestClient, run_id: str, body: dict[str, Any], headers: Any = None) -> httpx.Response:
    return client.post(f"/repair/runs/{run_id}/reviews", json=body, headers=AGENT if headers is None else headers)


def review(client: TestClient, run_id: str, digest_elapsed_seconds: float, **changes: Any) -> dict[str, Any]:
    """A review recorded (201); the answer."""
    response = post_review(client, run_id, review_body(digest_elapsed_seconds, **changes))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- the owner's calls and time


def set_switch(client: TestClient, project_id: str, enabled: bool) -> dict[str, Any]:
    """PUT /projects/{id}/repair (200); the ProjectMeta."""
    response = client.put(f"/projects/{project_id}/repair", json={"enabled": enabled})
    assert response.status_code == 200, response.text
    return response.json()


def advance(
    client: TestClient,
    clock: Clock,
    project_id: str,
    until: float,
    claims: tuple[tuple[str, str], ...] = ONE_CLAIM,
    *,
    step: float = 10,
    polls: bool = False,
) -> None:
    """Move the clock to `until` seconds after START in steps; at each step every claim heartbeats, as today's
    heartbeat lease does while a run's task holds its intent, and, with polls, the agent polls GET /repair/runs.
    """
    while clock.elapsed < until:
        clock.to(min(clock.elapsed + step, until))
        for intent_id, worker in claims:
            heartbeat(client, project_id, intent_id, worker)
        if polls:
            poll(client)


# --------------------------------------------------------------------------- answers and records


def coded(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """The detail of a repair error, {"detail": {"code", "message", ...}}, after checking its status and code."""
    assert response.status_code == status, (status, code, response.text)
    detail = response.json()["detail"]
    assert isinstance(detail, dict), response.text
    assert detail["code"] == code, response.text
    assert isinstance(detail["message"], str) and detail["message"], response.text
    return detail


def validation_locs(response: httpx.Response) -> list[list[Any]]:
    """The locs of FastAPI's own 422, {"detail": [{"loc", "msg", "type", ...}]}, after checking its shape."""
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert isinstance(errors, list) and errors, response.text
    for error in errors:
        assert {"loc", "msg", "type"} <= set(error), response.text
    return [error["loc"] for error in errors]


def records(caplog: pytest.LogCaptureFixture, logger: str) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records if record.name == logger]


def repair_lines(caplog: pytest.LogCaptureFixture, prefix: str = "") -> list[str]:
    """The messages of cairn.server.repair that start with `prefix`."""
    return [message for _level, message in records(caplog, REPAIR_LOGGER) if message.startswith(prefix)]


def table(name: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """The rows of a table, in rowid order (the name and condition are the tests' own)."""
    with db.get_conn() as conn:
        query = f"SELECT * FROM {name} {where} ORDER BY rowid"  # noqa: S608 (a test's own table name)
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def issue_details(run_id: str) -> list[dict[str, Any]]:
    return [json.loads(row["detail_json"]) for row in table("intent_issues", "WHERE run_id = ?", (run_id,))]


# --------------------------------------------------------------------------- the existing-endpoint script


def script_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The script's clock (one second later at every reading) in every server module that reads utcnow. The repair
    code reads it without moving it, so repair calls between the script's calls change none of its times.
    """
    clock = FakeClock(SCRIPT_CLOCK_START)
    original = services.utcnow
    for name, module in list(sys.modules.items()):
        if name.startswith("cairn.server") and getattr(module, "utcnow", None) is original:
            monkeypatch.setattr(module, "utcnow", clock)
    monkeypatch.setattr(repair, "utcnow", lambda: clock.now.strftime(repair.TIME_FORMAT))
    return clock


def send(client: TestClient, call: Call) -> httpx.Response:
    """One call of the existing-endpoint script."""
    if call.raw is not None:
        return client.request(call.method, call.path, content=call.raw, headers={"Content-Type": "application/json"})
    return client.request(call.method, call.path, json=call.body)


def run_script(client: TestClient, clock: FakeClock, before: Any = None, after: Any = None) -> list[tuple[int, bytes]]:
    """Replay the existing-endpoint script; `before(index, call)` and `after(index, call)` run around each call.
    Returns each call's (status, body).
    """
    answers: list[tuple[int, bytes]] = []
    for index, step in enumerate(SCRIPT):
        if isinstance(step, Advance):
            clock.advance(step.seconds)
            continue
        if before is not None:
            before(index, step)
        response = send(client, step)
        answers.append((response.status_code, response.content))
        if after is not None:
            after(index, step)
    return answers


def first_call(path: str, method: str = "POST", body: Any = None) -> int:
    """The index in the script of the first call to `path` (with `body`, if given)."""
    for index, step in enumerate(SCRIPT):
        if isinstance(step, Call) and (step.method, step.path) == (method, path) and body in (None, step.body):
            return index
    raise AssertionError(f"no {method} {path} in the script")


# The tables of the base schema (ee0f227), which existing endpoints write.
PRE_EXISTING_TABLES = (
    "settings",
    "projects",
    "facts",
    "intents",
    "intent_sources",
    "hints",
    "counters",
    "scoped_counters",
    "intent_issues",
    "sqlite_sequence",
)


def pre_existing_rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every pre-existing table of the database file, in rowid order."""
    with sqlite3.connect(str(path)) as conn:
        return {
            name: conn.execute(f"SELECT rowid, * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608 (fixed names)
            for name in PRE_EXISTING_TABLES
        }


def capture_repair_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Capture every record of the server's repair loggers, INFO and DEBUG included."""
    caplog.set_level(logging.DEBUG, logger=REPAIR_LOGGER)
    caplog.set_level(logging.DEBUG, logger=AUTH_LOGGER)
