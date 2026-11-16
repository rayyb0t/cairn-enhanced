"""The review loop and pipeline against stub endpoints (spec §9.4-§9.11): AG10-AG12, AG13-AG15, AG38-AG40, AG45,
AG49, AG56, and AG60's review at DEBUG. The real router's tests (AG8, AG9, AG37) are in test_review_server.py.

The agent is started as `cairn-repair` starts it (`cli.start`), with its loop on the real clock or on a fake one;
the Cairn server and the model are loopback stubs (`agentkit`). Every value is synthetic.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agentkit import (
    BUDGET,
    CHECKED_ELAPSED,
    ELAPSED_AT_POLL,
    EXTEND,
    INTENT,
    MODEL_NAME,
    OMIT,
    OTHER_PROJECT,
    PROJECT,
    SECOND_RUN_ID,
    SERVER_TIME_FORMAT,
    Agent,
    CairnStub,
    FakeClock,
    ModelStub,
    Reply,
    cairn_stubs,
    chat_answer,
    configure,
    decision_text,
    messages_answer,
    model_stubs,
    run_view,
    start_agent,
    text_block,
    wait_for,
    write_run_copy,
)
from conftest import (
    AGENT_TOKEN,
    DISPATCHER_UID,
    IS_ROOT,
    REVIEWER_KEY,
    SECOND_REVIEWER_KEY,
    Layout,
    TcpListener,
    reviewer_entry,
    set_mode,
)
from records import RUN_ID

from cairn_repair_agent import cli, envguard, loop, model, review
from cairn_repair_agent.config import ANTHROPIC_MESSAGES, OPENAI_CHAT

THIRD_RUN_ID = "3c4d5e6f708192a3b4c5d6e7f8091a2b"
FOURTH_RUN_ID = "9e8d7c6b5a4938271605f4e3d2c1b0a9"
SESSION_ID = "0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e44"
POSTED_KEYS = {
    "review_id",
    "window",
    "decision",
    "reason",
    "evidence",
    "next_step",
    "fallback",
    "expected_version",
    "digest_elapsed_seconds",
    "transcript_cut_seconds",
    "model",
    "digest",
    "raw_output",
}


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture
def cairn_stub() -> Iterator[CairnStub]:
    yield from cairn_stubs()


@pytest.fixture(autouse=True)
def _fresh_usage_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "_usage_warned", set())


def reviewer_on(stub: ModelStub, api_format: str = OPENAI_CHAT, **settings: Any) -> dict[str, Any]:
    entry = reviewer_entry(base_url=stub.url + "/v1" if api_format == OPENAI_CHAT else stub.url, **settings)
    entry["api_format"] = api_format
    if api_format == ANTHROPIC_MESSAGES and entry["api_key"] is None:
        entry["api_key"] = REVIEWER_KEY
    return entry


def ready(
    layout: Layout,
    cairn: CairnStub,
    stub: ModelStub,
    *extra: str,
    runs: list[dict[str, Any]] | None = None,
    copy: bool = True,
    projects: list[str] | None = None,
    clock: FakeClock | None = None,
    reviewers: list[dict[str, Any]] | None = None,
    **settings: Any,
) -> Agent:
    """An agent on these stubs, with the run listed open and its copy fresh (unless told otherwise)."""
    cairn.runs = [run_view()] if runs is None else runs
    configure(layout, cairn.url, reviewers=reviewers or [reviewer_on(stub, **settings)], projects=projects)
    if copy:
        write_run_copy(layout.root)
    return start_agent(layout, *extra, clock=clock)


def cycle(agent: Agent) -> None:
    """One poll, and every review it started, to its end."""
    agent.loop.poll_once()
    assert agent.loop.wait_idle(60), "a review did not finish"


def digests_for(stub: ModelStub, run_id: str) -> list[dict[str, Any]]:
    return [r.json() for r in stub.requests if run_id.encode() in r.body]


def record_copy_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The run ids whose copies the agent reads."""
    read: list[str] = []
    real = review.read_fresh_copy

    def recording(root: str, project_id: str, run_id: str, *args: Any, **kwargs: Any) -> Any:
        read.append(run_id)
        return real(root, project_id, run_id, *args, **kwargs)

    monkeypatch.setattr(review, "read_fresh_copy", recording)
    return read


def lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("cairn_repair_agent") and r.getMessage().startswith(prefix)
    ]


# --- AG10, AG11, AG14: which runs are reviewed ----------------------------------------------------------------------


