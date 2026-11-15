"""Shared helpers for the repair layer's dispatcher tests (test_repair_shadow.py, test_repair_dispatcher_*.py).

Every value here is synthetic: the tokens and keys are obviously fake, and the ids are only names.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml
from conftest import make_config

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import config as repair_config

FAKE_TOKEN = "test-dispatcher-token-not-a-secret-0001"  # noqa: S105 - fake; 39 printable ASCII characters
FAKE_REVIEWER_KEY = "test-reviewer-key-not-a-secret-0002"

REPAIR_LOGGER = "cairn.dispatcher.repair"
RUN_ID = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"
OTHER_RUN_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
IDS = {"project_id": "proj_027", "intent_id": "i014", "worker": "worker-1", "run_id": RUN_ID}
VALID_ID = "0199a1b2-7c3d-4e5f-9a8b-0c1d2e3f4a5b"

# The log lines of spec 8.11 that these tests look for.
CONFIGURED_LINE = "repair shadow configured transcript_root=%s reviewers=%s projects=%s"
NOT_CONFIGURED_LINE = "repair shadow not configured, runs are not reported reason=%s"
READABLE_LINE = "repair shadow token file readable by others token_file=%s mode=%s"
SERVER_NOT_CONFIGURED_LINE = "repair shadow server not configured status=%s code=%s"
REJECTED_LINE = "repair shadow session rejected project=%s intent=%s worker=%s run=%s len=%s sha256=%s"
PROBE_FAILED_LINE = "repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s"

REGISTER_BODY = {
    "project_id": "proj_027",
    "intent_id": "i014",
    "worker": "codex-1",
    "cli_type": "codex",
    "execution": "container",
    "budget_seconds": 1800,
    "session_id": None,
    "started_seconds_ago": 0.004,
    "locator": {
        "container": "cairn-dispatch-proj_027",
        "prompt_marker": "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml",
        "dispatcher_started_at": "2026-11-14T10:00:00.123Z",
    },
}


def reset_repair_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh once-per-process state for the token loader and the client's configuration-error line."""
    monkeypatch.setattr(repair_config, "_LOADER", repair_config._Loader())
    monkeypatch.setattr(repair_client, "_SERVER_NOT_CONFIGURED", repair_client._Once())


TEST_SERVER_HOSTS = "127.0.0.1,localhost"
PROXY_VARIABLES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


