"""RepairRunsClient (spec 8.6): the register and end calls, the answer table, and the housekeeping's two reads.

The servers here are real sockets on 127.0.0.1: a plain fake for exact status codes and bodies, a bare FastAPI
app for FastAPI's own bodies, and today's Cairn app for GET /projects and GET /projects/{id}. DS22 (configuration
errors are logged once per process) is in test_repair_shadow.py.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from repair_dispatcher_support import (
    FAKE_TOKEN,
    PROXY_VARIABLES,
    REGISTER_BODY,
    REJECTED_LINE,
    RUN_ID,
    VALID_ID,
    RecordedRequest,
    Reply,
    bypass_proxies,
    fake_server,
    json_reply,
    repair_error,
    repair_records,
    reset_repair_process_state,
    serve_app,
)

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair.client import PROJECT_NOT_FOUND_BODY, Answer, RepairRunsClient, classify
from cairn.server import db
from cairn.server.app import app as cairn_app


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    reset_repair_process_state(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()


@pytest.fixture(autouse=True)
def direct_to_test_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The servers listen on 127.0.0.1: no proxy that the shell names may stand in between."""
    bypass_proxies(monkeypatch)


CAIRN_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = "tests/test_repair_dispatcher_client.py::"
# Tests that reach a server: a plain fake and uvicorn with FastAPI in this file, and DS22 in test_repair_shadow.py.
SERVER_TESTS = [
    THIS_FILE + "test_calls_send_the_token_and_the_body_unchanged_and_the_reads_send_no_token",
    THIS_FILE + "test_coded_and_code_less_404s_from_a_real_fastapi_server",
    "tests/test_repair_shadow.py::test_configuration_errors_are_logged_once_per_process",
]


def test_tests_reach_their_servers_directly_from_a_shell_with_a_proxy(tmp_path: Path) -> None:
    """A shell whose HTTP_PROXY names an intercepting proxy, and whose NO_PROXY does not cover 127.0.0.1: these
    tests still pass, and the proxy sees none of their requests. They run in a fresh pytest, with that shell's
    environment."""
    with fake_server(lambda _request: Reply(502)) as proxy:
        environment = dict(os.environ)
        environment.update(dict.fromkeys(PROXY_VARIABLES, proxy.base_url))
        environment.update(dict.fromkeys(("NO_PROXY", "no_proxy"), "example.invalid"))
        environment["PYTHONPATH"] = str(CAIRN_ROOT / "src")
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'run'}"]
        result = subprocess.run(
            [*command, *SERVER_TESTS],
            cwd=CAIRN_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert proxy.requests == []


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (200, None, "ok"),
        (201, None, "ok"),
        (204, None, "ok"),
        (0, None, "retry"),
        (408, None, "retry"),
        (429, None, "retry"),
        (500, None, "retry"),
        (502, None, "retry"),
        (504, None, "retry"),
        (503, None, "retry"),
        (503, "overloaded", "retry"),
        (503, "token_not_configured", "not_configured"),
        (401, "unauthorized", "not_configured"),
        (401, None, "not_configured"),
        (404, None, "not_configured"),
        (404, "run_not_found", "refused"),
        (404, "project_not_found", "refused"),
        (404, "intent_not_found", "refused"),
        (403, "project_inactive", "refused"),
        (409, "repair_disabled", "refused"),
        (409, "intent_concluded", "refused"),
        (409, "not_explore", "refused"),
        (409, "not_claimed", "refused"),
        (409, "run_conflict", "refused"),
        (409, "session_conflict", "refused"),
        (422, None, "failed"),
        (422, "bad_run_id", "failed"),
        (422, "bad_started_seconds_ago", "failed"),
        (400, None, "failed"),
        (405, None, "failed"),
        (307, None, "failed"),
        (501, None, "failed"),
    ],
)
def test_answer_classification_follows_the_table_of_spec_8_6(status: int, code: str | None, kind: str) -> None:
    assert classify(status, code) == kind