@pytest.mark.parametrize("projects", [None, [PROJECT], []], ids=["null", "one_project", "empty"])
def test_agent_skips_projects_not_in_its_allowlist(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    projects: list[str] | None,
) -> None:
    """AG10: the settings file's `projects` narrows the switched-on projects. With null, the runs of every listed
    project are reviewed; with `["proj_001"]`, a run of `proj_002` gets no copy read, no model call, no post and one
    `repair review skipped … reason=project_not_listed` line however often it is listed; with `[]`, none is reviewed."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    reads = record_copy_reads(monkeypatch)
    runs = [run_view(RUN_ID, PROJECT), run_view(SECOND_RUN_ID, OTHER_PROJECT)]
    agent = ready(layout, cairn_stub, stub, runs=runs, projects=projects)
    write_run_copy(layout.root, SECOND_RUN_ID, OTHER_PROJECT)
    for _ in range(3):
        cycle(agent)
    reviewed = {RUN_ID, SECOND_RUN_ID} if projects is None else {RUN_ID} if projects else set()
    posted = {r.path.split("/")[3] for r in cairn_stub.calls("POST")}
    assert posted == reviewed
    assert set(reads) == reviewed and len(reads) == len(reviewed)
    assert {run_id for run_id in (RUN_ID, SECOND_RUN_ID) if digests_for(stub, run_id)} == reviewed
    skipped = lines(caplog, "repair review skipped")
    assert skipped == [
        f"repair review skipped run={run_id} window=1 reason=project_not_listed"
        for run_id in (RUN_ID, SECOND_RUN_ID)
        if run_id not in reviewed
    ]


def test_reviews_start_only_when_open_and_once_per_window(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG11: a `pending` window is waited for; an `open` one gets one review, once per run and window, however often
    it is listed; `late`, `reviewed`, `not_reviewed` and `closed` are never reviewed, even if listed open later."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    reads = record_copy_reads(monkeypatch)
    ids = {
        status: f"run-{status.replace('_', '-')}-0001"
        for status in ("pending", "late", "reviewed", "not_reviewed", "closed")
    }
    agent = ready(layout, cairn_stub, stub, runs=[run_view(run_id, status=status) for status, run_id in ids.items()])
    write_run_copy(layout.root, ids["pending"])
    cycle(agent)
    assert cairn_stub.calls("POST") == [] and stub.requests == [] and reads == []
    cairn_stub.runs = [run_view(run_id, status="open") for run_id in ids.values()]
    for _ in range(3):
        cycle(agent)
    assert [r.path for r in cairn_stub.calls("POST")] == [f"/repair/runs/{ids['pending']}/reviews"]
    assert reads == [ids["pending"]] and len(stub.requests) == 1


def test_reviews_start_only_when_open_and_once_per_window_while_in_flight(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG11: a window stays `open` while its review waits for the model, a wait of many poll intervals: the polls
    that list it again meanwhile start no second review. One copy read, one model request and one post for that run
    and window."""
    gate = threading.Event()
    stub = model_stub(Reply(body=chat_answer(EXTEND), gate=gate))
    reads = record_copy_reads(monkeypatch)
    agent = ready(layout, cairn_stub, stub)
    try:
        agent.loop.poll_once()
        assert wait_for(lambda: len(stub.requests) == 1, timeout=30)  # the review waits for its model answer
        for _ in range(3):
            agent.loop.poll_once()  # the window is still listed open: its review has not posted yet
        # A second review of the window would read the copy and call the model within this wait.
        assert not wait_for(lambda: len(reads) > 1 or len(stub.requests) > 1, timeout=2)
        assert agent.loop.in_flight() == 1 and cairn_stub.calls("POST") == []
    finally:
        gate.set()
    assert agent.loop.wait_idle(30)
    cycle(agent)  # listed open again after its review: done
    assert reads == [RUN_ID] and len(stub.requests) == 1
    assert [r.path for r in cairn_stub.calls("POST")] == [f"/repair/runs/{RUN_ID}/reviews"]


