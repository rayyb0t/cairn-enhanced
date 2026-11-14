"""ManagedProcess.kill()/cancel() against a real Docker daemon.

Skipped unless Docker answers, the test image is already present locally (nothing is pulled) and
its `timeout` is GNU or uutils coreutils. Image: CAIRN_KILL_TEST_IMAGE, default debian:bookworm-slim (no ps,
no python, no kill binary); run it once more with your worker image. Every container is labelled
cairn.test=container-kill, has no network, and is removed afterwards.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import docker
import pytest
import requests
from docker.errors import DockerException

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ManagedProcess

LOG = logging.getLogger(__name__)  # timings, shown with `pytest -rP --log-level=INFO`
IMAGE = os.environ.get("CAIRN_KILL_TEST_IMAGE", "debian:bookworm-slim")
LABEL = {"cairn.test": "container-kill"}
LISTER = "cairn-killtest-lister"
LIST_SCRIPT = r"""for d in /proc/[0-9]*; do
  read -r s 2>/dev/null < "$d/stat" || continue
  set -- ${s##*) }
  c=$(tr '\000\n' '  ' < "$d/cmdline" 2>/dev/null)
  [ -n "$c" ] && printf '%s\t%s\t%s\t%s\t%s\n' "${d#/proc/}" "$2" "$3" "${20}" "$c"
done
exit 0
"""


@dataclass
class Proc:
    pid: int
    ppid: int
    pgrp: int
    start: int
    cmdline: str


@pytest.fixture
def env() -> Iterator[tuple[ContainerManager, str, Any]]:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
    except (DockerException, requests.RequestException) as exc:  # no usable Docker here
        pytest.skip(f"Docker or image {IMAGE} not available: {exc}")
    name = f"cairn-killtest-{uuid.uuid4().hex[:8]}"
    container: Any = client.containers.run(IMAGE, ["sleep", "infinity"], detach=True, name=name, network_mode="none", labels=LABEL)
    try:
        version = container.exec_run(["timeout", "--version"]).output.decode("utf-8", "replace")
        if "GNU coreutils" not in version and "uutils coreutils" not in version:
            # e.g. BusyBox's timeout, which replaces itself with the command
            pytest.skip(f"{IMAGE}: timeout is neither GNU nor uutils coreutils, so no exec root may carry the wrapper's command line")
        manager = ContainerManager.__new__(ContainerManager)
        manager._config = ContainerConfig(image=IMAGE, network_mode="none", completed_action="remove")
        manager._client = client
        manager._ensure_running_locks = {}
        manager._ensure_running_locks_guard = threading.Lock()
        yield manager, name, container
    finally:
        container.remove(force=True)
        client.close()


def agent_argv(tag: str, base: int) -> list[str]:
    """An agent stand-in: a plain child, a SIGTERM-ignoring child and a setsid child (which keeps the output open)."""
    script = f"sleep {base + 1} &\n( trap '' TERM; exec sleep {base + 2} ) &\nsetsid sleep {base + 3} &\necho started\nwait\n"
    return ["sh", "-c", script, tag]


def listing(container: Any) -> list[Proc]:
    result = container.exec_run(["sh", "-c", LIST_SCRIPT, LISTER], stdout=True, stderr=False)
    procs = []
    for line in result.output.decode("utf-8", "replace").splitlines():
        pid, ppid, pgrp, start, cmdline = line.split("\t", 4)
        if LISTER not in cmdline:
            procs.append(Proc(int(pid), int(ppid), int(pgrp), int(start), cmdline.strip()))
    return procs


def wait_for(container: Any, needles: list[str], timeout: float = 10) -> list[Proc]:
    deadline = time.monotonic() + timeout
    while True:
        procs = listing(container)
        if all(any(needle in p.cmdline for p in procs) for needle in needles) or time.monotonic() > deadline:
            return procs
        time.sleep(0.1)


def start(manager: ContainerManager, name: str, argv: list[str], timeout_s: int = 120) -> ManagedProcess:
    process = manager.build_exec_process(name, {}, argv, timeout_seconds=timeout_s)
    process.start()
    return process


def running(process: ManagedProcess) -> bool:
    assert process._exec_id is not None
    return bool(process._api.exec_inspect(process._exec_id)["Running"])


def helper_execs(container: Any, since: float) -> list[str]:
    """exec_create events of kill helpers in this container since `since` (events have 1 s resolution)."""
    until = int(time.time()) + 1
    time.sleep(max(0.0, until - time.time()))
    events = container.client.events(since=int(since), until=until, filters={"container": container.id}, decode=True)
    return [str(e.get("Action", "")) for e in events if str(e.get("Action", "")).startswith("exec_create") and "cairn-kill" in str(e.get("Action", ""))]


def test_kill_stops_this_run_and_nothing_else(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    a = start(manager, name, agent_argv("kt-a é ✓", 3100))  # non-ASCII: the hash is over UTF-8
    b = start(manager, name, agent_argv("kt-b", 3200))
    wait_for(container, ["sleep 3102", "sleep 3103", "sleep 3202", "sleep 3203"])

    began = time.monotonic()
    a.kill()
    kill_s = time.monotonic() - began
    result = a.communicate(timeout=10)
    ended_s = time.monotonic() - began

    procs = listing(container)
    left = sorted(p.cmdline for p in procs if "kt-a" in p.cmdline or p.cmdline.startswith("sleep 31"))
    LOG.info("kill() %.3fs, run ended %.2fs after kill(), left of a: %s", kill_s, ended_s, left)
    assert kill_s < 1.0  # not held by Docker's 2 s wait for the output the setsid child keeps open
    assert ended_s < 5
    assert (result.returncode, result.stdout, result.timed_out, result.cancelled) == (137, "started\n", False, False)
    assert left == ["sleep 3103"]  # only the setsid child, which the wrapper's own deadline leaves too
    assert running(b)
    assert {"sleep 3201", "sleep 3202", "sleep 3203"} <= {p.cmdline for p in procs}
    b.cancel("stopped")
    assert b.communicate(timeout=10).returncode == 137


def test_kill_is_refused_while_an_identical_run_is_alive(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    argv = agent_argv("kt-twin", 3300)
    first = start(manager, name, argv)
    wait_for(container, ["sleep 3302"])
    second = start(manager, name, argv)
    deadline = time.monotonic() + 10
    roots: list[Proc] = []
    while len(roots) < 2 and time.monotonic() < deadline:
        roots = sorted((p for p in listing(container) if p.ppid == 0 and "kt-twin" in p.cmdline), key=lambda p: p.start)
        time.sleep(0.1)
    assert len(roots) == 2

    first.kill()
    second.cancel("stopped")
    time.sleep(0.5)

    assert running(first) and running(second)
    assert len([p for p in listing(container) if p.ppid == 0 and "kt-twin" in p.cmdline]) == 2
    container.exec_run(["sh", "-c", f"kill -KILL -{roots[1].pid}"])  # end the second run from outside
    second.communicate(timeout=10)
    first.kill()  # no twin left: now it is killed
    assert first.communicate(timeout=10).returncode == 137
    assert not [p for p in listing(container) if p.pgrp == roots[0].pid]


def test_cancel_right_after_start_ends_the_run(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    cancellation = TaskCancellation()
    cancellation.cancel("deleted")
    process = manager.build_exec_process(name, {}, agent_argv("kt-early", 3500), timeout_seconds=120)

    began = time.monotonic()
    process.start()
    cancellation.attach_process(process)  # exactly what run_worker_process does
    result = process.communicate(timeout=10)
    ended_s = time.monotonic() - began
    time.sleep(0.5)

    left = [p.cmdline for p in listing(container) if "kt-early" in p.cmdline or p.cmdline.startswith("sleep 350")]
    LOG.info("cancel right after start: ended after %.2fs, exec started: %s, left: %s", ended_s, process._exec_started, left)
    assert ended_s < 5
    assert (result.returncode, result.cancelled, result.cancel_reason) == (137, True, "deleted")
    assert left in ([], ["sleep 3503"])


def test_kill_before_the_reader_starts_the_exec_never_runs_it(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    process = manager.build_exec_process(name, {}, agent_argv("kt-never", 3700), timeout_seconds=120)
    original_start = threading.Thread.start
    held: list[threading.Thread] = []
    threading.Thread.start = lambda thread: held.append(thread)  # type: ignore[method-assign,assignment]
    try:
        process.start()  # exec_create only; the reader thread is held
    finally:
        threading.Thread.start = original_start  # type: ignore[method-assign]
    process.cancel("paused")
    original_start(held[0])
    result = process.communicate(timeout=10)

    assert process._exec_id is not None
    state = process._api.exec_inspect(process._exec_id)
    assert (result.returncode, result.cancelled) == (137, True)
    assert (state["Running"], state["ExitCode"], state["Pid"]) == (False, None, 0)  # created, never started
    assert not [p for p in listing(container) if "kt-never" in p.cmdline or p.cmdline.startswith("sleep 370")]


def test_communicate_timeout_ends_the_run(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    process = start(manager, name, agent_argv("kt-slow", 3600))
    wait_for(container, ["sleep 3602"])

    began = time.monotonic()
    result = process.communicate(timeout=1)
    ended_s = time.monotonic() - began

    left = [p.cmdline for p in listing(container) if "kt-slow" in p.cmdline or p.cmdline.startswith("sleep 360")]
    assert (result.returncode, result.timed_out) == (137, True)
    assert ended_s < 4
    assert left == ["sleep 3603"]


def test_normal_run_is_unchanged_and_a_late_kill_runs_no_helper(env: tuple[ContainerManager, str, Any]) -> None:
    manager, name, container = env
    since = time.time()
    process = start(manager, name, ["sh", "-c", "echo out; echo err >&2; exit 3"], timeout_s=60)
    result = process.communicate(timeout=30)

    process.kill()
    process.cancel("stopped")

    assert (result.returncode, result.stdout, result.stderr, result.timed_out, result.cancelled) == (3, "out\n", "err\n", False, False)
    assert helper_execs(container, since) == []
