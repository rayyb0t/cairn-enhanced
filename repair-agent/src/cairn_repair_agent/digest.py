"""The review digest (spec §9.6): built by code, never by the model, within a token budget.

The digest has two parts. The trusted header holds only what Cairn's server says about the run (`GET /repair/runs`)
and the copy's age, as `ReviewContext`. Everything else sits inside one `<untrusted_data id="<nonce>">` block, with a
fresh nonce per review: first the figures derived from the run's own log (`DERIVED FROM THE RUN'S LOG`: counts, times,
repeated commands and the calls running at the cut), then the intent and the facts, the last tool calls and the last
assistant messages. The run can write its own session file, so the derived figures are evidence, not facts, and the
system prompt says so; they are read only from the records the CLI writes (`transcripts.py`, §9.5), never from a tool's
output, which the last calls only quote.

Untrusted text is cleaned in this order (`clean_text`): NFKC; removal of Unicode Cc (except `\\n` and `\\t`), Cf, Cn,
Co and Cs; exact-value removal of the agent's own secrets; `<` and `>` mapped to U+2039 and U+203A, the square brackets
of a chat template's control token (`[INST]`, `[/INST]`, `[SYSTEM_PROMPT]`, `[|user|]`, `[|endofturn|]`, `[unused9]`,
in any case) to U+27E6 and U+27E7, and `untrusted...data` look-alikes rewritten to `untrusted-text`, so run text can
neither open nor close the fence, nor write a turn boundary into a rendered chat template; then the pattern layer. A
text is cleaned before it is cut to its limit, so no secret is cut in half before a layer could see it; a long one only
in parts near what can be shown, cut at line breaks, or inside a line with a guard (`_Cleaner`).

Size ladder, while over budget: last calls 12 → 10 → … → 4; output lines and message and fact lengths halved; output
lines 0+1, messages 300, facts 150; 2 calls only; finally a cut inside the fence that keeps the closing tag. Every limit
scales with `budget / 8000`. For a reviewer whose `context_window` is set, "over budget" also means over the room that
context leaves, counted by `token_bound` (`DigestBudget`).
"""

from __future__ import annotations

import bisect
import functools
import re
import secrets
import unicodedata
from array import array
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from .prompt import SYSTEM_PROMPT
from .redact import PRIVATE_KEY_END, ExactSecrets, private_key_blocks, redact_patterns
from .transcripts import CLI_TYPES, EDIT, FAILED, OK, SHELL, UNKNOWN, WRITE, ToolCall, Transcript, parse_time

DEFAULT_DIGEST_TOKENS = 8000
MIN_DIGEST_TOKENS = 4000  # Appendix C-1, Digest budget: half the default
FENCE = "untrusted_data"
LEFT_ANGLE = "\u2039"  # SINGLE LEFT-POINTING ANGLE QUOTATION MARK, for every '<' in run text
RIGHT_ANGLE = "\u203a"  # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK, for every '>' in run text
NOT_VISIBLE = (
    "Not visible here: processes that a shell command itself starts in the background or that run outside a tool "
    "call, sub-agents that a codex run spawns, and files written by shell commands."
)
CUT_NOTE = "…(digest cut to fit)"
REPEATED_TOP = 5
REPEATED_MIN = 3
MESSAGES_SHOWN = 2
TOOL_NAME_CHARS = 100
FACT_ID_CHARS = 40
_RUN_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}")
_FENCE_LOOKALIKE = re.compile(r"untrusted[\s_\-\u2010-\u2015\u2212]*data", re.IGNORECASE)
_ANGLES = str.maketrans({"<": LEFT_ANGLE, ">": RIGHT_ANGLE})
LEFT_WHITE_BRACKET = "\u27e6"  # MATHEMATICAL LEFT WHITE SQUARE BRACKET, for the '[' of a control token in run text
RIGHT_WHITE_BRACKET = "\u27e7"  # MATHEMATICAL RIGHT WHITE SQUARE BRACKET, for its ']'
# The control tokens that chat templates write in square brackets. A local model server renders the chat template with
# the digest inside and tokenizes that string with special-token parsing (Ollama's runner does, and so does a server
# that tokenizes with a Hugging Face tokenizer), so such a token in run text would become a real turn boundary. Three
# shapes, as the chat templates of local model servers write them (as of 2026-11):
# - a word of CONTROL_WORDS, also closed with '/': the whole set of the model family whose templates use [INST] and
#   [/INST] (the special tokens its tokenizer library lists), and another family's MASK words;
# - any word between '[|' and '|]': a third family's templates write their role and end-of-turn markers so
#   ([|system|], [|user|], [|assistant|], [|tool|], [|endofturn|]);
# - a reserved word '[unused<n>]': a fourth family's templates open and close each turn with [unused9] and [unused10].
CONTROL_WORDS = (
    "INST",
    "SYSTEM_PROMPT",
    "AVAILABLE_TOOLS",
    "TOOL_RESULTS",
    "TOOL_CALLS",
    "TOOL_CONTENT",
    "IMG",
    "IMG_BREAK",
    "IMG_END",
    "PREFIX",
    "MIDDLE",
    "SUFFIX",
    "ARGS",
    "CALL_ID",
    "AUDIO",
    "BEGIN_AUDIO",
    "TRANSCRIBE",
    "THINK",
    "STREAMING_PAD",
    "STREAMING_WORD",
    "NEXT_AUDIO_TEXT",
    "REPEAT_AUDIO_TEXT",
    "MODEL_SETTINGS",
    "MASK",
    "GMASK",
    "SMASK",
)
_CONTROL_TOKEN = re.compile(
    r"\[(/?(?:" + "|".join(sorted(CONTROL_WORDS, key=len, reverse=True)) + r")|\|[A-Za-z_]{1,32}\||unused\d{1,4})\]",
    re.IGNORECASE,
)
# --- tokens and budget --------------------------------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """§9.6's estimate, the digest budget's measure: ASCII characters ÷ 3 (rounded up) plus one token per non-ASCII
    character. It is calibrated, not a bound: digits, tabs and newlines are one token each to the tokenizers that split
    them one by one, and a rare character is several. `token_bound` is the bound."""
    ascii_count = len(text.encode("ascii", "ignore"))
    return -(-ascii_count // 3) + (len(text) - ascii_count)


def token_bound(text: str) -> int:
    """A bound on the tokens of `text` that holds for byte-level BPE and byte-fallback SentencePiece tokenizers: one
    token per UTF-8 byte (Appendix C-1, Digest budget). Each of their vocabularies has a token for every byte, and a
    character that has no token of its own falls back to its bytes, so no text is more tokens than bytes: a rare or
    unassigned character is up to 4, as many as its bytes, and a digit, a tab or a newline is 1 to the tokenizers that
    split them."""
    return len(text.encode("utf-8", "surrogatepass"))


SYSTEM_PROMPT_TOKENS = estimate_tokens(SYSTEM_PROMPT)


def prompt_tokens_estimate(digest_text: str) -> int:
    """`prompt_est` of §9.11: the system prompt plus the digest, by §9.6's rule."""
    return SYSTEM_PROMPT_TOKENS + estimate_tokens(digest_text)


