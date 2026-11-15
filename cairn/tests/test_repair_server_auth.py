"""Repair layer (shadow mode): the open endpoints, the role tokens and the error shapes (spec 6.6, 6.7, 7, 6.0).

SV6: the Repair switch, PUT /projects/{id}/repair, open like every endpoint on main, writes and logs (at WARNING, so it
shows with no logging configuration) only a change. SV10, SV11: today's settings stay as they are, and the repair
settings are read-only constants. SV12-SV15: the run endpoints need their role's token, compared as bytes in constant
time, and fail closed; a token is read once at startup, from a file or the environment, and refused (never logged)
when it is unreadable, not printable ASCII, short, or the other role's. SV47: errors raised by repair code have one
coded shape, FastAPI's own 422 is kept, and no exception handler is added.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_repair_server import Clock, at, heartbeat, new_project, traced_sql
from test_repair_server_support import (
    AGENT,
    AGENT_TOKEN,
    AUTH_LOGGER,
    DISPATCHER,
    DISPATCHER_TOKEN,
    REPAIR_LOGGER,
    REVIEW_RULES,
    capture_repair_logs,
    coded,
    freeze_clock,
    poll,
    post_review,
    put_run,
    records,
    register,
    review_body,
    run_body,
    serve,
    set_switch,
    table,
    validation_locs,
)

from cairn.server import db, repair_auth
from cairn.server.app import app
from cairn.server.models import ProjectMeta
from cairn.server.routers import repair as repair_router

AUTH = repair_auth.AUTH_HEADER
TOKEN_VARIABLES = (
    "CAIRN_REPAIR_AGENT_TOKEN",
    "CAIRN_REPAIR_AGENT_TOKEN_FILE",
    "CAIRN_REPAIR_DISPATCHER_TOKEN",
    "CAIRN_REPAIR_DISPATCHER_TOKEN_FILE",
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return freeze_clock(monkeypatch)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    yield from serve(tmp_path, monkeypatch)


@pytest.fixture
def no_token_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _create(client: TestClient, **extra: Any) -> dict[str, Any]:
    response = client.post("/projects", json={"title": "t", "origin": "start", "goal": "finish", **extra})
    assert response.status_code == 201, response.text
    return response.json()["project"]


def _stored_switch(project_id: str) -> int:
    return table("projects", "WHERE id = ?", (project_id,))[0]["repair_enabled"]


# --------------------------------------------------------------------------- SV6: the switch (spec 6.6)


# SV6
def test_switch_endpoint(api: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    capture_repair_logs(caplog)
    project = _create(api)
    project_id = project["id"]
    assert project["repair_enabled"] is False
    caplog.clear()

    # On: the answer is the project's ProjectMeta, which every project read now shows; one UPDATE, one WARNING line.
    with traced_sql(monkeypatch) as statements:
        response = api.put(f"/projects/{project_id}/repair", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json() == {**project, "repair_enabled": True}
    assert ProjectMeta.model_validate(response.json()).repair_enabled is True
    assert api.get(f"/projects/{project_id}").json()["project"] == response.json()
    assert next(item for item in api.get("/projects").json() if item["id"] == project_id)["repair_enabled"] is True
    assert _stored_switch(project_id) == 1
    assert [statement for statement in statements if statement.startswith("UPDATE")] == [
        f"UPDATE projects SET repair_enabled = 1 WHERE id = '{project_id}'"  # noqa: S608 (an expected trace line)
    ]
    assert records(caplog, REPAIR_LOGGER) == [
        (logging.WARNING, f"repair switch project={project_id} enabled=True client=-")
    ]

    # The same value again: the same answer, no write, no second record.
    caplog.clear()
    with traced_sql(monkeypatch) as statements:
        again = api.put(f"/projects/{project_id}/repair", json={"enabled": True})
    assert (again.status_code, again.json()) == (200, response.json())
    assert not any(statement.startswith("UPDATE") for statement in statements)
    assert records(caplog, REPAIR_LOGGER) == []

    # Off, then off again.
    off = set_switch(api, project_id, False)
    assert off == {**project, "repair_enabled": False}
    assert _stored_switch(project_id) == 0
    assert set_switch(api, project_id, False) == off
    assert records(caplog, REPAIR_LOGGER) == [
        (logging.WARNING, f"repair switch project={project_id} enabled=False client=-")
    ]

    # 404 with the plain detail the UI shows; 422 in FastAPI's shape, as bootstrap_enabled gets for these values.
    caplog.clear()
    missing = api.put("/projects/proj_999/repair", json={"enabled": True})
    assert (missing.status_code, missing.json()) == (404, {"detail": "Project not found"})
    for value in ("sometimes", None, 2, "shadow", [], {"enabled": True}):
        assert validation_locs(api.put(f"/projects/{project_id}/repair", json={"enabled": value})) == [
            ["body", "enabled"]
        ], value
    assert validation_locs(api.put(f"/projects/{project_id}/repair", json={})) == [["body", "enabled"]]
    assert _stored_switch(project_id) == 0
    assert records(caplog, REPAIR_LOGGER) == []

    # Allowed in any project status: a stopped project, and a completed one.
    assert api.put(f"/projects/{project_id}/status", json={"status": "stopped"}).status_code == 200
    stopped = set_switch(api, project_id, True)
    assert (stopped["status"], stopped["repair_enabled"]) == ("stopped", True)
    assert api.put(f"/projects/{project_id}/status", json={"status": "active"}).status_code == 200
    completed_id = _create(api)["id"]
    response = api.post(
        f"/projects/{completed_id}/complete", json={"from": ["origin"], "description": "done", "worker": "reasoner"}
    )
    assert response.status_code == 200, response.text
    completed = set_switch(api, completed_id, True)
    assert (completed["status"], completed["repair_enabled"]) == ("completed", True)
    assert records(caplog, REPAIR_LOGGER) == [
        (logging.WARNING, f"repair switch project={project_id} enabled=True client=-"),
        (logging.WARNING, f"repair switch project={completed_id} enabled=True client=-"),
    ]

    # client= names the connection's address only when it is a plain IP address: "testclient" above is not one,
    # and neither is an IPv6 address with a scope id, which ipaddress.ip_address() accepts.
    for host, shown, enabled in (("203.0.113.9", "203.0.113.9", False), ("::1%x enabled=False", "-", True)):
        caplog.clear()
        with TestClient(app, client=(host, 50000)) as other:
            assert other.put(f"/projects/{project_id}/repair", json={"enabled": enabled}).status_code == 200
        assert records(caplog, REPAIR_LOGGER) == [
            (logging.WARNING, f"repair switch project={project_id} enabled={enabled} client={shown}")
        ]


# --------------------------------------------------------------------------- SV10, SV11: settings (spec 4.4, 6.7)


# SV10
def test_get_and_put_settings_are_unchanged(api: TestClient, clock: Clock) -> None:
    assert api.get("/settings").content == b'{"intent_timeout":15,"reason_timeout":15}'
    # With Phase 2 running: a run registered, the agent polling.
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, "run-0001", project_id)
    poll(api)

    response = api.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert (response.status_code, response.content) == (200, b'{"intent_timeout":30,"reason_timeout":45}')
    assert api.get("/settings").content == b'{"intent_timeout":30,"reason_timeout":45}'
    # Repair keys are no settings: ignored like any unknown key, and the repair settings keep their constants.
    response = api.put(
        "/settings",
        json={"intent_timeout": 30, "reason_timeout": 45, "shadow": False, "slice_seconds": 900, "repair": {}},
    )
    assert (response.status_code, response.content) == (200, b'{"intent_timeout":30,"reason_timeout":45}')
    repair_settings = api.get("/repair/settings").json()
    assert (repair_settings["shadow"], repair_settings["slice_seconds"]) == (True, 600)
    # Today's refusals are unchanged.
    assert validation_locs(api.put("/settings", json={"intent_timeout": 4, "reason_timeout": 45})) == [
        ["body", "intent_timeout"]
    ]
    with db.get_conn() as conn:
        assert [row["name"] for row in conn.execute("PRAGMA table_info(settings)")] == [
            "intent_timeout",
            "reason_timeout",
        ]
        assert [tuple(row) for row in conn.execute("SELECT * FROM settings")] == [(30, 45)]


SETTINGS = {
    "shadow": True,
    "slice_seconds": 600,
    "max_extensions_per_run": 0,
    "max_extended_runs": 0,
    "max_detached_per_project": 0,
    "max_detached_total": 0,
    "max_detached_seconds": 0,
    "review": REVIEW_RULES,
    "roles_configured": {"agent": True, "dispatcher": True},
    "agent_last_seen_at": None,
}


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    return set()


# SV11
def test_repair_settings_are_read_only_constants(api: TestClient, clock: Clock) -> None:
    response = api.get("/repair/settings")  # open: no token
    assert response.status_code == 200
    assert response.json() == SETTINGS
    assert list(response.json()) == list(SETTINGS)
    assert not any("lapse" in key for key in _keys(response.json()))

    # No PUT and no other method: FastAPI's 405, and nothing changes.
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        refused = api.request(method, "/repair/settings", json={"shadow": False, "slice_seconds": 900})
        assert (refused.status_code, refused.json()) == (405, {"detail": "Method Not Allowed"}), method
    assert api.put("/settings", json={"intent_timeout": 20, "reason_timeout": 20}).status_code == 200
    assert api.get("/repair/settings").json() == SETTINGS

    # agent_last_seen_at is the agent's last default poll; evaluation reads do not record one.
    project_id = new_project(api)
    heartbeat(api, project_id)
    register(api, "run-0001", project_id)
    clock.to(5)
    poll(api, include_ended=True)
    assert api.get("/repair/settings").json()["agent_last_seen_at"] is None
    clock.to(10)
    poll(api)
    assert api.get("/repair/settings").json()["agent_last_seen_at"] == at(10)
    clock.to(20)
    poll(api, include_ended=True)
    assert api.get("/repair/runs/run-0001", headers=AGENT).status_code == 200
    assert api.get("/repair/settings").json() == {**SETTINGS, "agent_last_seen_at": at(10)}

    # roles_configured says which roles have a token.
    for agent, dispatcher in ((AGENT_TOKEN, None), (None, DISPATCHER_TOKEN), (None, None)):
        repair_auth.configure_tokens(agent, dispatcher)
        assert api.get("/repair/settings").json()["roles_configured"] == {
            "agent": agent is not None,
            "dispatcher": dispatcher is not None,
        }


# --------------------------------------------------------------------------- SV12-SV15: role tokens (spec 7)


# Bodies that are not JSON: cut short, plain text, and bytes that are not UTF-8.
NOT_JSON = (b'{"project_id": ', b"not json", b"\xff\xfe{}")


def _refused_headers(own: str, other: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("missing", {}),
        ("empty", {AUTH: ""}),
        ("wrong", {AUTH: "w" * 40}),
        ("the other role's", {AUTH: other}),
        ("cut by one character", {AUTH: own[:-1]}),
        ("one character more", {AUTH: own + "x"}),
        ("other letter case", {AUTH: own.upper()}),
        ("non-ASCII bytes", {AUTH: b"\xe9\xe9\xe9"}),
        ("own token then UTF-8", {AUTH: own.encode("ascii") + "\u00e9".encode()}),
        ("other headers", {"Authorization": f"Bearer {own}", "X-API-Key": own}),
    ]


def _agent_calls(project_id: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("GET", "/repair/runs", None),
        ("GET", "/repair/runs/run-0001", None),
        ("POST", "/repair/runs/run-0001/reviews", review_body(1352)),
    ]


def _dispatcher_calls(project_id: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("PUT", "/repair/runs/run-0002", run_body(project_id, "i002", "w2")),
        ("POST", "/repair/runs/run-0001/end", {"outcome": "success"}),
    ]


def _check_role(
    client: TestClient,
    calls: list[tuple[str, str, dict[str, Any] | None]],
    own: str,
    other: str,
    allowed: dict[str, int],
    unchanged: Any,
) -> None:
    """Every refused header gets 401 on every call, then `unchanged(path)` checks they had no effect, then the role's
    own token gets past the check.
    """
    for method, path, body in calls:
        for name, headers in _refused_headers(own, other):
            response = client.request(method, path, json=body, headers=headers)
            detail = coded(response, 401, "unauthorized")
            assert set(detail) == {"code", "message"}, (path, name)
        # Refused before any other check: a token-less request whose body is not JSON (FastAPI parses a body before it
        # resolves a route's dependencies), whose body fails its model, or whose run id fails its rule, still gets 401.
        for content in NOT_JSON:
            response = client.request(method, path, content=content, headers={"Content-Type": "application/json"})
            coded(response, 401, "unauthorized")
        if body is not None:
            coded(client.request(method, path, json={"unknown": True}), 401, "unauthorized")
        coded(client.request(method, path.replace("run-0001", "x%0Abad"), json=body), 401, "unauthorized")
        unchanged(path)
        authorized = client.request(method, path, json=body, headers={AUTH: own})
        assert authorized.status_code == allowed[path], (path, authorized.text)


# SV12
def test_agent_endpoints_need_the_agent_token(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    heartbeat(api, project_id)
    register(api, "run-0001", project_id)
    clock.to(1352)
    heartbeat(api, project_id)
    calls = _agent_calls(project_id)

    def unchanged(path: str) -> None:
        # A refused poll is not recorded (the first authorized call is the poll), and a refused review is not stored.
        seen = api.get("/repair/settings").json()["agent_last_seen_at"]
        assert seen == (None if path == "/repair/runs" else at(1352)), path
        assert table("repair_reviews") == [], path

    _check_role(
        api,
        calls,
        AGENT_TOKEN,
        DISPATCHER_TOKEN,
        {"/repair/runs": 200, "/repair/runs/run-0001": 200, "/repair/runs/run-0001/reviews": 201},
        unchanged,
    )
    assert api.get("/repair/settings").json()["agent_last_seen_at"] == at(1352)
    assert len(table("repair_reviews")) == 1

    # Fail closed: with no agent token configured, every agent endpoint answers 503, the right token included, and
    # whatever the body.
    repair_auth.configure_tokens(None, DISPATCHER_TOKEN)
    for method, path, body in calls:
        for headers in ({}, AGENT, {AUTH: DISPATCHER_TOKEN}):
            detail = coded(api.request(method, path, json=body, headers=headers), 503, "token_not_configured")
            assert set(detail) == {"code", "message"}
            not_json = {**headers, "Content-Type": "application/json"}
            coded(api.request(method, path, content=NOT_JSON[0], headers=not_json), 503, "token_not_configured")
    # The dispatcher's endpoints still work.
    assert api.post("/repair/runs/run-0001/end", json={"outcome": "success"}, headers=DISPATCHER).status_code == 200


# SV13
def test_dispatcher_endpoints_need_the_dispatcher_token(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    heartbeat(api, project_id)
    heartbeat(api, project_id, "i002", "w2")
    register(api, "run-0001", project_id)
    calls = _dispatcher_calls(project_id)
    before = table("repair_runs")

    def unchanged(path: str) -> None:
        # A refused register creates no run (the first authorized call is the register), and a refused /end ends none.
        current = table("repair_runs")
        assert current[: len(before)] == before, path
        registered = ["run-0001"] if path == "/repair/runs/run-0002" else ["run-0001", "run-0002"]
        assert [row["run_id"] for row in current] == registered, path

    _check_role(
        api,
        calls,
        DISPATCHER_TOKEN,
        AGENT_TOKEN,
        {"/repair/runs/run-0002": 201, "/repair/runs/run-0001/end": 200},
        unchanged,
    )
    assert [(row["run_id"], row["end_reason"]) for row in table("repair_runs")] == [
        ("run-0001", "dispatcher"),
        ("run-0002", None),
    ]

    repair_auth.configure_tokens(AGENT_TOKEN, None)
    for method, path, body in calls:
        for headers in ({}, DISPATCHER, {AUTH: AGENT_TOKEN}):
            coded(api.request(method, path, json=body, headers=headers), 503, "token_not_configured")
            not_json = {**headers, "Content-Type": "application/json"}
            coded(api.request(method, path, content=NOT_JSON[1], headers=not_json), 503, "token_not_configured")
    assert api.get("/repair/runs", headers=AGENT).status_code == 200


def _secret_free(caplog: pytest.LogCaptureFixture, secrets: list[str]) -> None:
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for secret in secrets:
            assert secret not in text, record.name


# SV14
def test_short_identical_or_unreadable_tokens_are_refused_and_never_logged(
    api: TestClient, tmp_path: Path, no_token_variables: None, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    short = "s" * 31
    non_ascii = "\u00e9" * 40
    with_tab = "t" * 20 + "\t" + "t" * 20
    fifo = tmp_path / "token.fifo"
    os.mkfifo(fifo)
    not_utf8 = tmp_path / "not-utf8.token"
    not_utf8.write_bytes(b"\xff" * 40)
    oversized = tmp_path / "oversized.token"
    oversized.write_text("o" * (repair_auth.MAX_TOKEN_FILE_BYTES + 1))
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    directory = tmp_path / "token-dir"
    directory.mkdir()
    valid_file = tmp_path / "valid.token"
    valid_file.write_text(AGENT_TOKEN + "\n")
    cases = [
        # (environment, agent refusal, dispatcher refusal); None: configured.
        (
            {"CAIRN_REPAIR_AGENT_TOKEN": short, "CAIRN_REPAIR_DISPATCHER_TOKEN": DISPATCHER_TOKEN},
            "shorter_than_32",
            None,
        ),
        (
            {"CAIRN_REPAIR_AGENT_TOKEN": AGENT_TOKEN, "CAIRN_REPAIR_DISPATCHER_TOKEN": AGENT_TOKEN},
            "identical",
            "identical",
        ),
        (
            {"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(valid_file), "CAIRN_REPAIR_DISPATCHER_TOKEN": AGENT_TOKEN},
            "identical",
            "identical",
        ),
        (
            {"CAIRN_REPAIR_AGENT_TOKEN": short, "CAIRN_REPAIR_DISPATCHER_TOKEN": short},
            "shorter_than_32",
            "shorter_than_32",
        ),
        ({"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(tmp_path / "missing.token")}, "file_unreadable", None),
        ({"CAIRN_REPAIR_DISPATCHER_TOKEN_FILE": str(directory)}, None, "file_unreadable"),
        ({"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(fifo)}, "file_unreadable", None),
        ({"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(oversized)}, "file_unreadable", None),
        ({"CAIRN_REPAIR_AGENT_TOKEN": non_ascii, "CAIRN_REPAIR_DISPATCHER_TOKEN": with_tab}, "not_ascii", "not_ascii"),
        ({"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(not_utf8)}, "not_ascii", None),
        ({"CAIRN_REPAIR_AGENT_TOKEN": "\u00e9"}, "not_ascii", None),  # not_ascii is checked before the length
        (
            {"CAIRN_REPAIR_AGENT_TOKEN_FILE": str(empty), "CAIRN_REPAIR_DISPATCHER_TOKEN": "   "},
            "shorter_than_32",
            "shorter_than_32",
        ),
    ]
    for environ, agent_reason, dispatcher_reason in cases:
        repair_auth.reset()
        caplog.clear()
        started = time.monotonic()
        repair_auth.load_tokens(environ)
        assert time.monotonic() - started < 1.0  # a FIFO with no writer does not hold up the start
        expected = [
            (logging.WARNING, f"repair token rejected role={role} reason={reason}")
            for role, reason in (("agent", agent_reason), ("dispatcher", dispatcher_reason))
            if reason is not None
        ]
        configured = {
            "agent": agent_reason is None and any("AGENT" in name for name in environ),
            "dispatcher": dispatcher_reason is None and any("DISPATCHER" in name for name in environ),
        }
        assert records(caplog, AUTH_LOGGER) == [
            *expected,
            (
                logging.INFO,
                f"repair tokens configured agent={configured['agent']} dispatcher={configured['dispatcher']}",
            ),
        ], environ
        assert repair_auth.roles_configured() == configured, environ
        # A refused role fails closed.
        if agent_reason is not None:
            coded(api.get("/repair/runs", headers=AGENT), 503, "token_not_configured")
        if dispatcher_reason is not None:
            coded(put_run(api, "run-0001", run_body("proj_001")), 503, "token_not_configured")
        _secret_free(caplog, [short, non_ascii, with_tab, AGENT_TOKEN, DISPATCHER_TOKEN, "\u00e9"])


# SV15
def test_tokens_load_from_env_and_file_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock, no_token_variables: None, caplog
) -> None:
    caplog.set_level(logging.INFO, logger=AUTH_LOGGER)
    token_file = tmp_path / "secrets" / "repair-agent.token"
    token_file.parent.mkdir()
    token_file.write_text(f"\n  {AGENT_TOKEN}  \n")  # the stripped content is the token
    monkeypatch.setenv("CAIRN_REPAIR_AGENT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CAIRN_REPAIR_DISPATCHER_TOKEN", f" {DISPATCHER_TOKEN}\n")

    server = serve(tmp_path, monkeypatch, tokens=False)
    assert repair_auth.roles_configured() == {"agent": False, "dispatcher": False}  # not read before the start
    client = next(server)
    try:
        # The app's lifespan read both.
        assert records(caplog, AUTH_LOGGER) == [(logging.INFO, "repair tokens configured agent=True dispatcher=True")]
        assert client.get("/repair/settings").json()["roles_configured"] == {"agent": True, "dispatcher": True}
        assert client.get("/repair/runs", headers=AGENT).status_code == 200
        coded(put_run(client, "run-0001", run_body("proj_999")), 404, "project_not_found")  # past the token check

        # Read once: a changed file, or another start of the app in the same process, changes nothing.
        new_token = "test-agent-token-changed-0123456789abc"  # noqa: S105 (a fake token for tests)
        token_file.write_text(new_token)
        monkeypatch.setenv("CAIRN_REPAIR_DISPATCHER_TOKEN", "test-dispatcher-token-changed-012345678")
        with TestClient(app) as again:
            assert again.get("/repair/runs", headers=AGENT).status_code == 200
            coded(again.get("/repair/runs", headers={AUTH: new_token}), 401, "unauthorized")
            coded(put_run(again, "run-0001", run_body("proj_999")), 404, "project_not_found")
    finally:
        server.close()

    # A restart reads the new values. With a file and a value for one role, the file is used.
    monkeypatch.setenv("CAIRN_REPAIR_AGENT_TOKEN", "test-agent-token-from-the-value-0123456")
    (tmp_path / "restart").mkdir()
    server = serve(tmp_path / "restart", monkeypatch, tokens=False)
    client = next(server)
    try:
        assert client.get("/repair/runs", headers={AUTH: new_token}).status_code == 200
        coded(
            client.get("/repair/runs", headers={AUTH: "test-agent-token-from-the-value-0123456"}), 401, "unauthorized"
        )
        coded(client.get("/repair/runs", headers=AGENT), 401, "unauthorized")
        assert (
            client.put(
                "/repair/runs/run-0001",
                json=run_body("proj_999"),
                headers={AUTH: "test-dispatcher-token-changed-012345678"},
            ).status_code
            == 404
        )
    finally:
        server.close()

    # Without any variable the server starts with both roles unconfigured, and the run endpoints fail closed.
    for name in TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "bare").mkdir()
    server = serve(tmp_path / "bare", monkeypatch, tokens=False)
    client = next(server)
    try:
        assert client.get("/repair/settings").json()["roles_configured"] == {"agent": False, "dispatcher": False}
        coded(client.get("/repair/runs", headers=AGENT), 503, "token_not_configured")
        coded(put_run(client, "run-0001", run_body("proj_999")), 503, "token_not_configured")
    finally:
        server.close()


# --------------------------------------------------------------------------- SV47: error shapes (spec 6.0)


# SV47
def test_error_shapes(api: TestClient, clock: Clock) -> None:
    project_id = new_project(api, intents=2)
    heartbeat(api, project_id)
    register(api, "run-0001", project_id)

    # Errors raised by repair code: {"detail": {"code", "message", ...}}.
    coded(api.get("/repair/runs"), 401, "unauthorized")
    coded(put_run(api, "run-0002", run_body(project_id), headers={}), 401, "unauthorized")
    repair_auth.configure_tokens(AGENT_TOKEN, None)
    coded(put_run(api, "run-0002", run_body(project_id)), 503, "token_not_configured")
    repair_auth.configure_tokens(AGENT_TOKEN, DISPATCHER_TOKEN)
    coded(api.get("/repair/runs/run-0009", headers=AGENT), 404, "run_not_found")
    coded(api.post("/repair/runs/run-0009/end", json={"outcome": "success"}, headers=DISPATCHER), 404, "run_not_found")
    coded(post_review(api, "run-0009", review_body(1352)), 404, "run_not_found")
    coded(put_run(api, "run-0002", run_body("proj_999")), 404, "project_not_found")
    coded(put_run(api, "run-0002", run_body(project_id, "i009")), 404, "intent_not_found")
    conflict = coded(put_run(api, "run-0001", run_body(project_id, budget_seconds=3600)), 409, "run_conflict")
    assert conflict["fields"] == ["budget_seconds"]
    coded(put_run(api, "run-0002", run_body(project_id, "i002", "w2")), 409, "not_claimed")
    coded(put_run(api, "abcdefgh%0A", run_body(project_id)), 422, "bad_run_id")
    coded(put_run(api, "run-0002", run_body(project_id, started_seconds_ago=1801)), 422, "bad_started_seconds_ago")
    coded(api.get("/repair/runs", params={"cursor": "abc"}, headers=AGENT), 422, "bad_cursor")
    stopped = new_project(api)
    assert api.put(f"/projects/{stopped}/status", json={"status": "stopped"}).status_code == 200
    coded(put_run(api, "run-0003", run_body(stopped)), 403, "project_inactive")

    # A body or parameter that fails its model keeps FastAPI's own 422: {"detail": [{"loc", "msg", "type", ...}]}.
    assert validation_locs(put_run(api, "run-0002", {**run_body(project_id), "budget_seconds": 0})) == [
        ["body", "budget_seconds"]
    ]
    assert validation_locs(api.post("/repair/runs/run-0001/end", json={"outcome": "Success"}, headers=DISPATCHER)) == [
        ["body", "outcome"]
    ]
    assert validation_locs(post_review(api, "run-0001", {**review_body(1352), "decision": "stop"})) == [
        ["body", "decision"]
    ]
    # So does a body that is not JSON, once the token has been checked.
    not_json = {**DISPATCHER, "Content-Type": "application/json"}
    assert validation_locs(api.put("/repair/runs/run-0002", content=NOT_JSON[0], headers=not_json)) == [["body", 15]]
    assert validation_locs(api.get("/repair/runs", params={"limit": 0}, headers=AGENT)) == [["query", "limit"]]
    assert validation_locs(api.get("/repair/runs", params={"include_ended": "maybe"}, headers=AGENT)) == [
        ["query", "include_ended"]
    ]
    assert validation_locs(api.put(f"/projects/{project_id}/repair", json={"enabled": "sometimes"})) == [
        ["body", "enabled"]
    ]
    # The switch, which the UI calls, keeps a plain string, as get_project_or_404 raises it.
    assert api.put("/projects/proj_999/repair", json={"enabled": True}).json() == {"detail": "Project not found"}
    # An unknown route keeps FastAPI's own answer, which a client tells from a repair answer by its shape.
    unknown = api.get("/repair/unknown", headers=AGENT)
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})
    # Existing endpoints keep their 422 shape.
    assert validation_locs(api.post("/projects", json={"title": "t", "origin": "o"})) == [["body", "goal"]]

    # No exception handler is added: the app has a bare FastAPI()'s, as at ee0f227. Nor any middleware.
    assert set(app.exception_handlers) == set(FastAPI().exception_handlers)
    assert app.user_middleware == []

    # The routes the repair router adds, and the app serves: the five run endpoints, the switch and the settings.
    added = {(route.path, tuple(sorted(route.methods))) for route in repair_router.router.routes}
    assert added == {
        ("/repair/runs/{run_id}", ("PUT",)),
        ("/repair/runs/{run_id}/end", ("POST",)),
        ("/repair/runs", ("GET",)),
        ("/repair/runs/{run_id}", ("GET",)),
        ("/repair/runs/{run_id}/reviews", ("POST",)),
        ("/projects/{project_id}/repair", ("PUT",)),
        ("/repair/settings", ("GET",)),
    }
    served = app.openapi()["paths"]
    for path, methods in added:
        assert set(served[path]) >= {method.lower() for method in methods}, path
