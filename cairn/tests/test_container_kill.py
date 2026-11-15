"""ManagedProcess.kill()/cancel() in container mode, against a fake Docker API.

KILL_GROUP_SCRIPT itself is run under real shells against a fake /proc tree at the end of this
file; the whole path against a real Docker daemon is in test_container_kill_docker.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from docker.errors import APIError, DockerException, NotFound

from cairn.dispatcher.runtime import process as process_module
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import KILL_GROUP_SCRIPT, ManagedProcess

COMMAND = ["timeout", "-k", "5s", "300s", "claude", "-p", "prompt with spaces\nand a newline é"]
OTHER = ["timeout", "-k", "5s", "300s", "claude", "-p", "another prompt"]
ENV = {"A": "B"}
SHA = hashlib.sha256(b"".join(arg.encode("utf-8") + b"\0" for arg in COMMAND)).hexdigest()


def config(argv: list[str]) -> dict[str, Any]:
    return {"tty": False, "entrypoint": argv[0], "arguments": argv[1:], "privileged": False, "user": ""}


class RunStream:
    """The run's output: yields its chunks, then blocks until end() (the run ending)."""

    def __init__(self) -> None:
        self.chunks: list[tuple[bytes | None, bytes | None]] = [(b"out", None), (None, b"err")]
        self.ended = threading.Event()
        self.closed = False

    def __iter__(self) -> Iterator[tuple[bytes | None, bytes | None]]:
        yield from self.chunks
        self.ended.wait(10)

    def end(self) -> None:
        self.ended.set()

    def close(self) -> None:
        self.closed = True


class HelperStream:
    def __init__(self, chunks: list[tuple[bytes | None, bytes | None]], then_block: bool) -> None:
        self._chunks = chunks
        self._then_block = then_block
        self.closed = False

    def __iter__(self) -> Iterator[tuple[bytes | None, bytes | None]]:
        yield from self._chunks
        if self._then_block:  # Docker holding the stream open: must not be waited for
            time.sleep(10)

    def close(self) -> None:
        self.closed = True


class RecordingResponse:
    """A stream's HTTP response: closing it releases the connection back to docker-py's pool."""

    def __init__(self) -> None:
        self.closed_by: list[str] = []

    def close(self) -> None:
        self.closed_by.append(threading.current_thread().name)


class SilentHelperStream:
    """A helper stream that never sends anything: Docker holding it open. close() shuts its socket
    down, which ends the blocked read as at the end of the stream, as docker-py's does; with
    fail_on_close, the read fails instead."""

    def __init__(self, fail_on_close: bool = False) -> None:
        self.closed = False
        self.closed_by: list[str] = []
        self._fail_on_close = fail_on_close
        self._closed = threading.Event()
        self._response = RecordingResponse()

    def __iter__(self) -> Iterator[tuple[bytes | None, bytes | None]]:
        return self

    def __next__(self) -> tuple[bytes | None, bytes | None]:
        self._closed.wait(10)
        if self._fail_on_close:
            raise ValueError("I/O operation on closed file")
        raise StopIteration

    def close(self) -> None:
        self.closed = True
        self.closed_by.append(threading.current_thread().name)
        self._closed.set()


class UncancellableHelperStream(SilentHelperStream):
    """docker-py over ssh://: close() cannot shut the stream down, so only Docker ends it (end())."""

    def close(self) -> None:
        self.closed_by.append(threading.current_thread().name)
        raise DockerException("Cancellable streams not supported for the SSH protocol")

    def end(self) -> None:
        self._closed.set()


