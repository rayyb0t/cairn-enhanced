"""Removal of secrets from run text before it goes into a digest (spec §9.6, cleaning steps 3 and 5).

Two layers, run on every untrusted string of a digest, so before the model call and before the digest is stored on
the server:

- `ExactSecrets` (step 3): the agent's own secrets, by exact value. The agent token, the `api_key` of every reviewer in
  the settings file (all of them, so that no reviewer receives another's key) and the server API key, each of at least
  8 characters, raw and in their JSON-escaped forms (once, and once more, as a JSON file printed inside a JSON string
  holds them), become `[REDACTED:agent_token]`, `[REDACTED:reviewer_key]` and `[REDACTED:server_api_key]`. A local
  worker can read the agent token through the server's token file (§11.2), and the intent text and the facts come from
  the server, which no exact-value layer has seen.
- `redact_patterns` (step 5, the last layer): credential-shaped text becomes `[REDACTED:<kind>]`. Best effort: a
  credential in a shape it does not know reaches the digest. Worker credentials are removed by exact value before the
  agent ever sees a copy (§8.8).

Counts are kept per label or kind, for the `repair digest built` line; a value is never logged.

Every pattern runs in time linear in the text: a run's output can be large and is run text, so no pattern may
backtrack over an unbounded run of characters, and the secret_assignment layer scans each value at most once.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping

MIN_SECRET_LENGTH = 8

# The labels of the three kinds, as they appear in [REDACTED:<label>] and in the counts.
AGENT_LABEL = "agent_token"
REVIEWER_LABEL = "reviewer_key"
SERVER_LABEL = "server_api_key"


def _json_forms(value: str) -> set[str]:
    """`value` and its JSON-escaped forms: escaped once (with and without ensure_ascii), and those escaped again."""
    once = {json.dumps(value, ensure_ascii=flag)[1:-1] for flag in (True, False)}
    twice = {json.dumps(text, ensure_ascii=flag)[1:-1] for text in once for flag in (True, False)}
    return {value} | once | twice


class ExactSecrets:
    """The agent's own secrets, removed by exact value (spec §9.6 step 3). Never logged, never in a repr."""

    def __init__(self, values: Mapping[str, Iterable[str | None]]) -> None:
        labels: dict[str, str] = {}
        for label, items in values.items():
            for value in items:
                if value is None or len(value) < MIN_SECRET_LENGTH:
                    continue
                for form in _json_forms(value):
                    labels.setdefault(form, label)
        self._labels = labels
        # Longest first: a value that holds a shorter one is replaced whole (as §8.8's redact_exact does).
        ordered = sorted(labels, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(form) for form in ordered)) if ordered else None
        self.longest_form = len(ordered[0]) if ordered else 0  # the length of the longest form, never a value

    def __repr__(self) -> str:
        return f"ExactSecrets(<{len(self._labels)} forms>)"

    @classmethod
    def for_agent(
        cls, agent_token: str | None, reviewer_keys: Iterable[str | None], server_api_key: str | None
    ) -> ExactSecrets:
        """The three kinds of §9.6 step 3: the agent token, every reviewer's key and the server API key."""
        return cls({AGENT_LABEL: [agent_token], REVIEWER_LABEL: list(reviewer_keys), SERVER_LABEL: [server_api_key]})

    def redact(self, text: str, counts: Counter[str]) -> str:
        if self._pattern is None:
            return text

        def replace(match: re.Match[str]) -> str:
            label = self._labels[match.group(0)]
            counts[label] += 1
            return f"[REDACTED:{label}]"

        return self._pattern.sub(replace, text)


NO_SECRETS = ExactSecrets({})


# --- the pattern layer (step 5) ----------------------------------------------------------------------------------------