def test_unreviewable_runs_are_skipped(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG14: a run that has ended, or whose window is not reviewable, is never reviewed: no copy read, no model call,
    no post, whatever its review status says; a view without its window times is not reviewed either."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    reads = record_copy_reads(monkeypatch)
    ended = run_view("run-ended-00001", state="ended", status="open")
    unreviewable = run_view("run-unreviewable-01", reviewable=False, status="not_reviewable")
    unreviewable_open = run_view("run-unreviewable-02", reviewable=False, status="open")
    no_times = run_view("run-no-times-000001")
    del no_times["window"]["review_accept_until"]
    agent = ready(layout, cairn_stub, stub, runs=[ended, unreviewable, unreviewable_open, no_times])
    for _ in range(2):
        cycle(agent)
    assert cairn_stub.calls("POST") == [] and stub.requests == [] and reads == []
    assert len(cairn_stub.calls("GET", "/repair/runs")) == 2


def test_restart_reviews_an_open_window_once(layout: Layout, cairn_stub: CairnStub, model_stub: Any) -> None:
    """AG13: state is in memory only. After a restart, a window still listed open is reviewed again, with a new
    `review_id`; the server's `409 already_reviewed` is final: no retry, and no new review of that window."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    cairn_stub.posts = [(201, {"created": True}), (409, {"detail": {"code": "already_reviewed", "message": "x"}})]
    first = ready(layout, cairn_stub, stub)
    cycle(first)
    restarted = start_agent(layout)
    for _ in range(3):
        cycle(restarted)
    posted = cairn_stub.posted()
    assert len(posted) == 2
    assert posted[0]["review_id"] != posted[1]["review_id"]
    assert all(re.fullmatch(r"rv-[0-9a-f]{24}", body["review_id"]) for body in posted)


def test_concurrent_reviews_do_not_block_each_other(layout: Layout, cairn_stub: CairnStub, model_stub: Any) -> None:
    """AG15: one slow model call holds only its own review: another run's review is posted while it waits."""
    gate = threading.Event()

    def route(body: bytes) -> Reply | None:
        return Reply(body=chat_answer(EXTEND), gate=gate) if RUN_ID.encode() in body else None

    stub = model_stub(Reply(body=chat_answer(EXTEND)), route=route)
    agent = ready(layout, cairn_stub, stub, runs=[run_view(RUN_ID), run_view(SECOND_RUN_ID)])
    write_run_copy(layout.root, SECOND_RUN_ID)
    try:
        agent.loop.poll_once()
        # The other review is done (its post answered, so it is no longer in flight) while the slow one still waits
        # for its model answer; an agent whose reviews block each other never gets there.
        assert wait_for(
            lambda: (
                len(cairn_stub.calls("POST")) == 1 and bool(digests_for(stub, RUN_ID)) and agent.loop.in_flight() == 1
            ),
            timeout=30,
        )
        assert cairn_stub.calls("POST")[0].path == f"/repair/runs/{SECOND_RUN_ID}/reviews"
    finally:
        gate.set()
    assert agent.loop.wait_idle(30)
    assert [r.path.split("/")[3] for r in cairn_stub.calls("POST")] == [SECOND_RUN_ID, RUN_ID]


def test_dry_run_posts_nothing(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """§9.9: with `--dry-run` the agent builds the digest and calls the model, but posts nothing, once per window."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    agent = ready(layout, cairn_stub, stub, "--dry-run")
    for _ in range(2):
        cycle(agent)
    assert len(stub.requests) == 1 and cairn_stub.calls("POST") == []
    assert len(lines(caplog, "repair digest built")) == 1 and lines(caplog, "repair review posted") == []
    (started,) = lines(caplog, "repair agent started")
    assert started.endswith(" dry_run=true")


def test_once_waits_for_every_review_its_poll_started(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.3 step 7: with `--once` and no signal, the agent waits for every review its one poll started, however long
    the model takes; the 15 s limit runs only from a signal on (test_agent_main.py). With the limit lowered to 1 s and
    the model's answer held for twice that, `run` returns 0 only after the review is posted, not at the limit with the
    review still in flight."""
    limit = 1.0
    monkeypatch.setattr(loop, "SHUTDOWN_WAIT_SECONDS", limit)
    gate = threading.Event()
    stub = model_stub(Reply(body=chat_answer(EXTEND), gate=gate))
    agent = ready(layout, cairn_stub, stub)
    stop = threading.Event()  # no signal comes
    ended: list[tuple[int, int, int]] = []  # run's exit code, and the posts and reviews in flight when it returned

    def run_once() -> None:
        code = agent.loop.run(once=True, stop=stop)
        ended.append((code, len(cairn_stub.calls("POST")), agent.loop.in_flight()))

    runner = threading.Thread(target=run_once, name="run-once", daemon=True)
    runner.start()
    try:
        assert wait_for(lambda: len(stub.requests) == 1, timeout=30)  # the review waits for its model answer
        runner.join(timeout=2 * limit)
        assert runner.is_alive() and cairn_stub.calls("POST") == []  # past the limit, still waiting for the review
    finally:
        gate.set()
        runner.join(timeout=30)
        stop.set()  # a run that failed to return ends now, rather than outlive the test
    assert ended == [(0, 1, 0)]
    assert len(cairn_stub.calls("GET", "/repair/runs")) == 1
    assert [body["decision"] for body in cairn_stub.posted()] == ["extend"]


# --- AG12: the deadlines --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("skew", [-3600, 0, 3600])
def test_deadlines_come_from_server_time_not_wall_clock(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, monkeypatch: pytest.MonkeyPatch, skew: int
) -> None:
    """AG12: both deadlines are mapped from the server's clock to the agent's monotonic clock: with the server's
    clock an hour ahead of the agent's wall clock, level with it, or an hour behind (and the wall clock itself moved
    the other way), the model deadline is 10 s and the post deadline 1 s before `review_accept_until`, counted from
    the poll's answer on the monotonic clock: the model call gets 420 s, and posting retries stop at 429 s."""
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - skew)
    server_now = datetime.fromtimestamp(real_time(), UTC).replace(microsecond=0) + timedelta(seconds=skew)
    start = server_now - timedelta(seconds=ELAPSED_AT_POLL)
    clock = FakeClock()
    stub = model_stub(Reply(body=chat_answer(decision_text())))
    cairn_stub.server_time = server_now.strftime(SERVER_TIME_FORMAT)
    cairn_stub.posts = [(503, {"detail": "busy"})]
    agent = ready(layout, cairn_stub, stub, runs=[run_view(start=start)], clock=clock)
    deadlines: list[float] = []
    reviewer = agent.loop.environment.reviewer
    real_call = reviewer.call

    def recording_call(digest_text: str, *, run: str, deadline: float) -> Any:
        deadlines.append(deadline)
        return real_call(digest_text, run=run, deadline=deadline)

    monkeypatch.setattr(reviewer, "call", recording_call)
    began = clock.monotonic()
    cycle(agent)
    assert deadlines == [began + (BUDGET - 10 - ELAPSED_AT_POLL) - 10]
    assert len(stub.requests) == 1
    # Posting: retried after 2, 4, 8 ... s while the retry starts before accept_until - 1 s (429 s after the poll).
    assert clock.sleeps == [2, 4, 8, 16, 32, 64, 128]
    assert len(cairn_stub.calls("POST")) == 8
    view = run_view(start=start)
    window = review.window_of(view)
    assert window is not None
    poll = review.Poll(server_now, began, 600)
    assert review.Deadlines.of(window, poll) == review.Deadlines(
        model=began + 420, post=began + 429, copy_until=began + 80
    )


# --- AG38: every failure's fallback ----------------------------------------------------------------------------------


def _stale(layout: Layout) -> None:
    write_run_copy(layout.root, checked=1200.0)  # the floor is 1350 - 45 = 1305 s


def _bad_slices(cairn: CairnStub) -> None:
    cairn.runs[0]["slices_used"] = -1  # the run view breaks §6.3: the digest's trusted header refuses it


UNCITED = decision_text("extend", "r", ("E99 made up",), "x")
FALLBACK_CASES: dict[str, tuple[dict[str, Any], tuple[str, str | None, str, str | None]]] = {
    "transcript_missing": ({"copy": False}, ("continue", "transcript_unreadable", "transcript copy missing", None)),
    "transcript_stale": (
        {"copy": False, "setup": _stale},
        ("continue", "transcript_unreadable", "transcript copy stale: checked 150 s before the review opened", None),
    ),
    "digest_error": ({"cairn": _bad_slices}, ("continue", "digest_error", "ValueError", None)),
    "model_timeout": (
        {"reply": Reply(mode="silent"), "timeout_seconds": 1, "max_attempts": 1},
        ("continue", "model_timeout", "timeout", None),
    ),
    "model_error_status": ({"reply": Reply(400)}, ("continue", "model_error", "status_400", None)),
    "model_error_refusal": (
        {"reply": Reply(body=chat_answer(None, refusal="I cannot help with that."))},
        ("continue", "model_error", "refusal", None),
    ),
    "invalid_output_not_json": (
        {"reply": Reply(body=chat_answer("not json"))},
        ("continue", "invalid_output", "not JSON: Expecting value", "not json"),
    ),
    "invalid_output_max_tokens": (
        {"reply": Reply(body=chat_answer(EXTEND, finish_reason="length"))},
        ("continue", "invalid_output", "max_tokens", EXTEND),
    ),
    "invalid_output_citation": (
        {"reply": Reply(body=chat_answer(UNCITED))},
        ("continue", "invalid_output", "extend without a valid citation", UNCITED),
    ),
    "valid": ({"reply": Reply(body=chat_answer(EXTEND))}, ("extend", None, "The listing is advancing.", EXTEND)),
}


@pytest.mark.parametrize("case", list(FALLBACK_CASES))
def test_each_failure_posts_the_right_fallback(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, case: str
) -> None:
    """AG38: each failure posts `continue` with its fallback and the cause as the reason, no evidence and an empty
    next step; the digest, raw output, model and cut are posted when they exist (§9.9, §9.10)."""
    options, (decision, fallback, reason, raw_output) = FALLBACK_CASES[case]
    stub = model_stub(options.get("reply", Reply(body=chat_answer(EXTEND))))
    settings = {key: options[key] for key in ("timeout_seconds", "max_attempts") if key in options}
    clock = FakeClock()
    cairn_stub.runs = [run_view()]
    if "cairn" in options:
        options["cairn"](cairn_stub)
    agent = ready(
        layout, cairn_stub, stub, runs=cairn_stub.runs, copy=options.get("copy", True), clock=clock, **settings
    )
    if "setup" in options:
        options["setup"](layout)
    cycle(agent)
    (posted,) = cairn_stub.posted()
    assert set(posted) == POSTED_KEYS
    assert (posted["decision"], posted["fallback"], posted["reason"]) == (decision, fallback, reason)
    assert posted["raw_output"] == raw_output
    assert posted["window"] == 1 and posted["expected_version"] == 1
    assert posted["digest_elapsed_seconds"] == ELAPSED_AT_POLL
    if fallback is not None:
        assert (posted["evidence"], posted["next_step"]) == ([], "")
    else:
        assert posted["evidence"] == ["E1 the first listing worked"] and posted["next_step"] == "List /etc next"
    copy_read = fallback != "transcript_unreadable"
    assert posted["transcript_cut_seconds"] == (CHECKED_ELAPSED if copy_read else None)
    digest_built = copy_read and fallback != "digest_error"
    assert (posted["digest"] is not None) is digest_built
    if digest_built:
        assert posted["digest"].startswith("REVIEW CONTEXT (from Cairn; trusted)\n")
        assert posted["digest"] == stub.requests[0].json()["messages"][1]["content"]  # the digest the model saw
        assert posted["model"] == MODEL_NAME
    else:
        assert posted["model"] is None and stub.requests == []
    if case == "transcript_stale":
        assert clock.sleeps and all(seconds <= 5 for seconds in clock.sleeps)  # read again every 5 s, until it closes


# --- AG39: posting --------------------------------------------------------------------------------------------------


def test_409_is_final_and_5xx_is_retried_until_the_post_deadline(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AG39: any 409 is final: one post, `repair review refused … code=<code>`, and the window is not reviewed again."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    cairn_stub.posts = [(409, {"detail": {"code": "window_closed", "message": "the review window has closed"}})]
    clock = FakeClock()
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=clock)
    for _ in range(3):
        cycle(agent)
    assert len(cairn_stub.calls("POST")) == 1 and clock.sleeps == []
    assert lines(caplog, "repair review refused") == [f"repair review refused run={RUN_ID} window=1 code=window_closed"]
    assert lines(caplog, "repair review posted") == []


@pytest.mark.parametrize("status", [0, 408, 429, 500, 502, 503, 504])
def test_409_is_final_and_5xx_is_retried_until_the_post_deadline_retries(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    """AG39: no answer (0), 408, 429 and 5xx are retried after 2, 4, 8 s … with the same `review_id`, while the retry
    starts before the post deadline (`accept_until` - 1 s, here 429 s after the poll), then given up with `repair
    review failed`; an answer of 201 after them is done."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    cairn_stub.posts = [(status, {"detail": "busy"})]
    clock = FakeClock()
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=clock)
    cycle(agent)
    posted = cairn_stub.posted()
    assert len(posted) == 8 and len({body["review_id"] for body in posted}) == 1
    assert clock.sleeps == [2, 4, 8, 16, 32, 64, 128]
    assert lines(caplog, "repair review failed") == [f"repair review failed run={RUN_ID} window=1 status={status}"]
    # The same failure, then a 201: posted, with the same review id.
    cairn_stub.requests.clear()
    cairn_stub.posts = [(status, {"detail": "busy"}), (201, {"created": True})]
    restarted = start_agent(layout, clock=FakeClock())
    cycle(restarted)
    posted = cairn_stub.posted()
    assert len(posted) == 2 and posted[0]["review_id"] == posted[1]["review_id"]
    assert lines(caplog, "repair review posted")[-1] == (
        f"repair review posted run={RUN_ID} window=1 decision=extend fallback=- status=201 truncated=false"
    )


@pytest.mark.parametrize(("after", "posted"), [(-0.5, True), (0.0, False), (0.5, False), (60.0, False)])
def test_409_is_final_and_5xx_is_retried_until_the_post_deadline_late_review(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    after: float,
    posted: bool,
) -> None:
    """AG39 (§9.7): a review that ends at or after the post deadline (`accept_until` - 1 s, here 429 s after the poll),
    as one whose model endpoint sent its headers a byte at a time can, is lost: nothing is posted, the agent logs
    `repair review failed … status=post_deadline` and does not review the window again, and the server records it as
    not reviewed. A review that ends just before the post deadline is posted."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    clock = FakeClock()
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=clock)
    post_deadline = clock.monotonic() + (BUDGET - 10 - ELAPSED_AT_POLL) - 1
    reviewer = agent.loop.environment.reviewer
    real_call = reviewer.call

    def held_call(digest_text: str, *, run: str, deadline: float) -> Any:
        result = real_call(digest_text, run=run, deadline=deadline)
        clock.advance(post_deadline + after - clock.monotonic())  # the model's answer ends then
        return result

    monkeypatch.setattr(reviewer, "call", held_call)
    for _ in range(2):
        cycle(agent)
    assert len(cairn_stub.calls("POST")) == (1 if posted else 0)
    if posted:
        assert lines(caplog, "repair review posted") == [
            f"repair review posted run={RUN_ID} window=1 decision=extend fallback=- status=201 truncated=false"
        ]
        assert lines(caplog, "repair review failed") == []
    else:
        assert lines(caplog, "repair review failed") == [
            f"repair review failed run={RUN_ID} window=1 status=post_deadline"
        ]
        assert lines(caplog, "repair review posted") == []


# --- AG49: token errors, other 503s, redirects and 422s ----------------------------------------------------------------


def _open_runs(*run_ids: str) -> list[dict[str, Any]]:
    return [run_view(run_id) for run_id in run_ids]


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"detail": {"code": "unauthorized", "message": "x"}}), (503, {"detail": {"code": "token_not_configured"}})],
)
def test_token_errors_pause_reviews_and_other_503s_are_retried(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, status: int, body: Any
) -> None:
    """AG49: a 401, or a 503 with code `token_not_configured`, logs one ERROR, drops the review in hand (no retry),
    and starts no new review for 60 s on the agent's clock; the ERROR is logged once until a post succeeds again."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    clock = FakeClock()
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    cairn_stub.posts = [(status, body)]
    agent = ready(layout, cairn_stub, stub, runs=_open_runs(RUN_ID), clock=clock)
    for run_id in (SECOND_RUN_ID, THIRD_RUN_ID, FOURTH_RUN_ID):
        write_run_copy(layout.root, run_id)
    cycle(agent)
    assert len(cairn_stub.calls("POST")) == 1 and clock.sleeps == []
    assert lines(caplog, "repair agent token rejected") == [f"repair agent token rejected status={status}"]
    cairn_stub.runs = _open_runs(RUN_ID, SECOND_RUN_ID)  # a new open window, during the pause
    clock.advance(59)
    cycle(agent)
    assert len(cairn_stub.calls("POST")) == 1 and not digests_for(stub, SECOND_RUN_ID)
    clock.advance(2)  # the pause is over: the second run is reviewed, and its post is rejected too
    cycle(agent)
    assert [r.path.split("/")[3] for r in cairn_stub.calls("POST")] == [RUN_ID, SECOND_RUN_ID]
    assert len(lines(caplog, "repair agent token rejected")) == 1  # once until a post succeeds again
    cairn_stub.posts = [(201, {"created": True})]
    cairn_stub.runs = _open_runs(THIRD_RUN_ID)
    clock.advance(61)
    cycle(agent)
    assert len(lines(caplog, "repair review posted")) == 1
    cairn_stub.posts = [(status, body)]
    cairn_stub.runs = _open_runs(FOURTH_RUN_ID)
    cycle(agent)
    assert lines(caplog, "repair agent token rejected") == [f"repair agent token rejected status={status}"] * 2


@pytest.mark.parametrize(
    "answer",
    [
        (503, {"detail": {"code": "other_code", "message": "x"}}),
        (503, b"Service Unavailable"),
        (503, b"[" * 200_000 + b"]" * 200_000),
    ],
    ids=["another_code", "not_json", "nested_200000_deep"],
)
def test_token_errors_pause_reviews_and_other_503s_are_retried_like_5xx(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, answer: tuple[int, Any]
) -> None:
    """AG49: a 503 with another code, a body that is not JSON, or one nested 200,000 deep has no
    `token_not_configured` code: it is retried like any 5xx until the post deadline, with no token ERROR."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    cairn_stub.posts = [answer]
    clock = FakeClock()
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=clock)
    cycle(agent)
    assert len(cairn_stub.calls("POST")) == 8 and clock.sleeps == [2, 4, 8, 16, 32, 64, 128]
    assert lines(caplog, "repair agent token rejected") == []
    assert lines(caplog, "repair review failed") == [f"repair review failed run={RUN_ID} window=1 status=503"]