class FailingHelperStream:
    """A helper stream whose read fails at once, long before the read bound."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def __iter__(self) -> FailingHelperStream:
        return self

    def __next__(self) -> tuple[bytes | None, bytes | None]:
        raise self.error

    def close(self) -> None:
        self.closed = True


class FakeAPI:
    """The run's exec is "exec-1"; every later exec_create is a kill helper."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.stream = RunStream()
        self.run_started = False  # Docker reports a created exec without a process until exec_start
        self.run_states: list[dict[str, Any] | Exception] = []
        self.run_state: dict[str, Any] = {"Running": True, "ExitCode": None, "Pid": 4242, "ProcessConfig": config(COMMAND)}
        self.ended_state: dict[str, Any] = {"Running": False, "ExitCode": 137, "Pid": 4242, "ProcessConfig": config(COMMAND)}
        self.others: dict[str, dict[str, Any] | Exception] = {}
        self.exec_ids_missing = False
        self.container_error: Exception | None = None
        self.helpers: list[tuple[list[str], dict[str, Any]]] = []
        self.helper_chunks: list[tuple[bytes | None, bytes | None]] = [(b"cairn-kill: killed 23\n", None)]
        self.helper_then_block = False
        self.helper_create_error: Exception | None = None
        self.helper_start_error: Exception | None = None
        self.helper_gate: threading.Event | None = None
        self.helper_stream_factory: Callable[[], Any] | None = None
        self.helper_streams: list[Any] = []

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def exec_create(self, container_id: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self.calls.append(("exec_create", (container_id, cmd), kwargs))
        if self.names().count("exec_create") == 1:
            return {"Id": "exec-1"}
        if self.helper_create_error is not None:
            raise self.helper_create_error
        self.helpers.append((list(cmd), kwargs))
        return {"Id": f"helper-{len(self.helpers)}"}

    def exec_start(self, exec_id: str, **kwargs: Any) -> Any:
        self.calls.append(("exec_start", (exec_id,), kwargs))
        if exec_id == "exec-1":
            self.run_started = True
            return self.stream
        if self.helper_gate is not None:
            self.helper_gate.wait(10)
        if self.helper_start_error is not None:
            raise self.helper_start_error
        if self.helper_stream_factory is not None:
            helper = self.helper_stream_factory()
            self.helper_streams.append(helper)
            return helper
        if any(out and out.startswith(b"cairn-kill: killed") for out, _ in self.helper_chunks):
            self.stream.end()
        helper = HelperStream(self.helper_chunks, self.helper_then_block)
        self.helper_streams.append(helper)
        return helper

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        self.calls.append(("exec_inspect", (exec_id,), {}))
        if exec_id != "exec-1":
            other = self.others[exec_id]
            if isinstance(other, Exception):
                raise other
            return other
        if self.stream.ended.is_set():
            return self.ended_state
        if not self.run_started:
            return {"Running": False, "ExitCode": None, "Pid": 0, "ProcessConfig": config(COMMAND)}
        if self.run_states:
            state = self.run_states.pop(0)
            if isinstance(state, Exception):
                raise state
            return state
        return self.run_state

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        self.calls.append(("inspect_container", (container_id,), {}))
        if self.container_error is not None:
            raise self.container_error
        return {"Id": container_id, "ExecIDs": None if self.exec_ids_missing else ["exec-1", *self.others]}


class FakeContainer:
    id = "container-id"
    name = "cairn-dispatch-proj_001"

    def __init__(self, api: FakeAPI) -> None:
        self.client = SimpleNamespace(api=api)


def make_process(command: list[str] | None = None) -> tuple[ManagedProcess, FakeAPI]:
    api = FakeAPI()
    return ManagedProcess(FakeContainer(api), list(command or COMMAND), dict(ENV)), api  # type: ignore[arg-type]


def started(process: ManagedProcess, api: FakeAPI) -> None:
    """start(), then wait until the reader thread has started the exec and read its first chunks."""
    process.start()
    deadline = time.monotonic() + 5
    while len(process._stderr) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "exec_start" in api.names()


def held_start(process: ManagedProcess) -> threading.Thread:
    """start() with the reader thread created but not yet running."""
    original = threading.Thread.start
    held: list[threading.Thread] = []
    threading.Thread.start = lambda thread: held.append(thread)  # type: ignore[method-assign,assignment]
    try:
        process.start()
    finally:
        threading.Thread.start = original  # type: ignore[method-assign]
    return held[0]


def warnings_for(caplog: pytest.LogCaptureFixture, text: str) -> list[int]:
    return [record.levelno for record in caplog.records if text in record.getMessage()]


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs Linux /proc")
def test_cmdline_hash_is_the_hash_of_proc_cmdline() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(30)", "spaces and é", "new\nline", ""]
    child = subprocess.Popen(argv)
    try:
        deadline = time.monotonic() + 5
        cmdline = b""
        while time.monotonic() < deadline:
            cmdline = Path(f"/proc/{child.pid}/cmdline").read_bytes()
            if cmdline.endswith(b"\0\0"):
                break
            time.sleep(0.01)
        assert process_module._cmdline_sha256(argv) == hashlib.sha256(cmdline).hexdigest()
    finally:
        child.kill()
        child.wait()


def test_normal_run_makes_exactly_the_same_docker_calls_as_before() -> None:
    process, api = make_process()
    started(process, api)
    api.ended_state = {"Running": False, "ExitCode": 3, "Pid": 4242}
    api.stream.end()

    result = process.communicate(timeout=5)

    assert api.calls[0] == (
        "exec_create",
        ("container-id", COMMAND),
        {"stdout": True, "stderr": True, "stdin": False, "tty": False, "environment": ENV},
    )
    assert api.calls[1] == ("exec_start", ("exec-1",), {"detach": False, "tty": False, "stream": True, "demux": True})
    assert set(api.names()[2:]) == {"exec_inspect"}
    assert (result.returncode, result.stdout, result.stderr) == (3, "out", "err")
    assert not result.timed_out and not result.cancelled


def test_kill_runs_one_helper_that_targets_this_runs_command_line() -> None:
    process, api = make_process()
    started(process, api)

    began = time.monotonic()
    process.kill()
    assert time.monotonic() - began < 1

    assert api.helpers == [
        (
            ["timeout", "-k", "1s", "3s", "/bin/sh", "-c", KILL_GROUP_SCRIPT, "cairn-kill", SHA],
            # the image's user; only a fixed locale is added to the container's environment
            {"stdout": True, "stderr": True, "stdin": False, "tty": False, "environment": {"LC_ALL": "C"}},
        )
    ]
    assert ("exec_start", ("helper-1",), {"detach": False, "tty": False, "stream": True, "demux": True}) in api.calls
    assert api.helper_streams[0].closed
    result = process.communicate(timeout=5)
    assert (result.returncode, result.timed_out, result.cancelled) == (137, False, False)

    calls = len(api.calls)
    process.kill()
    process.cancel("stopped")
    assert len(api.calls) == calls  # done: no further Docker call


def test_kill_matches_the_argv_docker_started() -> None:
    process, api = make_process()
    started(process, api)
    as_started = [*COMMAND[:-1], "prompt as Docker recorded it"]
    api.run_state = {**api.run_state, "ProcessConfig": config(as_started)}

    process.kill()

    assert api.helpers[0][0][-1] == process_module._cmdline_sha256(as_started)
    api.stream.end()


def test_kill_before_start_does_nothing() -> None:
    process, api = make_process()

    process.kill()

    assert api.calls == []
    started(process, api)  # the run then starts as usual, as before
    api.ended_state = {"Running": False, "ExitCode": 0, "Pid": 4242}
    api.stream.end()
    assert process.communicate(timeout=5).returncode == 0


@pytest.mark.parametrize(
    ("details", "argv"),
    [
        ({"ProcessConfig": config(COMMAND)}, COMMAND),
        ({"ProcessConfig": {"entrypoint": "sleep", "arguments": None}}, ["sleep"]),
        ({"ProcessConfig": {"entrypoint": "sleep"}}, ["sleep"]),
        ({}, None),
        ({"ProcessConfig": None}, None),
        ({"ProcessConfig": {"entrypoint": None, "arguments": ["x"]}}, None),
        ({"ProcessConfig": {"entrypoint": "sh", "arguments": "-c"}}, None),
        ({"ProcessConfig": {"entrypoint": "sh", "arguments": ["-c", 1]}}, None),
    ],
    ids=["full", "null-arguments", "no-arguments", "no-config", "null-config", "bad-entrypoint", "bad-arguments", "bad-argument"],
)
def test_exec_argv_reads_only_a_well_formed_process_config(details: dict[str, Any], argv: list[str] | None) -> None:
    assert process_module._exec_argv(details) == argv


def test_kill_after_the_run_ended_makes_one_inspect_and_nothing_else() -> None:
    process, api = make_process()
    started(process, api)
    api.run_state = {"Running": False, "ExitCode": 0, "Pid": 4242}
    calls = len(api.calls)

    process.kill()

    assert api.calls[calls:] == [("exec_inspect", ("exec-1",), {})]
    api.stream.end()


def test_kill_waits_for_the_process_of_an_exec_being_started() -> None:
    process, api = make_process()
    started(process, api)
    api.run_states = [
        {"Running": False, "ExitCode": None, "Pid": 0},  # exec_start sent, not yet handled
        {"Running": True, "ExitCode": None, "Pid": 0},  # Docker is creating the process
    ]
    calls = len(api.calls)

    process.kill()

    helper_created = next(i for i, call in enumerate(api.calls) if call[0] == "exec_create" and i >= calls)
    assert api.names()[calls:helper_created].count("exec_inspect") == 3  # the third one saw a Pid
    assert len(api.helpers) == 1


def test_kill_waits_for_a_missing_process_only_once(caplog: pytest.LogCaptureFixture) -> None:
    process, api = make_process()
    started(process, api)
    api.run_state = {"Running": True, "ExitCode": None, "Pid": 0}
    caplog.set_level(logging.DEBUG, logger=process_module.__name__)

    began = time.monotonic()
    process.kill()
    first = time.monotonic() - began
    began = time.monotonic()
    process.cancel("stopped")
    second = time.monotonic() - began

    assert process_module.EXEC_KILL_START_WAIT_SECONDS <= first < process_module.EXEC_KILL_START_WAIT_SECONDS + 1
    assert second < 0.5
    assert api.helpers == []
    assert warnings_for(caplog, "has no process yet") == [logging.WARNING, logging.DEBUG]
    api.stream.end()


def test_kill_stops_waiting_for_the_process_when_the_run_ends(caplog: pytest.LogCaptureFixture) -> None:
    process, api = make_process()
    started(process, api)
    api.run_state = {"Running": True, "ExitCode": None, "Pid": 0}
    caplog.set_level(logging.DEBUG, logger=process_module.__name__)
    reader_done = threading.Timer(0.2, process._done.set)  # the reader thread finishing meanwhile
    reader_done.start()

    began = time.monotonic()
    process.kill()

    assert time.monotonic() - began < 1  # not the whole EXEC_KILL_START_WAIT_SECONDS
    assert api.helpers == []
    assert warnings_for(caplog, "has no process yet") == []
    reader_done.join()
    api.stream.end()


def test_kill_before_exec_start_never_starts_the_exec() -> None:
    process, api = make_process()
    cancellation = TaskCancellation()
    cancellation.cancel("deleted")
    reader = held_start(process)
    cancellation.attach_process(process)  # what run_worker_process does right after start()
    reader.start()

    began = time.monotonic()
    result = process.communicate(timeout=5)

    assert time.monotonic() - began < 1
    assert api.names() == ["exec_create"]
    assert (result.returncode, result.stdout, result.stderr) == (137, "", "")
    assert (result.cancelled, result.cancel_reason, result.timed_out) == (True, "deleted", False)
    process.kill()
    assert api.names() == ["exec_create"]


def test_a_kill_racing_the_start_is_never_lost() -> None:
    for _ in range(50):
        process, api = make_process()
        reader = held_start(process)
        killer = threading.Thread(target=process.kill)
        killer.start()
        reader.start()
        killer.join(5)

        # Either the reader saw the request and never started the exec, or kill() saw the exec
        # started and killed it: never a started exec without a helper.
        assert ("exec_start" in api.names()) == (len(api.helpers) == 1)
        result = process.communicate(timeout=5)
        assert (result.returncode, result.timed_out) == (137, False)


def test_kill_refuses_while_another_running_exec_has_the_same_argv(caplog: pytest.LogCaptureFixture) -> None:
    process, api = make_process()
    started(process, api)
    api.others = {"exec-twin": {"Running": True, "ExitCode": None, "Pid": 99, "ProcessConfig": config(COMMAND)}}
    caplog.set_level(logging.DEBUG, logger=process_module.__name__)

    process.cancel("paused")
    process.cancel("stopped")

    assert api.helpers == []
    assert warnings_for(caplog, "other running execs have the same command line") == [logging.WARNING, logging.DEBUG]
    assert process._cancel_reason == "paused"

    api.others = {"exec-twin": {"Running": False, "ExitCode": 0, "Pid": 99, "ProcessConfig": config(COMMAND)}}
    process.cancel("stopped")
    assert len(api.helpers) == 1


def test_other_execs_that_cannot_be_confused_with_this_run_do_not_block_it() -> None:
    process, api = make_process()
    started(process, api)
    api.others = {
        "exec-other": {"Running": True, "ExitCode": None, "Pid": 98, "ProcessConfig": config(OTHER)},
        "exec-created": {"Running": False, "ExitCode": None, "Pid": 0, "ProcessConfig": config(COMMAND)},
        "exec-ended": {"Running": False, "ExitCode": 0, "Pid": 97, "ProcessConfig": config(COMMAND)},
        "exec-gone": NotFound("no such exec"),
    }

    process.kill()

    assert len(api.helpers) == 1
    assert process.communicate(timeout=5).returncode == 137


def test_a_running_exec_whose_argv_docker_does_not_report_counts_as_a_twin() -> None:
    process, api = make_process()
    started(process, api)
    api.others = {"exec-unknown": {"Running": True, "ExitCode": None, "Pid": 96}}

    process.kill()

    assert api.helpers == []
    api.stream.end()


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ([(b"cairn-kill: none\n", None)], "no process of the container exec found to kill"),
        ([(b"cairn-kill: ambiguous 22 23\n", None)], "helper reported ambiguous 22 23"),
        ([(b"cairn-kill: not-leader 22\n", None)], "helper reported not-leader 22"),
        ([(b"cairn-kill: kill-failed 22\n", None)], "helper reported kill-failed 22"),
        ([(b"cairn-kill: error no-sha256sum\n", None)], "helper reported error no-sha256sum"),
        ([(b'OCI runtime exec failed: exec: "timeout": not found\r\n', None)], 'output=OCI runtime exec failed: exec: "timeout"'),
        ([(None, b"/bin/sh: 1: cannot create: Permission denied")], "output=/bin/sh: 1: cannot create"),
    ],
    ids=["none", "ambiguous", "not-leader", "kill-failed", "error", "oci-error", "no-status"],
)
def test_helper_refusals_are_warned_once_and_retried(
    chunks: list[tuple[bytes | None, bytes | None]], message: str, caplog: pytest.LogCaptureFixture
) -> None:
    process, api = make_process()
    started(process, api)
    api.helper_chunks = chunks
    caplog.set_level(logging.DEBUG, logger=process_module.__name__)

    process.cancel("paused")
    process.cancel("stopped")

    assert len(api.helpers) == 2
    assert warnings_for(caplog, message) == [logging.WARNING, logging.DEBUG]
    assert not process._killed
    api.stream.end()


