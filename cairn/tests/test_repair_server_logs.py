"""Repair layer (shadow mode): the server's repair log lines (spec 12, 7, 6.6).

SV45. The server configures no logging, so only its WARNING lines show, bare, on `cairn serve`'s stderr: a switch
change, a rejected token and a failed authentication. Every repair line uses one of the fixed formats of spec 12 and
starts with "repair ". No line holds a token, a session id, a digest, raw model output, review text, a locator, or a
request path: an id is logged only once it matched its rule or was found in the database, a failed authentication
names the route's fixed endpoint, and a client address only when it is a plain IP address.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_repair_server import ACCEPT_UNTIL, Clock, heartbeat, new_project
from test_repair_server_support import (
    AGENT_TOKEN,
    AUTH_LOGGER,
    DISPATCHER,
    DISPATCHER_TOKEN,
    REPAIR_LOGGER,
    advance,
    coded,
    exit_end,
    freeze_clock,
    outcome_end,
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
)

import cairn
from cairn.server import repair_auth

# The complete list of the server's repair lines (spec 12): (level, logger, format).
LINES = {
    (logging.WARNING, REPAIR_LOGGER, "repair switch project=%s enabled=%s client=%s"),
    (logging.WARNING, AUTH_LOGGER, "repair token rejected role=%s reason=%s"),
    (logging.WARNING, AUTH_LOGGER, "repair auth failed role=%s endpoint=%s"),
    (logging.INFO, AUTH_LOGGER, "repair tokens configured agent=%s dispatcher=%s"),
    (
        logging.INFO,
        REPAIR_LOGGER,
        "repair run registered run=%s project=%s intent=%s worker=%s cli=%s execution=%s budget=%ss session=%s",
    ),
    (logging.INFO, REPAIR_LOGGER, "repair run updated run=%s fields=%s"),
    (logging.INFO, REPAIR_LOGGER, "repair run superseded run=%s by=%s project=%s intent=%s worker=%s"),
    (logging.INFO, REPAIR_LOGGER, "repair run ended run=%s project=%s intent=%s reason=%s outcome=%s"),
    (logging.INFO, REPAIR_LOGGER, "repair window not reviewed run=%s project=%s intent=%s window=%s why=%s"),
    (
        logging.INFO,
        REPAIR_LOGGER,
        "repair review recorded run=%s project=%s intent=%s window=%s decision=%s fallback=%s mode=shadow"
        " applied=false",
    ),
    (logging.INFO, REPAIR_LOGGER, "repair review refused run=%s code=%s"),
}

SESSION = "sess-canary-7f0e9a51c3d2"
CANARIES = {
    "agent token": AGENT_TOKEN,
    "dispatcher token": DISPATCHER_TOKEN,
    "session id": SESSION,
    "late session id": "sess-canary-late-0a1b2c",
    "wrong token": "wrong-token-canary-0123456789abcdef0123",
    "short token": "short-canary-token",
    "reason": "reason-canary text about the run",
    "evidence": "evidence-canary E14 t+21:10",
    "next step": "next-step-canary list /etc",
    "digest": "digest-canary of the run",
    "raw output": "raw-output-canary {}",
    "model": "model-canary-test",
    "locator": "locator-canary-container",
    "prompt marker": "/tmp/cairn-prompts/marker-canary/graph.yaml",
    "path": "injected",
    "escape": "\x1b",
    "forged line": "run=forged",
    "line break": "\n",
}
# A worker name built to write a forged line into the server log, if a line showed it as sent.
FORGED_WORKER = "w4\nrepair run ended run=forged project=proj_001 intent=i004 reason=dispatcher outcome=success\x1b[2K"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return freeze_clock(monkeypatch)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    yield from serve(tmp_path, monkeypatch)


class _Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port: int, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> int:
    """One request to the server on 127.0.0.1, directly (no proxy from the environment); its status."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _cairn_serve(tmp_path: Path, name: str, calls: list[tuple[str, str, Any, dict[str, str]]]) -> list[str]:
    """Run `cairn serve` in a subprocess with a temporary HOME and database and no repair or proxy-header variables,
    make the calls, stop it, and return its stderr lines.
    """
    home = tmp_path / f"home-{name}"
    home.mkdir()
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CAIRN_REPAIR_") and key != "FORWARDED_ALLOW_IPS"
    }
    env.update({"HOME": str(home), "PYTHONPATH": str(Path(cairn.__file__).resolve().parents[1])})
    command = [sys.executable, "-c", "from cairn.cli import main; main()", "serve", "--port", str(port)]
    command += ["--db-path", str(tmp_path / f"{name}.db")]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if _request(port, "GET", "/projects") == 200:
                    break
            except OSError:
                pass
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, "cairn serve did not start"
            time.sleep(0.1)
        statuses = [_request(port, method, path, body, headers) for method, path, body, headers in calls]
    finally:
        process.send_signal(signal.SIGINT)
        try:
            _stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate()
    assert all(status < 300 for status in statuses), statuses
    return stderr.splitlines()


