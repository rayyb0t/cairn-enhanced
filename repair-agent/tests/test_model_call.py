"""The model call in both wire formats (spec §9.7, Appendix C-1, owner decision O8): AG32-AG36, AG43, AG44, AG48,
AG52, AG60 and AG61.

Every call goes to a loopback stub (`agentkit.ModelStub`) with made-up keys and `test-model`; no model provider is
ever contacted. The read cases run on real sockets with a 1 s attempt limit; the per-attempt limits of AG52 run on a
fake monotonic clock, with a transport that never answers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
import urllib3.response
from agentkit import (
    EXTEND,
    MESSAGES_USAGE,
    MODEL_NAME,
    OMIT,
    USAGE,
    FakeClock,
    ModelStub,
    Reply,
    chat_answer,
    decision_text,
    messages_answer,
    model_stubs,
    text_block,
)
from conftest import REVIEWER_KEY, TcpListener, fake_secret, reviewer_entry, settings_data

from cairn_repair_agent import model
from cairn_repair_agent.config import ANTHROPIC_MESSAGES, OPENAI_CHAT, parse_settings
from cairn_repair_agent.decision import InvalidOutputError, validate_answer
from cairn_repair_agent.digest import prompt_tokens_estimate
from cairn_repair_agent.model import ModelResult, Reviewer, build_model_client
from cairn_repair_agent.prompt import DECISION_SCHEMA, SYSTEM_PROMPT

DIGEST = "REVIEW CONTEXT (from Cairn; trusted)\n" + "x" * 6000  # a stand-in digest; the request carries it as is
PROMPT_EST = prompt_tokens_estimate(DIGEST)
RUN = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"
FORMATS = (OPENAI_CHAT, ANTHROPIC_MESSAGES)
SAMPLING = ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "seed", "stop", "n")


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture(autouse=True)
def _fresh_usage_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage WARNING is once per process and endpoint: each test starts as a new process would."""
    monkeypatch.setattr(model, "_usage_warned", set())


def base_url_of(stub: ModelStub, api_format: str) -> str:
    """The reviewer's base_url: with /v1 for openai_chat, without it for anthropic_messages (C-1, Wire formats)."""
    return stub.url + "/v1" if api_format == OPENAI_CHAT else stub.url


def reviewer_on(
    base_url: str,
    api_format: str = OPENAI_CHAT,
    *,
    sleep: Callable[[float], None] = lambda seconds: None,
    monotonic: Callable[[], float] = time.monotonic,
    **settings: Any,
) -> Reviewer:
    """A reviewer of the settings file, parsed as the agent parses it, and its client. Retry pauses are skipped."""
    entry = reviewer_entry(base_url=base_url, api_format=api_format, **settings)
    if api_format == ANTHROPIC_MESSAGES and entry["api_key"] is None:
        entry["api_key"] = REVIEWER_KEY  # required with this format
    parsed = parse_settings(settings_data(reviewers=[entry]), "agent-settings.json").reviewers[0]
    return Reviewer(build_model_client(parsed), monotonic=monotonic, sleep=sleep)


def call(reviewer: Reviewer, *, seconds: float = 600, digest: str = DIGEST) -> ModelResult:
    return reviewer.call(digest, run=RUN, deadline=time.monotonic() + seconds)


def answer_for(api_format: str, **fields: Any) -> dict[str, Any]:
    return chat_answer(**fields) if api_format == OPENAI_CHAT else messages_answer(**fields)


def call_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """The `repair model call` lines, as key=value maps (every value is filtered, so it holds no space)."""
    lines = []
    for record in caplog.records:
        text = record.getMessage()
        if record.name == "cairn_repair_agent.model" and text.startswith("repair model call "):
            lines.append(dict(item.split("=", 1) for item in text.removeprefix("repair model call ").split(" ")))
    return lines


def usage_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("repair model usage missing")]


def assert_kept(result: ModelResult, text: str | None = None) -> None:
    """The answer goes on to §9.8 and passes it."""
    assert result.fallback is None, (result.fallback, result.cause)
    if text is not None:
        assert result.text == text
    validate_answer(result.text or "", {"E1", "E2"}, set())


# --- AG43 and AG60: the requests ------------------------------------------------------------------------------------


@pytest.mark.parametrize("structured_output", [True, False])
@pytest.mark.parametrize("key", [None, "key"])
def test_openai_compatible_request_shape(model_stub: Any, structured_output: bool, key: str | None) -> None:
    """AG43: `POST <base_url>/chat/completions` with exactly the body keys of C-1's Request, `max_tokens` from the
    reviewer, `response_format` only with `structured_output`, no sampling parameter, `Authorization` only with a key,
    and neither `x-api-key` nor `anthropic-version`, which only `anthropic_messages` sends."""
    stub = model_stub()
    api_key = REVIEWER_KEY if key else None
    reviewer = reviewer_on(stub.url + "/v1", api_key=api_key, max_tokens=2048, structured_output=structured_output)
    assert_kept(call(reviewer))
    assert [(r.method, r.path) for r in stub.requests] == [("POST", "/v1/chat/completions")]
    sent = stub.requests[0]
    body = sent.json()
    expected = {"model", "messages", "max_tokens", "stream"} | ({"response_format"} if structured_output else set())
    assert set(body) == expected
    assert body["model"] == MODEL_NAME and body["max_tokens"] == 2048 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": DIGEST}]
    if structured_output:
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "repair_decision", "strict": True, "schema": DECISION_SCHEMA},
        }
    assert not set(SAMPLING) & set(body)
    headers = {name.lower(): value for name, value in sent.headers.items()}
    if key:
        assert headers["authorization"] == f"Bearer {REVIEWER_KEY}"
    else:
        assert "authorization" not in headers
    assert "x-api-key" not in headers and "anthropic-version" not in headers and "anthropic-beta" not in headers
    assert headers["accept-encoding"] == "identity" and headers["content-type"] == "application/json"


