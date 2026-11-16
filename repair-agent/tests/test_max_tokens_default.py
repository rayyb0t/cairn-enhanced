"""The reviewer's default max_tokens (owner decision O10; build step R3, B1-B6): AG87-AG89.

A reviewer whose settings leave `max_tokens` out (null or absent) gets 16384 without a `context_window` (B1). With
one (B2), the default is `min(16384, max(4096, context_window - system_est - K * digest.max_tokens_estimate))`, K = 4,
the most UTF-8 bytes a digest holds per estimated token, so the room the context leaves for the digest never cuts a
digest the default's 4096 would not cut; the room also stays at least MIN_DIGEST_TOKENS (build step R3's deviation,
for a budget under 1,000). An explicit `max_tokens` is used as written (B3). One value per reviewer, resolved where the
settings file and repair.yaml's digest budget are both known, reaches the request of both formats, the digest budget
and the replay's plan (B4). A 400 or 422 for a request whose max_tokens came from the default logs one WARNING per
reviewer and process (B5).

Every value is synthetic; the model and the server are loopback stubs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from agentkit import (
    EXTEND,
    ModelStub,
    Reply,
    cairn_stubs,
    chat_answer,
    configure,
    messages_answer,
    model_stubs,
    run_view,
    start_agent,
    text_block,
    write_run_copy,
)
from conftest import REVIEWER_KEY, SECOND_REVIEWER_KEY, Layout, reviewer_entry, settings_data
from records import NONCE, T, context
from test_pin import write_eval_set

from cairn_repair_agent import config, digest, model, replay
from cairn_repair_agent.config import ANTHROPIC_MESSAGES, OPENAI_CHAT, StartupError, parse_settings
from cairn_repair_agent.digest import (
    DEFAULT_MAX_TOKENS,
    MIN_DIGEST_TOKENS,
    SMALL_CONTEXT_MAX_TOKENS,
    SYSTEM_PROMPT_TOKENS,
    Fact,
    build_digest,
    digest_budget,
    token_bound,
)
from cairn_repair_agent.model import Reviewer, build_model_client
from cairn_repair_agent.redact import NO_SECRETS
from cairn_repair_agent.transcripts import OK, SHELL, Message, ToolCall, Transcript

K = 4  # B2: ASCII is 3 bytes per estimated token, any other character 1 token of at most 4 bytes
CONTEXT_WINDOWS = [8192, 12288, 16384, 32768, 65536]
BUDGETS = [8000, 4000]  # the default digest budget and one other
WARNING = "repair model refused a request with the default max_tokens, set max_tokens if the model allows fewer"


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture
def cairn_stub() -> Iterator[Any]:
    yield from cairn_stubs()


@pytest.fixture(autouse=True)
def _fresh_once_per_process_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "_usage_warned", set())
    monkeypatch.setattr(model, "_default_max_tokens_warned", set())


def resolved(context_window: int | None, budget: int, max_tokens: int | None = None) -> config.ReviewerSettings:
    """The first reviewer of a settings file, as the agent reads it with repair.yaml's digest budget `budget`."""
    entry = reviewer_entry(context_window=context_window, max_tokens=max_tokens)
    return parse_settings(settings_data(reviewers=[entry]), "agent-settings.json", digest_tokens=budget).reviewers[0]


def thresholds(budget: int) -> tuple[int, int]:
    """B2's two thresholds for a budget: the last context_window whose default is 4096, the first whose is 16384."""
    kept = max(K * budget, MIN_DIGEST_TOKENS)
    return SYSTEM_PROMPT_TOKENS + kept + SMALL_CONTEXT_MAX_TOKENS, SYSTEM_PROMPT_TOKENS + kept + DEFAULT_MAX_TOKENS


def room_refusal(context_window: int, budget: int, max_tokens: int | None) -> str | None:
    """The room check's refusal of a reviewer (Appendix C-1, Digest budget), or None when it starts."""
    try:
        resolved(context_window, budget, max_tokens)
    except StartupError as exc:
        assert "context_window" in str(exc), exc
        return str(exc)
    return None


