"""The transcript copies of reported runs (spec 8.8), and the count of runs being reported per project.

A run's reporter thread (repair/shadow.py, spec 8.5) uses this module in three places: it joins its project's
count of runs being reported when it starts (step 1) and leaves it however it ends (step 9), and once its run is
registered it drives the run's CopySchedule while the run goes on (step 3) and for the final copy after the task
has returned (step 8). The copy housekeeping of the housekeeping thread (repair/housekeeping.py) never removes the
copy folder of a project whose count is above 0.

A CopySchedule copies the session file of a run of claudecode, codex or pi whose budget is at least
MIN_REVIEWABLE_BUDGET_SECONDS: first at COPY_START_FRACTION of the budget, then at a tick every
COPY_INTERVAL_SECONDS until COPY_STOP_FRACTION of the budget plus COPY_STOP_GRACE_SECONDS, and once more after the
task has returned. Each tick finds the session file (once found, it is kept), reads its size and modification time,
and copies it only when either changed since the last good copy; an unchanged tick only rewrites meta.json with a
new checked time. Every copied byte passes through the exact-value redaction (repair/redaction.py) before it is
written to <transcript_root>/<project_id>/<run_id>/transcript.jsonl, and meta.json says what the copy is.

In container mode the file is read through Docker's archive endpoint with this module's own Docker client, and found
with find execs whose reads this module bounds itself; Docker holds the container's lock while an archive streams,
so every read runs at full speed, stops once the task is cancelled, and closes its response in a finally. In local
mode the file is opened with O_NOFOLLOW | O_NONBLOCK. Every write goes through directory descriptors opened with
O_NOFOLLOW, so no symlink below the copy root is followed, and every folder and file gets the root's group and
modes 0750 and 0640 whatever the umask.

Nothing here raises into the reporter: a failure is recorded in meta.json's status and logged once per run and
reason. No log line holds a session id, a token, a key or a transcript's text.
"""

from __future__ import annotations

import base64
import errno
import glob
import hashlib
import itertools
import json
import logging
import os
import posixpath
import re
import select
import socket
import stat
import tarfile
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from cairn.dispatcher.repair import redaction
from cairn.dispatcher.repair.session_ids import valid_project_id, valid_run_id, valid_session_id

LOG = logging.getLogger(__name__)

# The schedule (spec 8.8). From Phase 3 it follows the current window instead.
COPY_START_FRACTION = 0.70
COPY_INTERVAL_SECONDS = 15
COPY_STOP_FRACTION = 0.80
COPY_STOP_GRACE_SECONDS = 15
# TC15 measured the 1 s bound of spec 8.8 with these two on a real daemon (Docker 29.9.2): a copy of 64 MiB holds the
# container's lock for about 0.25 s.
COPY_MAX_BYTES = 64 * 1024 * 1024
COPY_TOTAL_SECONDS = 30
MIN_REVIEWABLE_BUDGET_SECONDS = 350  # shorter budgets are never reviewed (owner decision O4, Q5)
COPIED_CLIS = frozenset({"claudecode", "codex", "pi"})

# Locate.
FIND_READ_SECONDS = 5
FIND_MAX_BYTES = 1024 * 1024
MARKER_MAX_LINES = 50
MARKER_MAX_BYTES = 1024 * 1024
CANDIDATES_MAX = 10
CANDIDATE_SLACK_SECONDS = 60  # candidates found by marker were modified after the run's start minus this
CANDIDATE_MAX_DEPTH = 4

# Docker.
DOCKER_TIMEOUT_SECONDS = 10
ARCHIVE_TIMEOUT_SECONDS = (5, 10)  # connect, read
READ_CHUNK_BYTES = 64 * 1024
TAR_SLACK_BYTES = 64 * 1024  # a tar stream's headers, PAX records and padding around its one member
SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
CONTAINER_HOME = "/home/kali"  # the worker image's user (container/Dockerfile)
CONTAINER_WORKDIR = "/home/kali/workspace"  # the image's WORKDIR; exec_create passes none
PI_ROOT = "/tmp/cairn-pi"  # workers/adapters/pi.py: /tmp/cairn-pi/<worker name>/sessions, also on a host

# Go's os.FileMode bits in the archive endpoint's stat, any of which makes the file something other than a
# regular file: directory, symlink, device, named pipe, socket, character device and irregular.
GO_MODE_NOT_REGULAR = (1 << 31) | (1 << 27) | (1 << 26) | (1 << 25) | (1 << 24) | (1 << 21) | (1 << 19)

# The copy folder.
TRANSCRIPT_NAME = "transcript.jsonl"
META_NAME = "meta.json"
SETTINGS_NAME = "agent-settings.json"
TEMPORARY_SUFFIX = ".tmp"
COPY_FILE_NAMES = frozenset(
    {TRANSCRIPT_NAME, META_NAME, TRANSCRIPT_NAME + TEMPORARY_SUFFIX, META_NAME + TEMPORARY_SUFFIX}
)
RUN_FOLDER_RULE = re.compile(r"[0-9a-f]{32}")  # the run ids this dispatcher makes: uuid4().hex
FOLDER_MODE = 0o750
FILE_MODE = 0o640

# meta.json's status values. not_found: the session file was not found by the run's session id (or a find hit its
# limits, or the file has gone); no_session: the run's session id is not known yet and no file was found by the run's
# prompt marker (claudecode's id, from its argv, is always known).
OK = "ok"
NOT_FOUND = "not_found"
NO_SESSION = "no_session"
TOO_LARGE = "too_large"
NOT_REGULAR = "not_regular"
ERROR = "error"


