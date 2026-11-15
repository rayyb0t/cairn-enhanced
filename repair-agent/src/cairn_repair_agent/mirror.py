"""`read_copy`: the dispatcher's transcript copy of one run, read safely (spec §9.5).

The agent reads exactly `<root>/<project_id>/<run_id>/meta.json` and `<root>/<project_id>/<run_id>/transcript.jsonl`,
both ids from the run's `RunView`: never a path from the server's `locator` or from `meta.json`, never through a
symlink below the root, never a FIFO or another file that is not regular, and never more than its size limits. Every
folder and file below the root is opened relative to its parent's descriptor with `O_NOFOLLOW`, and the files with
`O_NONBLOCK` too, then checked with `fstat`.

The result is a `MirrorCopy` or `Unavailable(reason)`: `bad_run_id`, `bad_project_id`, `missing`, `permission_denied`,
`unsafe_path`, `meta_invalid`, `not_ok:<status>` (`not_ok:other` for a status the dispatcher does not write, so no text
from `meta.json` reaches a log line or a posted reason: in local mode a worker can write the copy root), `too_large`,
`hash_mismatch` or `stale`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

RUN_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}")  # the server's rule (§6.1)
PROJECT_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # the id rule of §8.7, with no ".."
META_FILE = "meta.json"
TRANSCRIPT_FILE = "transcript.jsonl"
META_MAX_BYTES = 64 * 1024
META_VERSION = 1
# §8.8's statuses of a failed copy; any other value is reported as not_ok:other.
COPY_STATUSES = ("not_found", "no_session", "too_large", "not_regular", "error")
HASH_RETRY_SECONDS = 1.0
STALE_RETRY_SECONDS = 5.0
_READ_CHUNK = 1 << 20
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class MirrorCopy:
    """A copy that passed every check. `data` is the transcript's bytes; the elapsed times are the dispatcher's, in
    seconds since the run's start: when the bytes were copied, and when the session file was last found unchanged
    since then (the digest's cut, §9.6)."""

    data: bytes = field(repr=False)
    sha256: str
    copied_elapsed_seconds: float
    checked_elapsed_seconds: float


@dataclass(frozen=True)
class Unavailable:
    reason: str
    # For `stale`: the copy's checked_elapsed_seconds, a number from meta.json that passed the checks.
    checked_elapsed_seconds: float | None = None


class _RefusedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def valid_run_id(value: object) -> bool:
    return isinstance(value, str) and RUN_ID_RULE.fullmatch(value) is not None


def valid_project_id(value: object) -> bool:
    return isinstance(value, str) and PROJECT_ID_RULE.fullmatch(value) is not None and ".." not in value


def _open_reason(exc: OSError) -> str:
    if exc.errno == errno.ENOENT:
        return "missing"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    return "unsafe_path"  # ELOOP (a symlink), ENOTDIR (not a folder), ENXIO, ...


def _open_folder(name: str, dir_fd: int) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _RefusedError(_open_reason(exc)) from None


def _read_file(name: str, dir_fd: int, limit: int, too_large: str) -> bytes:
    """A regular file of at most `limit` bytes, opened without following a symlink or waiting on a FIFO."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _RefusedError(_open_reason(exc)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _RefusedError("unsafe_path")
        if info.st_size > limit:
            raise _RefusedError(too_large)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:  # the file grew after the fstat
                raise _RefusedError(too_large)
        return b"".join(chunks)
    except OSError as exc:
        raise _RefusedError(_open_reason(exc)) from None
    finally:
        os.close(fd)


def _elapsed(meta: dict[str, object], key: str) -> float:
    """A finite number that is not a boolean, as a float. JSON integers have no size limit, and `json.loads` reads up
    to 4,300 digits: an integer too large for a float (309 digits or more) is no finite number either, and converting
    it raises OverflowError, so it gives meta_invalid like Infinity does."""
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _RefusedError("meta_invalid")
    try:
        number = float(value)
    except OverflowError:
        raise _RefusedError("meta_invalid") from None
    if not math.isfinite(number):
        raise _RefusedError("meta_invalid")
    return number


def _read_once(root: str, project_id: str, run_id: str, max_bytes: int) -> MirrorCopy:
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise _RefusedError(_open_reason(exc)) from None
    try:
        project_fd = _open_folder(project_id, root_fd)
        try:
            run_fd = _open_folder(run_id, project_fd)
            try:
                meta_bytes = _read_file(META_FILE, run_fd, META_MAX_BYTES, "meta_invalid")
                try:
                    meta = json.loads(meta_bytes)
                except (ValueError, RecursionError):
                    raise _RefusedError("meta_invalid") from None
                if (
                    not isinstance(meta, dict)
                    or not (isinstance(meta.get("version"), int) and not isinstance(meta.get("version"), bool))
                    or meta.get("version") != META_VERSION
                    or meta.get("run_id") != run_id
                    or meta.get("project_id") != project_id
                ):
                    raise _RefusedError("meta_invalid")
                status = meta.get("status")
                if status != "ok":
                    known = isinstance(status, str) and status in COPY_STATUSES
                    raise _RefusedError(f"not_ok:{status}" if known else "not_ok:other")
                copied = _elapsed(meta, "copied_elapsed_seconds")
                checked = _elapsed(meta, "checked_elapsed_seconds")
                expected = meta.get("sha256")
                if not isinstance(expected, str):
                    raise _RefusedError("meta_invalid")
                data = _read_file(TRANSCRIPT_FILE, run_fd, max_bytes, "too_large")
            finally:
                os.close(run_fd)
        finally:
            os.close(project_fd)
    finally:
        os.close(root_fd)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise _RefusedError("hash_mismatch")
    return MirrorCopy(data=data, sha256=digest, copied_elapsed_seconds=copied, checked_elapsed_seconds=checked)


def read_copy(
    root: str,
    project_id: str,
    run_id: str,
    max_bytes: int,
    *,
    min_checked_elapsed: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MirrorCopy | Unavailable:
    """Steps 1-4 of spec §9.5 for the run `run_id` of `project_id`.

    A hash mismatch (a copy landed between the two reads) is retried once after `HASH_RETRY_SECONDS`. With
    `min_checked_elapsed` (see `freshness_floor`), a copy whose `checked_elapsed_seconds` is below it is `stale`.
    """
    if not valid_run_id(run_id):
        return Unavailable("bad_run_id")
    if not valid_project_id(project_id):
        return Unavailable("bad_project_id")
    try:
        copy = _read_once(root, project_id, run_id, max_bytes)
    except _RefusedError as refused:
        if refused.reason != "hash_mismatch":
            return Unavailable(refused.reason)
        sleep(HASH_RETRY_SECONDS)
        try:
            copy = _read_once(root, project_id, run_id, max_bytes)
        except _RefusedError as again:
            return Unavailable(again.reason)
    if min_checked_elapsed is not None and copy.checked_elapsed_seconds < min_checked_elapsed:
        return Unavailable("stale", checked_elapsed_seconds=copy.checked_elapsed_seconds)
    return copy


def read_run_copy(
    root: str,
    run: Mapping[str, Any],
    max_bytes: int,
    *,
    min_checked_elapsed: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MirrorCopy | Unavailable:
    """`read_copy` with both ids from the run's `RunView`. Nothing else of the view is read: the agent ignores the
    server's `locator` and the session id, and finds the copy by its ids alone (§9.5)."""
    project_id, run_id = run.get("project_id"), run.get("run_id")
    if not isinstance(run_id, str):
        return Unavailable("bad_run_id")
    if not isinstance(project_id, str):
        return Unavailable("bad_project_id")
    return read_copy(root, project_id, run_id, max_bytes, min_checked_elapsed=min_checked_elapsed, sleep=sleep)


def freshness_floor(review_opens_elapsed: float, max_staleness_seconds: float) -> float:
    """Spec §9.5 step 4: the least `checked_elapsed_seconds` a fresh copy has, `(review_opens_at - started_at) -
    max_staleness_seconds`, from the elapsed time at which the run's window opens for review."""
    return review_opens_elapsed - max_staleness_seconds


def read_fresh_copy(
    root: str,
    project_id: str,
    run_id: str,
    max_bytes: int,
    *,
    min_checked_elapsed: float,
    wait_until: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> MirrorCopy | Unavailable:
    """`read_copy`, re-read every `STALE_RETRY_SECONDS` while the copy is stale, until the monotonic time `wait_until`
    (the review's window closing, or the model and copy deadline if that is earlier, §9.4). Any other result is final
    at once."""
    while True:
        result = read_copy(root, project_id, run_id, max_bytes, min_checked_elapsed=min_checked_elapsed, sleep=sleep)
        if not (isinstance(result, Unavailable) and result.reason == "stale"):
            return result
        left = wait_until - monotonic()
        if left <= 0:
            return result
        sleep(min(STALE_RETRY_SECONDS, left))
