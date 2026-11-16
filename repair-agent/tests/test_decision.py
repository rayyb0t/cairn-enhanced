"""Validation of the model's answer (spec §9.8): AG28, AG29 and AG30.

An answer is one JSON object with exactly the four keys and their types; `extend` and `detach` must cite ids present
in the digest, and `detach` a call running at the cut. Anything else is `invalid_output`, with the cause as the
reason of an automatic continue; over-long text is cut to the server's limits, never refused.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from agentkit import ModelStub, Reply, chat_answer, decision_text, model_stubs
from conftest import reviewer_entry, settings_data

from cairn_repair_agent import decision as decision_module
from cairn_repair_agent.config import parse_settings
from cairn_repair_agent.decision import InvalidOutputError, validate_answer
from cairn_repair_agent.model import INVALID_OUTPUT, Reviewer, build_model_client
from cairn_repair_agent.review import fallback

EVENT_IDS = {"E1", "E2", "E3", "M1"}
RUNNING = {"E3"}


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


def answer(**fields: Any) -> str:
    base: dict[str, Any] = {
        "decision": "continue",
        "reason": "Stuck on the same timeout.",
        "evidence": ["E2 the same request timed out"],
        "next_step": "",
    }
    base.update(fields)
    return json.dumps(base)


# --- AG28 ---------------------------------------------------------------------------------------------------------


def test_valid_answers() -> None:
    """AG28: a continue (with or without evidence), an extend citing an id present in the digest, a detach citing a
    call running at the cut; a citation may sit in brackets after spaces, and a message id counts for extend."""
    plain = validate_answer(answer(), EVENT_IDS, RUNNING)
    assert (plain.decision, plain.reason, plain.evidence, plain.next_step) == (
        "continue",
        "Stuck on the same timeout.",
        ("E2 the same request timed out",),
        "",
    )
    assert plain.cited == ("E2",) and plain.truncated is False
    assert validate_answer(answer(evidence=[], next_step="a few words"), EVENT_IDS, RUNNING).decision == "continue"

    extend = validate_answer(
        answer(decision="extend", evidence=["E3 extraction at 70%"], next_step="list /etc"), EVENT_IDS, RUNNING
    )
    assert (extend.decision, extend.cited, extend.next_step) == ("extend", ("E3",), "list /etc")
    bracketed = validate_answer(
        answer(decision="extend", evidence=["  [E1] first partition found", "M1 the plan"], next_step="go on"),
        EVENT_IDS,
        set(),
    )
    assert bracketed.cited == ("E1", "M1")
    by_message = validate_answer(
        answer(decision="extend", evidence=["M1 the plan"], next_step="go on"), EVENT_IDS, set()
    )
    assert by_message.cited == ("M1",)

    detach = validate_answer(
        answer(decision="detach", evidence=["E1 earlier step", "E3 extraction at 70%"], next_step="finish it"),
        EVENT_IDS,
        RUNNING,
    )
    assert (detach.decision, detach.cited) == ("detach", ("E1", "E3"))


# --- AG29 ---------------------------------------------------------------------------------------------------------

INVALID = {
    "not_json": ("not json", "not JSON: Expecting value"),
    "two_objects": (answer() + answer(), "not JSON: Extra data"),
    "unknown_decision": (answer(decision="stop"), "schema: decision"),
    "an_extra_key": (answer(confidence=0.9), "schema: confidence"),
    "a_missing_key": (json.dumps({"decision": "continue"}), "schema: reason,evidence,next_step"),
    "evidence_not_a_list": (answer(evidence="E1"), "schema: evidence"),
    "evidence_with_a_number": (answer(evidence=["E1 x", 2]), "schema: evidence"),
    "next_step_null": (answer(next_step=None), "schema: next_step"),
    "reason_not_a_string": (answer(reason=["x"]), "schema: reason"),
    "not_an_object": (json.dumps(["continue"]), "schema: not an object"),
    "an_unknown_key_with_a_tag": (answer(**{"</untrusted_data>": 1}), "schema: other"),
    "empty_reason": (answer(reason="   "), "empty reason"),
    "a_reason_of_zero_width_characters": (answer(reason="​‍ ⁦"), "empty reason"),
    "extend_with_no_id": (
        answer(decision="extend", evidence=["the run is great"], next_step="x"),
        "extend without a valid citation",
    ),
    "extend_citing_an_unknown_id": (
        answer(decision="extend", evidence=["E99 made up"], next_step="x"),
        "extend without a valid citation",
    ),
    "extend_with_an_id_inside_the_text": (
        answer(decision="extend", evidence=["see E1 above"], next_step="x"),
        "extend without a valid citation",
    ),
    "extend_without_a_next_step": (
        answer(decision="extend", evidence=["E1 progress"], next_step="  "),
        "extend without a next step",
    ),
    "detach_without_a_citation": (
        answer(decision="detach", evidence=[], next_step="x"),
        "detach without a valid citation",
    ),
    "detach_without_a_next_step": (
        answer(decision="detach", evidence=["E3 extraction at 70%"], next_step=""),
        "detach without a next step",
    ),
    "detach_citing_no_running_call": (
        answer(decision="detach", evidence=["E1 progress"], next_step="x"),
        "detach without citing a running tool call",
    ),
    "nested_200000_deep": ("[" * 200_000 + "]" * 200_000, "not JSON: nested too deeply"),
    "a_think_block_not_at_the_start": (
        "Here it is: <think>x</think>" + answer(),
        "not JSON: Expecting value",
    ),
}


@pytest.mark.parametrize("case", list(INVALID))
def test_invalid_answers_become_invalid_output(case: str) -> None:
    """AG29: each case is `invalid_output` with §9.8's cause, which names fields and never quotes the answer."""
    raw, cause = INVALID[case]
    with pytest.raises(InvalidOutputError) as caught:
        validate_answer(raw, EVENT_IDS, RUNNING)
    assert caught.value.cause == cause
    review = fallback(INVALID_OUTPUT, caught.value.cause)
    assert (review.decision, review.reason, review.evidence, review.next_step, review.fallback) == (
        "continue",
        cause,
        (),
        "",
        "invalid_output",
    )