def _monotonic() -> float:
    """The clock of the schedule and of meta.json's elapsed times; the I/O bounds use time.monotonic() itself."""
    return time.monotonic()


def _wall() -> float:
    return time.time()


# The count of runs being reported in this process, per project id (spec 8.8, Removal). Reporter threads join and
# leave it under this lock, and the copy housekeeping holds the same lock while it checks the count and removes a
# project's folder, so that no reporter thread writes into a folder that is being removed. It is reentrant, so that
# a thread that holds it may also call reporting_count.
REPORTING_LOCK = threading.RLock()
_reporting: dict[str, int] = {}


def join_reporting(project_id: str) -> None:
    """A reporter thread of project_id has started (spec 8.5 step 1)."""
    with REPORTING_LOCK:
        _reporting[project_id] = _reporting.get(project_id, 0) + 1


def leave_reporting(project_id: str) -> None:
    """A reporter thread of project_id that joined has ended (spec 8.5 step 9)."""
    with REPORTING_LOCK:
        count = _reporting.get(project_id, 0) - 1
        if count > 0:
            _reporting[project_id] = count
        else:
            _reporting.pop(project_id, None)


def reporting_count(project_id: str) -> int:
    """How many reporter threads of project_id are running in this process."""
    with REPORTING_LOCK:
        return _reporting.get(project_id, 0)


# Docker -----------------------------------------------------------------------------------------------------------

_docker_lock = threading.Lock()
_docker_client: Any = None


def docker_api() -> Any:
    """The low-level API of the repair module's own Docker client, docker.from_env(timeout=10), made on first use.

    One client per process, shared by the reporter threads as ContainerManager's is shared by the task threads; the
    repair module never uses ContainerManager's private attributes. Local mode never calls this.
    """
    global _docker_client
    with _docker_lock:
        if _docker_client is None:
            import docker

            _docker_client = docker.from_env(timeout=DOCKER_TIMEOUT_SECONDS)
        return _docker_client.api


class _CancelledError(Exception):
    """The task was cancelled: no further locate, stat, copy or meta.json write for this run."""


class _NoAnswerError(Exception):
    """Docker gave no answer (a timeout, a connection error or a 5xx): this tick writes nothing."""


class _RefusedError(Exception):
    """The copy failed with a status of meta.json."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _docker_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _no_answer(exc: BaseException) -> bool:
    """Whether a Docker call that raised exc got no answer: a transport error, or a 5xx."""
    if isinstance(exc, requests.RequestException):
        return True
    status = _docker_status(exc)
    if status is None:
        return type(exc).__module__.startswith("docker")  # e.g. DockerException: the daemon cannot be reached
    return status >= 500


class _GuardedStream:
    """An archive response's raw stream, for tarfile: every read first checks the task's cancellation and the copy's
    deadline, and the stream is read at most limit bytes far."""

    def __init__(self, raw: Any, cancellation: Any, deadline: float, limit: int) -> None:
        self._raw = raw
        self._cancellation = cancellation
        self._deadline = deadline
        self._limit = limit
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if self._cancellation.is_cancelled:
            raise _CancelledError
        if time.monotonic() > self._deadline:
            raise _RefusedError(ERROR)
        wanted = READ_CHUNK_BYTES if size is None or size < 0 else size
        wanted = min(wanted, self._limit + 1 - self.read_bytes)
        if wanted <= 0:
            raise _RefusedError(TOO_LARGE)
        data = self._raw.read(wanted)
        self.read_bytes += len(data)
        if self.read_bytes > self._limit:
            raise _RefusedError(TOO_LARGE)
        return data

    # The rest of tarfile's file object protocol, which a stream ("r|") never uses: it only reads.

    def write(self, data: bytes) -> int:
        raise OSError("an archive response is read only")

    def tell(self) -> int:
        return self.read_bytes

    def seek(self, position: int) -> int:
        raise OSError("an archive response cannot seek")

    def close(self) -> None:
        return None  # the response is closed by its reader, in a finally


# Writes below the copy root ---------------------------------------------------------------------------------------


class UnsafePathError(Exception):
    """A symlink, or anything but a directory, where the copy root or one of its folders should be."""


_group_lock = threading.Lock()
_group_not_set_logged = False


def open_root(root: str) -> int:
    """A descriptor of the copy root, created with mode 0750 if it is missing; an existing root is never changed.

    Opened with O_RDONLY | O_DIRECTORY | O_NOFOLLOW: a root whose last component is a symlink, or that is not a
    directory, raises UnsafePathError.
    """
    created = False
    try:
        os.makedirs(root, FOLDER_MODE)
        created = True
    except FileExistsError:
        pass
    fd = _open_directory(root)
    if created:
        os.fchmod(fd, FOLDER_MODE)  # the mode makedirs gave it, whatever the umask
    return fd


def open_folder(parent_fd: int, name: str, gid: int) -> int:
    """A descriptor of the folder name in parent_fd, created if missing with mode 0750 and the root's group."""
    created = False
    try:
        os.mkdir(name, FOLDER_MODE, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    fd = _open_directory(name, parent_fd)
    if created:
        try:
            set_group(fd, gid)
            os.fchmod(fd, FOLDER_MODE)
        except BaseException:
            os.close(fd)
            raise
    return fd


def _open_directory(name: str, parent_fd: int | None = None) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(name) from exc
        raise


def write_file(folder_fd: int, name: str, data: bytes, gid: int) -> None:
    """name in folder_fd, replaced atomically: written to name.tmp (mode 0640, the root's group), fsynced, then moved
    over name with os.replace, which replaces a symlink at name rather than follow it. A name.tmp left by a failed
    write is removed first, never followed."""
    temporary = name + TEMPORARY_SUFFIX
    with suppress(FileNotFoundError):
        os.unlink(temporary, dir_fd=folder_fd)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE, dir_fd=folder_fd)
    try:
        set_group(fd, gid)
        os.fchmod(fd, FILE_MODE)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)


