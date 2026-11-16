"""The agent against the real Cairn server (spec §6.3-§6.5, §9.4, §9.9): AG8, AG9, AG37, and AG13 against the router.

The server is this repository's app (`cairn.server.app`, through the `dev` group's path dependency on `../cairn`),
served by uvicorn on loopback in this process, on a temporary database, with both role tokens configured and its
clock frozen; its lifespan is not run, so it never opens the default database. The model is a loopback stub. Every
value is synthetic.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import uvicorn
from agentkit import (
    EXTEND,
    INTENT,
    MODEL_NAME,
    OMIT,
    SECOND_RUN_ID,
    Agent,
    FakeClock,
    ModelStub,
    Reply,
    chat_answer,
    configure,
    model_stubs,
    start_agent,
    write_run_copy,
)
from conftest import AGENT_TOKEN, Layout, fake_secret, reviewer_entry
from records import CLAUDE, RUN_ID, after, claude_assistant_tool_use, claude_tool_result, dump, iso, load

from cairn.server import db, repair, repair_auth, services
from cairn.server.app import app
from cairn.server.routers import hints as hints_router
from cairn.server.routers import intents as intents_router
from cairn.server.routers import projects as projects_router
from cairn_repair_agent import review

NOW = "2026-11-14T17:19:20Z"  # 1360 s after the runs' start: their windows are open (1350-1440 s)
STARTED_SECONDS_AGO = 1360
DISPATCHER_TOKEN = fake_secret("dispatcher")
TOKEN_HEADER = repair_auth.AUTH_HEADER
SETUP_HEADER = "X-Test-Setup"  # marks the test's own calls, so that the agent's can be told apart
THIRD_RUN_ID = "3c4d5e6f708192a3b4c5d6e7f8091a2b"
FOURTH_RUN_ID = "9e8d7c6b5a4938271605f4e3d2c1b0a9"
MODEL_100 = "m" * 99 + "z"


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class Recorder:
    """An ASGI wrapper that records every request; with `lose_next_review_answer`, the app records the next review
    post, and its answer is replaced by a 503 without a code, as a connection lost after the commit would."""

    app: Any
    seen: list[Seen] = field(default_factory=list)
    lose_next_review_answer: bool = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope["headers"]}
        self.seen.append(Seen(scope["method"], scope["path"], headers))
        if self.lose_next_review_answer and scope["method"] == "POST" and scope["path"].endswith("/reviews"):
            self.lose_next_review_answer = False

            async def discard(message: Any) -> None:
                return

            await self.app(scope, receive, discard)
            await send(
                {"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]}
            )
            await send({"type": "http.response.body", "body": b'{"detail": "lost"}'})
            return
        await self.app(scope, receive, send)

    def agent_calls(self) -> list[Seen]:
        return [seen for seen in self.seen if SETUP_HEADER.lower() not in seen.headers]


@dataclass
class RealServer:
    url: str
    recorder: Recorder
    session: requests.Session

    def call(self, method: str, path: str, *, role: str | None = None, **kwargs: Any) -> requests.Response:
        headers = {SETUP_HEADER: "1"}
        if role is not None:
            headers[TOKEN_HEADER] = AGENT_TOKEN if role == "agent" else DISPATCHER_TOKEN
        return self.session.request(method, self.url + path, headers=headers, timeout=30, **kwargs)

    def project(self, *, repair_on: bool, intents: int) -> str:
        response = self.call(
            "POST",
            "/projects",
            json={
                "title": "t",
                "origin": "start",
                "goal": "finish",
                "bootstrap_enabled": False,
                "repair_enabled": repair_on,
            },
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["project"]["id"]
        for number in range(1, intents + 1):
            created = self.call(
                "POST",
                f"/projects/{project_id}/intents",
                json={"from": ["origin"], "description": f"{INTENT} ({number})", "creator": "reasoner", "worker": None},
            )
            assert created.status_code == 201, created.text
            heartbeat = self.call(
                "POST", f"/projects/{project_id}/intents/i{number:03d}/heartbeat", json={"worker": f"w{number}"}
            )
            assert heartbeat.status_code == 200, heartbeat.text
        return project_id

    def register(self, run_id: str, project_id: str, number: int) -> requests.Response:
        body = {
            "project_id": project_id,
            "intent_id": f"i{number:03d}",
            "worker": f"w{number}",
            "cli_type": "claudecode",
            "execution": "container",
            "budget_seconds": 1800,
            "session_id": None,
            "started_seconds_ago": STARTED_SECONDS_AGO,
            "locator": {"container": "cairn-dispatch-test", "prompt_marker": None},
        }
        return self.call("PUT", f"/repair/runs/{run_id}", role="dispatcher", json=body)

    def run(self, run_id: str) -> dict[str, Any]:
        response = self.call("GET", f"/repair/runs/{run_id}", role="agent")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def cairn_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RealServer]:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    repair.reset_memory()
    repair_auth.reset()
    repair_auth.configure_tokens(AGENT_TOKEN, DISPATCHER_TOKEN)
    for module in (services, intents_router, projects_router, hints_router, repair):
        monkeypatch.setattr(module, "utcnow", lambda: NOW)
    recorder = Recorder(app)
    server = uvicorn.Server(
        uvicorn.Config(recorder, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off", ws="none")
    )
    thread = threading.Thread(target=server.run, name="cairn-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "the test server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    session = requests.Session()
    session.trust_env = False
    try:
        yield RealServer(f"http://127.0.0.1:{port}", recorder, session)
    finally:
        session.close()
        server.should_exit = True
        thread.join(timeout=10)
        repair.reset_memory()
        repair_auth.reset()


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


def agent_on(layout: Layout, server: RealServer, stub: ModelStub, **settings: Any) -> Agent:
    configure(layout, server.url, reviewers=[reviewer_entry(base_url=stub.url + "/v1", **settings)])
    return start_agent(layout, clock=FakeClock())


def cycle(agent: Agent) -> None:
    agent.loop.poll_once()
    assert agent.loop.wait_idle(60), "a review did not finish"


def by_run(replies: dict[str, Reply]) -> Callable[[bytes], Reply | None]:
    def route(body: bytes) -> Reply | None:
        return next((reply for run_id, reply in replies.items() if run_id.encode() in body), None)

    return route


def repair_issues(server: RealServer, project_id: str) -> dict[str, dict[str, Any]]:
    """The repair_review issues of the project's intents, by run id, as GET /projects/{id} shows them."""
    answer = server.call("GET", f"/projects/{project_id}").json()
    issues = [issue for intent in answer["intents"] for issue in intent.get("issues", [])]
    return {issue["run_id"]: issue for issue in issues if issue["kind"] == "repair_review"}