# --- AG87 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", BUDGETS)
def test_the_default_max_tokens(budget: int) -> None:
    """AG87: 16384 without context_window; with it, the values at 8,192, 12,288, 16,384, 32,768, the two thresholds
    and 65,536, for the default budget and one other; an explicit value is kept in every case; and the room check
    gives the same refusals as at 4096."""
    assert DEFAULT_MAX_TOKENS == 16384 and SMALL_CONTEXT_MAX_TOKENS == 4096 and config.DEFAULT_MAX_TOKENS == 16384
    without = resolved(None, budget)
    assert (without.max_tokens, without.max_tokens_is_default) == (16384, True)
    # Absent and null are the same: the dispatcher leaves the key out when dispatch.yaml does (housekeeping.py).
    absent = {key: value for key, value in reviewer_entry().items() if key != "max_tokens"}
    parsed = parse_settings(settings_data(reviewers=[absent]), "f", digest_tokens=budget).reviewers[0]
    assert (parsed.max_tokens, parsed.max_tokens_is_default) == (16384, True)

    low, high = thresholds(budget)
    if budget == 8000:  # B2's worked numbers: 4096 up to about 37,300, and 16384 from about 49,600
        assert (low, high) == (37334, 49622)
    expected = {
        8192: None,  # refused, as at 4096: the room is under 4,000
        12288: 4096,
        16384: 4096,
        32768: 4096 if budget == 8000 else 32768 - SYSTEM_PROMPT_TOKENS - K * budget,
        low: 4096,
        low + 1: 4097,
        high - 1: 16383,
        high: 16384,
        65536: 16384,
    }
    for context_window, value in expected.items():
        if value is None:
            assert room_refusal(context_window, budget, None) is not None
            continue
        reviewer = resolved(context_window, budget)
        assert (reviewer.max_tokens, reviewer.max_tokens_is_default) == (value, True), context_window
        # The default never takes the room under K x budget, so it never cuts a digest (AG88).
        if value > 4096:
            assert digest.context_window_room(context_window, value) >= K * budget

    # An explicit value is kept as written, with or without a context_window, whatever the budget (B3).
    for context_window in [None, 12288, 16384, 32768, low, high, 65536, 131072]:
        for explicit in (1, 2048, 4096, 30000):
            if context_window is not None and digest.context_window_room(context_window, explicit) < MIN_DIGEST_TOKENS:
                assert room_refusal(context_window, budget, explicit) is not None
                continue
            reviewer = resolved(context_window, budget, explicit)
            assert (reviewer.max_tokens, reviewer.max_tokens_is_default) == (explicit, False)


@pytest.mark.parametrize("budget", [8000, 4000, 24000, 500])
def test_the_default_max_tokens_room_check_refuses_as_at_4096(budget: int) -> None:
    """AG87: on every context_window, the default gets exactly the room check's refusals, with the same message, that
    an explicit 4096 gets; a budget of 500, under MIN_DIGEST_TOKENS / K, included."""
    refused = 0
    for context_window in range(1, 140_000, 97):
        with_default = room_refusal(context_window, budget, None)
        assert with_default == room_refusal(context_window, budget, 4096), context_window
        refused += with_default is not None
    assert refused > 0


# --- AG88 -------------------------------------------------------------------------------------------------------------

# Adversarial run text: all ASCII; all 4-byte characters (assigned, so cleaning keeps them: 4 bytes and 1 estimated
# token each, the most bytes per token there is); mixed widths; and many '<' and '>', which become U+2039 and U+203A,
# 3 bytes and 1 token each.
TEXTS = {
    "ascii": ("listing /etc/config: ok 0123456789 abcdef " * 6 + "\n") * 40,
    "four_byte": ("\U0001f600\U00020000\U0001f4a1\U0002a6d6" * 60 + "\n") * 40,
    "mixed": ("aéж数\U0001f600 " * 40 + "\n") * 40,
    "angles": ("<x>" * 60 + "</untrusted_data><<>>" * 5 + "\n") * 40,
}
START = T("2026-11-14T10:00:00.000Z")