UVICORN_PREFIXES = ("TRACE:", "DEBUG:", "INFO:", "WARNING:", "ERROR:", "CRITICAL:")


def _in_process(api: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    monotonic = _Monotonic()
    monkeypatch.setattr(repair_auth, "_monotonic", monotonic)
    kept: list[logging.LogRecord] = []

    def clear() -> None:
        kept.extend(caplog.records)
        caplog.clear()

    # Rejected tokens at a start: the reasons, never the values.
    repair_auth.reset()
    repair_auth.load_tokens(
        {"CAIRN_REPAIR_AGENT_TOKEN": CANARIES["short token"], "CAIRN_REPAIR_DISPATCHER_TOKEN": "\u00e9" * 40}
    )
    repair_auth.reset()
    repair_auth.load_tokens(
        {"CAIRN_REPAIR_AGENT_TOKEN": AGENT_TOKEN, "CAIRN_REPAIR_DISPATCHER_TOKEN": DISPATCHER_TOKEN}
    )

    # A full run: register with a locator, the session id added later, the agent's review with every text, a run
    # flagged "not reviewed", a superseded run, both /end calls, and refusals.
    project_id = new_project(api, intents=4)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"))
    for intent_id, worker in claims:
        heartbeat(api, project_id, intent_id, worker)
    locator = {"container": CANARIES["locator"], "prompt_marker": CANARIES["prompt marker"]}
    register(api, "run-0001", project_id, locator=locator)
    assert put_run(api, "run-0001", run_body(project_id, session_id=SESSION, locator=locator)).status_code == 200
    register(api, "run-0002", project_id, "i002", "w2", session_id=CANARIES["late session id"])
    register(api, "run-0003", project_id, "i003", "w3")
    # A worker name that is no id, with a line break: today's heartbeat endpoint takes it as the claim's worker, and
    # register accepts the claim's own worker. The register and supersede lines show "-" in its place.
    claimed = api.post(f"/projects/{project_id}/intents/i004/heartbeat", json={"worker": FORGED_WORKER})
    assert (claimed.status_code, claimed.json()["worker"]) == (200, FORGED_WORKER)
    register(api, "run-0005", project_id, "i004", FORGED_WORKER)
    register(api, "run-0006", project_id, "i004", FORGED_WORKER)  # supersedes run-0005
    assert [line for line in repair_lines(caplog) if "intent=i004" in line] == [
        f"repair run registered run=run-0005 project={project_id} intent=i004 worker=- cli=codex execution=container"
        " budget=1800s session=no",
        f"repair run superseded run=run-0005 by=run-0006 project={project_id} intent=i004 worker=-",
        f"repair run registered run=run-0006 project={project_id} intent=i004 worker=- cli=codex execution=container"
        " budget=1800s session=no",
    ]
    coded(put_run(api, "run-0001", run_body(project_id, session_id="sess-canary-other-99")), 409, "session_conflict")
    advance(api, clock, project_id, 1352, claims, polls=True)
    texts = {
        "reason": CANARIES["reason"],
        "evidence": [CANARIES["evidence"]],
        "next_step": CANARIES["next step"],
        "digest": CANARIES["digest"],
        "raw_output": CANARIES["raw output"],
        "model": CANARIES["model"],
    }
    review(api, "run-0001", 1352, **texts)
    coded(post_review(api, "run-0001", review_body(1352, review_id="rv-other-0001", **texts)), 409, "already_reviewed")
    register(api, "run-0004", project_id, "i003", "w3")  # supersedes run-0003
    advance(api, clock, project_id, ACCEPT_UNTIL, claims, polls=True)
    exit_end(api, "run-0001", exited_seconds_ago=1.0)
    outcome_end(api, "run-0001", "success")
    set_switch(api, project_id, False)

    # Failed authentications: the role and the route's fixed name, at most once per role in any 60 s.
    clear()
    response = api.get("/repair/runs/x%0Ainjected%1B[31mred")
    assert response.status_code == 401
    assert records(caplog, AUTH_LOGGER) == [(logging.WARNING, "repair auth failed role=agent endpoint=runs.get")]
    assert api.get("/repair/runs/x%0Ainjected%1B[31mred").status_code == 401
    assert api.get("/repair/runs", headers={repair_auth.AUTH_HEADER: CANARIES["wrong token"]}).status_code == 401
    monotonic.now += 59.9
    assert api.post("/repair/runs/x%0Ainjected/reviews", json={}).status_code == 401
    assert records(caplog, AUTH_LOGGER) == [(logging.WARNING, "repair auth failed role=agent endpoint=runs.get")]
    # The other role has its own limit; and after 60 s the agent role logs again.
    assert put_run(api, "x%0Ainjected", run_body(project_id), headers={}).status_code == 401
    monotonic.now += 0.1
    assert api.post("/repair/runs/run-0001/reviews", json=review_body(1352)).status_code == 401
    assert records(caplog, AUTH_LOGGER)[1:] == [
        (logging.WARNING, "repair auth failed role=dispatcher endpoint=runs.register"),
        (logging.WARNING, "repair auth failed role=agent endpoint=runs.review"),
    ]
    # A token-less request whose body is not JSON is refused and logged the same way: the token is checked before the
    # body is read.
    monotonic.now += 60
    for path in ("/repair/runs/run-0001/end", "/repair/runs/run-0001/reviews"):
        response = api.post(path, content=b'{"outcome": \x1b[31m', headers={"Content-Type": "application/json"})
        coded(response, 401, "unauthorized")
    assert records(caplog, AUTH_LOGGER)[3:] == [
        (logging.WARNING, "repair auth failed role=dispatcher endpoint=runs.end"),
        (logging.WARNING, "repair auth failed role=agent endpoint=runs.review"),
    ]
    # A request for an id that matches nothing (404) and one whose run id fails its rule (422) log nothing.
    clear()
    assert api.get("/repair/runs/x%0Ainjected", headers={repair_auth.AUTH_HEADER: AGENT_TOKEN}).status_code == 404
    assert put_run(api, "x%0Ainjected", run_body(project_id), headers=DISPATCHER).status_code == 422
    assert records(caplog, REPAIR_LOGGER) == records(caplog, AUTH_LOGGER) == []
    clear()

    # Every repair line of the run is one of the fixed lines of spec 12, and every one of them was written. No
    # record of the server (every logger but the test client's own HTTP library) holds a token, a session id, a text
    # of the review, a locator value or the request path.
    repair_records = [record for record in kept if record.name in (REPAIR_LOGGER, AUTH_LOGGER)]
    for record in repair_records:
        assert (record.levelno, record.name, record.msg) in LINES, record.msg
        assert record.getMessage().startswith("repair ")
    assert {(record.levelno, record.name, record.msg) for record in repair_records} == LINES
    server_records = [record for record in kept if not record.name.startswith(("httpx", "httpcore"))]
    assert server_records
    for record in server_records:
        text = record.getMessage() + repr(record.args)
        for name, canary in CANARIES.items():
            assert canary not in text, (name, record.name, text)
        assert "\u00e9" not in text and "/repair/runs/" not in text, (record.name, text)


def _under_cairn_serve(tmp_path: Path) -> None:
    project = {"title": "t", "origin": "o", "goal": "g"}
    switch = "/projects/proj_001/repair"
    stderr = _cairn_serve(
        tmp_path,
        "switch",
        [
            ("POST", "/projects", project, {}),
            ("PUT", switch, {"enabled": True}, {}),
            ("PUT", switch, {"enabled": False}, {"X-Forwarded-For": "203.0.113.77"}),
            ("PUT", switch, {"enabled": True}, {"X-Forwarded-For": "not-an-address"}),
            ("PUT", switch, {"enabled": False}, {"X-Forwarded-For": "::1%x enabled=False"}),
        ],
    )
    # The bare lines, as Python's last-resort handler prints a WARNING with no logging configured; each call flips the
    # switch, because only a change is logged.
    assert [line for line in stderr if not line.startswith(UVICORN_PREFIXES)] == [
        "repair switch project=proj_001 enabled=True client=127.0.0.1",
        "repair switch project=proj_001 enabled=False client=203.0.113.77",
        "repair switch project=proj_001 enabled=True client=-",
        "repair switch project=proj_001 enabled=False client=-",
    ]

    # With the switch never on, a create, a read and a switch request that changes nothing: only uvicorn's lines, as at
    # ee0f227.
    stderr = _cairn_serve(
        tmp_path,
        "off",
        [
            ("POST", "/projects", project, {}),
            ("GET", "/projects/proj_001", None, {}),
            ("PUT", switch, {"enabled": False}, {}),
        ],
    )
    assert stderr, "uvicorn's own lines are missing"
    assert all(line.startswith(UVICORN_PREFIXES) for line in stderr), stderr
    assert not any("repair" in line for line in stderr), stderr


# SV45
@pytest.mark.parametrize("server", ["in_process", "cairn_serve"])
def test_logs_never_contain_tokens_session_ids_or_review_text(
    server: str, request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    if server == "in_process":
        _in_process(request.getfixturevalue("api"), request.getfixturevalue("clock"), monkeypatch, caplog)
    else:
        _under_cairn_serve(tmp_path)
