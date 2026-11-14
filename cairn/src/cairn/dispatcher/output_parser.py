from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        for start in _object_start_positions(segment):
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    segments = [text.strip()]
    segments.extend(match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(text))
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in enumerate(text) if char == "{"]


# ---------------------------------------------------------------------------
# Conservative rescue of conclude outputs whose JSON is broken.
#
# Only used after extract_json_object plus the task validator have already
# failed (see contracts.parse_validated), so the result of any output that
# parses today never changes. Only an output that is exactly one accepted
# envelope can be rescued; anything ambiguous is refused and the caller fails
# exactly as before.
# ---------------------------------------------------------------------------

# JSON whitespace only: Python's \s would also match NBSP, U+2028 and other characters JSON rejects.
_JSON_WS = "[ \t\n\r]*"
_SOLE_FENCE_RE = re.compile(r"\A```[A-Za-z]*[ \t]*\r?\n(.*?)```\Z", re.DOTALL)
# Raw C0 control characters other than tab, LF and CR (NUL, ESC, ...) are never accepted in rescued text.
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# A quote followed by two closing braces is how an envelope ends, so free text must not contain it.
_ENVELOPE_CLOSING_RE = re.compile('"' + _JSON_WS + r"\}" + _JSON_WS + r"\}")
# The end of one JSON member and the start of another: a closing quote or bracket, then a comma and
# a quoted, single-quoted or bare key, or a quoted key without the comma, followed by a colon.
_MEMBER_BOUNDARY_RE = re.compile(
    r'["}\]]' + _JSON_WS + "(?:," + _JSON_WS + r"""(?:"[^"]*"|'[^']*'|[A-Za-z_$][\w$-]*)|"[^"]*")""" + _JSON_WS + ":"
)


@dataclass(frozen=True)
class TextEnvelope:
    """An accepted payload whose only free-form value is a single string.

    ``path`` is the key path from the top-level object to that string, e.g.
    ("data", "description") for {"accepted": true, "data": {"description": "..."}}.
    """

    path: tuple[str, ...]
    prefix: re.Pattern[str]
    suffix: re.Pattern[str]

    @classmethod
    def for_path(cls, *path: str) -> TextEnvelope:
        opening = r"\{" + _JSON_WS + '"accepted"' + _JSON_WS + ":" + _JSON_WS + "true" + _JSON_WS + "," + _JSON_WS
        keys = (r"\{" + _JSON_WS).join('"' + re.escape(key) + '"' + _JSON_WS + ":" + _JSON_WS for key in path)
        closing = (_JSON_WS + r"\}") * len(path)
        return cls(path, re.compile(opening + keys + '"'), re.compile('"' + closing + r"\Z"))

    def build(self, text: str) -> dict[str, Any]:
        node: Any = text
        for key in reversed(self.path):
            node = {key: node}
        return {"accepted": True, **node}

    def text_of(self, payload: dict[str, Any]) -> str:
        node: Any = payload
        for key in self.path:
            node = node[key]
        return node

    def matches(self, payload: Any) -> bool:
        """Exactly this envelope: accepted is true, no other keys at any level, and a string
        without unsafe control characters."""
        if not isinstance(payload, dict) or set(payload) != {"accepted", self.path[0]}:
            return False
        if payload["accepted"] is not True:
            return False
        node: Any = payload[self.path[0]]
        for key in self.path[1:]:
            if not isinstance(node, dict) or set(node) != {key}:
                return False
            node = node[key]
        return isinstance(node, str) and _UNSAFE_CONTROL_RE.search(node) is None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


# strict=False relaxes exactly one rule: raw control characters (U+0000-U+001F) inside strings.
_LENIENT_DECODER = json.JSONDecoder(strict=False, object_pairs_hook=_reject_duplicate_keys)


def rescue_text_envelope(text: str, envelope: TextEnvelope) -> tuple[str, dict[str, Any]] | None:
    """Recover the payload of an output whose JSON is broken, or return None.

    The output must be exactly one object, optionally as the only content of one code fence,
    and the result must be exactly ``envelope`` with accepted true:

    1. "lenient_decode": the object is valid JSON apart from raw control characters inside
       strings (for example literal newlines). It is decoded with strict=False and duplicate
       keys are refused.
    2. "envelope_repair": the object is not JSON at all (a syntax error), but it starts with the
       envelope's fixed prefix and ends with its fixed suffix, so the text in between is taken
       verbatim as the string. Refused when that text could hide where the string ends or what
       it means: any backslash, the envelope's closing sequence, unbalanced braces, or a
       key/value boundary that is not inside an embedded {...} which is itself a JSON object.
    """
    region = _sole_object(text)
    if region is None:
        return None
    try:
        payload = _LENIENT_DECODER.decode(region)
    except json.JSONDecodeError:
        pass  # not JSON at all: only the envelope repair below may apply
    except (ValueError, RecursionError):
        return None  # JSON refused for another reason (duplicate key, number limit, nesting depth)
    else:
        # Valid JSON (raw control characters aside) keeps its own meaning and is never re-read as text.
        return ("lenient_decode", payload) if envelope.matches(payload) else None
    head = envelope.prefix.match(region)
    tail = envelope.suffix.search(region, head.end()) if head is not None else None
    if head is None or tail is None:
        return None
    body = region[head.end() : tail.start()]
    if not _free_text_is_unambiguous(body):
        return None
    payload = envelope.build(body)
    return ("envelope_repair", payload) if envelope.matches(payload) else None


def _sole_object(text: str) -> str | None:
    body = text.strip(" \t\r\n\ufeff")
    fence = _SOLE_FENCE_RE.match(body)
    if fence is not None:
        body = fence.group(1).strip(" \t\r\n")
    if not (body.startswith("{") and body.endswith("}")) or "```" in body:
        return None
    return body


def _free_text_is_unambiguous(text: str) -> bool:
    if "\\" in text or _ENVELOPE_CLOSING_RE.search(text):
        return False
    groups: list[tuple[int, int]] = []  # top-level {...} spans; braces must balance
    depth = start = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                groups.append((start, index + 1))
    if depth:
        return False
    # A key/value boundary may only sit inside an embedded {...} that is itself a JSON object (for
    # example a quoted request body); anywhere else it could be another member of the envelope.
    group = 0
    is_object: dict[int, bool] = {}
    for match in _MEMBER_BOUNDARY_RE.finditer(text):
        position = match.start() + 1  # just after the closing quote or bracket
        while group < len(groups) and groups[group][1] <= position:
            group += 1
        if group == len(groups) or groups[group][0] >= position:
            return False
        if group not in is_object:
            is_object[group] = _is_json_object(text[groups[group][0] : groups[group][1]])
        if not is_object[group]:
            return False
    return True


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(_LENIENT_DECODER.decode(text), dict)
    except (ValueError, RecursionError):
        return False