@pytest.mark.parametrize("slash", ["", "/"])
def test_anthropic_messages_request_shape(model_stub: Any, slash: str) -> None:
    """AG60: one `POST <base_url>/v1/messages` per attempt, with and without a trailing `/` on `base_url`, with
    `x-api-key`, `anthropic-version: 2023-06-01`, `Content-Type: application/json` and `Accept-Encoding: identity`, no
    `Authorization` or `anthropic-beta` header, and a body of exactly `model`, `max_tokens`, `system` and one user
    message holding the digest: no stream, tools, thinking, output_config, response_format or sampling."""
    stub = model_stub([Reply(500), Reply(body=messages_answer())])
    reviewer = reviewer_on(stub.url + slash, ANTHROPIC_MESSAGES, api_key=REVIEWER_KEY, max_tokens=3000)
    result = call(reviewer)
    assert_kept(result)
    assert [(r.method, r.path) for r in stub.requests] == [("POST", "/v1/messages")] * 2  # one per attempt
    for sent in stub.requests:
        body = sent.json()
        assert set(body) == {"model", "max_tokens", "system", "messages"}
        assert body == {
            "model": MODEL_NAME,
            "max_tokens": 3000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": DIGEST}],
        }
        headers = {name.lower(): value for name, value in sent.headers.items()}
        assert headers["x-api-key"] == REVIEWER_KEY
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"
        assert headers["accept-encoding"] == "identity"
        assert "authorization" not in headers and "anthropic-beta" not in headers


