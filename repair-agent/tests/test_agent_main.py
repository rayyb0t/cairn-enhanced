"""`cairn-repair` enters the review loop after its startup (spec §9.3 step 7): with `--once` it polls once, finishes
the reviews that poll started and exits 0; otherwise it polls until SIGINT or SIGTERM. Either way such a signal stops
polling and waits up to 15 s for reviews in flight, and a second signal exits at once. That `--once` without a signal
waits past that limit is checked in process, with the limit lowered, in test_review_loop.py.

Each case runs the agent's real entry point in its own process (`python -m cairn_repair_agent.cli`) against loopback
stubs, with the guarded variables left out of its environment, except the last three: they run `loop.run_agent` in
its own process on a stub poll (`signal_trials.py`), and deliver the signal at each line the main thread runs, or at
random moments, because a handler runs between any two of that thread's bytecodes. Every value is synthetic.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from agentkit import (
    EXTEND,
    CairnStub,
    ModelStub,
    Reply,
    cairn_stubs,
    chat_answer,
    configure,
    model_stubs,
    run_view,
    wait_for,
    write_run_copy,
)
from conftest import Layout, reviewer_entry

from cairn_repair_agent import envguard

PROJECT_FOLDER = Path(__file__).resolve().parents[1]
SIGNAL_TRIALS = Path(__file__).with_name("signal_trials.py")


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture
def cairn_stub() -> Iterator[CairnStub]:
    yield from cairn_stubs()


def spawn(layout: Layout, *extra: str) -> subprocess.Popen[str]:
    env = {name: value for name, value in os.environ.items() if name.upper() not in envguard.GUARDED_VARIABLES}
    return subprocess.Popen(
        [sys.executable, "-m", "cairn_repair_agent.cli", *layout.argv("--allow-shared-user", *extra)],
        cwd=PROJECT_FOLDER,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen[str], timeout: float) -> tuple[int, str]:
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return process.returncode, stderr


def ready(layout: Layout, cairn: CairnStub, stub: ModelStub, **settings: Any) -> None:
    configure(layout, cairn.url, reviewers=[reviewer_entry(base_url=stub.url + "/v1", **settings)])
    layout.write_config(poll_interval=2)


def test_main_runs_one_poll_with_once(layout: Layout, cairn_stub: CairnStub, model_stub: Any) -> None:
    """With `--once`, the agent polls once, finishes the review that poll started (its post included) and exits 0."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    ready(layout, cairn_stub, stub)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    code, stderr = finish(spawn(layout, "--once"), timeout=60)
    assert code == 0, stderr
    assert len(cairn_stub.calls("GET", "/repair/runs")) == 1
    assert [body["decision"] for body in cairn_stub.posted()] == ["extend"]
    assert "repair review posted" in stderr and "Traceback" not in stderr


def test_main_runs_the_loop_until_a_signal(layout: Layout, cairn_stub: CairnStub, model_stub: Any) -> None:
    """Without `--once` the agent polls every `poll_interval` until SIGTERM; with no review in flight, it then
    exits 0 at once and polls no more."""
    ready(layout, cairn_stub, model_stub())
    process = spawn(layout)
    try:
        assert wait_for(lambda: len(cairn_stub.calls("GET", "/repair/runs")) >= 2, timeout=30)
    finally:
        process.send_signal(signal.SIGTERM)
    began = time.monotonic()
    code, stderr = finish(process, timeout=30)
    assert code == 0, stderr
    assert time.monotonic() - began < 5
    assert "repair agent started" in stderr and "Traceback" not in stderr


@pytest.mark.parametrize("once", [False, True], ids=["loop", "once"])
def test_main_waits_up_to_15_s_for_a_review_in_flight(
    layout: Layout, cairn_stub: CairnStub, model_stub: Any, once: bool
) -> None:
    """After SIGTERM the agent polls no more and waits for the review in flight, but at most 15 s, then exits 0; so it
    does with `--once`, which without a signal waits for every review its one poll started."""
    stub = model_stub(Reply(mode="silent"))
    ready(layout, cairn_stub, stub, timeout_seconds=120)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    process = spawn(layout, *(["--once"] if once else []))
    try:
        assert wait_for(lambda: len(stub.requests) == 1, timeout=30)
    finally:
        process.send_signal(signal.SIGTERM)
    began = time.monotonic()
    time.sleep(0.5)  # a poll already sent when the signal came is answered and recorded by now
    polls = len(cairn_stub.calls("GET", "/repair/runs"))
    time.sleep(2.5)  # longer than poll_interval (2 s)
    assert process.poll() is None  # still waiting for the review in flight
    assert len(cairn_stub.calls("GET", "/repair/runs")) == polls  # and polling no more
    code, stderr = finish(process, timeout=30)
    waited = time.monotonic() - began
    assert code == 0, stderr
    assert 14.0 <= waited < 20.0, waited
    assert cairn_stub.calls("POST") == []


def test_main_exits_at_once_on_a_second_signal(layout: Layout, cairn_stub: CairnStub, model_stub: Any) -> None:
    """A second signal, while the agent waits for a review in flight, exits at once (status 128 + the signal)."""
    stub = model_stub(Reply(mode="silent"))
    ready(layout, cairn_stub, stub, timeout_seconds=120)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    process = spawn(layout)
    try:
        assert wait_for(lambda: len(stub.requests) == 1, timeout=30)
        process.send_signal(signal.SIGTERM)
        time.sleep(1)
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
    began = time.monotonic()
    code, _ = finish(process, timeout=30)
    assert code == 128 + signal.SIGTERM
    assert time.monotonic() - began < 3


def signal_trials(mode: str) -> int:
    """Run `signal_trials.py <mode>` in its own process (its docstring says what each mode does) and return how many
    trials it ran, every one of which ended as it must. A trial that hangs ends that process 10 s after it began."""
    result = subprocess.run(
        [sys.executable, str(SIGNAL_TRIALS), mode], cwd=PROJECT_FOLDER, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.fullmatch(rf"ok mode={mode} trials=(\d+)\n", result.stdout)
    assert match is not None, result.stdout + result.stderr
    return int(match.group(1))


def test_a_signal_at_any_line_of_the_loop_stops_it() -> None:
    """SIGTERM stops the loop wherever the main thread is when its handler runs, which is between any two of that
    thread's bytecodes. A handler that waits for a lock the thread holds there never returns: one that set a
    `threading.Event` met the Event's lock held by `Event.wait` between two polls, and the agent never stopped, nor at a
    second signal. Here SIGTERM comes once at each line the main thread runs from the first poll to the fourth, the
    lines of `threading` it calls included, one trial each: every trial returns 0."""
    assert signal_trials("lines") >= 20


def test_a_second_signal_exits_even_within_the_first_ones_handler() -> None:
    """A second SIGTERM exits at once (128 + SIGTERM) even when it comes while the first is still being handled: here
    at each line the main thread runs from the first signal until its handler has returned, one trial each. A handler
    that checked an Event for an earlier signal and then set it did not exit when the second signal came between its
    check and the Event's flag, and waited forever for the Event's lock, held by the first handler, at the flag."""
    assert signal_trials("second") >= 3


def test_a_signal_at_a_random_moment_stops_the_loop() -> None:
    """The same with asynchronous signals: SIGTERM from another thread at a random moment while the loop polls back to
    back, 20 trials, each of which returns 0. With the Event's lock in the handler, about half of them hung."""
    assert signal_trials("random") == 20
