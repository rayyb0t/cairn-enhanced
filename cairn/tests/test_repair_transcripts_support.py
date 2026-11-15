"""Helpers for the transcript-copy tests of spec 16 (TC ids) and the settings-file tests (DS28, DS29); no tests.

The copy is driven through a real ShadowRun (repair/shadow.py) whose fields the tests set, and a real CopySchedule
(repair/transcripts.py) with a fake clock. In container mode the repair module's Docker client is replaced by
FakeDockerApi, which serves an in-memory container file system through the same calls the module makes: the archive
endpoint's HEAD and GET, exec_create and exec_start(socket=True) for find, and inspect_container. Every value here is
synthetic: the keys and tokens are obviously fake, the ids are only names, and the session files are the pinned
fixtures of cairn/tests/fixtures/repair_transcripts (made with a stub model).
"""

from __future__ import annotations

import base64
import fnmatch
import io
import json
import logging
import posixpath
import socket
import tarfile
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests
from repair_dispatcher_runs import run_config
from repair_dispatcher_support import FAKE_TOKEN, write_token

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import transcripts
from cairn.dispatcher.repair.shadow import ShadowRun
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.workers.registry import get_driver

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION_FIXTURES = FIXTURES / "repair_transcripts"
COPY_FIXTURE = FIXTURES / "repair_copy"

# The pinned fixtures' prompt marker (their graph snapshot path) and session ids.
MARKER = "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml"
OTHER_MARKER = "/tmp/cairn-prompts/explore_execute-ffffffffffff/graph.yaml"
FIXTURE_FILES = {
    "claudecode": "claudecode-2.1.98.jsonl",
    "codex": "codex-0.118.0.jsonl",
    "pi": "pi-0.73.0.jsonl",
}
SESSION_IDS = {
    "claudecode": "48478df1-3770-472f-aedd-f72acd827290",
    "codex": "01a1dafd-8482-73f3-bd0c-ea91c05a4ef5",
    "pi": "01a1dafd-8b7c-7424-b7ac-61ab282deaa4",
}
RUN_ID = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"
CONTAINER = "cairn-dispatch-proj_027"
BUDGET = 1800
START = 1000.0  # the fake monotonic time of execute_started
WALL_START = 1794650399.8  # 2026-11-14T09:59:59.8Z, the fake wall time of execute_started


def fixture_bytes(cli: str, marker: str = MARKER) -> bytes:
    """A pinned session file, with its prompt marker replaced by marker."""
    data = (SESSION_FIXTURES / FIXTURE_FILES[cli]).read_bytes()
    return data.replace(MARKER.encode(), marker.encode())


def container_session_path(cli: str, session_id: str, worker: str = "w1") -> str:
    """Where the CLI writes its session file in the worker container (spec 8.8, Locate)."""
    if cli == "claudecode":
        return f"/home/kali/.claude/projects/-home-kali-workspace/{session_id}.jsonl"
    if cli == "codex":
        return f"/home/kali/.codex/sessions/2026/11/14/rollout-2026-11-14T16-56-50-{session_id}.jsonl"
    return f"/tmp/cairn-pi/{worker}/sessions/2026-11-14T16-56-51-836Z_{session_id}.jsonl"


class FakeClock:
    """transcripts' monotonic and wall clocks, moved by the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, monotonic: float = START, wall: float = WALL_START) -> None:
        self.monotonic = monotonic
        self.wall = wall
        monkeypatch.setattr(transcripts, "_monotonic", lambda: self.monotonic)
        monkeypatch.setattr(transcripts, "_wall", lambda: self.wall)

    def at(self, elapsed: float) -> None:
        """Move both clocks to elapsed seconds after the run's execute_started."""
        self.monotonic = START + elapsed
        self.wall = WALL_START + elapsed


