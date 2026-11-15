"""Fakes for the repair layer's run-level dispatcher tests (test_repair_dispatcher_{scheduler,shadow,server}.py).

An explore run is driven through today's run_explore_task or through run_explore_task_with_repair, with a backend
and processes that record every call, a CairnClient stand-in that records the run's own requests, and a fake
repair server that records the reporter's requests. Every value here is synthetic: the keys and tokens are
obviously fake, and the ids are only names.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClient, make_intent, make_project
from repair_dispatcher_support import (
    FAKE_TOKEN,
    RecordedRequest,
    Reply,
    json_reply,
    repair_section,
    write_token,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.repair import explore as repair_explore
from cairn.dispatcher.repair.shadow import ShadowRun
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server.models import Intent, ProjectDetail, ProjectMeta

CODEX_ID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
PI_ID = "0199b2c3-d4e5-7f60-9a1b-2c3d4e5f6a7b"
OK_JSON = '{"accepted":true,"data":{"description":"a fact"}}'
CLIS = ("claudecode", "codex", "pi", "mock")

# Distinctive fake secrets, one per worker variable and file, so that a test can look for each one anywhere.
CANARIES = {
    "ANTHROPIC_AUTH_TOKEN": "canary-anthropic-auth-token-7f3a91",
    "OPENAI_API_KEY": "canary-openai-api-key-2b8c44",
    "PI_API_KEY": "canary-pi-api-key-9d1e07",
    "COMMON_SECRET_TOKEN": "canary-common-env-token-5a6b13",
    "PROXY_PASSWORD": "canary-proxy-password-0c2d88",
}
ENV: dict[str, dict[str, str]] = {
    "claudecode": {
        "ANTHROPIC_MODEL": "test-model",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/anthropic",
        "ANTHROPIC_AUTH_TOKEN": CANARIES["ANTHROPIC_AUTH_TOKEN"],
    },
    "codex": {
        "CODEX_MODEL": "test-model",
        "CODEX_BASE_URL": "http://127.0.0.1:9/codex",
        "OPENAI_API_KEY": CANARIES["OPENAI_API_KEY"],
    },
    "pi": {
        "PI_MODEL": "test-model",
        "PI_BASE_URL": "http://127.0.0.1:9/pi",
        "PI_API_KEY": CANARIES["PI_API_KEY"],
        "PI_PROVIDER_API": "openai-completions",
    },
    "mock": {},
}

RUN_PATH = re.compile(r"/repair/runs/(?P<run_id>[^/?]+)(?P<end>/end)?")
SNAPSHOT = re.compile(r"(explore_(?:execute|conclude))-[0-9a-f]{12}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MILLISECONDS = re.compile(r"(_ms)=\d+")
WALL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def run_config(
    cli: str,
    server: str,
    token_file: Path,
    transcript_root: Path,
    *,
    execution: str = "container",
    timeout: int = 1800,
    conclude_timeout: int = 300,
    repair: bool = True,
    workers: list[dict[str, Any]] | None = None,
    common_env: dict[str, str] | None = None,
    **section: Any,
) -> DispatchConfig:
    """A dispatch.yaml with one worker of cli (or the workers given), and spec 8.3's repair: section by default."""
    payload: dict[str, Any] = {
        "server": server,
        "runtime": {
            "execution": execution,
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
            "explore": {"timeout": timeout, "conclude_timeout": conclude_timeout},
        },
        "workers": workers
        or [
            {
                "name": "w1",
                "type": cli,
                "task_types": ["explore"],
                "max_running": 2,
                "priority": 0,
                "env": {} if execution == "local" else dict(ENV[cli]),
            }
        ],
    }
    if common_env is not None:
        payload["common_env"] = common_env
    if execution == "container":
        payload["container"] = {"image": "test-image", "network_mode": "host", "completed_action": "stop"}
    if repair:
        payload["repair"] = repair_section(token_path=str(token_file), transcript_root=str(transcript_root), **section)
    return DispatchConfig.model_validate(payload)


def make_token_file(tmp_path: Path, content: str | bytes = FAKE_TOKEN, mode: int = 0o600) -> Path:
    return write_token(tmp_path / "secrets" / "repair-dispatcher.token", content, mode)