def test_token_errors_pause_reviews_and_other_503s_are_retried_redirect(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Any,
    tcp_listener: Callable[..., TcpListener],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AG49: a 307 to another host is not followed: nothing reaches that host, and the review fails at once."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    target = tcp_listener("127.0.0.2")
    cairn_stub.posts = [(307, {"detail": "moved"}, {"Location": target.url + f"/repair/runs/{RUN_ID}/reviews"})]
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=FakeClock())
    cycle(agent)
    assert len(cairn_stub.calls("POST")) == 1 and target.connections == 0
    assert lines(caplog, "repair review failed") == [f"repair review failed run={RUN_ID} window=1 status=307"]


INVALID_422 = {
    "fastapi_list": (
        {
            "detail": [
                {"loc": ["body", "reason"], "msg": "x", "type": "y"},
                {"loc": ["body", "evidence", 3], "msg": "x"},
            ]
        },
        "fields=reason,3",
    ),
    "coded": ({"detail": {"code": "bad_run_id", "message": "x"}}, "fields=bad_run_id"),
    "a_name_with_a_space": ({"detail": [{"loc": ["body", "a b"], "msg": "x"}]}, "fields=other"),
    "a_name_with_a_newline": ({"detail": [{"loc": ["body", "next_step\n"], "msg": "x"}]}, "fields=other"),
    "a_name_of_65_characters": ({"detail": [{"loc": ["body", "f" * 65], "msg": "x"}]}, "fields=other"),
    "a_negative_index": ({"detail": [{"loc": ["body", "evidence", -7], "msg": "x"}]}, "fields=other"),
    "an_index_of_4300_digits": (
        {"detail": [{"loc": ["body", "evidence", int("9" * 4300)], "msg": "x"}]},
        "fields=other",
    ),
    "no_detail": ({"error": "x"}, "fields=-"),
}