def copy_config(
    tmp_path: Path,
    cli: str = "codex",
    *,
    execution: str = "container",
    workers: list[dict[str, Any]] | None = None,
    common_env: dict[str, str] | None = None,
    **section: Any,
) -> DispatchConfig:
    """A dispatch.yaml with spec 8.3's repair: section, its copy root at tmp_path/copies."""
    token_file = write_token(tmp_path / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
    return run_config(
        cli,
        "http://127.0.0.1:9",
        token_file,
        tmp_path / "copies",
        execution=execution,
        timeout=BUDGET,
        workers=workers,
        common_env=common_env,
        **section,
    )


def make_run(
    config: DispatchConfig,
    *,
    cli: str | None = None,
    worker_index: int = 0,
    project_id: str = "proj_027",
    intent_id: str = "i014",
    run_id: str = RUN_ID,
    container: str | None = CONTAINER,
    budget: int = BUDGET,
    session_id: str | None = None,
    marker: str | None = MARKER,
    cancellation: TaskCancellation | None = None,
) -> ShadowRun:
    """A registered run, as the reporter thread hands it to its CopySchedule: started at START on the fake clock."""
    worker = config.workers[worker_index]
    execution = config.runtime.execution
    run = ShadowRun(
        run_id=run_id,
        config=config,
        project_id=project_id,
        intent_id=intent_id,
        worker=worker,
        execution=execution,
        driver=get_driver(worker.type, execution),
        backend=None,
        cancellation=cancellation or TaskCancellation(),
    )
    if cli is not None:
        run.cli_type = cli
    run.container = container
    run.budget_seconds = budget
    run.prompt_marker = marker
    run.started_monotonic = START
    run.started_wall = WALL_START
    run.session_id = session_id
    return run


def copy_folder(config: DispatchConfig, project_id: str = "proj_027", run_id: str = RUN_ID) -> Path:
    assert config.repair is not None
    return Path(config.repair.transcript_root) / project_id / run_id


def read_meta(config: DispatchConfig, project_id: str = "proj_027", run_id: str = RUN_ID) -> dict[str, Any]:
    return json.loads((copy_folder(config, project_id, run_id) / "meta.json").read_bytes())


def read_transcript(config: DispatchConfig, project_id: str = "proj_027", run_id: str = RUN_ID) -> bytes:
    return (copy_folder(config, project_id, run_id) / "transcript.jsonl").read_bytes()


def copy_lines(caplog: pytest.LogCaptureFixture, fmt: str) -> list[tuple[Any, ...]]:
    return [tuple(record.args or ()) for record in caplog.records if record.msg == fmt]


COPIED = "repair shadow transcript copied project=%s intent=%s run=%s bytes=%s redactions=%s copy=%s"
COPY_FAILED = "repair shadow transcript copy failed project=%s intent=%s run=%s reason=%s"
GROUP_NOT_SET = "repair shadow transcript copy group not set root_gid=%s"
TOO_LARGE_LINE = "repair shadow credential file too large, skipped file=%s bytes=%s"


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)


# A fake Docker API --------------------------------------------------------------------------------------------------

GO_SYMLINK = 1 << 27
GO_NAMED_PIPE = 1 << 25
GO_DIR = 1 << 31


@dataclass
class FakeFile:
    """A file in the fake container. kind is file, symlink, fifo or dir; size overrides the stat's size."""

    data: bytes = b""
    mtime: str = "2026-11-14T10:00:00.000000000Z"
    epoch: float = WALL_START  # its modification time, for find -newermt
    kind: str = "file"
    target: str = ""
    size: int | None = None
    member: str | None = None  # the tar member's type for a GET, when it differs from the stat (file, symlink, two)


class FakeRaw:
    """An archive response's body, read in chunks; on_chunk runs after each read."""

    def __init__(self, data: bytes, chunk: int, on_chunk: Callable[[int], None] | None) -> None:
        self._stream = io.BytesIO(data)
        self.chunk = chunk
        self.on_chunk = on_chunk
        self.reads = 0
        self.bytes_read = 0
        self.closed = False

    def read(self, size: int = -1, **_kwargs: Any) -> bytes:
        if self.closed:
            raise ValueError("read after the response was closed")
        wanted = self.chunk if size is None or size < 0 else min(size, self.chunk)
        data = self._stream.read(wanted)
        self.reads += 1
        self.bytes_read += len(data)
        if self.on_chunk is not None:
            self.on_chunk(self.reads)
        return data