@pytest.mark.parametrize(
    ("status", "body", "code", "kind"),
    [
        pytest.param(404, b'{"detail":"Not Found"}', None, "not_configured", id="404-route-missing"),
        pytest.param(404, b'{"detail":"Project not found"}', None, "not_configured", id="404-string-detail"),
        pytest.param(404, b"<html>Not Found</html>", None, "not_configured", id="404-html"),
        pytest.param(404, b"", None, "not_configured", id="404-empty"),
        pytest.param(404, b'{"detail":{"message":"no code"}}', None, "not_configured", id="404-detail-without-code"),
        pytest.param(404, b'{"detail":{"code":"Run\\nNot Found"}}', None, "not_configured", id="404-unloggable-code"),
        pytest.param(
            404,
            json.dumps(repair_error("run_not_found", pad="x" * 5000)).encode(),
            None,
            "not_configured",
            id="404-oversized",
        ),
        pytest.param(
            404, json.dumps(repair_error("run_not_found")).encode(), "run_not_found", "refused", id="404-coded"
        ),
        pytest.param(
            503,
            json.dumps(repair_error("token_not_configured")).encode(),
            "token_not_configured",
            "not_configured",
            id="503-coded",
        ),
        pytest.param(503, b"Service Unavailable", None, "retry", id="503-plain"),
        pytest.param(
            409,
            json.dumps(repair_error("run_conflict", fields=["budget_seconds"])).encode(),
            "run_conflict",
            "refused",
            id="409",
        ),
        pytest.param(
            422,
            b'{"detail":[{"loc":["body","budget_seconds"],"msg":"x","type":"int_type"}]}',
            None,
            "failed",
            id="422-fastapi",
        ),
        pytest.param(
            201, b'{"created":true,"run":{"deadline_at":"2026-11-14T11:00:00Z","version":2}}', None, "ok", id="201"
        ),
    ],
)
def test_answers_are_read_from_the_servers_status_and_body(
    status: int, body: bytes, code: str | None, kind: str
) -> None:
    with fake_server(lambda _request: Reply(status, body)) as server:
        client = RepairRunsClient(server.base_url, FAKE_TOKEN)
        try:
            answers = [client.register(RUN_ID, REGISTER_BODY), client.end(RUN_ID, {"outcome": "success"})]
        finally:
            client.close()
    assert answers == [Answer(status=status, code=code, kind=kind)] * 2


def test_coded_and_code_less_404s_from_a_real_fastapi_server() -> None:
    """FastAPI's own bodies: a route that raises a coded 404 is a refusal; a server without the route is not configured."""
    app = FastAPI()

    @app.put("/repair/runs/{run_id}")
    def register(run_id: str) -> None:
        raise HTTPException(404, {"code": "project_not_found", "message": "project not found"})

    bare = FastAPI()
    with serve_app(app) as with_router, serve_app(bare) as without_router:
        answers = []
        for base_url in (with_router, without_router):
            client = RepairRunsClient(base_url, FAKE_TOKEN)
            try:
                answers.append(client.register(RUN_ID, REGISTER_BODY))
            finally:
                client.close()
    assert answers == [
        Answer(status=404, code="project_not_found", kind="refused"),
        Answer(status=404, code=None, kind="not_configured"),
    ]