def set_group(fd: int, gid: int) -> None:
    """Give fd the copy root's group; without the right to (the dispatcher's user is not in that group), log once
    per process and keep the dispatcher's group, which the agent's user then cannot read (spec 8.8, Write)."""
    global _group_not_set_logged
    try:
        os.fchown(fd, -1, gid)
    except PermissionError:
        with _group_lock:
            first = not _group_not_set_logged
            _group_not_set_logged = True
        if first:
            LOG.warning("repair shadow transcript copy group not set root_gid=%s", gid)


# Locate -----------------------------------------------------------------------------------------------------------


def claude_project_slug(cwd: str) -> str:
    """Claude Code 2.1.98's folder name for a working directory: every character outside [a-zA-Z0-9] becomes -."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def holds_marker(head: bytes, marker: str | None, cli_type: str) -> bool:
    """Whether the run's prompt marker is in a user message before the first tool call, in the first 50 lines.

    head is at most the first MARKER_MAX_BYTES of a session file. It is decoded and split into lines as the agent
    reads a copy (spec 8.8 Locate, 9.5): as UTF-8 with replacement, at \\n only, never with str.splitlines, which also
    ends a line at U+2028, U+2029, U+0085 and other characters that the CLIs write raw inside a JSON string, such as
    the intent's text in the prompt; an empty piece, such as the one after the last \\n, is skipped and not counted.
    Each line is parsed with json.loads and examined inside one guard: a line that does not parse (a half-written
    line, or one nested deeper than json.loads can go), that is not an object, or that holds a field of a type its CLI
    does not write there, is not a user message, and still counts toward the 50 lines. Tool output is never a user
    message, so another run's file that only mentions the marker in a tool's output is not taken for this run's.
    """
    if not marker:
        return False
    text = head[:MARKER_MAX_BYTES].decode("utf-8", "replace")
    for line in itertools.islice((piece for piece in text.split("\n") if piece), MARKER_MAX_LINES):
        try:
            verdict = _examine(line, marker, cli_type)
        except Exception:  # noqa: S112 - no line's content may raise out of the check
            continue
        if verdict is not None:
            return verdict
    return False


def _examine(line: str, marker: str, cli_type: str) -> bool | None:
    """True for a user message that holds the marker, False at the first tool call, None for any other line."""
    record = json.loads(line)
    if not isinstance(record, dict):
        return None
    if cli_type == "claudecode":
        return _examine_claude(record, marker)
    if cli_type == "codex":
        return _examine_codex(record, marker)
    if cli_type == "pi":
        return _examine_pi(record, marker)
    return None


def _examine_claude(record: dict[str, Any], marker: str) -> bool | None:
    kind = record.get("type")
    message = record.get("message")
    if kind == "assistant":
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(_block_type(block) == "tool_use" for block in content):
            return False
        return None
    if kind != "user" or not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return True if marker in content else None
    if not isinstance(content, list) or any(_block_type(block) == "tool_result" for block in content):
        return None
    texts = [block.get("text") for block in content if _block_type(block) == "text"]
    return True if any(isinstance(text, str) and marker in text for text in texts) else None


def _examine_codex(record: dict[str, Any], marker: str) -> bool | None:
    kind = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if kind == "response_item":
        payload_type = payload.get("type")
        if payload_type == "function_call":
            return False
        if payload_type != "message" or payload.get("role") != "user":
            return None
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        texts = [block.get("text") for block in content if _block_type(block) == "input_text"]
        return True if any(isinstance(text, str) and marker in text for text in texts) else None
    if kind == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        return True if isinstance(message, str) and marker in message else None
    return None


def _examine_pi(record: dict[str, Any], marker: str) -> bool | None:
    message = record.get("message")
    if record.get("type") != "message" or not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    if role == "assistant":
        if isinstance(content, list) and any(_block_type(block) == "toolCall" for block in content):
            return False
        return None
    if role != "user":
        return None
    if isinstance(content, str):
        return True if marker in content else None
    if not isinstance(content, list):
        return None
    texts = [block.get("text") for block in content if _block_type(block) == "text"]
    return True if any(isinstance(text, str) and marker in text for text in texts) else None


def _block_type(block: Any) -> Any:
    return block.get("type") if isinstance(block, dict) else None


@dataclass(frozen=True, slots=True)
class _Source:
    path: str
    located_by: str  # session_id, prompt_marker or path


@dataclass(frozen=True, slots=True)
class _Stat:
    size: int
    identity: tuple[Any, ...]  # what must change for the file to be copied again: its size and modification time


# The schedule -----------------------------------------------------------------------------------------------------


class CopySchedule:
    """The transcript copies of one registered run (spec 8.8, Schedule), driven by its reporter thread.

    run is the run's ShadowRun, from which the copy reads what it needs (its ids, worker, execution, budget,
    container or work folder, prompt marker, session id once known, start time and cancellation); token is the
    dispatcher token, which the copy's redaction removes by exact value.

    Its methods handle their own failures (spec 8.8 records them in meta.json's status). Should one raise anyway,
    or the constructor, the reporter logs it once and stops the run's copies, and its requests go on.

    A run is copied only when its CLI is claudecode, codex or pi and its budget is at least
    MIN_REVIEWABLE_BUDGET_SECONDS; for any other run next_due() is None and final_copy() does nothing. The secret
    values are collected here, on the reporter thread, when the run is first observed, and again before each copy.
    """

    def __init__(self, run: Any, token: str) -> None:
        self.run = run
        self._token = token
        self._index = 0  # the next tick: COPY_START_FRACTION of the budget plus this many intervals
        self._done = False
        self._source: _Source | None = None
        self._source_bytes: int | None = None
        self._good_identity: tuple[Any, ...] | None = None
        self._good_meta: dict[str, Any] | None = None
        self._copies = 0
        self._cwd: str | None = None
        self._secrets: dict[str, str] = {}
        self._failures_logged: set[str] = set()
        self._active = self._applies()
        if self._active:
            self._collect_secrets()

    # The reporter's interface. ---------------------------------------------------------------------------------------

    def next_due(self) -> float | None:
        """The time.monotonic() value at which the next tick is due, or None when no tick is left."""
        if not self._active or self._done:
            return None
        return self._slot(self._index)

    def tick(self) -> None:
        """One tick of the schedule, called once it is due: a copy, a checked-time rewrite of meta.json, or nothing."""
        if not self._active or self._done:
            return
        try:
            self._attempt(final=False)
        finally:
            now = _monotonic()
            while (slot := self._slot(self._index)) is not None and slot <= now:
                self._index += 1  # a tick that ran long skips the ticks it covered

    def final_copy(self) -> None:
        """The final copy, after the task has returned and only if it was not cancelled (spec 8.5 step 8).

        The schedule itself checks the rest of step 8's condition: the run reached 70% of its budget and is reviewable.
        """
        if not self._active or self._done:
            return
        try:
            if self.run.cancellation.is_cancelled or not self._reached_start():
                return
            self._attempt(final=True)
        finally:
            self._done = True

    # The schedule. ---------------------------------------------------------------------------------------------------

    def _applies(self) -> bool:
        run = self.run
        budget = run.budget_seconds
        return (
            run.cli_type in COPIED_CLIS
            and run.execution in ("container", "local")
            and isinstance(budget, int)
            and budget >= MIN_REVIEWABLE_BUDGET_SECONDS
            and run.started_monotonic is not None
            and run.config.repair is not None
        )

    def _slot(self, index: int) -> float | None:
        budget = self.run.budget_seconds
        offset = COPY_START_FRACTION * budget + COPY_INTERVAL_SECONDS * index
        if offset > COPY_STOP_FRACTION * budget + COPY_STOP_GRACE_SECONDS + 1e-6:
            return None
        return self.run.started_monotonic + offset

    def _reached_start(self) -> bool:
        run = self.run
        end = run.exited_monotonic if run.exited_monotonic is not None else _monotonic()
        return end - run.started_monotonic >= COPY_START_FRACTION * run.budget_seconds - 1e-6

    def _attempt(self, *, final: bool) -> None:
        try:
            self._copy(final)
        except _CancelledError:
            self._done = True  # the previous good copy and its meta.json stay as they were
        except _NoAnswerError:
            pass  # no new status, so nothing is written
        except Exception:
            self._fail(ERROR, final)

    def _copy(self, final: bool) -> None:
        run = self.run
        if not valid_project_id(run.project_id) or not valid_run_id(run.run_id):
            # A run id always passes (uuid4().hex, and the reporter registers no other), so this is the project id.
            self._log_failure("bad_project_id" if not valid_project_id(run.project_id) else ERROR)
            self._done = True
            return
        self._check_cancelled()
        located = self._locate()
        if isinstance(located, str):
            self._fail(located, final)
            return
        self._check_cancelled()
        try:
            if run.execution == "container":
                self._copy_from_container(located, final)
            else:
                self._copy_local(located, final)
        except _RefusedError as refused:
            self._fail(refused.status, final)

    def _check_cancelled(self) -> None:
        if self.run.cancellation.is_cancelled:
            raise _CancelledError

    def _collect_secrets(self) -> None:
        """Add the secret values in effect now; the run keeps every value it has read (tokens rotate)."""
        run = self.run
        for value, name in redaction.secret_values(run.config, run.execution, os.environ, self._token).items():
            self._secrets.setdefault(value, name)

    # Locate. ---------------------------------------------------------------------------------------------------------

    def _locate(self) -> _Source | str:
        """The run's session file, found once and then kept; or not_found or no_session for this tick."""
        if self._source is not None:
            return self._source
        session_id = self.run.session_id if valid_session_id(self.run.session_id) else None
        if self.run.execution == "container":
            found = self._locate_in_container(session_id)
        else:
            found = self._locate_on_host(session_id)
        if isinstance(found, _Source):
            self._source = found
        return found

    def _locate_in_container(self, session_id: str | None) -> _Source | str:
        run = self.run
        api = self._api()
        marker = run.prompt_marker
        if session_id is None:
            if run.cli_type == "claudecode":
                return NO_SESSION
            since = int((run.started_wall or _wall()) - CANDIDATE_SLACK_SECONDS)
            argv = ["find", self._container_sessions(), "-maxdepth", str(CANDIDATE_MAX_DEPTH), "-type", "f"]
            argv += ["-name", "*.jsonl", "-newermt", f"@{since}", "-printf", "%T@ %p\\n"]
            lines = self._find(api, argv)
            if lines is None:
                return NOT_FOUND
            stamped: list[tuple[float, str]] = []
            for line in lines:
                stamp, _, path = line.partition(" ")
                with suppress(ValueError):
                    if posixpath.isabs(path):
                        stamped.append((float(stamp), path))
            stamped.sort(reverse=True)
            for _stamp, path in stamped[:CANDIDATES_MAX]:
                if holds_marker(self._container_head(api, path), marker, run.cli_type):
                    return _Source(path, "prompt_marker")
            return NO_SESSION
        exact = None
        if run.cli_type == "claudecode":
            config_dir = self._container_directory(run.worker.env.get("CLAUDE_CONFIG_DIR"), CONTAINER_HOME + "/.claude")
            exact = f"{config_dir}/projects/{claude_project_slug(self._container_cwd(api))}/{session_id}.jsonl"
            if holds_marker(self._container_head(api, exact), marker, run.cli_type):
                return _Source(exact, "path")
            argv = ["find", f"{config_dir}/projects", "-maxdepth", "2", "-type", "f", "-name", f"{session_id}.jsonl"]
        elif run.cli_type == "codex":
            argv = ["find", self._container_sessions(), "-maxdepth", "4", "-type", "f"]
            argv += ["-name", f"rollout-*-{session_id}.jsonl"]
        else:
            argv = [
                "find",
                self._container_sessions(),
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-name",
                f"*_{session_id}.jsonl",
            ]
        lines = self._find(api, argv)
        if lines is None:
            return NOT_FOUND
        candidates = sorted({line for line in lines if posixpath.isabs(line) and line != exact})
        for path in candidates[:CANDIDATES_MAX]:
            if holds_marker(self._container_head(api, path), marker, run.cli_type):
                return _Source(path, "session_id")
        return NOT_FOUND

    def _container_sessions(self) -> str:
        """The folder the run's CLI keeps its session files in, inside the container (codex or pi)."""
        run = self.run
        if run.cli_type == "codex":
            return self._container_directory(run.worker.env.get("CODEX_HOME"), CONTAINER_HOME + "/.codex") + "/sessions"
        return posixpath.join(PI_ROOT, run.worker_name, "sessions")

    @staticmethod
    def _container_directory(value: str | None, default: str) -> str:
        if not value:
            return default
        if value == "~" or value.startswith("~/"):
            value = CONTAINER_HOME + value[1:]
        return posixpath.normpath(posixpath.join(CONTAINER_WORKDIR, value))

    def _container_cwd(self, api: Any) -> str:
        """The container's Config.WorkingDir, the CLI's cwd (exec_create passes no workdir): one inspect per run."""
        if self._cwd is None:
            cwd = CONTAINER_WORKDIR
            try:
                info = api.inspect_container(self.run.container)
                value = info.get("Config", {}).get("WorkingDir") if isinstance(info, dict) else None
                if isinstance(value, str) and posixpath.isabs(value):
                    cwd = value
            except Exception as exc:
                if _no_answer(exc):
                    raise _NoAnswerError from exc
            self._cwd = cwd
        return self._cwd

    def _find(self, api: Any, argv: list[str]) -> list[str] | None:
        """The stdout lines of one find exec in the run's container; None when the read hit a limit.

        The exec has no shell and no environment of its own. Its output is read on this thread with exec_start's
        raw socket, at most FIND_READ_SECONDS in total and at most FIND_MAX_BYTES of frames, waiting with poll() and a
        timeout, never with docker-py's own read, whose poll() has none; then the socket is closed. The bound never
        depends on a binary in the container: timeout -k 1 5 before find is only a second guard.
        """
        self._check_cancelled()
        command = ["timeout", "-k", "1", str(FIND_READ_SECONDS), *argv]
        try:
            exec_id = api.exec_create(self.run.container, command, stdout=True, stderr=True, stdin=False, tty=False)
            sock = api.exec_start(exec_id["Id"], socket=True)
        except Exception as exc:
            if _no_answer(exc):
                raise _NoAnswerError from exc
            return []  # e.g. the container is gone: nothing found
        try:
            data = _read_frames(sock)
        finally:
            _close_socket(sock)
        if data is None:
            return None
        return [line for line in data.decode("utf-8", "replace").split("\n") if line]

    def _container_head(self, api: Any, path: str) -> bytes:
        """At most the first MARKER_MAX_BYTES of a regular file in the container, through the archive endpoint; the
        response is closed afterwards. Empty for a missing or irregular file."""
        self._check_cancelled()
        response = self._archive(api, "get", path, stream=True)
        try:
            if response.status_code != 200:
                if response.status_code >= 500:
                    raise _NoAnswerError
                return b""
            reader = _GuardedStream(
                response.raw,
                self.run.cancellation,
                time.monotonic() + COPY_TOTAL_SECONDS,
                COPY_MAX_BYTES + TAR_SLACK_BYTES,
            )
            try:
                with tarfile.open(fileobj=reader, mode="r|", bufsize=READ_CHUNK_BYTES) as archive:
                    member = archive.next()
                    if member is None or not member.isreg():
                        return b""
                    source = archive.extractfile(member)
                    return b"" if source is None else source.read(MARKER_MAX_BYTES)
            except _CancelledError:
                raise
            except Exception:  # a refused, broken or timed-out stream: this candidate is not taken
                return b""
        finally:
            response.close()

    def _locate_on_host(self, session_id: str | None) -> _Source | str:
        run = self.run
        marker = run.prompt_marker
        env = {**os.environ, **run.worker.env}
        home = env.get("HOME") or None
        if session_id is None:
            if run.cli_type == "claudecode":
                return NO_SESSION
            folder = self._host_sessions(env, home)
            if folder is None:
                return NO_SESSION
            since = (run.started_wall or _wall()) - CANDIDATE_SLACK_SECONDS
            for path in _recent_session_files(folder, since):
                if holds_marker(_host_head(path), marker, run.cli_type):
                    return _Source(path, "prompt_marker")
            return NO_SESSION
        exact = None
        if run.cli_type == "claudecode":
            config_dir = _host_directory(env.get("CLAUDE_CONFIG_DIR"), home, ".claude")
            if config_dir is None:
                return NOT_FOUND
            if isinstance(run.container, str):
                cwd = os.path.realpath(run.container)  # the CLI sees its physical cwd
                exact = os.path.join(config_dir, "projects", claude_project_slug(cwd), f"{session_id}.jsonl")
                if holds_marker(_host_head(exact), marker, run.cli_type):
                    return _Source(exact, "path")
            pattern = os.path.join(glob.escape(config_dir), "projects", "*", f"{session_id}.jsonl")
        else:
            folder = self._host_sessions(env, home)
            if folder is None:
                return NOT_FOUND
            if run.cli_type == "codex":
                pattern = os.path.join(glob.escape(folder), "*", "*", "*", f"rollout-*-{session_id}.jsonl")
            else:
                pattern = os.path.join(glob.escape(folder), f"*_{session_id}.jsonl")
        candidates = sorted(path for path in glob.glob(pattern) if path != exact)
        for path in candidates[:CANDIDATES_MAX]:
            if holds_marker(_host_head(path), marker, run.cli_type):
                return _Source(path, "session_id")
        return NOT_FOUND

    def _host_sessions(self, env: dict[str, str], home: str | None) -> str | None:
        run = self.run
        if run.cli_type == "codex":
            codex_home = _host_directory(env.get("CODEX_HOME"), home, ".codex")
            return None if codex_home is None else os.path.join(codex_home, "sessions")
        return os.path.join(PI_ROOT, run.worker_name, "sessions")

    # Copy. -----------------------------------------------------------------------------------------------------------

    def _copy_from_container(self, source: _Source, final: bool) -> None:
        api = self._api()
        stat_result = self._container_stat(api, source.path)
        now = (_monotonic(), _wall())  # the stat's time
        self._source_bytes = stat_result.size
        if stat_result.identity == self._good_identity:
            self._checked(now, final)
            return
        self._check_cancelled()
        self._collect_secrets()
        data = self._container_read(api, source.path)
        self._store(data, stat_result, source, now, final)

    def _container_stat(self, api: Any, path: str) -> _Stat:
        """The archive endpoint's stat: a regular file within the cap, or _RefusedError; _NoAnswerError for no answer."""
        self._check_cancelled()
        response = self._archive(api, "head", path)
        try:
            status = response.status_code
            if status >= 500:
                raise _NoAnswerError
            if status == 404:
                raise _RefusedError(NOT_FOUND)
            if status != 200:
                raise _RefusedError(ERROR)
            header = response.headers.get("X-Docker-Container-Path-Stat")
            try:
                info = json.loads(base64.b64decode(header or "", validate=False))
            except (ValueError, RecursionError) as exc:
                raise _RefusedError(ERROR) from exc
        finally:
            response.close()
        if not isinstance(info, dict):
            raise _RefusedError(ERROR)
        size, mode, link = info.get("size"), info.get("mode"), info.get("linkTarget")
        if not isinstance(size, int) or isinstance(size, bool) or not isinstance(mode, int) or isinstance(mode, bool):
            raise _RefusedError(ERROR)
        if link or mode & GO_MODE_NOT_REGULAR:
            raise _RefusedError(NOT_REGULAR)
        if size > COPY_MAX_BYTES:
            self._source_bytes = size
            raise _RefusedError(TOO_LARGE)
        return _Stat(size=size, identity=(size, info.get("mtime")))

    def _container_read(self, api: Any, path: str) -> bytes:
        """The file's bytes through the archive endpoint, at full speed: one regular member within the cap, within
        COPY_TOTAL_SECONDS; the task's cancellation is checked between the chunk reads, and the response is closed in
        any case, which releases Docker's container lock."""
        self._check_cancelled()
        response = self._archive(api, "get", path, stream=True)
        try:
            if response.status_code == 404:
                raise _RefusedError(NOT_FOUND)
            if response.status_code != 200:
                raise _RefusedError(ERROR)
            reader = _GuardedStream(
                response.raw,
                self.run.cancellation,
                time.monotonic() + COPY_TOTAL_SECONDS,
                COPY_MAX_BYTES + TAR_SLACK_BYTES,
            )
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES) as spool:
                try:
                    with tarfile.open(fileobj=reader, mode="r|", bufsize=READ_CHUNK_BYTES) as archive:
                        member = archive.next()
                        if member is None or not member.isreg():
                            raise _RefusedError(NOT_REGULAR)
                        if member.size > COPY_MAX_BYTES:
                            raise _RefusedError(TOO_LARGE)
                        source = archive.extractfile(member)
                        if source is None:
                            raise _RefusedError(NOT_REGULAR)
                        while chunk := source.read(READ_CHUNK_BYTES):
                            spool.write(chunk)
                        if archive.next() is not None:
                            raise _RefusedError(ERROR)  # exactly one member
                except (_CancelledError, _RefusedError):
                    raise
                except Exception as exc:  # a broken stream, a read timeout, a tar error
                    raise _RefusedError(ERROR) from exc
                spool.seek(0)
                return spool.read()
        finally:
            response.close()

    def _archive(self, api: Any, method: str, path: str, *, stream: bool = False) -> Any:
        """A request to the container's archive endpoint with the client's own requests.Session methods (docker-py's
        get_archive disables the socket timeout)."""
        url = f"{api.base_url}/v{api.api_version}/containers/{quote(str(self.run.container), safe='')}/archive"
        try:
            if method == "head":
                return api.head(url, params={"path": path}, timeout=ARCHIVE_TIMEOUT_SECONDS)
            return api.get(
                url,
                params={"path": path},
                stream=stream,
                timeout=ARCHIVE_TIMEOUT_SECONDS,
                headers={"Accept-Encoding": "identity"},
            )
        except Exception as exc:
            raise _NoAnswerError from exc

    def _api(self) -> Any:
        try:
            return docker_api()
        except Exception as exc:
            raise _NoAnswerError from exc

    def _copy_local(self, source: _Source, final: bool) -> None:
        """The file on this host: opened with O_NOFOLLOW (a symlink is refused) and O_NONBLOCK (a FIFO never blocks),
        then checked with fstat: a regular file within the cap."""
        try:
            fd = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise _RefusedError(NOT_FOUND) from exc
        except OSError as exc:
            raise _RefusedError(NOT_REGULAR if exc.errno == errno.ELOOP else ERROR) from exc
        try:
            info = os.fstat(fd)
            now = (_monotonic(), _wall())
            if not stat.S_ISREG(info.st_mode):
                raise _RefusedError(NOT_REGULAR)
            self._source_bytes = info.st_size
            if info.st_size > COPY_MAX_BYTES:
                raise _RefusedError(TOO_LARGE)
            stat_result = _Stat(size=info.st_size, identity=(info.st_size, info.st_mtime_ns))
            if stat_result.identity == self._good_identity:
                self._checked(now, final)
                return
            self._check_cancelled()
            self._collect_secrets()
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, min(READ_CHUNK_BYTES * 16, COPY_MAX_BYTES + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > COPY_MAX_BYTES:  # it grew past the cap after fstat
                    raise _RefusedError(TOO_LARGE)
        finally:
            os.close(fd)
        self._store(b"".join(chunks), stat_result, source, now, final)

    # meta.json and the writes. ---------------------------------------------------------------------------------------

    def _store(self, data: bytes, stat_result: _Stat, source: _Source, now: tuple[float, float], final: bool) -> None:
        """A good copy: the redacted bytes, then its meta.json."""
        self._check_cancelled()
        redacted, counts = redaction.redact_exact(data, self._secrets)
        copied_at, elapsed = self._times(now)
        meta = self._meta_base()
        meta.update(
            status=OK,
            copied_at=copied_at,
            copied_elapsed_seconds=elapsed,
            checked_at=copied_at,
            checked_elapsed_seconds=elapsed,
            source_path=source.path,
            source_bytes=stat_result.size,
            copied_bytes=len(redacted),
            sha256=hashlib.sha256(redacted).hexdigest(),
            redactions=counts,
            copies=self._copies + 1,
            final=final,
            located_by=source.located_by,
        )
        if not self._write(redacted, meta):
            return
        self._copies += 1
        self._good_identity = stat_result.identity
        self._good_meta = {**meta, "final": False}
        run = self.run
        LOG.log(
            logging.INFO if self._copies == 1 else logging.DEBUG,
            "repair shadow transcript copied project=%s intent=%s run=%s bytes=%s redactions=%s copy=%s",
            run.project_id,
            run.intent_id,
            run.run_id,
            len(redacted),
            sum(counts.values()),
            self._copies,
        )

    def _checked(self, now: tuple[float, float], final: bool) -> None:
        """The file is unchanged since the last good copy: only meta.json, with the new checked time."""
        if self._good_meta is None:
            return
        checked_at, elapsed = self._times(now)
        meta = {**self._good_meta, "checked_at": checked_at, "checked_elapsed_seconds": elapsed, "final": final}
        if self._write(None, meta):
            self._good_meta = {**meta, "final": False}

    def _fail(self, status: str, final: bool) -> None:
        """A failed copy: meta.json with the status, and the previous good transcript and its times left in place."""
        meta = {**self._good_meta} if self._good_meta is not None else self._meta_base()
        meta.update(status=status, final=final)
        if self._source is not None:
            meta.update(
                source_path=self._source.path, source_bytes=self._source_bytes, located_by=self._source.located_by
            )
        self._log_failure(status)
        with suppress(_CancelledError):
            self._write(None, meta)

    def _meta_base(self) -> dict[str, Any]:
        run = self.run
        return {
            "version": 1,
            "run_id": run.run_id,
            "project_id": run.project_id,
            "intent_id": run.intent_id,
            "worker": run.worker_name,
            "cli_type": run.cli_type,
            "status": None,
            "copied_at": None,
            "copied_elapsed_seconds": None,
            "checked_at": None,
            "checked_elapsed_seconds": None,
            "source_path": None,
            "source_bytes": None,
            "copied_bytes": None,
            "sha256": None,
            "redactions": {},
            "copies": 0,
            "final": False,
            "located_by": None,
        }

    def _times(self, now: tuple[float, float]) -> tuple[str, float]:
        monotonic_now, wall_now = now
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall_now))
        return stamp, round(max(0.0, monotonic_now - self.run.started_monotonic), 1)

    def _write(self, transcript: bytes | None, meta: dict[str, Any]) -> bool:
        """transcript.jsonl (when given), then meta.json, in <root>/<project_id>/<run_id>/; False if that failed."""
        self._check_cancelled()
        run = self.run
        try:
            root_fd = open_root(run.config.repair.transcript_root)
            try:
                gid = os.fstat(root_fd).st_gid
                project_fd = open_folder(root_fd, run.project_id, gid)
                try:
                    run_fd = open_folder(project_fd, run.run_id, gid)
                    try:
                        if transcript is not None:
                            write_file(run_fd, TRANSCRIPT_NAME, transcript, gid)
                        payload = json.dumps(meta, ensure_ascii=True, allow_nan=False).encode("ascii")
                        write_file(run_fd, META_NAME, payload, gid)
                    finally:
                        os.close(run_fd)
                finally:
                    os.close(project_fd)
            finally:
                os.close(root_fd)
        except UnsafePathError:
            self._log_failure("unsafe_path")
            return False
        except Exception:
            self._log_failure(ERROR)
            return False
        return True

    def _log_failure(self, reason: str) -> None:
        if reason in self._failures_logged:
            return
        self._failures_logged.add(reason)
        run = self.run
        LOG.warning(
            "repair shadow transcript copy failed project=%s intent=%s run=%s reason=%s",
            run.project_id,
            run.intent_id,
            run.run_id,
            reason,
        )