def context_window_room(context_window: int, max_tokens: int) -> int:
    """Appendix C-1, Digest budget: what a reviewer's context leaves for the digest once the system prompt and the
    longest answer fit, `context_window - system_est - max_tokens`. `system_est`, §9.6's estimate of the system prompt,
    is about 1,240, and covers the prompt itself and its chat template: the system prompt is 820 to 920 tokens to
    each of eight byte-level and byte-fallback tokenizers of current model families (build step R2's evidence), and a
    chat template adds at most about 100."""
    return context_window - SYSTEM_PROMPT_TOKENS - max_tokens


class DigestBudget(int):
    """The budget of the digests sent to one reviewer (Appendix C-1, Digest budget). The int is the budget by §9.6's
    estimate, which the size ladder scales with; `room`, set only when the reviewer's `context_window` is, is what that
    context leaves for the digest, which every digest built for it then fits by `token_bound`, so that a server whose
    real context is that `context_window` (or more) never gets a prompt longer than its context, which a truncating
    server (Ollama) would cut silently. Without a room, or with a `context_window` larger than the server's real
    context, nothing keeps the prompt within the context: the context-truncation check (`model._checked_prompt`) sees
    a cut only when the context is below about the prompt's estimate.

    It is an int so that it is logged and recorded as the budget it is: it equals that int and hashes as it. Two
    budgets, though, are equal only when their rooms are too. A reviewer with `context_window` 16,384 keeps the
    default budget of 8,000 and has a room, a reviewer without one has none, and their digests differ once the room
    cuts one; so a set or a dict of the budgets of several reviewers, like the gate replay's, which builds one digest
    per distinct budget, keeps them apart, and each reviewer gets the digest of its own budget and room. A plain int
    in such a set or dict would stand for a budget of any room, so keep them out of it; and arithmetic on a budget
    gives a plain int, without the room: `build_digest` reads the room of the budget it is given."""

    room: int | None

    def __new__(cls, tokens: int, room: int | None = None) -> DigestBudget:
        budget = super().__new__(cls, tokens)
        budget.room = room
        return budget

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DigestBudget):
            return int.__eq__(self, other) and self.room == other.room
        return int.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        if isinstance(other, DigestBudget):
            return int.__ne__(self, other) or self.room != other.room
        return int.__ne__(self, other)

    __hash__ = int.__hash__  # the int's, as `__eq__` must allow: a budget equals the int of its tokens


def digest_budget(max_tokens_estimate: int, context_window: int | None, max_tokens: int) -> DigestBudget:
    """The budget of the digests sent to a reviewer: `digest.max_tokens_estimate`, or less for a reviewer whose
    `context_window` cannot hold it with the system prompt and `max_tokens`; with a `context_window`, also the room it
    leaves, which each digest fits by `token_bound` (Appendix C-1, Digest budget)."""
    if context_window is None:
        return DigestBudget(max_tokens_estimate)
    room = context_window_room(context_window, max_tokens)
    return DigestBudget(min(max_tokens_estimate, room), room)


# --- cleaning ---------------------------------------------------------------------------------------------------------


UNWANTED_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs"})
_LAST_BMP = 0xFFFF


@functools.cache
def _unwanted_characters() -> re.Pattern[str]:
    """Runs of the characters below U+10000 of Unicode categories Cc (but `\\n` and `\\t`), Cf, Cn, Co and Cs, as this
    interpreter's `unicodedata` knows them, in one character class (built once, about 350 ranges), or runs of the
    characters above, whose categories `_without_unwanted` checks one by one. Cn, the unassigned code points, carries
    no text: a tokenizer has no token for one and spends one per UTF-8 byte on it, up to four. The class holds no range
    above U+FFFF, where Cn alone has about 370: with them, `re` checked every character of every text range by range,
    about 25 times slower."""
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for code in range(_LAST_BMP + 1):
        unwanted = code not in (0x09, 0x0A) and unicodedata.category(chr(code)) in UNWANTED_CATEGORIES
        if unwanted and start is None:
            start = code
        elif not unwanted and start is not None:
            ranges.append((start, code - 1))
            start = None
    if start is not None:
        ranges.append((start, _LAST_BMP))
    parts = [
        re.escape(chr(low)) if low == high else f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in ranges
    ]
    return re.compile("[" + "".join(parts) + "]+|[" + chr(_LAST_BMP + 1) + "-" + chr(0x10FFFF) + "]+")


def _without_unwanted(match: re.Match[str]) -> str:
    """A run of `_unwanted_characters`: nothing for a run below U+10000, and for a run above it, its characters that
    are not of those categories."""
    run = match.group()
    if ord(run[0]) <= _LAST_BMP:
        return ""
    return "".join(char for char in run if unicodedata.category(char) not in UNWANTED_CATEGORIES)


def _white_brackets(match: re.Match[str]) -> str:
    return LEFT_WHITE_BRACKET + match.group(1) + RIGHT_WHITE_BRACKET


def clean_text(text: str, exact: ExactSecrets, counts: Counter[str]) -> str:
    """Spec §9.6's cleaning of untrusted text, steps 1-5 in order; redactions are counted into `counts`. Step 4 maps
    the square brackets of a control token after steps 1 and 2, so a full-width form, or one with a removed character
    in it, is caught too, and after step 3, so a secret that holds a control token is removed whole first."""
    text = unicodedata.normalize("NFKC", text)
    text = _unwanted_characters().sub(_without_unwanted, text)
    text = exact.redact(text, counts)  # before the brackets are mapped: a secret may hold '<', '>' or '[INST]'
    text = text.translate(_ANGLES)
    text = _CONTROL_TOKEN.sub(_white_brackets, text)
    text = _FENCE_LOOKALIKE.sub("untrusted-text", text)
    return redact_patterns(text, counts)