def bypass_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send a test's requests to its servers on 127.0.0.1 directly, whatever proxy the shell's environment names.

    RepairRunsClient follows the environment's proxy settings (requests' default), as CairnClient does and as it
    should in production. A shell that sets HTTP_PROXY without a NO_PROXY entry for 127.0.0.1, as one that follows
    dispatch.local.example.yaml's advice to export http_proxy may, would otherwise send these requests to that
    proxy. Each test file that starts a server applies this to every test with an autouse fixture.
    """
    for name in ("NO_PROXY", "no_proxy"):  # requests reads the lower-case name first
        monkeypatch.setenv(name, TEST_SERVER_HOSTS)


def repair_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith(REPAIR_LOGGER)]


def repair_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str, tuple[Any, ...]]]:
    """(level, format, args) of each record of the repair module."""
    return [(record.levelno, str(record.msg), tuple(record.args or ())) for record in repair_records(caplog)]


def repair_error(code: str, **extra: Any) -> dict[str, Any]:
    """A repair error body, {"detail": {"code": ..., "message": ...}}."""
    return {"detail": {"code": code, "message": f"repair {code}", **extra}}


def worker_config(cli: str) -> WorkerConfig:
    return WorkerConfig.model_validate(
        {"name": "worker-1", "type": cli, "task_types": ["explore"], "max_running": 1, "priority": 0}
    )


def register_body(cli: str, session_id: str | None) -> dict[str, Any]:
    """A register body of spec 8.6 for the run of IDS."""
    return {
        "project_id": "proj_027",
        "intent_id": "i014",
        "worker": "worker-1",
        "cli_type": cli,
        "execution": "container",
        "budget_seconds": 1800,
        "session_id": session_id,
        "started_seconds_ago": 0.004,
        "locator": {"container": "cairn-dispatch-proj_027", "prompt_marker": None, "dispatcher_started_at": None},
    }


class FakeProcess:
    """The buffers a running process keeps: ManagedProcess's names, or LocalProcess's with local=True."""

    def __init__(
        self, stdout: list[str] | None = None, stderr: list[str] | None = None, *, local: bool = False
    ) -> None:
        if local:
            self._stdout_chunks = stdout or []
            self._stderr_chunks = stderr or []
        else:
            self._stdout = stdout or []
            self._stderr = stderr or []


def write_token(path: Path, content: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def token_config(token_file: Path, **section: Any) -> DispatchConfig:
    """A DispatchConfig whose repair: section reads its token from token_file."""
    return DispatchConfig.model_validate(dispatch_payload(repair_section(token_path=str(token_file), **section)))


class OpenSpy:
    """Counts os.open calls on one path, by thread name; optionally holds the read of that file."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path, *, read_delay: float = 0.0) -> None:
        self.path = str(path)
        self.openers: list[str] = []
        self.release = threading.Event()
        self.release.set()
        self.reading = threading.Event()
        self.bytes_read = 0
        self._fds: set[int] = set()
        real_open, real_read = os.open, os.read

        def spy_open(name: Any, *args: Any, **kwargs: Any) -> int:
            fd = real_open(name, *args, **kwargs)
            if os.fsdecode(name) == self.path:
                self.openers.append(threading.current_thread().name)
                self._fds.add(fd)
            return fd

        def spy_read(fd: int, size: int) -> bytes:
            if fd not in self._fds:
                return real_read(fd, size)
            self.reading.set()
            self.release.wait(10)
            time.sleep(read_delay)
            data = real_read(fd, size)
            self.bytes_read += len(data)
            return data

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "read", spy_read)


def repair_section(
    token_path: str = "/srv/cairn-test/secrets/repair-dispatcher.token",  # noqa: S107 - a path, not a secret
    **overrides: Any,
) -> dict[str, Any]:
    """Spec 8.3's example repair: section, with its placeholders replaced by synthetic values."""
    section: dict[str, Any] = {
        "token_file": token_path,
        "transcript_root": "/var/lib/cairn-repair/transcripts",
        "projects": None,
        "reviewers": [
            {
                "name": "reviewer_local",
                "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1"},
            },
            {
                "name": "reviewer_provider",
                "env": {
                    "REVIEW_MODEL": "test-model",
                    "REVIEW_BASE_URL": "https://provider.invalid/v1",
                    "REVIEW_API_KEY": FAKE_REVIEWER_KEY,
                },
                "timeout_seconds": 180,
            },
        ],
    }
    section.update(overrides)
    return section


def dispatch_payload(repair: dict[str, Any] | None = None) -> dict[str, Any]:
    """conftest's make_config payload as dispatch.yaml would hold it, with an optional repair: section."""
    payload = make_config().model_dump(exclude={"repair"})
    if repair is not None:
        payload["repair"] = copy.deepcopy(repair)
    return payload


def write_dispatch_yaml(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


Responder = Callable[[RecordedRequest], Reply]


def json_reply(status: int, payload: Any) -> Reply:
    return Reply(status, json.dumps(payload, separators=(",", ":")).encode())


@dataclass
class FakeServer:
    """A plain HTTP server on 127.0.0.1 that records every request and answers with responder."""

    responder: Responder
    requests: list[RecordedRequest] = field(default_factory=list)
    base_url: str = ""


@contextmanager
def fake_server(responder: Responder) -> Iterator[FakeServer]:
    state = FakeServer(responder=responder)

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            request = RecordedRequest(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
            )
            state.requests.append(request)
            reply = state.responder(request)
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(reply.body)))
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(reply.body)

        do_GET = do_PUT = do_POST = _handle  # noqa: N815 - BaseHTTPRequestHandler's method names

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), name="fake-repair-server", daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def serve_app(app: Any) -> Iterator[str]:
    """Serve an ASGI app with uvicorn on 127.0.0.1 and a free port; yields its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, name="uvicorn-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
