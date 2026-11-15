"""Dispatcher tests of spec 16 for touch point (a) in scheduler/loop.py: DS1, DS2, DS3, DS4 and DS30.

The scheduler picks the explore task by the project's Repair switch (_explore_task_for), and DispatcherLoop.run
starts the repair side once, only when dispatch.yaml has a repair: section (_start_repair_side). The fresh-interpreter
checks run the real DispatcherLoop in a subprocess against a fake Cairn server, in local mode with a mock worker, and
replace only its task executor, so that the explore it dispatches is recorded instead of run. Every value here is
synthetic.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import make_intent, make_project
from repair_dispatcher_runs import make_token_file, run_project
from repair_dispatcher_support import (
    RecordedRequest,
    Reply,
    bypass_proxies,
    fake_server,
    json_reply,
    repair_section,
    reset_repair_process_state,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.repair import housekeeping
from cairn.dispatcher.repair.explore import run_explore_task_with_repair
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler import loop as loop_module
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.server.models import ProjectDetail, ProjectMeta

CAIRN_DIR = Path(__file__).resolve().parents[1]
# The mock worker's local binary is python3 (checked on PATH at start in local mode): this interpreter's folder first.
INTERPRETER_PATH = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
UNAVAILABLE = "repair module unavailable, running today's explore task project=%s"
HOUSEKEEPING_FAILED = "repair shadow housekeeping failed step=%s error=%s"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(loop_module, "_repair_module_failed", False)
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future[str]:
        self.calls.append((fn, args, kwargs))
        return Future()

    def shutdown(self, wait: bool = True) -> None:
        return None


class ClaimClient:
    def heartbeat(self, *_args: Any) -> ApiResult:
        return ApiResult(200, {})


def loop_config(**repair: Any) -> DispatchConfig:
    payload: dict[str, Any] = {
        "server": "http://127.0.0.1:9",
        "runtime": {
            "interval": 60,
            "max_workers": 2,
            "max_running_projects": 1,
            "max_project_workers": 2,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 3},
            "explore": {"timeout": 1800, "conclude_timeout": 300},
        },
        "container": {"image": "test-image", "network_mode": "host", "completed_action": "stop"},
        "workers": [{"name": "w1", "type": "mock", "task_types": ["explore"], "max_running": 2, "priority": 0}],
    }
    if repair:
        payload["repair"] = repair_section(**repair)
    return DispatchConfig.model_validate(payload)


def dispatch_loop() -> DispatcherLoop:
    """A loop built as test_scheduler_logic.py builds them, with __new__, ready for _dispatch_explore."""
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = loop_config()
    loop.client = ClaimClient()  # type: ignore[assignment]
    loop.container_manager = object()  # type: ignore[assignment]
    loop.executor = RecordingExecutor()  # type: ignore[assignment]
    loop.futures = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    return loop


def unclaimed(intent_id: str = "i001") -> Any:
    intent = make_intent(intent_id)
    intent.worker = None
    return intent


class ImportSpy:
    """Records every absolute import of a module under cairn.dispatcher.repair."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: list[str] = []
        real = builtins.__import__

        def spy(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
            if level == 0 and name.startswith("cairn.dispatcher.repair"):
                self.names.append(name)
            return real(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", spy)


# DS1 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("repair", [False, True])
def test_scheduler_submits_todays_arguments_and_picks_the_task_by_the_switch(repair: bool) -> None:
    """DS1. The scheduler submits today's run_explore_task with the switch off and run_explore_task_with_repair with
    it on, each with the same 8 arguments, the very objects of today's call, and the same RunningTask.
    """
    loop = dispatch_loop()
    intent = unclaimed()
    project = run_project(intent, repair=repair)

    assert loop._dispatch_explore(project, "graph-yaml", intent) is True

    [(fn, args, kwargs)] = loop.executor.calls  # type: ignore[attr-defined]
    assert fn is (run_explore_task_with_repair if repair else run_explore_task)
    assert kwargs == {}
    assert len(args) == 8
    expected = (loop.config, loop.client, loop.container_manager, project, "graph-yaml", intent, loop.config.workers[0])
    assert all(actual is wanted for actual, wanted in zip(args[:7], expected, strict=True))
    assert type(args[7]) is TaskCancellation and not args[7].is_cancelled
    [(_future, task)] = loop.futures.items()
    assert task == RunningTask(project.project.id, "explore", "w1", args[7], intent_id=intent.id)


# DS2 -----------------------------------------------------------------------------------------------------------------

# Run in a fresh interpreter: DispatcherLoop(<dispatch.yaml>) and run(once=True), with the task executor replaced by a
# recorder. Prints, as JSON, the repair modules loaded, the threads started, the tasks submitted, the backend and
# CairnClient calls, and every log record.
LOOP_SCRIPT = textwrap.dedent(
    """
    import json, logging, sys, threading
    from concurrent.futures import Future
    from pathlib import Path

    started = []
    real_start = threading.Thread.start

    def start(self):
        started.append([self.name, self.daemon])
        return real_start(self)

    threading.Thread.start = start
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append([record.name, record.levelno, record.getMessage()])

    logging.getLogger().addHandler(Collect())
    logging.getLogger().setLevel(logging.DEBUG)

    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.scheduler.loop import DispatcherLoop

    calls = []

    def record(cls, label):
        for name in [n for n in vars(cls) if not n.startswith("_") and callable(getattr(cls, n))]:
            real = getattr(cls, name)

            def wrapper(self, *args, _real=real, _name=name, **kwargs):
                calls.append([label, _name, repr(args), repr(sorted(kwargs.items()))])
                return _real(self, *args, **kwargs)

            setattr(cls, name, wrapper)

    record(CairnClient, "client")
    record(LocalBackend, "backend")

    class Executor:
        submitted = []

        def submit(self, fn, *args, **kwargs):
            self.submitted.append([fn.__module__ + "." + fn.__qualname__, [type(a).__name__ for a in args]])
            return Future()

        def shutdown(self, wait=True):
            return None

    loaded_before = sorted(n for n in sys.modules if n.startswith("cairn.dispatcher.repair"))
    loop = DispatcherLoop(Path(sys.argv[1]))
    loop.executor = Executor()
    loop.run(once=True)
    print("RESULT " + json.dumps({
        "loaded_before": loaded_before,
        "loaded": sorted(n for n in sys.modules if n.startswith("cairn.dispatcher.repair")),
        "threads": started,
        "submitted": Executor.submitted,
        "calls": calls,
        "records": records,
    }))
    """
)


def cairn_responder(projects: dict[str, ProjectDetail]) -> Any:
    """A fake Cairn server: settings, the project list and details, the export, and the explore claim."""

    def summary(detail: ProjectDetail) -> dict[str, Any]:
        unclaimed_count = sum(1 for intent in detail.intents if intent.to is None and intent.worker is None)
        return {
            **detail.project.model_dump(mode="json"),
            "repair_enabled": False,
            "fact_count": len(detail.facts),
            "intent_count": len(detail.intents),
            "working_intent_count": len(detail.intents) - unclaimed_count,
            "unclaimed_intent_count": unclaimed_count,
            "hint_count": len(detail.hints),
        }

    def respond(request: RecordedRequest) -> Reply:
        path = request.path.split("?", 1)[0]
        if path == "/settings":
            return json_reply(200, {"intent_timeout": 300, "reason_timeout": 300})
        if path == "/projects":
            return json_reply(200, [summary(detail) for detail in projects.values()])
        for project_id, detail in projects.items():
            body = detail.model_dump(mode="json", by_alias=True)
            body["project"]["repair_enabled"] = False
            if path == f"/projects/{project_id}":
                return json_reply(200, body)
            if path == f"/projects/{project_id}/export":
                return Reply(200, b"facts: []\n", "text/plain")
            if path.startswith(f"/projects/{project_id}/intents/") and path.endswith("/heartbeat"):
                return json_reply(200, body["intents"][0])
        return json_reply(404, {"detail": "Not Found"})

    return respond


def dispatch_yaml(tmp_path: Path, server: str, *, repair: str) -> Path:
    """A local-mode dispatch.yaml with one mock explore worker; repair is "none", "empty" (repair: with no value)
    or "section".
    """
    payload: dict[str, Any] = {
        "server": server,
        "runtime": {
            "execution": "local",
            "interval": 5,
            "max_workers": 2,
            "max_running_projects": 1,
            "max_project_workers": 2,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "local": {"workspace_root": str(tmp_path / "work")},
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 3},
            "explore": {"timeout": 1800, "conclude_timeout": 300},
        },
        "workers": [{"name": "mock-1", "type": "mock", "task_types": ["explore"], "max_running": 1, "priority": 0}],
    }
    text = yaml.safe_dump(payload, sort_keys=False)
    if repair == "empty":
        text += "repair:\n"
    elif repair == "section":
        token = make_token_file(tmp_path)
        section = repair_section(token_path=str(token), transcript_root=str(tmp_path / "copies"))
        text += yaml.safe_dump({"repair": section}, sort_keys=False)
    path = tmp_path / f"dispatch-{repair}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def run_dispatcher_once(tmp_path: Path, repair: str) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]]:
    """The real DispatcherLoop's run(once=True) in a fresh interpreter, and the requests the fake server got."""
    workdir = tmp_path / repair
    workdir.mkdir()
    home = workdir / "home"
    home.mkdir()
    project = make_project(intents=[unclaimed()])
    with fake_server(cairn_responder({"proj_001": project})) as server:
        config_path = dispatch_yaml(workdir, server.base_url, repair=repair)
        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": INTERPRETER_PATH,
            "PYTHONPATH": str(CAIRN_DIR / "src"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        before = tree(workdir)
        result = subprocess.run(
            [sys.executable, "-c", LOOP_SCRIPT, str(config_path)],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        if repair != "section":
            assert tree(workdir) == before  # no file or folder created
        requests_seen = [(request.method, request.path, request.body) for request in server.requests]
    [line] = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    return json.loads(line.removeprefix("RESULT ")), requests_seen


def outside_repair(records: list[list[Any]]) -> list[list[Any]]:
    """The records of every logger but cairn.dispatcher.repair*, with the fake server's port set aside."""
    return [
        [name, level, re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", message)]
        for name, level, message in records
        if not name.startswith("cairn.dispatcher.repair")
    ]


def test_switch_off_never_imports_the_repair_package(tmp_path: Path) -> None:
    """DS2 (fresh interpreter). Without a repair: section, DispatcherLoop(...) and run(once=True) with every switch
    off import nothing under cairn.dispatcher.repair, start no thread and create no file or folder. With the section,
    the same run imports cairn.dispatcher.repair.housekeeping, starts exactly one thread, repair-housekeeping, and makes
    the same CairnClient and backend calls, sends the same requests, submits the same tasks and logs the same records
    outside cairn.dispatcher.repair* as the run without it.
    """
    without, without_requests = run_dispatcher_once(tmp_path, "none")
    assert without["loaded_before"] == [] and without["loaded"] == []
    assert without["threads"] == []
    assert without["submitted"] == [
        [
            "cairn.dispatcher.tasks.explore.run_explore_task",
            [
                "DispatchConfig",
                "CairnClient",
                "LocalBackend",
                "ProjectDetail",
                "str",
                "Intent",
                "WorkerConfig",
                "TaskCancellation",
            ],
        ]
    ]
    assert ("POST", "/projects/proj_001/intents/i001/heartbeat") in [(m, p) for m, p, _ in without_requests]

    with_section, with_requests = run_dispatcher_once(tmp_path, "section")
    assert with_section["loaded_before"] == []
    assert "cairn.dispatcher.repair.housekeeping" in with_section["loaded"]
    assert "cairn.dispatcher.repair.explore" not in with_section["loaded"]  # every switch is off
    assert with_section["threads"] == [["repair-housekeeping", True]]
    assert with_section["submitted"] == without["submitted"]
    assert with_section["calls"] == without["calls"]
    assert with_requests == without_requests
    assert outside_repair(with_section["records"]) == outside_repair(without["records"])
    assert [record for record in without["records"] if "repair" in record[2]] == []


# DS3 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 1, "yes", 2, "shadow"])
def test_switch_read_is_strict_about_true(value: Any, tmp_path: Path) -> None:
    """DS3. Stand-ins without the attribute or with a truthy non-bool get today's task; a GET /projects payload in
    which one project's repair_enabled is null, 1, "yes", 2 or "shadow" parses (CairnClient: TypeAdapter of
    list[ProjectSummary], ProjectDetail.model_validate), reads off and gets today's task, and the other projects in
    the payload are unaffected.
    """
    for stand_in in (SimpleNamespace(id="proj_001"), SimpleNamespace(id="proj_001", repair_enabled=value)):
        assert loop_module._explore_task_for(SimpleNamespace(project=stand_in)) is run_explore_task  # type: ignore[arg-type]
    for truthy in ("true", "True", [True], 1.0, object()):
        stand_in = SimpleNamespace(id="proj_001", repair_enabled=truthy)
        assert loop_module._explore_task_for(SimpleNamespace(project=stand_in)) is run_explore_task  # type: ignore[arg-type]

    projects = {
        "proj_001": make_project(intents=[unclaimed()]),
        "proj_002": make_project(intents=[unclaimed()]),
        "proj_003": make_project(intents=[unclaimed()]),
    }
    for project_id, detail in projects.items():
        detail.project.id = project_id
    switch = {"proj_001": value, "proj_002": True}  # proj_003 sends no field, as an older server does

    def respond(request: RecordedRequest) -> Reply:
        path = request.path.split("?", 1)[0]
        if path == "/projects":
            items = []
            for project_id, detail in projects.items():
                item = {
                    **detail.project.model_dump(mode="json"),
                    "fact_count": 3,
                    "intent_count": 1,
                    "working_intent_count": 0,
                    "unclaimed_intent_count": 1,
                    "hint_count": 1,
                }
                if project_id in switch:
                    item["repair_enabled"] = switch[project_id]
                items.append(item)
            return json_reply(200, items)
        project_id = path.removeprefix("/projects/")
        body = projects[project_id].model_dump(mode="json", by_alias=True)
        if project_id in switch:
            body["project"]["repair_enabled"] = switch[project_id]
        return json_reply(200, body)

    has_field = "repair_enabled" in ProjectMeta.model_fields
    with fake_server(respond) as server:
        client = CairnClient(server.base_url)
        try:
            summaries = client.list_projects()
            details = {project_id: client.get_project(project_id) for project_id in projects}
        finally:
            client.close()
    assert [summary.id for summary in summaries] == ["proj_001", "proj_002", "proj_003"]
    for summary in summaries:
        expected_on = has_field and summary.id == "proj_002"  # only a JSON true reads on (spec 6.8)
        assert getattr(summary, "repair_enabled", False) is expected_on
    for project_id, detail in details.items():
        expected_on = has_field and project_id == "proj_002"
        assert getattr(detail.project, "repair_enabled", False) is expected_on
        expected_task = run_explore_task_with_repair if expected_on else run_explore_task
        assert loop_module._explore_task_for(detail) is expected_task


# DS4 -----------------------------------------------------------------------------------------------------------------


def test_scheduler_falls_back_once_when_the_repair_module_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DS4. When cairn.dispatcher.repair.explore cannot be loaded, a switched-on project gets today's task, logged
    once per process; also when the import fails first in _start_repair_side: one ERROR with project=-, no
    housekeeping thread, and a switched-on project dispatched afterwards gets today's task with no second import
    attempt.
    """
    imports = ImportSpy(monkeypatch)
    monkeypatch.setitem(sys.modules, "cairn.dispatcher.repair.explore", None)  # the import raises ImportError
    loop = dispatch_loop()
    for intent_id in ("i001", "i002"):
        intent = unclaimed(intent_id)
        assert loop._dispatch_explore(run_project(intent), "graph-yaml", intent) is True
    assert [fn for fn, _args, _kwargs in loop.executor.calls] == [run_explore_task, run_explore_task]  # type: ignore[attr-defined]
    errors = [record for record in caplog.records if record.msg == UNAVAILABLE]
    assert [(record.levelno, record.args) for record in errors] == [(logging.ERROR, ("proj_001",))]
    assert errors[0].exc_info is not None
    assert imports.names == ["cairn.dispatcher.repair.explore"]  # the second dispatch tried no import

    monkeypatch.setattr(loop_module, "_repair_module_failed", False)
    monkeypatch.setitem(sys.modules, "cairn.dispatcher.repair.housekeeping", None)
    monkeypatch.delitem(sys.modules, "cairn.dispatcher.repair.explore")
    imports.names.clear()
    caplog.clear()
    threads_before = set(threading.enumerate())
    config = loop_config(token_path=str(make_token_file(tmp_path)), transcript_root=str(tmp_path / "copies"))
    loop_module._start_repair_side(config)
    loop_module._start_repair_side(config)
    errors = [record for record in caplog.records if record.msg == UNAVAILABLE]
    assert [(record.levelno, record.args) for record in errors] == [(logging.ERROR, ("-",))]
    assert imports.names == ["cairn.dispatcher.repair.housekeeping"]
    assert not [
        thread for thread in set(threading.enumerate()) - threads_before if thread.name == "repair-housekeeping"
    ]
    loop = dispatch_loop()
    intent = unclaimed()
    assert loop._dispatch_explore(run_project(intent), "graph-yaml", intent) is True
    [(fn, _args, _kwargs)] = loop.executor.calls  # type: ignore[attr-defined]
    assert fn is run_explore_task
    assert imports.names == ["cairn.dispatcher.repair.housekeeping"]  # no import of the explore module
    assert len([record for record in caplog.records if record.msg == UNAVAILABLE]) == 1


# DS30 ----------------------------------------------------------------------------------------------------------------


class StopAfter:
    """housekeeping._STOP for a test: wait() advances a fake clock by its timeout and ends the thread after n passes."""

    def __init__(self, passes: int) -> None:
        self.passes = passes
        self.now = 0.0
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        self.now += float(timeout or 0.0)
        return len(self.waits) >= self.passes


def section_config(tmp_path: Path) -> DispatchConfig:
    return loop_config(token_path=str(make_token_file(tmp_path)), transcript_root=str(tmp_path / "copies"))


def in_process_loop(config_path: Path) -> tuple[DispatcherLoop, RecordingExecutor, list[tuple[Any, ...]]]:
    """The real DispatcherLoop of config_path, with its executor replaced by a recorder and its client and backend
    calls recorded."""
    loop = DispatcherLoop(config_path)
    executor = RecordingExecutor()
    loop.executor = executor  # type: ignore[assignment]
    calls: list[tuple[Any, ...]] = []
    for label, target in (("client", loop.client), ("backend", loop.container_manager)):
        for name in [name for name in dir(type(target)) if not name.startswith("_")]:
            real = getattr(target, name)
            if not callable(real):
                continue

            def wrapper(*args: Any, _real: Any = real, _name: str = name, _label: str = label, **kwargs: Any) -> Any:
                calls.append((_label, _name, repr(args), repr(sorted(kwargs.items()))))
                return _real(*args, **kwargs)

            setattr(target, name, wrapper)
    return loop, executor, calls


ASPECTS = (
    "once_before_the_healthchecks",
    "nothing_without_the_section",
    "start_raises",
    "startup_healthcheck_only",
    "one_daemon_thread",
    "thread_cannot_start",
    "steps_and_passes_are_guarded",
    "passes_every_60_seconds",
)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_repair_side_starts_once_and_only_with_the_section(
    aspect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DS30. run() calls _start_repair_side once, before the start-up health checks; without repair:, or with a
    repair: key that has no value, it imports nothing and starts nothing; an exception that start_repair_side raises is
    logged once as spec 8.1's ERROR with project=-, and run() goes on, with today's explore task for every project;
    cairn dispatch --startup-healthcheck-only starts nothing. With the section: one repair-housekeeping daemon thread;
    a thread that cannot start logs step=start once, and run(once=True) makes the same scheduler calls as without the
    section; an exception in each step and in a pass is logged once per process and step, and the later steps and
    passes still run; on a fake clock, passes run at start and then every 60 s.
    """
    if aspect == "once_before_the_healthchecks":
        order: list[Any] = []
        project = make_project(intents=[unclaimed()])
        with fake_server(cairn_responder({"proj_001": project})) as server:
            loop = DispatcherLoop(dispatch_yaml(tmp_path, server.base_url, repair="section"))
            loop.executor = RecordingExecutor()  # type: ignore[assignment]
            monkeypatch.setattr(loop_module, "_start_repair_side", lambda config: order.append(("repair", config)))
            real_checks = loop.run_startup_healthchecks
            monkeypatch.setattr(loop, "run_startup_healthchecks", lambda: order.append("checks") or real_checks())
            loop.run(once=True)
        assert order == [("repair", loop.config), "checks"]
        assert order[0][1] is loop.config

    elif aspect == "nothing_without_the_section":
        without, without_requests = run_dispatcher_once(tmp_path, "none")
        empty, empty_requests = run_dispatcher_once(tmp_path, "empty")
        for result in (without, empty):
            assert result["loaded"] == [] and result["threads"] == []
        assert empty["calls"] == without["calls"] and empty["submitted"] == without["submitted"]
        assert empty_requests == without_requests

    elif aspect == "start_raises":
        monkeypatch.setattr(housekeeping, "start_repair_side", _raiser)
        project = make_project(intents=[unclaimed()])
        with fake_server(cairn_responder({"proj_001": project})) as server:
            loop, executor, _calls = in_process_loop(dispatch_yaml(tmp_path, server.base_url, repair="section"))
            loop.run(once=True)  # goes on: the dispatch below happened
            loop_module._start_repair_side(loop.config)  # not tried again
        errors = [record for record in caplog.records if record.msg == UNAVAILABLE]
        assert [(record.levelno, record.args) for record in errors] == [(logging.ERROR, ("-",))]
        assert errors[0].exc_info is not None
        assert [fn for fn, _args, _kwargs in executor.calls] == [run_explore_task]
        switched_on = run_project(unclaimed())
        assert loop_module._explore_task_for(switched_on) is run_explore_task

    elif aspect == "startup_healthcheck_only":
        config_path = dispatch_yaml(tmp_path, "http://127.0.0.1:9", repair="section")
        script = textwrap.dedent(
            """
            import sys, threading
            started = []
            real_start = threading.Thread.start
            threading.Thread.start = lambda self: started.append(self.name) or real_start(self)
            from cairn.cli import main
            try:
                main(["dispatch", "--config", sys.argv[1], "--startup-healthcheck-only"], standalone_mode=False)
            finally:
                loaded = sorted(n for n in sys.modules if n.startswith("cairn.dispatcher.repair"))
                print("RESULT", loaded, started)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(config_path)],
            cwd=tmp_path,
            env={**os.environ, "PATH": INTERPRETER_PATH, "PYTHONPATH": str(CAIRN_DIR / "src"), "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "RESULT [] []" in result.stdout

    elif aspect == "one_daemon_thread":
        stop = threading.Event()
        monkeypatch.setattr(housekeeping, "_STOP", stop)
        before = set(threading.enumerate())
        loop_module._start_repair_side(section_config(tmp_path))
        try:
            new = [thread for thread in set(threading.enumerate()) - before if thread.name.startswith("repair-")]
            assert [(thread.name, thread.daemon) for thread in new] == [("repair-housekeeping", True)]
        finally:
            stop.set()
            for thread in new:
                thread.join(5)

    elif aspect == "thread_cannot_start":

        class UnstartableThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(housekeeping, "threading", SimpleNamespace(Thread=UnstartableThread))
        results = {}
        for repair in ("none", "section"):
            project = make_project(intents=[unclaimed()])
            workdir = tmp_path / repair
            workdir.mkdir()
            with fake_server(cairn_responder({"proj_001": project})) as server:
                loop, executor, calls = in_process_loop(dispatch_yaml(workdir, server.base_url, repair=repair))
                loop.run(once=True)
                if repair == "section":
                    housekeeping.start_repair_side(loop.config)  # a second failure is not logged again
                requests_seen = [(request.method, request.path, request.body) for request in server.requests]
            submitted = [(fn, [type(arg).__name__ for arg in args]) for fn, args, _kwargs in executor.calls]
            results[repair] = (submitted, calls, requests_seen)
        assert results["section"] == results["none"]
        failures = [record for record in caplog.records if record.msg == HOUSEKEEPING_FAILED]
        assert [(record.levelno, record.args) for record in failures] == [(logging.WARNING, ("start", "RuntimeError"))]
        assert not [record for record in caplog.records if record.msg == UNAVAILABLE]

    elif aspect == "steps_and_passes_are_guarded":
        ran: list[str] = []

        def failing(step: str) -> Any:
            def step_function(*_args: Any) -> None:
                ran.append(step)
                raise OSError(f"{step} failed")

            return step_function

        monkeypatch.setattr(housekeeping, "load_state", failing("token"))
        monkeypatch.setattr(housekeeping, "check_shared_keys", failing("shared_keys"))
        monkeypatch.setattr(housekeeping, "write_agent_settings", failing("settings"))
        monkeypatch.setattr(housekeeping, "remove_copies", failing("removal"))
        for _ in range(2):  # a second thread in the same process logs nothing more
            monkeypatch.setattr(housekeeping, "_STOP", StopAfter(3))
            housekeeping._housekeeping(section_config(tmp_path))
        assert ran == ["token", "shared_keys", "settings", "removal", "removal", "removal"] * 2
        failures = [record.args for record in caplog.records if record.msg == HOUSEKEEPING_FAILED]
        assert failures == [
            ("token", "OSError"),
            ("shared_keys", "OSError"),
            ("settings", "OSError"),
            ("removal", "OSError"),
        ]

    elif aspect == "passes_every_60_seconds":
        clock = StopAfter(4)
        passes: list[float] = []
        clients: list[Any] = []
        monkeypatch.setattr(housekeeping, "_STOP", clock)

        def removal(config: DispatchConfig, client: Any) -> None:
            passes.append(clock.now)
            clients.append(client)

        monkeypatch.setattr(housekeeping, "remove_copies", removal)
        housekeeping._housekeeping(section_config(tmp_path))
        assert passes == [0.0, 60.0, 120.0, 180.0]
        assert clock.waits == [60.0] * 4
        assert len({id(client) for client in clients}) == 1  # the housekeeping thread's one client
        assert clients[0]._token is None  # its reads send no repair token
        assert [record for record in caplog.records if record.msg == HOUSEKEEPING_FAILED] == []


def _raiser(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("injected failure")