# What of a long text is cleaned. The digest shows a prefix of each text, and the first and last lines of an output,
# while NFKC can make a text 18 times longer and every later step copies it: cleaned whole, one text near the copy's
# size took minutes and gigabytes. So a long text is cleaned only near what can be shown, in parts that are each
# cleaned as a text of their own: for a prefix, one window at its start; for an output, runs of whole lines from each
# end, and of a line too long for a run, only its start.
#
# A part is cut at a line break where it can be: no layer matches across one, so no secret is cut in half, but for
# the private-key kind, whose block spans lines. So a long output is first searched whole for that kind's blocks, with
# its own markers (`_KeyBlocks`, a search for each marker): the lines that a block holds after the one with its BEGIN,
# up to the one with its END, are never read, from either end, only counted as omitted, while the line with its BEGIN
# is read and the block redacted from there to the part's end, as it is to the text's end. That search reads the text
# raw, so it does not see a marker that only cleaning makes one (an NFKC look-alike, or one with characters in it that
# step 2 removes): such a block is redacted only within a part, to the part's end, and a part that holds an END with no
# BEGIN before it is cut after that END's line, with the lines before it.
#
# Inside a line, a cut can split a secret, and its first part then escapes the layers: so the last `_GUARD_CHARS`
# cleaned characters before such a cut are dropped, more than any secret but a private key, and at least the longest
# form of the agent's own secrets. Every kind but two matches a secret cut short once enough of it is there; the jwt
# kind needs a token's three parts and the url_userinfo kind the '@' after the userinfo, so a secret of theirs can run
# past the cut whatever its length. Neither holds white space: where the run of characters without white space that
# reaches the cut holds an `eyJ` or a `://` before the guard, the part is cut there too, before `eyJ` or after `://`.
_GUARD_CHARS = 4 * 1024
_WINDOW_SLACK = 16 * 1024  # a prefix window holds four times the shown length and this
_OUTPUT_RUN = 16 * 1024  # at least this much of an output's whole lines at a time
_PRIVATE_KEY_END_LINE = re.compile(PRIVATE_KEY_END.pattern + r"[^\n]*")
_UNENDED = re.compile(r"(?=eyJ)|://")  # where a jwt or url_userinfo secret can start: at the end of a match
_SPACE = re.compile(r"\s")
CUT_TEXT = "…"  # ends a text or a line cut before cleaning


@dataclass(frozen=True)
class _Cleaned:
    """An output cleaned: whole, or, for a long output, its non-empty lines read from its start and from its end (a
    line cut before cleaning ends with `CUT_TEXT`), and an estimate of the lines between them, which are never
    cleaned; None when the two reads met, so that every line is known."""

    whole: str | None = None
    first: tuple[str, ...] = ()
    last: tuple[str, ...] = ()
    between: int | None = None


def _after_orphan_key_end(cleaned: str) -> tuple[str, int]:
    """A part that does not start the text, without the end of a private key's block that started before it: the
    text after the last END line left unredacted, and the number of lines cut off."""
    block_end = max((found.end() for found in _PRIVATE_KEY_END_LINE.finditer(cleaned)), default=0)
    return cleaned[block_end:], (cleaned.count("\n", 0, block_end) + 1 if block_end else 0)


class _KeyBlocks:
    """The private-key blocks of a long output that span lines, as the private_key kind finds them in its raw text, so
    that the lines they hold are never read. A block within one line holds no line's start, and a part that holds any
    of what follows its BEGIN marker holds that marker too (a part is whole lines, or the start of a line), so the
    part's own cleaning redacts it."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._starts = array("q")
        self._ends = array("q")
        for start, end in private_key_blocks(text):
            if text.find("\n", start, end) < 0:
                continue
            if self._ends and text.find("\n", self._ends[-1], start) < 0:
                self._ends[-1] = end  # it starts on the line where the one before ends: one run of held lines
            else:
                self._starts.append(start)
                self._ends.append(end)

    def _holding(self, position: int) -> int | None:
        """The index of the block that holds `position` past its start, if one does."""
        index = bisect.bisect_left(self._starts, position) - 1
        return index if index >= 0 and self._ends[index] > position else None

    def skip(self, position: int) -> tuple[int, int]:
        """From a line's start: the start of the first line from there that no block holds, and the number of lines
        skipped. A block holds the lines up to the one with its END, or to the text's end."""
        text, skipped = self._text, 0
        while (index := self._holding(position)) is not None:
            line_break = text.find("\n", self._ends[index])
            after = len(text) if line_break < 0 else line_break + 1
            last_unended = line_break < 0 and not text.endswith("\n")  # a last line with no line break after it
            skipped += text.count("\n", position, after) + (1 if last_unended else 0)
            position = after
        return position, skipped

    def skip_back(self, stop: int, floor: int) -> tuple[int, int]:
        """From a line's end, back to the line's start `floor`: the end of the last line before it that no block holds,
        and the number of lines skipped. A block holds the lines after the one with its BEGIN."""
        text, skipped = self._text, 0
        while stop > floor:
            index = self._holding(max(floor, text.rfind("\n", 0, stop) + 1))
            if index is None:
                break
            first_held = max(floor, text.find("\n", self._starts[index]) + 1)
            skipped += 1 + text.count("\n", first_held, stop)
            stop = first_held - 1
        return stop, skipped


