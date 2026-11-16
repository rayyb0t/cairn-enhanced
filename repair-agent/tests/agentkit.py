"""Helpers for the review pipeline's tests (spec §9.4-§9.11, Appendix C-1). No tests here.

- `ModelStub`: a scripted model endpoint on loopback for both wire formats, after the agent prototype's
  `mock_llm.py`, answering with whole JSON bodies (never a stream), and able to trickle a body, go silent, or answer
  late, so that the read loop's rules can be checked on real sockets.
- `CairnStub`: a scripted Cairn server on loopback for the agent's three calls: `GET /repair/runs`, `GET /projects/{id}`
  and `POST /repair/runs/{run_id}/reviews`.
- `run_view` and `poll_body`: a RunView and a `GET /repair/runs` answer in §6.3's shape, with §5.4's window times.
- `FakeClock`: a monotonic clock whose `sleep` moves it.
- `Agent`: a layout's agent, started as `cairn-repair` starts it, with its loop on a chosen clock.

Every value is synthetic: made-up keys, `test-model`, loopback addresses only.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from conftest import Layout, reviewer_entry, settings_data
from records import CLAUDE, RUN_ID, fixture_bytes
from test_mirror import write_copy

from cairn_repair_agent import cli
from cairn_repair_agent.loop import RepairAgent

SERVER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PROJECT = "proj_001"
OTHER_PROJECT = "proj_002"
INTENT_ID = "i001"
INTENT = "List the configuration files of the extracted firmware."
BUDGET = 1800
# The run of claudecode-2.1.98.jsonl, whose records are at 16:56:48-16:56:49: its window opens 1350 s after the start
# (17:19:10), closes at 1440 s (17:20:40) and accepts a review until 1790 s (17:26:30).
RUN_START = datetime(2026, 11, 14, 16, 56, 40, tzinfo=UTC)
ELAPSED_AT_POLL = 1360
CHECKED_ELAPSED = 1355.0  # the copy's last check: fresh, since the floor is 1350 - 45 s
MODEL_NAME = "test-model"
SECOND_RUN_ID = "7a1b2c3d4e5f60718293a4b5c6d7e8f9"


def at(seconds: float, start: datetime = RUN_START) -> str:
    return (start + timedelta(seconds=seconds)).strftime(SERVER_TIME_FORMAT)


def run_view(
    run_id: str = RUN_ID,
    project_id: str = PROJECT,
    *,
    status: str = "open",
    state: str = "running",
    reviewable: bool = True,
    cli_type: str = "claudecode",
    budget: int = BUDGET,
    elapsed: int = ELAPSED_AT_POLL,
    number: int = 1,
    version: int = 1,
    intent_id: str = INTENT_ID,
    start: datetime = RUN_START,
) -> dict[str, Any]:
    """A RunView of GET /repair/runs (§6.3), with the window times of §5.4."""
    return {
        "run_id": run_id,
        "project_id": project_id,
        "intent_id": intent_id,
        "intent": {"description": INTENT, "from": ["f003"]},
        "worker": "w1",
        "cli_type": cli_type,
        "execution": "container",
        "session_id": None,
        "locator": {"container": "/etc/passwd", "prompt_marker": "/etc/passwd"},  # ignored by the agent (§9.5)
        "budget_seconds": budget,
        "started_at": at(0, start),
        "deadline_at": at(budget, start),
        "version": version,
        "slices_used": 0,
        "state": state,
        "elapsed_seconds": elapsed if state != "ended" else None,
        "remaining_seconds": budget - elapsed if state != "ended" else None,
        "window": {
            "number": number,
            "start_at": at(0, start),
            "end_at": at(budget, start),
            "seconds": budget,
            "review_opens_at": at(math.floor(0.75 * budget), start),
            "review_closes_at": at(math.floor(0.8 * budget), start),
            "review_accept_until": at(budget - 10, start),
            "reviewable": reviewable,
            "review_status": status,
        },
        "reviews": [],
        "registered_at": at(0, start),
        "updated_at": at(1, start),
        "ended_at": None,
        "end_reason": None,
        "last_alive_at": None,
        "outcome": None,
        "fact_id": None,
    }


def poll_body(runs: list[dict[str, Any]], server_time: str | None = None) -> dict[str, Any]:
    """An answer of GET /repair/runs (§6.3); its server time is the first run's elapsed time after its start."""
    if server_time is None:
        server_time = at(ELAPSED_AT_POLL)
    return {
        "server_time": server_time,
        "shadow": True,
        "slice_seconds": 600,
        "review_rules": {
            "open_fraction": 0.75,
            "close_fraction": 0.8,
            "accept_margin_seconds": 10,
            "min_window_seconds": 350,
            "digest_time_margin_seconds": 5,
            "reviewable_cli_types": ["claudecode", "codex", "pi"],
        },
        "runs": runs,
        "next_cursor": None,
    }