@pytest.mark.parametrize("where", ["inspect", "inspect_container", "exec_create", "exec_start"])
def test_docker_errors_are_logged_not_raised(where: str, caplog: pytest.LogCaptureFixture) -> None:
    process, api = make_process()
    started(process, api)
    error = APIError("container is not running")
    if where == "inspect":
        api.run_states = [error]
    elif where == "inspect_container":
        api.container_error = error
    elif where == "exec_create":
        api.helper_create_error = error
    else:
        api.helper_start_error = error

    assert process.kill() is None

    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not process._killed
    api.stream.end()


def test_transport_errors_still_propagate_as_before() -> None:
    process, api = make_process()
    started(process, api)
    api.run_states = [requests.ConnectionError("connection refused")]

    with pytest.raises(requests.ConnectionError):
        process.kill()
    api.stream.end()


def test_unencodable_argv_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    process, api = make_process(["timeout", "-k", "5s", "300s", "agent", "\udcff"])
    started(process, api)
    api.run_state = {"Running": True, "ExitCode": None, "Pid": 4242}  # no ProcessConfig: falls back to self.command

    process.kill()

    assert api.helpers == []
    assert "cannot identify container exec to kill" in caplog.text
    api.stream.end()


def test_helper_output_is_read_only_up_to_its_status_line() -> None:
    process, api = make_process()
    started(process, api)
    api.helper_then_block = True

    began = time.monotonic()
    process.kill()

    assert time.monotonic() - began < 1
    assert process._killed and api.helper_streams[0].closed


