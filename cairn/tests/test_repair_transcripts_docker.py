"""Transcript-copy test of spec 16 against a real Docker daemon: TC15 (build step 7; skipped without a daemon).

Docker holds a container's lock while an archive of it streams (spec 8.8), so every other call on that container
waits: another run's exec_create, an inspect, and the two the scheduler's main loop makes when it stops a project,
the cancel kill and the cleanup inspect. TC15 checks the bound that keeps this short on this machine's daemon,
against this tree's kill path (spec 8.9): BOUND_SECONDS per call, while a COPY_MAX_BYTES copy streams, right after a
refused copy, and for one scheduler loop iteration that stops a switched-on project during its copy (a live copy, and
a final copy of a task that has already returned). The image must be present locally (nothing is pulled):
CAIRN_TEST_DOCKER_IMAGE, default debian:bookworm-slim. With CAIRN_TC15_REPORT set to a path, the measured times are
written there as JSON. Every value here is synthetic.

Whichever call reaches Docker first while the copy streams waits for the lock, and once it returns Docker has let the
lock go, so a call issued after it waits for nothing. Each trial therefore issues exec_create and inspect at the same
moment, each from a thread and a client of its own, while the copy has most of its archive still to stream, and checks
for each call that it was issued then (_overlap).
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from repair_dispatcher_support import FAKE_TOKEN, reset_repair_process_state
from test_repair_transcripts_support import SESSION_IDS, FakeClock, copy_config, fixture_bytes, make_run

from cairn.dispatcher.config import ContainerConfig, DispatchConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.repair import transcripts
from cairn.dispatcher.repair.transcripts import CopySchedule
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProjectSummary

IMAGE = os.environ.get("CAIRN_TEST_DOCKER_IMAGE", "debian:bookworm-slim")
BOUND_SECONDS = 1.0
TRIALS = 10
SESSIONS = "/home/kali/.codex/sessions/2026/11/14"
SESSION_PATH = f"{SESSIONS}/rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
MIB = 1024 * 1024
STREAMING_BYTES = 2 * MIB  # a copy that has read this much is the session file's copy, streaming
# More than Docker can be ahead of the copy's reads. Docker lets the container's lock go only once it has written the
# whole archive, and it can have written no more than the connection's buffers (a few hundred KiB on a local socket)
# beyond what the copy has read. A call issued while at least this much of the archive was still unread therefore
# reached Docker while it held the lock: its request goes out within milliseconds, in which the copy reads a few MiB.
IN_FLIGHT_MAX_BYTES = 16 * MIB


def _docker() -> Any:
    try:
        import docker

        client = docker.from_env(timeout=10)
        client.ping()
        client.images.get(IMAGE)
        return client
    except Exception:
        return None


pytestmark = pytest.mark.skipif(_docker() is None, reason=f"needs a Docker daemon with {IMAGE} present")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_docker_client", None)  # the repair module's own client, for this daemon
    caplog.set_level(logging.INFO, logger="cairn")


def _padded_session() -> bytes:
    """The pinned codex session file, padded with one more record to exactly COPY_MAX_BYTES."""
    head = fixture_bytes("codex")
    prefix, suffix = b'{"type":"event_msg","payload":{"type":"pad","text":"', b'"}}\n'
    return head + prefix + b"x" * (transcripts.COPY_MAX_BYTES - len(head) - len(prefix) - len(suffix)) + suffix


def _archive(files: dict[str, bytes], links: dict[str, str]) -> bytes:
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name.lstrip("/"))
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for name, target in links.items():
            info = tarfile.TarInfo(name.lstrip("/"))
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return body.getvalue()


@pytest.fixture
def container() -> Iterator[tuple[ContainerManager, str, str, Any]]:
    """A project container of this tree's ContainerManager, holding a COPY_MAX_BYTES session file, a symlink to it and
    a file over the cap."""
    manager = ContainerManager(ContainerConfig(image=IMAGE, network_mode="none", completed_action="remove"))
    project_id = f"p2tc15{os.getpid()}"
    name = manager.ensure_running(project_id)
    client = _docker()
    assert client is not None
    try:
        handle = client.containers.get(name)
        handle.exec_run(["mkdir", "-p", SESSIONS])
        files = {
            SESSION_PATH: _padded_session(),
            "/home/kali/big-70mib.jsonl": fixture_bytes("codex") + b"y" * (70 * 1024 * 1024),
        }
        assert handle.put_archive("/", _archive(files, {f"{SESSIONS}/rollout-link.jsonl": SESSION_PATH}))
        yield manager, project_id, name, client.api
    finally:
        manager.cleanup_completed(project_id)
        manager.close()
        client.close()


class Streams:
    """Marks when the schedule's copy of the session file is streaming, how far its archive stream has been read, and
    when its response was closed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.streaming = threading.Event()
        self.stream: Any = None  # the copy's _GuardedStream, once it has read STREAMING_BYTES
        self.closed_at: float | None = None
        real_read = transcripts._GuardedStream.read
        real_container_read = CopySchedule._container_read

        def read(stream: Any, size: int = -1) -> bytes:
            data = real_read(stream, size)
            if self.stream is None and stream.read_bytes >= STREAMING_BYTES:
                self.stream = stream  # the session file's copy: the marker check's head read stops near 1 MiB
                self.streaming.set()
            return data

        def container_read(schedule: CopySchedule, api: Any, path: str) -> bytes:
            try:
                return real_container_read(schedule, api, path)
            finally:
                self.closed_at = time.monotonic()

        monkeypatch.setattr(transcripts._GuardedStream, "read", read)
        monkeypatch.setattr(CopySchedule, "_container_read", container_read)

    def read_bytes(self) -> int:
        """How far the copy's archive stream has been read."""
        stream = self.stream
        return 0 if stream is None else stream.read_bytes

    def reset(self) -> None:
        self.streaming.clear()
        self.stream = None
        self.closed_at = None


