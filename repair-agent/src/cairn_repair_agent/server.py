"""`RepairServerClient`: the agent's client for the Cairn server (spec §6.3-§6.5, §9.3).

Its own `requests.Session` with `trust_env = False` and no proxy, so the agent token, digests and raw output always
go straight to the settings file's `server`; no request follows a redirect (§9.9), and a redirect's body is never
read. The agent token goes only to the repair endpoints, in `X-Cairn-Repair-Token` (§7); a dev-0.3.0 port's
`X-API-Key`, when configured, goes with every request. A call never raises for an HTTP status: it returns the status
and the decoded body, and status 0 when no answer arrived. Nor does it raise for anything the request or the read of
its answer raises: `requests` lets an `OSError` through for a CA file removed after the start, and urllib3's
`LocationParseError` for a host it cannot encode, and each is status 0 with the exception's class name, as a refused
connection is.

An answer is read with `Accept-Encoding: identity` and at most `SERVER_ANSWER_MAX_BYTES` of decoded body. The
`server` URL comes from the settings file, which a local worker can point at a listener of its own (§9.3), so a larger
answer, or a small compressed one that would expand in memory, is closed past the cap and has no body, like one that
does not parse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

ROLE_HEADER = "X-Cairn-Repair-Token"
API_KEY_HEADER = "X-API-Key"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
RUN_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}")  # the server's rule (§6.1)
# Well above the largest real answer, a large project's GET /projects/{id}; GET /repair/runs at its limit of 500 runs
# is under 2 MB, even with intent texts of 2,000 characters.
SERVER_ANSWER_MAX_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 65536


class NoRedirectSession(requests.Session):
    """A session that never resolves a redirect, for both of the agent's clients (§9.3, Appendix C-1).

    With `allow_redirects=False`, requests still runs its redirect generator on every 3xx that has a `Location`, to
    fill `Response.next`: that reads the whole 3xx body into memory, outside any size cap or deadline, and with
    `max_redirects = 0` raises `TooManyRedirects` instead of returning the status. With no redirect target, a 3xx comes
    back as its status with its body unread, and nothing is ever sent to its `Location`.
    """

    def get_redirect_target(self, resp: requests.Response) -> str | None:
        return None


@dataclass(frozen=True)
class ServerAnswer:
    """One answer: `status` 0 means no HTTP answer arrived (`error` is then the exception's class name)."""

    status: int
    body: Any = None
    error: str | None = None

    @property
    def code(self) -> str | None:
        """The `code` of a repair error, `{"detail": {"code": ...}}` (§6.0); None for any other body."""
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        return code if isinstance(code, str) else None


class RepairServerClient:
    def __init__(self, server: str, token: str, *, api_key: str | None = None, ca_bundle: str | None = None) -> None:
        self._base = server.rstrip("/")
        self._token = token
        self._api_key = api_key
        session = NoRedirectSession()
        session.trust_env = False  # no proxy, CA or netrc setting from the environment
        session.proxies = {}
        session.max_redirects = 0
        session.verify = ca_bundle if (ca_bundle is not None and server.startswith("https://")) else True
        session.headers["Accept-Encoding"] = "identity"
        self._session = session

    @property
    def session(self) -> requests.Session:
        return self._session

    def close(self) -> None:
        self._session.close()

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        include_ended: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        timeout: float | None = None,
    ) -> ServerAnswer:
        params: dict[str, str] = {}
        if project_id is not None:
            params["project_id"] = project_id
        if include_ended:
            params["include_ended"] = "true"
        if limit is not None:
            params["limit"] = str(limit)
        if cursor is not None:
            params["cursor"] = cursor
        return self._request("GET", "/repair/runs", params=params, timeout=timeout)

    def get_run(self, run_id: str, *, timeout: float | None = None) -> ServerAnswer:
        return self._request("GET", f"/repair/runs/{_run_segment(run_id)}", timeout=timeout)

    def get_project(self, project_id: str, *, timeout: float | None = None) -> ServerAnswer:
        return self._request("GET", f"/projects/{quote(project_id, safe='')}", timeout=timeout)

    def post_review(self, run_id: str, body: dict[str, Any], *, timeout: float | None = None) -> ServerAnswer:
        return self._request("POST", f"/repair/runs/{_run_segment(run_id)}/reviews", json_body=body, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ServerAnswer:
        headers: dict[str, str] = {}
        if path.startswith("/repair/"):
            headers[ROLE_HEADER] = self._token
        if self._api_key is not None:
            headers[API_KEY_HEADER] = self._api_key
        read_timeout = READ_TIMEOUT_SECONDS if timeout is None else max(timeout, 0.001)
        try:
            response = self._session.request(
                method,
                self._base + path,
                params=params or None,
                json=json_body,
                headers=headers,
                timeout=(min(CONNECT_TIMEOUT_SECONDS, read_timeout), read_timeout),
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:  # a RequestException, or what requests lets through (see the module docstring)
            return ServerAnswer(status=0, error=type(exc).__name__)
        try:
            # A redirect is final by its status alone (§9.9), so its body is never read.
            content = None if 300 <= response.status_code < 400 else _read_capped(response)
        except Exception as exc:
            return ServerAnswer(status=0, error=type(exc).__name__)
        finally:
            response.close()
        body: Any = None
        if content:
            try:
                body = json.loads(content)
            except (ValueError, RecursionError):
                body = None
        return ServerAnswer(status=response.status_code, body=body)


def _read_capped(response: requests.Response) -> bytes | None:
    """The decoded body, or None once it passes SERVER_ANSWER_MAX_BYTES; the rest is never read."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(READ_CHUNK_BYTES):
        size += len(chunk)
        if size > SERVER_ANSWER_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _run_segment(run_id: str) -> str:
    if not RUN_ID_RULE.fullmatch(run_id):
        raise ValueError("run id does not match the server's rule")
    return run_id