class SwitchedOnMeta(ProjectMeta):
    """A project whose Repair switch is on, whether or not this tree's ProjectMeta has the field yet (spec 6.8)."""

    repair_enabled: bool = True


def switched_on(project: ProjectDetail) -> ProjectDetail:
    data = project.project.model_dump()
    data.pop("repair_enabled", None)
    project.project = SwitchedOnMeta(**data)
    return project


def run_project(intent: Intent | None = None, *, repair: bool = True) -> ProjectDetail:
    project = make_project(intents=[intent or make_intent()])
    return switched_on(project) if repair else project


class RecordingClient(FakeClient):
    """conftest's FakeClient, recording every call the run makes, in order."""

    def __init__(self, project: ProjectDetail) -> None:
        super().__init__(project)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_project(self, project_id: str) -> ProjectDetail:
        self.calls.append(("get_project", (project_id,), {}))
        return super().get_project(project_id)

    def conclude(self, *args: Any, **kwargs: Any) -> ApiResult:
        self.calls.append(("conclude", args, kwargs))
        return super().conclude(*args, **kwargs)

    def release(self, *args: Any) -> ApiResult:
        self.calls.append(("release", args, {}))
        return super().release(*args)

    def heartbeat(self, *args: Any) -> ApiResult:
        self.calls.append(("heartbeat", args, {}))
        return super().heartbeat(*args)


class StreamingProcess:
    """Stands in for ManagedProcess: output arrives in chunks on a reader thread, into _stdout and _stderr.

    script: chunks, a list of (delay, "out" or "err", text); rc; timed_out; cancelled; start_raises;
    communicate_raises.
    """

    def __init__(self, log: list[Any], script: dict[str, Any]) -> None:
        self.log = log
        self.script = script
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._reader: threading.Thread | None = None
        self.ended_at: float | None = None

    def start(self) -> None:
        self.log.append(("process.start",))
        if self.script.get("start_raises"):
            raise RuntimeError("exec_create failed")
        self._reader = threading.Thread(target=self._stream, name="fake-exec-reader", daemon=True)
        self._reader.start()

    def _stream(self) -> None:
        for delay, stream, text in self.script.get("chunks", []):
            time.sleep(delay)
            (self._stdout if stream == "out" else self._stderr).append(text)
        self.ended_at = time.monotonic()

    def communicate(self, timeout: float | None) -> ProcessResult:
        self.log.append(("process.communicate", timeout))
        assert self._reader is not None
        self._reader.join()
        if self.script.get("communicate_raises"):
            raise RuntimeError("exec stream broke")
        return ProcessResult(
            returncode=self.script.get("rc", 0),
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self.script.get("timed_out", False),
            cancelled=self.script.get("cancelled", False),
            cancel_reason="project stopped" if self.script.get("cancelled") else None,
        )

    def kill(self) -> None:
        self.log.append(("process.kill",))

    def cancel(self, reason: str) -> None:
        self.log.append(("process.cancel", reason))


class ScriptedBackend:
    """A ContainerManager stand-in that records every call exactly as it was made: (name, args, kwargs)."""

    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.log: list[Any] = []
        self.scripts = list(scripts)
        self.processes: list[StreamingProcess] = []

    def container_name(self, *args: Any, **kwargs: Any) -> str:
        self.log.append(("container_name", args, kwargs))
        return f"cairn-dispatch-{args[0]}"

    def ensure_running(self, *args: Any, **kwargs: Any) -> str:
        self.log.append(("ensure_running", args, kwargs))
        return f"cairn-dispatch-{args[0]}"

    def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        self.log.append(("write_text_file", args, kwargs))

    def build_exec_process(self, *args: Any, **kwargs: Any) -> StreamingProcess:
        self.log.append(("build_exec_process", args, kwargs))
        script = self.scripts.pop(0)
        if script.get("build_raises"):
            raise RuntimeError("container not found: cairn-dispatch-proj_001")
        process = StreamingProcess(self.log, script)
        self.processes.append(process)
        return process