class _Cleaner:
    """Cleans each distinct text once per digest; the redactions of the texts a rendering used are summed at the
    end, so a text that the ladder drops adds none. A long text is cleaned only in parts near what can be shown."""

    def __init__(self, exact: ExactSecrets) -> None:
        self._exact = exact
        self._guard = max(_GUARD_CHARS, exact.longest_form)
        self._done: dict[tuple[object, ...], tuple[object, Counter[str]]] = {}
        self.used: set[tuple[object, ...]] = set()

    def _get(self, key: tuple[object, ...], make: Callable[[Counter[str]], object]) -> object:
        done = self._done.get(key)
        if done is None:
            counts: Counter[str] = Counter()
            done = (make(counts), counts)
            self._done[key] = done
        self.used.add(key)
        return done[0]

    def _whole(self, text: str) -> str:
        return cast(str, self._get(("whole", text), lambda counts: clean_text(text, self._exact, counts)))

    def _cut_line(self, text: str, counts: Counter[str]) -> str:
        """A part that ends inside a line, cleaned, without the guard before its end, and without what follows the
        first place where a jwt or url_userinfo secret can start in the run without white space that reaches it."""
        cleaned = clean_text(text, self._exact, counts)
        keep = max(0, len(cleaned) - self._guard)
        reaching = _last_run_start(cleaned)
        if reaching < keep:
            unended = _UNENDED.search(cleaned, reaching, keep + 2)  # + 2: an `eyJ` that starts before `keep` ends after
            if unended is not None:
                keep = min(keep, unended.end())
        return cleaned[:keep]

    def _part(self, text: str, start: int, end: int, window: int, counts: Counter[str]) -> tuple[str, bool]:
        """(the line from `start` to `end` cleaned, whether it was cut): whole when it fits `window`, else its first
        `window` characters, cut inside the line."""
        if end - start <= window:
            return clean_text(text[start:end], self._exact, counts), False
        return self._cut_line(text[start : start + window], counts), True

    def prefix(self, text: str, keep: int) -> str:
        """`text` cleaned, for a display of at most `keep` characters from its start: whole when it is short, else a
        window at its start, cut after the last line that ends in its second half or else inside a line, followed by
        `CUT_TEXT`."""
        size = 4 * keep + _WINDOW_SLACK
        if len(text) <= size:
            return self._whole(text)

        def make(counts: Counter[str]) -> str:
            line_break = text.rfind("\n", size // 2, size)
            if line_break >= 0:
                return clean_text(text[:line_break], self._exact, counts) + CUT_TEXT
            return self._cut_line(text[:size], counts) + CUT_TEXT

        return cast(str, self._get(("prefix", text, size), make))

    def output(self, text: str, head: int, tail: int, line_chars: int) -> _Cleaned:
        """An output cleaned, for a display of its first `head` and last `tail` non-empty lines, each at most
        `line_chars` characters: whole when it is short, else from each end, a run of whole lines or the start of a
        long line at a time, until the lines are found (or after a bounded number of parts), past the lines that a
        private key's block holds."""
        run = max(_OUTPUT_RUN, 4 * max(head, tail, 1) * line_chars)
        if len(text) <= 2 * run:
            return _Cleaned(self._whole(text))
        line_window = 4 * line_chars + self._guard

        def make(counts: Counter[str]) -> _Cleaned:
            blocks = _KeyBlocks(text)
            first: list[str] = []
            dropped = position = 0  # `position`: where the lines not yet read start
            for _ in range(2 * head + 2):
                if len(first) >= head or position >= len(text):
                    break
                position, held = blocks.skip(position)
                dropped += held
                if position >= len(text):
                    break
                line_break = text.rfind("\n", position, position + run) if position + run < len(text) else len(text)
                if line_break >= position:  # whole lines (the rest, when the text ends within a run)
                    cleaned = clean_text(text[position:line_break], self._exact, counts)
                    cut, following = False, line_break
                else:  # a line longer than a run: its start
                    following = text.find("\n", position + run)
                    following = len(text) if following < 0 else following
                    cleaned, cut = self._part(text, position, following, line_window, counts)
                if position:
                    cleaned, lost = _after_orphan_key_end(cleaned)
                    dropped += lost
                    cut = cut and bool(cleaned or not lost)  # a cut line cut off with an END line is not shown
                first += _lines(cleaned, cut)
                position = following + 1
            covered = min(position, len(text))  # the first line that the read from the start left starts here

            parts: list[list[str]] = []
            found, stop = 0, len(text)  # `stop`: where the lines not yet read end
            for _ in range(2 * tail + 2):
                if found >= tail or stop <= covered:
                    break
                stop, held = blocks.skip_back(stop, covered)
                dropped += held
                if stop <= covered:
                    break
                line_start = max(covered, text.rfind("\n", 0, stop) + 1)
                if stop - line_start > run:  # a line longer than a run: its start
                    start = line_start
                    cleaned, cut = self._part(text, start, stop, line_window, counts)
                else:  # the whole lines that start in the run before `stop`, but those that a block holds
                    start = max(covered, text.find("\n", max(0, stop - run - 1), line_start) + 1)
                    shown, held = blocks.skip(start)  # a block that ends before `line_start`, which none holds
                    dropped += held
                    cleaned, cut = clean_text(text[shown:stop], self._exact, counts), False
                cleaned, lost = _after_orphan_key_end(cleaned)
                dropped += lost
                cut = cut and bool(cleaned or not lost)
                parts.insert(0, _lines(cleaned, cut))
                found += len(parts[0])
                stop = start - 1
            last = [line for part in parts for line in part]
            unread_end = min(stop + 1, len(text))  # where the lines read from the end start
            if covered >= unread_end and not dropped:  # the two reads met: every line is known
                return _Cleaned(None, tuple(first), tuple(last), None)
            # The lines that start between the two reads are never read: one for each line break between them.
            between = text.count("\n", covered, unread_end) if unread_end > covered else 0
            return _Cleaned(None, tuple(first), tuple(last), between + dropped)

        return cast(_Cleaned, self._get(("output", text, head, tail, line_chars), make))

    def counts(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        for key in self.used:
            total.update(self._done[key][1])
        return dict(total)


def _lines(cleaned: str, cut: bool) -> list[str]:
    """The non-empty lines of a cleaned part; when the part was cut inside its last line, that line is marked, and so
    shown even when nothing of it is left."""
    lines = cleaned.split("\n")
    if cut:
        lines[-1] += CUT_TEXT
    return [line for line in lines if line.strip()]


def _last_run_start(text: str) -> int:
    """Where the run of characters without white space that ends `text` starts: `len(text)` when it ends in white
    space, 0 when it holds none."""
    space = _SPACE.search(text[::-1])
    return 0 if space is None else len(text) - space.start()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit)] + "…"


def _one_line(text: str) -> str:
    """Run text shown on one line of the digest (a command, a tool name, a fact id): its line breaks marked."""
    return text.replace("\n", " ⏎ ")


def _indented(text: str, indent: str) -> str:
    """Run text shown as a block: every line after the first indented, so that no line of it starts like an id."""
    return text.replace("\n", "\n" + indent)


# --- inputs -------------------------------------------------------------------------------------------------------------


def _count(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"run view: {name} is not an integer of at least {minimum}")
    return value


def _field(mapping: Mapping[str, Any], key: str) -> Any:
    """A value of a server answer, whatever its type: the caller checks it."""
    return mapping.get(key)