class _Call(threading.Thread):
    """One Docker call on the container, from a thread of its own, issued once go is set: how long it took, and how far
    the copy's archive stream had been read when it was issued."""

    def __init__(self, label: str, call: Callable[[], Any], go: threading.Event, streams: Streams | None) -> None:
        super().__init__(name=f"tc15-{label}", daemon=True)
        self.label = label
        self._call = call
        self._go = go
        self._streams = streams
        self.read_at_issue: int | None = None
        self.seconds: float | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        if not self._go.wait(60):
            return
        if self._streams is not None:
            self.read_at_issue = self._streams.read_bytes()
        started = time.monotonic()
        try:
            self._call()
        except BaseException as exc:
            self.error = exc
        self.seconds = time.monotonic() - started


def _issue_both(name: str, clients: tuple[Any, Any], go: threading.Event, streams: Streams | None) -> list[_Call]:
    """exec_create and inspect on the container named name, each from a thread and a client of its own, both issued
    at the same moment, when go is set: neither waits for the other to return, so while a copy streams each reaches
    Docker while it still holds the lock."""
    exec_api, inspect_api = clients
    calls = [
        _Call("exec_create", lambda: exec_api.exec_create(name, ["true"]), go, streams),
        _Call("inspect", lambda: inspect_api.inspect_container(name), go, streams),
    ]
    for call in calls:
        call.start()
    return calls


def _finished(calls: list[_Call]) -> dict[str, _Call]:
    for call in calls:
        call.join(120)
        assert not call.is_alive() and call.error is None and call.seconds is not None, (call.label, call.error)
    return {call.label: call for call in calls}


def _overlap(call: _Call) -> bool:
    """Whether call was issued while Docker held the container's lock for the copy: with at least IN_FLIGHT_MAX_BYTES
    of the COPY_MAX_BYTES archive still unread, Docker was still writing it (IN_FLIGHT_MAX_BYTES, above)."""
    issued = call.read_at_issue
    return issued is not None and STREAMING_BYTES <= issued <= transcripts.COPY_MAX_BYTES - IN_FLIGHT_MAX_BYTES


def _clients() -> tuple[Any, Any]:
    import docker

    return docker.from_env(timeout=10).api, docker.from_env(timeout=10).api


def _in_thread(function: Any) -> threading.Thread:
    thread = threading.Thread(target=function, name="tc15-copy", daemon=True)
    thread.start()
    return thread


def _report(name: str, value: Any) -> None:
    path = os.environ.get("CAIRN_TC15_REPORT")
    if path:
        report = json.loads(Path(path).read_text()) if Path(path).exists() else {}
        report[name] = value
        Path(path).write_text(json.dumps(report, indent=2))


def _stopping_loop(manager: ContainerManager, futures: dict[Future[str], RunningTask]) -> DispatcherLoop:
    """A scheduler loop as test_scheduler_logic.py builds them, for _cancel_inactive_tasks and
    _queue_container_cleanups."""
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.container_manager = manager
    loop.futures = futures
    loop.cleanup_futures = {}
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop._inactive_cleanup_done = {}
    loop._cleanup_pending = set()
    loop._log_state = {}
    return loop


def _stopped(project_id: str) -> list[ProjectSummary]:
    return [
        ProjectSummary(
            id=project_id,
            title="tc15",
            status="stopped",
            bootstrap_enabled=True,
            created_at="2026-11-14T10:00:00Z",
            fact_count=2,
            intent_count=1,
            working_intent_count=1,
            unclaimed_intent_count=0,
            hint_count=0,
        )
    ]