def session_lines(cli: str) -> list[tuple[float, str, str]]:
    """The output that carries the CLI's session id, cut inside the id, as a reader can see it."""
    if cli == "codex":
        return [
            (0.0, "err", "OpenAI Codex v0.118.0\n--------\nworkdir: /home/kali/workspace\nsession id: 0199a1b2-"),
            (0.02, "err", CODEX_ID.removeprefix("0199a1b2-") + "\n--------\n"),
        ]
    if cli == "pi":
        return [(0.0, "out", '{"type":"session","id":"' + PI_ID[:9]), (0.02, "out", PI_ID[9:] + '","cwd":"/w"}\n')]
    return []


def final_answer(cli: str, text: str) -> list[tuple[float, str, str]]:
    if cli == "pi":
        message = {"type": "turn_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        return [(0.01, "out", json.dumps(message) + "\n")]
    return [(0.01, "out", text)]


ENDINGS = (
    "success",
    "parse_failed_then_conclude",
    "timeout_then_conclude",
    "timeout_then_conclude_fails",
    "command_failed",
    "rejected",
    "cancelled",
    "start_raises",
    "build_raises",
)


def scripts_for(cli: str, ending: str) -> list[dict[str, Any]]:
    """The execute's script and, for endings that reach the conclude fallback, the conclude's."""
    head = session_lines(cli)
    ok = final_answer(cli, OK_JSON)
    if ending == "success":
        return [{"chunks": head + ok}]
    if ending == "parse_failed_then_conclude":
        return [{"chunks": head + final_answer(cli, "still working")}, {"chunks": ok}]
    if ending == "timeout_then_conclude":
        return [{"chunks": head, "rc": 124}, {"chunks": ok}]
    if ending == "timeout_then_conclude_fails":
        return [{"chunks": head, "rc": 124}, {"chunks": [(0.0, "err", "boom")], "rc": 1}]
    if ending == "command_failed":
        return [{"chunks": [*head, (0.0, "err", "fatal")], "rc": 2}]
    if ending == "rejected":
        return [{"chunks": head + final_answer(cli, '{"accepted":false,"reason":"policy_refusal"}')}]
    if ending == "cancelled":
        return [{"chunks": head, "rc": -15, "cancelled": True}]
    if ending == "start_raises":
        return [{"start_raises": True}]
    if ending == "build_raises":
        return [{"build_raises": True}]
    raise AssertionError(ending)


def normalize(value: Any, names: dict[str, str]) -> Any:
    """Replace what differs between two identical runs: snapshot folders, Claude Code's uuid4, milliseconds."""
    if isinstance(value, str):
        value = SNAPSHOT.sub(r"\1-<hex>", value)
        value = MILLISECONDS.sub(r"\1=<n>", value)
        return UUID.sub(lambda match: names.setdefault(match.group(0), f"<uuid{len(names)}>"), value)
    if isinstance(value, tuple):
        return tuple(normalize(item, names) for item in value)
    if isinstance(value, list):
        return [normalize(item, names) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, names) for key, item in value.items()}
    if isinstance(value, ProcessResult):
        return ProcessResult(
            returncode=value.returncode,
            stdout=normalize(value.stdout, names),
            stderr=normalize(value.stderr, names),
            timed_out=value.timed_out,
            cancelled=value.cancelled,
            cancel_reason=value.cancel_reason,
        )
    return value


def run_records(caplog: pytest.LogCaptureFixture) -> list[tuple[str, int, str]]:
    """Every record of today's dispatcher loggers: cairn.*, outside cairn.dispatcher.repair*."""
    return [
        (record.name, record.levelno, record.getMessage())
        for record in caplog.records
        if record.name.startswith("cairn.") and not record.name.startswith("cairn.dispatcher.repair")
    ]


@dataclass
class RepairCall:
    """One request of a reporter thread, as the fake repair server saw it."""

    call: str  # register | session | end | outcome
    run_id: str
    method: str
    path: str
    body: Any
    headers: dict[str, str]
    at: float