def test_calls_send_the_token_and_the_body_unchanged_and_the_reads_send_no_token() -> None:
    projects = [_project_summary("proj_001")]
    with fake_server(lambda request: _route(request, projects)) as server:
        client = RepairRunsClient(server.base_url, FAKE_TOKEN)
        try:
            assert client.register(RUN_ID, REGISTER_BODY).ok
            assert client.end(RUN_ID, {"outcome": "success"}).ok
            listed = client.list_projects()
            gone = client.project_gone("proj_002")
        finally:
            client.close()
        keyed = RepairRunsClient(server.base_url, FAKE_TOKEN, api_key="test-api-key-0009")
        try:
            assert keyed.register(RUN_ID, REGISTER_BODY).ok
            keyed.list_projects()
        finally:
            keyed.close()
    assert listed is not None
    assert [project.id for project in listed] == ["proj_001"]
    assert gone is True
    register, end, listing, lookup, keyed_register, keyed_listing = server.requests
    assert (register.method, register.path) == ("PUT", f"/repair/runs/{RUN_ID}")
    assert json.loads(register.body) == REGISTER_BODY
    assert register.headers["content-type"] == "application/json"
    assert (end.method, end.path, json.loads(end.body)) == (
        "POST",
        f"/repair/runs/{RUN_ID}/end",
        {"outcome": "success"},
    )
    for request in (register, end, keyed_register):
        assert request.headers["x-cairn-repair-token"] == FAKE_TOKEN
    assert (listing.method, listing.path, lookup.method, lookup.path) == (
        "GET",
        "/projects",
        "GET",
        "/projects/proj_002",
    )
    for request in (listing, lookup, keyed_listing):
        assert "x-cairn-repair-token" not in request.headers
    for request in (register, end, listing, lookup):
        assert "x-api-key" not in request.headers
    assert keyed_register.headers["x-api-key"] == keyed_listing.headers["x-api-key"] == "test-api-key-0009"


def test_no_request_follows_a_redirect() -> None:
    with fake_server(lambda _request: json_reply(200, [])) as elsewhere:
        moved = Reply(307, b"", headers={"Location": f"{elsewhere.base_url}/projects"})
        with fake_server(lambda _request: moved) as server:
            client = RepairRunsClient(server.base_url, FAKE_TOKEN)
            try:
                answers = [client.register(RUN_ID, REGISTER_BODY), client.end(RUN_ID, {"outcome": "success"})]
                listed = client.list_projects()
                gone = client.project_gone("proj_001")
            finally:
                client.close()
    assert answers == [Answer(status=307, code=None, kind="failed")] * 2
    assert listed is None
    assert gone is False
    assert len(server.requests) == 4
    assert elsewhere.requests == []


def test_no_answer_is_status_0_and_retried_by_kind() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    client = RepairRunsClient(f"http://127.0.0.1:{port}", FAKE_TOKEN)
    try:
        assert client.register(RUN_ID, REGISTER_BODY) == Answer(status=0, code=None, kind="retry")
        assert client.end(RUN_ID, {"outcome": "success"}) == Answer(status=0, code=None, kind="retry")
        assert client.list_projects() is None
        assert client.project_gone("proj_001") is False
    finally:
        client.close()

    # A server that accepts the connection and never answers: the 3 s read timeout bounds the call.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(8)
        client = RepairRunsClient(f"http://127.0.0.1:{silent.getsockname()[1]}", FAKE_TOKEN)
        started = time.monotonic()
        try:
            answer = client.register(RUN_ID, REGISTER_BODY)
        finally:
            client.close()
        elapsed = time.monotonic() - started
    assert answer == Answer(status=0, code=None, kind="retry")
    assert 2.5 < elapsed < 6.0


def test_calls_refuse_a_bad_run_id_and_a_missing_token() -> None:
    client = RepairRunsClient("http://127.0.0.1:9")
    with pytest.raises(ValueError, match="dispatcher token"):
        client.register(RUN_ID, REGISTER_BODY)
    client = RepairRunsClient("http://127.0.0.1:9", FAKE_TOKEN)
    for run_id in ("short", f"{RUN_ID}\n", "../" + RUN_ID, "a" * 65):
        with pytest.raises(ValueError, match="run-id rule"):
            client.register(run_id, REGISTER_BODY)


