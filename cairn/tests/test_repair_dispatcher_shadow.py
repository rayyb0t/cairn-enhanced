"""Dispatcher tests of spec 16 for the run with the Repair switch on: the pass-throughs and the reporter thread.

Build step 6: DS5-DS8, DS10, DS11, DS13-DS20, DS23, DS24 and DS26. The scheduler's touch point (DS1-DS4, DS30) is
in test_repair_dispatcher_scheduler.py, and the two tests against the real server (DS9, DS25) are in
test_repair_dispatcher_server.py. A run goes through today's run_explore_task, or through
run_explore_task_with_repair, with a backend and processes that record every call (repair_dispatcher_runs.py); the
reporter thread's requests go to a fake repair server on 127.0.0.1. Every value here is synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from conftest import FakeLease, make_intent
from repair_dispatcher_runs import (
    CANARIES,
    CLIS,
    CODEX_ID,
    ENDINGS,
    ENV,
    OK_JSON,
    PI_ID,
    WALL_TIME,
    ClientSpy,
    FakeRepairServer,
    RecordingClient,
    RepairCall,
    RunCapture,
    ScriptedBackend,
    final_answer,
    make_token_file,
    normalize,
    records_with,
    repair_server,
    run_config,
    run_project,
    run_records,
    scripts_for,
    session_lines,
)
from repair_dispatcher_support import (
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    IDS,
    bypass_proxies,
    json_reply,
    reset_repair_process_state,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import explore as repair_explore
from cairn.dispatcher.repair import housekeeping, session_ids, shadow, transcripts
from cairn.dispatcher.repair.explore import ObservedBackend, run_explore_task_with_repair
from cairn.dispatcher.repair.session_ids import OutputSessionProbe
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.runtime.process import ManagedProcess, ProcessResult
from cairn.dispatcher.tasks import common
from cairn.dispatcher.tasks.common import did_timeout
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.dispatcher.workers.adapters import CodexDriver, PiDriver
from cairn.dispatcher.workers.registry import get_driver

REPOSITORY = Path(__file__).resolve().parents[2]
CAIRN_SRC = REPOSITORY / "cairn" / "src"
OUTPUT_IDS = {"codex": CODEX_ID, "pi": PI_ID}
REGISTER_KEYS = {
    "project_id",
    "intent_id",
    "worker",
    "cli_type",
    "execution",
    "budget_seconds",
    "session_id",
    "started_seconds_ago",
    "locator",
}
LOCATOR_KEYS = {"container", "prompt_marker", "dispatcher_started_at"}
END_KEYS = {"timed_out", "cancelled", "returncode", "exited_seconds_ago"}

# Spec 8.11, the complete list of the dispatcher's repair log lines.
SPEC_LINES = (
    "repair shadow configured transcript_root=%s reviewers=%s projects=%s",
    "repair shadow not configured, runs are not reported reason=%s",
    "repair shadow token file readable by others token_file=%s mode=%s",
    "repair reviewer shares a worker key reviewer=%s worker=%s",
    "repair shadow agent settings written path=%s reviewers=%s",
    "repair shadow agent settings not written path=%s error=%s",
    "repair shadow housekeeping failed step=%s error=%s",
    "repair shadow run registered project=%s intent=%s worker=%s run=%s cli=%s budget=%ss session=%s",
    "repair shadow session registered project=%s intent=%s worker=%s run=%s source=%s after_s=%s",
    "repair shadow session rejected project=%s intent=%s worker=%s run=%s len=%s sha256=%s",
    "repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s",
    "repair shadow run ended project=%s intent=%s worker=%s run=%s code=%s timed_out=%s cancelled=%s execute_s=%s",
    "repair shadow outcome reported project=%s intent=%s run=%s outcome=%s",
    "repair shadow registration refused project=%s intent=%s worker=%s run=%s call=%s status=%s code=%s",
    "repair shadow registration failed project=%s intent=%s worker=%s run=%s call=%s status=%s attempts=%s",
    "repair shadow server not configured status=%s code=%s",
    "repair shadow transcript copied project=%s intent=%s run=%s bytes=%s redactions=%s copy=%s",
    "repair shadow transcript copy failed project=%s intent=%s run=%s reason=%s",
    "repair shadow transcript copy group not set root_gid=%s",
    "repair shadow credential file too large, skipped file=%s bytes=%s",
    "repair shadow copies removed project=%s reason=%s count=%s",
    "repair shadow copies not removed project=%s reason=%s error=%s",
    "repair shadow hook failed project=%s intent=%s worker=%s run=%s hook=%s error=%s",
    "repair shadow reporter failed project=%s intent=%s worker=%s run=%s",
    "repair module unavailable, running today's explore task project=%s",
)
NOT_CONFIGURED = SPEC_LINES[1]
HOUSEKEEPING_FAILED = SPEC_LINES[6]
RUN_REGISTERED = SPEC_LINES[7]
SESSION_REGISTERED = SPEC_LINES[8]
PROBE_FAILED = SPEC_LINES[10]
RUN_ENDED = SPEC_LINES[11]
OUTCOME_REPORTED = SPEC_LINES[12]
REFUSED = SPEC_LINES[13]
FAILED = SPEC_LINES[14]
SERVER_NOT_CONFIGURED = SPEC_LINES[15]
HOOK_FAILED = SPEC_LINES[22]
REPORTER_FAILED = SPEC_LINES[23]
# The lines that build step 6's code logs; build step 7 adds the shared-key, settings-file and copy lines.
STEP6_LINES = frozenset(SPEC_LINES[:3] + SPEC_LINES[6:16] + SPEC_LINES[22:])


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fresh once-per-process state, short waits, and test servers reached directly; no record holds a secret."""
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(session_ids, "PROBE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(shadow, "RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(shadow, "TASK_DONE_GRACE_SECONDS", 5)
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        assert FAKE_TOKEN not in text
        assert FAKE_REVIEWER_KEY not in text


@pytest.fixture
def server() -> Iterator[FakeRepairServer]:
    with repair_server() as fake:
        yield fake


@pytest.fixture
def runs(server: FakeRepairServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunCapture]:
    """The runs of the test; their reporter threads end before the fake server stops (fixture order)."""
    capture = RunCapture(monkeypatch)
    yield capture
    capture.finish()


def config_for(cli: str, server: FakeRepairServer, tmp_path: Path, **kwargs: Any) -> DispatchConfig:
    return run_config(cli, server.base_url, make_token_file(tmp_path), tmp_path / "copies", **kwargs)


@dataclass
class Run:
    observed: dict[str, Any]
    duration: float
    outcome: str
    backend: Any
    client: RecordingClient


def run_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    config: DispatchConfig,
    *,
    repair: bool,
    scripts: list[dict[str, Any]] | None = None,
    backend: Any = None,
    intent_id: str = "i001",
    lease: Any = None,
    cancelled: bool = False,
) -> Run:
    """One explore run through today's task (repair False) or its Repair version, and everything the run did."""
    intent = make_intent(intent_id)
    project = run_project(intent, repair=repair)
    client = RecordingClient(project)
    backend = backend if backend is not None else ScriptedBackend(scripts or [])
    the_lease = lease if lease is not None else FakeLease()
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: the_lease)
    cancellation = TaskCancellation()
    if cancelled:
        cancellation.cancel("project stopped")
    task = run_explore_task_with_repair if repair else run_explore_task
    caplog.clear()
    started = time.monotonic()
    outcome = task(config, client, backend, project, "facts: []\n", intent, config.workers[0], cancellation)
    duration = time.monotonic() - started
    names: dict[str, str] = {}
    observed = {
        "outcome": outcome,
        "backend": normalize(getattr(backend, "log", None), names),
        "client": normalize(client.calls, names),
        "logs": normalize(run_records(caplog), names),
        "lease": (getattr(the_lease, "started", None), getattr(the_lease, "stopped", None)),
    }
    return Run(observed, duration, outcome, backend, client)