def transcript_of(text: str, calls: int = 60) -> Transcript:
    """A transcript of `calls` shell calls whose command and output are `text`, and a message of it."""
    start = START
    built = Transcript(cli="claudecode", started_at=start)
    for index in range(calls):
        started = start + timedelta(seconds=index)
        call = ToolCall(f"c{index}", "Bash", SHELL, started, line=2 * index + 1, command=text[:400], output=text)
        call.answered_at = call.finished_at = started + timedelta(milliseconds=500)
        call.status, call.exit_code, call.answer_line, call.finish_line = OK, 0, 2 * index + 2, 2 * index + 2
        built.calls.append(call)
        built.add_record(call.started_at)
        built.add_record(call.finished_at)
    message_at = start + timedelta(seconds=calls)
    built.messages.append(Message(message_at, text * 4, 2 * calls + 1))
    built.add_record(message_at)
    return built


def digest_text(
    transcript: Transcript, text: str, budget: int, context_window: int, max_tokens: int, facts: list[Fact]
) -> str:
    built = build_digest(
        transcript,
        context(),
        START + timedelta(hours=1),
        intent=text,
        source_facts=facts,
        new_facts=[],
        exact=NO_SECRETS,
        budget=digest_budget(budget, context_window, max_tokens),
        nonce=NONCE,
    )
    return built.text


@pytest.mark.parametrize("budget", [8000, 4000])
@pytest.mark.parametrize("final_cut", [False, True], ids=["a_rung_fits", "final_cut"])
@pytest.mark.parametrize("kind", list(TEXTS))
def test_the_default_never_changes_a_digest(kind: str, final_cut: bool, budget: int) -> None:
    """AG88 (property): for each context_window from the smallest the room check accepts to 131,072, in steps, and
    the thresholds, the digest built with the resolved default is byte for byte the digest built with 4096, for each
    adversarial text, whether a rung of the size ladder fits or the final cut fills the digest up to its budget (every
    fact is listed, its length only limited, so 200 facts leave no rung within the budget). The texts are adversarial:
    without a room, the final cut's digest of 4-byte characters holds more than 3 x its budget in bytes, so the room of
    O10's draft formula (K = 1) would cut it."""
    text = TEXTS[kind]
    transcript = transcript_of(text)
    facts = [Fact(f"f{index:03d}", text) for index in range(200)] if final_cut else []
    smallest = next(cw for cw in range(1, 20_000) if room_refusal(cw, budget, None) is None)
    low, high = thresholds(budget)
    windows = sorted({*range(smallest, 131_073, 4096), smallest, low, low + 1, high - 1, high, 131_072})
    unroomed = digest_text(transcript, text, budget, 10**9, 4096, facts)  # a room no digest reaches: the budget alone
    assert (digest.CUT_NOTE in unroomed) == final_cut
    if kind == "four_byte" and final_cut:
        assert token_bound(unroomed) > 3 * budget
    compared = 0
    for context_window in windows:
        default = resolved(context_window, budget).max_tokens
        if default == 4096:
            continue  # the same max_tokens: the same digest by construction
        today = digest_text(transcript, text, budget, context_window, 4096, facts)
        assert digest_text(transcript, text, budget, context_window, default, facts) == today, context_window
        assert today == unroomed  # a room of at least K x budget never binds: the budget alone sizes the digest
        compared += 1
    assert compared >= 10


def test_k_bounds_the_bytes_of_every_digest_text() -> None:
    """AG88's lemma: no text holds more than K bytes (`token_bound`) per token of §9.6's estimate, and 4-byte text
    reaches it; so a digest within its budget is at most K x budget bytes."""
    for text in [*TEXTS.values(), "", "a", "ab", "abc", "é", "\ud800", "\U0010fffd" * 7, "x\ty\n" * 9]:
        assert token_bound(text) <= K * digest.estimate_tokens(text), text[:20]
    assert token_bound("\U0001f600" * 100) == K * digest.estimate_tokens("\U0001f600" * 100)
    assert digest.DIGEST_BYTES_PER_TOKEN == K


# --- AG89 -------------------------------------------------------------------------------------------------------------


def reviewer_on(stub: ModelStub, api_format: str, **settings: Any) -> Reviewer:
    entry = reviewer_entry(
        base_url=stub.url + ("/v1" if api_format == OPENAI_CHAT else ""), api_format=api_format, **settings
    )
    if api_format == ANTHROPIC_MESSAGES and entry["api_key"] is None:
        entry["api_key"] = REVIEWER_KEY
    parsed = parse_settings(settings_data(reviewers=[entry]), "agent-settings.json").reviewers[0]
    return Reviewer(build_model_client(parsed), sleep=lambda seconds: None)