def test_a_session_id_that_fails_the_rule_is_sent_as_null(caplog: pytest.LogCaptureFixture) -> None:
    """Spec 8.7's rule is applied to every body the client sends, last of all: an id that fails it is never used.

    The run is registered without one, and one WARNING gives the id's length and SHA-256 prefix only. The caller's
    body is left as it was.
    """
    hostile = "../../etc/x\nINJECTED"
    end_fields = {"timed_out": True, "cancelled": False, "returncode": 124, "exited_seconds_ago": 0.2}
    calls = [
        ("register", {**REGISTER_BODY, "session_id": VALID_ID}),  # an id that passes is sent as it is
        ("register", {**REGISTER_BODY, "session_id": hostile}),
        ("register", {**REGISTER_BODY, "session_id": "x" * 129}),
        ("register", {**REGISTER_BODY, "session_id": 7}),  # not a string: sent as null, with nothing to log
        ("end", {**end_fields, "session_id": hostile}),  # no end body has the key; one that did gets the same
        ("end", end_fields),
    ]
    with fake_server(lambda request: _route(request, [])) as server:
        client = RepairRunsClient(server.base_url, FAKE_TOKEN)
        try:
            for call, body in calls:
                before = copy.deepcopy(body)
                send = client.register if call == "register" else client.end
                assert send(RUN_ID, body).ok
                assert body == before
        finally:
            client.close()

    assert [json.loads(request.body) for request in server.requests] == [
        {**REGISTER_BODY, "session_id": VALID_ID},
        {**REGISTER_BODY, "session_id": None},
        {**REGISTER_BODY, "session_id": None},
        {**REGISTER_BODY, "session_id": None},
        {**end_fields, "session_id": None},
        end_fields,
    ]

    def fingerprint(value: str) -> tuple[int, str]:
        return len(value), hashlib.sha256(value.encode()).hexdigest()[:12]

    assert [(record.levelno, record.msg, record.args) for record in repair_records(caplog)] == [
        (logging.WARNING, REJECTED_LINE, ("proj_027", "i014", "codex-1", RUN_ID, *fingerprint(hostile))),
        (logging.WARNING, REJECTED_LINE, ("proj_027", "i014", "codex-1", RUN_ID, *fingerprint("x" * 129))),
        (logging.WARNING, REJECTED_LINE, ("-", "-", "-", RUN_ID, *fingerprint(hostile))),
    ]
    for record in caplog.records:
        assert hostile not in record.getMessage() and "\n" not in record.getMessage()


