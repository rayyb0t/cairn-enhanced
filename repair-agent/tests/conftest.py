"""Shared fixtures for the repair agent's tests.

Every value here is synthetic: made-up tokens and keys, `test-model`, and loopback stub servers. Two layouts:
- `layout`: a repair.yaml, token, copy root, state folder and settings file under the test's own folder, owned by
  whoever runs the tests. The agent runs in this process.
- `host_install`: the host install of spec §13 step 4, with the dispatcher's user, the agent's user and root as
  distinct owners. The agent runs in a forked child that drops to the agent's uid, as `sudo -u cairn-repair` would,
  so the isolation check sees real permissions. It needs root and is skipped otherwise.

Each layout's repair.yaml names its own state folder, where the agent pins its reviewer (owner decision O9). Most
tests change the reviewer between two starts of the agent, which the pin would refuse, so outside the pin's own tests
(marked `reviewer_pin`) every start of the agent in this process pins its reviewer afresh.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import traceback
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

# start() imports these after its environment guard. Import them here, so that a child that has dropped its
# privileges finds them loaded and needs no read access to the virtual environment.
from cairn_repair_agent import cli, config, isolation, model, pin, server  # noqa: F401

AGENT_UID = AGENT_GID = 61001
DISPATCHER_UID = DISPATCHER_GID = 61002
OTHER_UID = OTHER_GID = 61003

IS_ROOT = os.geteuid() == 0
needs_root = pytest.mark.skipif(not IS_ROOT, reason="runs the agent as other uids, which needs root")


def fake_secret(label: str) -> str:
    """An obviously fake secret of at least 32 printable ASCII characters."""
    return f"fake-{label}-" + "0123456789abcdef" * 2


AGENT_TOKEN = fake_secret("agent")
REVIEWER_KEY = fake_secret("reviewer-key")
SECOND_REVIEWER_KEY = fake_secret("second-reviewer-key")


def reviewer_entry(name: str = "reviewer_local", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "model": "test-model",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": None,
    }
    entry.update(overrides)
    return entry


def settings_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "server": "http://127.0.0.1:8000",
        "projects": None,
        "reviewers": [reviewer_entry()],
    }
    data.update(overrides)
    return data


def spec_example_settings() -> dict[str, Any]:
    """§9.2's example, with its placeholders filled in."""
    return {
        "version": 1,
        "server": "http://127.0.0.1:8000",
        "projects": None,
        "reviewers": [
            {"name": "reviewer_local", "model": "test-model", "base_url": "http://127.0.0.1:11434/v1", "api_key": None},
            {
                "name": "reviewer_provider",
                "model": "test-model",
                "base_url": "https://provider.example/v1",
                "api_key": SECOND_REVIEWER_KEY,
                "timeout_seconds": 180,
            },
        ],
    }


def set_mode(path: Path | str, mode: int) -> None:
    """Give a test file or folder an exact mode. Several isolation cases need a permissive one on purpose."""
    os.chmod(path, mode)


def write_bytes(path: Path, content: str | bytes, *, mode: int = 0o640) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    set_mode(path, mode)
    return path


@pytest.fixture(autouse=True)
def _restore_environment() -> Iterator[None]:
    """The environment guard removes variables from os.environ: give every test the environment back."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _empty_system_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The isolation check's fixed system paths (sockets, container markers) point at an empty folder."""
    system = tmp_path_factory.mktemp("system-root")
    monkeypatch.setattr(isolation, "SYSTEM_ROOT", str(system))
    return system


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "reviewer_pin: the reviewer pin persists between starts, as in the agent")


