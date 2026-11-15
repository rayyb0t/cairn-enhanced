"""run_explore_task_with_repair: the task the scheduler submits for an explore of a project whose Repair switch is on.

It runs today's run_explore_task, unchanged, with the same eight arguments, and returns or raises what that task
returns or raises (spec 8.4). The only difference is the backend it hands that task: for an observed run, an
ObservedBackend that forwards every call to the real backend unchanged and tells the run's ShadowRun when the
execute process was built, started and finished. The hooks only store values; every request and copy is made on
the run's own reporter thread (repair/shadow.py), and no hook can raise into the run.

Rules for the pass-throughs (spec 8.4), each enforced by a test:
- B1 Forward exactly. write_text_file and build_exec_process take *args, **kwargs and pass them on unchanged;
  ensure_running and container_name forward their argument; __getattr__ forwards everything else. Return values and
  exceptions pass through. kill_after_seconds is never added.
- B2 Only the first build_exec_process is observed. In run_explore_task the first call is always the execute; a
  second one is the conclude fallback, and its real process is returned untouched.
- B3 The path of a write_text_file call before the first build is kept as the run's prompt marker: the execute's
  graph snapshot path, which the explore prompt holds verbatim and which is unique per run.
- B4 ObservedProcess: start() calls the real start(), then the hook, so a start() that raises propagates and
  nothing is registered; communicate(t) calls the real communicate(t), then the hook, and returns the same
  ProcessResult; kill() and cancel(reason) forward, as the heartbeat lease and the cancellation call them.
- B5 Hooks never break the run. Every hook goes through _quietly; if one fails, the run carries on unobserved.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair.config import cached_state
from cairn.dispatcher.repair.shadow import ShadowRun
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_explore_task_with_repair(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ExecutionBackend,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Today's explore task, unchanged, with its run reported for the repair agent (Phase 2, shadow mode)."""
    run = _new_shadow_run(config, container_manager, project, intent, worker, cancellation)  # no I/O; never raises
    backend = container_manager if run is None else ObservedBackend(container_manager, run)
    outcome: str | None = None
    try:
        # run_explore_task declares ContainerManager but takes either backend, as local mode already shows; cast()
        # only quiets the type checker and passes the object through unchanged.
        outcome = run_explore_task(
            config, client, cast(ContainerManager, backend), project, export_yaml, intent, worker, cancellation
        )
        return outcome
    finally:
        if run is not None:
            _quietly(run, "task_finished", run.task_finished, outcome)


def _new_shadow_run(
    config: DispatchConfig,
    container_manager: ExecutionBackend,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> ShadowRun | None:
    """The run's ShadowRun, or None when runs are not reported. No I/O, and never raises.

    None when the cached state says "not configured": no repair: section in dispatch.yaml, or a token that a thread
    has already found bad (spec 8.3). While no thread has read the token yet, the run is observed, and its reporter
    thread reads the token first.
    """
    try:
        state = cached_state(config)
        if state is not None and not state.configured:
            return None
        execution = config.runtime.execution
        return ShadowRun(
            run_id=uuid.uuid4().hex,
            config=config,
            project_id=project.project.id,
            intent_id=intent.id,
            worker=worker,
            execution=execution,
            driver=get_driver(worker.type, execution),
            backend=container_manager,
            cancellation=cancellation,
        )
    except Exception as exc:
        LOG.warning(
            "repair shadow hook failed project=%s intent=%s worker=%s run=%s hook=%s error=%s",
            getattr(getattr(project, "project", None), "id", "-"),
            getattr(intent, "id", "-"),
            getattr(worker, "name", "-"),
            "-",
            "setup",
            type(exc).__name__,
        )
        return None


def _quietly(run: ShadowRun, hook: str, function: Callable[..., Any], *args: Any) -> Any:
    """function(*args); an Exception is logged with its class name only, and None is returned (B5)."""
    try:
        return function(*args)
    except Exception as exc:
        LOG.warning(
            "repair shadow hook failed project=%s intent=%s worker=%s run=%s hook=%s error=%s",
            run.project_id,
            run.intent_id,
            run.worker_name,
            run.run_id,
            hook,
            type(exc).__name__,
        )
        return None


def _argument(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


class ObservedBackend:
    """The run's backend, unchanged: every call goes to the real backend with the same arguments (B1).

    One instance per run, so that two runs of one project never share it. Only the first build_exec_process call,
    the execute, is observed (B2); the path written before it is the run's prompt marker (B3).
    """

    def __init__(self, inner: ExecutionBackend, run: ShadowRun) -> None:
        self._inner = inner
        self._run = run
        self._builds = 0

    def container_name(self, project_id: str) -> str:
        return self._inner.container_name(project_id)

    def ensure_running(self, project_id: str) -> str:
        return self._inner.ensure_running(project_id)

    def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.write_text_file(*args, **kwargs)
        if self._builds == 0:
            _quietly(self._run, "note_prompt_file", self._run.note_prompt_file, _argument(args, kwargs, 1, "path"))
        return result

    def build_exec_process(self, *args: Any, **kwargs: Any) -> Any:
        self._builds += 1
        process = self._inner.build_exec_process(*args, **kwargs)
        if self._builds != 1:
            return process
        observed = _quietly(self._run, "note_execute", self._observe, process, args, kwargs)
        return process if observed is None else observed

    def _observe(self, process: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> ObservedProcess | None:
        noted = self._run.note_execute(
            _argument(args, kwargs, 0, "container_name"),
            _argument(args, kwargs, 2, "command"),
            _argument(args, kwargs, 3, "timeout_seconds"),
        )
        return ObservedProcess(process, self._run) if noted else None

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


class ObservedProcess:
    """The run's execute process, unchanged; it only tells the ShadowRun when it started and finished (B4)."""

    def __init__(self, inner: Any, run: ShadowRun) -> None:
        self._inner = inner
        self._run = run

    def start(self) -> None:
        result = self._inner.start()
        _quietly(self._run, "execute_started", self._run.execute_started, self._inner)
        return result

    def communicate(self, timeout: float | None) -> ProcessResult:
        result = self._inner.communicate(timeout)
        _quietly(self._run, "execute_finished", self._run.execute_finished, result)
        return result

    def kill(self) -> None:
        return self._inner.kill()

    def cancel(self, reason: str) -> None:
        return self._inner.cancel(reason)

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)
