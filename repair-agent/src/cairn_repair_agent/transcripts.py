"""Readers of the worker CLIs' session files (spec §9.5): claude-code 2.1.98, codex 0.118.0 and pi 0.73.0.

Each reader turns a transcript copy into one CLI-neutral `Transcript`: the tool calls, each with its start, its answer
and, once it is known, when and how it finished; the assistant messages; the prompts and the conclude prompt. Readers
are lenient, so that a newer CLI degrades to fewer events.

Lines. The copy is decoded as UTF-8 with replacement and split at `\\n` only, never with `str.splitlines`: the pinned
CLIs write U+2028, U+2029 and U+0085 from a tool's output raw inside their JSON strings, and a reader that also ended a
line there would cut the record into pieces that do not decode and leave its call running at the cut. An empty piece,
such as the one after the file's final `\\n`, is skipped and not counted; any other piece is decoded as it is. Each line
is decoded and handled inside one guard: a line that `json.loads` cannot decode (`ValueError`, such as a half-written
last line, or `RecursionError`, such as a line nested 200,000 deep), that decodes to anything but an object, or whose
handling raises either (codex's `arguments` is a JSON string decoded inside the record), counts as unreadable, changes
nothing, and the next line is read. Handling raises nothing else: every field read here has its type checked first, a
field of another type counts as absent, and times are compared and subtracted, never converted.

Statuses come from the CLI's records, never from what a tool printed. Whether a call failed, its exit code, and whether
a job or process still runs are read from the fields the CLI writes (Claude Code's `is_error` and `toolUseResult`, pi's
`isError` and the status line it appends after the output), or, for codex, from the header codex builds around an
`exec_command` or `write_stdin` answer; never from the output itself, which is often the target's data. Where that part
cannot be found, the status is unknown.

Times. A `timestamp` is read with `datetime.fromisoformat`, a `Z` read as `+00:00`; one it cannot read counts as absent,
and one without a zone is UTC, set with `replace(tzinfo=timezone.utc)`, so that any two times compare. A time keeps the
offset it was read with: `astimezone` would read a time without a zone as the host's local time, and raises
`OverflowError` for a time near either end of the range.
"""

from __future__ import annotations

import bisect
import json
import re
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

CLI_TYPES = ("claudecode", "codex", "pi")
CONCLUDE_LINE = "This is the conclude phase."  # prompts/default/explore_conclude.md:4

# A call's kind, for the digest: its command or paths, whether its output is quoted, and whether it is ever listed as
# running at the cut (a poll never is).
SHELL = "shell"
WRITE = "write"
EDIT = "edit"
READ = "read"
POLL = "poll"
OTHER = "other"

# A finished call's status: what its record says. A codex call whose header cannot be read, and every codex tool that
# has no header, finishes with an unknown status, neither failed nor successful.
OK = "ok"
FAILED = "failed"
UNKNOWN = "unknown"

# Exit codes and session ids are read as at most 19 ASCII digits (with a '-' before an exit code that is negative):
# a 64-bit value. Longer runs of digits, which only a record written into the file holds, are no exit code: int()
# refuses more than 4,300 digits with a ValueError, which would make the whole line unreadable.
_EXIT_DIGITS = r"-?[0-9]{1,19}"
_ID_DIGITS = r"[0-9]{1,19}"
_INT_LIMIT = 2**63

# --- Claude Code 2.1.98 -------------------------------------------------------------------------------------------------

CLAUDE_OUTPUT_TOOLS = frozenset({"TaskOutput", "AgentOutputTool", "BashOutputTool"})  # cli.js:3504
CLAUDE_STOP_TOOLS = frozenset({"TaskStop", "KillShell"})  # cli.js:3493
CLAUDE_POLL_TOOLS = CLAUDE_OUTPUT_TOOLS | CLAUDE_STOP_TOOLS
CLAUDE_PATH_TOOLS = {
    "Write": (WRITE, "file_path"),
    "Edit": (EDIT, "file_path"),
    "NotebookEdit": (EDIT, "notebook_path"),
}
CLAUDE_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch"})
JOB_TERMINAL = ("completed", "failed", "killed")  # cli.js:7935
SHELL_JOB = "shell"  # a job that started with a backgroundTaskId (a Bash job)
AGENT_JOB = "agent"  # a job that started with status async_launched and an agentId (an Agent job)