def answer(api_format: str) -> dict[str, Any]:
    return chat_answer(EXTEND) if api_format == OPENAI_CHAT else messages_answer([text_block(EXTEND)])


@pytest.mark.parametrize("api_format", [OPENAI_CHAT, ANTHROPIC_MESSAGES])
def test_the_request_carries_the_resolved_max_tokens(
    api_format: str, model_stub: Callable[..., ModelStub], caplog: pytest.LogCaptureFixture
) -> None:
    """AG89: in both formats the request's max_tokens is the resolved value: the default (16384 without a
    context_window, B2's value with one) or the explicit one; and no B5 WARNING for an answered request."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=answer(api_format)))
    cases: list[tuple[dict[str, Any], int]] = [
        ({}, 16384),
        ({"context_window": 40000}, 40000 - SYSTEM_PROMPT_TOKENS - K * 8000),
        ({"context_window": 16384}, 4096),
        ({"context_window": 65536}, 16384),
        ({"max_tokens": 3000}, 3000),
        ({"max_tokens": 30000}, 30000),
        ({"max_tokens": 2048, "context_window": 65536}, 2048),
    ]
    for settings, expected in cases:
        reviewer = reviewer_on(stub, api_format, **settings)
        result = reviewer.call("a digest", run="r1", deadline=time.monotonic() + 600)
        assert result.fallback is None, result
        assert json.loads(stub.requests[-1].body)["max_tokens"] == expected, settings
        reviewer.client.session.close()
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith(WARNING)] == []


def test_the_request_carries_the_resolved_max_tokens_live_and_in_the_replay(
    layout: Layout, cairn_stub: Any, model_stub: Callable[..., ModelStub], capsys: pytest.CaptureFixture[str]
) -> None:
    """AG89, B4: a live review sends the resolved default, computed with repair.yaml's digest budget, which also sizes
    the digest budget on the started line; and the replay, reading the same repair.yaml, plans with the same value:
    its `planned` line's max_output_tokens is calls x the resolved max_tokens of each reviewer."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    reviewers = [
        reviewer_entry("reviewer_default", base_url=stub.url + "/v1", context_window=40000),
        reviewer_entry("reviewer_explicit", base_url=stub.url + "/v1", max_tokens=3000, api_key=SECOND_REVIEWER_KEY),
    ]
    for budget, live_default in ((8000, 40000 - SYSTEM_PROMPT_TOKENS - K * 8000), (4000, 16384)):
        layout.write_config(digest={"max_tokens_estimate": budget})
        configure(layout, cairn_stub.url, reviewers=reviewers)
        cairn_stub.runs = [run_view()]
        write_run_copy(layout.root)
        agent = start_agent(layout)
        assert agent.started.model.reviewer.max_tokens == live_default
        assert agent.started.digest_tokens == budget  # the room stays at least 4 x budget: the budget is unchanged
        agent.loop.poll_once()
        assert agent.loop.wait_idle(60)
        assert json.loads(stub.requests[-1].body)["max_tokens"] == live_default

        eval_dir = write_eval_set(layout.base / f"eval-{budget}")
        out = layout.base / f"replay-{budget}"
        code = replay.main([str(eval_dir), "--config", str(layout.config_path), "--out", str(out), "--dry-run"])
        printed = capsys.readouterr().out
        assert code == 0, printed
        planned = [line for line in printed.splitlines() if line.startswith("planned ")]
        assert planned == [
            f"planned reviewer=reviewer_default calls=1 max_input_tokens={_planned_input(out, 'reviewer_default')} "
            f"max_output_tokens={live_default} ceiling=no_price",
            f"planned reviewer=reviewer_explicit calls=1 max_input_tokens={_planned_input(out, 'reviewer_explicit')} "
            "max_output_tokens=3000 ceiling=no_price",
        ]


def _planned_input(out: Path, reviewer: str) -> int:
    (written,) = sorted((out / "digests" / reviewer).iterdir())
    return digest.prompt_tokens_estimate(written.read_text(encoding="utf-8"))


