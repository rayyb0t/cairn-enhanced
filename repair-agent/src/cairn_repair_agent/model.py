"""The model client (spec §9.7, Appendix C-1): one client, a plain `requests` call with no SDK, in the wire format that
the reviewer's `api_format` names (owner decision O8, C-1 Wire formats):

- `openai_chat`, the OpenAI-compatible chat completions call, `POST {base_url}/chat/completions`, for a model server
  on your machine and for a hosted OpenAI-compatible API alike; its key, when one is configured, goes as
  `Authorization: Bearer <key>`;
- `anthropic_messages`, the Claude Messages API, `POST {base_url}/v1/messages`, for a reviewer with a Claude API key of
  its own; the key goes as `x-api-key`, with `anthropic-version: 2023-06-01`, and no `Authorization` header is sent.

Each reviewer in use gets its own `requests.Session` with `trust_env = False`: `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NETRC` and `REQUESTS_CA_BUNDLE` in the environment can never send a digest elsewhere, and no worker
variable (`OPENAI_*`, `ANTHROPIC_*`) is read. The destination, the key, the CA file and the proxy come only from the
reviewer's entry in the settings file. The session never resolves a redirect (`NoRedirectSession`), so a 3xx comes
back as its status with its body unread, and is `model_error` (C-1); nothing, the key included, reaches its Location.

`Reviewer.call` makes one review's model call: at most `max_attempts` attempts, each limited to
`min(timeout_seconds, time left before the model deadline)`, connect 5 s, and started only with at least 10 s left
before that deadline. A timeout, a connection error, 429 and 5xx are retried; nothing else is, and the client makes no
retries of its own. The status line and headers are read under the read timeout; the body is read in pieces against
the attempt's deadline, with the socket's timeout set to the time left before each read, at most 1 MiB, and parsed
with one guarded `json.loads`. A watchdog holds the whole read of the body to that deadline as well: one read can make
several socket reads inside urllib3 and http.client (a gzip body whose bytes decode to nothing yet, a chunk-size line),
so at the deadline it shuts the response's socket for reading, and the attempt is a timeout. Answers are mapped as C-1,
Answers and Wire formats, say. One `repair model call` line is logged per attempt (§9.11), holding only filtered
values: never a key, the digest or any answer text.

A reviewer whose settings leave `max_tokens` out gets the default (owner decision O10, `digest.default_max_tokens`),
which a model that allows fewer output tokens refuses with a 400 or 422 on every request. Such a status stays a
`model_error`, not retried; it also logs, once per reviewer and process, a WARNING that says to set `max_tokens`. The
answer's body is never read for it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError
from urllib3.exceptions import SSLError as Urllib3SSLError

from .config import ANTHROPIC_MESSAGES, ReviewerSettings
from .digest import prompt_tokens_estimate
from .prompt import DECISION_SCHEMA, chat_messages, messages_api_prompt
from .server import NoRedirectSession

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"  # O8: the value Claude Code's own health check sends
CONNECT_TIMEOUT_SECONDS = 5.0
MIN_ATTEMPT_SECONDS = 10.0  # an attempt starts only with at least this much time left before the model deadline
RETRY_PAUSE_SECONDS = 2.0  # before retrying a status or a connection error, when the time left allows it
MAX_BODY_BYTES = 1024 * 1024
READ_PIECE_BYTES = 65536
TOKEN_COUNT_MAX = 100_000_000
LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.ip_address("::1")

# §9.9 and §9.11: what may be posted and logged from an answer.
RESPONSE_MODEL_RULE = re.compile(r"[\x21-\x7e]{1,100}")
REQUEST_ID_RULE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
STOP_RULE = re.compile(r"[a-z_]{1,32}")
CLASS_NAME_RULE = re.compile(r"[A-Za-z0-9_]{1,64}")
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# The fallbacks of a model call (§9.10).
MODEL_TIMEOUT = "model_timeout"
MODEL_ERROR = "model_error"
INVALID_OUTPUT = "invalid_output"

# The outcomes of one attempt, as the `repair model call` line shows them (§9.11).
OK = "ok"
TIMEOUT = "timeout"
CONNECTION_ERROR = "connection_error"
REFUSAL = "refusal"
BAD_RESPONSE = "bad_response"
CONTEXT_TRUNCATED = "context_truncated"
NO_TIME_LEFT = "no time left for a model call"

_usage_warned: set[str] = set()
_usage_lock = threading.Lock()
# The statuses with which a provider refuses a max_tokens above what its model allows (O10), and the reviewers the
# WARNING about it has been logged for.
DEFAULT_MAX_TOKENS_REFUSED = frozenset({"status_400", "status_422"})
_default_max_tokens_warned: set[str] = set()
_default_max_tokens_lock = threading.Lock()


@dataclass(frozen=True)
class ModelClient:
    """The client of one reviewer: its session, the URL its requests go to, and its endpoint for log lines."""

    reviewer: ReviewerSettings
    session: requests.Session = field(repr=False)
    url: str
    endpoint: str


def endpoint_of(url: str) -> str:
    """`scheme://host:port` of a URL, never a path or credentials (C-1); the port is the default one if not given."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def is_plain_http_on_a_non_loopback_host(url: str) -> bool:
    """`http://` to a host other than `localhost`, `::1` or an address in 127.0.0.0/8 (§9.3)."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    if address.version == 4:
        return address not in LOOPBACK_V4
    return address != LOOPBACK_V6


def warn_if_plain_http(reviewer: ReviewerSettings) -> bool:
    """Log the plain-http WARNING once for a reviewer in use whose base_url needs it (§9.3); true if it did."""
    if not is_plain_http_on_a_non_loopback_host(reviewer.base_url):
        return False
    logger.warning(
        "repair model endpoint is plain http on a non-loopback host endpoint=%s", endpoint_of(reviewer.base_url)
    )
    return True


def build_model_client(reviewer: ReviewerSettings) -> ModelClient:
    """The reviewer's own client (C-1, Client): nothing from the environment, no redirects, and the URL and headers
    of its `api_format` (C-1, Wire formats). The key goes only in the header of that format."""
    session = NoRedirectSession()
    session.trust_env = False
    session.max_redirects = 0
    session.proxies = {"http": reviewer.proxy, "https": reviewer.proxy} if reviewer.proxy is not None else {}
    session.verify = reviewer.ca_bundle if reviewer.ca_bundle is not None else True
    session.headers["Accept-Encoding"] = "identity"
    if reviewer.api_format == ANTHROPIC_MESSAGES:
        if reviewer.api_key is not None:  # always set: the settings file's reader requires it for this format
            session.headers["x-api-key"] = reviewer.api_key
        session.headers["anthropic-version"] = ANTHROPIC_VERSION
        path = MESSAGES_PATH
    else:
        if reviewer.api_key is not None:
            session.headers["Authorization"] = f"Bearer {reviewer.api_key}"
        path = CHAT_COMPLETIONS_PATH
    return ModelClient(
        reviewer=reviewer,
        session=session,
        url=reviewer.base_url.rstrip("/") + path,
        endpoint=endpoint_of(reviewer.base_url),
    )


def request_body(reviewer: ReviewerSettings, digest_text: str) -> dict[str, Any]:
    """The JSON body of the reviewer's format (C-1, Request and Wire formats): no sampling parameter, no reasoning
    setting; with `anthropic_messages` also no `stream`, tools, thinking, `output_config` or schema."""
    if reviewer.api_format == ANTHROPIC_MESSAGES:
        system, messages = messages_api_prompt(digest_text)
        return {"model": reviewer.model, "max_tokens": reviewer.max_tokens, "system": system, "messages": messages}
    body: dict[str, Any] = {
        "model": reviewer.model,
        "messages": chat_messages(digest_text),
        "max_tokens": reviewer.max_tokens,
        "stream": False,
    }
    if reviewer.structured_output:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "repair_decision", "strict": True, "schema": DECISION_SCHEMA},
        }
    return body


def answer_text(text: str) -> str:
    """C-1's one rule: a text that, after leading whitespace, starts with `<think>` and contains `</think>` loses
    everything up to and including the first `</think>`. Nothing else is changed."""
    rest = text.lstrip()
    if rest.startswith(THINK_OPEN) and THINK_CLOSE in rest:
        return rest.split(THINK_CLOSE, 1)[1]
    return text


# --- one attempt's answer -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Exchange:
    """What one attempt got: its outcome (§9.11) and the filtered values read from the answer."""

    outcome: str
    stop: str = "-"
    model: str | None = None  # the response's `model`, when the filter accepts it
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str = "-"
    text: str | None = field(default=None, repr=False)  # the answer text as received
    max_tokens: bool = False  # the answer stopped at max_tokens
    usage_missing: bool = False  # no prompt-token count: the context check could not run


def token_count(usage: object, key: str) -> int | None:
    """A token count of `usage`: an integer that is not a boolean, from 0 to 100,000,000 (C-1, Answers); None for
    anything else, a `usage` that is null or not an object included."""
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if type(value) is int and 0 <= value <= TOKEN_COUNT_MAX:
        return value
    return None


def stop_value(value: object) -> str:
    """`stop` of §9.11: the reason when it is a short lower-case word, `other` for any other value, `-` for none."""
    if value is None:
        return "-"
    if isinstance(value, str) and STOP_RULE.fullmatch(value):
        return value
    return "other"


def response_model(answer: object) -> str | None:
    """The response's `model` when §9.9's filter accepts it: printable ASCII without spaces, at most 100 characters."""
    value = answer.get("model") if isinstance(answer, dict) else None
    if isinstance(value, str) and RESPONSE_MODEL_RULE.fullmatch(value):
        return value
    return None


def request_id_of(headers: Mapping[str, str], api_format: str) -> str:
    """The request id of §9.11: `x-request-id`, or with `anthropic_messages` `request-id` (else `x-request-id`)."""
    value = headers.get("request-id") if api_format == ANTHROPIC_MESSAGES else None
    if value is None:
        value = headers.get("x-request-id")
    if isinstance(value, str) and REQUEST_ID_RULE.fullmatch(value):
        return value
    return "-"


def _openai_text(content: object) -> str | None:
    """`message.content`: a string, or a list of text parts joined; None when it holds no text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part if isinstance(part, str) else part["text"]
            for part in content
            if isinstance(part, str)
            or (isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
        ]
        return "".join(parts) if parts else None
    return None