_CLAUDE_EXIT_LINE = re.compile(rf"Exit code ({_EXIT_DIGITS})")  # cli.js:4144, the first line of a failed Bash answer
_TASK_ID_LINE = re.compile(r"<task-id>([^<>\n]+)</task-id>")
_TASK_STATUS_LINE = re.compile(r"<status>(completed|failed|killed)</status>")

# --- codex 0.118.0 ------------------------------------------------------------------------------------------------------

CODEX_SHELL_TOOLS = frozenset({"exec_command", "shell", "shell_command", "local_shell"})
CODEX_POLL_TOOLS = frozenset({"write_stdin"})
# The lines of the header codex builds between a `Command: ` line and the line `Output:`; each must be one of these.
_CODEX_HEADER_LINE = re.compile(
    r"Chunk ID: [0-9a-f]+"
    r"|Wall time: [0-9.]+ seconds"
    rf"|Process exited with code ({_EXIT_DIGITS})"
    rf"|Process running with session ID ({_ID_DIGITS})"
    r"|Original token count: [0-9]+"
)
_CODEX_HEADER_MAX_LINES = 16
# The text codex builds for a patch it applied (probe codexpatch-session.jsonl:11), matched as a prefix, never decoded.
CODEX_PATCH_APPLIED = '{"output":"Success. Updated the following files:'
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
CODEX_SUBAGENT_NOTICE = "<subagent_notification>"

# --- pi 0.73.0 ------------------------------------------------------------------------------------------------------------

PI_PATH_TOOLS = {"write": WRITE, "edit": EDIT}
PI_READ_TOOLS = frozenset({"read", "grep", "find", "ls"})
_PI_EXIT_LINE = re.compile(rf"Command exited with code ({_EXIT_DIGITS})")  # bash.js:268, the last line


@dataclass
class ToolCall:
    """One tool call, from its call record. `finished_at` is when it finished: at its answer, or, for a Claude Code
    job or a codex process that runs on after its answer, when the CLI's records end it. While that is not known, or
    later than a cut, the call is running at that cut."""

    call_id: str
    tool: str | None  # the tool's name as the call record gives it; None when that is not a string
    kind: str
    started_at: datetime
    line: int  # the call record's line in the copy (1-based)
    command: str | None = None  # a shell call's command, when the input holds it as a string
    target: str | None = None  # the path that a non-shell call's input names, when it is a string
    paths: tuple[str, ...] = ()  # the files a write or edit call writes, counted only if it does not fail
    answered_at: datetime | None = None
    answer_line: int | None = None
    output: str = ""  # the answer's text
    finished_at: datetime | None = None
    finish_line: int | None = None
    status: str | None = None  # OK, FAILED or UNKNOWN once finished
    exit_code: int | None = None
    job_id: str | None = None  # a Claude Code background job started at its answer
    job_kind: str | None = None  # SHELL_JOB or AGENT_JOB
    session: int | None = None  # a codex process still running after the answer, by its session id

    def finished_by(self, cut: datetime) -> bool:
        return self.finished_at is not None and self.finished_at <= cut

    def answered_by(self, cut: datetime) -> bool:
        return self.answered_at is not None and self.answered_at <= cut


@dataclass(frozen=True)
class Message:
    at: datetime
    text: str
    line: int


@dataclass(frozen=True)
class JobEnd:
    """A record that ends a Claude Code job: a TaskOutput answer with a terminal `task.status`, a successful TaskStop
    answer, or a `<task-notification>` in an enqueue or a queued_command attachment."""

    at: datetime
    line: int
    job_id: str
    status: str  # completed, failed or killed
    exit_code: int | None = None


@dataclass(frozen=True)
class LinesAtCut:
    """The transcript up to a cut (spec §9.6): its records and unreadable lines, and where its first record is."""

    entries: int
    unreadable: int
    # The time of its first record in file order, as an offset from the transcript's `started_at` (the digest's t+
    # offsets start there): times are only subtracted, and a time of its own could lie outside the range of `datetime`
    # in `started_at`'s offset from UTC.
    first: timedelta | None