def test_calls_and_reads_pass_their_own_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register and end: 2 s to connect and 3 s to read (spec 8.5); test_no_answer_is_status_0_and_retried_by_kind
    shows the 3 s on a silent server. The housekeeping's two reads: 2 s to connect and CairnClient's read timeout,
    10 s (spec 8.6)."""
    cairn_read_timeout = inspect.signature(CairnClient).parameters["timeout"].default
    assert cairn_read_timeout == 10.0
    seen: list[tuple[str, str, Any]] = []
    real_request = requests.Session.request

    def spy(self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        seen.append((method, urlsplit(url).path, kwargs.get("timeout")))
        return real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    with fake_server(lambda request: _route(request, [_project_summary("proj_001")])) as server:
        client = RepairRunsClient(server.base_url, FAKE_TOKEN)
        try:
            assert client.register(RUN_ID, REGISTER_BODY).ok
            assert client.end(RUN_ID, {"outcome": "success"}).ok
            assert client.list_projects() is not None
            assert client.project_gone("proj_002") is True
        finally:
            client.close()
    assert seen == [
        ("PUT", f"/repair/runs/{RUN_ID}", (2.0, 3.0)),
        ("POST", f"/repair/runs/{RUN_ID}/end", (2.0, 3.0)),
        ("GET", "/projects", (2.0, cairn_read_timeout)),
        ("GET", "/projects/proj_002", (2.0, cairn_read_timeout)),
    ]


def _project_summary(project_id: str, status: str = "active") -> dict[str, Any]:
    return {
        "id": project_id,
        "title": "t",
        "status": status,
        "bootstrap_enabled": True,
        "created_at": "2026-01-01T00:00:00Z",
        "reason": None,
        "fact_count": 2,
        "intent_count": 0,
        "working_intent_count": 0,
        "unclaimed_intent_count": 0,
        "hint_count": 0,
    }


def _route(request: RecordedRequest, projects: list[dict[str, Any]]) -> Reply:
    if request.path.startswith("/repair/runs/"):
        return json_reply(201 if request.method == "PUT" else 200, {"run": {}})
    if request.path == "/projects":
        return json_reply(200, projects)
    if request.path.startswith("/projects/"):
        return Reply(404, PROJECT_NOT_FOUND_BODY)
    return json_reply(404, {"detail": "Not Found"})


@pytest.fixture
def cairn_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with serve_app(cairn_app) as base_url:
        yield base_url


def test_project_gone_needs_the_servers_exact_404_body(cairn_server: str) -> None:
    with TestClient(cairn_app) as http:
        created = http.post("/projects", json={"title": "t", "origin": "start", "goal": "finish"}).json()["project"][
            "id"
        ]
        deleted = http.post("/projects", json={"title": "t", "origin": "start", "goal": "finish"}).json()["project"][
            "id"
        ]
        assert http.delete(f"/projects/{deleted}").status_code in (200, 204)
        assert http.get("/projects/proj_999").content == PROJECT_NOT_FOUND_BODY
    assert len(PROJECT_NOT_FOUND_BODY) == 30

    client = RepairRunsClient(cairn_server)
    try:
        assert client.project_gone(created) is False
        assert client.project_gone(deleted) is True
        assert client.project_gone("proj_999") is True
        listed = client.list_projects()
    finally:
        client.close()
    assert listed is not None
    assert [project.id for project in listed] == [created]

    # A server without the route answers FastAPI's {"detail":"Not Found"}: not a deletion.
    with serve_app(FastAPI()) as bare:
        client = RepairRunsClient(bare)
        try:
            assert client.project_gone("proj_999") is False
            assert client.list_projects() is None
        finally:
            client.close()


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(Reply(404, PROJECT_NOT_FOUND_BODY + b" "), id="one-byte-more"),
        pytest.param(Reply(404, b'{"detail": "Project not found"}'), id="other-spacing"),
        pytest.param(
            Reply(404, gzip.compress(PROJECT_NOT_FOUND_BODY), headers={"Content-Encoding": "gzip"}), id="gzip"
        ),
        pytest.param(Reply(401, PROJECT_NOT_FOUND_BODY), id="401"),
        pytest.param(Reply(500, PROJECT_NOT_FOUND_BODY), id="500"),
        pytest.param(Reply(200, PROJECT_NOT_FOUND_BODY), id="200"),
    ],
)
def test_project_gone_is_false_for_any_other_answer(reply: Reply) -> None:
    with fake_server(lambda _request: reply) as server:
        client = RepairRunsClient(server.base_url)
        try:
            assert client.project_gone("proj_001") is False
        finally:
            client.close()
    assert server.requests[0].headers["accept-encoding"] == "identity"


def test_project_gone_sends_nothing_for_an_id_that_fails_the_rule() -> None:
    with fake_server(lambda _request: Reply(404, PROJECT_NOT_FOUND_BODY)) as server:
        client = RepairRunsClient(server.base_url)
        try:
            results = [
                client.project_gone(value) for value in ("../proj_001", "proj_001\n", "a/b", "..", "", "p" * 129)
            ]
        finally:
            client.close()
    assert results == [False] * 6
    assert server.requests == []


def test_project_gone_reads_at_most_64_bytes_of_a_404() -> None:
    """A 404 that announces 10 MB and then stalls: the answer comes from the first 64 bytes, at once."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve() -> None:
        connection, _address = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 10000000\r\n\r\n" + PROJECT_NOT_FOUND_BODY + b"x" * 100
            )
            stop.wait(10)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = RepairRunsClient(f"http://127.0.0.1:{listener.getsockname()[1]}")
    started = time.monotonic()
    try:
        gone = client.project_gone("proj_001")
    finally:
        client.close()
        stop.set()
        thread.join(5)
        listener.close()
    assert gone is False
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(Reply(500, b"[]"), id="500"),
        pytest.param(Reply(200, b"not json"), id="not-json"),
        pytest.param(Reply(200, b'[{"id": "proj_001"}]'), id="not-a-summary"),
        pytest.param(Reply(200, b"[" * 100000 + b"]" * 100000), id="deep"),
    ],
)
def test_list_projects_gives_no_list_for_a_bad_answer(reply: Reply) -> None:
    with fake_server(lambda _request: reply) as server:
        client = RepairRunsClient(server.base_url)
        try:
            assert client.list_projects() is None
        finally:
            client.close()