def _anthropic_text(content: list[Any]) -> str | None:
    """The `text` of every block of type `text` with a string `text`, joined in order; thinking and every other block
    are ignored. None when there is no such block."""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "".join(parts) if parts else None


def openai_answer(answer: object, prompt_est: int, request_id: str = "-") -> Exchange:
    """C-1, Answers, in its order: a body without `choices[0].message`, a refusal, a stop at `length`, a prompt cut
    to under half of `prompt_est`, else the answer text."""
    usage = answer.get("usage") if isinstance(answer, dict) else None
    first = None
    if isinstance(answer, dict):
        choices = answer.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
    finish = first.get("finish_reason") if first is not None else None
    values: dict[str, Any] = {
        "stop": stop_value(finish),
        "model": response_model(answer),
        "input_tokens": token_count(usage, "prompt_tokens"),
        "output_tokens": token_count(usage, "completion_tokens"),
        "request_id": request_id,
    }
    message = first.get("message") if first is not None else None
    if not isinstance(message, dict):
        return Exchange(BAD_RESPONSE, **values)
    text = _openai_text(message.get("content"))
    if message.get("refusal"):  # a non-empty refusal; null and "" are none
        return Exchange(REFUSAL, text=text, **values)
    if finish == "length":
        return Exchange(OK, text=text, max_tokens=True, **values)
    return _checked_prompt(Exchange(OK, text=text, **values), prompt_est)


