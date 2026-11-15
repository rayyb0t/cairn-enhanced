"""Repair layer (shadow mode): role tokens for the repair run endpoints.

Main has no authentication on any endpoint. Phase 2 protects only its new machine endpoints: the repair agent's
(list the runs, read one, post a review) need the agent token, and the dispatcher's (register a run, end it) need the
dispatcher token. The Repair switch, GET /repair/settings and every existing endpoint stay open.

Each role's token is read once, by the app's lifespan at startup, from `CAIRN_REPAIR_<ROLE>_TOKEN_FILE` (a file whose
content, with surrounding whitespace removed, is the token) or `CAIRN_REPAIR_<ROLE>_TOKEN` (the value itself, with
surrounding whitespace removed, as the agent reads its own); when both are set, the file is used. A token is refused,
and its role left unconfigured, when its file cannot be read, when it holds a character outside printable ASCII
(0x20-0x7E), when it is shorter than 32 characters, or when both roles have the same token. The server always
starts. A request to an endpoint whose role has no token gets 503 `token_not_configured` (fail closed); a missing or
wrong token, the other role's included, gets 401 `unauthorized`. Each protected route declares its check as a
dependency, and the repair routes run that same check before anything else, before FastAPI reads the body, which it
does before it resolves dependencies: so a body that is not JSON gets the 401 or 503 too, and is never read.

Only ASCII whitespace is removed around a token (ASCII_WHITESPACE), as the dispatcher and the agent remove it around
theirs: they apply the same rule to their token files (spec 7), so a token file that one of them refuses as not_ascii
is refused here too, and roles_configured never shows a dispatcher token that the dispatcher itself refuses.

Token values are never logged. A failed authentication is logged with the role and the route's fixed endpoint name,
never the request path, which can hold anything a caller sends before it is authenticated, at most once per role in
any 60 seconds.
"""

from __future__ import annotations

import hmac
import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request

LOG = logging.getLogger(__name__)

AUTH_HEADER = "X-Cairn-Repair-Token"
MIN_TOKEN_LENGTH = 32
ROLES = ("agent", "dispatcher")
TOKEN_ENV = {"agent": "CAIRN_REPAIR_AGENT_TOKEN", "dispatcher": "CAIRN_REPAIR_DISPATCHER_TOKEN"}
# A token file is a few dozen bytes. A larger file, or anything but a regular file (a FIFO, a device), is refused as
# unreadable, so that reading it can never hold up or exhaust the server at its start.
MAX_TOKEN_FILE_BYTES = 65536
# The whitespace removed around a token: what bytes.strip() removes, as the dispatcher and the agent remove it around
# theirs. str.strip() would also remove 0x1C-0x1F, U+0085, U+00A0 and the other Unicode spaces, none of them printable
# ASCII; they stay, and the printable-ASCII rule refuses them here as there.
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
AUTH_FAILURE_LOG_INTERVAL_SECONDS = 60.0
# The fixed names of the protected routes, as a failed authentication logs them.
ENDPOINTS = frozenset({"runs.list", "runs.get", "runs.review", "runs.register", "runs.end"})

# The clock of the failure log's rate limit. A module attribute, so that a test can move it.
_monotonic: Callable[[], float] = time.monotonic


class _Unreadable:
    """A token file that could not be read."""


_UNREADABLE = _Unreadable()


class _TokenState:
    """The configured tokens (bytes, or None for an unconfigured role) and the failure log's last lines, in memory."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.configured = False
        self.tokens: dict[str, bytes | None] = dict.fromkeys(ROLES)
        self.failure_logged_at: dict[str, float] = {}


_STATE = _TokenState()


# --------------------------------------------------------------------------- configuration


def _read_token_file(path: str) -> str | _Unreadable:
    """The file's text with the ASCII whitespace around it removed (ASCII_WHITESPACE), or _UNREADABLE."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return _UNREADABLE
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOKEN_FILE_BYTES:
            return _UNREADABLE
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # closed with the file object
            data = handle.read(MAX_TOKEN_FILE_BYTES + 1)
    except OSError:
        return _UNREADABLE
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_TOKEN_FILE_BYTES:
        return _UNREADABLE
    # Bytes that are not UTF-8 are kept as surrogates, which the printable-ASCII rule then refuses.
    return data.decode("utf-8", errors="surrogateescape").strip(ASCII_WHITESPACE)


def _problem(token: str) -> str | None:
    """Why a token is refused, or None. Only printable ASCII passes: a header carries those bytes unchanged."""
    if not (token.isascii() and token.isprintable()):
        return "not_ascii"
    if len(token) < MIN_TOKEN_LENGTH:
        return "shorter_than_32"
    return None