def test_anthropic_messages_request_shape_ignores_the_environment(
    model_stub: Any, tcp_listener: Callable[..., TcpListener], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG60: with a capturing proxy named in HTTP_PROXY, HTTPS_PROXY and ALL_PROXY (upper and lower case), and a
    worker's ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS, all set while
    the client is built (the guard bypassed), the request still goes to `base_url` only, with the reviewer's own key,
    and no header holds one of their values; through the reviewer's own `proxy`, it goes to that proxy only."""
    stub, worker, proxy = model_stub(Reply(body=messages_answer())), model_stub(), tcp_listener()
    worker_values = {
        "ANTHROPIC_API_KEY": fake_secret("worker-anthropic-key"),
        "ANTHROPIC_AUTH_TOKEN": fake_secret("worker-anthropic-token"),
        "ANTHROPIC_BASE_URL": worker.url,
        "ANTHROPIC_CUSTOM_HEADERS": "X-Worker-Canary: " + fake_secret("worker-header"),
    }
    for name, value in worker_values.items():
        monkeypatch.setenv(name, value)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy.url)
        monkeypatch.setenv(name.lower(), proxy.url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    reviewer = reviewer_on(stub.url, ANTHROPIC_MESSAGES, api_key=REVIEWER_KEY)
    assert_kept(call(reviewer))
    assert len(stub.requests) == 1 and worker.requests == [] and proxy.connections == 0
    headers = stub.requests[0].headers
    assert headers["x-api-key"] == REVIEWER_KEY
    for value in worker_values.values():
        assert all(value.split(": ", 1)[-1] not in header for header in headers.values())
    assert "X-Worker-Canary" not in headers
    # The reviewer's own proxy is the only one used: the capturing proxy is that proxy now.
    proxied = reviewer_on(stub.url, ANTHROPIC_MESSAGES, api_key=REVIEWER_KEY, proxy=proxy.url)
    result = call(proxied)
    assert (result.fallback, result.cause) == ("model_error", "connection_error")
    assert proxy.connections == 2 and len(stub.requests) == 1  # two attempts, both to the proxy


@pytest.mark.parametrize("api_format", FORMATS)
def test_anthropic_messages_request_shape_follows_no_redirect(
    model_stub: Any, tcp_listener: Callable[..., TcpListener], api_format: str
) -> None:
    """AG60 (and AG44 for openai_chat): a 307 to another host is `model_error` with its status, not retried, and
    that host receives nothing, so the key never reaches it."""
    target = tcp_listener("127.0.0.2")
    stub = model_stub(Reply(307, headers={"Location": target.url + "/v1/messages"}))
    result = call(reviewer_on(base_url_of(stub, api_format), api_format, api_key=REVIEWER_KEY))
    assert (result.fallback, result.cause) == ("model_error", "status_307")
    assert len(stub.requests) == 1 and target.connections == 0


# --- AG44 and AG61: the answers -------------------------------------------------------------------------------------


def test_openai_compatible_answers(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG44: a valid answer is kept, with its text as received as the raw output; a leading think block is removed
    and one that is not at the start is not, so that answer is `invalid_output`; `length` is `invalid_output`
    (`max_tokens`); a refusal is `model_error`; a 5xx and a 429 are retried once when time allows; a 400, and a 408 as
    any other 4xx, are not retried; a non-JSON body is `model_error`, `bad_response`."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    think = "<think>the run is stuck; E1 shows it</think>\n" + EXTEND
    cases: list[tuple[list[Reply], tuple[str | None, str], int]] = [
        ([Reply(body=chat_answer(EXTEND))], (None, ""), 1),
        ([Reply(body=chat_answer("  \n" + think))], (None, ""), 1),
        ([Reply(body=chat_answer(decision_text(), finish_reason="length"))], ("invalid_output", "max_tokens"), 1),
        ([Reply(body=chat_answer(None, refusal="I cannot help with that."))], ("model_error", "refusal"), 1),
        ([Reply(503), Reply(body=chat_answer(EXTEND))], (None, ""), 2),
        ([Reply(429), Reply(body=chat_answer(EXTEND))], (None, ""), 2),
        ([Reply(400), Reply(body=chat_answer(EXTEND))], ("model_error", "status_400"), 1),
        ([Reply(408), Reply(body=chat_answer(EXTEND))], ("model_error", "status_408"), 1),  # any other 4xx (§9.7)
        ([Reply(raw=b"<html>busy</html>")], ("model_error", "bad_response"), 1),
    ]
    for replies, expected, requests_made in cases:
        stub = model_stub(replies)
        result = call(reviewer_on(stub.url + "/v1"))
        assert (result.fallback, result.cause) == expected, replies
        assert len(stub.requests) == requests_made
    kept = call(reviewer_on(model_stub(Reply(body=chat_answer("  \n" + think))).url + "/v1"))
    assert kept.text is not None and kept.text.strip() == EXTEND and kept.raw_output == "  \n" + think
    assert validate_answer(kept.text, {"E1"}, set()).decision == "extend"
    not_leading = call(reviewer_on(model_stub(Reply(body=chat_answer("Answer: " + think))).url + "/v1"))
    assert not_leading.fallback is None and not_leading.text == "Answer: " + think
    with pytest.raises(InvalidOutputError, match="not JSON"):
        validate_answer(not_leading.text or "", {"E1"}, set())
    for record in caplog.records:  # no answer text, digest or key in any line
        assert "the run is stuck" not in record.getMessage() and "x" * 50 not in record.getMessage()


def test_openai_compatible_answers_model_name_and_context_check(
    model_stub: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AG44: a response `model` of 150 characters gives the reviewer's configured `model`, posted and logged; a valid
    extend whose `usage.prompt_tokens` is under half of `prompt_est` is `model_error`, `context_truncated`, and one at
    half or above keeps its decision."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND, model="m" * 150)))
    result = call(reviewer_on(stub.url + "/v1", model="configured-model"))
    assert result.model == "configured-model" and call_lines(caplog)[-1]["model"] == "configured-model"
    half = -(-PROMPT_EST // 2)  # the smallest count at half or above
    for count, expected in ((half - 1, ("model_error", "context_truncated")), (half, (None, ""))):
        stub = model_stub(Reply(body=chat_answer(EXTEND, usage={"prompt_tokens": count, "completion_tokens": 9})))
        result = call(reviewer_on(stub.url + "/v1"))
        assert (result.fallback, result.cause) == expected, count
        line = call_lines(caplog)[-1]
        assert line["input_tokens"] == str(count) and line["prompt_est"] == str(PROMPT_EST)
        assert line["outcome"] == ("context_truncated" if count < half else "ok")


def test_openai_compatible_answers_without_usage(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG44: two valid answers without `usage` keep their decisions with exactly one usage WARNING (per process and
    endpoint); a `usage` that is null, a list or a string counts as missing; a `prompt_tokens` of 400 digits, of
    4,300 digits, of -1 or of `true` counts as missing (`input_tokens=-`, the WARNING once per process and
    endpoint), and a `completion_tokens` of that kind shows `output_tokens=-`."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND, usage=OMIT)))
    reviewer = reviewer_on(stub.url + "/v1")
    for _ in range(2):
        assert_kept(call(reviewer))
    assert usage_warnings(caplog) == [
        f"repair model usage missing, a cut prompt cannot be detected endpoint={model.endpoint_of(stub.url)}"
    ]
    for usage in (None, [], [1, 2], "50000"):
        caplog.clear()
        stub = model_stub(Reply(body=chat_answer(EXTEND, usage=usage)))
        assert_kept(call(reviewer_on(stub.url + "/v1")))
        line = call_lines(caplog)[-1]
        assert (line["input_tokens"], line["output_tokens"]) == ("-", "-")
        assert len(usage_warnings(caplog)) == 1
    bad_counts: list[Any] = [int("9" * 400), int("9" * 4300), -1, True]
    stub = model_stub([Reply(raw=_chat_with_counts(count, count)) for count in bad_counts])
    reviewer = reviewer_on(stub.url + "/v1")
    caplog.clear()
    for _ in bad_counts:
        assert_kept(call(reviewer))
        line = call_lines(caplog)[-1]
        assert (line["input_tokens"], line["output_tokens"]) == ("-", "-")
    assert len(usage_warnings(caplog)) == 1  # one endpoint, one process


def _chat_with_counts(prompt: Any, completion: Any) -> bytes:
    """A valid answer whose counts are written as JSON literals, so that a 4,300-digit integer arrives as such."""
    body = json.dumps(chat_answer(EXTEND, usage={"prompt_tokens": "P", "completion_tokens": "C"}))
    return body.replace('"P"', json.dumps(prompt)).replace('"C"', json.dumps(completion)).encode()


REFUSAL_TEXT = "I cannot help with that."


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_are_checked_in_order(model_stub: Any, api_format: str) -> None:
    """AG44 and AG61 (C-1, Answers, checked in their order): a refusal comes before a stop at the token limit, and that
    stop before the prompt-token check. An `openai_chat` refusal whose `finish_reason` is `length` is `model_error`,
    `refusal`; an answer that stopped at the limit (`length`; `stop_reason` `max_tokens`) with a prompt count under
    half of `prompt_est` is `invalid_output`, `max_tokens`, not `context_truncated`; and a refusal with such a count
    is `model_error`, `refusal`."""
    cut = 10  # a prompt count far under half of prompt_est
    cases: list[tuple[dict[str, Any], tuple[str, str]]]
    if api_format == OPENAI_CHAT:
        counts = {"prompt_tokens": cut, "completion_tokens": 9}
        cases = [
            (chat_answer(None, refusal=REFUSAL_TEXT, finish_reason="length"), ("model_error", "refusal")),
            (chat_answer(EXTEND, finish_reason="length", usage=counts), ("invalid_output", "max_tokens")),
            (chat_answer(None, refusal=REFUSAL_TEXT, usage=counts), ("model_error", "refusal")),
        ]
    else:
        counts = {"input_tokens": cut, "output_tokens": 9}
        cases = [
            (
                messages_answer([text_block(EXTEND)], stop_reason="max_tokens", usage=counts),
                ("invalid_output", "max_tokens"),
            ),
            (messages_answer([text_block(EXTEND)], stop_reason="refusal", usage=counts), ("model_error", "refusal")),
        ]
    for answer, expected in cases:
        stub = model_stub(Reply(body=answer))
        result = call(reviewer_on(base_url_of(stub, api_format), api_format))
        assert (result.fallback, result.cause) == expected, answer
        assert len(stub.requests) == 1


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_an_unnamed_exception_is_a_model_error(
    model_stub: Any, api_format: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG44 and AG61 (§9.7): an exception from the model client that the rules do not name is `model_error` with the
    exception's class name, and is not retried although a second attempt would fit: raised by the request itself, or
    while the body is read."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=answer_for(api_format)))
    reviewer = reviewer_on(base_url_of(stub, api_format), api_format, max_attempts=2)
    sent: list[str] = []

    def raising_post(url: str, **kwargs: Any) -> requests.Response:
        sent.append(url)
        raise ValueError("an exception no rule names")

    monkeypatch.setattr(reviewer.client.session, "post", raising_post)
    result = call(reviewer)
    assert (result.fallback, result.cause, result.attempts) == ("model_error", "error_ValueError", 1)
    assert len(sent) == 1 and stub.requests == []

    def raising_read(*args: Any, **kwargs: Any) -> bytes:
        raise LookupError("an exception no rule names")

    monkeypatch.setattr(model, "read_body", raising_read)
    result = call(reviewer_on(base_url_of(stub, api_format), api_format, max_attempts=2))
    assert (result.fallback, result.cause, result.attempts) == ("model_error", "error_LookupError", 1)
    assert len(stub.requests) == 1
    assert [line["outcome"] for line in call_lines(caplog)] == ["error_ValueError", "error_LookupError"]


# --- the read cases, in both formats (AG44, AG61) -------------------------------------------------------------------


def _timed(reviewer: Reviewer) -> tuple[ModelResult, float]:
    began = time.monotonic()
    result = call(reviewer)
    return result, time.monotonic() - began


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_read_cases(model_stub: Any, api_format: str) -> None:
    """AG44 (and AG61 for anthropic_messages): with a 1 s attempt limit and one attempt, a body sent one byte per
    second after the headers, and a byte just before the limit followed by nothing, are `model_timeout` within 1 s of
    the limit (here within 0.5 s); so are a gzip body whose header holds a file name (with a Content-Length, or ending
    with the connection) and a chunked body whose chunk-size line holds an extension, each sent one byte every 0.2 s,
    which a read inside urllib3 or http.client would otherwise read on for as long as the bytes keep coming, each
    socket read within the time left. A valid answer with Content-Length, chunked, or gzip-compressed although the
    request asked for identity, parses with no exception."""
    body = json.dumps(answer_for(api_format)).encode()
    trickles = [("trickle", 1.0), ("late", 0.9), ("trickle-gzip", 0.2), ("trickle-gzip-close", 0.2)]
    for mode, interval in [*trickles, ("trickle-chunked", 0.2)]:
        stub = model_stub(Reply(raw=body, mode=mode, interval=interval))
        result, elapsed = _timed(
            reviewer_on(base_url_of(stub, api_format), api_format, timeout_seconds=1, max_attempts=1)
        )
        assert (result.fallback, result.cause) == ("model_timeout", "timeout"), mode
        # Within 1 s of the limit, and well within it: without the per-read socket timeout, the read after the late
        # byte would run a whole read timeout past it, to about 1.9 s (Appendix C-1, Client).
        assert 0.95 <= elapsed < 1.5, (mode, elapsed)
    for mode in ("length", "chunked", "gzip"):
        stub = model_stub(Reply(raw=body, mode=mode))
        result = call(reviewer_on(base_url_of(stub, api_format), api_format))
        assert_kept(result, decision_text())


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_read_cases_without_a_watchdog_thread(
    model_stub: Any, api_format: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG44 (and AG61): when no thread can be started for the body's watchdog, the read keeps C-1's own rules: a valid
    answer is used, and a body sent one byte per second still ends at the attempt's limit as `model_timeout`."""

    def no_thread(self: threading.Timer) -> None:
        raise RuntimeError("can't start new thread")

    body = json.dumps(answer_for(api_format)).encode()
    stubs = [model_stub(Reply(raw=body)), model_stub(Reply(raw=body, mode="trickle", interval=1.0))]
    monkeypatch.setattr(threading.Timer, "start", no_thread)
    assert_kept(call(reviewer_on(base_url_of(stubs[0], api_format), api_format)), decision_text())
    result, elapsed = _timed(
        reviewer_on(base_url_of(stubs[1], api_format), api_format, timeout_seconds=1, max_attempts=1)
    )
    assert (result.fallback, result.cause) == ("model_timeout", "timeout") and 0.95 <= elapsed < 1.5, elapsed


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_a_body_over_one_mebibyte(
    model_stub: Any, api_format: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG44 (and AG61): a 2 MiB body is `model_error`, `bad_response`, with at most 1 MiB and one byte read; so is a
    valid answer followed by blanks to 2 MiB, whose first 1 MiB and one byte would parse, and one padded to exactly
    1 MiB and one byte, while the same answer padded to exactly 1 MiB (1,048,576 bytes) is used."""
    read: list[int] = []
    real_read1 = urllib3.response.HTTPResponse.read1

    def counting_read1(
        self: urllib3.response.HTTPResponse, amt: int | None = None, decode_content: bool | None = None
    ) -> bytes:
        data = real_read1(self, amt, decode_content)
        read.append(len(data))
        return data

    monkeypatch.setattr(urllib3.response.HTTPResponse, "read1", counting_read1)
    answer = json.dumps(answer_for(api_format)).encode()
    for body, used in (
        (b"[" + b"0," * (1024 * 1024) + b"0]", False),
        (answer.ljust(2 * 1024 * 1024), False),
        (answer.ljust(1024 * 1024 + 1), False),
        (answer.ljust(1024 * 1024), True),
    ):
        read.clear()
        stub = model_stub(Reply(raw=body))
        result = call(reviewer_on(base_url_of(stub, api_format), api_format))
        if used:
            assert_kept(result, decision_text())
        else:
            assert (result.fallback, result.cause) == ("model_error", "bad_response"), len(body)
        assert sum(read) <= 1024 * 1024 + 1


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_a_body_nested_200000_deep(model_stub: Any, api_format: str) -> None:
    """AG44 (and AG61): a body nested 200,000 deep is `model_error`, `bad_response`; no exception escapes."""
    stub = model_stub(Reply(raw=b"[" * 200_000 + b"]" * 200_000))
    result = call(reviewer_on(base_url_of(stub, api_format), api_format))
    assert (result.fallback, result.cause) == ("model_error", "bad_response")


@pytest.mark.parametrize("api_format", FORMATS)
def test_openai_compatible_answers_a_complete_body_releases_its_connection(
    model_stub: Any, api_format: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG44 (and AG61): a valid answer sent with Content-Length is read to its end with no exception. urllib3 releases
    the connection, and `connection` becomes None, as the last byte is read: the read loop must not touch its socket
    then. Each read sees the socket's timeout set to the time left while the connection is held."""
    timeouts: list[float] = []
    released: list[bool] = []
    real_read1 = urllib3.response.HTTPResponse.read1

    def watching_read1(
        self: urllib3.response.HTTPResponse, amt: int | None = None, decode_content: bool | None = None
    ) -> bytes:
        connection = self.connection
        released.append(connection is None)
        timeout = connection.sock.gettimeout() if connection is not None and connection.sock is not None else None
        if timeout is not None:
            timeouts.append(timeout)
        return real_read1(self, amt, decode_content)

    monkeypatch.setattr(urllib3.response.HTTPResponse, "read1", watching_read1)
    stub = model_stub(Reply(body=answer_for(api_format)))
    result = call(reviewer_on(base_url_of(stub, api_format), api_format, timeout_seconds=30))
    assert_kept(result, decision_text())
    assert released[-1] is True  # the last read found the connection released
    assert timeouts and all(0 < value <= 30 for value in timeouts)


# --- AG32-AG36: refusal, retries, timeouts ---------------------------------------------------------------------------


def test_refusal_is_a_model_error(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG32: a `message.refusal` that is a non-empty string, with `content` null, is `model_error`, `refusal`, with
    `stop` from `finish_reason`; a refusal of null or "" beside a valid content keeps the decision."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(None, refusal="I can't help with that request.", finish_reason="stop")))
    result = call(reviewer_on(stub.url + "/v1"))
    assert (result.fallback, result.cause, result.raw_output) == ("model_error", "refusal", None)
    line = call_lines(caplog)[-1]
    assert (line["outcome"], line["stop"]) == ("refusal", "stop")
    assert len(stub.requests) == 1  # not retried
    for refusal in (None, ""):
        stub = model_stub(Reply(body=chat_answer(EXTEND, refusal=refusal)))
        assert_kept(call(reviewer_on(stub.url + "/v1")), EXTEND)


@pytest.mark.parametrize("api_format", FORMATS)
def test_server_error_is_retried_once_when_time_allows(model_stub: Any, api_format: str) -> None:
    """AG33: a 500 is retried once when at least 10 s remain before the model deadline, and the second answer is
    used; when less than that remains after the first attempt, it is not retried. The pause before the retry stays
    within the time left."""
    pauses: list[float] = []
    stub = model_stub([Reply(500), Reply(body=answer_for(api_format, content=_content(api_format, EXTEND)))])
    result = call(reviewer_on(base_url_of(stub, api_format), api_format, sleep=pauses.append))
    assert_kept(result, EXTEND)
    assert len(stub.requests) == 2 and result.attempts == 2
    assert pauses == [2.0]
    # 11 s left, and the first answer takes 1.5 s: less than 10 s remain, so there is no second attempt.
    stub = model_stub([Reply(500, delay=1.5), Reply(body=answer_for(api_format))])
    result = call(reviewer_on(base_url_of(stub, api_format), api_format), seconds=11)
    assert (result.fallback, result.cause, len(stub.requests)) == ("model_error", "status_500", 1)


def _content(api_format: str, text: str) -> Any:
    return text if api_format == OPENAI_CHAT else [text_block(text)]


@pytest.mark.parametrize("api_format", FORMATS)
def test_bad_request_is_not_retried(model_stub: Any, api_format: str) -> None:
    """AG34: a 400 is `model_error` with its status, after one request."""
    stub = model_stub([Reply(400), Reply(body=answer_for(api_format))])
    result = call(reviewer_on(base_url_of(stub, api_format), api_format))
    assert (result.fallback, result.cause, len(stub.requests)) == ("model_error", "status_400", 1)


@pytest.mark.parametrize("api_format", FORMATS)
def test_timeouts_become_model_timeout(model_stub: Any, api_format: str, caplog: pytest.LogCaptureFixture) -> None:
    """AG35: an endpoint that never answers, with a 1 s attempt limit, is retried once and then `model_timeout`; each
    attempt is logged with outcome `timeout`."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(mode="silent"))
    began = time.monotonic()
    result = call(reviewer_on(base_url_of(stub, api_format), api_format, timeout_seconds=1, max_attempts=2))
    assert (result.fallback, result.cause, result.attempts, result.model) == ("model_timeout", "timeout", 2, MODEL_NAME)
    assert len(stub.requests) == 2 and time.monotonic() - began < 4
    assert [line["outcome"] for line in call_lines(caplog)] == ["timeout", "timeout"]


@pytest.mark.parametrize("api_format", FORMATS)
def test_no_call_when_too_little_time_is_left(
    model_stub: Any, api_format: str, caplog: pytest.LogCaptureFixture
) -> None:
    """AG36: with less than 10 s left before the model deadline, no attempt starts: no request, no `repair model
    call` line, and the review is `model_timeout`, with no model."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub()
    result = call(reviewer_on(base_url_of(stub, api_format), api_format), seconds=9)
    assert (result.fallback, result.cause, result.attempts, result.model) == (
        "model_timeout",
        "no time left for a model call",
        0,
        None,
    )
    assert stub.requests == [] and call_lines(caplog) == []


# --- AG52: per-attempt limits ---------------------------------------------------------------------------------------


class _NeverAnswers:
    """A transport that never answers: each request waits out its read timeout on the fake clock, then times out."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.attempts: list[tuple[float, tuple[float, float]]] = []

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        connect, read = kwargs["timeout"]
        self.attempts.append((self.clock.monotonic(), (connect, read)))
        self.clock.advance(read)
        raise requests.exceptions.ReadTimeout("no answer")


@pytest.mark.parametrize(
    ("left", "timeout_seconds", "expected"),
    [(425, 60, [60.0, 60.0]), (63, 60, [60.0]), (125, 180, [125.0]), (9, 60, [])],
)
@pytest.mark.parametrize("api_format", FORMATS)
def test_timeout_seconds_is_per_attempt(
    api_format: str, left: float, timeout_seconds: int, expected: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG52: on a fake monotonic clock, against endpoints that never answer: with 425 s left and `timeout_seconds`
    60, two attempts of 60 s, then `model_timeout`; with 63 s left, one attempt of 60 s and no second; with 125 s left
    and `timeout_seconds` 180, one attempt of 125 s; no attempt starts after the model deadline. Connect is 5 s."""
    clock = FakeClock()
    reviewer = reviewer_on(
        "http://127.0.0.1:9/v1" if api_format == OPENAI_CHAT else "http://127.0.0.1:9",
        api_format,
        timeout_seconds=timeout_seconds,
        max_attempts=2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    transport = _NeverAnswers(clock)
    monkeypatch.setattr(reviewer.client.session, "post", transport.post)
    deadline = clock.monotonic() + left
    result = reviewer.call(DIGEST, run=RUN, deadline=deadline)
    assert (result.fallback, result.attempts) == ("model_timeout", len(expected))
    assert [read for _, (_, read) in transport.attempts] == expected
    assert all(connect == 5.0 for _, (connect, _) in transport.attempts)
    assert all(started < deadline for started, _ in transport.attempts)
    assert clock.sleeps == []  # no pause after a timeout


# --- AG48: what is posted and logged from an answer ------------------------------------------------------------------

MODEL_100 = "".join(chr(0x21 + index % 94) for index in range(100))


@pytest.mark.parametrize("api_format", FORMATS)
def test_posted_and_logged_model_request_id_and_stop_are_filtered(
    model_stub: Any, api_format: str, caplog: pytest.LogCaptureFixture
) -> None:
    """AG48: a 100-character printable response `model` is posted and logged; one of 101 or 150 characters, with a
    space, an ESC, a trailing newline or not a string gives the configured `model` in both places. A request id with a
    space, a newline or 129 characters, and a stop reason with a capital letter or a newline, are logged as `-` and
    `other`; no header logs `request_id=-`; a stop reason that is a number, a list or an object is `other`, null or
    missing is `-`, and each keeps a valid answer's decision. With both `request-id` and `x-request-id`,
    anthropic_messages logs the first and openai_chat the second."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    id_header = "x-request-id" if api_format == OPENAI_CHAT else "request-id"
    stop_key = "finish_reason" if api_format == OPENAI_CHAT else "stop_reason"
    plain_stop = "stop" if api_format == OPENAI_CHAT else "end_turn"
    cases: list[tuple[dict[str, Any], dict[str, str], tuple[str, str, str]]] = [
        ({"model": MODEL_100}, {id_header: "req_0123:ab.c-d"}, (MODEL_100, "req_0123:ab.c-d", plain_stop)),
        ({"model": "m" * 101}, {}, ("configured", "-", plain_stop)),
        ({"model": "m" * 150}, {id_header: "has space"}, ("configured", "-", plain_stop)),
        ({"model": "test model"}, {id_header: "r" * 129}, ("configured", "-", plain_stop)),
        ({"model": "test\x1bmodel"}, {id_header: "r" * 128}, ("configured", "r" * 128, plain_stop)),
        ({"model": "test-model\n"}, {}, ("configured", "-", plain_stop)),
        ({"model": 7}, {}, ("configured", "-", plain_stop)),
        ({"model": OMIT}, {}, ("configured", "-", plain_stop)),
        ({stop_key: "Stop"}, {}, (MODEL_NAME, "-", "other")),
        ({stop_key: "stop\n"}, {}, (MODEL_NAME, "-", "other")),
        ({stop_key: 3}, {}, (MODEL_NAME, "-", "other")),
        ({stop_key: ["stop"]}, {}, (MODEL_NAME, "-", "other")),
        ({stop_key: {"type": "stop"}}, {}, (MODEL_NAME, "-", "other")),
        ({stop_key: None}, {}, (MODEL_NAME, "-", "-")),
        ({stop_key: OMIT}, {}, (MODEL_NAME, "-", "-")),
    ]
    for fields, headers, (posted, request_id, stop) in cases:
        caplog.clear()
        content = _content(api_format, EXTEND)
        stub = model_stub(Reply(body=answer_for(api_format, content=content, **fields), headers=headers))
        result = call(reviewer_on(base_url_of(stub, api_format), api_format, model="configured"))
        assert_kept(result, EXTEND)
        assert result.model == posted, fields
        line = call_lines(caplog)[-1]
        assert (line["model"], line["request_id"], line["stop"]) == (posted, request_id, stop), fields
    if api_format == ANTHROPIC_MESSAGES:  # without request-id, the x-request-id header is read
        stub = model_stub(Reply(body=messages_answer(), headers={"x-request-id": "req-x"}))
        call(reviewer_on(stub.url, api_format))
        assert call_lines(caplog)[-1]["request_id"] == "req-x"
    # With both headers, anthropic_messages logs request-id and openai_chat x-request-id (§9.11).
    stub = model_stub(Reply(body=answer_for(api_format), headers={"request-id": "req-a", "x-request-id": "req-b"}))
    call(reviewer_on(base_url_of(stub, api_format), api_format))
    assert call_lines(caplog)[-1]["request_id"] == ("req-a" if api_format == ANTHROPIC_MESSAGES else "req-b")
    # A request id holding a line break is logged as `-`, a trailing one included, which `re.match` with `$` would
    # accept. The pinned HTTP stack joins a folded header line with a space before the client sees it, so the filter
    # is checked on the headers as the client receives them.
    for value in ("req-1\n", "req\nid", "\n"):
        assert model.request_id_of({id_header: value}, api_format) == "-", value


# --- AG61: the anthropic_messages answers ----------------------------------------------------------------------------


def test_anthropic_messages_answers(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG61: the `text` of every block of type `text` is joined in order; `thinking`, `redacted_thinking`, blocks of
    any other type and a `text` block whose text is not a string are ignored; then §9.8 decides: a valid answer split
    over two text blocks around a thinking block keeps its decision, a leading think block in the joined text is
    removed, and an answer with no text block is `invalid_output` (`not JSON`)."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    half = len(EXTEND) // 2
    blocks = [
        {"type": "thinking", "thinking": "E1 shows progress", "signature": "sig"},
        text_block(EXTEND[:half]),
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": 42},
        {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
        "a bare string",
        text_block(EXTEND[half:]),
    ]
    stub = model_stub(Reply(body=messages_answer(blocks)))
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
    assert_kept(result, EXTEND)
    assert result.raw_output == EXTEND
    stub = model_stub(Reply(body=messages_answer([text_block("<think>E1 is advancing</think>"), text_block(EXTEND)])))
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
    assert_kept(result, EXTEND)
    stub = model_stub(Reply(body=messages_answer([{"type": "thinking", "thinking": "hmm"}])))
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
    assert result.fallback is None and result.text == "" and result.raw_output is None
    with pytest.raises(InvalidOutputError) as caught:
        validate_answer(result.text or "", {"E1"}, set())
    assert caught.value.cause.startswith("not JSON")


STOP_REASONS: list[tuple[Any, tuple[str | None, str], str]] = [
    ("end_turn", (None, ""), "end_turn"),
    ("stop_sequence", (None, ""), "stop_sequence"),
    ("max_tokens", ("invalid_output", "max_tokens"), "max_tokens"),
    ("refusal", ("model_error", "refusal"), "refusal"),
    ("model_context_window_exceeded", ("model_error", "context_truncated"), "model_context_window_exceeded"),
    ("tool_use", (None, ""), "tool_use"),
    ("pause_turn", (None, ""), "pause_turn"),
    ("a_new_reason", (None, ""), "a_new_reason"),
    (OMIT, (None, ""), "-"),
    (None, (None, ""), "-"),
    (3, (None, ""), "other"),
    (["end_turn"], (None, ""), "other"),
    ({"type": "end_turn"}, (None, ""), "other"),
]


@pytest.mark.parametrize(("stop_reason", "expected", "logged"), STOP_REASONS)
def test_anthropic_messages_answers_stop_reasons(
    model_stub: Any, caplog: pytest.LogCaptureFixture, stop_reason: Any, expected: tuple[str | None, str], logged: str
) -> None:
    """AG61: `end_turn` and `stop_sequence` keep the decision; `max_tokens` is `invalid_output` (`max_tokens`)
    whatever the text; `refusal` is `model_error`, `refusal`; `model_context_window_exceeded` is `model_error`,
    `context_truncated`; `tool_use`, `pause_turn`, an unknown value and none go on to §9.8, and so does a stop reason
    that is null, a number, a list or an object, logged as `stop=-` (null) or `stop=other`."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=messages_answer([text_block(EXTEND)], stop_reason=stop_reason)))
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
    assert (result.fallback, result.cause) == expected
    if expected[0] is None:
        assert_kept(result, EXTEND)
    assert call_lines(caplog)[-1]["stop"] == logged
    assert len(stub.requests) == 1


def test_anthropic_messages_answers_usage(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG61: `input_tokens` plus `cache_creation_input_tokens` and `cache_read_input_tokens`, where those are token
    counts, is the logged `input_tokens` and the count the `context_truncated` check compares: a sum under half of
    `prompt_est` is `context_truncated`, a sum at half or above keeps the decision, also when `input_tokens` alone is
    under half; `output_tokens` is logged; without `input_tokens`, or with one of 400 or 4,300 digits, -1 or `true`,
    the decision is kept with one usage WARNING per endpoint and `input_tokens=-`, and a cache count of that kind is
    not added; a `usage` that is null, a list or a string counts as missing, with both counts `-`."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    half = -(-PROMPT_EST // 2)
    split = {"input_tokens": 10, "cache_creation_input_tokens": half - 110, "cache_read_input_tokens": 100}
    cases: list[tuple[dict[str, Any], tuple[str | None, str], str]] = [
        ({"input_tokens": half - 1, "output_tokens": 7}, ("model_error", "context_truncated"), str(half - 1)),
        ({"input_tokens": half, "output_tokens": 7}, (None, ""), str(half)),
        (split, (None, ""), str(half)),
        ({**split, "cache_read_input_tokens": 99}, ("model_error", "context_truncated"), str(half - 1)),
        ({**split, "cache_read_input_tokens": True}, ("model_error", "context_truncated"), str(half - 100)),
        ({**split, "cache_read_input_tokens": -1}, ("model_error", "context_truncated"), str(half - 100)),
    ]
    for usage, expected, logged in cases:
        stub = model_stub(Reply(body=messages_answer([text_block(EXTEND)], usage=usage)))
        result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
        assert (result.fallback, result.cause) == expected, usage
        line = call_lines(caplog)[-1]
        assert (line["input_tokens"], line["prompt_est"]) == (logged, str(PROMPT_EST)), usage
        assert line["output_tokens"] == ("7" if "output_tokens" in usage else "-")
    assert usage_warnings(caplog) == []
    # Missing counts: one WARNING for this endpoint, and the counts logged as -.
    stub = model_stub(
        [
            Reply(body=messages_answer([text_block(EXTEND)], usage={"output_tokens": 5})),
            Reply(raw=_messages_with_input(int("9" * 400))),
            Reply(raw=_messages_with_input(int("9" * 4300))),
            Reply(raw=_messages_with_input(-1)),
            Reply(raw=_messages_with_input(True)),
        ]
    )
    reviewer = reviewer_on(stub.url, ANTHROPIC_MESSAGES)
    for _ in range(5):
        assert_kept(call(reviewer), EXTEND)
        assert call_lines(caplog)[-1]["input_tokens"] == "-"
    assert len(usage_warnings(caplog)) == 1
    for usage in (None, [], "50000"):
        stub = model_stub(Reply(body=messages_answer([text_block(EXTEND)], usage=usage)))
        assert_kept(call(reviewer_on(stub.url, ANTHROPIC_MESSAGES)), EXTEND)
        line = call_lines(caplog)[-1]
        assert (line["input_tokens"], line["output_tokens"]) == ("-", "-")
    assert len(usage_warnings(caplog)) == 4  # one more for each of the three new endpoints


def _messages_with_input(count: Any) -> bytes:
    usage = {"input_tokens": "I", "cache_read_input_tokens": "I", "output_tokens": 3}
    body = json.dumps(messages_answer([text_block(EXTEND)], usage=usage))
    return body.replace('"I"', json.dumps(count)).encode()


@pytest.mark.parametrize("status", [429, 500, 503, 529, 400, 401, 402, 403, 404, 408, 413])
def test_anthropic_messages_answers_statuses(model_stub: Any, caplog: pytest.LogCaptureFixture, status: int) -> None:
    """AG61: 429, 500, 503 and 529 are retried once when time allows; 400, 401, 402, 403, 404 and 413 are not, nor is
    a 408, as any other 4xx (§9.7), each a `model_error` with `status_<code>` in the log line."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub([Reply(status), Reply(body=messages_answer([text_block(EXTEND)]))])
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
    if status in (429, 500, 503, 529):
        assert_kept(result, EXTEND)
        assert [line["outcome"] for line in call_lines(caplog)] == [f"status_{status}", "ok"]
        assert len(stub.requests) == 2
    else:
        assert (result.fallback, result.cause) == ("model_error", f"status_{status}")
        assert [line["outcome"] for line in call_lines(caplog)] == [f"status_{status}"]
        assert len(stub.requests) == 1


def test_anthropic_messages_answers_bad_bodies(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG61: a body that is not JSON, not an object, or whose `content` is not a list is `model_error`,
    `bad_response`; the log line shows the response's `request-id` and the filtered `model` of a valid answer."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    for raw in (
        b"not json",
        b"[1, 2]",
        json.dumps(messages_answer("a string")).encode(),
        json.dumps({"x": 1}).encode(),
    ):
        stub = model_stub(Reply(raw=raw))
        result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES))
        assert (result.fallback, result.cause, len(stub.requests)) == ("model_error", "bad_response", 1), raw
    stub = model_stub(Reply(body=messages_answer(model=MODEL_100), headers={"request-id": "req_011CabcDEF"}))
    result = call(reviewer_on(stub.url, ANTHROPIC_MESSAGES, model="configured"))
    line = call_lines(caplog)[-1]
    assert (result.model, line["model"], line["request_id"], line["stop"]) == (
        MODEL_100,
        MODEL_100,
        "req_011CabcDEF",
        "end_turn",
    )
    assert line["output_tokens"] == str(MESSAGES_USAGE["output_tokens"])


def test_openai_compatible_answers_log_the_counts(model_stub: Any, caplog: pytest.LogCaptureFixture) -> None:
    """AG44: the line shows `usage.prompt_tokens` and `usage.completion_tokens` as `input_tokens` and
    `output_tokens`, the attempt number, the reviewer's name and the time taken."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stub = model_stub([Reply(500), Reply(body=chat_answer(EXTEND))])
    call(reviewer_on(stub.url + "/v1", name="reviewer_local"))
    first, second = call_lines(caplog)
    assert (first["attempt"], first["outcome"], first["input_tokens"], first["output_tokens"]) == (
        "1",
        "status_500",
        "-",
        "-",
    )
    assert (second["attempt"], second["outcome"]) == ("2", "ok")
    assert (second["input_tokens"], second["output_tokens"]) == (
        str(USAGE["prompt_tokens"]),
        str(USAGE["completion_tokens"]),
    )
    assert second["reviewer"] == "reviewer_local" and second["run"] == RUN and int(second["ms"]) >= 0
    assert set(second) == {
        "run",
        "reviewer",
        "model",
        "attempt",
        "outcome",
        "stop",
        "input_tokens",
        "prompt_est",
        "output_tokens",
        "ms",
        "request_id",
    }