def anthropic_prompt_tokens(usage: object) -> int | None:
    """The prompt's size (C-1, Wire formats): `input_tokens`, plus the cache counts where those are token counts;
    None without a token count in `input_tokens`."""
    count = token_count(usage, "input_tokens")
    if count is None:
        return None
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        cached = token_count(usage, key)
        if cached is not None:
            count += cached
    return count


def anthropic_answer(answer: object, prompt_est: int, request_id: str = "-") -> Exchange:
    """C-1, Wire formats: a body that is not an object whose `content` is a list, `stop_reason` `refusal`,
    `max_tokens`, `model_context_window_exceeded`, a prompt cut to under half of `prompt_est`, else the text.
    `tool_use`, `pause_turn` and any other stop reason go on to §9.8 with the answer's text."""
    usage = answer.get("usage") if isinstance(answer, dict) else None
    stop = answer.get("stop_reason") if isinstance(answer, dict) else None
    values: dict[str, Any] = {
        "stop": stop_value(stop),
        "model": response_model(answer),
        "input_tokens": anthropic_prompt_tokens(usage),
        "output_tokens": token_count(usage, "output_tokens"),
        "request_id": request_id,
    }
    content = answer.get("content") if isinstance(answer, dict) else None
    if not isinstance(content, list):
        return Exchange(BAD_RESPONSE, **values)
    text = _anthropic_text(content)
    if stop == "refusal":
        return Exchange(REFUSAL, text=text, **values)
    if stop == "max_tokens":
        return Exchange(OK, text=text, max_tokens=True, **values)
    if stop == "model_context_window_exceeded":
        return Exchange(CONTEXT_TRUNCATED, text=text, **values)
    return _checked_prompt(Exchange(OK, text=text, **values), prompt_est)