def test_concurrent_kills_run_one_helper() -> None:
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    first = threading.Thread(target=process.kill)
    first.start()
    deadline = time.monotonic() + 5
    while "helper-1" not in str(api.calls) and time.monotonic() < deadline:
        time.sleep(0.01)

    began = time.monotonic()
    process.cancel("stopped")  # e.g. the scheduler while the heartbeat thread is killing
    assert time.monotonic() - began < 0.5

    api.helper_gate.set()
    first.join(5)
    assert len(api.helpers) == 1
    assert process.communicate(timeout=5).cancel_reason == "stopped"


def test_a_kill_that_gets_the_lock_after_another_one_succeeded_does_nothing() -> None:
    process, api = make_process()
    started(process, api)

    class LateLock:
        """The kill lock as a second caller gets it: just after the first caller's kill succeeded."""

        def __init__(self) -> None:
            self.lock = threading.Lock()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            acquired = self.lock.acquire(blocking, timeout)
            process._killed = True
            return acquired

        def release(self) -> None:
            self.lock.release()

    late = LateLock()
    process._kill_lock = late  # type: ignore[assignment]
    calls = len(api.calls)

    process.kill()

    assert api.calls[calls:] == []
    assert not late.lock.locked()
    api.stream.end()


def started_attempt_in(thread: threading.Thread, api: FakeAPI) -> None:
    """Start `thread`, whose kill attempt then waits in its helper's exec_start (api.helper_gate) until released."""
    thread.start()
    deadline = time.monotonic() + 5
    while "helper-1" not in str(api.calls) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "helper-1" in str(api.calls)


def test_a_one_shot_kill_waits_for_an_attempt_in_progress_and_then_makes_its_own() -> None:
    # The scheduler's cancel() is in its attempt when the lease is lost: dropping that kill(), as e45ad72 did,
    # leaves the run to cancel()'s next attempt, 30 s later when this one was slow (and to nothing at all when the
    # attempt in progress is the other one-shot kill()). It waits, and as that attempt did not kill, makes its own.
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    outcomes: list[list[tuple[bytes | None, bytes | None]]] = [[(b"cairn-kill: none\n", None)], [(b"cairn-kill: killed 23\n", None)]]

    def next_helper() -> HelperStream:
        chunks = outcomes.pop(0)
        if chunks[0][0] and chunks[0][0].startswith(b"cairn-kill: killed"):
            api.stream.end()
        return HelperStream(chunks, False)

    api.helper_stream_factory = next_helper
    scheduler = threading.Thread(target=process.cancel, args=("deleted",))
    started_attempt_in(scheduler, api)
    lease = threading.Thread(target=process.kill)
    lease.start()
    time.sleep(0.2)
    assert lease.is_alive()  # waiting for the attempt in progress, not dropped

    api.helper_gate.set()
    scheduler.join(5)
    lease.join(5)
    assert len(api.helpers) == 2 and process._killed
    assert process.communicate(timeout=5).returncode == 137


def test_a_one_shot_kill_that_waited_for_an_attempt_that_killed_does_nothing_more() -> None:
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    scheduler = threading.Thread(target=process.cancel, args=("deleted",))
    started_attempt_in(scheduler, api)
    lease = threading.Thread(target=process.kill)
    lease.start()
    time.sleep(0.2)

    api.helper_gate.set()
    scheduler.join(5)
    lease.join(5)
    assert process._killed and len(api.helpers) == 1


def test_a_one_shot_kill_waits_for_another_attempt_only_up_to_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_module, "EXEC_KILL_WAIT_FOR_ATTEMPT_SECONDS", 0.3)
    process, api = make_process()
    started(process, api)
    process._kill_lock.acquire()  # an attempt in progress on another thread that does not end in time
    try:
        began = time.monotonic()
        process.kill()
        assert 0.25 <= time.monotonic() - began < 2
    finally:
        process._kill_lock.release()
    assert api.helpers == []
    api.stream.end()


def test_a_paused_cancel_never_takes_the_kill_lock() -> None:
    # A lease-loss kill() landing while a paused tick held the lock would otherwise wait on it for nothing.
    process, api = make_process()
    started(process, api)
    process._cancel_paused_until = time.monotonic() + 30
    taken: list[str] = []

    class RecordingLock:
        def __init__(self) -> None:
            self.lock = threading.Lock()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            taken.append(threading.current_thread().name)
            return self.lock.acquire(blocking, timeout)

        def release(self) -> None:
            self.lock.release()

    process._kill_lock = RecordingLock()  # type: ignore[assignment]
    calls = len(api.calls)

    process.cancel("deleted")
    assert taken == [] and api.calls[calls:] == []

    process.kill()  # the pause is cancel()'s only
    assert taken and process._killed


