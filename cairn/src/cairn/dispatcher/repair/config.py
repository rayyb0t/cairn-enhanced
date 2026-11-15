"""The dispatcher token's process-wide loader and the repair side's "configured" state (spec 8.3).

The dispatcher token is read from the repair: section's token_file and from nowhere else. It is never read
from an environment variable: in local mode every variable set on the dispatcher reaches every worker of every
project (runtime/local_backend.py, {**os.environ, **env}).

The file is read once per process, under one lock, by whichever thread needs the token first: the housekeeping
thread at dispatcher start, or a run's reporter thread. The result is kept for the process lifetime, so a fixed
token file takes effect after a dispatcher restart. The task thread never reads the file: it asks cached_state,
which does no I/O and never waits for a read in progress.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from dataclasses import dataclass, field

from cairn.dispatcher.config import DispatchConfig

LOG = logging.getLogger(__name__)

TOKEN_MIN_CHARS = 32
TOKEN_FILE_MAX_BYTES = 65536  # the repair agent's cap for its secret files; a larger file holds no token
ASCII_WHITESPACE = b" \t\n\r\x0b\x0c"  # what bytes.strip() removes, as the agent strips its token files

# Why runs of switched-on projects are not reported, checked in this order (spec 8.3).
NO_REPAIR_SECTION = "no_repair_section"
UNREADABLE = "token_unreadable"
NOT_ASCII = "not_ascii"
SHORTER_THAN_32 = "shorter_than_32"


@dataclass(frozen=True, slots=True)
class RepairState:
    """Whether runs of switched-on projects are reported, and the dispatcher token they are reported with.

    When configured is False, reason is one of the four reasons above and token is None. The token is left out
    of the repr, so a state printed by mistake shows no secret.
    """

    configured: bool
    reason: str | None = None
    token: str | None = field(default=None, repr=False)


class _Loader:
    """The process-wide state behind cached_state and load_state."""

    def __init__(self) -> None:
        self.read_lock = threading.Lock()  # held while the token file is read, so that it is read once
        self.state_lock = threading.Lock()  # guards state, and is never held across I/O
        self.state: RepairState | None = None

    def settle(self, state: RepairState) -> tuple[RepairState, bool]:
        """Keep state unless one is kept already; also say whether this call kept it."""
        with self.state_lock:
            if self.state is None:
                self.state = state
                return state, True
            return self.state, False


_LOADER = _Loader()


def cached_state(config: DispatchConfig) -> RepairState | None:
    """The state, with no I/O, for the task thread.

    None while no thread has read the token yet. Without a repair: section there is nothing to read, so the
    answer is settled here: not configured, with reason no_repair_section, logged once per process.
    """
    loader = _LOADER
    state = loader.state
    if state is None and config.repair is None:
        state = _settle(loader, config, RepairState(configured=False, reason=NO_REPAIR_SECTION))
    return state


def load_state(config: DispatchConfig) -> RepairState:
    """The state, reading the token file once per process, for the housekeeping thread and reporter threads."""
    loader = _LOADER
    state = loader.state
    if state is not None:
        return state
    repair = config.repair
    if repair is None:
        return _settle(loader, config, RepairState(configured=False, reason=NO_REPAIR_SECTION))
    with loader.read_lock:
        state = loader.state
        if state is not None:
            return state
        return _settle(loader, config, _read_token(repair.token_file))


def _settle(loader: _Loader, config: DispatchConfig, state: RepairState) -> RepairState:
    kept, settled_here = loader.settle(state)
    if settled_here:
        _log_state(config, kept)
    return kept


def _log_state(config: DispatchConfig, state: RepairState) -> None:
    repair = config.repair
    if state.configured and repair is not None:
        LOG.info(
            "repair shadow configured transcript_root=%s reviewers=%s projects=%s",
            repair.transcript_root,
            len(repair.reviewers),
            "all" if repair.projects is None else len(repair.projects),
        )
    else:
        LOG.warning("repair shadow not configured, runs are not reported reason=%s", state.reason)


def _read_token(path: str) -> RepairState:
    """The token in path: its content with surrounding whitespace removed, printable ASCII, 32 characters or more.

    These are the server's rules for its own token files (spec 7), and the agent's, as one definition (SEC-12):
    only ASCII whitespace around the token is removed, and then every byte must be in 0x20-0x7E. So a token file
    that the agent or the server would refuse as not_ascii, such as one ending in 0x1F or in a UTF-8 no-break space,
    is not_ascii here too. A file that cannot be opened or read, that is not a regular file (a FIFO would block the
    read, a device would never end it), or that is larger than TOKEN_FILE_MAX_BYTES is token_unreadable.
    """
    try:
        content = _read_regular_file(path)
    except Exception:
        return RepairState(configured=False, reason=UNREADABLE)
    try:
        value = content.strip(ASCII_WHITESPACE).decode("ascii")
    except UnicodeDecodeError:
        return RepairState(configured=False, reason=NOT_ASCII)
    if not (value.isascii() and value.isprintable()):
        return RepairState(configured=False, reason=NOT_ASCII)
    if len(value) < TOKEN_MIN_CHARS:
        return RepairState(configured=False, reason=SHORTER_THAN_32)
    return RepairState(configured=True, token=value)


def _read_regular_file(path: str) -> bytes:
    """The file's bytes, at most TOKEN_FILE_MAX_BYTES of them: a larger file raises, having read one byte more."""
    # O_NONBLOCK: opening a FIFO would otherwise wait for a writer; a regular file ignores the flag.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("token_file is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= TOKEN_FILE_MAX_BYTES and (chunk := os.read(fd, TOKEN_FILE_MAX_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
        if size > TOKEN_FILE_MAX_BYTES:
            raise OSError("token_file is larger than TOKEN_FILE_MAX_BYTES")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o044:
            LOG.warning("repair shadow token file readable by others token_file=%s mode=%s", path, f"{mode:04o}")
        return b"".join(chunks)
    finally:
        os.close(fd)