# --- AG37 ---------------------------------------------------------------------------------------------------------


def test_review_body_matches_the_server_contract(
    layout: Layout, cairn_server: RealServer, model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AG37: through the real router, the agent's reviews are recorded as shadow `repair_review` issues; a review
    whose answer was lost is posted again with the same `review_id` and gets the 200 replay; a review whose `model` is
    the reviewer's 100-character configured `model` gets 201, and so does one whose response `model` has 150
    characters, for which the configured one is posted; a fallback review (no copy) is accepted too."""
    caplog.set_level("INFO", logger="cairn_repair_agent")
    project = cairn_server.project(repair_on=True, intents=4)
    run_ids = [RUN_ID, SECOND_RUN_ID, THIRD_RUN_ID, FOURTH_RUN_ID]
    for number, run_id in enumerate(run_ids, start=1):
        response = cairn_server.register(run_id, project, number)
        assert response.status_code == 201, response.text
    for run_id in run_ids[:3]:  # the fourth run has no copy
        write_run_copy(layout.root, run_id, project)
    stub = model_stub(
        route=by_run(
            {
                RUN_ID: Reply(body=chat_answer(EXTEND, model="served-model")),
                SECOND_RUN_ID: Reply(body=chat_answer(EXTEND, model=OMIT)),
                THIRD_RUN_ID: Reply(body=chat_answer(EXTEND, model="m" * 150)),
            }
        )
    )
    agent = agent_on(layout, cairn_server, stub, model=MODEL_100)
    cairn_server.recorder.lose_next_review_answer = True
    cycle(agent)

    posts = [seen for seen in cairn_server.recorder.agent_calls() if seen.method == "POST"]
    assert sorted(seen.path for seen in posts) == sorted(
        [f"/repair/runs/{run_id}/reviews" for run_id in run_ids] + [posts[0].path]
    )  # every run once, and the one whose answer was lost twice
    posted = [line for line in _messages(caplog) if line.startswith("repair review posted")]
    statuses = sorted(line.split(" status=")[1].split(" ")[0] for line in posted)
    assert statuses == ["200", "201", "201", "201"]
    models = {run_id: cairn_server.run(run_id)["reviews"] for run_id in run_ids}
    assert {run_id: [r["model"] for r in reviews] for run_id, reviews in models.items()} == {
        RUN_ID: ["served-model"],
        SECOND_RUN_ID: [MODEL_100],
        THIRD_RUN_ID: [MODEL_100],
        FOURTH_RUN_ID: [None],
    }
    for run_id in run_ids[:3]:
        (stored,) = models[run_id]
        assert stored["decision"] == "extend" and stored["source"] == "agent" and stored["raw_output"] == EXTEND
        assert stored["digest"].startswith("REVIEW CONTEXT (from Cairn; trusted)\n")
    issues = repair_issues(cairn_server, project)
    assert set(issues) == set(run_ids)
    for run_id, issue in issues.items():
        detail = issue["detail"]
        assert (detail["mode"], detail["applied"]) == ("shadow", False)
        if run_id == FOURTH_RUN_ID:
            assert (detail["decision"], detail["fallback"], detail["reason"]) == (
                "continue",
                "transcript_unreadable",
                "transcript copy missing",
            )
        else:
            assert (detail["decision"], detail["fallback"]) == ("extend", None)
            assert detail["evidence"] == ["E1 the first listing worked"] and detail["next_step"] == "List /etc next"
    # The lost answer: both posts of that run carried one review_id, and only one review exists for it.
    lost_run = posts[0].path.split("/")[3]
    assert len(models[lost_run]) == 1


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("cairn_repair_agent")]


def _long_outputs_transcript() -> bytes:
    """claudecode-2.1.98.jsonl with twelve more shell calls after its last record, each printing 40 lines of about
    1,000 characters, in the record shapes of its lines 5 and 6."""
    records = load(CLAUDE)
    start = datetime(2026, 11, 14, 16, 57, 0, tzinfo=UTC)
    for number in range(12):
        call_id = f"toolu_long_{number:02d}"
        output = "\n".join(f"row {number:02d}-{row:02d} " + "listing entry value " * 50 for row in range(40))
        command = {"command": f"cat listing-{number:02d}.txt", "description": f"read listing {number}"}
        result = {"stdout": output, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
        records.append(claude_assistant_tool_use(call_id, "Bash", command, iso(after(start, 2 * number))))
        records.append(claude_tool_result(call_id, output, iso(after(start, 2 * number + 1)), tool_use_result=result))
    return dump(records)


def test_review_body_matches_the_server_contract_long_texts_are_cut(
    layout: Layout, cairn_server: RealServer, model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AG37 (§9.9): the posted digest is cut to 65,536 characters and the raw output to 16,384, the server's limits
    (§6.5), so a long text never makes a review a final 422. Through the real router, a review whose digest has more
    than 65,536 characters (a digest budget raised to 60,000 tokens, and a run with long tool outputs) and whose answer
    follows a think block of 30,000 characters gets 201, and the server keeps the first 65,536 characters of the digest
    the model saw and the first 16,384 of the answer as received."""
    caplog.set_level("INFO", logger="cairn_repair_agent")
    project = cairn_server.project(repair_on=True, intents=1)
    assert cairn_server.register(RUN_ID, project, 1).status_code == 201
    write_run_copy(layout.root, RUN_ID, project, data=_long_outputs_transcript())
    layout.write_config(digest={"max_tokens_estimate": 60000})
    answer = "<think>" + "t" * 30000 + "</think>" + EXTEND
    stub = model_stub(Reply(body=chat_answer(answer)))
    cycle(agent_on(layout, cairn_server, stub))
    (sent,) = stub.requests
    digest = sent.json()["messages"][1]["content"]
    assert len(digest) > 65536
    (stored,) = cairn_server.run(RUN_ID)["reviews"]
    assert (stored["decision"], stored["fallback"]) == ("extend", None)
    assert stored["digest"] == digest[:65536]
    assert stored["raw_output"] == answer[:16384]
    posted = [line for line in _messages(caplog) if line.startswith("repair review posted")]
    assert posted == [
        f"repair review posted run={RUN_ID} window=1 decision=extend fallback=- status=201 truncated=false"
    ]


# --- AG8 and AG9 ----------------------------------------------------------------------------------------------------


def test_agent_only_calls_its_endpoints(layout: Layout, cairn_server: RealServer, model_stub: Any) -> None:
    """AG8: through a whole review, the agent calls only `GET /repair/runs`, `GET /projects/{id}` for the facts and
    `POST /repair/runs/{run_id}/reviews`, sends the agent token to the repair endpoints only, and calls the model at
    the reviewer's base_url only."""
    project = cairn_server.project(repair_on=True, intents=1)
    assert cairn_server.register(RUN_ID, project, 1).status_code == 201
    write_run_copy(layout.root, RUN_ID, project)
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    agent = agent_on(layout, cairn_server, stub)
    for _ in range(2):
        cycle(agent)
    calls = [(seen.method, seen.path) for seen in cairn_server.recorder.agent_calls()]
    assert calls == [
        ("GET", "/repair/runs"),
        ("GET", f"/projects/{project}"),
        ("POST", f"/repair/runs/{RUN_ID}/reviews"),
        ("GET", "/repair/runs"),
    ]
    for seen in cairn_server.recorder.agent_calls():
        if seen.path.startswith("/repair/"):
            assert seen.headers[TOKEN_HEADER.lower()] == AGENT_TOKEN
        else:
            assert TOKEN_HEADER.lower() not in seen.headers
        assert "authorization" not in seen.headers and "x-api-key" not in seen.headers
    assert [(r.method, r.path) for r in stub.requests] == [("POST", "/v1/chat/completions")]
    assert cairn_server.run(RUN_ID)["window"]["review_status"] == "reviewed"


def test_agent_does_nothing_when_no_project_has_repair_on(
    layout: Layout, cairn_server: RealServer, model_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG9: with Repair off for every project (one never switched on, whose run the server refuses to register, and
    one switched off after its run was registered), the agent only polls: no copy read, no model call, no post."""
    reads: list[str] = []
    real = review.read_fresh_copy

    def recording(root: str, project_id: str, run_id: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(run_id)
        return real(root, project_id, run_id, *args, **kwargs)

    monkeypatch.setattr(review, "read_fresh_copy", recording)
    never_on = cairn_server.project(repair_on=False, intents=1)
    refused = cairn_server.register(RUN_ID, never_on, 1)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "repair_disabled"
    switched_off = cairn_server.project(repair_on=True, intents=1)
    assert cairn_server.register(SECOND_RUN_ID, switched_off, 1).status_code == 201
    write_run_copy(layout.root, SECOND_RUN_ID, switched_off)
    response = cairn_server.call("PUT", f"/projects/{switched_off}/repair", json={"enabled": False})
    assert response.status_code == 200, response.text
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    agent = agent_on(layout, cairn_server, stub)
    for _ in range(3):
        cycle(agent)
    assert [(seen.method, seen.path) for seen in cairn_server.recorder.agent_calls()] == [("GET", "/repair/runs")] * 3
    assert stub.requests == [] and reads == []


# --- AG13 against the router ----------------------------------------------------------------------------------------


def test_restart_reviews_an_open_window_once_against_the_server(
    layout: Layout, cairn_server: RealServer, model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AG13: a window reviewed before a restart is listed `reviewed` afterwards, so the restarted agent posts
    nothing; a window still open, whose review was lost with the agent, is reviewed after the restart with a new
    `review_id`, and the server's `409 already_reviewed` answers a second agent's review of a reviewed window."""
    project = cairn_server.project(repair_on=True, intents=2)
    assert cairn_server.register(RUN_ID, project, 1).status_code == 201
    write_run_copy(layout.root, RUN_ID, project)
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    first = agent_on(layout, cairn_server, stub)
    cycle(first)
    restarted = start_agent(layout, clock=FakeClock())
    cycle(restarted)
    posts = [seen for seen in cairn_server.recorder.agent_calls() if seen.method == "POST"]
    assert len(posts) == 1 and cairn_server.run(RUN_ID)["window"]["review_status"] == "reviewed"
    # Two agents that both found the second run open (the model answers only once both have polled): one review is
    # recorded, and the other gets the final 409, with no retry.
    caplog.set_level("INFO", logger="cairn_repair_agent")
    assert cairn_server.register(SECOND_RUN_ID, project, 2).status_code == 201
    write_run_copy(layout.root, SECOND_RUN_ID, project)
    gate = threading.Event()
    gated = model_stub(Reply(body=chat_answer(EXTEND), gate=gate))
    one, two = agent_on(layout, cairn_server, gated), start_agent(layout, clock=FakeClock())
    one.loop.poll_once()
    two.loop.poll_once()
    gate.set()
    assert one.loop.wait_idle(60) and two.loop.wait_idle(60)
    second_posts = [seen for seen in cairn_server.recorder.agent_calls() if seen.method == "POST"][1:]
    assert len(second_posts) == 2
    reviews = cairn_server.run(SECOND_RUN_ID)["reviews"]
    assert len(reviews) == 1 and reviews[0]["model"] == MODEL_NAME
    refused = [line for line in _messages(caplog) if line.startswith("repair review refused")]
    assert refused == [f"repair review refused run={SECOND_RUN_ID} window=1 code=already_reviewed"]