@pytest.fixture(autouse=True)
def _each_start_pins_afresh(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the pin's own tests, each start of the agent in this process finds no pin and pins its reviewer, as a
    start does once the owner has accepted a change by removing the pin file (owner decision O9)."""
    if request.node.get_closest_marker("reviewer_pin") is not None:
        return
    real_check = pin.check_reviewer_pin

    def check_afresh(state_dir: str, reviewer: Any, endpoint: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(pin.pin_path(state_dir))
        real_check(state_dir, reviewer, endpoint)

    monkeypatch.setattr(pin, "check_reviewer_pin", check_afresh)


# --- running the agent --------------------------------------------------------------------------------------------


@dataclass
class AgentRun:
    started: bool
    records: list[tuple[str, str, str]]  # (logger, level, message)
    stderr: str
    token: str | None = None
    reviewer: str | None = None
    model_url: str | None = None

    def messages(self, prefix: str = "") -> list[str]:
        return [message for _, _, message in self.records if message.startswith(prefix)]

    def text(self) -> str:
        return "\n".join(message for _, _, message in self.records) + "\n" + self.stderr


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.name, record.levelname, record.getMessage()))


def _start(argv: list[str]) -> dict[str, Any]:
    collector = _Collector()
    package_logger = logging.getLogger("cairn_repair_agent")
    previous_level = package_logger.level
    package_logger.addHandler(collector)
    package_logger.setLevel(logging.DEBUG)
    captured = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    try:
        agent = cli.start_or_report(cli.build_parser().parse_args(argv))
    finally:
        sys.stderr = real_stderr
        package_logger.removeHandler(collector)
        package_logger.setLevel(previous_level)
    return {
        "started": agent is not None,
        "records": collector.records,
        "stderr": captured.getvalue(),
        "token": None if agent is None else agent.token,
        "reviewer": None if agent is None else agent.model.reviewer.name,
        "model_url": None if agent is None else agent.model.url,
    }


def run_in_child(fn: Callable[[], Any], uid: int, gid: int, groups: list[int]) -> Any:
    """Run `fn` in a forked child that has dropped to `uid`; return its JSON-able result."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: never returns into pytest
        try:
            os.close(read_fd)
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            payload: dict[str, Any] = {"ok": fn()}
        except BaseException:
            payload = {"error": traceback.format_exc()}
        try:
            with os.fdopen(write_fd, "wb") as out:
                out.write(json.dumps(payload).encode("utf-8"))
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as source:
        data = source.read()
    os.waitpid(pid, 0)
    payload = json.loads(data)
    if "error" in payload:
        raise AssertionError(f"the child failed:\n{payload['error']}")
    return payload["ok"]


def run_agent(argv: list[str], *, as_user: tuple[int, int, list[int]] | None = None) -> AgentRun:
    """Run the agent's startup (spec §9.3 steps 1-6) with these arguments, in this process or as another user."""
    if as_user is None:
        return AgentRun(**_start(argv))
    uid, gid, groups = as_user
    result = run_in_child(lambda: _start(argv), uid, gid, groups)
    result["records"] = [tuple(record) for record in result["records"]]
    return AgentRun(**result)


# --- layouts ------------------------------------------------------------------------------------------------------


@dataclass
class Layout:
    """A repair.yaml, its token and a copy root with a settings file, all owned by the user running the tests."""

    base: Path
    config_path: Path
    token_path: Path
    root: Path
    settings_path: Path
    dispatch_path: Path
    config: dict[str, Any] = field(default_factory=dict)
    state_dir: Path = Path()  # the agent's state folder (repair.yaml's state.dir), which holds the reviewer pin

    def write_config(self, data: dict[str, Any] | None = None, **changes: Any) -> Path:
        content = dict(self.config if data is None else data)
        content.update(changes)
        self.config = content
        return write_bytes(self.config_path, yaml.safe_dump(content, sort_keys=False), mode=0o600)

    def write_settings(self, data: Any, *, mode: int = 0o640) -> Path:
        text = data if isinstance(data, (str, bytes)) else json.dumps(data)
        if self.settings_path.is_symlink() or self.settings_path.exists():
            self.settings_path.unlink()
        return write_bytes(self.settings_path, text, mode=mode)

    def argv(self, *extra: str) -> list[str]:
        return ["--config", str(self.config_path), *extra]

    def start(self, *extra: str) -> AgentRun:
        return run_agent(self.argv(*extra))


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    root = tmp_path / "transcripts"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    set_mode(state, 0o700)
    lay = Layout(
        base=tmp_path,
        config_path=tmp_path / "repair.yaml",
        token_path=write_bytes(tmp_path / "agent.token", AGENT_TOKEN + "\n", mode=0o600),
        root=root,
        settings_path=root / config.SETTINGS_FILE_NAME,
        dispatch_path=tmp_path / "dispatch.yaml",
        state_dir=state,
    )
    lay.write_config(
        {
            "token_file": str(lay.token_path),
            "dispatch_config_to_check": str(lay.dispatch_path),
            "transcripts": {"root": str(root)},
            "state": {"dir": str(state)},
        }
    )
    lay.write_settings(settings_data())
    return lay


@dataclass
class HostInstall(Layout):
    """§13 step 4: root owns the agent's code and configuration, the agent's user its token, and the dispatcher's
    user the copy root (group: the agent's), the settings file and dispatch.yaml."""

    system: Path = Path()
    home: Path = Path()
    packages: list[Path] = field(default_factory=list)
    homes: dict[int, str] = field(default_factory=dict)

    def write_config(self, data: dict[str, Any] | None = None, **changes: Any) -> Path:
        path = super().write_config(data, **changes)
        os.chown(path, 0, AGENT_GID)
        os.chmod(path, 0o640)
        return path

    def write_settings(self, data: Any, *, mode: int = 0o640, uid: int = DISPATCHER_UID, gid: int = AGENT_GID) -> Path:
        path = super().write_settings(data, mode=mode)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        return path

    def check(self, uid: int = AGENT_UID, groups: tuple[int, ...] = (AGENT_GID,)) -> list[str]:
        config_path, root = str(self.config_path), str(self.root)

        def body() -> list[str]:
            loaded = config.load_config(config_path)
            return isolation.check(loaded, config_path, config.load_settings(root))

        return run_in_child(body, uid, groups[0], list(groups))

    def start(self, *extra: str, uid: int = AGENT_UID, groups: tuple[int, ...] = (AGENT_GID,)) -> AgentRun:
        # The state folder belongs to the user the agent runs as (§13 step 4 b), whichever user a test starts it as.
        os.chown(self.state_dir, uid, groups[0])
        return run_agent(self.argv(*extra), as_user=(uid, groups[0], list(groups)))


def _chown(path: Path, uid: int, gid: int, mode: int) -> None:
    os.chown(path, uid, gid)
    set_mode(path, mode)


@pytest.fixture
def host_install(monkeypatch: pytest.MonkeyPatch) -> Iterator[HostInstall]:
    if not IS_ROOT:
        pytest.skip("the host-install layout needs root")
    # Not under pytest's own temporary folder, which only root may enter: the agent's user must reach these paths.
    base = Path(tempfile.mkdtemp(prefix="cairn-repair-agent-test-"))
    set_mode(base, 0o755)
    try:
        system = base / "system"
        system.mkdir(mode=0o755)
        etc = base / "etc-cairn-repair"
        etc.mkdir()
        _chown(etc, 0, AGENT_GID, 0o750)
        token = write_bytes(etc / "agent.token", AGENT_TOKEN + "\n")
        _chown(token, AGENT_UID, AGENT_GID, 0o400)
        home = base / "home-dispatcher"
        home.mkdir()
        _chown(home, DISPATCHER_UID, DISPATCHER_GID, 0o755)
        dispatch = write_bytes(home / "dispatch.yaml", "server: http://127.0.0.1:8000\n")
        _chown(dispatch, DISPATCHER_UID, DISPATCHER_GID, 0o600)
        root = base / "transcripts"
        root.mkdir()
        _chown(root, DISPATCHER_UID, AGENT_GID, 0o2750)
        state = base / "state"
        state.mkdir()
        _chown(state, AGENT_UID, AGENT_GID, 0o700)
        packages = []
        for name in ("package", "purelib", "platlib"):
            folder = base / "opt" / name
            folder.mkdir(parents=True)
            set_mode(folder, 0o755)
            packages.append(folder)
        set_mode(base / "opt", 0o755)
        install = HostInstall(
            base=base,
            config_path=etc / "repair.yaml",
            token_path=token,
            root=root,
            settings_path=root / config.SETTINGS_FILE_NAME,
            dispatch_path=dispatch,
            state_dir=state,
            system=system,
            home=home,
            packages=packages,
        )
        monkeypatch.setattr(isolation, "SYSTEM_ROOT", str(system))
        monkeypatch.setattr(isolation, "package_paths", lambda: [str(path) for path in install.packages])

        def getpwuid(uid: int) -> Any:
            if uid in install.homes:
                return types.SimpleNamespace(pw_dir=install.homes[uid])
            raise KeyError(uid)

        monkeypatch.setattr(isolation, "pwd", types.SimpleNamespace(getpwuid=getpwuid))
        install.write_config(
            {
                "token_file": str(token),
                "dispatch_config_to_check": str(dispatch),
                "transcripts": {"root": str(root)},
                "state": {"dir": str(state)},
            }
        )
        install.write_settings(settings_data())
        yield install
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- stub servers -------------------------------------------------------------------------------------------------


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class StubServer:
    """A loopback HTTP server that records every request and answers with a fixed JSON body, or with `raw` bytes, and
    any extra `headers`; with `tls` (a certificate file and its key file) it serves https."""

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        host: str = "127.0.0.1",
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.requests: list[Recorded] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                stub.requests.append(Recorded(self.command, self.path, dict(self.headers.items()), payload))
                content = raw if raw is not None else json.dumps({"ok": True} if body is None else body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # a client that stops reading
                    self.wfile.write(content)

            do_GET = do_POST = do_PUT = _handle  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._server.daemon_threads = True
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        # A short poll, so that close() does not wait half a second for serve_forever to notice.
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        scheme = "http" if tls is None else "https"
        self.url = f"{scheme}://{host}:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class TcpListener:
    """Accepts connections, counts them, and closes them at once: a capturing proxy, or a TLS peer that hangs up."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.connections = 0
        self._socket = socket.socket()
        self._socket.bind((host, 0))
        self._socket.listen(16)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.port = self._socket.getsockname()[1]
        self.url = f"http://{host}:{self.port}"

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            self.connections += 1
            connection.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
        self._thread.join(timeout=5)


@pytest.fixture
def stub_server() -> Iterator[Callable[..., StubServer]]:
    servers: list[StubServer] = []

    def make(status: int = 200, body: Any = None, **options: Any) -> StubServer:
        servers.append(StubServer(status, body, **options))
        return servers[-1]

    yield make
    for stub in servers:
        stub.close()


@pytest.fixture
def tcp_listener() -> Iterator[Callable[..., TcpListener]]:
    listeners: list[TcpListener] = []

    def make(host: str = "127.0.0.1") -> TcpListener:
        listeners.append(TcpListener(host))
        return listeners[-1]

    yield make
    for listener in listeners:
        listener.close()


def file_mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)