@pytest.mark.parametrize("case", list(INVALID_422))
def test_token_errors_pause_reviews_and_other_503s_are_retried_422_fields(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    """AG49: a 422 is final and logs its field names (the last element of each FastAPI `loc`, or a coded 422's code);
    a name holding a space or a newline, or of 65 characters, is logged as `other`, and so is a list index whose digits
    the same rule refuses (§9.11): a negative one, or one of 4,300 digits, which `json.loads` accepts."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    body, fields = INVALID_422[case]
    cairn_stub.posts = [(422, body)]
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=FakeClock())
    cycle(agent)
    assert len(cairn_stub.calls("POST")) == 1
    assert lines(caplog, "repair review rejected") == [f"repair review rejected as invalid run={RUN_ID} {fields}"]


@pytest.mark.parametrize(
    ("code", "logged"),
    [
        ("already_reviewed", "already_reviewed"),
        ("a b", "other"),
        ("digest_too_late\n", "other"),
        ("c" * 65, "other"),
        (None, "other"),
    ],
)
def test_token_errors_pause_reviews_and_other_503s_are_retried_409_codes(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, code: Any, logged: str
) -> None:
    """AG49: a 409's code is logged when it is a plain name of at most 64 characters, else as `other`; a 404
    `run_not_found` is final and refused too, and a 404 without that code is a failure."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    cairn_stub.posts = [(409, {"detail": {"code": code, "message": "x"}})]
    agent = ready(layout, cairn_stub, model_stub(Reply(body=chat_answer(EXTEND))), clock=FakeClock())
    cycle(agent)
    assert lines(caplog, "repair review refused") == [f"repair review refused run={RUN_ID} window=1 code={logged}"]
    caplog.clear()
    cairn_stub.requests.clear()
    cairn_stub.posts = [(404, {"detail": {"code": "run_not_found", "message": "x"}})]
    cycle(start_agent(layout, clock=FakeClock()))
    assert lines(caplog, "repair review refused") == [f"repair review refused run={RUN_ID} window=1 code=run_not_found"]
    cairn_stub.posts = [(404, {"detail": "Not Found"})]
    cycle(start_agent(layout, clock=FakeClock()))
    assert lines(caplog, "repair review failed") == [f"repair review failed run={RUN_ID} window=1 status=404"]


# --- AG45: the environment's proxies ----------------------------------------------------------------------------------


@pytest.mark.parametrize("client", ["model", "server"])
def test_agent_clients_ignore_proxy_environment(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Any,
    tcp_listener: Callable[..., TcpListener],
    monkeypatch: pytest.MonkeyPatch,
    client: str,
) -> None:
    """AG45: with HTTP_PROXY, HTTPS_PROXY and ALL_PROXY, upper and lower case, naming a capturing proxy, and the
    clients built while they are still set (the guard bypassed), no request reaches that proxy through a whole
    review: the model call goes to the reviewer's http://127.0.0.1 base_url, and the server client's GET /repair/runs
    and review POST to the server. With the reviewer's own `proxy` set, the model client uses that proxy only, and the
    server client still uses none."""
    proxy = tcp_listener()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy.url)
        monkeypatch.setenv(name.lower(), proxy.url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(envguard, "strip_environment", lambda environ=None: [])
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    agent = ready(layout, cairn_stub, stub)
    assert os.environ["https_proxy"] == proxy.url  # still set while the clients were built
    cycle(agent)
    assert proxy.connections == 0
    assert [(r.method, r.path.split("?")[0]) for r in cairn_stub.requests if not r.path.startswith("/projects/")] == [
        ("GET", "/repair/runs"),
        ("POST", f"/repair/runs/{RUN_ID}/reviews"),
    ]
    assert len(stub.requests) == 1
    if client == "model":
        # The reviewer's own proxy: a stub that answers as the model would, through which the request goes.
        reviewer_proxy = model_stub(Reply(body=chat_answer(decision_text())))
        agent = ready(layout, cairn_stub, stub, runs=[run_view(SECOND_RUN_ID)], copy=False, proxy=reviewer_proxy.url)
        write_run_copy(layout.root, SECOND_RUN_ID)
        cycle(agent)
        assert [r.path for r in reviewer_proxy.requests] == [stub.url + "/v1/chat/completions"]
        assert len(stub.requests) == 1 and proxy.connections == 0
        assert cairn_stub.posted()[-1]["reason"] == "The run repeats one failing command."
    else:
        posts = len(cairn_stub.calls("POST"))
        agent = ready(layout, cairn_stub, stub, runs=[run_view(SECOND_RUN_ID)], copy=False, proxy=proxy.url)
        write_run_copy(layout.root, SECOND_RUN_ID)
        cycle(agent)
        assert len(cairn_stub.calls("POST")) == posts + 1  # straight to the server, not through any proxy
        assert proxy.connections == 2  # only the model's two attempts went to the reviewer's proxy


# --- AG56 and AG60: the first reviewer and its format -------------------------------------------------------------


@pytest.mark.parametrize("first_key", [REVIEWER_KEY, None])
def test_live_agent_uses_only_the_first_reviewer(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, first_key: str | None
) -> None:
    """AG56: with two reviewers on stub servers, every model call of a live review goes to the first's base_url, with
    `Authorization: Bearer <its key>` (none when its key is null) and its `model`; the second gets no request; the
    startup line names the first reviewer, its model and its endpoint."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    first, second = model_stub([Reply(500), Reply(body=chat_answer(EXTEND))]), model_stub()
    reviewers = [
        reviewer_on(first, name="reviewer_first", model="first-model", api_key=first_key),
        reviewer_on(second, name="reviewer_second", model="second-model", api_key=SECOND_REVIEWER_KEY),
    ]
    agent = ready(layout, cairn_stub, first, reviewers=reviewers, clock=FakeClock())
    cycle(agent)
    assert second.requests == []
    assert [(r.method, r.path) for r in first.requests] == [("POST", "/v1/chat/completions")] * 2
    for sent in first.requests:
        assert sent.json()["model"] == "first-model"
        if first_key is None:
            assert "Authorization" not in sent.headers
        else:
            assert sent.headers["Authorization"] == f"Bearer {first_key}"
        assert SECOND_REVIEWER_KEY not in json.dumps(sent.headers)
    (started,) = lines(caplog, "repair agent started")
    assert f" reviewer=reviewer_first model=first-model endpoint={model.endpoint_of(first.url)} " in started


@pytest.mark.parametrize("api_format", [ANTHROPIC_MESSAGES, OPENAI_CHAT])
def test_anthropic_messages_request_shape_across_a_review(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, caplog: pytest.LogCaptureFixture, api_format: str
) -> None:
    """AG60: a review by an `anthropic_messages` reviewer, at --log-level DEBUG, sends its key as `x-api-key` with
    `anthropic-version` and no `Authorization`, and no log record holds the key; the same review with `openai_chat`
    sends `Authorization: Bearer <key>` and neither `x-api-key` nor `anthropic-version`."""
    caplog.set_level(logging.DEBUG)
    answer = messages_answer([text_block(EXTEND)]) if api_format == ANTHROPIC_MESSAGES else chat_answer(EXTEND)
    stub = model_stub(Reply(body=answer))
    agent = ready(layout, cairn_stub, stub, "--log-level", "DEBUG", api_format=api_format, api_key=REVIEWER_KEY)
    cycle(agent)
    (sent,) = stub.requests
    headers = {name.lower(): value for name, value in sent.headers.items()}
    if api_format == ANTHROPIC_MESSAGES:
        assert sent.path == "/v1/messages"
        assert (headers["x-api-key"], headers["anthropic-version"]) == (REVIEWER_KEY, "2023-06-01")
        assert "authorization" not in headers
    else:
        assert sent.path == "/v1/chat/completions"
        assert headers["authorization"] == f"Bearer {REVIEWER_KEY}"
        assert "x-api-key" not in headers and "anthropic-version" not in headers
    assert cairn_stub.posted()[0]["decision"] == "extend"
    for record in caplog.records:
        assert REVIEWER_KEY not in record.getMessage()
        assert all(REVIEWER_KEY not in str(arg) for arg in (record.args or ()))


# --- AG40: log hygiene --------------------------------------------------------------------------------------------------

LOG_FORMATS = [
    "repair agent started server=%s reviewer=%s model=%s endpoint=%s digest_tokens=%s poll=%ss projects=%s "
    "transcripts=%s dry_run=%s",
    "repair agent isolation check failed reasons=%s",
    "repair agent running without isolation reasons=%s",
    "repair reviewer pinned reviewer=%s endpoint=%s proxy=%s ca_bundle=%s",
    "repair reviewer changed since it was pinned fields=%s pin=%s",
    "repair reviewer pin unusable path=%s reason=%s",
    "repair reviewer pin unusable path=%s reason=folder_missing; as root, create the folder with: install -d -m 0700 "
    "-o %s -g %s %s",
    "repair reviewer pin: if you changed the reviewer yourself, remove the pin file as root and start the agent again",
    "repair reviewer pin: the agent cannot use its state folder; check state.dir in repair.yaml, and as root make it "
    "the agent's own folder with: install -d -m 0700 -o %s -g %s %s",
    "repair reviewer pin: the agent's state folder is a symlink, which it does not follow; check state.dir in "
    "repair.yaml, and as root replace the link with a folder of the agent's own: rm %s && install -d -m 0700 -o %s "
    "-g %s %s",
    "repair agent ignored environment variables names=%s",
    "repair agent token file readable by others file=%s mode=%s",
    "repair reviewer settings are writable by the dispatcher's user, local workers can change them path=%s",
    "repair agent settings file readable by others path=%s mode=%s",
    "repair model endpoint is plain http on a non-loopback host endpoint=%s",
    # Build step RA2: the WARNING for local workers' temporary output, and the class of a poll without an answer.
    "repair agent can read local workers' temporary output paths=%s; give them private temp folders "
    "(runbook, host install, step 4 e)",
    "repair agent poll failed status=%s retry_in=%ss",
    "repair agent poll failed status=0 error=%s retry_in=%ss",
    "repair review started run=%s project=%s intent=%s window=%s elapsed=%ss remaining=%ss cli=%s",
    "repair review skipped run=%s window=%s reason=project_not_listed",
    "repair transcript unavailable run=%s reason=%s",
    "repair digest built run=%s tokens_est=%s tool_calls=%s running=%s redactions=%s shrunk=%s build_ms=%s",
    "repair model call run=%s reviewer=%s model=%s attempt=%s outcome=%s stop=%s input_tokens=%s prompt_est=%s "
    "output_tokens=%s ms=%s request_id=%s",
    "repair model usage missing, a cut prompt cannot be detected endpoint=%s",
    "repair model refused a request with the default max_tokens, set max_tokens if the model allows fewer "
    "reviewer=%s max_tokens=%s status=%s",
    "repair review posted run=%s window=%s decision=%s fallback=%s status=%s truncated=%s",
    "repair review refused run=%s window=%s code=%s",
    "repair agent token rejected status=%s",
    "repair review rejected as invalid run=%s fields=%s",
    "repair review failed run=%s window=%s status=%s",
]
FORMAT_RULES = [re.compile(re.escape(fmt).replace("%s", r"\S+")) for fmt in LOG_FORMATS]
REASON_TEXT = "The listing is advancing and needs one more slice to finish its last directory."
EVIDENCE_TEXT = "E1 the first listing worked and printed hello-from-tool"
NEXT_STEP_TEXT = "List /etc/config once the listing finishes"


def _check_records(records: list[logging.LogRecord], secrets: list[str]) -> None:
    agent = [record for record in records if record.name.startswith("cairn_repair_agent")]
    assert agent, "no agent record was captured"
    for record in agent:
        text = record.getMessage()
        assert text.startswith("repair "), text
        assert any(rule.fullmatch(text) for rule in FORMAT_RULES), text
        for secret in secrets:
            assert secret not in text, (secret, text)


LOGGER_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _string(node: ast.expr, constants: dict[str, str]) -> str | None:
    """A string literal, a module-level string constant, or a sum of these; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string(node.left, constants), _string(node.right, constants)
        return None if left is None or right is None else left + right
    return None


def _logged_formats() -> list[tuple[str, int, str | None]]:
    """Every call of a module's `logger` in the agent's package, read from its source: (module, line, format), the
    format None when only running the code could tell it."""
    found: list[tuple[str, int, str | None]] = []
    for path in sorted(Path(cli.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict[str, str] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target, value = statement.targets[0], _string(statement.value, constants)
                if isinstance(target, ast.Name) and value is not None:
                    constants[target.id] = value
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
                and node.func.attr in LOGGER_METHODS
            ):
                arguments = node.args[1:] if node.func.attr == "log" else node.args
                found.append((path.name, node.lineno, _string(arguments[0], constants) if arguments else None))
    return found


def test_the_log_catalogue_holds_every_format_the_agent_logs() -> None:
    """AG40: LOG_FORMATS is §9.11's complete list, the formats of every logger call in the agent's code, each once,
    and none that the code no longer logs. It is read from the source, so a line that no scenario here produces (a
    refusal's second line, say) is held to the catalogue too."""
    found = _logged_formats()
    assert len(found) > len(LOG_FORMATS) // 2, found  # the scan sees the agent's calls
    assert [(module, line) for module, line, text in found if text is None] == []  # each format is a literal
    logged = {text for _, _, text in found if text is not None}
    assert len(set(LOG_FORMATS)) == len(LOG_FORMATS)
    assert sorted(logged - set(LOG_FORMATS)) == []  # logged, but not in the catalogue
    assert sorted(set(LOG_FORMATS) - logged) == []  # in the catalogue, but never logged


def test_logs_never_contain_tokens_keys_ids_or_run_text(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Any,
    tcp_listener: Callable[..., TcpListener],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AG40: every record under `cairn_repair_agent.*` uses one of §9.11's formats and starts with `repair `, across a
    startup with every startup WARNING, a full review, each fallback and each answer of §9.9's table; no record holds
    the agent token, a reviewer's key, the session id, digest or raw output text, or any reason, evidence or next-step
    text. A cut reason gives `truncated=true` on `repair review posted`."""
    caplog.set_level(logging.DEBUG)
    long_reason = REASON_TEXT + " " + "r" * 2100
    answer = decision_text("extend", long_reason, (EVIDENCE_TEXT,), NEXT_STEP_TEXT)
    stub = model_stub(Reply(body=chat_answer(answer, usage=OMIT), headers={"x-request-id": "req-1"}))
    # A startup with every WARNING: no isolation, guarded variables, a token file and a settings file readable by
    # others, a settings file the dispatcher's user owns, and a first reviewer on plain http to a non-loopback host.
    set_mode(layout.token_path, 0o640)
    monkeypatch.setenv("HTTPS_PROXY", tcp_listener().url)
    cairn_stub.runs = [run_view()]
    for run in cairn_stub.runs:
        run["session_id"] = SESSION_ID
    configure(layout, cairn_stub.url, reviewers=[reviewer_entry(base_url="http://10.0.0.5:11434/v1")])
    set_mode(layout.settings_path, 0o644)
    if IS_ROOT:
        os.chown(layout.settings_path, DISPATCHER_UID, os.getgid())
    start_agent(layout)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for prefix in (
        "repair agent running without isolation",
        "repair agent ignored environment variables",
        "repair agent token file readable by others",
        "repair agent settings file readable by others",
        "repair model endpoint is plain http",
    ) + (("repair reviewer settings are writable by the dispatcher's user",) if IS_ROOT else ()):
        assert any(message.startswith(prefix) for message in warnings), prefix
    # A full review whose reason is cut, then each fallback and each answer of §9.9.
    agent = ready(layout, cairn_stub, stub, runs=cairn_stub.runs, api_key=REVIEWER_KEY)
    cycle(agent)
    assert lines(caplog, "repair review posted") == [
        f"repair review posted run={RUN_ID} window=1 decision=extend fallback=- status=201 truncated=true"
    ]
    assert len(lines(caplog, "repair model usage missing")) == 1
    answers: list[tuple[int, Any]] = [
        (200, {"created": False}),
        (409, {"detail": {"code": "already_reviewed"}}),
        (404, {"detail": {"code": "run_not_found"}}),
        (401, {"detail": {"code": "unauthorized"}}),
        (503, {"detail": {"code": "token_not_configured"}}),
        (422, {"detail": [{"loc": ["body", "reason"], "msg": "x"}]}),
        (307, {"detail": "moved"}),
    ]
    for status, body in answers:
        cairn_stub.posts = [(status, body)]
        cycle(start_agent(layout, clock=FakeClock()))
    cairn_stub.posts = [(500, {"detail": "busy"})]
    cycle(start_agent(layout, clock=FakeClock()))
    cairn_stub.posts = []
    for reply, copy in (
        (Reply(body=chat_answer("not json")), True),
        (Reply(400), True),
        (Reply(mode="silent"), True),
        (Reply(body=chat_answer(EXTEND)), False),
    ):
        if copy:
            write_run_copy(layout.root)
        else:
            _remove_copy(layout)
        stub.replies = [reply]
        cycle(
            _silent_agent(layout, cairn_stub, stub)
            if reply.mode == "silent"
            else start_agent(layout, clock=FakeClock())
        )
    cairn_stub.polls = [(503, {"detail": "busy"})]
    start_agent(layout).loop.poll_once()
    assert lines(caplog, "repair agent poll failed") == ["repair agent poll failed status=503 retry_in=5s"]
    # The digest_error fallback, a post that gets no answer, a run of a project the settings file does not list, and
    # a start that the isolation check refuses (the layout's copy root is writable by the user running the tests).
    cairn_stub.polls = []
    stub.replies = [Reply(body=chat_answer(EXTEND))]
    configure(layout, cairn_stub.url, reviewers=[reviewer_on(stub, api_key=REVIEWER_KEY)])
    for run_id in (SECOND_RUN_ID, THIRD_RUN_ID):
        write_run_copy(layout.root, run_id)
    broken = run_view(SECOND_RUN_ID)
    broken["slices_used"] = -1
    cairn_stub.runs = [broken]
    cycle(start_agent(layout, clock=FakeClock()))
    assert cairn_stub.posted()[-1]["fallback"] == "digest_error"
    cairn_stub.posts = [(0, None)]
    cairn_stub.runs = [run_view(THIRD_RUN_ID)]
    cycle(start_agent(layout, clock=FakeClock()))
    assert lines(caplog, "repair review failed")[-1] == f"repair review failed run={THIRD_RUN_ID} window=1 status=0"
    configure(layout, cairn_stub.url, reviewers=[reviewer_on(stub, api_key=REVIEWER_KEY)], projects=["proj_999"])
    cycle(start_agent(layout, clock=FakeClock()))
    assert lines(caplog, "repair review skipped") == [
        f"repair review skipped run={THIRD_RUN_ID} window=1 reason=project_not_listed"
    ]
    assert cli.start_or_report(cli.build_parser().parse_args(layout.argv())) is None
    assert lines(caplog, "repair agent isolation check failed")
    secrets = [AGENT_TOKEN, REVIEWER_KEY, SESSION_ID, "hello-from-tool", REASON_TEXT, EVIDENCE_TEXT, NEXT_STEP_TEXT]
    secrets += ["not json", INTENT, "transcript copy missing", "The run repeats"]
    _check_records(caplog.records, secrets)
    for prefix in (
        "repair review started",
        "repair digest built",
        "repair model call",
        "repair transcript unavailable",
        "repair review refused",
        "repair agent token rejected",
        "repair review rejected as invalid",
        "repair review failed",
    ):
        assert lines(caplog, prefix), prefix


def _remove_copy(layout: Layout) -> None:
    for name in ("meta.json", "transcript.jsonl"):
        path = layout.root / PROJECT / RUN_ID / name
        if path.exists():
            path.unlink()


def _silent_agent(layout: Layout, cairn: CairnStub, stub: ModelStub) -> Agent:
    configure(layout, cairn.url, reviewers=[reviewer_on(stub, timeout_seconds=1, max_attempts=1)])
    return start_agent(layout, clock=FakeClock())