COMPARED = [8000, 24000]  # replay_eval_set.py --digest-tokens 8000,24000, as §9.6 suggests


@pytest.mark.parametrize("repair_yaml_budget", COMPARED)
def test_the_request_carries_the_resolved_max_tokens_at_each_compared_budget(
    repair_yaml_budget: int,
    layout: Layout,
    cairn_stub: Any,
    model_stub: Callable[..., ModelStub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG89, B4 with B2 and §15.3: the replay reviews each budget of `--digest-tokens` as a live agent with that budget
    in its repair.yaml would, whatever repair.yaml's own budget is. A reviewer that leaves max_tokens out, with a
    context_window of 65,536, sends 16384 at 8,000 and 4096 at 24,000 (the default of each budget); one that sets it,
    and one without a context_window, send the same value at both; and the plan line counts each call's value."""
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    url = stub.url + "/v1"
    reviewers = [
        reviewer_entry("reviewer_default", model="default-model", base_url=url, context_window=65536),
        reviewer_entry(
            "reviewer_explicit",
            model="explicit-model",
            base_url=url,
            context_window=65536,
            max_tokens=3000,
            api_key=SECOND_REVIEWER_KEY,
        ),
        reviewer_entry("reviewer_hosted", model="hosted-model", base_url=url, api_key=REVIEWER_KEY),
    ]
    layout.write_config(digest={"max_tokens_estimate": repair_yaml_budget})
    configure(layout, cairn_stub.url, reviewers=reviewers)
    eval_dir = write_eval_set(layout.base / "eval")
    out = layout.base / "replay"
    argv = [str(eval_dir), "--config", str(layout.config_path), "--out", str(out)]
    code = replay.main([*argv, "--digest-tokens", ",".join(map(str, COMPARED)), "--yes"])
    printed = capsys.readouterr().out
    assert code == 0, printed

    sent: dict[str, list[int]] = {}
    for request in stub.requests:
        body = json.loads(request.body)
        sent.setdefault(body["model"], []).append(body["max_tokens"])
    assert sent == {"default-model": [16384, 4096], "explicit-model": [3000, 3000], "hosted-model": [16384, 16384]}
    records = [json.loads(line) for line in (out / replay.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()]
    assert [(record["reviewer"], record["digest_budget"]) for record in records] == [
        (name, budget) for name in ("reviewer_default", "reviewer_explicit", "reviewer_hosted") for budget in COMPARED
    ]
    planned = {
        values["reviewer"]: int(values["max_output_tokens"])
        for values in (dict(re.findall(r"(\w+)=(\S+)", line)) for line in printed.splitlines())
        if "max_output_tokens" in values
    }
    assert planned == {"reviewer_default": 16384 + 4096, "reviewer_explicit": 2 * 3000, "reviewer_hosted": 2 * 16384}


@pytest.mark.parametrize("repair_yaml_budget", COMPARED)
def test_the_request_carries_the_resolved_max_tokens_and_digest_of_each_compared_budget(
    repair_yaml_budget: int,
) -> None:
    """AG89, B4 with B2 and §15.3: for a reviewer that leaves max_tokens out, with a context_window of 65,536, each
    budget of `--digest-tokens` gets the max_tokens, the room and so the digest, byte for byte, of a live agent whose
    repair.yaml has that budget, whatever repair.yaml's own budget: never the room that repair.yaml's default leaves,
    which cuts the ASCII digest of 24,000 tokens shorter than such a live agent does. A set max_tokens is kept at every
    budget."""
    context_window = 65536
    text = TEXTS["ascii"]
    case = replay.Case(
        intent_id="i001",
        run_id="replay-i001",
        transcript_name=replay.REDACTED_NAME,
        transcript_sha256="0" * 64,
        cli="claudecode",
        budget_seconds=1800,
        cut=START + timedelta(hours=1),
        cut_seconds=3600.0,
        transcript=transcript_of(text),
        intent=text,
        source_facts=tuple(Fact(f"f{index:03d}", text) for index in range(200)),  # every rung over 24,000
        new_facts=(),
    )
    reviewer = resolved(context_window, repair_yaml_budget)  # as the replay reads it, with repair.yaml's budget
    budgets = replay.budgets_for(reviewer, COMPARED)
    built = replay.build_digests(
        [case],
        repetitions=1,
        budgets=[budget for budget, _ in budgets],
        exact=NO_SECRETS,
        slice_seconds=600,
        probe=lambda case: False,
        nonce=lambda: NONCE,
    )
    assert [settings.max_tokens for _, settings in budgets] == [16384, 4096]
    for (budget, settings), requested in zip(budgets, COMPARED, strict=True):
        live = resolved(context_window, requested)  # the reviewer of a live agent whose repair.yaml has this budget
        assert (settings.max_tokens, settings.max_tokens_is_default) == (live.max_tokens, True)
        assert model.request_body(settings, "a digest")["max_tokens"] == live.max_tokens
        live_budget = digest_budget(requested, context_window, live.max_tokens)
        assert (int(budget), budget.room) == (int(live_budget), live_budget.room)
        live_digest = build_digest(
            case.transcript,
            case.context(600),
            case.cut,
            intent=case.intent,
            source_facts=case.source_facts,
            new_facts=case.new_facts,
            exact=NO_SECRETS,
            budget=live_budget,
            nonce=NONCE,
        )
        assert built[(case.intent_id, 1, False, budget)].digest.text == live_digest.text, requested
    narrow = budgets[1][0]
    wide_room = digest.context_window_room(context_window, 16384)  # the room 16384, the default at 8,000, leaves
    assert token_bound(built[(case.intent_id, 1, False, narrow)].digest.text) > wide_room  # which would cut it

    explicit = resolved(context_window, repair_yaml_budget, 3000)
    assert [(settings.max_tokens, settings) for _, settings in replay.budgets_for(explicit, COMPARED)] == [
        (3000, explicit),
        (3000, explicit),
    ]
    hosted = resolved(None, repair_yaml_budget)
    assert [settings for _, settings in replay.budgets_for(hosted, COMPARED)] == [hosted, hosted]


@pytest.mark.parametrize("api_format", [OPENAI_CHAT, ANTHROPIC_MESSAGES])
def test_the_request_carries_the_resolved_max_tokens_and_a_refused_default_warns_once(
    api_format: str, model_stub: Callable[..., ModelStub], caplog: pytest.LogCaptureFixture
) -> None:
    """AG89, B5: a 400 or a 422 for a request whose max_tokens came from the default logs the WARNING once per reviewer
    and process, with the reviewer, the max_tokens sent and the status, and never the answer's body; an explicit
    max_tokens, and any other status (401, 404, 429, 500), log none. The review is still a model_error, not retried."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    canary = "body-canary-the-model-allows-at-most-4096-output-tokens"

    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.getMessage().startswith(WARNING)]

    def call(stub: ModelStub, **settings: Any) -> model.ModelResult:
        reviewer = reviewer_on(stub, api_format, **settings)
        try:
            return reviewer.call("a digest", run="r1", deadline=time.monotonic() + 600)
        finally:
            reviewer.client.session.close()

    for status in (401, 404, 429, 500):
        stub = model_stub(Reply(status=status, body={"error": {"message": canary}}))
        call(stub, max_attempts=1)
    for status in (400, 422):
        call(model_stub(Reply(status=status, body={"error": {"message": canary}})), max_tokens=16384)
    assert warnings() == []

    refusing = model_stub(Reply(status=400, body={"error": {"message": canary}}))
    first = call(refusing, max_attempts=2)
    assert (first.fallback, first.cause, first.attempts) == ("model_error", "status_400", 1)
    call(refusing, context_window=65536)  # the same reviewer again, also with the default: no second line
    assert warnings() == [f"{WARNING} reviewer=reviewer_local max_tokens=16384 status=400"]
    other = model_stub(Reply(status=422, body={"error": {"message": canary}}))
    call(other, name="reviewer_other", context_window=40000)
    assert warnings() == [
        f"{WARNING} reviewer=reviewer_local max_tokens=16384 status=400",
        f"{WARNING} reviewer=reviewer_other max_tokens={40000 - SYSTEM_PROMPT_TOKENS - K * 8000} status=422",
    ]
    assert [r.levelname for r in caplog.records if r.getMessage().startswith(WARNING)] == ["WARNING", "WARNING"]
    assert all(canary not in r.getMessage() for r in caplog.records)