def _tc15_trials(
    container: tuple[ContainerManager, str, str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC15. On a real daemon: a symlinked session file is refused (and the archive endpoint returns it as a
    symlink); a 70 MiB file is refused; exec_create and inspect on the same container finish within BOUND_SECONDS in
    every one of TRIALS trials while a COPY_MAX_BYTES copy streams, and right after a refused copy. In each trial both
    calls are issued at once, from threads of their own, when the copy has read STREAMING_BYTES of its archive, and
    each must have been issued while Docker held the container's lock for the copy (_overlap), in every trial.
    """
    _manager, project_id, name, _api = container
    streams = Streams(monkeypatch)
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, "codex")
    clients = _clients()
    try:
        _tc15_while_streaming(project_id, name, clients, streams, clock, config, tmp_path)
        _tc15_after_refused(project_id, name, clients, clock, config, tmp_path)
    finally:
        for client in clients:
            client.close()


def _tc15_while_streaming(
    project_id: str,
    name: str,
    clients: tuple[Any, Any],
    streams: Streams,
    clock: FakeClock,
    config: DispatchConfig,
    tmp_path: Path,
) -> None:
    """TC15's trials while a COPY_MAX_BYTES copy streams (_tc15_trials)."""
    timings: dict[str, list[float]] = {"exec_create": [], "inspect": [], "copy_stream": []}
    for label in ("exec_create", "inspect"):
        timings[f"{label}_issued_at_mib"] = []  # how far the copy's archive had been read when the call was issued
        timings[f"{label}_overlap"] = []
    # A copy root's run folder per trial: a fresh run each time, so each trial locates, stats and copies.
    for trial in range(TRIALS):
        streams.reset()
        run = make_run(
            config, project_id=project_id, container=name, session_id=SESSION_IDS["codex"], run_id=f"{trial:032x}"
        )
        schedule = CopySchedule(run, FAKE_TOKEN)
        clock.at(1260)
        calls = _issue_both(name, clients, streams.streaming, streams)  # waiting for the copy to stream
        started = time.monotonic()
        thread = _in_thread(schedule.tick)
        assert streams.streaming.wait(60), "the copy never streamed"
        finished = _finished(calls)
        thread.join(120)
        assert not thread.is_alive()
        assert streams.closed_at is not None
        for label, call in finished.items():
            assert call.seconds is not None and call.read_at_issue is not None
            timings[label].append(round(call.seconds, 4))
            timings[f"{label}_issued_at_mib"].append(round(call.read_at_issue / MIB, 2))
            timings[f"{label}_overlap"].append(1.0 if _overlap(call) else 0.0)
        timings["copy_stream"].append(round(streams.closed_at - started, 4))
        meta = json.loads((tmp_path / "copies" / project_id / f"{trial:032x}" / "meta.json").read_bytes())
        assert (meta["status"], meta["source_bytes"], meta["located_by"]) == (
            "ok",
            transcripts.COPY_MAX_BYTES,
            "session_id",
        )
    _report("while_streaming", timings)
    assert max(timings["exec_create"]) < BOUND_SECONDS, timings
    assert max(timings["inspect"]) < BOUND_SECONDS, timings
    # Each call, in every trial, was issued while Docker held the container's lock for the copy.
    assert sum(timings["exec_create_overlap"]) == TRIALS, timings
    assert sum(timings["inspect_overlap"]) == TRIALS, timings


def _tc15_after_refused(
    project_id: str,
    name: str,
    clients: tuple[Any, Any],
    clock: FakeClock,
    config: DispatchConfig,
    tmp_path: Path,
) -> None:
    """TC15's refused copies (_tc15_trials): a symlink and a file over the cap are refused at the stat, and right
    after each refusal both calls, issued at once, finish within BOUND_SECONDS."""
    low_level = clients[0]
    url = f"{low_level.base_url}/v{low_level.api_version}/containers/{name}/archive"
    response = low_level.get(
        url,
        params={"path": f"{SESSIONS}/rollout-link.jsonl"},
        stream=True,
        timeout=(5, 10),
        headers={"Accept-Encoding": "identity"},  # without it, Docker gzips the archive
    )
    try:
        with tarfile.open(fileobj=response.raw, mode="r|") as archive:
            member = archive.next()
        assert member is not None and member.issym()  # spec 17 A9: a final-component symlink comes back as one
    finally:
        response.close()
    now = threading.Event()
    now.set()
    refused: dict[str, list[float]] = {"exec_create": [], "inspect": []}
    for index, (path, status) in enumerate(
        ((f"{SESSIONS}/rollout-link.jsonl", "not_regular"), ("/home/kali/big-70mib.jsonl", "too_large"))
    ):
        run = make_run(
            config, project_id=project_id, container=name, session_id=SESSION_IDS["codex"], run_id=f"{index + 100:032x}"
        )
        schedule = CopySchedule(run, FAKE_TOKEN)
        schedule._source = transcripts._Source(path, "path")
        for _ in range(TRIALS // 2):
            clock.at(1260)
            schedule.tick()
            for label, call in _finished(_issue_both(name, clients, now, None)).items():
                assert call.seconds is not None
                refused[label].append(round(call.seconds, 4))
        meta = json.loads((tmp_path / "copies" / project_id / f"{index + 100:032x}" / "meta.json").read_bytes())
        assert meta["status"] == status and meta["copies"] == 0
    _report("after_refused", refused)
    assert max(refused["exec_create"]) < BOUND_SECONDS and max(refused["inspect"]) < BOUND_SECONDS, refused


def _tc15_stop_during_copy(
    copy: str,
    container: tuple[ContainerManager, str, str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC15, stop during copy, against this tree's kill path (spec 8.9): a switched-on project is stopped while its
    copy streams, and one scheduler loop iteration (_cancel_inactive_tasks, then _queue_container_cleanups) finishes
    within BOUND_SECONDS. A live copy is cancelled: it closes its stream at once, and its previous good transcript and
    meta.json are unchanged. A final copy, made after the task has returned, is cancelled by nothing: the iteration
    waits only for that copy to end.
    """
    assert copy in ("live", "final")
    manager, project_id, name, _api = container
    streams = Streams(monkeypatch)
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, "codex")
    cancellation = TaskCancellation()
    process = manager.build_exec_process(name, {}, ["sleep", "300"], timeout_seconds=1800)
    process.start()
    cancellation.attach_process(process)
    run = make_run(
        config, project_id=project_id, container=name, session_id=SESSION_IDS["codex"], cancellation=cancellation
    )
    schedule = CopySchedule(run, FAKE_TOKEN)
    clock.at(1260)
    schedule.tick()  # the previous good copy
    folder = tmp_path / "copies" / project_id / run.run_id
    before = {item: (folder / item).read_bytes() for item in ("meta.json", "transcript.jsonl")}
    handle = manager._client.containers.get(name)
    assert handle.exec_run(["touch", SESSION_PATH]).exit_code == 0  # a new modification time: copied again
    futures: dict[Future[str], RunningTask] = {}
    if copy == "live":
        futures[Future()] = RunningTask(project_id, "explore", "w1", cancellation, intent_id="i014")
        operation = schedule.tick
        clock.at(1275)
    else:  # final
        run.exited_monotonic = run.started_monotonic + 1400  # the task returned; _reap_futures dropped it
        operation = schedule.final_copy
        clock.at(1500)
    loop = _stopping_loop(manager, futures)
    streams.reset()
    thread = _in_thread(operation)
    try:
        assert streams.streaming.wait(60), "the copy never streamed"
        summaries = _stopped(project_id)
        started = time.monotonic()
        loop._cancel_inactive_tasks(summaries)
        loop._queue_container_cleanups(summaries)
        iteration = time.monotonic() - started
        thread.join(120)
        for future in list(loop.cleanup_futures):
            future.result(30)
    finally:
        loop.cleanup_executor.shutdown(wait=True)
        process.kill()
        process.communicate(10)
    _report(f"stop_during_{copy}_copy", {"iteration_seconds": round(iteration, 4)})
    assert iteration < BOUND_SECONDS, iteration
    if copy == "live":
        assert cancellation.is_cancelled
        assert {item: (folder / item).read_bytes() for item in before} == before
    else:
        meta = json.loads((folder / "meta.json").read_bytes())
        assert (meta["final"], meta["copies"]) == (True, 2)


@pytest.mark.parametrize("case", ["trials", "stop_during_live_copy", "stop_during_final_copy"])
def test_container_archive_reads_against_real_docker(
    case: str,
    container: tuple[ContainerManager, str, str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC15, case by case: the trials while a copy streams and after refused copies (_tc15_trials), and one scheduler
    loop iteration that stops the project during a live copy or a final copy (_tc15_stop_during_copy)."""
    if case == "trials":
        _tc15_trials(container, tmp_path, monkeypatch)
    else:
        copy = {"stop_during_live_copy": "live", "stop_during_final_copy": "final"}[case]
        _tc15_stop_during_copy(copy, container, tmp_path, monkeypatch)