def _install(values: Mapping[str, str | _Unreadable | None]) -> None:
    accepted: dict[str, str] = {}
    for role in ROLES:
        value = values.get(role)
        if value is None:
            continue
        reason = "file_unreadable" if isinstance(value, _Unreadable) else _problem(value)
        if reason is not None:
            LOG.warning("repair token rejected role=%s reason=%s", role, reason)
            continue
        assert isinstance(value, str)
        accepted[role] = value
    if len(accepted) == len(ROLES) and len(set(accepted.values())) == 1:
        # One secret for both roles would let either one act as the other.
        for role in ROLES:
            LOG.warning("repair token rejected role=%s reason=%s", role, "identical")
        accepted = {}
    with _STATE.lock:
        _STATE.tokens = {role: accepted[role].encode("ascii") if role in accepted else None for role in ROLES}
        _STATE.configured = True
    LOG.info("repair tokens configured agent=%s dispatcher=%s", "agent" in accepted, "dispatcher" in accepted)


def configure_tokens(agent: str | None, dispatcher: str | None) -> None:
    """Install the role tokens, None leaving a role unconfigured, after the checks of the startup read."""
    _install({"agent": agent, "dispatcher": dispatcher})


def load_tokens(environ: Mapping[str, str] | None = None) -> None:
    """Read the role tokens from the environment, once: the app's lifespan calls this at startup, and it does nothing
    once tokens have been configured. Never raises, so the server always starts.
    """
    with _STATE.lock:
        if _STATE.configured:
            return
    env = os.environ if environ is None else environ
    values: dict[str, str | _Unreadable | None] = {}
    for role, name in TOKEN_ENV.items():
        path = env.get(f"{name}_FILE")
        value = env.get(name)
        if path:
            values[role] = _read_token_file(path)
        elif value:
            values[role] = value.strip(ASCII_WHITESPACE)
        else:
            values[role] = None
    _install(values)


def roles_configured() -> dict[str, bool]:
    """Which roles have a token, as GET /repair/settings reports them."""
    with _STATE.lock:
        return {role: _STATE.tokens[role] is not None for role in ROLES}


def reset() -> None:
    """Forget the tokens and the failure log, as a restart does. For tests."""
    with _STATE.lock:
        _STATE.configured = False
        _STATE.tokens = dict.fromkeys(ROLES)
        _STATE.failure_logged_at.clear()


# --------------------------------------------------------------------------- the check


def _log_failure(role: str, endpoint: str) -> None:
    now = _monotonic()
    with _STATE.lock:
        last = _STATE.failure_logged_at.get(role)
        if last is not None and now - last < AUTH_FAILURE_LOG_INTERVAL_SECONDS:
            return
        _STATE.failure_logged_at[role] = now
    LOG.warning("repair auth failed role=%s endpoint=%s", role, endpoint)


def verify(role: str, endpoint: str, presented: str | None) -> None:
    """Raise unless `presented` is the role's token. Compared as UTF-8 bytes in constant time: Starlette decodes a
    header as latin-1, and compare_digest refuses two str with non-ASCII text, so a header of other bytes is a plain
    mismatch (401), never a 500.
    """
    with _STATE.lock:
        expected = _STATE.tokens.get(role)
    if expected is None:
        raise HTTPException(
            503,
            {"code": "token_not_configured", "message": f"the repair {role} token is not configured on this server"},
        )
    if presented is None or not hmac.compare_digest(presented.encode("utf-8"), expected):
        _log_failure(role, endpoint)
        raise HTTPException(401, {"code": "unauthorized", "message": f"this endpoint needs the repair {role} token"})


# The (role, endpoint) each dependency made by require_agent and require_dispatcher checks. The repair routes look
# their own up, so that they can run the check before FastAPI reads the request's body (routers/repair.py).
_REQUIREMENTS: dict[Callable[..., Any], tuple[str, str]] = {}


def _requirement(role: str, endpoint: str) -> Callable[..., None]:
    if endpoint not in ENDPOINTS:
        raise ValueError(f"unknown repair endpoint name {endpoint!r}")

    def check(token: Annotated[str | None, Header(alias=AUTH_HEADER)] = None) -> None:
        verify(role, endpoint, token)

    _REQUIREMENTS[check] = (role, endpoint)
    return check


def requirement_of(dependency: Callable[..., Any] | None) -> tuple[str, str] | None:
    """The (role, endpoint) that `dependency` checks, if require_agent or require_dispatcher made it, else None."""
    return _REQUIREMENTS.get(dependency) if dependency is not None else None


def verify_request(role: str, endpoint: str, request: Request) -> None:
    """verify() with the request's token header, read as the dependency reads it: by name in any letter case, the
    first one if it is sent twice.
    """
    verify(role, endpoint, request.headers.get(AUTH_HEADER))


def require_agent(endpoint: str) -> Callable[..., None]:
    """The dependency of a route that needs the agent token; `endpoint` is the route's fixed name for the log."""
    return _requirement("agent", endpoint)


def require_dispatcher(endpoint: str) -> Callable[..., None]:
    """The dependency of a route that needs the dispatcher token; `endpoint` is the route's fixed name for the log."""
    return _requirement("dispatcher", endpoint)