@dataclass
class FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    raw: FakeRaw | None = None
    closed: bool = False

    def close(self) -> None:
        self.closed = True
        if self.raw is not None:
            self.raw.closed = True


class ExecSocket:
    """Our end of a find exec's hijacked connection: a real socket, with its reads counted and its close recorded."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock_obj = sock
        self.received = 0
        self.closed = False
        self.shut = False

    def fileno(self) -> int:
        return self._sock_obj.fileno()

    def recv(self, size: int) -> bytes:
        data = self._sock_obj.recv(size)
        self.received += len(data)
        return data

    def shutdown(self, how: int) -> None:
        self.shut = True
        self._sock_obj.shutdown(how)

    def close(self) -> None:
        self.closed = True
        self._sock_obj.close()


def frame(payload: bytes, stream: int = 1) -> bytes:
    """One frame of Docker's multiplexed exec stream."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class FakeDockerApi:
    """The calls the repair module makes on its Docker client's API, over an in-memory container file system.

    find_mode is normal (the find's output, then end of stream), silent (nothing is ever sent) or flood (frames are
    sent without end). raise_on names calls that raise a connection error; head_status overrides the stat's status;
    on_chunk runs after each chunk read of an archive GET.
    """

    base_url = "http+docker://localhost"
    api_version = "1.47"

    def __init__(self, files: dict[str, FakeFile] | None = None, *, working_dir: str = "", chunk: int = 65536) -> None:
        self.files: dict[str, FakeFile] = dict(files or {})
        self.working_dir = working_dir
        self.chunk = chunk
        self.calls: list[tuple[Any, ...]] = []
        self.responses: list[FakeResponse] = []
        self.sockets: list[ExecSocket] = []
        self.find_mode = "normal"
        self.raise_on: set[str] = set()
        self.head_status: int | None = None
        self.on_chunk: Callable[[int], None] | None = None
        self.before: Callable[[], None] | None = None  # runs before every call, e.g. to add a file
        self._commands: dict[str, list[str]] = {}
        self._peers: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.lock = threading.Lock()

    # The archive endpoint (requests.Session methods of the client).

    def head(self, url: str, params: dict[str, str] | None = None, timeout: Any = None, **kwargs: Any) -> FakeResponse:
        self._enter()
        path = (params or {})["path"]
        self.calls.append(("head", url, path, timeout, kwargs))
        if "head" in self.raise_on:
            raise requests.ConnectionError("injected: no answer")
        if self.head_status is not None:
            return self._respond(FakeResponse(self.head_status))
        found = self.files.get(path)
        if found is None:
            return self._respond(FakeResponse(404))
        mode = {"file": 0o644, "symlink": GO_SYMLINK | 0o777, "fifo": GO_NAMED_PIPE | 0o644, "dir": GO_DIR | 0o755}
        stat = {
            "name": posixpath.basename(path),
            "size": found.size if found.size is not None else len(found.data),
            "mode": mode[found.kind],
            "mtime": found.mtime,
            "linkTarget": found.target if found.kind == "symlink" else "",
        }
        header = base64.b64encode(json.dumps(stat).encode()).decode()
        return self._respond(FakeResponse(200, {"X-Docker-Container-Path-Stat": header}))

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        stream: bool = False,
        timeout: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        self._enter()
        path = (params or {})["path"]
        self.calls.append(("get", url, path, timeout, stream, dict(headers or {}), kwargs))
        if "get" in self.raise_on:
            raise requests.ConnectionError("injected: no answer")
        found = self.files.get(path)
        if found is None:
            return self._respond(FakeResponse(404))
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w", format=tarfile.PAX_FORMAT) as archive:
            kind = found.member or found.kind
            info = tarfile.TarInfo(posixpath.basename(path))
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = found.target or "/etc/passwd"
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            else:
                info.size = len(found.data)
                archive.addfile(info, io.BytesIO(found.data))
                if kind == "two":
                    second = tarfile.TarInfo("second.jsonl")
                    second.size = 4
                    archive.addfile(second, io.BytesIO(b"more"))
        return self._respond(FakeResponse(200, raw=FakeRaw(body.getvalue(), self.chunk, self.on_chunk)))

    # Execs and inspect (docker-py's APIClient methods).

    def exec_create(self, container: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self._enter()
        self.calls.append(("exec_create", container, list(cmd), kwargs))
        if "exec_create" in self.raise_on:
            raise requests.ConnectionError("injected: no answer")
        exec_id = f"exec-{len(self._commands) + 1}"
        self._commands[exec_id] = list(cmd)
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, socket: bool = False, **kwargs: Any) -> ExecSocket:
        self._enter()
        self.calls.append(("exec_start", exec_id, socket, kwargs))
        ours, peer = _socketpair()
        wrapped = ExecSocket(ours)
        self.sockets.append(wrapped)
        self._peers.append(peer)
        if self.find_mode == "normal":
            peer.sendall(frame(self._find_output(self._commands[exec_id])) + frame(b"find: x: denied\n", 2))
            peer.close()
        elif self.find_mode == "flood":
            thread = threading.Thread(target=self._flood, args=(peer,), name="fake-find-flood", daemon=True)
            thread.start()
            self._threads.append(thread)
        return wrapped

    def inspect_container(self, name: str) -> dict[str, Any]:
        self._enter()
        self.calls.append(("inspect", name))
        if "inspect" in self.raise_on:
            raise requests.ConnectionError("injected: no answer")
        return {"Config": {"WorkingDir": self.working_dir}}

    # Helpers.

    def calls_of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]

    def close(self) -> None:
        self._stop.set()
        for peer in self._peers:
            with suppress(OSError):
                peer.close()
        for thread in self._threads:
            thread.join(5)

    def _enter(self) -> None:
        if self.before is not None:
            self.before()

    def _respond(self, response: FakeResponse) -> FakeResponse:
        self.responses.append(response)
        return response

    def _flood(self, peer: socket.socket) -> None:
        block = frame(b"/home/kali/.codex/sessions/x.jsonl\n" * 1000)
        try:
            while not self._stop.is_set():
                peer.sendall(block)
        except OSError:
            return

    def _find_output(self, command: list[str]) -> bytes:
        """What find prints for this fake file system: GNU find's -maxdepth, -type f, -name, -newermt @E, -printf."""
        argv = command[command.index("find") + 1 :]
        folder = argv[0].rstrip("/")
        options = dict(zip(argv[1::2], argv[2::2], strict=False))
        depth = int(options.get("-maxdepth", "100"))
        pattern = options.get("-name", "*")
        newer = float(options["-newermt"].lstrip("@")) if "-newermt" in options else None
        lines = []
        for path, found in sorted(self.files.items()):
            if found.kind != "file" or not path.startswith(folder + "/"):
                continue
            relative = path[len(folder) + 1 :]
            if relative.count("/") + 1 > depth or not fnmatch.fnmatchcase(posixpath.basename(path), pattern):
                continue
            if newer is not None and found.epoch <= newer:
                continue
            lines.append(f"{found.epoch:.10f} {path}" if "-printf" in options else path)
        return "".join(line + "\n" for line in lines).encode()


def _socketpair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def install_docker(monkeypatch: pytest.MonkeyPatch, api: FakeDockerApi) -> FakeDockerApi:
    monkeypatch.setattr(transcripts, "docker_api", lambda: api)
    return api


def logger_records(caplog: pytest.LogCaptureFixture, level: int = logging.DEBUG) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= level]