@dataclass
class FakeRepairServer:
    """Answers PUT /repair/runs/{run_id} and POST /repair/runs/{run_id}/end as the server's repair router would.

    A PUT is a session call when it sets a session_id that the run's first PUT did not have, and a register call
    (or its retry) otherwise; a POST is the outcome call when its body holds outcome, else the exit end call.
    answers maps a call to the statuses of its next requests, one per request, then the default (register 201,
    others 200); replies maps a call to a fixed reply; delay holds every request that long, unless released.
    """

    calls: list[RepairCall] = field(default_factory=list)
    answers: dict[str, list[int]] = field(default_factory=dict)
    replies: dict[str, Reply] = field(default_factory=dict)
    delay: float = 0.0
    release: threading.Event = field(default_factory=threading.Event)
    base_url: str = ""
    _first_session: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, request: RecordedRequest) -> Reply:
        path = request.path.split("?", 1)[0]
        match = RUN_PATH.fullmatch(path)
        if match is None:
            return json_reply(404, {"detail": "Not Found"})
        run_id = match.group("run_id")
        try:
            body = json.loads(request.body) if request.body else None
        except ValueError:
            body = None
        with self._lock:
            if request.method == "PUT":
                session_id = body.get("session_id") if isinstance(body, dict) else None
                first = self._first_session.setdefault(run_id, session_id)
                call = "session" if session_id is not None and session_id != first else "register"
            else:
                call = "outcome" if isinstance(body, dict) and "outcome" in body else "end"
            self.calls.append(
                RepairCall(call, run_id, request.method, request.path, body, request.headers, time.monotonic())
            )
            queued = self.answers.get(call)
            status = queued.pop(0) if queued else (201 if call == "register" else 200)
        if self.delay:
            self.release.wait(self.delay)
        if call in self.replies:
            return self.replies[call]
        if 200 <= status < 300:
            return json_reply(status, {"created": call == "register", "run": {"run_id": run_id}})
        return json_reply(status, {"detail": {"code": "test_refusal", "message": "refused"}})

    def calls_of(self, run_id: str) -> list[RepairCall]:
        with self._lock:
            return [call for call in self.calls if call.run_id == run_id]

    def kinds(self, run_id: str) -> list[str]:
        return [call.call for call in self.calls_of(run_id)]


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A reporter whose read timed out (the hanging server of DS7) has closed its socket before the answer.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


@contextmanager
def repair_server(fake: FakeRepairServer | None = None) -> Iterator[FakeRepairServer]:
    """The fake repair server on 127.0.0.1 and a free port, as repair_dispatcher_support.fake_server serves its fakes."""
    fake = fake or FakeRepairServer()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            request = RecordedRequest(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
            )
            reply = fake(request)
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)

        do_GET = do_PUT = do_POST = _handle  # noqa: N815 - BaseHTTPRequestHandler's method names

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = _QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), name="fake-repair-server", daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        fake.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class RunCapture:
    """Every ShadowRun built during a test, through repair/explore.py's _new_shadow_run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runs: list[ShadowRun | None] = []
        real = repair_explore._new_shadow_run

        def capture(*args: Any, **kwargs: Any) -> ShadowRun | None:
            run = real(*args, **kwargs)
            self.runs.append(run)
            return run

        monkeypatch.setattr(repair_explore, "_new_shadow_run", capture)

    @property
    def observed(self) -> list[ShadowRun]:
        return [run for run in self.runs if run is not None]

    def wait(self, timeout: float = 15.0) -> None:
        """Wait until every reporter thread of this test has ended."""
        deadline = time.monotonic() + timeout
        for run in self.observed:
            if run.thread is not None:
                run.thread.join(max(0.0, deadline - time.monotonic()))
                assert not run.thread.is_alive(), f"reporter of run {run.run_id} still running"

    def finish(self) -> None:
        """Let every reporter thread of this test end soon, whatever it waits for."""
        for run in self.observed:
            run.ended.set()
            run.task_done.set()
        for run in self.observed:
            if run.thread is not None:
                run.thread.join(10)


class ClientSpy:
    """Records the thread each register and end call is made on, around the real RepairRunsClient methods."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cairn.dispatcher.repair.client import RepairRunsClient

        self.threads: list[tuple[str, str]] = []
        for name in ("register", "end"):
            real = getattr(RepairRunsClient, name)

            def spy(self_: Any, run_id: str, body: dict[str, Any], _real: Callable[..., Any] = real, _name: str = name):
                self.threads.append((_name, threading.current_thread().name))
                return _real(self_, run_id, body)

            monkeypatch.setattr(RepairRunsClient, name, spy)


def records_with(caplog: pytest.LogCaptureFixture, fmt: str) -> list[logging.LogRecord]:
    """The records whose format string is fmt."""
    return [record for record in caplog.records if record.msg == fmt]
