"""Helpers for the transcript and digest tests: the pinned CLI fixtures, in-memory edits of their records, and records
in the shapes the pinned CLIs write.

The fixtures are `cairn/tests/fixtures/repair_transcripts/` (their README gives each job's lines). Negative cases are
in-memory edits of those records, never new fixture files (build plan step 9): an edit changes, adds or moves a few
records in a shape that a probe of the pinned CLI wrote, or that only bytes written into the file have. Everything here
is synthetic.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cairn_repair_agent import transcripts
from cairn_repair_agent.digest import Digest, ReviewContext, build_digest
from cairn_repair_agent.redact import NO_SECRETS, ExactSecrets
from cairn_repair_agent.transcripts import Transcript

REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURES = REPOSITORY / "cairn" / "tests" / "fixtures" / "repair_transcripts"
GOLDEN_COPY_PROJECT = REPOSITORY / "cairn" / "tests" / "fixtures" / "repair_copy" / "proj_027"

CLAUDE = "claudecode-2.1.98.jsonl"
CODEX = "codex-0.118.0.jsonl"
PI = "pi-0.73.0.jsonl"
BACKGROUND = "claudecode-2.1.98-background.jsonl"
AUTOBACKGROUND = "claudecode-2.1.98-autobackground.jsonl"
AGENT_BACKGROUND = "claudecode-2.1.98-agent-background.jsonl"
NOTICE_IN_OUTPUT = "claudecode-2.1.98-notice-in-output.jsonl"
ALL_FILES = (CLAUDE, BACKGROUND, AUTOBACKGROUND, AGENT_BACKGROUND, NOTICE_IN_OUTPUT, CODEX, PI)
CLI_OF = {
    name: ("codex" if name.startswith("codex") else "pi" if name.startswith("pi") else "claudecode")
    for name in ALL_FILES
}

# Jobs of the Claude Code job files (their README).
JOB_A, JOB_B, JOB_C = "bto947nq7", "bj77lt3s0", "bvzmt1l5o"
JOB_D, JOB_E = "bwdp3a0ve", "bit9nr99m"
JOB_G = "ab6626ce4932c0654"
JOB_R = "baw3i5flv"

# Line separators that the pinned CLIs write raw inside JSON strings (spec §9.5).
LINE_SEPARATOR = chr(0x2028)
PARAGRAPH_SEPARATOR = chr(0x2029)
NEXT_LINE = chr(0x0085)

RUN_ID = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"
NONCE = "0123456789abcdef"

Record = dict[str, Any]


def T(text: str) -> datetime:  # noqa: N802  # a short name for a time, as the spec writes it
    """A time as the spec writes it, such as `03:45:16.406Z` on a fixture's day, or a full ISO time."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load(name: str) -> list[Record]:
    """The records of a fixture file, one per line; record `i` is line `i + 1`."""
    records = [json.loads(piece) for piece in fixture_bytes(name).split(b"\n") if piece]
    assert all(isinstance(record, dict) for record in records)
    return records


def dump(records: list[Any]) -> bytes:
    """Records as a session file: one JSON line each, U+2028, U+2029 and U+0085 written raw, as the CLIs write them."""
    return b"".join(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n" for record in records)


def edit(records: list[Record], line: int, change: Callable[[Record], object]) -> list[Record]:
    """A deep copy of `records` with `change` applied to the record of line `line` (1-based)."""
    edited = copy.deepcopy(records)
    change(edited[line - 1])
    return edited


def parse(name: str, data: bytes | None = None) -> Transcript:
    return transcripts.parse_transcript(CLI_OF[name], fixture_bytes(name) if data is None else data)


def parse_records(name: str, records: list[Any]) -> Transcript:
    return transcripts.parse_transcript(CLI_OF[name], dump(records))


def by_id(transcript: Transcript) -> dict[str, transcripts.ToolCall]:
    return {call.call_id: call for call in transcript.calls}


def running_ids(transcript: Transcript, cut: datetime) -> list[str]:
    return [call.call_id for call in transcript.running_at(cut)]


def last_time(records: list[Record]) -> datetime:
    times = [transcripts.parse_time(record.get("timestamp")) for record in records]
    return max(time for time in times if time is not None)


def context(cli: str = "claudecode", **changes: Any) -> ReviewContext:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "cli_type": cli,
        "window_number": 1,
        "window_seconds": 1800,
        "budget_seconds": 1800,
        "elapsed_seconds": 1361,
        "remaining_seconds": 439,
        "slices_used": 0,
        "slice_seconds": 600,
        "copy_age_seconds": 9,
    }
    values.update(changes)
    return ReviewContext(**values)