def test_a_container_that_lists_no_execs_does_not_block_the_kill() -> None:
    process, api = make_process()
    started(process, api)
    api.exec_ids_missing = True

    process.kill()

    assert len(api.helpers) == 1
    assert process.communicate(timeout=5).returncode == 137


def test_communicate_timeout_kills_the_run() -> None:
    process, api = make_process()
    started(process, api)

    result = process.communicate(timeout=0.2)

    assert len(api.helpers) == 1
    assert (result.returncode, result.timed_out, result.cancelled) == (137, True, False)


@pytest.mark.parametrize("fail_on_close", [False, True], ids=["read-ends", "read-fails"])
def test_a_helper_that_never_answers_is_abandoned_at_the_read_bound(
    fail_on_close: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Docker ends a helper's stream only after every earlier exit in the container, so kill() must
    # not wait for the stream to end when the helper prints nothing.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.3)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = lambda: SilentHelperStream(fail_on_close)

    began = time.monotonic()
    process.kill()

    assert time.monotonic() - began < 2
    assert not process._killed and api.helper_streams[0].closed
    assert warnings_for(caplog, "kill helper gave no result") == [logging.WARNING]
    api.stream.end()


def test_a_helper_error_line_on_stderr_ends_the_read(caplog: pytest.LogCaptureFixture) -> None:
    # The script sends its own stderr to /dev/null, so a stderr line means the helper itself failed.
    process, api = make_process()
    started(process, api)
    api.helper_chunks = [(None, b"timeout: failed to run command '/bin/sh': No such file or directory\n")]
    api.helper_then_block = True

    began = time.monotonic()
    process.kill()

    assert time.monotonic() - began < 1
    assert not process._killed and api.helper_streams[0].closed
    assert warnings_for(caplog, "output=timeout: failed to run command") == [logging.WARNING]
    api.stream.end()


def test_a_slow_attempt_pauses_later_cancel_attempts(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # cancel() is repeated every scheduler tick; an attempt that took long and did not kill must not
    # hold the scheduler loop on every tick.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.3)
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = SilentHelperStream
    caplog.set_level(logging.DEBUG, logger=process_module.__name__)

    process.cancel("stopped")
    assert len(api.helpers) == 1
    assert warnings_for(caplog, "cancel() skips kill attempts for 30s") == [logging.WARNING]
    calls = len(api.calls)

    began = time.monotonic()
    process.cancel("stopped")
    assert time.monotonic() - began < 0.1
    assert api.calls[calls:] == []  # paused: no Docker call at all

    process._cancel_paused_until = 0.0  # the pause is over
    process.cancel("stopped")
    assert len(api.helpers) == 2
    api.stream.end()


def test_a_fast_helper_failure_does_not_pause_later_attempts() -> None:
    process, api = make_process()
    started(process, api)
    api.helper_chunks = [(b"cairn-kill: none\n", None)]

    process.cancel("paused")
    process.cancel("stopped")

    assert len(api.helpers) == 2 and process._cancel_paused_until == 0.0
    api.stream.end()


def test_the_pause_lasts_its_configured_time_and_then_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.3)
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_PAUSE_SECONDS", 0.8)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = SilentHelperStream

    process.cancel("stopped")
    ended = time.monotonic()
    time.sleep(max(0.0, ended + 0.45 - time.monotonic()))
    process.cancel("stopped")  # 0.45 s in: still paused
    assert len(api.helpers) == 1
    time.sleep(max(0.0, ended + 0.95 - time.monotonic()))
    process.cancel("stopped")  # 0.95 s in: the pause is over
    assert len(api.helpers) == 2
    api.stream.end()


def test_a_slow_attempt_that_fails_in_docker_also_pauses(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # e.g. exec_start answering only after seconds, with an error: the scheduler must not pay that every tick.
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    api.helper_start_error = APIError("500 Server Error: timeout")
    opener = threading.Timer(0.3, api.helper_gate.set)
    opener.start()

    process.cancel("stopped")
    calls = len(api.calls)
    process.cancel("stopped")

    assert api.calls[calls:] == []
    assert warnings_for(caplog, "cancel() skips kill attempts") == [logging.WARNING]
    opener.join()
    api.stream.end()


def test_a_slow_twin_check_that_refuses_also_pauses_cancel(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Docker answers an exec's inspect only after it has handled that exec's exit, which may wait 2 s.
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.others = {"exec-twin": {"Running": True, "ExitCode": None, "Pid": 99, "ProcessConfig": config(COMMAND)}}
    inspect = api.exec_inspect

    def slow_inspect(exec_id: str) -> dict[str, Any]:
        if exec_id == "exec-twin":
            time.sleep(0.3)
        return inspect(exec_id)

    monkeypatch.setattr(api, "exec_inspect", slow_inspect)

    process.cancel("stopped")
    calls = len(api.calls)
    process.cancel("stopped")

    assert api.helpers == [] and api.calls[calls:] == []
    assert warnings_for(caplog, "cancel() skips kill attempts for 30s") == [logging.WARNING]
    api.stream.end()


def test_a_slow_attempt_that_killed_neither_pauses_nor_warns(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    opener = threading.Timer(0.3, api.helper_gate.set)  # exec_start answers after 0.3 s
    opener.start()

    process.cancel("stopped")

    assert process._killed and process._cancel_paused_until == 0.0
    assert warnings_for(caplog, "cancel() skips kill attempts") == []
    opener.join()


@pytest.mark.parametrize("caller", ["kill", "communicate"])
def test_one_shot_kills_still_try_while_cancel_is_paused(caller: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # HeartbeatLease._fail and communicate()'s timeout call kill() once, off the scheduler thread:
    # nothing would retry a kill they skipped.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.3)
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = SilentHelperStream
    process.cancel("deleted")  # the scheduler's cancel(): slow, no verdict, so a pause
    assert process._cancel_paused_until > time.monotonic() + 25
    api.helper_stream_factory = None  # the helper now answers "killed" at once

    if caller == "kill":
        process.kill()
    else:
        result = process.communicate(timeout=0.2)
        assert (result.returncode, result.timed_out) == (137, True)

    assert len(api.helpers) == 2 and process._killed


def test_the_read_bound_only_wakes_the_read_and_kill_releases_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Over DOCKER_HOST=tcp:// all requests share one connection pool: a response released by the
    # Timer thread and by the reading thread at once could hand one connection to two requests.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = SilentHelperStream
    me = threading.current_thread().name

    process.kill()

    stream = api.helper_streams[0]
    assert stream.closed_by[0] != me  # the read bound's Timer woke the read (a socket shutdown)...
    assert stream._response.closed_by == [me]  # ...and only kill()'s own thread released the response, once
    api.stream.end()


def test_a_stream_docker_py_cannot_cancel_is_read_until_docker_ends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Over ssh:// the Timer cannot wake the read; it still never releases the response itself.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = UncancellableHelperStream
    me = threading.current_thread().name
    ender = threading.Timer(0.6, lambda: api.helper_streams[0].end())  # Docker ending the stream
    ender.start()

    began = time.monotonic()
    process.kill()

    assert time.monotonic() - began >= 0.5
    assert api.helper_streams[0]._response.closed_by == [me]
    ender.join()
    api.stream.end()


@pytest.mark.parametrize("error", [OSError("connection reset by peer"), ValueError("bad frame")], ids=["oserror", "valueerror"])
def test_a_read_error_before_the_read_bound_is_not_hidden(error: Exception) -> None:
    # Only a read that the read bound itself cut short is expected to fail; any other error is raised as before.
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = lambda: FailingHelperStream(error)

    with pytest.raises(type(error)):
        process.kill()

    assert api.helper_streams[0].closed and not process._killed
    api.stream.end()


def test_the_read_bound_timer_is_stopped_when_the_status_line_arrives() -> None:
    process, api = make_process()
    started(process, api)
    before = {thread for thread in threading.enumerate() if isinstance(thread, threading.Timer)}

    process.kill()

    assert process._killed
    timers = [thread for thread in threading.enumerate() if isinstance(thread, threading.Timer) and thread not in before]
    for timer in timers:
        timer.join(0.5)
    assert [timer for timer in timers if timer.is_alive()] == []


def test_a_slow_kill_attempt_also_pauses_later_cancel_attempts(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # The pause protects the scheduler loop whatever made the slow attempt: after a lease-loss kill() found the
    # container slow, the next ticks' cancel() must not pay for it again.
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.3)
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = SilentHelperStream

    process.kill()
    calls = len(api.calls)
    process.cancel("deleted")

    assert len(api.helpers) == 1 and api.calls[calls:] == []
    assert warnings_for(caplog, "cancel() skips kill attempts for 30s") == [logging.WARNING]
    api.stream.end()


def test_waiting_for_the_runs_process_is_not_timed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the Docker calls after the run's process was found count: a kill that first waited for Docker to
    # create the run's process and then failed fast does not pause cancel().
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    no_pid: dict[str, Any] = {"Running": True, "ExitCode": None, "Pid": 0, "ProcessConfig": config(COMMAND)}
    api.run_states = [no_pid] * 6  # about 0.3 s of 50 ms polls before the process exists
    api.helper_chunks = [(b"cairn-kill: none\n", None)]

    process.cancel("stopped")
    process.cancel("stopped")

    assert len(api.helpers) == 2 and process._cancel_paused_until == 0.0
    api.stream.end()


def test_the_read_bound_thread_never_dies_with_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # Over ssh:// docker-py's stream.close() raises. The Timer thread must swallow that: an exception there would
    # only be printed to stderr.
    errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    monkeypatch.setattr(process_module, "EXEC_KILL_HELPER_READ_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_stream_factory = UncancellableHelperStream
    ender = threading.Timer(0.5, lambda: api.helper_streams[0].end())  # Docker ending the stream
    ender.start()

    process.kill()
    ender.join()
    for timer in [thread for thread in threading.enumerate() if isinstance(thread, threading.Timer)]:
        timer.join(1)

    assert errors == []
    api.stream.end()


def test_a_slow_attempt_that_raises_also_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    # For example docker-py's own timeout on exec_start: requests' ReadTimeout is not a DockerException, so it
    # leaves cancel() (the scheduler loop logs it and goes on); the next ticks must not wait for it again.
    monkeypatch.setattr(process_module, "EXEC_KILL_SLOW_ATTEMPT_SECONDS", 0.2)
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    api.helper_start_error = requests.exceptions.ReadTimeout("read timed out")
    opener = threading.Timer(0.3, api.helper_gate.set)  # exec_start fails after 0.3 s
    opener.start()

    with pytest.raises(requests.exceptions.ReadTimeout):
        process.cancel("stopped")
    calls = len(api.calls)
    process.cancel("stopped")

    assert api.calls[calls:] == []
    opener.join()
    api.stream.end()


def test_a_read_bound_that_cannot_start_its_thread_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # cancel() runs on the scheduler thread, whose loop survives only requests errors.
    process, api = make_process()
    started(process, api)

    def no_thread(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Timer, "start", no_thread)

    process.cancel("deleted")

    assert api.helper_streams[0].closed and not process._killed
    assert warnings_for(caplog, "kill helper started but its result could not be read") == [logging.WARNING]
    assert warnings_for(caplog, "error=can't start new thread") == [logging.WARNING]
    api.stream.end()


def test_an_exec_that_cannot_be_inspected_blocks_the_kill(caplog: pytest.LogCaptureFixture) -> None:
    # Only NotFound means "gone". Any other error inspecting another exec may hide a live twin.
    process, api = make_process()
    started(process, api)
    api.others = {
        "exec-other": {"Running": True, "ExitCode": None, "Pid": 98, "ProcessConfig": config(["other"])},
        "exec-broken": APIError("500 Server Error: internal"),
    }

    process.kill()

    assert api.helpers == []
    assert warnings_for(caplog, "failed to kill container exec") == [logging.WARNING]
    api.stream.end()


def test_a_status_line_split_across_frames_is_read_whole() -> None:
    process, api = make_process()
    started(process, api)
    api.helper_chunks = [(b"cairn-kill: kil", None), (b"led 23\n", None)]

    process.kill()

    assert process._killed
    api.stream.end()


def test_the_twin_check_compares_the_argv_docker_recorded() -> None:
    process, api = make_process()
    started(process, api)
    as_started = [*COMMAND[:-1], "prompt as Docker recorded it"]
    api.run_state = {**api.run_state, "ProcessConfig": config(as_started)}
    api.others = {"exec-twin": {"Running": True, "ExitCode": None, "Pid": 99, "ProcessConfig": config(as_started)}}

    process.kill()

    assert api.helpers == []
    api.stream.end()


class PausingProcess(ManagedProcess):
    """The reader thread pauses right after reading _kill_requested, until kill() has returned (or 0.5 s)."""

    reader_paused = threading.Event()
    kill_returned = threading.Event()

    @property
    def _kill_requested(self) -> bool:  # type: ignore[override]
        value = self.__dict__.get("_kr", False)
        if threading.current_thread().name == "reader-under-test":
            self.reader_paused.set()
            self.kill_returned.wait(0.5)
        return value

    @_kill_requested.setter
    def _kill_requested(self, value: bool) -> None:
        self.__dict__["_kr"] = value


def test_the_start_handshake_never_loses_a_kill() -> None:
    api = FakeAPI()
    PausingProcess.reader_paused = threading.Event()
    PausingProcess.kill_returned = threading.Event()
    process = PausingProcess(FakeContainer(api), list(COMMAND), dict(ENV))  # type: ignore[arg-type]
    reader = held_start(process)
    reader.name = "reader-under-test"
    reader.start()
    assert PausingProcess.reader_paused.wait(5)  # the reader has read _kill_requested and not acted on it yet

    process.kill()
    PausingProcess.kill_returned.set()
    deadline = time.monotonic() + 1
    while not (api.run_started or process._done.is_set()) and time.monotonic() < deadline:
        time.sleep(0.01)

    # Either the exec never started, or kill() saw it started and ran the helper. Checked before
    # communicate(), whose own timeout would kill the run and hide a lost kill.
    assert api.run_started == (len(api.helpers) == 1)
    api.stream.end()
    assert process.communicate(timeout=5).returncode == 137


# -- KILL_GROUP_SCRIPT under real shells, against a fake /proc tree ----------------------------

SHELLS = [cmd for cmd in (["dash", "-c"], ["bash", "--posix", "-c"], ["busybox", "sh", "-c"]) if shutil.which(cmd[0])]
needs_shell = pytest.mark.skipif(not SHELLS, reason="no POSIX shell to run the helper script")
each_shell = pytest.mark.parametrize("shell", SHELLS, ids=[shell[0] for shell in SHELLS])
RECORD_KILLS = 'kill() { echo "$*" >> "$FAKE_KILL_LOG"; }\n'


def _proc(root: Path, pid: int | str, comm: str, ppid: int, pgrp: int, start: int, argv: list[str] | None) -> None:
    fields = ["S", str(ppid), str(pgrp), str(pgrp), "0", "-1", *["0"] * 13, str(start), *["0"] * 30]
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    if argv is not None:
        (directory / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))


@pytest.fixture
def fake_proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    _proc(root, "self", "sh", 0, 900, 60000, None)
    _proc(root, 1, "sleep", 0, 1, 100, COMMAND)  # PID 1 is never a candidate, whatever its argv
    _proc(root, 22, "time) out", 0, 22, 50000, COMMAND)  # this run's root; its name contains ") "
    _proc(root, 33, "claude", 22, 22, 50001, ["claude"])  # its agent, in its group
    _proc(root, 40, "timeout", 0, 40, 50010, OTHER)  # another run's root
    _proc(root, 41, "timeout", 7, 41, 50000, COMMAND)  # same argv, but not an exec root
    _proc(root, 55, "timeout", 0, 55, 50020, None)  # an exec root that exited between its stat and cmdline reads
    (root / "77").mkdir()  # exited between the listing and the read
    _proc(root, "9x", "timeout", 0, 9, 50000, COMMAND)  # not a PID, whatever it holds
    return root


def run_script(shell: list[str], root: Path, want: str = SHA, prelude: str = RECORD_KILLS) -> tuple[str, str]:
    log = root.parent / "kills.log"
    log.write_text("")
    done = subprocess.run(
        [*shell, prelude + KILL_GROUP_SCRIPT, "cairn-kill", want, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "FAKE_KILL_LOG": str(log)},
    )
    return done.stdout.strip(), log.read_text().strip()


@needs_shell
@each_shell
def test_script_kills_the_group_of_the_only_matching_root(shell: list[str], fake_proc: Path) -> None:
    assert run_script(shell, fake_proc) == ("cairn-kill: killed 22", "-KILL -22")


@needs_shell
@each_shell
def test_script_signals_nothing_without_a_match(shell: list[str], fake_proc: Path) -> None:
    assert run_script(shell, fake_proc, want="00" * 32) == ("cairn-kill: none", "")


@needs_shell
@each_shell
def test_script_refuses_two_matching_roots(shell: list[str], fake_proc: Path) -> None:
    _proc(fake_proc, 23, "timeout", 0, 9, 50002, COMMAND)  # same argv, exec root, not even a leader
    assert run_script(shell, fake_proc) == ("cairn-kill: ambiguous 22 23", "")


@needs_shell
@each_shell
def test_script_refuses_a_root_that_does_not_lead_its_group(shell: list[str], fake_proc: Path) -> None:
    _proc(fake_proc, 22, "timeout", 0, 9, 50000, COMMAND)
    assert run_script(shell, fake_proc) == ("cairn-kill: not-leader 22", "")


@needs_shell
@each_shell
@pytest.mark.parametrize(
    "change",
    ["s/ 50000 / 50999 /", "s/) S 0 22 22 /) S 7 22 22 /", "s/) S 0 22 22 /) S 0 9 22 /"],
    ids=["start-time", "parent", "group"],
)
def test_script_refuses_a_root_that_changed_before_the_signal(shell: list[str], fake_proc: Path, change: str) -> None:
    # Every sha256sum call (22's, then 40's) rewrites 22's stat after the scan has read it: a new start
    # time (22 ended and its PID was reused), a new parent or a new group, before the signal.
    rewrite = f"_proc22={fake_proc}/22/stat\n" + f'sha256sum() {{ command sha256sum; sed -i "{change}" "$_proc22"; }}\n'
    assert run_script(shell, fake_proc, prelude=RECORD_KILLS + rewrite) == ("cairn-kill: none", "")


@needs_shell
@each_shell
def test_script_refuses_a_root_that_ended_before_the_signal(shell: list[str], tmp_path: Path) -> None:
    # 22's stat is the last one the scan reads, and sha256sum then removes it, as if 22 had just ended:
    # the failed re-read must stop the script, not let it check the line the scan read.
    root = tmp_path / "proc"
    _proc(root, "self", "sh", 0, 900, 60000, None)
    _proc(root, 1, "sleep", 0, 1, 100, None)
    _proc(root, 22, "timeout", 0, 22, 50000, COMMAND)
    remove = f"_proc22={root}/22/stat\n" + 'sha256sum() { command sha256sum; rm -f "$_proc22"; }\n'
    assert run_script(shell, root, prelude=RECORD_KILLS + remove) == ("cairn-kill: none", "")


@needs_shell
@each_shell
def test_script_reports_a_failed_kill(shell: list[str], fake_proc: Path) -> None:
    assert run_script(shell, fake_proc, prelude="kill() { return 1; }\n") == ("cairn-kill: kill-failed 22", "")


@needs_shell
@each_shell
def test_script_reports_missing_tools(shell: list[str], fake_proc: Path) -> None:
    no_sha256sum = "command() { return 1; }\n"  # the script's only use of command is `command -v sha256sum`
    assert run_script(shell, fake_proc, prelude=no_sha256sum)[0] == "cairn-kill: error no-sha256sum"
    assert run_script(shell, fake_proc.parent)[0] == "cairn-kill: error no-proc"


@needs_shell
@each_shell
def test_script_does_not_count_a_matching_process_whose_parent_is_pid_1(shell: list[str], fake_proc: Path) -> None:
    # An orphan reparented to the container's init is not an exec root, whatever its command line:
    # it must not make this run's root ambiguous, nor be signalled.
    _proc(fake_proc, 30, "timeout", 1, 30, 50003, COMMAND)
    assert run_script(shell, fake_proc) == ("cairn-kill: killed 22", "-KILL -22")


@needs_shell
@each_shell
def test_the_helpers_environment_keeps_the_shell_quiet_on_stderr(shell: list[str], fake_proc: Path) -> None:
    # The helper's read ends at the first line on stdout or stderr. bash as /bin/sh warns on stderr
    # at startup when LC_ALL names a locale the image lacks; Docker puts the helper's own LC_ALL
    # over the container's, so the status line stays the first line.
    log = fake_proc.parent / "kills.log"
    log.write_text("")
    done = subprocess.run(
        [*shell, RECORD_KILLS + KILL_GROUP_SCRIPT, "cairn-kill", SHA, str(fake_proc)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "LC_ALL": "en_ZZ.UTF-8", "FAKE_KILL_LOG": str(log), **process_module.EXEC_KILL_HELPER_ENV},
    )
    assert (done.stdout, done.stderr) == ("cairn-kill: killed 22\n", "")


# -- the lock wait and the pause check (review11) -------------------------------------------------


def calls_by_thread(api: FakeAPI) -> list[tuple[str, str]]:
    """Record (thread name, call) for every Docker call the fake API answers from now on."""
    seen: list[tuple[str, str]] = []
    for method in ("exec_create", "exec_start", "exec_inspect", "inspect_container"):
        original = getattr(api, method)

        def recording(*args: Any, _original: Any = original, _method: str = method, **kwargs: Any) -> Any:
            seen.append((threading.current_thread().name, _method))
            return _original(*args, **kwargs)

        setattr(api, method, recording)
    return seen


def test_a_one_shot_kill_that_waited_for_an_attempt_that_killed_makes_no_docker_call() -> None:
    # It waited because the lock was taken: once it has the lock, the attempt it waited for may have killed,
    # and it must look before it makes a call of its own.
    process, api = make_process()
    started(process, api)
    api.helper_gate = threading.Event()
    seen = calls_by_thread(api)
    scheduler = threading.Thread(target=process.cancel, args=("deleted",), name="scheduler")
    started_attempt_in(scheduler, api)
    lease = threading.Thread(target=process.kill, name="lease")
    lease.start()
    time.sleep(0.2)
    assert lease.is_alive()

    api.helper_gate.set()
    scheduler.join(5)
    lease.join(5)
    assert process._killed and len(api.helpers) == 1
    assert [call for thread, call in seen if thread == "lease"] == []


def test_a_cancel_that_takes_the_lock_just_after_a_slow_attempt_ended_makes_no_attempt() -> None:
    # The pause is read once before the lock, without it. An attempt on another thread that was slow, did not
    # kill and released the lock in between has just set it: the check under the lock must see it.
    process, api = make_process()
    started(process, api)

    class LockOfASlowAttemptThatJustEnded:
        def __init__(self) -> None:
            self.lock = threading.Lock()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            acquired = self.lock.acquire(blocking, timeout)
            process._cancel_paused_until = time.monotonic() + 30
            return acquired

        def release(self) -> None:
            self.lock.release()

    process._kill_lock = LockOfASlowAttemptThatJustEnded()  # type: ignore[assignment]
    calls = len(api.calls)

    process.cancel("deleted")

    assert api.calls[calls:] == [] and not process._killed
    api.stream.end()


def test_the_wait_for_another_attempt_covers_a_measured_attempt_and_no_more() -> None:
    # Longer than the helper's read plus the wait for the run's process and than every attempt measured on Docker
    # (11.2 s at most); not so long that communicate()'s timeout or the lease loss waits much longer than that.
    wait = process_module.EXEC_KILL_WAIT_FOR_ATTEMPT_SECONDS
    assert process_module.EXEC_KILL_HELPER_READ_SECONDS + process_module.EXEC_KILL_START_WAIT_SECONDS < wait
    assert 11.2 < wait <= 15


def test_a_paused_cancel_says_so_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=process_module.LOG.name)
    process, api = make_process()
    started(process, api)
    process._cancel_paused_until = time.monotonic() + 30

    process.cancel("deleted")

    assert [r.levelno for r in caplog.records if "cancel skips the kill attempt after a slow one" in r.getMessage()] == [logging.DEBUG]
    api.stream.end()


def test_a_one_shot_kill_gives_up_at_its_bound_rather_than_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    # As test_a_one_shot_kill_waits_for_another_attempt_only_up_to_its_bound, but kill() runs on a thread of its
    # own: a wait without a bound then fails the test instead of hanging the test run (the lock is never freed).
    monkeypatch.setattr(process_module, "EXEC_KILL_WAIT_FOR_ATTEMPT_SECONDS", 0.3)
    process, api = make_process()
    started(process, api)
    process._kill_lock.acquire()  # an attempt in progress on another thread that does not end in time
    lease = threading.Thread(target=process.kill, name="lease", daemon=True)
    try:
        began = time.monotonic()
        lease.start()
        lease.join(5)
        assert not lease.is_alive() and 0.25 <= time.monotonic() - began < 2
    finally:
        process._kill_lock.release()
        lease.join(5)
    assert api.helpers == []
    api.stream.end()
