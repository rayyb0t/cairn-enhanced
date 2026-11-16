"""The reviewer pin (owner decision O9, the Q18 safeguard): the live agent remembers the reviewer it first used and
refuses to start when that reviewer changes, until the owner accepts the change by removing the pin file as root.

In local mode the explore and reason workers run as the owner's user, so a worker steered by a target's prompt
injection can edit the `repair:` section of dispatch.yaml, or the settings file the dispatcher writes from it, and
point the reviewer at a listener of its own, which would then receive every digest. With the pin, such a change stops
the agent instead of redirecting it.

- What is pinned: the live reviewer's (the settings file's first reviewer's) `name`, `base_url`, `api_format`,
  `model`, `proxy` and `ca_bundle`, as validated (`PINNED_FIELDS`). Never the key, and never `max_tokens`,
  `context_window`, `timeout_seconds`, `max_attempts` or `structured_output`, none of which can redirect a digest.
- Where: `<state.dir>/reviewer-pin.json`, in a folder that only the agent's own user can write (`StateConfig`; the
  isolation check's `state_dir_*` reasons) and whose mode gives that user read, search and write (u+rwx, checked
  here before the pin is read or written), the file 0600, written atomically (a temporary file created with
  O_EXCL | O_NOFOLLOW, fsync, `os.replace`, fsync of the folder), read with O_NOFOLLOW | O_NONBLOCK, checked on its
  descriptor (a regular file of the agent's own, neither group- nor world-writable, at most 64 KiB), and parsed once
  inside a guard. The folder itself is opened with O_NOFOLLOW too (build step RB1), so a symlinked state folder is
  refused here even where the isolation check's `state_dir_symlink` was waived (`--allow-shared-user`).
- When: at the live agent's start only (`cli.start`), after the isolation check and the settings file's WARNINGs, and
  before any client is built or any request sent; with `--dry-run` and `--once` too. The replay and the gate tools
  never touch the state folder.

No line holds the key, a proxy URL, or more of `base_url` than `scheme://host:port`, and a refusal names the changed
fields, never a value. The pinned line also says whether a proxy and a CA file are pinned (`set` or `none`, never the
value: a proxy URL can hold a user name and password; build step RB1), so that at the moment of trust on first use
the owner sees, besides the reviewer's name and endpoint, whether anything else decides where digests go. It shows no
path of `base_url`, no proxy URL and no CA file's path: the pin file holds them, which the owner reads as root to see
everything that was pinned. Every refusal is logged here and raised as a StartupError with `logged=True`: exit code 2.
Its second line gives the remedy: remove the pin file as root (REMEDY), or, when the agent cannot use the state folder
itself (it cannot open it, or the folder is its own but its mode lacks u+rwx), make it the agent's own folder
(FOLDER_REMEDY), or, when the state folder is a symlink, which the folder's open refuses, replace the link with a
folder of the agent's own (FOLDER_SYMLINK_REMEDY). A missing folder is one line that names the command that creates
it, and a failed write, which leaves no pin file, has no second line.
"""

from __future__ import annotations

import contextlib
import errno
import grp
import json
import logging
import os
import pwd
import secrets
import shlex
import stat
from datetime import UTC, datetime
from typing import Any, NoReturn

from .config import ReviewerSettings, StartupError

logger = logging.getLogger(__name__)

PIN_FILE_NAME = "reviewer-pin.json"
PIN_VERSION = 1
PIN_MAX_BYTES = 64 * 1024
PIN_MODE = 0o600
GROUP_OR_WORLD_WRITABLE = 0o022
PINNED_FIELDS = ("name", "base_url", "api_format", "model", "proxy", "ca_bundle")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REMEDY = (
    "repair reviewer pin: if you changed the reviewer yourself, remove the pin file as root and start the agent again"
)
# The remedy when the agent cannot use the state folder itself (but it is there): there may be no pin file to remove,
# and removing one would not help; the folder must become the agent's own. The command is the one `folder_missing`
# names; on a folder that is there, `install -d` sets its owner and mode.
FOLDER_REMEDY = (
    "repair reviewer pin: the agent cannot use its state folder; check state.dir in repair.yaml, and as root make it "
    "the agent's own folder with: install -d -m 0700 -o %s -g %s %s"
)
# The remedy when the state folder is a symlink, which the folder's open (O_NOFOLLOW) refuses: FOLDER_REMEDY's
# `install -d` would follow the link and leave it in place, so the agent would stop again with the same lines. The
# link itself is removed (`rm` removes a link, never what it names; `state.dir` never ends in `/`), then the folder is
# created as `folder_missing`'s command creates it.
FOLDER_SYMLINK_REMEDY = (
    "repair reviewer pin: the agent's state folder is a symlink, which it does not follow; check state.dir in "
    "repair.yaml, and as root replace the link with a folder of the agent's own: rm %s && install -d -m 0700 -o %s "
    "-g %s %s"
)
# What the agent's own state folder must give its user: read (open it), search (open the pin in it) and write (make
# the pin). The runbook's command creates it 0700.
OWNER_RWX = stat.S_IRWXU