@dataclass(frozen=True)
class ReviewContext:
    """The trusted inputs: from `GET /repair/runs` (the run, its window, `slice_seconds`), and the copy's age. Each
    value is checked, so the header can never hold anything but an id of the server's rule, a CLI type and integers."""

    run_id: str
    cli_type: str
    window_number: int
    window_seconds: int
    budget_seconds: int
    elapsed_seconds: int
    remaining_seconds: int
    slices_used: int
    slice_seconds: int
    copy_age_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or _RUN_ID_RULE.fullmatch(self.run_id) is None:
            raise ValueError("run view: run_id does not match the server's rule")
        if self.cli_type not in CLI_TYPES:
            raise ValueError("run view: cli_type is not a reviewable CLI type")
        _count(self.window_number, "window.number", minimum=1)
        _count(self.window_seconds, "window.seconds", minimum=1)
        _count(self.budget_seconds, "budget_seconds", minimum=1)
        _count(self.elapsed_seconds, "elapsed_seconds")
        _count(self.remaining_seconds, "remaining_seconds")
        _count(self.slices_used, "slices_used")
        _count(self.slice_seconds, "slice_seconds")
        _count(self.copy_age_seconds, "copy age")

    @classmethod
    def from_run(cls, run: Mapping[str, Any], *, slice_seconds: Any, copy_age_seconds: int) -> ReviewContext:
        """From a `RunView` of `GET /repair/runs` and that answer's `slice_seconds`; every value is checked in
        `__post_init__`. Raises ValueError for a run view that does not have the shape of §6.3."""
        window = _field(run, "window")
        if not isinstance(window, Mapping):
            raise ValueError("run view: window is not an object")
        return cls(
            run_id=_field(run, "run_id"),
            cli_type=_field(run, "cli_type"),
            window_number=_field(window, "number"),
            window_seconds=_field(window, "seconds"),
            budget_seconds=_field(run, "budget_seconds"),
            elapsed_seconds=_field(run, "elapsed_seconds"),
            remaining_seconds=_field(run, "remaining_seconds"),
            slices_used=_field(run, "slices_used"),
            slice_seconds=slice_seconds,
            copy_age_seconds=copy_age_seconds,
        )


def copy_age(elapsed_seconds: float, seconds_since_poll: float, checked_elapsed_seconds: float) -> int:
    """The copy's age when the digest is built: the run's elapsed time then (`run.elapsed_seconds` plus the agent's
    monotonic time since that poll's response) minus `meta.checked_elapsed_seconds`, never below 0."""
    return max(0, int(elapsed_seconds + seconds_since_poll - checked_elapsed_seconds))


@dataclass(frozen=True)
class Fact:
    fact_id: str
    description: str


def facts_for_run(
    project: object, *, intent_from: object, own_intent_id: object, since: datetime, until: datetime | None = None
) -> tuple[list[Fact] | None, list[Fact] | None]:
    """The source facts of the run's intent and the facts other intents added since the run started, from a
    `GET /projects/{id}` answer (spec §9.6, Inputs). Facts carry no time, so a fact is new when the intent that made it
    (its `to`) concluded after `since` (and, with `until`, not after it). (None, None) when the answer does not have
    that shape; unreadable entries are left out."""
    if not isinstance(project, Mapping):
        return None, None
    facts, intents = project.get("facts"), project.get("intents")
    if not isinstance(facts, list) or not isinstance(intents, list):
        return None, None
    by_id: dict[str, Fact] = {}
    for item in facts:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and isinstance(item.get("description"), str):
            by_id.setdefault(item["id"], Fact(item["id"], item["description"]))
    sources: list[Fact] = []
    if isinstance(intent_from, list):
        for fact_id in intent_from:
            if isinstance(fact_id, str) and fact_id in by_id and by_id[fact_id] not in sources:
                sources.append(by_id[fact_id])
    added: list[tuple[datetime, int, Fact]] = []
    for position, intent in enumerate(intents):
        if not isinstance(intent, Mapping) or intent.get("id") == own_intent_id:
            continue
        target, concluded = intent.get("to"), parse_time(intent.get("concluded_at"))
        if not isinstance(target, str) or target not in by_id or concluded is None or concluded <= since:
            continue
        if until is None or concluded <= until:
            added.append((concluded, position, by_id[target]))
    added.sort(key=lambda item: (item[0], item[1]))
    return sources, [fact for _, _, fact in added]


# --- the digest ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Digest:
    text: str
    nonce: str
    tokens_estimate: int
    budget: int
    shrunk: int  # ladder steps taken; one more than the last step when the final cut was needed
    cut: datetime
    event_ids: frozenset[str]  # the E and M ids present in the text
    running_ids: frozenset[str]  # the E ids listed under RUNNING AT THE CUT, present in the text
    tool_calls: int
    running: int
    redactions: dict[str, int] = field(default_factory=dict)  # per kind, never values

    @property
    def redaction_count(self) -> int:
        return sum(self.redactions.values())


@dataclass(frozen=True)
class _Limits:
    calls: int
    head_lines: int
    tail_lines: int
    message_chars: int
    fact_chars: int
    line_chars: int
    command_chars: int