def _host_directory(value: str | None, home: str | None, default: str) -> str | None:
    """${VALUE:-~/<default>} on this host, with ~ as the merged environment's HOME; None if it is not absolute."""
    if not value:
        value = "~/" + default
    if value == "~" or value.startswith("~/"):
        if home is None or not os.path.isabs(home):
            return None
        value = home + value[1:]
    return value if os.path.isabs(value) else None


def _host_head(path: str) -> bytes:
    """At most the first MARKER_MAX_BYTES of a regular file on this host; empty for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        chunks: list[bytes] = []
        size = 0
        while size < MARKER_MAX_BYTES and (chunk := os.read(fd, MARKER_MAX_BYTES - size)):
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    except OSError:
        return b""
    finally:
        os.close(fd)


def _recent_session_files(folder: str, since: float) -> list[str]:
    """The *.jsonl files at most CANDIDATE_MAX_DEPTH below folder modified at or after since, newest first, at most
    CANDIDATES_MAX of them. Symlinks are neither followed nor listed."""
    found: list[tuple[float, str]] = []
    stack = [(folder, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 < CANDIDATE_MAX_DEPTH:
                        stack.append((entry.path, depth + 1))
                elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    modified = entry.stat(follow_symlinks=False).st_mtime
                    if modified >= since:
                        found.append((modified, entry.path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [path for _modified, path in found[:CANDIDATES_MAX]]


def _read_frames(sock: Any) -> bytes | None:
    """The stdout of a multiplexed exec stream, read with poll() and a timeout; None if the read hit a limit."""
    deadline = time.monotonic() + FIND_READ_SECONDS
    raw = getattr(sock, "_sock", sock)
    poller = select.poll()
    poller.register(sock, select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR)
    received = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        pending = getattr(raw, "pending", None)
        if not (callable(pending) and pending()) and not poller.poll(max(1, int(remaining * 1000))):
            return None
        wanted = min(READ_CHUNK_BYTES, FIND_MAX_BYTES - len(received))
        chunk = sock.recv(wanted) if hasattr(sock, "recv") else sock.read(wanted)
        if chunk is None:
            continue
        if not chunk:
            break
        received += chunk
        if len(received) >= FIND_MAX_BYTES:
            return None
    stdout = bytearray()
    position = 0
    while position + 8 <= len(received):
        kind, length = received[position], int.from_bytes(received[position + 4 : position + 8], "big")
        payload = received[position + 8 : position + 8 + length]
        if kind == 1:
            stdout += payload
        position += 8 + length
    return bytes(stdout)


def _close_socket(sock: Any) -> None:
    """Close an exec's raw socket, and the connection under it, at once."""
    raw = getattr(sock, "_sock", None)
    for target in (raw, sock):
        if target is not None and hasattr(target, "shutdown"):
            with suppress(Exception):
                target.shutdown(socket.SHUT_RDWR)
    for target in (sock, raw, getattr(sock, "_response", None)):
        if target is not None:
            with suppress(Exception):
                target.close()