# Why a pin cannot be used (build step R3, A5 and A6).
FOLDER_MISSING = "folder_missing"
NOT_A_REGULAR_FILE = "not_a_regular_file"
OWNER_OR_MODE = "owner_or_mode"
TOO_LARGE = "too_large"
MALFORMED = "malformed"
WRONG_VERSION = "wrong_version"
UNREADABLE = "unreadable"
WRITE_FAILED = "write_failed"


class _UnusableError(Exception):
    """A pin that cannot be used, with its reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pinned_values(reviewer: ReviewerSettings) -> dict[str, Any]:
    """What is pinned of a reviewer, in PINNED_FIELDS' order: strings, or None."""
    return {name: getattr(reviewer, name) for name in PINNED_FIELDS}


def pin_path(state_dir: str) -> str:
    return os.path.join(state_dir, PIN_FILE_NAME)


def _errno_reason(exc: OSError) -> str:
    """`unreadable`, named by the errno's name only."""
    name = errno.errorcode.get(exc.errno, "EIO") if exc.errno is not None else "EIO"
    return f"{UNREADABLE}:{name}"


def agent_user_and_group() -> tuple[str, str]:
    """The agent's effective user and group, by name from pwd and grp, or by number without an entry."""
    uid, gid = os.geteuid(), os.getegid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return user, group


def check_reviewer_pin(state_dir: str, reviewer: ReviewerSettings, endpoint: str) -> None:
    """Pin the live reviewer on a first start, go on when the pin matches, and refuse to start (StartupError, logged)
    when it differs or cannot be used. `endpoint` is `scheme://host:port` of the reviewer's base_url, as the started
    line shows it."""
    path = pin_path(state_dir)
    folder: int | None = None
    reason = f"{UNREADABLE}:EIO"
    try:
        # O_NOFOLLOW: a symlinked folder fails here (ENOTDIR on Linux), even with the isolation check waived.
        folder = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _check_own_folder_mode(os.fstat(folder))
    except OSError as exc:
        if folder is not None:
            os.close(folder)
            folder = None
        reason = FOLDER_MISSING if exc.errno == errno.ENOENT else _errno_reason(exc)
    except ValueError:  # a path the file system cannot encode
        reason = f"{UNREADABLE}:EINVAL"
    if folder is None:
        # The lines name the command that creates the folder, or makes it the agent's own, for this user and group.
        user, group = agent_user_and_group()
        command = (shlex.quote(user), shlex.quote(group), shlex.quote(state_dir))
        if reason == FOLDER_MISSING:
            logger.error(
                "repair reviewer pin unusable path=%s reason=folder_missing; as root, create the folder with: "
                "install -d -m 0700 -o %s -g %s %s",
                path,
                *command,
            )
        else:  # there, but not a folder the agent can use (EACCES; ENOTDIR, a file or a symlink...): no pin to remove
            logger.error("repair reviewer pin unusable path=%s reason=%s", path, reason)
            if _is_symlink(state_dir):  # `install -d` alone would follow the link: it must be replaced
                logger.error(FOLDER_SYMLINK_REMEDY, shlex.quote(state_dir), *command)
            else:
                logger.error(FOLDER_REMEDY, *command)
        raise StartupError("repair reviewer pin unusable", logged=True)
    try:
        current = pinned_values(reviewer)
        pinned: dict[str, Any] | None = None
        unusable: str | None = None
        try:
            pinned = _read_pin(folder)
            if pinned is None:
                _write_pin(folder, current)
        except _UnusableError as exc:
            unusable = exc.reason
        if unusable is not None:
            _refuse_unusable(path, unusable)
        if pinned is None:
            logger.info(
                "repair reviewer pinned reviewer=%s endpoint=%s proxy=%s ca_bundle=%s",
                reviewer.name,
                endpoint,
                _set_or_none(current["proxy"]),
                _set_or_none(current["ca_bundle"]),
            )
            return
        changed = [name for name in PINNED_FIELDS if pinned[name] != current[name]]
        if changed:
            logger.error("repair reviewer changed since it was pinned fields=%s pin=%s", ",".join(changed), path)
            logger.error(REMEDY)
            raise StartupError("repair reviewer changed since it was pinned", logged=True)
    finally:
        os.close(folder)