def ladder(budget: int) -> list[_Limits]:
    """The size ladder's steps for a budget; every limit scales with `budget / 8000`.

    §9.6 limits the number of last calls, never the calls running at the cut or the facts: each running call is shown,
    so that detach can cite it, the newest included, and each fact, with its length limited. When they do not fit, the
    final cut keeps the digest within its budget."""
    scale = budget / DEFAULT_DIGEST_TOKENS

    def scaled(value: int, cap: int | None = None, low: int = 1) -> int:
        result = max(low, round(value * scale))
        return result if cap is None else min(result, cap)

    fixed = {"line_chars": scaled(200), "command_chars": scaled(300)}
    first_calls = scaled(12, cap=60)
    fewest_calls = min(first_calls, scaled(4))
    head, tail, message, fact = scaled(2, cap=10, low=0), scaled(4, cap=40), scaled(1200, cap=6000), scaled(400)
    steps = [_Limits(first_calls, head, tail, message, fact, **fixed)]
    calls = first_calls
    while calls > fewest_calls:
        calls = max(fewest_calls, calls - scaled(2))
        steps.append(_Limits(calls, head, tail, message, fact, **fixed))
    steps.append(_Limits(fewest_calls, head // 2, tail // 2, message // 2, fact // 2, **fixed))
    steps.append(_Limits(fewest_calls, 0, scaled(1), scaled(300), scaled(150), **fixed))
    steps.append(_Limits(min(fewest_calls, scaled(2)), 0, scaled(1), scaled(300), scaled(150), **fixed))
    return steps


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"t+{whole // 60:02d}:{whole % 60:02d}"


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


_NORMALISE_CHUNK = 64 * 1024


def _normalised(command: str) -> str:
    """A command with its runs of white space made one space, for comparing commands: `" ".join(command.split())`,
    split a chunk at a time, each ending at white space, so that no word is cut. Split at once, a long command of short
    words made one object per word, gigabytes of them."""
    if len(command) <= _NORMALISE_CHUNK:
        return " ".join(command.split())
    parts: list[str] = []
    start = 0
    while start < len(command):
        space = _SPACE.search(command, start + _NORMALISE_CHUNK)
        end = space.start() if space is not None else len(command)
        part = " ".join(command[start:end].split())
        if part:
            parts.append(part)
        start = end
    return " ".join(parts)


class _Figures:
    """Everything derived from the transcript at the cut (spec §9.6, Content), before any text is shown."""

    def __init__(self, transcript: Transcript, cut: datetime) -> None:
        self.transcript = transcript
        self.cut = cut
        # The transcript up to the cut: its entries and unreadable lines, and its first record, where t+ starts (at the
        # cut when no record is at or before it). That record's time is kept as an offset from the transcript's start,
        # and an offset from it is found by subtraction alone (§9.5).
        self.lines = transcript.lines_at(cut)
        if self.lines.first is not None and transcript.started_at is not None:
            self._origin, self._shift = transcript.started_at, self.lines.first
        else:
            self._origin, self._shift = cut, timedelta(0)
        self.calls = transcript.calls_by(cut)
        self.ids = {id(call): f"E{number}" for number, call in enumerate(self.calls, start=1)}
        span = max(1.0, self.offset(cut))
        self.recent_from = 0.8 * span  # the last 20% of the run so far, as an offset from t0
        self.running = transcript.running_at(cut)
        self.messages = [message for message in transcript.messages if message.at <= cut]

        finished = [call for call in self.calls if call.finished_by(cut)]
        self.failed = [call for call in finished if call.status == FAILED]
        failed_ids = {id(call) for call in self.failed}
        successes = [call.finished_at for call in finished if call.status == OK and call.finished_at is not None]
        self.last_success = max(successes) if successes else None

        first_file: dict[str, ToolCall] = {}
        for call in finished:
            if call.status != FAILED:
                for path in call.paths:
                    first_file.setdefault(path, call)
        self.files = first_file

        self.commands: list[tuple[str, ToolCall]] = []  # (normalised command, call) of shell calls with a command
        for call in self.calls:
            if call.kind == SHELL and call.command is not None:
                self.commands.append((_normalised(call.command), call))
        first_command: dict[str, ToolCall] = {}
        for command, call in self.commands:
            first_command.setdefault(command, call)
        self.first_command = first_command
        self.last_new_command = max((call.started_at for call in first_command.values()), default=None)

        totals: Counter[str] = Counter(command for command, _ in self.commands)
        failures: Counter[str] = Counter(command for command, call in self.commands if id(call) in failed_ids)
        self.repeated = [
            (command, count, failures[command], first_command[command])
            for command, count in totals.most_common()
            if count >= REPEATED_MIN
        ][:REPEATED_TOP]
        self.streak = 0
        if self.commands:
            last = self.commands[-1][0]
            for command, _ in reversed(self.commands):
                if command != last:
                    break
                self.streak += 1

    def offset(self, at: datetime) -> float:
        """Seconds from the transcript's first record at or before the cut to `at`."""
        return ((at - self._origin) - self._shift).total_seconds()

    def recent(self, call: ToolCall) -> bool:
        return self.offset(call.started_at) >= self.recent_from


def _status(call: ToolCall, cut: datetime) -> str:
    """`running <s> s`, or the duration with `exit N`, `failed`, `ok` or `status unknown`: integers and fixed words."""
    if not call.finished_by(cut) or call.finished_at is None:
        return f"running {max(0, int(_seconds(cut, call.started_at)))} s"
    took = max(0, int(_seconds(call.finished_at, call.started_at)))
    if call.exit_code is not None:
        return f"{took} s, exit {int(call.exit_code)}"
    if call.status == FAILED:
        return f"{took} s, failed"
    if call.status == UNKNOWN:
        return f"{took} s, status unknown"
    return f"{took} s, ok"


def _output_lines(cleaned: _Cleaned, head: int, tail: int, limit: int) -> list[str]:
    """The first `head` and last `tail` non-empty lines of a cleaned output, each cut to `limit`."""
    if cleaned.whole is not None or cleaned.between is None:
        if cleaned.whole is not None:
            lines = [line for line in cleaned.whole.split("\n") if line.strip()]
        else:  # read from both ends, which met
            lines = [*cleaned.first, *cleaned.last]
        if not lines:
            return ["(no output)"]
        if len(lines) <= head + tail:
            return [_cut(line, limit) for line in lines]
        shown = [_cut(line, limit) for line in lines[:head]]
        shown.append(f"… ({len(lines) - head - tail} lines omitted) …")
        if tail:
            shown.extend(_cut(line, limit) for line in lines[-tail:])
        return shown
    # A long output, read from each end: the lines between were only counted, empty ones included.
    shown_first, shown_last = cleaned.first[:head], (cleaned.last[-tail:] if tail else ())
    omitted = len(cleaned.first) - len(shown_first) + cleaned.between + len(cleaned.last) - len(shown_last)
    shown = [_cut(line, limit) for line in shown_first]
    if omitted:
        shown.append(f"… (about {omitted} lines omitted) …")
    shown.extend(_cut(line, limit) for line in shown_last)
    return shown or ["(no output)"]


@dataclass
class _Body:
    """The lines inside the fence, with the size of the digest they make with the header and the closing tag. Once the
    digest is over budget no line is added: no rung then fits, and the final cut keeps lines from the top, so that no
    line after that one could be shown. Over budget means over `budget` by §9.6's estimate, or over `room` by
    `token_bound` when the reviewer's context_window sets one."""

    budget: int
    room: int | None
    ascii: int  # ASCII characters of the header, the lines with their line breaks, and the closing tag
    other: int  # and their other characters
    other_bytes: int  # and those characters' UTF-8 bytes
    lines: list[tuple[str, frozenset[str], bool]] = field(default_factory=list)  # (text, ids on it, a running entry)

    @property
    def over(self) -> bool:
        return not _fits(self.ascii, self.other, self.other_bytes, self.budget, self.room)

    def add(self, text: str, ids: frozenset[str] = frozenset(), running: bool = False) -> None:
        if self.over:
            return
        self.lines.append((text, ids, running))
        ascii_count, other, other_bytes = _characters(text)
        self.ascii, self.other = self.ascii + ascii_count + 1, self.other + other
        self.other_bytes += other_bytes

    def copy(self) -> _Body:
        return _Body(self.budget, self.room, self.ascii, self.other, self.other_bytes, list(self.lines))


def _fits(ascii_count: int, other: int, other_bytes: int, budget: int, room: int | None) -> bool:
    """Whether text of these characters is within the budget by §9.6's estimate, and within the room by
    `token_bound` (its UTF-8 bytes) when there is one."""
    if -(-ascii_count // 3) + other > budget:
        return False
    return room is None or ascii_count + other_bytes <= room


def _tool_name(call: ToolCall, clean: _Cleaner) -> str:
    if call.tool is None:
        return "?"
    return _cut(_one_line(clean.prefix(call.tool, TOOL_NAME_CHARS)), TOOL_NAME_CHARS)


def _subject(call: ToolCall, clean: _Cleaner, command_chars: int) -> str:
    text = call.command if call.kind == SHELL else call.target
    return "" if text is None else ": " + _cut(_one_line(clean.prefix(text, command_chars)), command_chars)


def _call_line(figures: _Figures, clean: _Cleaner, call: ToolCall, command_chars: int) -> str:
    return (
        f"{figures.ids[id(call)]} {_clock(figures.offset(call.started_at))} {_tool_name(call, clean)} "
        f"({_status(call, figures.cut)}){_subject(call, clean, command_chars)}"
    )


def _render_derived(figures: _Figures, clean: _Cleaner, command_chars: int, body: _Body) -> None:
    """The figures derived from the run's log, RUNNING AT THE CUT among them: the same on every rung, so rendered
    once. Every running call is listed (§9.6 limits only the last calls), while the digest is within its budget."""
    cut = figures.cut
    recent_calls = [call for call in figures.calls if figures.recent(call)]
    body.add("DERIVED FROM THE RUN'S LOG")
    body.add(
        f"entries {figures.lines.entries} | unreadable lines {figures.lines.unreadable} | "
        f"cut at {_clock(figures.offset(cut))}"
    )
    body.add(
        f"activity: {len(figures.calls)} tool calls, {len(figures.first_command)} distinct shell commands, "
        f"{len(figures.failed)} failed, {len(figures.files)} files written or edited through tools"
    )
    body.add(
        f"last 20% of the run (since {_clock(figures.recent_from)}): {len(recent_calls)} tool calls, "
        f"{sum(1 for call in figures.first_command.values() if figures.recent(call))} new distinct commands, "
        f"{sum(1 for call in figures.failed if figures.recent(call))} failed, "
        f"{sum(1 for call in figures.files.values() if figures.recent(call))} new files"
    )
    last_new = "none" if figures.last_new_command is None else _clock(figures.offset(figures.last_new_command))
    last_success = "none" if figures.last_success is None else _clock(figures.offset(figures.last_success))
    body.add(
        f"last new distinct command: {last_new} | last successful tool result: {last_success} | "
        f"identical last shell command repeated {figures.streak}x in a row"
    )

    if figures.repeated:
        body.add(f"REPEATED SHELL COMMANDS ({REPEATED_MIN}+ times):")
        for _, count, failed, first in figures.repeated:
            body.add(f"- {count}x ({failed} failed):{_subject(first, clean, command_chars)[1:]}")
    else:
        body.add(f"REPEATED SHELL COMMANDS ({REPEATED_MIN}+ times): none")

    if figures.running:
        body.add("RUNNING AT THE CUT:")
        for call in figures.running:
            if body.over:
                break
            body.add(_call_line(figures, clean, call, command_chars), frozenset({figures.ids[id(call)]}), running=True)
    else:
        body.add("RUNNING AT THE CUT: none")


def _render_rest(
    figures: _Figures,
    clean: _Cleaner,
    limits: _Limits,
    widest: _Limits,
    intent: str,
    source_facts: Sequence[Fact] | None,
    new_facts: Sequence[Fact] | None,
    body: _Body,
) -> None:
    """The intent, the facts, the last calls and the latest messages, with one rung's `limits`. A text is cleaned for
    the `widest` limits, the first rung's, so that each is cleaned once. Every fact is listed (§9.6 limits only its
    length), while the digest is within its budget."""
    cut = figures.cut
    body.add("INTENT:")
    body.add("  " + _indented(_cut(clean.prefix(intent, widest.fact_chars), limits.fact_chars), "  "))
    for heading, facts in (
        ("SOURCE FACTS", source_facts),
        ("FACTS ADDED BY OTHER RUNS SINCE THIS RUN STARTED", new_facts),
    ):
        if facts is None:
            body.add(f"{heading}: unknown")
        elif not facts:
            body.add(f"{heading}: none")
        else:
            body.add(f"{heading}:")
            for fact in facts:
                if body.over:
                    break
                text = _indented(_cut(clean.prefix(fact.description, widest.fact_chars), limits.fact_chars), "    ")
                body.add(f"- {_cut(_one_line(clean.prefix(fact.fact_id, FACT_ID_CHARS)), FACT_ID_CHARS)}: {text}")

    shown = figures.calls[-limits.calls :] if limits.calls > 0 else []
    body.add(f"LAST {len(shown)} OF {len(figures.calls)} TOOL CALLS (oldest first):")
    for call in shown:
        body.add(_call_line(figures, clean, call, limits.command_chars), frozenset({figures.ids[id(call)]}))
        if call.kind not in (WRITE, EDIT) and call.finished_by(cut) and call.answered_by(cut):
            output = clean.output(call.output, widest.head_lines, widest.tail_lines, widest.line_chars)
            for line in _output_lines(output, limits.head_lines, limits.tail_lines, limits.line_chars):
                body.add(f"    | {line}")

    messages = figures.messages[-MESSAGES_SHOWN:]
    if messages:
        body.add("LATEST AGENT MESSAGES (oldest first):")
        first = len(figures.messages) - len(messages) + 1
        for number, message in enumerate(messages, start=first):
            text = _indented(_cut(clean.prefix(message.text, widest.message_chars), limits.message_chars), "    ")
            body.add(f"M{number} {_clock(figures.offset(message.at))}: {text}", frozenset({f"M{number}"}))
    else:
        body.add("LATEST AGENT MESSAGES (oldest first): none")


def _header(context: ReviewContext) -> list[str]:
    percent = round(100 * context.elapsed_seconds / context.budget_seconds)
    return [
        "REVIEW CONTEXT (from Cairn; trusted)",
        f"run {context.run_id} | worker CLI {context.cli_type} | window {context.window_number} "
        f"({context.window_seconds} s)",
        f"time used {context.elapsed_seconds} s of budget {context.budget_seconds} s ({percent}%) | time left "
        f"{context.remaining_seconds} s | extensions used {context.slices_used} | one extension = "
        f"{context.slice_seconds} s",
        f"transcript copy age {context.copy_age_seconds} s",
        NOT_VISIBLE,
        "",
    ]


def build_digest(
    transcript: Transcript,
    context: ReviewContext,
    cut: datetime,
    *,
    intent: str,
    source_facts: Sequence[Fact] | None,
    new_facts: Sequence[Fact] | None,
    exact: ExactSecrets,
    budget: int,
    nonce: str | None = None,
) -> Digest:
    """Build the digest of `transcript` at `cut` (spec §9.6).

    `cut` is the time of the copy's last check, `meta.checked_elapsed_seconds` after the run's start (`live_cut`), or
    the replay's 75% point: a call with no finish by then is running at the cut, with its output hidden, and every
    figure counts only the transcript up to the cut. `intent` is the intent's description (`RunView.intent`);
    `source_facts` and `new_facts` come from `facts_for_run`, None when unknown. `exact` holds the agent's own secrets
    (`ExactSecrets.for_agent`), and `budget` is the reviewer's digest budget (`digest_budget`): neither has a default,
    so that no caller can build a digest without §9.6's step 3, or without its reviewer's budget, by leaving one out.
    A `DigestBudget` with a room also keeps the digest within that room by `token_bound`; a plain int sets no room.
    """
    nonce = nonce if nonce is not None else secrets.token_hex(8)
    opening, closing = f'<{FENCE} id="{nonce}">', f'</{FENCE} id="{nonce}">'
    figures = _Figures(transcript, cut)
    clean = _Cleaner(exact)
    head = "\n".join(_header(context)) + "\n" + opening + "\n"
    steps = ladder(budget)
    room = budget.room if isinstance(budget, DigestBudget) else None
    # The derived figures are the same on every rung: rendered once. When they alone are over budget, no rung fits.
    derived = _Body(budget, room, *_characters(head + closing))
    _render_derived(figures, clean, steps[0].command_chars, derived)
    derived_used = set(clean.used)
    shrunk = len(steps)  # the final cut, unless a rung fits
    body = derived
    if not derived.over:
        for index, limits in enumerate(steps):
            clean.used = set(derived_used)
            body = derived.copy()
            _render_rest(figures, clean, limits, steps[0], intent, source_facts, new_facts, body)
            if not body.over:
                shrunk = index
                break
    if shrunk == len(steps):
        body = _cut_body(body, head, closing, budget, room)
    text = _join(head, body, closing)
    present = frozenset(identifier for _, ids, _ in body.lines for identifier in ids)
    running = frozenset(identifier for _, ids, is_running in body.lines if is_running for identifier in ids)
    return Digest(
        text=text,
        nonce=nonce,
        tokens_estimate=estimate_tokens(text),
        budget=budget,
        shrunk=shrunk,
        cut=cut,
        event_ids=present,
        running_ids=running,
        tool_calls=len(figures.calls),
        running=len(figures.running),
        redactions=clean.counts(),
    )


def _join(head: str, body: _Body, closing: str) -> str:
    return head + "\n".join(line for line, _, _ in body.lines) + "\n" + closing


def _characters(text: str) -> tuple[int, int, int]:
    """The ASCII characters of `text`, its other characters, and their UTF-8 bytes."""
    ascii_count = len(text.encode("ascii", "ignore"))
    return ascii_count, len(text) - ascii_count, token_bound(text) - ascii_count


def _cut_body(body: _Body, head: str, closing: str, budget: int, room: int | None) -> _Body:
    """The last rung: a cut inside the fence. The body's lines are kept from the top while they fit with the note and
    the closing tag, which always stays."""
    ascii_count, other, other_bytes = _characters(head + CUT_NOTE + "\n" + closing)
    kept: list[tuple[str, frozenset[str], bool]] = []
    for line, ids, running in body.lines:
        line_ascii, line_other, line_bytes = _characters(line + "\n")
        if not _fits(ascii_count + line_ascii, other + line_other, other_bytes + line_bytes, budget, room):
            break
        ascii_count, other, other_bytes = ascii_count + line_ascii, other + line_other, other_bytes + line_bytes
        kept.append((line, ids, running))
    kept.append((CUT_NOTE, frozenset(), False))
    return _Body(budget, room, ascii_count, other, other_bytes, kept)


# The server keeps a run's started_at in whole seconds: it truncates its own clock to the second and subtracts the
# dispatcher's started_seconds_ago rounded to a second, so started_at is up to 1.5 s before the run's true start, and
# the dispatcher's checked_elapsed_seconds, rounded to 0.1 s, adds up to 0.05 s more. The live cut is that much later
# than the sum, so that it is never before the moment the copy was taken: every record in the copy is before the cut,
# and a call whose result is in the copy is finished at it. A running call's time is then overstated by at most about
# 2.6 s, which no figure of the digest depends on.
LIVE_CUT_SLACK_SECONDS = 2.0


def live_cut(started_at: datetime, checked_elapsed_seconds: float) -> datetime:
    """The live digest's cut: the copy's last check, `checked_elapsed_seconds` after the run's `started_at`, plus
    LIVE_CUT_SLACK_SECONDS for the server's whole-second `started_at` (§9.6). Raises ValueError when that time is out
    of range."""
    try:
        return started_at + timedelta(seconds=checked_elapsed_seconds + LIVE_CUT_SLACK_SECONDS)
    except OverflowError:
        raise ValueError("the copy's checked time is out of range") from None