PROJECT_ANSWER = {
    "project": {"id": PROJECT, "title": "t", "status": "active"},
    "facts": [{"id": "f003", "description": "The firmware image holds a squashfs root."}],
    "intents": [{"id": INTENT_ID, "from": ["f003"], "to": None, "description": INTENT, "concluded_at": None}],
    "hints": [],
}


def write_run_copy(
    root: Path,
    run_id: str = RUN_ID,
    project_id: str = PROJECT,
    data: bytes | None = None,
    *,
    checked: float = CHECKED_ELAPSED,
    **meta: Any,
) -> Path:
    """The dispatcher's copy of a run (§8.8): claudecode-2.1.98.jsonl by default, last checked at `checked`."""
    content = fixture_bytes(CLAUDE) if data is None else data
    return write_copy(
        root,
        project_id,
        run_id,
        content,
        cli_type="claudecode",
        copied_elapsed_seconds=min(checked, 1300.0),
        checked_elapsed_seconds=checked,
        **meta,
    )


# --- answers in both formats -----------------------------------------------------------------------------------------


def decision_text(
    decision: str = "continue",
    reason: str = "The run repeats one failing command.",
    evidence: tuple[str, ...] = ("E2 the same listing failed",),
    next_step: str = "",
) -> str:
    return json.dumps({"decision": decision, "reason": reason, "evidence": list(evidence), "next_step": next_step})


EXTEND = decision_text("extend", "The listing is advancing.", ("E1 the first listing worked",), "List /etc next")
USAGE = {"prompt_tokens": 50000, "completion_tokens": 120}
MESSAGES_USAGE = {"input_tokens": 50000, "output_tokens": 120}
OMIT: Any = object()  # a keyword given OMIT leaves its key out of the answer
_MISSING: Any = object()


def chat_answer(
    content: Any = _MISSING,
    *,
    finish_reason: Any = "stop",
    model: Any = MODEL_NAME,
    usage: Any = _MISSING,
    refusal: Any = _MISSING,
) -> dict[str, Any]:
    """An `openai_chat` answer (C-1, Answers): a keyword left out gives the plain case, `None` sends null, and `OMIT`
    leaves the key out."""
    message: dict[str, Any] = {"role": "assistant", "content": decision_text() if content is _MISSING else content}
    if refusal is not _MISSING:
        message["refusal"] = refusal
    choice: dict[str, Any] = {"index": 0, "message": message}
    if finish_reason is not OMIT:
        choice["finish_reason"] = finish_reason
    body: dict[str, Any] = {"id": "chatcmpl-test", "object": "chat.completion", "choices": [choice]}
    if model is not OMIT:
        body["model"] = model
    usage = dict(USAGE) if usage is _MISSING else usage
    if usage is not OMIT:
        body["usage"] = usage
    return body


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def messages_answer(
    content: Any = _MISSING,
    *,
    stop_reason: Any = "end_turn",
    model: Any = MODEL_NAME,
    usage: Any = _MISSING,
) -> dict[str, Any]:
    """An `anthropic_messages` answer (C-1, Wire formats), with the same keywords as `chat_answer`."""
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [text_block(decision_text())] if content is _MISSING else content,
        "stop_sequence": None,
    }
    if stop_reason is not OMIT:
        body["stop_reason"] = stop_reason
    if model is not OMIT:
        body["model"] = model
    usage = dict(MESSAGES_USAGE) if usage is _MISSING else usage
    if usage is not OMIT:
        body["usage"] = usage
    return body


# --- the stub model endpoint -------------------------------------------------------------------------------------------