def lines_of(caplog: pytest.LogCaptureFixture, fmt: str, run_id: str | None = None) -> list[tuple[Any, ...]]:
    """The args of each record of format fmt, of one run when run_id is given."""
    found = []
    for record in records_with(caplog, fmt):
        args = tuple(record.args or ())
        if run_id is None or run_id in args:
            found.append(args)
    return found


def timing_free(calls: list[RepairCall]) -> list[tuple[str, Any]]:
    """The calls with the values that differ between two identical runs set aside: times and the run id."""
    shaped = []
    for call in calls:
        body = dict(call.body)
        for key in ("started_seconds_ago", "exited_seconds_ago"):
            if key in body:
                body[key] = "<seconds>"
        if "locator" in body:
            body["locator"] = {**body["locator"], "dispatcher_started_at": "<time>"}
            body["locator"]["prompt_marker"] = re.sub(r"[0-9a-f]{12}", "<hex>", body["locator"]["prompt_marker"])
        shaped.append((call.call, body))
    return shaped


# DS5 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cli", CLIS)
@pytest.mark.parametrize("ending", ENDINGS)
def test_switch_on_changes_no_command_request_or_outcome(
    cli: str,
    ending: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS5. Every backend call (full argv, env, timeout, kill_after_seconds, call shape), snapshot write, process
    call, CairnClient call, the outcome and every log record outside cairn.dispatcher.repair* are today's; then the
    registration sequence is checked, request by request.
    """
    config = config_for(cli, server, tmp_path)
    spy = ClientSpy(monkeypatch)
    cancelled = ending == "cancelled"
    today = run_once(monkeypatch, caplog, config, repair=False, scripts=scripts_for(cli, ending), cancelled=cancelled)
    with_repair = run_once(
        monkeypatch, caplog, config, repair=True, scripts=scripts_for(cli, ending), cancelled=cancelled
    )

    assert with_repair.observed == today.observed

    [run] = runs.observed
    if ending in ("start_raises", "build_raises"):
        # A process that never started is never registered: no reporter thread, no request.
        assert run.thread is None
        time.sleep(0.2)
        assert server.calls == []
        return
    runs.wait()
    calls = server.calls_of(run.run_id)
    assert server.calls == calls  # nothing but this run's calls
    expected = ["register", "session", "end", "outcome"] if cli in OUTPUT_IDS else ["register", "end", "outcome"]
    assert [call.call for call in calls] == expected
    assert re.fullmatch(r"[0-9a-f]{32}", run.run_id)
    assert [call.path for call in calls] == [
        f"/repair/runs/{run.run_id}" if call.method == "PUT" else f"/repair/runs/{run.run_id}/end" for call in calls
    ]
    assert [call.method for call in calls] == ["PUT"] * (len(calls) - 2) + ["POST", "POST"]
    assert all(call.headers.get("x-cairn-repair-token") == FAKE_TOKEN for call in calls)
    # Every request is made on the run's reporter thread, never on the task thread.
    assert spy.threads and {thread for _, thread in spy.threads} == {"repair-shadow-proj_001-i001"}

    builds = [entry for entry in with_repair.backend.log if entry[0] == "build_exec_process"]
    writes = [entry for entry in with_repair.backend.log if entry[0] == "write_text_file"]
    execute_argv = builds[0][1][2]
    marker = writes[0][1][1]
    assert re.fullmatch(r"/tmp/cairn-prompts/explore_execute-[0-9a-f]{12}/graph\.yaml", marker)
    register = calls[0].body
    assert register == {
        "project_id": "proj_001",
        "intent_id": "i001",
        "worker": "w1",
        "cli_type": cli,
        "execution": "container",
        "budget_seconds": 1800,
        "session_id": execute_argv[2] if cli == "claudecode" else None,
        "started_seconds_ago": register["started_seconds_ago"],
        "locator": {
            "container": "cairn-dispatch-proj_001",
            "prompt_marker": marker,
            "dispatcher_started_at": register["locator"]["dispatcher_started_at"],
        },
    }
    assert isinstance(register["started_seconds_ago"], float) and 0 <= register["started_seconds_ago"] < 30
    assert WALL_TIME.fullmatch(register["locator"]["dispatcher_started_at"])
    if cli == "claudecode":
        assert execute_argv[1] == "--session-id"
    if cli in OUTPUT_IDS:
        assert calls[1].body == {**register, "session_id": OUTPUT_IDS[cli]}

    script = scripts_for(cli, ending)[0]
    returncode = script.get("rc", 0)
    result = ProcessResult(returncode, "", "", cancelled=cancelled)
    end = calls[-2].body
    assert end == {
        "timed_out": did_timeout(result),
        "cancelled": cancelled,
        "returncode": returncode,
        "exited_seconds_ago": end["exited_seconds_ago"],
    }
    assert 0 <= end["exited_seconds_ago"] < 30
    assert calls[-1].body == {"outcome": today.outcome}
    assert lines_of(caplog, OUTCOME_REPORTED, run.run_id) == [("proj_001", "i001", run.run_id, today.outcome)]


# DS6 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cli", ["codex", "claudecode"])
def test_lease_lost_run_is_identical_with_switch_on(
    cli: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS6. A run whose lease was lost ends as today, "failed", with the switch on."""
    config = config_for(cli, server, tmp_path)

    def lost_lease() -> FakeLease:
        lease = FakeLease()
        lease.failure = SimpleNamespace(status_code=409)  # type: ignore[assignment]
        return lease

    today = run_once(monkeypatch, caplog, config, repair=False, scripts=scripts_for(cli, "success"), lease=lost_lease())
    assert today.outcome == "failed"
    with_repair = run_once(
        monkeypatch, caplog, config, repair=True, scripts=scripts_for(cli, "success"), lease=lost_lease()
    )
    assert with_repair.observed == today.observed
    runs.wait()
    [run] = runs.observed
    assert server.kinds(run.run_id)[-2:] == ["end", "outcome"]
    assert server.calls_of(run.run_id)[-1].body == {"outcome": "failed"}


def test_real_lease_failure_kills_through_the_observed_process_exactly_as_today(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS6. The real HeartbeatLease gets a 409 and kills the process it holds: with the switch on that is the
    ObservedProcess, which passes the kill straight through.
    """
    config = config_for("codex", server, tmp_path)

    def real_lease() -> HeartbeatLease:
        return HeartbeatLease(lambda: ApiResult(409, None, "claimed by someone else"), "scope", "w1", 1)

    scripts = [{"chunks": [*session_lines("codex"), (1.8, "out", OK_JSON)]}]
    today = run_once(monkeypatch, caplog, config, repair=False, scripts=[dict(scripts[0])], lease=real_lease())
    assert today.outcome == "failed"
    assert ("process.kill",) in today.observed["backend"]
    with_repair = run_once(monkeypatch, caplog, config, repair=True, scripts=[dict(scripts[0])], lease=real_lease())
    assert with_repair.observed == today.observed
    runs.wait()
    [run] = runs.observed
    assert server.kinds(run.run_id) == ["register", "session", "end", "outcome"]


# DS7 -----------------------------------------------------------------------------------------------------------------

FAULTS = (
    "hang",
    "raise",
    "status_500",
    "status_404",
    "status_409",
    "setup",
    "thread_start",
    "note_prompt_file",
    "note_execute",
    "execute_started",
    "execute_finished",
    "task_finished",
    "probe",
    "deep_pi_line",
)
DEEP_LINE = "[" * 200_000 + "]" * 200_000 + "\n"  # pi's parser catches only JSONDecodeError; this raises RecursionError


def _raiser(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("injected failure")


def fault_scripts(fault: str) -> list[dict[str, Any]]:
    if fault == "deep_pi_line":
        return [{"chunks": [(0.0, "out", DEEP_LINE), *session_lines("pi"), *final_answer("pi", OK_JSON)]}]
    return scripts_for("codex", "timeout_then_conclude")


@pytest.mark.parametrize("fault", FAULTS)
def test_registration_failures_never_change_or_slow_the_run(
    fault: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS7. 14 faults on the repair side: the run's result is today's and it takes no longer (within 0.5 s)."""
    cli = "pi" if fault == "deep_pi_line" else "codex"
    config = config_for(cli, server, tmp_path)
    today = run_once(monkeypatch, caplog, config, repair=False, scripts=fault_scripts(fault))

    if fault == "hang":
        server.delay = 30.0  # released when the test ends
        monkeypatch.setattr(repair_client, "CALL_TIMEOUT_SECONDS", (1.0, 0.3))
    elif fault == "raise":
        monkeypatch.setattr(repair_client.RepairRunsClient, "register", _raiser)
    elif fault == "status_500":
        server.answers = {"register": [500, 500, 500]}
    elif fault == "status_404":
        server.replies = {"register": json_reply(404, {"detail": "Not Found"})}  # a server without the repair router
    elif fault == "status_409":
        server.answers = {"register": [409]}
    elif fault == "setup":
        monkeypatch.setattr(repair_explore, "ShadowRun", _raiser)
    elif fault == "thread_start":

        class UnstartableThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        fake_threading = SimpleNamespace(Thread=UnstartableThread, Lock=threading.Lock, Event=threading.Event)
        monkeypatch.setattr(shadow, "threading", fake_threading)
    elif fault in ("note_prompt_file", "note_execute", "execute_started", "execute_finished", "task_finished"):
        monkeypatch.setattr(shadow.ShadowRun, fault, _raiser)
    elif fault == "probe":
        monkeypatch.setattr(session_ids, "complete_lines", _raiser)

    with_repair = run_once(monkeypatch, caplog, config, repair=True, scripts=fault_scripts(fault))
    assert with_repair.observed == today.observed
    assert with_repair.duration < today.duration + 0.5  # the run never waits for the repair side

    if fault == "setup":
        assert runs.runs == [None]
        assert lines_of(caplog, HOOK_FAILED) == [("proj_001", "i001", "w1", "-", "setup", "RuntimeError")]
        return
    [run] = runs.observed
    if fault in ("note_prompt_file", "note_execute", "execute_started", "execute_finished", "task_finished"):
        assert lines_of(caplog, HOOK_FAILED, run.run_id) == [
            ("proj_001", "i001", "w1", run.run_id, fault, "RuntimeError")
        ]
    if fault == "thread_start":
        assert run.thread is None
        assert lines_of(caplog, HOOK_FAILED, run.run_id) == [
            ("proj_001", "i001", "w1", run.run_id, "execute_started", "RuntimeError")
        ]
    if fault in ("note_execute", "execute_started", "thread_start"):
        time.sleep(0.2)
        assert server.calls == []  # the run went on unobserved
        return
    if fault == "task_finished":
        runs.finish()  # the outcome never arrives; the reporter gives up waiting for it
        assert server.kinds(run.run_id) == ["register", "session", "end"]
        return
    runs.wait()
    kinds = server.kinds(run.run_id)
    if fault == "hang":
        assert kinds == ["register"] * 3
        assert lines_of(caplog, FAILED, run.run_id) == [("proj_001", "i001", "w1", run.run_id, "register", 0, 3)]
    elif fault == "raise":
        assert kinds == []
        assert len(lines_of(caplog, REPORTER_FAILED, run.run_id)) == 1
    elif fault == "status_500":
        assert kinds == ["register"] * 3
        assert lines_of(caplog, FAILED, run.run_id) == [("proj_001", "i001", "w1", run.run_id, "register", 500, 3)]
    elif fault == "status_404":
        assert kinds == ["register"]
        assert lines_of(caplog, SERVER_NOT_CONFIGURED) == [(404, "-")]
    elif fault == "status_409":
        assert kinds == ["register"]
        assert lines_of(caplog, REFUSED, run.run_id) == [
            ("proj_001", "i001", "w1", run.run_id, "register", 409, "test_refusal")
        ]
    elif fault in ("probe", "deep_pi_line"):
        # The probe failure is logged once, the run has no id from its output, and both /end calls are still sent.
        error = "RecursionError" if fault == "deep_pi_line" else "RuntimeError"
        assert lines_of(caplog, PROBE_FAILED, run.run_id) == [("proj_001", "i001", "w1", run.run_id, error)]
        assert kinds == ["register", "end", "outcome"]
    elif fault == "note_prompt_file":
        assert server.calls_of(run.run_id)[0].body["locator"]["prompt_marker"] is None
        assert kinds == ["register", "session", "end", "outcome"]
    elif fault == "execute_finished":
        # The task's return is the end signal, and the exit fields are unknown. With no result there is no
        # end-of-run parse, so the id is sent only if a probe saw it before the task returned.
        assert kinds in (["register", "session", "end", "outcome"], ["register", "end", "outcome"])
        end = server.calls_of(run.run_id)[-2].body
        assert {key: end[key] for key in ("timed_out", "cancelled", "returncode")} == dict.fromkeys(
            ("timed_out", "cancelled", "returncode")
        )


# DS8 -----------------------------------------------------------------------------------------------------------------


def test_dispatcher_ignores_register_and_end_response_bodies(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS8. Answers whose run has a moved deadline_at and version 2 change nothing: the run is today's, and the
    reporter sends the same requests as with plain answers.
    """
    config = config_for("codex", server, tmp_path)
    scripts = scripts_for("codex", "timeout_then_conclude")
    today = run_once(monkeypatch, caplog, config, repair=False, scripts=[dict(script) for script in scripts])
    plain = run_once(monkeypatch, caplog, config, repair=True, scripts=[dict(script) for script in scripts])
    runs.wait()
    moved_run = {"deadline_at": "2099-01-01T00:00:00Z", "version": 2, "slices_used": 1, "state": "running"}
    server.replies = {
        "register": json_reply(201, {"created": True, "run": moved_run}),
        "session": json_reply(200, {"created": False, "run": moved_run}),
        "end": json_reply(200, {"run": moved_run}),
        "outcome": json_reply(200, {"run": moved_run}),
    }
    moved = run_once(monkeypatch, caplog, config, repair=True, scripts=[dict(script) for script in scripts])
    runs.wait()
    assert plain.observed == today.observed
    assert moved.observed == today.observed
    first, second = runs.observed
    assert server.kinds(second.run_id) == ["register", "session", "end", "outcome"]
    assert timing_free(server.calls_of(second.run_id)) == timing_free(server.calls_of(first.run_id))


# DS10 ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("execution", ["container", "local"])
def test_request_bodies_have_exact_key_sets(
    execution: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS10. Register, session, end and outcome bodies have exactly spec 8.6's keys, for every CLI, and the end at
    exit when communicate() raised has every exit field null except exited_seconds_ago.
    """
    for cli in CLIS:
        config = config_for(cli, server, tmp_path, execution=execution)
        run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for(cli, "success"))
        runs.wait()
        run = runs.observed[-1]
        calls = server.calls_of(run.run_id)
        assert [call.call for call in calls] == (
            ["register", "session", "end", "outcome"] if cli in OUTPUT_IDS else ["register", "end", "outcome"]
        )
        register = calls[0].body
        assert set(register) == REGISTER_KEYS
        assert set(register["locator"]) == LOCATOR_KEYS
        assert register["execution"] == execution
        assert type(register["budget_seconds"]) is int and type(register["started_seconds_ago"]) is float
        for call in calls[1:-2]:
            assert set(call.body) == REGISTER_KEYS and set(call.body["locator"]) == LOCATOR_KEYS
        assert set(calls[-2].body) == END_KEYS
        assert calls[-1].body == {"outcome": "success"}

    config = config_for("codex", server, tmp_path)
    broken = [{"chunks": session_lines("codex"), "communicate_raises": True}]
    outcome = run_once(monkeypatch, caplog, config, repair=True, scripts=broken).outcome
    assert outcome == "failed"
    runs.wait()
    run = runs.observed[-1]
    end = next(call.body for call in server.calls_of(run.run_id) if call.call == "end")
    assert set(end) == END_KEYS
    assert end["timed_out"] is None and end["cancelled"] is None and end["returncode"] is None
    assert isinstance(end["exited_seconds_ago"], float) and end["exited_seconds_ago"] >= 0


# DS11 ----------------------------------------------------------------------------------------------------------------


def test_worker_secrets_never_reach_a_body_or_a_log_record(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS11. Distinctive values of at least 8 characters in every worker's env (common_env included), pi's argv
    apiKey (its models.json), a proxy password and each reviewer's REVIEW_API_KEY reach no register, session or end
    body, no request header but the token's, and no log record of any logger.
    """
    second_key = "test-second-reviewer-key-0003"
    workers = []
    for cli in ("claudecode", "codex", "pi"):
        env = dict(ENV[cli])
        env["HTTPS_PROXY"] = f"http://proxyuser:{CANARIES['PROXY_PASSWORD']}@127.0.0.1:9"
        workers.append(
            {"name": f"w-{cli}", "type": cli, "task_types": ["explore"], "max_running": 2, "priority": 0, "env": env}
        )
    section_reviewers = [
        {
            "name": "reviewer_one",
            "env": {
                "REVIEW_MODEL": "test-model",
                "REVIEW_BASE_URL": "https://provider.invalid/v1",
                "REVIEW_API_KEY": FAKE_REVIEWER_KEY,
            },
        },
        {
            "name": "reviewer_two",
            "env": {
                "REVIEW_MODEL": "test-model",
                "REVIEW_BASE_URL": "http://127.0.0.1:9/v1",
                "REVIEW_API_KEY": second_key,
            },
        },
    ]
    config = run_config(
        "codex",
        server.base_url,
        make_token_file(tmp_path),
        tmp_path / "copies",
        workers=workers,
        common_env={"COMMON_SECRET_TOKEN": CANARIES["COMMON_SECRET_TOKEN"]},
        reviewers=section_reviewers,
    )
    pi_worker = next(worker for worker in config.workers if worker.type == "pi")
    assert CANARIES["COMMON_SECRET_TOKEN"] in pi_worker.env.values()  # common_env reached every worker
    caplog.set_level(logging.DEBUG)  # every logger, not only cairn's
    records: list[logging.LogRecord] = []
    for index, worker in enumerate(config.workers):
        cli = worker.type
        intent = make_intent(f"i00{index + 1}")
        project = run_project(intent)
        backend = ScriptedBackend(scripts_for(cli, "success"))
        monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())
        outcome = run_explore_task_with_repair(
            config, RecordingClient(project), backend, project, "facts: []\n", intent, worker, TaskCancellation()
        )
        assert outcome == "success"
        if cli == "pi":
            argv = next(entry for entry in backend.log if entry[0] == "build_exec_process")[1][2]
            assert any(CANARIES["PI_API_KEY"] in part for part in argv)  # the canary is in pi's own argv
        runs.wait()
        records.extend(caplog.records)
    assert len(runs.observed) == 3
    secrets = [*CANARIES.values(), FAKE_REVIEWER_KEY, second_key]
    for call in server.calls:
        assert call.body is not None
        dumped = json.dumps(call.body) + json.dumps(call.body, ensure_ascii=False) + call.path
        headers = {name: value for name, value in call.headers.items() if name != "x-cairn-repair-token"}
        for secret in [*secrets, FAKE_TOKEN]:
            assert secret not in dumped
            assert secret not in json.dumps(headers)
    for record in records:
        text = record.getMessage() + (record.exc_text or "")
        for secret in secrets:
            assert secret not in text


# DS13 ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cli", ["codex", "pi"])
def test_session_id_is_registered_while_the_run_is_still_going(
    cli: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS13. The id appears 0.3 s after the start, never in the register; a session call sends it while the run
    is still going (the process ends about 1.5 s after its start).
    """
    config = config_for(cli, server, tmp_path)
    head = session_lines(cli)
    chunks = [(0.3, *head[0][1:]), *head[1:], (1.2, "err", "late output\n"), *final_answer(cli, OK_JSON)]
    run = run_once(monkeypatch, caplog, config, repair=True, scripts=[{"chunks": chunks}])
    assert run.outcome == "success"
    runs.wait()
    [shadow_run] = runs.observed
    calls = server.calls_of(shadow_run.run_id)
    assert [call.call for call in calls] == ["register", "session", "end", "outcome"]
    assert calls[0].body["session_id"] is None
    assert calls[1].body == {**calls[0].body, "session_id": OUTPUT_IDS[cli]}
    [process] = run.backend.processes
    assert process.ended_at is not None
    assert calls[1].at < process.ended_at  # before the process ended, about 1.2 s after its id appeared
    [logged] = lines_of(caplog, SESSION_REGISTERED, shadow_run.run_id)
    assert logged[4] == "output" and isinstance(logged[5], float) and logged[5] >= 0.3


# DS14 ----------------------------------------------------------------------------------------------------------------


class GrowingProcess:
    """A running container process's output buffers (ManagedProcess's names), which the test appends to."""

    def __init__(self) -> None:
        self._stdout: list[str] = []
        self._stderr: list[str] = []


def test_probe_never_returns_a_cut_off_id() -> None:
    """DS14. 500 random splits of each CLI's output, with decoy lines: every probe of a growing buffer gives
    nothing or the id that the full parse gives (tasks/explore.py's), and the last one gives that id.
    """
    rng = random.Random(7)
    codex_err = (
        f"OpenAI Codex v0.118.0\n--------\nworkdir: /w\nsession id: {CODEX_ID}\n--------\nuser\n"
        "session id: deadbeef-0000\n"
    )
    pi_out = '{"type":"agent_start"}\n{"type":"session","id":"' + PI_ID + '"}\n{"type":"session","id":"other-id"}\n'
    for cli, text, stream in (("codex", codex_err, "stderr"), ("pi", pi_out, "stdout")):
        driver = get_driver(cli)
        final = driver.extract_session(None, text if stream == "stdout" else "", text if stream == "stderr" else "")
        assert final == OUTPUT_IDS[cli]
        for _ in range(500):
            cuts = sorted(rng.sample(range(1, len(text)), rng.randint(1, 12)))
            chunks = [text[start:end] for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True)]
            process = GrowingProcess()
            buffer = process._stdout if stream == "stdout" else process._stderr
            probe = OutputSessionProbe(driver, cli, **IDS)
            for chunk in chunks:
                buffer.append(chunk)
                assert probe.probe(process) in (None, final)
            assert probe.found == final


# DS15 ----------------------------------------------------------------------------------------------------------------


class CountingDriver:
    """A driver whose extract_session is the real one, counted."""

    def __init__(self, cli: str) -> None:
        self.real = get_driver(cli)
        self.calls = 0

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        self.calls += 1
        return self.real.extract_session(session, stdout, stderr)


@pytest.mark.parametrize("cli", ["codex", "pi"])
def test_probe_stops_at_the_cap_on_the_driver_stream(
    cli: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS15. Probing stops once the stream the driver reads (codex: stderr; pi: stdout) holds PROBE_MAX_CHARS without
    an id; the other stream never counts toward the cap.
    """
    cap = session_ids.PROBE_MAX_CHARS
    filler = "x" * 1023 + "\n"
    id_line = f"session id: {CODEX_ID}\n" if cli == "codex" else '{"type":"session","id":"' + PI_ID + '"}\n'
    driver_stream, other_stream = ("stderr", "stdout") if cli == "codex" else ("stdout", "stderr")

    # The driver's stream reaches the cap without an id: the probe stops, and a later id is not looked for.
    process = GrowingProcess()
    buffers = {"stdout": process._stdout, "stderr": process._stderr}
    counting = CountingDriver(cli)
    probe = OutputSessionProbe(counting, cli, **IDS)  # type: ignore[arg-type]
    while sum(map(len, buffers[driver_stream])) < cap:
        buffers[driver_stream].append(filler * 64)
        assert probe.probe(process) is None
    assert probe.exhausted and not probe.active
    calls = counting.calls
    buffers[driver_stream].append(id_line)
    assert probe.probe(process) is None
    assert counting.calls == calls

    # Twice the cap on the other stream does not stop it: the id in the driver's stream is still found.
    process = GrowingProcess()
    buffers = {"stdout": process._stdout, "stderr": process._stderr}
    probe = OutputSessionProbe(get_driver(cli), cli, **IDS)
    buffers[other_stream].extend([filler * 64] * (2 * cap // (len(filler) * 64)))
    assert probe.probe(process) is None and probe.active
    buffers[driver_stream].append(id_line)
    assert probe.probe(process) == OUTPUT_IDS[cli]

    # A run whose driver stream holds the cap before its id: no session call while it runs; its id is found by the
    # end-of-run parse (spec 8.5 step 4), which the cap does not skip.
    config = config_for(cli, server, tmp_path)
    stream = "err" if driver_stream == "stderr" else "out"
    chunks = [(0.0, stream, filler * (cap // len(filler))), (0.2, stream, id_line), (0.3, "out", "")]
    run_once(monkeypatch, caplog, config, repair=True, scripts=[{"chunks": chunks, "rc": 2}])
    runs.wait()
    [run] = runs.observed
    calls_made = server.calls_of(run.run_id)
    assert [call.call for call in calls_made] == ["register", "session", "end", "outcome"]
    [logged] = lines_of(caplog, SESSION_REGISTERED, run.run_id)
    assert logged[4] == "final_output"


# DS16 ----------------------------------------------------------------------------------------------------------------


def test_end_skips_the_full_parse_when_an_id_was_already_sent(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS16. An id sent while the run went on is not looked for again at its end: the reporter thread never parses
    the full output. A run whose id was not seen while it ran gets exactly one full parse, by the reporter.
    """
    parses: list[tuple[str, str, str]] = []
    real = CodexDriver.extract_session

    def spy(self: CodexDriver, session: str | None, stdout: str, stderr: str) -> str | None:
        parses.append((threading.current_thread().name, stdout, stderr))
        return real(self, session, stdout, stderr)

    monkeypatch.setattr(CodexDriver, "extract_session", spy)
    config = config_for("codex", server, tmp_path)
    header = f"session id: {CODEX_ID}\n"

    early = [{"chunks": [(0.0, "err", header), (0.6, "out", OK_JSON)]}]
    run = run_once(monkeypatch, caplog, config, repair=True, scripts=early)
    runs.wait()
    full = run.backend.processes[0]
    full_err = "".join(full._stderr)
    reporter = [entry for entry in parses if entry[0].startswith("repair-shadow-")]
    assert reporter, "the reporter probed the output"
    assert all(entry[1] == "" for entry in reporter)  # probes of stderr only, never the end-of-run parse
    task_parses = [entry for entry in parses if not entry[0].startswith("repair-shadow-")]
    assert task_parses == [(task_parses[0][0], OK_JSON, full_err)]  # tasks/explore.py's own parse, unchanged
    [first] = runs.observed
    assert server.kinds(first.run_id) == ["register", "session", "end", "outcome"]

    # The header comes with the last output, after the reporter's only probe (the interval is long here).
    parses.clear()
    monkeypatch.setattr(session_ids, "PROBE_INTERVAL_SECONDS", 30.0)
    late = [{"chunks": [(1.0, "err", header), (0.0, "out", OK_JSON)]}]
    run = run_once(monkeypatch, caplog, config, repair=True, scripts=late)
    runs.wait()
    second = runs.observed[-1]
    full_err = "".join(run.backend.processes[0]._stderr)
    reporter = [entry for entry in parses if entry[0].startswith("repair-shadow-")]
    assert [entry for entry in reporter if entry[1] == OK_JSON] == [(reporter[-1][0], OK_JSON, full_err)]
    assert server.kinds(second.run_id) == ["register", "session", "end", "outcome"]
    [logged] = lines_of(caplog, SESSION_REGISTERED, second.run_id)
    assert logged[4] == "final_output"


# DS17 ----------------------------------------------------------------------------------------------------------------


def test_two_concurrent_runs_in_one_container_get_their_own_session_ids(
    server: FakeRepairServer, runs: RunCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS17. Two runs of one project in one container, at once: each run registers its own id, never the other's."""
    config = config_for("codex", server, tmp_path)
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())
    ids = {"i001": "0199aaaa-0000-7000-8000-000000000001", "i002": "0199bbbb-0000-7000-8000-000000000002"}
    outcomes: dict[str, str] = {}

    def one(intent_id: str, delay: float) -> None:
        intent = make_intent(intent_id)
        project = run_project(intent)
        chunks = [(delay, "err", f"session id: {ids[intent_id]}\n"), (0.6, "out", OK_JSON)]
        backend = ScriptedBackend([{"chunks": chunks}])
        outcomes[intent_id] = run_explore_task_with_repair(
            config, RecordingClient(project), backend, project, "g", intent, config.workers[0], TaskCancellation()
        )

    threads = [threading.Thread(target=one, args=("i001", 0.2)), threading.Thread(target=one, args=("i002", 0.0))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == {"i001": "success", "i002": "success"}
    runs.wait()
    assert len(runs.observed) == 2
    for run in runs.observed:
        calls = server.calls_of(run.run_id)
        assert [call.call for call in calls] == ["register", "session", "end", "outcome"]
        assert {call.body["intent_id"] for call in calls[:2]} == {run.intent_id}
        assert calls[1].body["session_id"] == ids[run.intent_id]


# DS18 ----------------------------------------------------------------------------------------------------------------


def write_fake_codex(bin_dir: Path, *, filler: int, stamps: Path, sleep: float = 1.5) -> None:
    """A codex on PATH: its header on stderr, filler more characters there, then the answer after sleep seconds.

    It appends its time.monotonic() to stamps twice, one line each: once its stderr is written, and just before it
    answers and exits. That clock is system-wide (CLOCK_MONOTONIC on Linux), so the stamps compare with the test's
    own times and with the fake server's. `codex exec resume ...` (the conclude fallback) answers at once.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"if sys.argv[1:3] == ['exec', 'resume']:\n"
        f"    print({OK_JSON!r})\n"
        "    raise SystemExit(0)\n"
        "def stamp():\n"
        f"    with open({str(stamps)!r}, 'a', encoding='utf-8') as file:\n"
        "        file.write(repr(time.monotonic()) + '\\n')\n"
        f"sys.stderr.write('OpenAI Codex v0.118.0\\nsession id: {CODEX_ID}\\n'); sys.stderr.flush()\n"
        f"sys.stderr.write('x' * {filler} + '\\n'); sys.stderr.flush()\n"
        "stamp()\n"
        f"time.sleep({sleep})\n"
        "stamp()\n"
        f"print({OK_JSON!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


@pytest.mark.parametrize(("filler", "early"), [(0, False), (70_000, True)], ids=["0-False", "70000-True"])
def test_local_mode_session_id_arrives_when_todays_reader_has_it(
    filler: int,
    early: bool,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS18. A real LocalProcess reads 64 KiB at a time (runtime/local_process.py): a short header reaches the
    buffer only at the end of the run, when stderr closes as the process exits (then the id is sent only after
    that), a long one as soon as 64 KiB of stderr have been read (then a probe sends it while the run goes on).

    The probes run every 0.05 s (the fixture's interval) through the fake codex's 1.5 s sleep, and the fake codex
    stamps its clock once its stderr is written and again just before it answers and exits. For the short header,
    probes looked at the buffer between the two stamps, and the session call still came after the second: a reader
    that handed the header over at once would have had it sent during the sleep. Which part sends it is not
    checked: the header reaches the buffer before communicate() returns and the run is marked ended, so a probe
    that lands in between sends it from the output, which is also correct; otherwise the end-of-run parse does.
    """
    stamps = tmp_path / "codex-stamps"
    write_fake_codex(tmp_path / "bin", filler=filler, stamps=stamps)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    looks: list[float] = []  # when each probe of the run looked at the process's buffer
    real_snapshot = session_ids.output_snapshot

    def snapshot(process: object, stream: str) -> list[str] | None:
        looks.append(time.monotonic())
        return real_snapshot(process, stream)

    monkeypatch.setattr(session_ids, "output_snapshot", snapshot)
    config = config_for("codex", server, tmp_path, execution="local", timeout=30)
    from cairn.dispatcher.config import LocalConfig

    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    run = run_once(monkeypatch, caplog, config, repair=True, backend=backend)
    assert run.outcome == "success"
    runs.wait()
    [shadow_run] = runs.observed
    calls = server.calls_of(shadow_run.run_id)
    assert [call.call for call in calls] == ["register", "session", "end", "outcome"]
    register = calls[0].body
    assert register["execution"] == "local"
    assert register["locator"]["container"] == str(tmp_path / "work" / "proj_001")
    assert register["session_id"] is None
    assert calls[1].body == {**register, "session_id": CODEX_ID}
    [logged] = lines_of(caplog, SESSION_REGISTERED, shadow_run.run_id)
    # The fake codex's stamps: its stderr written, and just before it answered and exited. They are on the clock of
    # the test and of the fake server's request times; exited_monotonic, when communicate() returned, comes later.
    written, exiting = map(float, stamps.read_text(encoding="utf-8").split())
    assert shadow_run.exited_monotonic is not None
    assert exiting < shadow_run.exited_monotonic
    if early:
        assert logged[4] == "output"
        assert calls[1].at < exiting  # while the process still ran (about 1.5 s more)
    else:
        assert any(written < moment < exiting for moment in looks)  # probes looked between the two stamps
        assert logged[4] in ("output", "final_output")
        assert calls[1].at > exiting  # only once the process was exiting, when the header reached the buffer


# DS19 ----------------------------------------------------------------------------------------------------------------


def _docker_client() -> Any:
    try:
        import docker

        client = docker.from_env(timeout=5)
        client.ping()
        return client
    except Exception:
        return None


DS19_IMAGE = os.environ.get("CAIRN_TEST_DOCKER_IMAGE", "debian:bookworm-slim")


@pytest.mark.skipif(_docker_client() is None, reason="needs a Docker daemon")
def test_container_mode_session_id_arrives_while_the_exec_runs(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS19. A real exec in a real container: codex's stderr header is in ManagedProcess._stderr about 0.1 s after
    the start, and the session call reaches the server while the exec still runs (it runs about 3 s).
    """
    from cairn.dispatcher.config import ContainerConfig
    from cairn.dispatcher.runtime.containers import ContainerManager
    from cairn.dispatcher.workers.base import DriverResult

    script = f"echo 'session id: {CODEX_ID}' >&2; sleep 3; echo '{OK_JSON}'"
    monkeypatch.setattr(
        CodexDriver, "build_execute", lambda self, worker, prompt, session: DriverResult(argv=["sh", "-c", script])
    )
    manager = ContainerManager(ContainerConfig(image=DS19_IMAGE, network_mode="none", completed_action="remove"))
    config = config_for("codex", server, tmp_path, timeout=30)
    project_id = f"p2ds19{os.getpid()}"
    intent = make_intent()
    project = run_project(intent)
    project.project.id = project_id
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())
    started = time.monotonic()
    try:
        outcome = run_explore_task_with_repair(
            config, RecordingClient(project), manager, project, "g", intent, config.workers[0], TaskCancellation()
        )
    finally:
        manager.cleanup_completed(project_id)
        manager.close()
    elapsed = time.monotonic() - started
    assert outcome == "success"
    runs.wait()
    [run] = runs.observed
    calls = server.calls_of(run.run_id)
    assert [call.call for call in calls] == ["register", "session", "end", "outcome"]
    assert calls[1].body["session_id"] == CODEX_ID
    assert calls[1].at - started < 2.0 < elapsed
    [logged] = lines_of(caplog, SESSION_REGISTERED, run.run_id)
    assert logged[4] == "output"


# DS20 ----------------------------------------------------------------------------------------------------------------


class StopAfter:
    """housekeeping._STOP for a test: wait() records its timeout on a fake clock and ends the thread after n passes."""

    def __init__(self, passes: int) -> None:
        self.passes = passes
        self.waits: list[float] = []
        self.now = 0.0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        self.now += float(timeout or 0.0)
        return len(self.waits) >= self.passes


def bad_token_config(reason: str, server: FakeRepairServer, folder: Path) -> DispatchConfig:
    """A dispatch.yaml without a repair: section, or with one whose token file gives reason."""
    token_file = folder / "secrets" / "repair-dispatcher.token"
    if reason == "not_ascii":
        make_token_file(folder, "test-dispatcher-token-\u00e9\u00e9-not-ascii-0001".encode())
    elif reason == "shorter_than_32":
        make_token_file(folder, "test-token-too-short")
    return run_config("codex", server.base_url, token_file, folder / "copies", repair=reason != "no_repair_section")


@pytest.mark.parametrize("reason", ["no_repair_section", "token_unreadable", "not_ascii", "shorter_than_32"])
def test_missing_or_invalid_repair_config_means_unobserved_runs_and_one_warning(
    reason: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS20. Each reason of spec 8.3: a switched-on project's runs run today's task with the real backend, with no
    ShadowRun, thread or request, and the reason is logged once per process. With a bad token the housekeeping
    thread still runs its later steps and its passes, each for real, and it leaves the same agent settings file as
    a valid token does: the file does not depend on the token (spec 8.3, Loading).

    The valid token's file comes from a control run of the same repair: section in a process state of its own.
    Until build step 7 fills in write_agent_settings, neither run writes a file, so both are None; from step 7 on,
    DS28 makes the control's file exist, and this check then fails if a bad token stops the write.
    """
    backends: list[Any] = []
    real_steps = (housekeeping.check_shared_keys, housekeeping.write_agent_settings, housekeeping.remove_copies)
    real_task = repair_explore.run_explore_task

    def recording_task(*args: Any) -> str:
        backends.append(args[2])
        return real_task(*args)

    monkeypatch.setattr(repair_explore, "run_explore_task", recording_task)
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())

    def dispatch(config: DispatchConfig, count: int) -> None:
        for _ in range(count):
            intent = make_intent()
            project = run_project(intent)
            backend = ScriptedBackend(scripts_for("codex", "success"))
            outcome = run_explore_task_with_repair(
                config, RecordingClient(project), backend, project, "g", intent, config.workers[0], TaskCancellation()
            )
            assert outcome == "success"

    def warnings() -> list[tuple[int, Any]]:
        return [(record.levelno, record.args) for record in caplog.records if record.msg == NOT_CONFIGURED]

    def run_housekeeping(config: DispatchConfig) -> tuple[list[str], bytes | None]:
        """The housekeeping thread's body for two passes, each step recorded and then run for real; the steps, and
        the bytes of the agent settings file it leaves in the copy root (None when there is none).
        """
        check_shared_keys, write_agent_settings, remove_copies = real_steps
        steps: list[str] = []

        def spy_shared_keys(config_: DispatchConfig) -> None:
            steps.append("shared_keys")
            check_shared_keys(config_)

        def spy_settings(config_: DispatchConfig, state: Any) -> None:
            steps.append(f"settings:{state.reason}")
            write_agent_settings(config_, state)

        def spy_removal(config_: DispatchConfig, client: Any) -> None:
            steps.append("removal")
            remove_copies(config_, client)

        monkeypatch.setattr(housekeeping, "_failed_steps", set())
        monkeypatch.setattr(housekeeping, "check_shared_keys", spy_shared_keys)
        monkeypatch.setattr(housekeeping, "write_agent_settings", spy_settings)
        monkeypatch.setattr(housekeeping, "remove_copies", spy_removal)
        monkeypatch.setattr(housekeeping, "_STOP", StopAfter(2))
        housekeeping._housekeeping(config)
        assert config.repair is not None
        settings_file = Path(config.repair.transcript_root) / "agent-settings.json"
        return steps, settings_file.read_bytes() if settings_file.exists() else None

    config = bad_token_config(reason, server, tmp_path / "first")
    caplog.clear()
    if reason != "no_repair_section":
        # The housekeeping thread reads the token at dispatcher start, before any dispatch; its later steps and its
        # passes still run, and its settings file is the one a valid token gives (the control, in its own process
        # state).
        control_folder = tmp_path / "control"
        control = run_config("codex", server.base_url, make_token_file(control_folder), control_folder / "copies")
        control_steps, control_settings = run_housekeeping(control)
        assert control_steps == ["shared_keys", "settings:None", "removal", "removal"]
        assert lines_of(caplog, HOUSEKEEPING_FAILED) == []
        reset_repair_process_state(monkeypatch)
        caplog.clear()
        steps, settings = run_housekeeping(config)
        assert steps == ["shared_keys", f"settings:{reason}", "removal", "removal"]
        assert lines_of(caplog, HOUSEKEEPING_FAILED) == []
        assert settings == control_settings
    dispatch(config, 3)
    assert len(backends) == 3 and all(isinstance(backend, ScriptedBackend) for backend in backends)
    assert runs.runs == [None, None, None]
    assert warnings() == [(logging.WARNING, (reason,))]

    if reason != "no_repair_section":
        # In a new process, a run's reporter thread reads the bad token first: that run is observed, its reporter
        # logs the reason once and stops before any request, and the runs after it are not observed.
        reset_repair_process_state(monkeypatch)
        config = bad_token_config(reason, server, tmp_path / "second")
        backends.clear()
        caplog.clear()
        dispatch(config, 1)
        runs.wait()
        dispatch(config, 2)
        assert isinstance(backends[0], ObservedBackend)
        assert all(isinstance(backend, ScriptedBackend) for backend in backends[1:])
        assert runs.runs[3] is not None and runs.runs[4:] == [None, None]
        assert warnings() == [(logging.WARNING, (reason,))]
    time.sleep(0.2)
    assert server.calls == []
    assert not any(thread.name.startswith("repair-shadow-") for thread in threading.enumerate())


# DS23 ----------------------------------------------------------------------------------------------------------------


def _phase0_parser() -> ModuleType:
    path = REPOSITORY / "tools" / "repair-phase0" / "baseline_from_logs.py"
    spec = importlib.util.spec_from_file_location("phase0_baseline_from_logs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is built
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_new_log_lines_do_not_match_the_phase0_parser() -> None:
    """DS23. No line of spec 8.11 matches a pattern of the Phase 0 log parser, with ordinary values or with values
    that copy the parser's own messages; the parser's patterns still match today's source; and every repair line in
    the dispatcher's source is one of spec 8.11's, this step's among them.
    """
    parser = _phase0_parser()
    assert parser.check_formats(CAIRN_SRC) == []
    assert all(spec.prefix and not spec.prefix.startswith("repair") for spec in parser.EVENT_SPECS)
    logged = parser.logged_messages(CAIRN_SRC)
    repair_logged = {message for message in logged if message.startswith("repair ")}
    assert repair_logged <= set(SPEC_LINES)
    assert repair_logged >= STEP6_LINES  # every line this step logs is in the source
    decoys = [spec.fmt.replace("%s", "1") for spec in parser.EVENT_SPECS]
    for fmt in SPEC_LINES:
        count = fmt.count("%s")
        for values in (
            ("proj_027", "i014", "codex-1", "5f2c9a1e7b3d4c0a9e8d7c6b5a493827", "codex", 1800, "yes", "x")[:count],
            tuple(decoys[index % len(decoys)] for index in range(count)),
            tuple("\n" + decoys[(index + 3) % len(decoys)] for index in range(count)),
        ):
            message = fmt % tuple(values) if count else fmt
            assert parser.match_event(message) is None, message
            for line in message.splitlines()[:1]:
                assert parser.match_event(line) is None, line


# DS24 ----------------------------------------------------------------------------------------------------------------


class _ExecApi:
    """The Docker API calls a ManagedProcess makes for one exec: its output frames, then exit code 0."""

    def __init__(self, frames: list[tuple[bytes | None, bytes | None]]) -> None:
        self.frames = frames

    def exec_create(self, _container_id: str, _command: list[str], **_kwargs: Any) -> dict[str, str]:
        return {"Id": "exec-1"}

    def exec_start(self, _exec_id: str, **_kwargs: Any) -> Iterator[tuple[bytes | None, bytes | None]]:
        return iter(self.frames)

    def exec_inspect(self, _exec_id: str) -> dict[str, Any]:
        return {"Running": False, "ExitCode": 0}


def test_buffer_attribute_names_still_exist(tmp_path: Path) -> None:
    """DS24. The probe reads the process classes' own buffers by their private names: ManagedProcess._stdout and
    _stderr, LocalProcess._stdout_chunks and _stderr_chunks. They exist, are lists, receive the output, and the
    probe finds codex's and pi's ids in them. This fails if those names change.
    """
    assert session_ids.STDOUT_BUFFERS == ("_stdout", "_stdout_chunks")
    assert session_ids.STDERR_BUFFERS == ("_stderr", "_stderr_chunks")
    pi_line = '{"type":"session","id":"' + PI_ID + '"}\n'

    api = _ExecApi([(None, f"OpenAI Codex\nsession id: {CODEX_ID}\n".encode()), (pi_line.encode(), None)])
    container = SimpleNamespace(id="container-1", name="cairn-dispatch-proj_001", client=SimpleNamespace(api=api))
    managed = ManagedProcess(container, ["true"], {})  # type: ignore[arg-type]
    assert isinstance(managed._stdout, list) and isinstance(managed._stderr, list)
    managed.start()
    result = managed.communicate(5)
    assert "".join(managed._stdout) == result.stdout == pi_line
    assert CODEX_ID in "".join(managed._stderr)

    command = ["/bin/sh", "-c", f"printf 'session id: {CODEX_ID}\\n' >&2; printf '%s' '{pi_line}'"]
    local = LocalProcess(command, cwd=str(tmp_path), env=dict(os.environ))
    assert isinstance(local._stdout_chunks, list) and isinstance(local._stderr_chunks, list)
    local.start()
    result = local.communicate(10)
    assert "".join(local._stdout_chunks) == result.stdout == pi_line
    assert "".join(local._stderr_chunks) == result.stderr

    for process in (managed, local):
        assert OutputSessionProbe(CodexDriver(), "codex", **IDS).probe(process) == CODEX_ID
        assert OutputSessionProbe(PiDriver(), "pi", **IDS).probe(process) == PI_ID


# DS26 ----------------------------------------------------------------------------------------------------------------


def test_a_422_stops_reporting_only_on_register(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DS26. A 422 on register is logged once and nothing else is sent; a 422 on the session call is logged once,
    and both /end calls are still sent; a 422 on the exit /end still lets the outcome /end through.
    """
    config = config_for("codex", server, tmp_path)

    def run_with(answers: dict[str, list[int]]) -> str:
        server.answers = answers
        run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
        runs.wait()
        return runs.observed[-1].run_id

    run_id = run_with({"register": [422]})
    assert server.kinds(run_id) == ["register"]
    assert lines_of(caplog, FAILED, run_id) == [("proj_001", "i001", "w1", run_id, "register", 422, 1)]
    assert lines_of(caplog, RUN_REGISTERED, run_id) == []

    run_id = run_with({"session": [422]})
    assert server.kinds(run_id) == ["register", "session", "end", "outcome"]
    assert lines_of(caplog, FAILED, run_id) == [("proj_001", "i001", "w1", run_id, "session", 422, 1)]
    assert lines_of(caplog, SESSION_REGISTERED, run_id) == []
    assert len(lines_of(caplog, RUN_ENDED, run_id)) == 1
    assert lines_of(caplog, OUTCOME_REPORTED, run_id) == [("proj_001", "i001", run_id, "success")]

    run_id = run_with({"end": [422]})
    assert server.kinds(run_id) == ["register", "session", "end", "outcome"]
    assert lines_of(caplog, FAILED, run_id) == [("proj_001", "i001", "w1", run_id, "end", 422, 1)]
    assert lines_of(caplog, RUN_ENDED, run_id) == []
    assert lines_of(caplog, OUTCOME_REPORTED, run_id) == [("proj_001", "i001", run_id, "success")]


# The reporter's own steps (spec 8.5), beyond the catalogue's ids. ----------------------------------------------------


def test_reporter_joins_and_leaves_its_projects_count_and_drops_the_process(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 8.5 steps 1, 9 and 10: the project's count of runs being reported is 1 while the reporter runs and 0
    after, however it ends (a registered run, a refused register, a reporter that raised); the process reference is
    dropped.
    """
    config = config_for("codex", server, tmp_path)
    seen: list[int] = []
    real_register = repair_client.RepairRunsClient.register

    def counting_register(self_: Any, run_id: str, body: dict[str, Any]) -> Any:
        seen.append(transcripts.reporting_count("proj_001"))
        return real_register(self_, run_id, body)

    monkeypatch.setattr(repair_client.RepairRunsClient, "register", counting_register)
    for answers in ({}, {"register": [409]}):
        server.answers = answers
        run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
        runs.wait()
        assert transcripts.reporting_count("proj_001") == 0
        assert runs.observed[-1].process is None
    assert seen and set(seen) == {1}
    monkeypatch.setattr(repair_client.RepairRunsClient, "register", _raiser)
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
    runs.wait()
    assert transcripts.reporting_count("proj_001") == 0
    assert len(lines_of(caplog, REPORTER_FAILED, runs.observed[-1].run_id)) == 1


def test_retries_follow_the_attempt_limits_and_recompute_the_times(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 8.5 and 8.6: register is tried up to 3 times and each other call up to 2, on no answer, 408, 429, 500,
    502, 504 or a 503 without token_not_configured; started_seconds_ago and exited_seconds_ago are recomputed on
    each attempt, and the session call reuses the register body that got the 2xx.
    """
    monkeypatch.setattr(shadow, "RETRY_DELAY_SECONDS", 0.2)
    config = config_for("codex", server, tmp_path)
    server.answers = {"register": [503, 502], "session": [504, 200], "end": [429, 429], "outcome": [408, 200]}
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
    runs.wait()
    [run] = runs.observed
    calls = server.calls_of(run.run_id)
    assert [call.call for call in calls] == ["register"] * 3 + ["session"] * 2 + ["end"] * 2 + ["outcome"] * 2
    registers = [call.body["started_seconds_ago"] for call in calls[:3]]
    assert registers == sorted(registers) and registers[2] - registers[0] >= 0.35
    assert calls[3].body == calls[4].body == {**calls[2].body, "session_id": CODEX_ID}
    ends = [call.body["exited_seconds_ago"] for call in calls[5:7]]
    assert ends[1] - ends[0] >= 0.15
    assert lines_of(caplog, FAILED, run.run_id) == [("proj_001", "i001", "w1", run.run_id, "end", 429, 2)]
    assert len(lines_of(caplog, OUTCOME_REPORTED, run.run_id)) == 1


def test_a_configuration_error_stops_the_runs_requests_and_a_refusal_does_not(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 8.6: a 401 on the session call stops this run's requests (no /end); a refusal of the session call is one
    INFO line per run, and both /end calls still follow.
    """
    config = config_for("codex", server, tmp_path)
    server.answers = {"session": [401]}
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
    runs.wait()
    run_id = runs.observed[-1].run_id
    assert server.kinds(run_id) == ["register", "session"]
    assert lines_of(caplog, SERVER_NOT_CONFIGURED) == [(401, "test_refusal")]

    server.answers = {"session": [409], "end": [409], "outcome": [404]}
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
    runs.wait()
    run_id = runs.observed[-1].run_id
    assert server.kinds(run_id) == ["register", "session", "end", "outcome"]
    assert lines_of(caplog, REFUSED, run_id) == [("proj_001", "i001", "w1", run_id, "session", 409, "test_refusal")]
    assert lines_of(caplog, FAILED, run_id) == []


def test_a_cancelled_task_gets_no_final_copy_and_an_uncancelled_one_does(
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 8.5 step 8, as far as build step 6 goes: the reporter asks its CopySchedule for the final copy only
    after the task has returned and only if the task was not cancelled; build step 7's schedule decides the rest.
    """
    finals: list[str] = []
    monkeypatch.setattr(transcripts.CopySchedule, "final_copy", lambda self: finals.append(self.run.run_id))
    config = config_for("codex", server, tmp_path)
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "cancelled"), cancelled=True)
    runs.wait()
    assert finals == []
    run_once(monkeypatch, caplog, config, repair=True, scripts=scripts_for("codex", "success"))
    runs.wait()
    assert finals == [runs.observed[-1].run_id]


@pytest.mark.parametrize("operation", ["constructor", "next_due", "tick", "final_copy"])
def test_a_copy_schedule_that_raises_stops_the_copies_and_not_the_requests(
    operation: str,
    server: FakeRepairServer,
    runs: RunCapture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 8.5: the copy schedule handles its own failures; one that escapes anyway, from building the schedule or
    from one of its operations, is logged once with the reporter-failed line, the run's copies stop, and the session
    call, both /end calls and the outcome still follow.
    """
    attempts: list[str] = []

    def broken(_self: Any, *_args: Any) -> Any:
        attempts.append(operation)
        raise RuntimeError("copy failed")

    if operation == "tick":
        monkeypatch.setattr(transcripts.CopySchedule, "next_due", lambda _self: 0.0)  # always due
    monkeypatch.setattr(transcripts.CopySchedule, "__init__" if operation == "constructor" else operation, broken)
    config = config_for("codex", server, tmp_path)
    chunks = [*session_lines("codex"), (0.3, "out", OK_JSON)]
    run = run_once(monkeypatch, caplog, config, repair=True, scripts=[{"chunks": chunks}])
    assert run.outcome == "success"
    runs.wait()
    [shadow_run] = runs.observed
    assert server.kinds(shadow_run.run_id) == ["register", "session", "end", "outcome"]
    assert len(lines_of(caplog, REPORTER_FAILED, shadow_run.run_id)) == 1
    assert attempts == [operation]  # not tried again


def test_hooks_only_store_values_and_the_reporter_thread_is_a_named_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 8.5: the hooks make no request and read no file; the thread they start is repair-shadow-<project>-<intent>,
    a daemon; note_execute refuses a run that cannot be timed.
    """
    config = run_config("claudecode", "http://127.0.0.1:9", make_token_file(tmp_path), tmp_path / "copies")
    worker = config.workers[0]
    started: list[threading.Thread] = []
    monkeypatch.setattr(shadow.ShadowRun, "_report", lambda self: started.append(threading.current_thread()))
    opened: list[str] = []
    real_open = os.open
    monkeypatch.setattr(
        os, "open", lambda path, *args, **kwargs: opened.append(str(path)) or real_open(path, *args, **kwargs)
    )
    run = shadow.ShadowRun(
        run_id="0" * 32,
        config=config,
        project_id="proj_027",
        intent_id="i014",
        worker=worker,
        execution="container",
        driver=get_driver("claudecode"),
        backend=None,
        cancellation=TaskCancellation(),
    )
    assert run.note_execute("c", ["claude"], 0) is False
    assert run.note_execute("c", ["claude"], True) is False
    assert run.note_execute("c", ("claude",), 60) is False
    argv = ["claude", "--session-id", "0199a1b2-7c3d-4e5f-9a8b-0c1d2e3f4a5b", "-p", "--", "prompt"]
    assert run.note_execute("cairn-dispatch-proj_027", argv, 1800) is True
    assert (run.container, run.budget_seconds, run.argv_session) == (
        "cairn-dispatch-proj_027",
        1800,
        "0199a1b2-7c3d-4e5f-9a8b-0c1d2e3f4a5b",
    )
    run.note_prompt_file("/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml")
    run.execute_started(object())
    assert run.thread is not None and run.thread.daemon and run.thread.name == "repair-shadow-proj_027-i014"
    run.thread.join(5)
    assert started == [run.thread]
    run.execute_finished(ProcessResult(0, "", ""))
    assert run.ended.is_set() and not run.task_done.is_set()
    run.task_finished("success")
    assert run.task_done.is_set() and run.outcome == "success"
    assert opened == []