_MICROSECOND = timedelta(microseconds=1)
# A record without a readable time, among the record offsets: below every offset, since two times in the range of
# `datetime` are less than 2**59 microseconds apart.
_NO_TIME = -(2**63)


@dataclass
class Transcript:
    cli: str
    calls: list[ToolCall] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    prompts: list[datetime] = field(default_factory=list)
    conclude_at: datetime | None = None  # the first prompt that holds the conclude line
    started_at: datetime | None = None  # the time of the first record with a readable one, in file order
    entries: int = 0  # records read, in the whole copy
    unreadable: int = 0  # non-empty lines that did not decode to an object, or whose handling raised (whole copy)
    job_ends: list[JobEnd] = field(default_factory=list)  # every job-end record read (Claude Code)
    # In file order, for `lines_at`: each record's time as whole microseconds after `started_at` (`_NO_TIME` for a
    # record without a readable one), packed as 8-byte integers, since a copy can hold about two million records; and
    # the unreadable lines in runs, as the number of records before each run and the run's length.
    record_offsets: array[int] = field(default_factory=lambda: array("q"), repr=False)
    unreadable_before: array[int] = field(default_factory=lambda: array("q"), repr=False)
    unreadable_runs: array[int] = field(default_factory=lambda: array("q"), repr=False)

    def add_record(self, at: datetime | None) -> None:
        """A record read, in file order, with its time (None when it has no readable one). Its offset is exact: a
        subtraction, never a conversion (§9.5)."""
        if at is None:
            self.record_offsets.append(_NO_TIME)
        else:
            if self.started_at is None:
                self.started_at = at
            self.record_offsets.append((at - self.started_at) // _MICROSECOND)
        self.entries += 1

    def add_unreadable(self) -> None:
        """An unreadable line, in file order, with its place among the records."""
        self.unreadable += 1
        records = len(self.record_offsets)
        if self.unreadable_before and self.unreadable_before[-1] == records:
            self.unreadable_runs[-1] += 1
        else:
            self.unreadable_before.append(records)
            self.unreadable_runs.append(1)

    def calls_by(self, cut: datetime) -> list[ToolCall]:
        """The calls started at or before the cut, in the order of their records."""
        return [call for call in self.calls if call.started_at <= cut]

    def running_at(self, cut: datetime) -> list[ToolCall]:
        """Spec §9.5: calls started with no finish by the cut (Claude Code jobs and codex processes included); a poll
        call never."""
        return [call for call in self.calls_by(cut) if call.kind != POLL and not call.finished_by(cut)]

    def lines_at(self, cut: datetime) -> LinesAtCut:
        """Spec §9.6: the entries and unreadable lines of the transcript up to the cut, and its first record's time.

        A record counts when its time is at or before the cut, as a call does (`calls_by`); a line that has no time of
        its own, an unreadable line or a record without a readable time, counts when it comes before the first record
        whose time is after the cut. So a replay that cuts a whole session file at its 75% point counts what a live copy
        taken then holds (§15.3), not the records the run wrote after the cut, and a copy that ends at its cut, as a
        live copy does, counts every line. The first record is the first one in file order whose time is at or before
        the cut: a record appended later with an earlier time does not move it.
        """
        offsets = self.record_offsets
        if self.started_at is None:  # no record has a readable time
            return LinesAtCut(len(offsets), self.unreadable, None)
        limit = (cut - self.started_at) // _MICROSECOND  # exact, like the offsets: whole microseconds after one time
        boundary = next((index for index, offset in enumerate(offsets) if offset > limit), len(offsets))
        entries = sum(
            1 for index, offset in enumerate(offsets) if (index < boundary if offset == _NO_TIME else offset <= limit)
        )
        unreadable = sum(
            run for before, run in zip(self.unreadable_before, self.unreadable_runs, strict=True) if before <= boundary
        )
        first = next((offset for offset in offsets if offset != _NO_TIME and offset <= limit), None)
        return LinesAtCut(entries, unreadable, None if first is None else first * _MICROSECOND)


# --- values -----------------------------------------------------------------------------------------------------------


def parse_time(value: object) -> datetime | None:
    """A record's `timestamp`: absent unless a string `fromisoformat` reads; without a zone, UTC (never converted)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def text_of(value: object) -> str:
    """A result's or a message's text: the value itself when it is a string; for a list of blocks, the `text` of each
    block that has a string one, joined with `\\n`; any other value gives empty text."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    texts: list[str] = []
    for block in value:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def _text_field(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _first_text_field(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _text_field(data, key)
        if value is not None:
            return value
    return None


def _integer(value: object) -> int | None:
    """An integer that is not a boolean, within 64 bits; anything else counts as absent."""
    if isinstance(value, int) and not isinstance(value, bool) and -_INT_LIMIT <= value < _INT_LIMIT:
        return value
    return None


# A piece that can decode to an object: `{` after JSON's whitespace (a piece holds no `\n`). Any other piece is counted
# as unreadable without being decoded, as json.loads would leave it: it raises, or gives a value that is no object.
_OBJECT_START = re.compile(r"[ \t\r]*\{")
_SPLIT_CHUNK = 1024 * 1024


def _pieces(data: bytes | str) -> Iterator[tuple[int, str]]:
    """(line number, piece) for every non-empty piece of the copy, split at `\\n` only: the pieces and numbers that
    `split("\\n")` gives, split a chunk of about 1 MiB at a time. A copy of the maximum size split at once into short
    pieces would hold them all in memory together (22 million objects for one of 2-byte lines)."""
    number, position = 1, 0  # the number of the piece that starts at `position`
    if isinstance(data, str):
        while position < len(data):
            end = data.find("\n", position + _SPLIT_CHUNK)  # a chunk ends at a line break, or at the end
            end = len(data) if end < 0 else end
            for piece in data[position:end].split("\n"):
                if piece:
                    yield number, piece
                number += 1
            position = end + 1
        return
    while position < len(data):
        end = data.find(b"\n", position + _SPLIT_CHUNK)
        end = len(data) if end < 0 else end
        for raw in data[position:end].split(b"\n"):
            if raw:
                yield number, raw.decode("utf-8", errors="replace")
            number += 1
        position = end + 1


# --- the readers ------------------------------------------------------------------------------------------------------


class _Reader:
    def __init__(self, cli: str) -> None:
        self.transcript = Transcript(cli=cli)
        self.calls: dict[str, ToolCall] = {}

    def read(self, data: bytes | str) -> Transcript:
        transcript = self.transcript
        for number, piece in _pieces(data):
            try:
                if _OBJECT_START.match(piece) is None:
                    # No object starts here: json.loads would raise or give another value, unreadable either way.
                    self.skip_line()
                    continue
                record = json.loads(piece)
                if not isinstance(record, dict):
                    self.skip_line()
                    continue
                at = parse_time(record.get("timestamp"))
                if at is not None:
                    self.handle(number, record, at)
            except (ValueError, RecursionError):
                self.skip_line()
                continue
            transcript.add_record(at)
        self.finish()
        return transcript

    def skip_line(self) -> None:
        """An unreadable line: counted, with its place among the records, and otherwise skipped."""
        self.transcript.add_unreadable()

    def handle(self, line: int, record: dict[str, Any], at: datetime) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        return

    # shared steps

    def add_call(self, call: ToolCall) -> bool:
        """Keep the first call record of an id; a later one with the same id is no new call."""
        if call.call_id in self.calls:
            return False
        self.calls[call.call_id] = call
        self.transcript.calls.append(call)
        return True

    def answer(self, call_id: str, line: int, at: datetime, text: str) -> ToolCall | None:
        """The call this result answers, now answered; None for an unknown id or a call answered before."""
        call = self.calls.get(call_id)
        if call is None or call.answered_at is not None:
            return None
        call.answered_at, call.answer_line, call.output = at, line, text
        return call

    @staticmethod
    def finish_call(call: ToolCall, line: int, at: datetime, status: str, exit_code: int | None = None) -> None:
        call.finished_at, call.finish_line, call.status, call.exit_code = at, line, status, exit_code

    def message(self, line: int, at: datetime, text: object) -> None:
        if isinstance(text, str) and text.strip():
            self.transcript.messages.append(Message(at, text, line))

    def prompt(self, at: datetime, text: str) -> None:
        transcript = self.transcript
        transcript.prompts.append(at)
        if transcript.conclude_at is None and CONCLUDE_LINE in text:
            transcript.conclude_at = at


class _ClaudeReader(_Reader):
    def __init__(self) -> None:
        super().__init__("claudecode")
        self.jobs: dict[str, list[ToolCall]] = {}

    def handle(self, line: int, record: dict[str, Any], at: datetime) -> None:
        kind = record.get("type")
        message = record.get("message")
        if kind == "assistant" and isinstance(message, dict) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    self.add_call(_claude_call(block, at, line))
                elif block.get("type") == "text":
                    self.message(line, at, block.get("text"))
        elif kind == "user" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                # A notification is no prompt (an agent's result and a job's last output are model and target text),
                # nor is a compaction summary, which the worker model writes from the conversation (cli.js:4531).
                if not content.startswith("<task-notification>") and record.get("isCompactSummary") is not True:
                    self.prompt(at, content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        use_id = block.get("tool_use_id")
                        if isinstance(use_id, str):
                            self.result(line, at, record, block, use_id)
        elif kind == "queue-operation":
            if record.get("operation") == "enqueue":
                self.notification(line, at, record.get("content"))
        elif kind == "attachment":
            attachment = record.get("attachment")
            if (
                isinstance(attachment, dict)
                and attachment.get("type") == "queued_command"
                and attachment.get("commandMode") == "task-notification"
            ):
                self.notification(line, at, attachment.get("prompt"))

    def result(self, line: int, at: datetime, record: dict[str, Any], block: dict[str, Any], use_id: str) -> None:
        call = self.answer(use_id, line, at, text_of(block.get("content")))
        if call is None:
            return
        failed = block.get("is_error") is True
        tool_result = record.get("toolUseResult")
        result = tool_result if isinstance(tool_result, dict) else None
        # A job starts at its answer's record, whatever the call's input: a string backgroundTaskId (a Bash job), or
        # status async_launched with a string agentId (an Agent job). Never from the answer's text.
        job_id, job_kind = _claude_job_start(result)
        if job_id is not None:
            call.job_id, call.job_kind = job_id, job_kind
            self.jobs.setdefault(job_id, []).append(call)
            return
        exit_code = None
        if call.tool == "Bash":
            if failed:
                exit_code = _claude_failure_exit(call.output)
            else:
                # Exit code 1 of grep, rg, find, diff, test or [ is no error to 2.1.98 (cli.js:2434).
                exit_code = 1 if result is not None and isinstance(result.get("returnCodeInterpretation"), str) else 0
        self.finish_call(call, line, at, FAILED if failed else OK, exit_code)
        if result is None:
            return
        if call.tool in CLAUDE_OUTPUT_TOOLS:
            task = result.get("task")
            if isinstance(task, dict):
                job, status = task.get("task_id"), task.get("status")
                if isinstance(job, str) and isinstance(status, str) and status in JOB_TERMINAL:
                    self.transcript.job_ends.append(JobEnd(at, line, job, status, _integer(task.get("exitCode"))))
        elif call.tool in CLAUDE_STOP_TOOLS and not failed:
            job = result.get("task_id")
            if isinstance(job, str):
                self.transcript.job_ends.append(JobEnd(at, line, job, "killed"))

    def notification(self, line: int, at: datetime, text: object) -> None:
        parsed = parse_task_notification(text)
        if parsed is not None:
            self.transcript.job_ends.append(JobEnd(at, line, parsed[0], parsed[1]))

    def finish(self) -> None:
        """A job ends at the first of its end records by time, matched by id over the whole copy, whatever the order of
        the two records in the file; an end record before the job's call ends nothing.

        The first end record not before a call is found by bisection: records written into the file can give one job
        id many calls and many end records, and a scan from the first end record for each call would take a time that
        grows with their product."""
        ends: dict[str, list[JobEnd]] = {}
        for end in self.transcript.job_ends:
            ends.setdefault(end.job_id, []).append(end)
        for job_id, calls in self.jobs.items():
            candidates = sorted(ends.get(job_id, ()), key=lambda end: (end.at, end.line))
            times = [end.at for end in candidates]
            for call in calls:
                index = bisect.bisect_left(times, call.started_at)  # the first end record whose time is not before it
                if index == len(candidates):
                    continue
                end = candidates[index]
                if end.status == "completed":
                    # 2.1.98 writes completed for a Bash job only at exit code 0; an Agent job has no exit code.
                    self.finish_call(call, end.line, end.at, OK, 0 if call.job_kind == SHELL_JOB else None)
                elif end.status == "failed":
                    self.finish_call(call, end.line, end.at, FAILED, end.exit_code)
                else:
                    self.finish_call(call, end.line, end.at, FAILED)


def _claude_call(block: dict[str, Any], at: datetime, line: int) -> ToolCall:
    name = block.get("name")
    tool = name if isinstance(name, str) else None
    data = block.get("input")
    args: dict[str, Any] = data if isinstance(data, dict) else {}
    call = ToolCall(call_id=block["id"], tool=tool, kind=OTHER, started_at=at, line=line)
    if tool == "Bash":
        call.kind, call.command = SHELL, _text_field(args, "command")
    elif tool is not None and tool in CLAUDE_PATH_TOOLS:
        call.kind, key = CLAUDE_PATH_TOOLS[tool]
        call.target = _text_field(args, key)
        call.paths = (call.target,) if call.target else ()
    elif tool is not None and tool in CLAUDE_POLL_TOOLS:
        call.kind = POLL
    else:
        call.kind = READ if tool is not None and tool in CLAUDE_READ_TOOLS else OTHER
        call.target = _first_text_field(args, ("file_path", "notebook_path", "path"))
    return call


def _claude_job_start(result: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if result is None:
        return None, None
    background = result.get("backgroundTaskId")
    if isinstance(background, str):
        return background, SHELL_JOB
    agent = result.get("agentId")
    if result.get("status") == "async_launched" and isinstance(agent, str):
        return agent, AGENT_JOB
    return None, None


def _claude_failure_exit(text: str) -> int | None:
    """A failed Bash call's exit code: N when the first line of its answer is exactly `Exit code N`, else none."""
    match = _CLAUDE_EXIT_LINE.fullmatch(text.split("\n", 1)[0])
    return _integer(int(match.group(1))) if match else None


def parse_task_notification(text: object) -> tuple[str, str] | None:
    """(job id, terminal status) from the header of Claude Code's own `<task-notification>`, or None.

    The first line must be exactly `<task-notification>` and the second exactly `<task-id>ID</task-id>`; the status is
    the first line that is exactly `<status>S</status>`, S completed, failed or killed, before the first line that is
    exactly `</task-notification>`, and nothing after that line is read: 2.1.98's notice that a job waits for input
    has no status, and after its closing line come the job's last output lines (cli.js:2419-2420).
    """
    if not isinstance(text, str) or not text.startswith("<task-notification>\n"):
        return None
    lines = text.split("\n")
    task = _TASK_ID_LINE.fullmatch(lines[1])
    if task is None:
        return None
    for line in lines[2:]:
        if line == "</task-notification>":
            return None
        status = _TASK_STATUS_LINE.fullmatch(line)
        if status is not None:
            return task.group(1), status.group(1)
    return None


class _CodexReader(_Reader):
    def __init__(self) -> None:
        super().__init__("codex")
        self.stdin_sessions: dict[str, int | None] = {}  # a write_stdin call's session_id argument
        self.patches: dict[str, str] = {}  # an apply_patch call's patch text
        self.running: dict[int, ToolCall] = {}  # exec_command processes still running, by session id
        # The line breaks of the cmd of the exec_command that started each session, counted once when it starts: every
        # write_stdin answer for the session repeats that cmd in its header, and records written into the file can
        # pair a long cmd with many polls.
        self.session_breaks: dict[int, int] = {}

    def handle(self, line: int, record: dict[str, Any], at: datetime) -> None:
        payload = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call", "local_shell_call"):
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                self.call(line, at, payload, kind, call_id)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                self.result(line, at, call_id, text_of(payload.get("output")))
        elif kind == "message":
            role = payload.get("role")
            if role == "assistant":
                self.message(line, at, text_of(payload.get("content")))
            elif role == "user":
                text = text_of(payload.get("content"))
                # A sub-agent's notification carries its final message, which is model text and can quote the target.
                if not text.startswith(CODEX_SUBAGENT_NOTICE):
                    self.prompt(at, text)

    def call(self, line: int, at: datetime, payload: dict[str, Any], kind: str, call_id: str) -> None:
        args: dict[str, Any] = {}
        if kind == "function_call":
            raw = payload.get("arguments")
            if isinstance(raw, str):
                # Inside the line's guard: an arguments string that does not decode makes the whole line unreadable.
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    args = decoded
        name = payload.get("name")
        tool = "local_shell" if kind == "local_shell_call" else (name if isinstance(name, str) else None)
        call = ToolCall(call_id=call_id, tool=tool, kind=OTHER, started_at=at, line=line)
        if tool is not None and tool in CODEX_SHELL_TOOLS:
            call.kind = SHELL
            call.command = _text_field(args, "cmd") if tool == "exec_command" else None
            if tool == "shell_command":
                call.command = _text_field(args, "command")
        elif tool is not None and tool in CODEX_POLL_TOOLS:
            call.kind = POLL
        elif tool == "apply_patch":
            call.kind = EDIT
            # The patch text is a custom_tool_call's input (the freeform apply_patch tool codex offers).
            patch = _text_field(payload, "input") if kind == "custom_tool_call" else None
            if patch is not None:
                call.target = " ".join(_PATCH_FILE.findall(patch)) or None
                self.patches.setdefault(call_id, patch)
        else:
            call.target = _first_text_field(args, ("file_path", "path"))
        if self.add_call(call) and call.kind == POLL:
            self.stdin_sessions[call_id] = _integer(args.get("session_id"))

    def result(self, line: int, at: datetime, call_id: str, text: str) -> None:
        call = self.answer(call_id, line, at, text)
        if call is None:
            return
        if call.tool == "write_stdin":
            self.poll_result(call, line, at, text)
        elif call.tool == "exec_command":
            breaks = call.command.count("\n") if call.command is not None else None
            header = codex_header(text, breaks) if breaks is not None else None
            if breaks is not None and header is not None and header.session is not None and header.exit_code is None:
                # Process running with session ID N: the call runs on, as session N, until a write_stdin answer for
                # session N says the process exited (or the run ends).
                call.session = header.session
                self.running[header.session] = call
                self.session_breaks[header.session] = breaks
            elif header is not None and header.exit_code is not None:
                code = header.exit_code
                self.finish_call(call, line, at, OK if code == 0 else FAILED, code)
            else:
                self.finish_call(call, line, at, UNKNOWN)
        else:
            self.other_result(call, line, at, text)

    def poll_result(self, call: ToolCall, line: int, at: datetime, text: str) -> None:
        """A write_stdin answer. The poll finishes at its answer; when its header (which repeats the `Command:` part
        of the exec_command that started the session) says that the session's process exited, that exec_command
        finishes here, a failure unless the code is 0. An answer for another session ends nothing."""
        session = self.stdin_sessions.get(call.call_id)
        header = process = None
        if session is not None:
            breaks = self.session_breaks.get(session)
            header = codex_header(text, breaks) if breaks is not None else None
            process = self.running.get(session)
        self.finish_call(call, line, at, OK if header is not None else UNKNOWN)
        if session is not None and process is not None and header is not None and header.exit_code is not None:
            code = header.exit_code
            self.finish_call(process, line, at, OK if code == 0 else FAILED, code)
            process.session = None
            del self.running[session]

    def other_result(self, call: ToolCall, line: int, at: datetime, text: str) -> None:
        """Every other codex tool has no header: it finishes at its answer with an unknown status, and its text is
        never read for one. Only an applied patch counts its files, from codex's own success text at its start."""
        self.finish_call(call, line, at, UNKNOWN)
        patch = self.patches.get(call.call_id)
        if patch is not None and text.startswith(CODEX_PATCH_APPLIED):
            call.paths = tuple(dict.fromkeys(_PATCH_FILE.findall(patch)))


@dataclass(frozen=True)
class CodexHeader:
    exit_code: int | None
    session: int | None


def codex_header(text: str, command_breaks: int) -> CodexHeader | None:
    """The header codex builds before `Output:` in an `exec_command` or `write_stdin` answer, or None if it cannot be
    read. The first line must begin with `Command: `; the command is shown with its own line breaks, so as many lines
    as the command has line breaks (`command_breaks`: the call's own `cmd`, or for a `write_stdin` answer the `cmd` of
    the `exec_command` that started its session) are passed over, without being split off; then every line up to the
    first one that is exactly `Output:` must have one of the header's shapes, the first a `Chunk ID: …` line."""
    if not text.startswith("Command: "):
        return None
    start = 0
    for _ in range(command_breaks + 1):  # past the Command line and the command's own line breaks
        start = text.find("\n", start) + 1
        if start == 0:
            return None
    lines = text[start:].split("\n", _CODEX_HEADER_MAX_LINES)
    if not lines[0].startswith("Chunk ID: "):
        return None
    exit_code = session = None
    for line in lines:
        if line == "Output:":
            return CodexHeader(exit_code, session)
        shape = _CODEX_HEADER_LINE.fullmatch(line)
        if shape is None:
            return None
        if shape.group(1) is not None:
            exit_code = _integer(int(shape.group(1)))
        if shape.group(2) is not None:
            session = _integer(int(shape.group(2)))
    return None


class _PiReader(_Reader):
    def __init__(self) -> None:
        super().__init__("pi")

    def handle(self, line: int, record: dict[str, Any], at: datetime) -> None:
        message = record.get("message")
        if record.get("type") != "message" or not isinstance(message, dict):
            return
        role = message.get("role")
        content = message.get("content")
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "toolCall" and isinstance(block.get("id"), str):
                    self.add_call(_pi_call(block, at, line))
                elif block.get("type") == "text":
                    self.message(line, at, block.get("text"))
        elif role == "toolResult":
            call_id = message.get("toolCallId")
            if not isinstance(call_id, str):
                return
            call = self.answer(call_id, line, at, text_of(content))
            if call is None:
                return
            # pi always sets isError (agent-loop.js:372-393); a successful call has exit code 0 whatever its text says.
            failed = message.get("isError") is True
            exit_code = None
            if call.tool == "bash":
                exit_code = _pi_failure_exit(call.output) if failed else 0
            self.finish_call(call, line, at, FAILED if failed else OK, exit_code)
        elif role == "user":
            self.prompt(at, text_of(content))


def _pi_call(block: dict[str, Any], at: datetime, line: int) -> ToolCall:
    name = block.get("name")
    tool = name if isinstance(name, str) else None
    data = block.get("arguments")
    args: dict[str, Any] = data if isinstance(data, dict) else {}
    call = ToolCall(call_id=block["id"], tool=tool, kind=OTHER, started_at=at, line=line)
    if tool == "bash":
        # A number, a boolean or null here is turned into text by pi before it runs the call, but the record keeps
        # the model's value: it counts as absent (spec §9.5).
        call.kind, call.command = SHELL, _text_field(args, "command")
    elif tool is not None and tool in PI_PATH_TOOLS:
        call.kind = PI_PATH_TOOLS[tool]
        call.target = _text_field(args, "path")
        call.paths = (call.target,) if call.target else ()
    else:
        call.kind = READ if tool is not None and tool in PI_READ_TOOLS else OTHER
        call.target = _text_field(args, "path")
    return call


def _pi_failure_exit(text: str) -> int | None:
    """A failed bash call's exit code: N when the last line of its text is exactly `Command exited with code N`."""
    match = _PI_EXIT_LINE.fullmatch(text.rstrip("\n").rsplit("\n", 1)[-1])
    return _integer(int(match.group(1))) if match else None


_READERS = {"claudecode": _ClaudeReader, "codex": _CodexReader, "pi": _PiReader}


def parse_transcript(cli: str, data: bytes | str) -> Transcript:
    """Read a transcript copy of the CLI type `cli` (from the run's `RunView`, never guessed for a live run)."""
    reader = _READERS.get(cli)
    if reader is None:
        raise ValueError("unknown CLI type")
    return reader().read(data)


def detect_cli(data: bytes | str, max_lines: int = 50) -> str | None:
    """Guess the CLI type from a file's first records, for eval-set files whose worker type is unknown (spec §15.3)."""
    for count, (_, piece) in enumerate(_pieces(data)):
        if count >= max_lines:
            break
        try:
            record = json.loads(piece)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "session_meta":
            return "codex"
        if kind == "session" and "version" in record:
            return "pi"
        if "sessionId" in record or kind in ("queue-operation", "user", "assistant", "summary"):
            return "claudecode"
    return None