def _set_or_none(value: str | None) -> str:
    """Whether a pinned setting is set, for the pinned line: never its value."""
    return "none" if value is None else "set"


def _is_symlink(path: str) -> bool:
    """Whether `path` is a symlink itself (`os.lstat`), for the remedy of a state folder the agent could not open;
    False when it cannot be looked at."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return False


def _check_own_folder_mode(info: os.stat_result) -> None:
    """Raise EACCES when the state folder is the agent's own but its mode does not give its user read, search and
    write (u+rwx), before the pin is read or written. A non-root agent could open a 0600 folder but not open a pin in
    it, and read a pin in a 0500 one but not write one: failures that would otherwise read as the pin file's own
    (`unreadable:EACCES` with the pin's remedy, or `write_failed`), although the folder is at fault. EACCES is what
    such a user gets; the agent refuses whoever it runs as, root too, so that the folder's remedy, which gives the
    folder its mode back, follows. Another user's folder is the isolation check's (`state_dir_owner`)."""
    if info.st_uid == os.geteuid() and (info.st_mode & OWNER_RWX) != OWNER_RWX:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))


def _refuse_unusable(path: str, reason: str) -> NoReturn:
    logger.error("repair reviewer pin unusable path=%s reason=%s", path, reason)
    if reason != WRITE_FAILED:  # no pin file was written, so there is none to remove
        logger.error(REMEDY)
    raise StartupError("repair reviewer pin unusable", logged=True)


def _read_pin(folder: int) -> dict[str, Any] | None:
    """The pinned values, or None when there is no pin file yet. Raises _UnusableError."""
    try:
        fd = os.open(PIN_FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        if exc.errno == errno.ELOOP:  # a symlink, which O_NOFOLLOW refuses
            raise _UnusableError(NOT_A_REGULAR_FILE) from None
        raise _UnusableError(_errno_reason(exc)) from None
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _UnusableError(NOT_A_REGULAR_FILE)
            if info.st_uid != os.geteuid() or info.st_mode & GROUP_OR_WORLD_WRITABLE:
                raise _UnusableError(OWNER_OR_MODE)
            if info.st_size > PIN_MAX_BYTES:
                raise _UnusableError(TOO_LARGE)
            chunks: list[bytes] = []
            size = 0
            while size <= PIN_MAX_BYTES:
                chunk = os.read(fd, PIN_MAX_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except OSError as exc:
            raise _UnusableError(_errno_reason(exc)) from None
    finally:
        os.close(fd)
    if size > PIN_MAX_BYTES:
        raise _UnusableError(TOO_LARGE)
    return _parsed(b"".join(chunks))


def _parsed(data: bytes) -> dict[str, Any]:
    """The pinned values of a pin file's content, parsed once inside a guard. Raises _UnusableError."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):  # not UTF-8 (UnicodeDecodeError is a ValueError), not JSON, nested too deeply
        raise _UnusableError(MALFORMED) from None
    if not isinstance(document, dict):
        raise _UnusableError(MALFORMED)
    version = document.get("version")
    if type(version) is int and version != PIN_VERSION:
        raise _UnusableError(WRONG_VERSION)
    reviewer = document.get("reviewer")
    if (
        set(document) != {"version", "pinned_at", "reviewer"}
        or type(version) is not int
        or not isinstance(document["pinned_at"], str)
        or not isinstance(reviewer, dict)
        or set(reviewer) != set(PINNED_FIELDS)
        or not all(value is None or isinstance(value, str) for value in reviewer.values())
    ):
        raise _UnusableError(MALFORMED)
    return {name: reviewer[name] for name in PINNED_FIELDS}


def _write_pin(folder: int, values: dict[str, Any]) -> None:
    """Write the pin atomically (A6): a temporary file in the folder, created O_EXCL | O_NOFOLLOW with mode 0600,
    written and fsynced, then `os.replace`d to its name, and the folder fsynced. The temporary file is removed on any
    failure. Raises _UnusableError(write_failed)."""
    document = {"version": PIN_VERSION, "pinned_at": datetime.now(UTC).strftime(TIME_FORMAT), "reviewer": values}
    data = (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    temporary = f".{PIN_FILE_NAME}.{secrets.token_hex(8)}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(temporary, flags, PIN_MODE, dir_fd=folder)
    except OSError:
        raise _UnusableError(WRITE_FAILED) from None
    try:
        try:
            os.fchmod(fd, PIN_MODE)  # exactly 0600, whatever the umask
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, PIN_FILE_NAME, src_dir_fd=folder, dst_dir_fd=folder)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=folder)
        raise _UnusableError(WRITE_FAILED) from None
    try:
        os.fsync(folder)
    except OSError:
        raise _UnusableError(WRITE_FAILED) from None
