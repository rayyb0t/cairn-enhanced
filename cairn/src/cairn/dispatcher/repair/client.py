"""RepairRunsClient: the repair side's requests to the Cairn server (spec 8.6).

One instance per thread, each with its own requests.Session: each run's reporter thread has one, and so does the
housekeeping thread. A reporter thread registers its run and reports its session id and its end
(PUT /repair/runs/{run_id} and POST /repair/runs/{run_id}/end, with the dispatcher token). The housekeeping
thread only reads two existing endpoints, GET /projects and GET /projects/{id}, without the token.

Nothing here raises for a server that answers badly or not at all, and nothing is logged but the one line of a
configuration error (once per process) and the "session rejected" line of spec 8.7. No request follows a
redirect, so the token never goes to another address.

Every register and end body passes through one method, which applies the session-id rule last of all: a
session_id that fails it is sent as null, with the one "session rejected" WARNING (its length and SHA-256 prefix
only), so the run is registered without one, as spec 8.7 says, even if a caller did not apply the rule first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import requests
from pydantic import TypeAdapter

from cairn.dispatcher.repair.session_ids import accept_session_id, valid_project_id, valid_run_id, valid_session_id
from cairn.server.models import ProjectSummary

LOG = logging.getLogger(__name__)

REPAIR_AUTH_HEADER = "X-Cairn-Repair-Token"  # the dispatcher token, on register and end only
API_KEY_HEADER = "X-API-Key"  # a dev-0.3.0 port only, sent as that branch's CairnClient sends it

CALL_TIMEOUT_SECONDS = (2.0, 3.0)  # register and end: connect, read (spec 8.5)
PROJECT_READ_TIMEOUT_SECONDS = (2.0, 10.0)  # the housekeeping's reads: CairnClient's read timeout
NO_ANSWER = 0
ERROR_BODY_MAX_BYTES = 4096  # a repair error body is far smaller; a larger one is read as having no code
PROJECT_GONE_READ_BYTES = 64
# What get_project_or_404 (server/services.py) raises for a project that does not exist, as FastAPI's default
# handler writes it. Any other 404 body, such as {"detail":"Not Found"} for a route the server lacks, is not it.
PROJECT_NOT_FOUND_BODY = b'{"detail":"Project not found"}'
CODE_RULE = re.compile(r"[a-z0-9_]{1,64}")  # the server's codes; any other value is never logged

AnswerKind = Literal["ok", "retry", "not_configured", "refused", "failed"]
RETRY_STATUSES = frozenset({NO_ANSWER, 408, 429, 500, 502, 504})


@dataclass(frozen=True, slots=True)
class Answer:
    """The server's answer to a register or end call.

    status is 0 when no answer arrived. code is the code of a repair error body, {"detail": {"code": ...}}, and
    None when the body has no such code. kind is what the reporter does with it (classify).
    """

    status: int
    code: str | None
    kind: AnswerKind

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def classify(status: int, code: str | None) -> AnswerKind:
    """The Answers table of spec 8.6.

    - ok: a 2xx. The body is ignored.
    - retry: no answer, 408, 429, 500, 502, 504, or a 503 without code token_not_configured; retried up to the
      call's attempt limit.
    - not_configured: 401, 503 token_not_configured, or a 404 without a code (a server without the repair
      router); logged once per process; reporting stops for the run.
    - refused: 403 (project_inactive), a 404 with a code, or 409; an expected refusal. A refused register stops
      reporting; a refused session or end call does not.
    - failed: 422, a version-skew bug, and every status the table does not list; not retried. A failed register
      stops reporting; a failed session or end call does not.
    """
    if 200 <= status < 300:
        return "ok"
    if status == 503:
        return "not_configured" if code == "token_not_configured" else "retry"
    if status in RETRY_STATUSES:
        return "retry"
    if status == 401:
        return "not_configured"
    if status == 404:
        return "refused" if code is not None else "not_configured"
    if status in (403, 409):
        return "refused"
    return "failed"


class _Once:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False

    def first(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True


_SERVER_NOT_CONFIGURED = _Once()


class RepairRunsClient:
    """The repair side's requests to the server at base_url (dispatch.yaml's server), for one thread.

    token is the dispatcher token, which only register and end send. The session is created on the first
    request, so building a client does no I/O. Like CairnClient's, it follows the proxy settings of the
    environment (requests' default).
    """

    def __init__(self, base_url: str, token: str | None = None, *, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._api_key = api_key
        self._session: requests.Session | None = None
        self._summary_adapter: TypeAdapter[list[ProjectSummary]] | None = None

    def register(self, run_id: str, body: dict[str, Any]) -> Answer:
        """PUT /repair/runs/{run_id}: the register call, and the session call (the accepted body with session_id)."""
        return self._call("PUT", f"/repair/runs/{_run_path(run_id)}", run_id, body)

    def end(self, run_id: str, body: dict[str, Any]) -> Answer:
        """POST /repair/runs/{run_id}/end: the exit fields when the process exits, then the task's outcome."""
        return self._call("POST", f"/repair/runs/{_run_path(run_id)}/end", run_id, body)

    def list_projects(self) -> list[ProjectSummary] | None:
        """GET /projects, parsed as CairnClient.list_projects parses it; None for any other answer or no answer."""
        try:
            response = self._requests().get(
                f"{self._base_url}/projects",
                headers=self._headers(),
                timeout=PROJECT_READ_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except Exception:
            return None
        try:
            if not 200 <= response.status_code < 300:
                return None
            if self._summary_adapter is None:
                self._summary_adapter = TypeAdapter(list[ProjectSummary])
            return self._summary_adapter.validate_python(response.json())
        except Exception:
            return None
        finally:
            response.close()

    def project_gone(self, project_id: str) -> bool:
        """Whether GET /projects/{id} answers 404 with exactly the server's "Project not found" body.

        At most the first 64 bytes of a 404's body are read, and no other body; nothing is parsed. Any other
        answer, no answer, or an id that fails the id rule (no request is sent then) is False.
        """
        if not valid_project_id(project_id):
            return False
        try:
            response = self._requests().get(
                f"{self._base_url}/projects/{quote(project_id, safe='')}",
                headers={**self._headers(), "Accept-Encoding": "identity"},
                timeout=PROJECT_READ_TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
            )
        except Exception:
            return False
        try:
            if response.status_code != 404:
                return False
            return response.raw.read(PROJECT_GONE_READ_BYTES, decode_content=False) == PROJECT_NOT_FOUND_BODY
        except Exception:
            return False
        finally:
            response.close()

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):  # best effort; nothing else may be logged
                session.close()

    def _call(self, method: str, path: str, run_id: str, body: dict[str, Any]) -> Answer:
        # path holds run_id, which _run_path has checked, so run_id may be logged below.
        if self._token is None:
            raise ValueError("register and end calls need the dispatcher token")
        body = _session_id_checked(run_id, body)
        headers = {**self._headers(), REPAIR_AUTH_HEADER: self._token, "Accept-Encoding": "identity"}
        try:
            response = self._requests().request(
                method,
                f"{self._base_url}{path}",
                json=body,
                headers=headers,
                timeout=CALL_TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
            )
        except Exception:  # any transport, URL or encoding error counts as no answer
            return _answer(NO_ANSWER, None)
        try:
            status = response.status_code
            code = None if 200 <= status < 300 else _repair_code(response)
        finally:
            response.close()
        return _answer(status, code)

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self._api_key} if self._api_key else {}

    def _requests(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session


def _run_path(run_id: str) -> str:
    if not valid_run_id(run_id):
        raise ValueError("run id does not pass the run-id rule")
    return quote(run_id, safe="")


def _session_id_checked(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """body, or a copy of it with session_id null if its session_id fails the session-id rule (spec 8.7).

    The caller's body is never changed. A rejected string is logged once here, as accept_session_id logs it, with
    "-" for an id the body does not hold.
    """
    value = body.get("session_id")
    if value is None or valid_session_id(value):
        return body
    accept_session_id(
        value,
        project_id=body.get("project_id", "-"),
        intent_id=body.get("intent_id", "-"),
        worker=body.get("worker", "-"),
        run_id=run_id,
    )
    return {**body, "session_id": None}


def _answer(status: int, code: str | None) -> Answer:
    answer = Answer(status=status, code=code, kind=classify(status, code))
    if answer.kind == "not_configured" and _SERVER_NOT_CONFIGURED.first():
        LOG.warning("repair shadow server not configured status=%s code=%s", status, code or "-")
    return answer


def _repair_code(response: requests.Response) -> str | None:
    """The code of a repair error body, {"detail": {"code": "<code>", ...}}, if it passes CODE_RULE; else None."""
    try:
        raw = response.raw.read(ERROR_BODY_MAX_BYTES + 1, decode_content=True)
    except Exception:
        return None
    if not raw or len(raw) > ERROR_BODY_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and CODE_RULE.fullmatch(code) else None