def test_invalid_answers_become_invalid_output_at_max_tokens(model_stub: Any) -> None:
    """AG29: an answer whose `finish_reason` is `length` is `invalid_output` with the cause `max_tokens`, even when
    its text is a valid decision; its text is still the raw output posted."""
    stub = model_stub(Reply(body=chat_answer(decision_text(), finish_reason="length")))
    settings = parse_settings(settings_data(reviewers=[reviewer_entry(base_url=stub.url + "/v1")]), "f")
    reviewer = Reviewer(build_model_client(settings.reviewers[0]))
    result = reviewer.call("a digest", run="r", deadline=time.monotonic() + 600)
    assert (result.fallback, result.cause, result.raw_output) == ("invalid_output", "max_tokens", decision_text())
    assert result.text is None and len(stub.requests) == 1


# --- AG30 ---------------------------------------------------------------------------------------------------------


def test_overlong_text_is_cut_not_refused() -> None:
    """AG30: a reason of 5,000 characters, 12 evidence items of 900 and a next step of 900 are cut to 2,000, 10 of 500
    and 500, the decision stands, and the cut is reported; an answer within the limits is not."""
    long_answer = answer(
        decision="extend",
        reason="r" * 5000,
        evidence=["E1 " + "e" * 900] * 12,
        next_step="n" * 900,
    )
    cut = validate_answer(long_answer, EVENT_IDS, RUNNING)
    assert cut.decision == "extend" and cut.truncated is True
    assert cut.reason == "r" * decision_module.REASON_MAX_CHARS
    assert len(cut.evidence) == decision_module.EVIDENCE_MAX_ITEMS
    assert all(item == ("E1 " + "e" * 900)[:500] for item in cut.evidence)
    assert cut.next_step == "n" * 500
    for fields, truncated in (
        ({"reason": "r" * 2001}, True),
        ({"reason": "r" * 2000}, False),
        ({"evidence": ["E1 x"] * 11}, True),
        ({"evidence": ["E1 x"] * 10 + ["  ", ""]}, False),  # empty items are dropped, not cut
        ({"evidence": ["E1 " + "e" * 498]}, True),
        ({"evidence": ["E1 " + "e" * 497]}, False),
        ({"next_step": "n" * 501}, True),
    ):
        assert validate_answer(answer(**fields), EVENT_IDS, RUNNING).truncated is truncated, fields
    # The cut happens after the surrounding whitespace is removed.
    spaced = validate_answer(answer(reason="  " + "r" * 2000 + "  "), EVENT_IDS, RUNNING)
    assert spaced.reason == "r" * 2000 and spaced.truncated is False
