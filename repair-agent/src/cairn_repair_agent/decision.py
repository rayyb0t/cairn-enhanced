"""Validation of the model's answer (spec §9.8).

The answer text (after the think-block rule, Appendix C-1) must be exactly one JSON object with the four keys of
`DECISION_SCHEMA` and their types, and `extend` and `detach` must cite ids present in this review's digest: a
`detach` must cite a tool call listed under RUNNING AT THE CUT. Anything else is `InvalidOutputError`, whose `cause` is
posted as the reason of an automatic `continue` (`fallback: invalid_output`). Nothing is extracted from prose and no
tag is stripped.

Over-long text is cut, not refused, to the server's request limits (§6.5): reason 2,000 characters, at most 10
evidence items of 500, next step 500; `Decision.truncated` then says so. A cause never quotes the answer: it names
fields, and an unknown key only when it is a plain name.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

REASON_MAX_CHARS = 2000
EVIDENCE_MAX_ITEMS = 10
EVIDENCE_MAX_CHARS = 500
NEXT_STEP_MAX_CHARS = 500
DECISIONS = ("continue", "extend", "detach")
FIELDS = ("decision", "reason", "evidence", "next_step")
CITATION = re.compile(r"^\s*\[?([EM]\d+)\b")
_FIELD_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
_NAMES_SHOWN = 10
# What the server drops before it checks that a reason is not blank (§6.5): Unicode Cc and Cf.
_INVISIBLE = frozenset({"Cc", "Cf"})


class InvalidOutputError(ValueError):
    """An answer that fails §9.8; `cause` is the reason posted with `fallback: invalid_output`."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
    evidence: tuple[str, ...]
    next_step: str
    cited: tuple[str, ...]  # the evidence ids present in the digest, in order
    truncated: bool


def _cut(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _visible(text: str) -> bool:
    """Something is left once whitespace and Unicode Cc and Cf characters are removed, as the server cleans a reason
    before its not-blank check: a reason of zero-width characters only is empty here, not a 422 there."""
    return any(not char.isspace() and unicodedata.category(char) not in _INVISIBLE for char in text)


def _schema_problems(data: dict[object, object]) -> list[str]:
    problems: list[str] = []
    unknown = [key for key in data if key not in FIELDS]
    names = sorted({key if isinstance(key, str) and _FIELD_NAME.fullmatch(key) else "other" for key in unknown})
    problems.extend(names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        problems.append(f"+{len(names) - _NAMES_SHOWN} more")
    decision, reason, evidence, next_step = (data.get(name) for name in FIELDS)
    if not isinstance(decision, str) or decision not in DECISIONS:
        problems.append("decision")
    if not isinstance(reason, str):
        problems.append("reason")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        problems.append("evidence")
    if not isinstance(next_step, str):
        problems.append("next_step")
    return problems


def validate_answer(text: str, event_ids: Collection[str], running_ids: Collection[str]) -> Decision:
    """§9.8 on the answer text: `event_ids` are the E and M ids present in this digest, `running_ids` the E ids of
    the calls it lists as running at the cut. Raises InvalidOutputError with the cause."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidOutputError(f"not JSON: {exc.msg}") from None
    except RecursionError:
        raise InvalidOutputError("not JSON: nested too deeply") from None
    except ValueError:  # such as an integer longer than Python reads from text
        raise InvalidOutputError("not JSON: a value cannot be read") from None
    if not isinstance(data, dict):
        raise InvalidOutputError("schema: not an object")
    problems = _schema_problems(data)
    if problems:
        raise InvalidOutputError(f"schema: {','.join(problems)}")
    decision: str = data["decision"]
    reason, reason_cut = _cut(data["reason"], REASON_MAX_CHARS)
    if not _visible(reason):
        raise InvalidOutputError("empty reason")
    items = [item.strip() for item in data["evidence"]]
    items = [item for item in items if item]
    evidence: list[str] = []
    evidence_cut = len(items) > EVIDENCE_MAX_ITEMS
    for item in items[:EVIDENCE_MAX_ITEMS]:
        kept, was_cut = _cut(item, EVIDENCE_MAX_CHARS)
        evidence.append(kept)
        evidence_cut = evidence_cut or was_cut
    next_step, step_cut = _cut(data["next_step"], NEXT_STEP_MAX_CHARS)
    cited = tuple(
        match.group(1) for item in evidence if (match := CITATION.match(item)) and match.group(1) in event_ids
    )
    if decision in ("extend", "detach"):
        if not cited:
            raise InvalidOutputError(f"{decision} without a valid citation")
        if not next_step:
            raise InvalidOutputError(f"{decision} without a next step")
    if decision == "detach" and not any(identifier in running_ids for identifier in cited):
        raise InvalidOutputError("detach without citing a running tool call")
    return Decision(decision, reason, tuple(evidence), next_step, cited, reason_cut or evidence_cut or step_cut)
