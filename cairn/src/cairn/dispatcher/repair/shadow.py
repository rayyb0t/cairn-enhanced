"""ShadowRun: the hooks of one observed explore run, and its reporter thread (spec 8.5).

The task thread only calls the hooks, through repair/explore.py's pass-throughs, and every hook only stores values
and returns. The reporter thread, a daemon thread started when the execute process has started, does all I/O, in
this order (spec 8.5):

1. Join the project's count of runs being reported (step 9 leaves it, however the thread ends). Read the dispatcher
   token if no thread has read it yet (repair/config.py). Not configured: stop.
2. Register the run: PUT /repair/runs/{run_id}, up to START_ATTEMPTS attempts RETRY_DELAY_SECONDS apart, with
   started_seconds_ago recomputed on each. The body that got the 2xx is kept as the accepted register body. Not
   registered: stop, with no copies and no /end.
3. Until the execute process has ended: probe its output for a codex or pi session id every PROBE_INTERVAL_SECONDS,
   and send a valid id (a session call: the accepted register body with session_id set, up to UPDATE_ATTEMPTS
   attempts); run the copy schedule. Wait on the end between steps.
4. After the end: if no id was sent and the driver reads its id from the output (codex, pi), parse the full output
   once, the same parse as tasks/explore.py, and send a valid id the same way. Skipped for claudecode and mock, when
   an id was already sent, and when a probe of this run raised.
5. End: POST /repair/runs/{run_id}/end with the exit fields, exited_seconds_ago recomputed on each attempt.
6. Wait for the task to return, at most tasks.explore.conclude_timeout + TASK_DONE_GRACE_SECONDS.
7. Outcome: POST /repair/runs/{run_id}/end with the task's return value.
8. The final copy, after the task has returned, unless the task was cancelled.
9. Leave the project's count of runs being reported.
10. Close the client's session and drop the process reference.

The server's answers are handled as spec 8.6's table says (repair/client.py classifies them): a refusal or a 422 on
a session or end call never stops the steps after it, and a configuration error (401, 503 token_not_configured, or a
404 from a server without the repair router) stops this run's requests. An exception in a probe or in the
end-of-run parse never stops them either, and neither does one from the run's CopySchedule, while it is built or
run: that only ends the run's copies. No log line holds a session id, a token or a key.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.repair import session_ids, transcripts
from cairn.dispatcher.repair.client import Answer, RepairRunsClient
from cairn.dispatcher.repair.config import load_state
from cairn.dispatcher.repair.session_ids import OutputSessionProbe, SessionParser, accept_session_id, argv_session_seed
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import did_timeout

LOG = logging.getLogger(__name__)

START_ATTEMPTS = 3  # register: a short blip of the server is retried
UPDATE_ATTEMPTS = 2  # session and end calls
RETRY_DELAY_SECONDS = 2.0
TASK_DONE_GRACE_SECONDS = 120  # step 6 waits at most tasks.explore.conclude_timeout plus this for the task to return
LOCATOR_VALUE_MAX_CHARS = 1024  # the server's limit for a locator value (spec 6.1); a longer value is sent as null


class ShadowRun:
    """One explore run of a project whose Repair switch is on, reported to the server for the repair agent.

    Built by repair/explore.py for each run, with no I/O. The hooks are called on the task thread and only store
    values; the reporter thread they start does everything else.
    """

    def __init__(
        self,
        *,
        run_id: str,
        config: DispatchConfig,
        project_id: str,
        intent_id: str,
        worker: WorkerConfig,
        execution: str,
        driver: SessionParser,
        backend: Any,
        cancellation: TaskCancellation,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.project_id = project_id
        self.intent_id = intent_id
        self.worker = worker
        self.worker_name = worker.name
        self.cli_type: str = worker.type
        self.execution = execution
        self.driver = driver
        self.backend = backend
        self.cancellation = cancellation
        self.ended = threading.Event()  # the execute process has ended, or the task has returned without it
        self.task_done = threading.Event()  # the task has returned
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Stored by the hooks, on the task thread.
        self.prompt_marker: str | None = None
        self.container: str | None = None  # the container's name, or in local mode the project's work folder
        self.budget_seconds: int | None = None
        self.argv_session: str | None = None  # claudecode's --session-id value, not yet checked against the id rule
        self.process: Any = None
        self.started_monotonic: float | None = None
        self.started_wall: float | None = None
        self.exited_monotonic: float | None = None
        self.result: ProcessResult | None = None
        self.outcome: str | None = None
        # Kept by the reporter thread.
        self.session_id: str | None = None  # the id the server has accepted for this run
        self.register_body: dict[str, Any] | None = None  # the register body that got the 2xx
        self._found_session: str | None = None  # an id found in the output that has not been sent yet
        self._found_source = "output"
        self._session_settled = False  # a session call got a final answer (anything but retries running out)
        self._requests_stopped = False  # a configuration error: this run sends no further request
        self._refusal_logged = False
        self._failures_logged: set[str] = set()
        self._copies_failed = False

    # Hooks, called on the task thread: they only store values. ---------------------------------------------------

    def note_prompt_file(self, path: object) -> None:
        """write_text_file was called before the first build: path is the execute's graph snapshot, the marker."""
        if isinstance(path, str):
            self.prompt_marker = path

    def note_execute(self, container_name: object, command: object, timeout_seconds: object) -> bool:
        """The execute process has been built. False when the run cannot be timed, and so is not observed."""
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            return False
        if not isinstance(command, list):
            return False
        self.container = container_name if isinstance(container_name, str) else None
        self.budget_seconds = timeout_seconds
        self.argv_session = argv_session_seed(self.cli_type, command)
        return True

    def execute_started(self, process: object) -> None:
        """The execute process has started: record the start and start the reporter thread."""
        with self._lock:
            self.process = process
            self.started_monotonic = time.monotonic()
            self.started_wall = time.time()
        thread = threading.Thread(
            target=self._report, name=f"repair-shadow-{self.project_id}-{self.intent_id}", daemon=True
        )
        thread.start()  # if this raises, _quietly logs it and the run goes on unobserved
        self.thread = thread

    def execute_finished(self, result: ProcessResult) -> None:
        """communicate() has returned the execute process's result."""
        with self._lock:
            self.result = result
            if self.exited_monotonic is None:
                self.exited_monotonic = time.monotonic()
        self.ended.set()

    def task_finished(self, outcome: object) -> None:
        """The task has returned outcome, or raised (outcome None); the only end signal when communicate() raised."""
        with self._lock:
            self.outcome = outcome if isinstance(outcome, str) else None
            if self.exited_monotonic is None and self.started_monotonic is not None:
                self.exited_monotonic = time.monotonic()
        self.ended.set()
        self.task_done.set()

    # The reporter thread. ------------------------------------------------------------------------------------------

    def _report(self) -> None:
        joined = False
        client: RepairRunsClient | None = None
        try:
            transcripts.join_reporting(self.project_id)
            joined = True
            state = load_state(self.config)
            token = state.token
            if not state.configured or token is None:
                return
            client = RepairRunsClient(self.config.server, token)
            if not self._register(client):
                return
            # Built through the same guard as its operations: if building it raises, the run has no copies, and its
            # session call, both /end calls and the outcome still follow.
            copies: transcripts.CopySchedule | None = self._copies(lambda: transcripts.CopySchedule(self, token))
            probe = OutputSessionProbe(
                self.driver,
                self.cli_type,
                project_id=self.project_id,
                intent_id=self.intent_id,
                worker=self.worker_name,
                run_id=self.run_id,
            )
            self._watch(client, probe, copies)
            self._final_session(client, probe)
            self._end(client)
            task_returned = self.task_done.wait(self.config.tasks.explore.conclude_timeout + TASK_DONE_GRACE_SECONDS)
            if task_returned:
                self._report_outcome(client)
            if copies is not None and task_returned and not self.cancellation.is_cancelled:
                self._copies(copies.final_copy)
        except Exception:
            self._log_failure()
        finally:
            if joined:
                transcripts.leave_reporting(self.project_id)
            if client is not None:
                client.close()
            with self._lock:
                self.process = None

    def _register(self, client: RepairRunsClient) -> bool:
        """Step 2. True once the server has accepted the run."""
        seed = None
        if self.argv_session is not None:
            seed = accept_session_id(
                self.argv_session,
                project_id=self.project_id,
                intent_id=self.intent_id,
                worker=self.worker_name,
                run_id=self.run_id,
            )
        sent: list[dict[str, Any]] = []

        def attempt() -> Answer:
            body = self._register_body(seed)
            sent.append(body)
            return client.register(self.run_id, body)

        answer = self._call("register", attempt, START_ATTEMPTS)
        if not answer.ok:
            return False
        self.register_body = sent[-1]
        LOG.info(
            "repair shadow run registered project=%s intent=%s worker=%s run=%s cli=%s budget=%ss session=%s",
            self.project_id,
            self.intent_id,
            self.worker_name,
            self.run_id,
            self.cli_type,
            self.budget_seconds,
            "no" if seed is None else "yes",
        )
        if seed is not None:
            self.session_id = seed
            self._log_session("argv")
        return True

    def _watch(
        self, client: RepairRunsClient, probe: OutputSessionProbe, copies: transcripts.CopySchedule | None
    ) -> None:
        """Step 3, until the execute process has ended. copies is None when the run's CopySchedule failed to build."""
        next_probe = time.monotonic()
        while not self.ended.is_set():
            if self._probing(probe) and time.monotonic() >= next_probe:
                self._probe_once(client, probe)
                next_probe = time.monotonic() + session_ids.PROBE_INTERVAL_SECONDS
            due = None if copies is None else self._copies(copies.next_due)
            if copies is not None and due is not None and time.monotonic() >= due:
                self._copies(copies.tick)
                continue
            wakes = [moment for moment in (next_probe if self._probing(probe) else None, due) if moment is not None]
            self.ended.wait(max(0.0, min(wakes) - time.monotonic()) if wakes else None)

    def _copies(self, operation: Callable[[], Any]) -> Any:
        """Building the run's CopySchedule, or one of its operations, which handle their own failures (spec 8.8).

        Anything they let out anyway is logged once, with its traceback, and ends this run's copies; the session, both
        /end calls and the outcome still follow.
        """
        if self._copies_failed:
            return None
        try:
            return operation()
        except Exception:
            self._copies_failed = True
            self._log_failure()
            return None

    def _log_failure(self) -> None:
        LOG.warning(
            "repair shadow reporter failed project=%s intent=%s worker=%s run=%s",
            self.project_id,
            self.intent_id,
            self.worker_name,
            self.run_id,
            exc_info=True,
        )

    def _probing(self, probe: OutputSessionProbe) -> bool:
        return self.session_id is None and self._found_session is None and probe.active

    def _probe_once(self, client: RepairRunsClient, probe: OutputSessionProbe) -> None:
        try:
            found = probe.probe(self.process)
        except Exception as exc:
            self._probe_failed(probe, exc)
            return
        if found is not None:
            self._found_session, self._found_source = found, "output"
            self._send_session(client)

    def _final_session(self, client: RepairRunsClient, probe: OutputSessionProbe) -> None:
        """Step 4: the end-of-run parse, then the session call, for an id that has not been sent."""
        if self._requests_stopped or self.session_id is not None or self._session_settled:
            return
        if self._found_session is None:
            result = self.result
            if result is None or not probe.reads_output or probe.failed or probe.rejected:
                return
            try:
                found = probe.final(result.stdout, result.stderr)
            except Exception as exc:
                self._probe_failed(probe, exc)
                return
            if found is None:
                return
            self._found_session, self._found_source = found, "final_output"
        self._send_session(client)

    def _probe_failed(self, probe: OutputSessionProbe, exc: Exception) -> None:
        """A probe or the end-of-run parse raised: logged once per run; the run then has no id from its output."""
        if probe.failed:
            return
        probe.failed = True
        LOG.warning(
            "repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s",
            self.project_id,
            self.intent_id,
            self.worker_name,
            self.run_id,
            type(exc).__name__,
        )

    def _send_session(self, client: RepairRunsClient) -> None:
        """The session call: the accepted register body with session_id set; started_seconds_ago is not recomputed."""
        session_id, source = self._found_session, self._found_source
        if session_id is None or self.register_body is None or self._session_settled or self._requests_stopped:
            return
        body = {**self.register_body, "session_id": session_id}
        answer = self._call("session", lambda: client.register(self.run_id, body), UPDATE_ATTEMPTS)
        if answer.kind != "retry":
            self._session_settled = True
        if answer.ok:
            self.session_id = session_id
            self._log_session(source)

    def _end(self, client: RepairRunsClient) -> None:
        """Step 5: the exit fields."""
        if self._requests_stopped:
            return
        answer = self._call("end", lambda: client.end(self.run_id, self._exit_body()), UPDATE_ATTEMPTS)
        if not answer.ok:
            return
        result = self.result
        LOG.info(
            "repair shadow run ended project=%s intent=%s worker=%s run=%s code=%s timed_out=%s cancelled=%s execute_s=%s",
            self.project_id,
            self.intent_id,
            self.worker_name,
            self.run_id,
            "-" if result is None else result.returncode,
            "-" if result is None else did_timeout(result),
            "-" if result is None else result.cancelled,
            _seconds_between(self.started_monotonic, self.exited_monotonic),
        )

    def _report_outcome(self, client: RepairRunsClient) -> None:
        """Step 7: the task's return value."""
        outcome = self.outcome
        if self._requests_stopped or outcome is None:
            return
        answer = self._call("outcome", lambda: client.end(self.run_id, {"outcome": outcome}), UPDATE_ATTEMPTS)
        if answer.ok:
            LOG.info(
                "repair shadow outcome reported project=%s intent=%s run=%s outcome=%s",
                self.project_id,
                self.intent_id,
                self.run_id,
                outcome,
            )

    def _call(self, call: str, send: Callable[[], Answer], attempts: int) -> Answer:
        """One register, session or end call, retried as spec 8.6 says, and its answer logged."""
        attempt = 1
        answer = send()
        while answer.kind == "retry" and attempt < attempts:
            time.sleep(RETRY_DELAY_SECONDS)
            attempt += 1
            answer = send()
        self._note_answer(call, answer, attempt)
        return answer

    def _note_answer(self, call: str, answer: Answer, attempts: int) -> None:
        if answer.ok:
            return
        if answer.kind == "not_configured":
            # The client has logged "repair shadow server not configured" once per process.
            self._requests_stopped = True
            return
        if answer.kind == "refused":
            if not self._refusal_logged:
                self._refusal_logged = True
                LOG.info(
                    "repair shadow registration refused project=%s intent=%s worker=%s run=%s call=%s status=%s code=%s",
                    self.project_id,
                    self.intent_id,
                    self.worker_name,
                    self.run_id,
                    call,
                    answer.status,
                    answer.code or "-",
                )
            return
        # Retries ran out, or a 422 (or another status spec 8.6 does not list), which is not retried.
        if call not in self._failures_logged:
            self._failures_logged.add(call)
            LOG.warning(
                "repair shadow registration failed project=%s intent=%s worker=%s run=%s call=%s status=%s attempts=%s",
                self.project_id,
                self.intent_id,
                self.worker_name,
                self.run_id,
                call,
                answer.status,
                attempts,
            )

    def _log_session(self, source: str) -> None:
        LOG.info(
            "repair shadow session registered project=%s intent=%s worker=%s run=%s source=%s after_s=%s",
            self.project_id,
            self.intent_id,
            self.worker_name,
            self.run_id,
            source,
            _seconds_between(self.started_monotonic, time.monotonic()),
        )

    # Bodies (spec 8.6), with exact key sets. -----------------------------------------------------------------------

    def _register_body(self, session_id: str | None) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "intent_id": self.intent_id,
            "worker": self.worker_name,
            "cli_type": self.cli_type,
            "execution": self.execution,
            "budget_seconds": self.budget_seconds,
            "session_id": session_id,
            "started_seconds_ago": _seconds_ago(self.started_monotonic),
            "locator": {
                "container": _locator_value(self.container),
                "prompt_marker": _locator_value(self.prompt_marker),
                "dispatcher_started_at": _wall_time(self.started_wall),
            },
        }

    def _exit_body(self) -> dict[str, Any]:
        result = self.result
        return {
            "timed_out": None if result is None else did_timeout(result),
            "cancelled": None if result is None else result.cancelled,
            "returncode": None if result is None else result.returncode,
            "exited_seconds_ago": _seconds_ago(self.exited_monotonic),
        }


def _seconds_ago(moment: float | None) -> float:
    return 0.0 if moment is None else round(max(0.0, time.monotonic() - moment), 3)


def _seconds_between(start: float | None, end: float | None) -> float | str:
    return "-" if start is None or end is None else round(max(0.0, end - start), 2)


def _locator_value(value: str | None) -> str | None:
    return value if value is not None and len(value) <= LOCATOR_VALUE_MAX_CHARS else None


def _wall_time(timestamp: float | None) -> str | None:
    """A wall-clock time as 2026-11-14T10:00:00.123Z (UTC, milliseconds)."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