# (kind, pattern, group): the whole match is replaced, or, with a group, only that group's text, so that a kept prefix
# (the word Bearer, a URL's scheme) stays. Every quantifier that is followed by more pattern runs over a class that the
# next character of the pattern is not in, or is bounded, so no match attempt backtracks over an unbounded run; and no
# unbounded run is scanned from more than one of its positions (the jwt kind, below).
_BASE64URL = r"[A-Za-z0-9_\-]"  # the alphabet of a JWT's three parts
# A JWT starts at a word boundary with `eyJ` (base64url of `{"`). `\beyJ` also matches after a `-`, which is in the
# base64url alphabet, so `\beyJ…` alone would start a match at every `eyJ` of a run such as `eyJ-eyJ-…` and scan the
# rest of the run each time, a time quadratic in the run's length. So a match starts only where a run of base64url
# characters starts, skips (possessively, never giving a character back) the run's characters before its leftmost
# `\beyJ`, and takes the token as group 1, so that the skipped characters are kept. It finds the same tokens as
# `\beyJ…`: a later `eyJ` in the same run would end its first part at the same place, the end of that run, so it cannot
# match where the leftmost one did not.
_JWT = re.compile(
    rf"(?<!{_BASE64URL})(?:(?!\beyJ){_BASE64URL})*+"  # the run's characters before its leftmost \beyJ, kept
    rf"(eyJ{_BASE64URL}{{10,}}+\.{_BASE64URL}{{10,}}+\.{_BASE64URL}{{10,}}+)"  # the token
)
# The private_key kind: a block from a BEGIN marker to the first END marker after it, or to the end of the text.
PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(r"-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----")
_PRIVATE_KEY = re.compile(rf"{PRIVATE_KEY_BEGIN.pattern}[\s\S]*?(?:{PRIVATE_KEY_END.pattern}|\Z)")
_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("private_key", _PRIVATE_KEY, 0),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("jwt", _JWT, 1),
    ("bearer", re.compile(r"(?i)\bbearer[ \t]+([A-Za-z0-9._~+/=\-]{16,})"), 1),
    # scheme://user:password@ -> scheme://[REDACTED:url_userinfo]@ ; the scheme is at most 32 characters.
    ("url_userinfo", re.compile(r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://([^\s/?#@:]*+:[^\s/?#@]*+)@"), 1),
)

# secret_assignment: <name containing key|secret|token|passw|pwd|auth> = or : <6+ characters>, plain or JSON; the name
# is kept. Any name containing "key" counts, the bare name "key" included (pi's auth.json: {"type": "api_key",
# "key": ...}). A name is a whole run of name characters (the possessive quantifier never gives one back), so the
# search below looks at each run once; a run whose name is no secret's is passed over, and the search goes on after it,
# so an assignment inside another one's value ("x=a_token=...") is still found. A value that starts with an HTTP
# authentication scheme word ("Authorization: Basic <credentials>") keeps the word, as the bearer kind does, and loses
# the credentials after it; a value already redacted by an earlier kind is left as it is.
#
# Each value is scanned at most once: a value that is redacted is passed over whole, and a value already redacted is
# recognised from its first characters, before the value pattern runs, since the search then goes on inside it. Were
# such a value scanned to its end first, each further assignment inside it ("auth=[REDACTED:x]auth=[REDACTED:x]...",
# which a target can print and the dispatcher's exact-value layer writes) would scan the rest of the run again, a time
# quadratic in its length.
_NAME_SEPARATOR = re.compile(r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]++)([\"']?[ \t]*+[=:][ \t]*+)")
_SECRET_NAME = re.compile(r"key|secret|token|passw|pwd|auth", re.IGNORECASE)
_VALUE_DOUBLE_QUOTED = re.compile(r"\"((?:[^\"\\\n]|\\.){6,})")
_VALUE_SINGLE_QUOTED = re.compile(r"'([^'\n]{6,})")
_VALUE_PLAIN = re.compile(r"([^\s\"',;]{6,})")
_SCHEME_WORD = re.compile(r"(?i)(?:basic|bearer|digest|token|negotiate|ntlm)[ \t]++")
_REDACTED_PREFIX = "[REDACTED:"
_SECRET_MIN_LENGTH = 6


def _already_redacted(text: str, start: int) -> bool:
    """Whether the value that starts at `start` (after its opening quote, if any) holds a redaction where its secret
    would start: `[REDACTED:` there, or after a scheme word. A plain value's scheme word is passed over before its span
    is taken; a quoted value's only when at least 6 of the value's characters follow it, as `[REDACTED:` (10
    characters, none a quote, a backslash or a line break) always does. So this is the answer the value's span gives,
    found without scanning the value."""
    if text.startswith(_REDACTED_PREFIX, start):
        return True
    scheme = _SCHEME_WORD.match(text, start)
    return scheme is not None and text.startswith(_REDACTED_PREFIX, scheme.end())


def _assignment_value(text: str, start: int) -> tuple[int, int] | None:
    """The span of the secret in the value that starts at `start`, or None when there is none to redact."""
    quoted = text.startswith(('"', "'"), start)
    if _already_redacted(text, start + 1 if quoted else start):
        return None  # before the value pattern runs, so that a value passed over is never scanned to its end
    if text.startswith('"', start):
        value = _VALUE_DOUBLE_QUOTED.match(text, start)
    elif quoted:
        value = _VALUE_SINGLE_QUOTED.match(text, start)
    else:
        scheme = _SCHEME_WORD.match(text, start)
        value = _VALUE_PLAIN.match(text, scheme.end() if scheme is not None else start)
    if value is None:
        return None
    begin, end = value.span(1)
    scheme = _SCHEME_WORD.match(text, begin, end)
    if scheme is not None and end - scheme.end() >= _SECRET_MIN_LENGTH:
        begin = scheme.end()
    return begin, end


def _redact_assignments(text: str, counts: Counter[str]) -> str:
    parts: list[str] = []
    kept_until = 0
    position = 0
    while True:
        match = _NAME_SEPARATOR.search(text, position)
        if match is None:
            break
        name_end = match.end(1)
        span = _assignment_value(text, match.end()) if _SECRET_NAME.search(match.group(1)) else None
        if span is None:
            position = name_end
            continue
        parts.append(text[kept_until : span[0]])
        parts.append("[REDACTED:secret_assignment]")
        counts["secret_assignment"] += 1
        kept_until = position = span[1]
    if not parts:
        return text
    parts.append(text[kept_until:])
    return "".join(parts)


def private_key_blocks(text: str) -> Iterator[tuple[int, int]]:
    """The private_key kind's blocks in `text`, in order, as (start, end): the blocks its pattern matches, found with a
    search for each marker, so that a long text takes a scan for each marker rather than a step of the pattern at each
    of its characters (test_digest checks that both find the same blocks)."""
    position = 0
    while (begin := PRIVATE_KEY_BEGIN.search(text, position)) is not None:
        end = PRIVATE_KEY_END.search(text, begin.end())
        position = len(text) if end is None else end.end()
        yield begin.start(), position


def redact_patterns(text: str, counts: Counter[str]) -> str:
    """Spec §9.6, pattern-based redaction: credential-shaped text becomes `[REDACTED:<kind>]`."""
    for kind, pattern, group in _PATTERNS:

        def replace(match: re.Match[str], kind: str = kind, group: int = group) -> str:
            counts[kind] += 1
            if group == 0:
                return f"[REDACTED:{kind}]"
            whole = match.group(0)
            start, end = match.start(group) - match.start(0), match.end(group) - match.start(0)
            return f"{whole[:start]}[REDACTED:{kind}]{whole[end:]}"

        text = pattern.sub(replace, text)
    return _redact_assignments(text, counts)