def digest_of(
    transcript: Transcript,
    cut: datetime,
    *,
    intent: str = "List the configuration files of the extracted firmware.",
    exact: ExactSecrets = NO_SECRETS,
    budget: int = 8000,
    **context_changes: Any,
) -> Digest:
    return build_digest(
        transcript,
        context(transcript.cli, **context_changes),
        cut,
        intent=intent,
        source_facts=[],
        new_facts=[],
        exact=exact,
        budget=budget,
        nonce=NONCE,
    )


def fence_body(digest: Digest) -> str:
    """The text inside the fence."""
    opening, closing = f'<untrusted_data id="{digest.nonce}">', f'</untrusted_data id="{digest.nonce}">'
    return digest.text.split(opening, 1)[1].rsplit(closing, 1)[0]


def header_of(digest: Digest) -> str:
    """The text before the fence."""
    return digest.text.split(f'<untrusted_data id="{digest.nonce}">', 1)[0]


def call_line(digest: Digest, call_id: str) -> str:
    """The line of `E<n>` in the last-calls list."""
    body = fence_body(digest).split("TOOL CALLS (oldest first):", 1)[1]
    lines = [line for line in body.split("\n") if line.startswith(call_id + " ")]
    assert len(lines) == 1, (call_id, lines)
    return lines[0]


def section(digest: Digest, heading: str) -> list[str]:
    """The lines of a section of the fence, from its heading to the next heading in capitals."""
    lines = fence_body(digest).split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    found = [lines[start]]
    for line in lines[start + 1 :]:
        if line[:1].isupper() and line.split(" ", 1)[0].isupper() and not line.startswith(("E", "M")):
            break
        found.append(line)
    return found


# --- records in the shapes the pinned CLIs write ----------------------------------------------------------------------


def claude_assistant_tool_use(call_id: str, name: str, tool_input: Any, at: str, session: str = "s") -> Record:
    return {
        "type": "assistant",
        "timestamp": at,
        "sessionId": session,
        "message": {
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
        },
    }


def claude_tool_result(
    call_id: str, content: Any, at: str, *, is_error: bool | None = False, tool_use_result: Any = None
) -> Record:
    block: Record = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    record: Record = {"type": "user", "timestamp": at, "message": {"role": "user", "content": [block]}}
    if tool_use_result is not None:
        record["toolUseResult"] = tool_use_result
    return record


def claude_text(text: str, at: str) -> Record:
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def claude_prompt(text: str, at: str) -> Record:
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def claude_enqueue(content: str, at: str) -> Record:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": at, "sessionId": "s", "content": content}


def task_notification(job: str, status: str, tool_use_id: str = "toolu_fixture_0", summary: str = "done") -> str:
    """A `<task-notification>` in the layout 2.1.98 writes (a fixture's line 12)."""
    return (
        f"<task-notification>\n<task-id>{job}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<output-file>/tmp/claude-1000/-home-kali-workspace/s/tasks/{job}.output</output-file>\n"
        f"<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>"
    )


def codex_call(call_id: str, name: str, arguments: Any, at: str) -> Record:
    payload: Record = {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}
    return {"timestamp": at, "type": "response_item", "payload": payload}


def codex_output(call_id: str, output: Any, at: str) -> Record:
    return {
        "timestamp": at,
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def codex_header(command_shown: str, status_line: str, output: str, chunk: str = "c9a4fb", tokens: int = 4) -> str:
    """The answer codex 0.118.0 builds for an exec_command or write_stdin call (codex-0.118.0.jsonl:11)."""
    return (
        f"Command: {command_shown}\nChunk ID: {chunk}\nWall time: 0.5003 seconds\n{status_line}\n"
        f"Original token count: {tokens}\nOutput:\n{output}"
    )


def pi_tool_call(call_id: str, name: str, arguments: Any, at: str) -> Record:
    return {
        "type": "message",
        "id": call_id[-8:],
        "timestamp": at,
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}],
        },
    }


def pi_tool_result(call_id: str, name: str, text: str, at: str, *, is_error: bool) -> Record:
    return {
        "type": "message",
        "id": call_id[-8:] + "r",
        "timestamp": at,
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": name,
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


def after(at: datetime, seconds: float) -> datetime:
    return at + timedelta(seconds=seconds)


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