def _checked_prompt(exchange: Exchange, prompt_est: int) -> Exchange:
    """The context-truncation check (C-1, Answers): a prompt count under half of the agent's estimate means the
    server cut the prompt, so the answer is not used. Without a count, the answer is used and the check cannot run.

    It sees a cut only when the server's context is below about `prompt_est`: Ollama cuts a prompt to about half its
    context, so a cut prompt's count is under half the estimate only for a context under the estimate, a few thousand
    tokens. What keeps a prompt whole is the digest's room (digest.DigestBudget), when the reviewer's context_window is
    the server's real context; a reviewer without one, or with one larger than that context, can have its prompt cut
    unseen by a server whose context is above the estimate. The estimate is calibrated, not a bound, in both directions:
    very compressible run text can put a prompt the server read whole under half of it, which makes the review a
    recorded continue (`context_truncated`), the fail-safe side."""
    if exchange.input_tokens is None:
        return replace(exchange, usage_missing=True)
    if 2 * exchange.input_tokens < prompt_est:
        return replace(exchange, outcome=CONTEXT_TRUNCATED)
    return exchange


# --- the call -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelResult:
    """One review's model call. With `fallback` None, `text` is the answer (after the think-block rule) for §9.8;
    otherwise `fallback` is `model_timeout`, `model_error` or `invalid_output` and `cause` goes into the posted
    reason. `raw_output` is the answer text as received, `model` the value posted and logged (None when no attempt
    was made)."""

    fallback: str | None
    cause: str
    text: str | None = field(default=None, repr=False)
    raw_output: str | None = field(default=None, repr=False)
    model: str | None = None
    attempts: int = 0


class _AttemptTimeoutError(Exception):
    """The attempt's deadline passed while its body was read."""


class _BodyTooLargeError(Exception):
    """More than MAX_BODY_BYTES of decoded body."""


def read_body(response: requests.Response, deadline: float, monotonic: Callable[[], float]) -> bytes:
    """The decoded body, read in pieces against the attempt's deadline (C-1, Client).

    Before each read the socket's timeout is set to the time left, when urllib3 still holds the connection: it
    releases it, and `connection` becomes None, once the last byte of a body with Content-Length has been read.
    `read1` returns as soon as any bytes arrive; `decode_content=True` decodes a body that a server compressed
    although the request asked for `identity`. Raises _AttemptTimeoutError, _BodyTooLargeError, or urllib3's own errors.
    """
    raw = response.raw
    chunks: list[bytes] = []
    size = 0
    while True:
        left = deadline - monotonic()
        if left <= 0:
            raise _AttemptTimeoutError
        connection = getattr(raw, "connection", None)
        sock = getattr(connection, "sock", None) if connection is not None else None
        if sock is not None:
            sock.settimeout(left)
        piece = raw.read1(min(READ_PIECE_BYTES, MAX_BODY_BYTES + 1 - size), decode_content=True)
        if not piece:
            return b"".join(chunks)
        size += len(piece)
        if size > MAX_BODY_BYTES:
            raise _BodyTooLargeError
        chunks.append(piece)