@dataclass
class Reply:
    """One scripted answer. `mode`: `length` (Content-Length), `chunked`, `gzip` (compressed although the request asks
    for identity), `trickle` (the headers, then one body byte every `interval` s), `trickle-gzip` (as `trickle`, the
    body gzip-compressed with a long file name in its gzip header, whose bytes decode to nothing), `trickle-gzip-close`
    (as `trickle-gzip`, with `Connection: close` and no length: the body ends with the connection, and http.client
    drops the connection's socket), `trickle-chunked` (as `trickle`, the body chunked, its chunk-size line with a long
    chunk extension, which http.client reads to its end), `late` (the headers and a declared length, then one byte
    after `interval` s, then silence), `silent` (no answer at all)."""

    status: int = 200
    body: Any = None
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    mode: str = "length"
    interval: float = 1.0
    delay: float = 0.0  # seconds before the status line
    gate: threading.Event | None = None  # the answer waits for it

    def content(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return json.dumps(chat_answer() if self.body is None else self.body).encode("utf-8")


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    at: float

    def json(self) -> Any:
        return json.loads(self.body)


STATUS_TEXT = {200: "OK", 307: "Temporary Redirect", 400: "Bad Request", 429: "Too Many Requests"}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ModelStub:
    """A loopback model endpoint for both formats: each request is recorded and answered with the next scripted reply;
    the last one repeats. `url` has no path: a reviewer's `base_url` adds `/v1` for `openai_chat`, and none for
    `anthropic_messages`."""

    def __init__(
        self,
        replies: list[Reply] | Reply | None = None,
        *,
        host: str = "127.0.0.1",
        route: Callable[[bytes], Reply | None] | None = None,
    ) -> None:
        self.replies: list[Reply] = (
            [Reply()] if replies is None else replies if isinstance(replies, list) else [replies]
        )
        self.route = route  # picks a reply from the request body, before the scripted ones
        self.requests: list[Recorded] = []
        self.closing = threading.Event()
        stub = self
        stub_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                with stub_lock:
                    stub.requests.append(
                        Recorded("POST", self.path, dict(self.headers.items()), payload, time.monotonic())
                    )
                    reply = stub.replies[min(len(stub.requests), len(stub.replies)) - 1]
                    if stub.route is not None:
                        reply = stub.route(payload) or reply
                with contextlib.suppress(OSError):
                    stub.answer(self, reply)
                self.close_connection = True

            do_GET = do_PUT = do_POST  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = _Server((host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        self.url = f"http://{host}:{self._server.server_address[1]}"

    def answer(self, handler: BaseHTTPRequestHandler, reply: Reply) -> None:
        if reply.gate is not None:
            reply.gate.wait(timeout=30)
        if reply.delay and self.closing.wait(reply.delay):
            return
        if reply.mode == "silent":
            self.closing.wait(timeout=60)
            return
        out = handler.wfile
        content = reply.content()
        head = [f"HTTP/1.1 {reply.status} {STATUS_TEXT.get(reply.status, 'Status')}", "Content-Type: application/json"]
        head += [f"{name}: {value}" for name, value in reply.headers.items()]
        if reply.mode in ("trickle-gzip", "trickle-gzip-close", "trickle-chunked"):
            if reply.mode != "trickle-chunked":
                content = gzip_with_a_name(content)
                close = reply.mode == "trickle-gzip-close"  # the body ends when the connection does
                head += ["Content-Encoding: gzip", "Connection: close" if close else f"Content-Length: {len(content)}"]
            else:
                content = b"%x;ext=%s\r\n" % (len(content), b"x" * 40) + content + b"\r\n0\r\n\r\n"
                head.append("Transfer-Encoding: chunked")
            out.write(("\r\n".join(head) + "\r\n\r\n").encode())
            out.flush()
            self.trickle(out, content, reply.interval)
            return
        if reply.mode == "gzip":
            content = gzip.compress(content)
            head.append("Content-Encoding: gzip")
        if reply.mode == "chunked":
            head.append("Transfer-Encoding: chunked")
            out.write(("\r\n".join(head) + "\r\n\r\n").encode())
            out.write(b"%x\r\n" % len(content) + content + b"\r\n0\r\n\r\n")
            out.flush()
            return
        declared = len(content) + (10 if reply.mode == "late" else 0)
        head.append(f"Content-Length: {declared}")
        out.write(("\r\n".join(head) + "\r\n\r\n").encode())
        out.flush()
        if reply.mode == "trickle":
            self.trickle(out, content, reply.interval)
            return
        if reply.mode == "late":
            if self.closing.wait(reply.interval):
                return
            out.write(content[:1])
            out.flush()
            self.closing.wait(timeout=60)
            return
        out.write(content)
        out.flush()

    def trickle(self, out: Any, content: bytes, interval: float) -> None:
        """Send `content` one byte every `interval` s, until the stub closes."""
        for byte in content:
            if self.closing.wait(interval):
                return
            out.write(bytes([byte]))
            out.flush()

    def close(self) -> None:
        self.closing.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def gzip_with_a_name(content: bytes) -> bytes:
    """`content` gzip-compressed, with a file name of 40 characters in the gzip header (its FNAME field)."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="n" * 40 + ".json", mode="wb", fileobj=buffer, mtime=0) as compressed:
        compressed.write(content)
    return buffer.getvalue()


def model_stubs() -> Iterator[Callable[..., ModelStub]]:
    """The body of a test file's `model_stub` fixture: a factory of stubs, each closed at the test's end."""
    stubs: list[ModelStub] = []

    def make(replies: list[Reply] | Reply | None = None, **options: Any) -> ModelStub:
        stubs.append(ModelStub(replies, **options))
        return stubs[-1]

    yield make
    for stub in stubs:
        stub.close()


# --- the stub Cairn server -------------------------------------------------------------------------------------------


class CairnStub:
    """A loopback Cairn server for the agent's three calls. `runs` is what GET /repair/runs lists, unless `polls`
    scripts the answers; `posts` scripts the answers to review posts (the last repeats; 201 when empty). A body given
    as bytes is sent as is; status 0 closes the connection without an answer."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.server_time: str | None = None
        self.polls: list[tuple[int, Any]] = []
        self.posts: list[tuple[int, Any] | tuple[int, Any, dict[str, str]]] = []
        self.project: tuple[int, Any] = (200, PROJECT_ANSWER)
        self.requests: list[Recorded] = []
        self.post_gate: threading.Event | None = None
        stub = self
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                with lock:
                    stub.requests.append(
                        Recorded(self.command, self.path, dict(self.headers.items()), payload, time.monotonic())
                    )
                    status, body, headers = stub.answer_for(self.command, self.path)
                if self.command == "POST" and stub.post_gate is not None:
                    stub.post_gate.wait(timeout=30)
                if status == 0:  # no answer at all: the connection closes
                    self.close_connection = True
                    return
                content = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(content)

            do_GET = do_POST = do_PUT = _handle  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def answer_for(self, method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        if method == "GET" and path.split("?")[0] == "/repair/runs":
            if self.polls:
                status, body = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
                return status, body, {}
            return 200, poll_body(self.runs, self.server_time), {}
        if method == "GET" and path.startswith("/projects/"):
            return self.project[0], self.project[1], {}
        if method == "POST" and path.startswith("/repair/runs/") and path.endswith("/reviews"):
            posted = len([r for r in self.requests if r.method == "POST"])
            if not self.posts:
                return 201, {"created": True, "mode": "shadow", "applied": False}, {}
            answer = self.posts[min(posted, len(self.posts)) - 1]
            return answer[0], answer[1], answer[2] if len(answer) > 2 else {}  # type: ignore[misc]
        return 404, {"detail": "Not Found"}, {}

    def calls(self, method: str | None = None, prefix: str = "") -> list[Recorded]:
        return [r for r in self.requests if (method is None or r.method == method) and r.path.startswith(prefix)]

    def posted(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.calls("POST", "/repair/runs/")]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def cairn_stubs() -> Iterator[CairnStub]:
    """The body of a test file's `cairn_stub` fixture: one stub server, closed at the test's end."""
    stub = CairnStub()
    yield stub
    stub.close()


# --- the clock and the agent -----------------------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that moves only when `sleep` or `advance` is called (thread-safe)."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@dataclass
class Agent:
    """A layout's agent: its startup as `cairn-repair` runs it, and its loop."""

    started: cli.StartedAgent
    loop: RepairAgent


def start_agent(layout: Layout, *extra: str, clock: FakeClock | None = None, **options: Any) -> Agent:
    """Start the layout's agent (spec §9.3 steps 1-6, `--allow-shared-user`: the layout's own isolation reasons are
    not what these tests check), and build its loop on `clock` (the real one when None)."""
    started = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user", *extra)))
    if clock is not None:
        options.setdefault("monotonic", clock.monotonic)
        options.setdefault("sleep", clock.sleep)
    return Agent(started, RepairAgent.from_started(started, **options))


def configure(
    layout: Layout,
    server: str,
    *,
    reviewers: list[dict[str, Any]] | None = None,
    projects: list[str] | None = None,
) -> None:
    """The layout's settings file: this server, these reviewers (one local one by default), these projects."""
    layout.write_settings(
        settings_data(
            server=server, projects=projects, reviewers=reviewers or [reviewer_entry(base_url="http://127.0.0.1:9/v1")]
        )
    )


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()