class _Watchdog:
    """Holds the whole read of an answer's body to the attempt's deadline (C-1, Client).

    `read_body` checks the deadline between reads and sets the socket's timeout to the time left before each one, but
    one `read1` call can make several socket reads inside urllib3 and http.client, each allowed that whole time: urllib3
    reads on while a gzip body's bytes decode to nothing (a gzip header's file name, sent a byte at a time), and
    http.client reads a chunk-size line (with its chunk extension) to its end. Such a body, sent a byte now and then,
    held an attempt of 1 s for as long as the server kept sending. So at the deadline the watchdog shuts the response's
    socket for reading (urllib3's `HTTPResponse.shutdown`), which ends any read in progress, and `disarm` says that it
    did: the attempt is then a timeout, whatever that read returned or raised."""

    def __init__(self, raw: Any, seconds: float) -> None:
        self._raw = raw
        self._lock = threading.Lock()
        self._disarmed = False
        self._fired = False
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.name = "repair-model-watchdog"
        self._timer.daemon = True
        try:
            self._timer.start()
        except RuntimeError:  # no thread could be started: each read is still bounded by the time left (read_body)
            self._disarmed = True

    def _fire(self) -> None:
        with self._lock:
            if self._disarmed:
                return
            self._fired = True
            with contextlib.suppress(Exception):  # a connection already released or closed has nothing left to read
                self._raw.shutdown()

    def disarm(self) -> bool:
        """Stop the watchdog once the read has ended; true when the deadline came first."""
        with self._lock:
            self._disarmed = True
        self._timer.cancel()
        return self._fired


def _error_outcome(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"error_{name if CLASS_NAME_RULE.fullmatch(name) else 'other'}"


def _retryable(outcome: str) -> bool:
    if outcome in (TIMEOUT, CONNECTION_ERROR):
        return True
    if outcome.startswith("status_"):
        code = int(outcome.removeprefix("status_"))
        return code == 429 or 500 <= code <= 599
    return False


def _count(value: int | None) -> object:
    return "-" if value is None else value


def _warn_usage_missing(endpoint: str) -> None:
    with _usage_lock:
        if endpoint in _usage_warned:
            return
        _usage_warned.add(endpoint)
    logger.warning("repair model usage missing, a cut prompt cannot be detected endpoint=%s", endpoint)


def _warn_if_the_default_max_tokens_was_refused(reviewer: ReviewerSettings, outcome: str) -> None:
    """Once per reviewer and process: an attempt whose max_tokens came from the default got a 400 or 422, the
    statuses with which a provider refuses a max_tokens above what its model allows (O10). Logged from the status
    alone: an error status's body is never read (C-1)."""
    if outcome not in DEFAULT_MAX_TOKENS_REFUSED or not reviewer.max_tokens_is_default:
        return
    with _default_max_tokens_lock:
        if reviewer.name in _default_max_tokens_warned:
            return
        _default_max_tokens_warned.add(reviewer.name)
    logger.warning(
        "repair model refused a request with the default max_tokens, set max_tokens if the model allows fewer "
        "reviewer=%s max_tokens=%s status=%s",
        reviewer.name,
        reviewer.max_tokens,
        outcome.removeprefix("status_"),
    )


class Reviewer:
    """One reviewer's model call, on its own client, against a deadline on the monotonic clock (§9.4, §9.7)."""

    def __init__(
        self,
        client: ModelClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self._monotonic = monotonic
        self._sleep = sleep

    @property
    def settings(self) -> ReviewerSettings:
        return self.client.reviewer

    def call(self, digest_text: str, *, run: str, deadline: float) -> ModelResult:
        """At most `max_attempts` attempts, each with `min(timeout_seconds, time left)` and at least 10 s left before
        `deadline` (the model deadline, `accept_until - 10 s`, on the monotonic clock), so none starts after it."""
        reviewer = self.client.reviewer
        body = request_body(reviewer, digest_text)
        prompt_est = prompt_tokens_estimate(digest_text)
        attempts = 0
        last: Exchange | None = None
        while attempts < reviewer.max_attempts:
            left = deadline - self._monotonic()
            if left < MIN_ATTEMPT_SECONDS:
                break
            attempts += 1
            limit = min(float(reviewer.timeout_seconds), left)
            started = self._monotonic()
            last = self._attempt(body, started + limit, limit, prompt_est)
            self._log(run, attempts, last, prompt_est, started)
            _warn_if_the_default_max_tokens_was_refused(reviewer, last.outcome)
            if not _retryable(last.outcome):
                break
            if last.outcome != TIMEOUT and attempts < reviewer.max_attempts:
                pause = min(RETRY_PAUSE_SECONDS, deadline - self._monotonic() - MIN_ATTEMPT_SECONDS)
                if pause > 0:
                    self._sleep(pause)
        return self._result(last, attempts)

    def _attempt(self, body: dict[str, Any], attempt_deadline: float, limit: float, prompt_est: int) -> Exchange:
        api_format = self.client.reviewer.api_format
        try:
            response = self.client.session.post(
                self.client.url,
                json=body,
                stream=True,
                allow_redirects=False,
                timeout=(min(CONNECT_TIMEOUT_SECONDS, limit), limit),
            )
        except requests.exceptions.Timeout:  # ConnectTimeout and ReadTimeout (the status line and headers)
            return Exchange(TIMEOUT)
        except requests.exceptions.ConnectionError:
            return Exchange(CONNECTION_ERROR)
        except Exception as exc:  # §9.7: never retried, and never ends the review thread
            return Exchange(_error_outcome(exc))
        request_id = "-"
        try:
            request_id = request_id_of(response.headers, api_format)
            status = response.status_code
            if not 200 <= status <= 299:  # a redirect or an error status: its body is never read
                return Exchange(f"status_{status}", request_id=request_id)
            content, outcome = self._read(response, attempt_deadline)
            if outcome != OK:
                return Exchange(outcome, request_id=request_id)
            try:
                answer = json.loads(content)
            except (ValueError, RecursionError):
                return Exchange(BAD_RESPONSE, request_id=request_id)
            if api_format == ANTHROPIC_MESSAGES:
                exchange = anthropic_answer(answer, prompt_est, request_id)
            else:
                exchange = openai_answer(answer, prompt_est, request_id)
            if exchange.usage_missing:
                _warn_usage_missing(self.client.endpoint)
            return exchange
        except Exception as exc:
            return Exchange(_error_outcome(exc), request_id=request_id)
        finally:
            response.close()

    def _read(self, response: requests.Response, attempt_deadline: float) -> tuple[bytes, str]:
        """The body and `ok`, or what ended its read (C-1, Client): `timeout`, `bad_response` (past 1 MiB, or a body
        that cannot be decoded), `connection_error`, or `error_<class>` for an exception the rules do not name. The
        watchdog makes it a `timeout` when the attempt's deadline came before the read ended."""
        watchdog = _Watchdog(response.raw, attempt_deadline - self._monotonic())
        content, outcome = b"", OK
        try:
            content = read_body(response, attempt_deadline, self._monotonic)
        except (_AttemptTimeoutError, ReadTimeoutError):
            outcome = TIMEOUT
        except (_BodyTooLargeError, DecodeError):
            outcome = BAD_RESPONSE
        except (ProtocolError, Urllib3SSLError):
            outcome = CONNECTION_ERROR
        except Exception as exc:  # §9.7: model_error with the exception's class name, not retried
            outcome = _error_outcome(exc)
        finally:
            deadline_came_first = watchdog.disarm()
        return (b"", TIMEOUT) if deadline_came_first else (content, outcome)

    def _posted_model(self, exchange: Exchange | None) -> str:
        if exchange is not None and exchange.model is not None:
            return exchange.model
        return self.client.reviewer.model

    def _log(self, run: str, attempt: int, exchange: Exchange, prompt_est: int, started: float) -> None:
        logger.info(
            "repair model call run=%s reviewer=%s model=%s attempt=%s outcome=%s stop=%s input_tokens=%s "
            "prompt_est=%s output_tokens=%s ms=%s request_id=%s",
            run,
            self.client.reviewer.name,
            self._posted_model(exchange),
            attempt,
            exchange.outcome,
            exchange.stop,
            _count(exchange.input_tokens),
            prompt_est,
            _count(exchange.output_tokens),
            max(0, round((self._monotonic() - started) * 1000)),
            exchange.request_id,
        )

    def _result(self, last: Exchange | None, attempts: int) -> ModelResult:
        if last is None:
            return ModelResult(MODEL_TIMEOUT, NO_TIME_LEFT)
        model = self._posted_model(last)
        if last.outcome == OK:
            if last.max_tokens:
                return ModelResult(INVALID_OUTPUT, "max_tokens", raw_output=last.text, model=model, attempts=attempts)
            text = answer_text(last.text or "")
            return ModelResult(None, "", text=text, raw_output=last.text, model=model, attempts=attempts)
        if last.outcome == TIMEOUT:
            return ModelResult(MODEL_TIMEOUT, TIMEOUT, model=model, attempts=attempts)
        return ModelResult(MODEL_ERROR, last.outcome, raw_output=last.text, model=model, attempts=attempts)
