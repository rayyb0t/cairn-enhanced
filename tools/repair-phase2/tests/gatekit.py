"""Helpers for the gate tools' tests (spec §15, §16 GT1-GT11, GT13-GT15). No tests here.

- Session files in the shapes the pinned CLIs write (claude-code 2.1.98, codex 0.118.0, pi 0.73.0; the committed
  fixtures `cairn/tests/fixtures/repair_transcripts/` show each), built from a list of tool calls with made-up commands.
- Synthetic Phase 0 evaluation sets, made with the Phase 0 tool itself (`tools/repair-phase0/export_eval_set.py`) from a
  made-up dispatcher log and project, in container or local mode, with a session file copied into each intent folder as
  `transcript.md` asks.
- The repair agent's own files for the replay: `repair.yaml`, the agent token and the settings file the dispatcher
  writes, with reviewers on loopback stubs.
- `ModelStub`: a reviewer on loopback, in either wire format (`openai_chat` or `anthropic_messages`, owner decision O8),
  recording every request.

Every value is synthetic: `proj_027` is only a name, the targets are lab names, the keys are made up, and the model is
`test-model`. Nothing here imports the repair agent at module level, so the redaction test runs in the cairn
environment too.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
PHASE0_TOOLS = REPOSITORY / "tools" / "repair-phase0"
PROJECT = "proj_027"
MODEL = "test-model"
GUARDED = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NETRC",
    "SSLKEYLOGFILE",
)
T0 = datetime(2026, 11, 14, 10, 0, 5, tzinfo=UTC)
CONCLUDE_PROMPT = "# Conclude\nThis is the conclude phase.\nSummarise what the run confirmed."


def fake_secret(label: str, length: int = 40) -> str:
    """A made-up secret, long enough for every exact-value layer."""
    text = f"fake-{label}-" + "0123456789abcdef" * 4
    return text[:length]


def clear_guarded_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replay strips the proxy, CA and key-log variables from its environment before it builds a model client
    (§9.3 step 3). In-process, that is the test runner's environment: remove them through monkeypatch first, so the
    runner gets them back."""
    for name in list(os.environ):
        if name.upper() in GUARDED:
            monkeypatch.delenv(name, raising=False)


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


# --- session files ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """A shell call `at` seconds after the start, answered `done` seconds after it (None: never answered)."""

    at: float
    done: float | None
    command: str
    output: str = "ok"
    failed: bool = False
    code: int = 0


@dataclass(frozen=True)
class Session:
    cli: str
    calls: tuple[Call, ...]
    prompt: str = "# Task\nExplore the intent.\n## Current Intent\n```\ni001\n```"
    messages: tuple[tuple[float, str], ...] = ()
    conclude_at: float | None = None
    session_id: str = "01a1dafd-0000-7000-8000-000000000001"
    start: datetime = T0

    def records(self) -> list[dict[str, Any]]:
        builder = {"claudecode": _claude_records, "codex": _codex_records, "pi": _pi_records}[self.cli]
        return builder(self)

    def data(self) -> bytes:
        return dump(self.records())


def dump(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n" for record in records)


def _ordered(items: list[tuple[float, int, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [record for _, _, record in sorted(items, key=lambda item: (item[0], item[1]))]


def _claude_records(session: Session) -> list[dict[str, Any]]:
    at = session.start
    items: list[tuple[float, int, dict[str, Any]]] = []

    def add(offset: float, record: dict[str, Any]) -> None:
        record["timestamp"] = iso(at + timedelta(seconds=offset))
        record["sessionId"] = session.session_id
        items.append((offset, len(items), record))

    add(0, {"type": "user", "message": {"role": "user", "content": session.prompt}})
    for number, call in enumerate(session.calls):
        call_id = f"toolu_{number:04d}"
        add(
            call.at,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": MODEL,
                    "content": [
                        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": call.command}}
                    ],
                },
            },
        )
        if call.done is None:
            continue
        text = f"Exit code {call.code}\n{call.output}" if call.failed else call.output
        add(
            call.done,
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": call.failed}
                    ],
                },
                "toolUseResult": {"stdout": call.output, "stderr": "", "interrupted": False},
            },
        )
    for offset, text in session.messages:
        add(
            offset,
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": MODEL, "content": [{"type": "text", "text": text}]},
            },
        )
    if session.conclude_at is not None:
        add(session.conclude_at, {"type": "user", "message": {"role": "user", "content": CONCLUDE_PROMPT}})
    return _ordered(items)


def _codex_records(session: Session) -> list[dict[str, Any]]:
    at = session.start
    items: list[tuple[float, int, dict[str, Any]]] = []

    def add(offset: float, record: dict[str, Any]) -> None:
        items.append((offset, len(items), {"timestamp": iso(at + timedelta(seconds=offset)), **record}))

    add(
        0,
        {
            "type": "session_meta",
            "payload": {"id": session.session_id, "cwd": "/home/kali/workspace", "cli_version": "0.118.0"},
        },
    )
    add(
        0,
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": session.prompt}]},
        },
    )
    for number, call in enumerate(session.calls):
        call_id = f"call_{number:04d}"
        add(
            call.at,
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": call.command}),
                    "call_id": call_id,
                },
            },
        )
        if call.done is None:
            continue
        code = call.code if call.failed else 0
        header = (
            f"Command: /bin/bash -lc '{call.command}'\nChunk ID: c9a4fb\nWall time: {call.done - call.at:.4f} seconds\n"
            f"Process exited with code {code}\nOriginal token count: 4\nOutput:\n{call.output}\n"
        )
        add(
            call.done,
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": call_id, "output": header},
            },
        )
    for offset, text in session.messages:
        add(
            offset,
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
            },
        )
    if session.conclude_at is not None:
        add(
            session.conclude_at,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": CONCLUDE_PROMPT}],
                },
            },
        )
    return _ordered(items)


def _pi_records(session: Session) -> list[dict[str, Any]]:
    at = session.start
    items: list[tuple[float, int, dict[str, Any]]] = []

    def add(offset: float, record: dict[str, Any]) -> None:
        items.append((offset, len(items), {**record, "timestamp": iso(at + timedelta(seconds=offset))}))

    add(0, {"type": "session", "version": 3, "id": session.session_id, "cwd": "/home/kali/workspace"})
    add(
        0,
        {
            "type": "message",
            "id": "m0",
            "message": {"role": "user", "content": [{"type": "text", "text": session.prompt}]},
        },
    )
    for number, call in enumerate(session.calls):
        call_id = f"call_{number:04d}"
        add(
            call.at,
            {
                "type": "message",
                "id": f"a{number}",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": call_id, "name": "bash", "arguments": {"command": call.command}}
                    ],
                    "model": MODEL,
                },
            },
        )
        if call.done is None:
            continue
        text = f"{call.output}\n\nCommand exited with code {call.code}" if call.failed else call.output
        add(
            call.done,
            {
                "type": "message",
                "id": f"r{number}",
                "message": {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "toolName": "bash",
                    "content": [{"type": "text", "text": text}],
                    "isError": call.failed,
                },
            },
        )
    for offset, text in session.messages:
        add(
            offset,
            {
                "type": "message",
                "id": f"t{offset}",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "model": MODEL},
            },
        )
    if session.conclude_at is not None:
        add(
            session.conclude_at,
            {
                "type": "message",
                "id": "c",
                "message": {"role": "user", "content": [{"type": "text", "text": CONCLUDE_PROMPT}]},
            },
        )
    return _ordered(items)


def made_up_session_id(cli: str, intent: str, kind: str) -> str:
    """A UUID-shaped session id, the same on every run for the same CLI, intent and kind."""
    digits = hashlib.sha256(f"{cli}/{intent}".encode()).hexdigest()[:12]
    return f"01a1dafd-0000-7000-{kind}000-{digits}"


def progressing_session(cli: str, budget: int, *, intent: str = "i001", session_id: str | None = None) -> Session:
    """A run that keeps finding new things up to its review point, with a long local job running at the cut."""
    cut = 0.75 * budget
    calls = [
        Call(10, 15, "binwalk -e lab-firmware.bin", "extracted 3 partitions"),
        Call(0.3 * budget, 0.3 * budget + 20, "ls _lab-firmware.bin.extracted", "squashfs-root\nkernel.img"),
        Call(0.6 * budget, 0.6 * budget + 5, "find squashfs-root/etc -name '*.conf'", "lab.conf\nnet.conf"),
        Call(0.7 * budget, cut + 120, "unsquashfs -d rootfs part2.squashfs", "unpacked 1840 files"),
    ]
    return Session(
        cli,
        tuple(calls),
        prompt=f"# Task\nExplore the intent.\n## Current Intent\n```\n{intent}\n```",
        messages=((0.62 * budget, "Found two configuration files; unpacking the second partition next."),),
        conclude_at=budget + 5,
        session_id=session_id or made_up_session_id(cli, intent, "8"),
    )


def stuck_session(cli: str, budget: int, *, intent: str = "i002", session_id: str | None = None) -> Session:
    """A run that repeats the same failing request up to its review point."""
    calls = [
        Call(
            i * 0.07 * budget,
            i * 0.07 * budget + 30,
            "curl -s http://lab-target.test:8080/admin",
            "connection timed out",
            True,
            28,
        )
        for i in range(1, 10)
    ]
    return Session(
        cli,
        tuple(calls),
        prompt=f"# Task\nExplore the intent.\n## Current Intent\n```\n{intent}\n```",
        messages=((0.66 * budget, "Retrying the admin page again."),),
        conclude_at=budget + 5,
        session_id=session_id or made_up_session_id(cli, intent, "9"),
    )


# --- Phase 0 evaluation sets ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    intent_id: str
    cli: str
    budget: int
    label: str = "progressing"
    session: Session | None = None
    description: str = "Map the lab service"
    log_budget: bool = True  # False: the log has no `starting container exec` line for the run

    def session_file(self) -> Session:
        if self.session is not None:
            return self.session
        build = progressing_session if self.label != "stuck" else stuck_session
        return build(self.cli, self.budget, intent=self.intent_id)


WORKERS = {"claudecode": "claude-a", "codex": "codex-b", "pi": "pi-c"}


def dispatcher_log(cases: Sequence[EvalCase], *, mode: str, project: str = PROJECT) -> str:
    """A made-up dispatcher log of timed-out explore runs, one per case, in the format Phase 0 parses."""
    container = f"cairn-dispatch-{project}" if mode == "container" else f"/home/lab/cairn-work/{project}"
    start = datetime(2026, 11, 14, 18, 0, 0)
    lines: list[str] = []

    def line(at: datetime, level: str, logger: str, message: str) -> None:
        lines.append(f"[{at:%Y-%m-%d %H:%M:%S}] {level} {logger} {message}")

    for number, case in enumerate(cases):
        at = start + timedelta(hours=number)
        worker = WORKERS[case.cli]
        line(
            at,
            "INFO",
            "scheduler.loop",
            f"dispatched explore project={project} intent={case.intent_id} worker={worker}",
        )
        if case.log_budget:
            line(
                at + timedelta(seconds=1),
                "INFO",
                "tasks.common",
                f"starting container exec container={container} worker={worker} phase=explore_execute timeout={case.budget}s",
            )
        end = at + timedelta(seconds=case.budget + 1)
        line(
            end,
            "WARNING",
            "tasks.explore",
            f"explore timed out project={project} intent={case.intent_id} worker={worker} "
            f"execute_ms={case.budget * 1000 + 50} total_ms={case.budget * 1000 + 90} stdout_preview= stderr_preview=",
        )
        line(
            end,
            "INFO",
            "tasks.explore",
            f"starting conclude fallback project={project} intent={case.intent_id} worker={worker}",
        )
        line(
            end,
            "INFO",
            "tasks.common",
            f"starting container exec container={container} worker={worker} phase=explore_conclude timeout=120s",
        )
        line(
            end + timedelta(seconds=30),
            "INFO",
            "tasks.common",
            f"intent concluded project={project} intent={case.intent_id} worker={worker} source=explore_conclude phase_ms=30000",
        )
    return "\n".join(lines) + "\n"


def project_document(cases: Sequence[EvalCase], *, project: str = PROJECT) -> dict[str, Any]:
    """A made-up `GET /projects/{id}` answer holding the cases' intents and a few facts."""
    intents = [
        {
            "id": case.intent_id,
            "from": ["f001"],
            "to": None,
            "description": case.description,
            "creator": "reason",
            "created_at": "2026-11-14T09:59:00Z",
            "concluded_at": None,
        }
        for case in cases
    ]
    intents.append(
        {
            "id": "i900",
            "from": ["origin"],
            "to": "f002",
            "description": "Read the lab banner",
            "creator": "reason",
            "created_at": "2026-11-14T09:59:00Z",
            "concluded_at": iso(T0 + timedelta(seconds=60)),
        }
    )
    return {
        "project": {"id": project, "title": "Lab", "status": "active", "bootstrap_enabled": False},
        "facts": [
            {"id": "origin", "description": "A lab VM runs a small web service."},
            {"id": "f001", "description": "The lab firmware image is lab-firmware.bin."},
            {"id": "f002", "description": "The lab banner names version 1.2."},
        ],
        "intents": intents,
        "hints": [],
    }


def phase0_tools() -> Any:
    """`tools/repair-phase0/export_eval_set.py`, imported from its folder as its own tests import it."""
    if str(PHASE0_TOOLS) not in sys.path:
        sys.path.insert(0, str(PHASE0_TOOLS))
    import export_eval_set

    return export_eval_set


def write_eval_set(
    root: Path,
    cases: Sequence[EvalCase],
    *,
    mode: str = "container",
    project: str = PROJECT,
    session_name: Callable[[EvalCase], str] | None = None,
) -> Path:
    """An eval set as Phase 0 exports it (`export_eval_set.export`), with each case's session file copied into its
    intent folder, `project.json` saved beside it and `labels.csv` filled in."""
    export_eval_set = phase0_tools()
    log = root / "dispatcher.log"
    log.write_text(dispatcher_log(cases, mode=mode, project=project), encoding="utf-8")
    document = project_document(cases, project=project)
    out = root / f"eval-{project}"
    export_eval_set.export(
        project,
        [str(log)],
        document,
        out,
        execution=mode,
        worker_types={worker: cli for cli, worker in WORKERS.items()},
    )
    for case in cases:
        session = case.session_file()
        name = session_name(case) if session_name is not None else f"{session.session_id}.jsonl"
        (out / case.intent_id / name).write_bytes(session.data())
    (out / "project.json").write_text(json.dumps(document), encoding="utf-8")
    write_labels(
        out / "labels.csv", [(case.intent_id, case.label, f"made-up evidence for {case.intent_id}") for case in cases]
    )
    return out


def write_labels(path: Path, rows: Sequence[tuple[str, str, str]]) -> Path:
    text = "intent_id,label,evidence,notes\n" + "".join(
        f"{intent},{label},{evidence},\n" for intent, label, evidence in rows
    )
    path.write_text(text, encoding="utf-8")
    return path


def mark_redacted(eval_dir: Path) -> None:
    """Rename each intent folder's one session file to the replay's redacted name, with the redaction.json that
    `redact_eval_set.py` writes, for tests of the replay that do not run the redaction (which needs the cairn side)."""
    for folder in sorted(path for path in eval_dir.iterdir() if (path / "intent.json").is_file()):
        sources = sorted(path for path in folder.iterdir() if path.suffix == ".jsonl")
        assert len(sources) == 1, folder
        source = sources[0]
        info = source.stat()
        target = folder / "transcript.redacted.jsonl"
        target.write_bytes(source.read_bytes())
        report = {
            "version": 2,  # redact_eval_set.py's REPORT_VERSION: the replay refuses an earlier step 0's report
            "files": [
                {
                    "source": source.name,
                    "source_bytes": info.st_size,
                    "source_mtime_ns": info.st_mtime_ns,
                    "output": target.name,
                    "redactions": {},
                }
            ],
            "redactions": {},
        }
        (folder / "redaction.json").write_text(json.dumps(report), encoding="utf-8")


# --- the agent's files ------------------------------------------------------------------------------------------------


def reviewer_entry(name: str, base_url: str, **settings: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "model": MODEL, "base_url": base_url, "api_key": None}
    entry.update(settings)
    return entry


@dataclass
class AgentFiles:
    config: Path
    root: Path
    token: str = field(repr=False)


def write_agent_files(
    base: Path, reviewers: Sequence[Mapping[str, Any]] | None, *, poll_interval: int = 5, digest_tokens: int = 8000
) -> AgentFiles:
    """`repair.yaml`, the agent token and the copy root with the settings file the dispatcher writes (§9.2); without
    `reviewers`, no settings file."""
    base.mkdir(parents=True, exist_ok=True)
    root = base / "transcripts"
    root.mkdir()
    token = fake_secret("agent-token", 48)
    token_file = base / "agent.token"
    token_file.write_text(token + "\n", encoding="ascii")
    token_file.chmod(0o600)
    if reviewers is not None:
        settings = {"version": 1, "server": "http://127.0.0.1:8000", "projects": None, "reviewers": list(reviewers)}
        (root / "agent-settings.json").write_text(json.dumps(settings), encoding="utf-8")
    config = base / "repair.yaml"
    config.write_text(
        f'token_file: "{token_file}"\n'
        "dispatch_config_to_check: null\n"
        f"poll_interval: {poll_interval}\n"
        "transcripts:\n"
        f'  root: "{root}"\n'
        "digest:\n"
        f"  max_tokens_estimate: {digest_tokens}\n",
        encoding="utf-8",
    )
    return AgentFiles(config=config, root=root, token=token)


def dispatch_yaml(
    *,
    execution: str,
    reviewer_key: str | None = None,
    worker_env: Mapping[str, str] | None = None,
    second_reviewer_key: str | None = None,
) -> str:
    """A made-up dispatch.yaml with one claudecode worker and one reviewer (spec §8.3's repair: section); with
    `second_reviewer_key`, a second reviewer, `r2`, in the anthropic_messages format with that key of its own (O8)."""
    if worker_env is None:
        worker_env = (
            {}
            if execution == "local"
            else {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/",
                "ANTHROPIC_AUTH_TOKEN": fake_secret("worker-token"),
                "ANTHROPIC_MODEL": MODEL,
            }
        )
    env = dict(worker_env)
    env_lines = "".join(f'      {name}: "{value}"\n' for name, value in env.items())
    runtime_extra = "  worker_healthcheck: disabled\n" if execution == "local" else ""
    container = (
        'container:\n  image: "cairn-worker:test"\n  network_mode: "bridge"\n  completed_action: "stop"\n'
        if execution == "container"
        else "local:\n  completed_action: keep\n"
    )
    key_line = f'        REVIEW_API_KEY: "{reviewer_key}"\n' if reviewer_key else ""
    second = (
        '    - name: "r2"\n'
        "      api_format: anthropic_messages\n"
        "      env:\n"
        '        REVIEW_MODEL: "test-model"\n'
        '        REVIEW_BASE_URL: "http://127.0.0.1:9"\n'
        f'        REVIEW_API_KEY: "{second_reviewer_key}"\n'
        if second_reviewer_key
        else ""
    )
    return (
        'server: "http://127.0.0.1:8000"\n'
        "runtime:\n"
        f"  execution: {execution}\n"
        f"{runtime_extra}"
        "  max_workers: 1\n  max_running_projects: 1\n  max_project_workers: 1\n  interval: 3\n"
        "  healthcheck_timeout: 5\n  prompt_group: default\n"
        "tasks:\n  bootstrap:\n    timeout: 120\n    conclude_timeout: 30\n  reason:\n    timeout: 45\n"
        "  explore:\n    timeout: 1800\n    conclude_timeout: 120\n"
        f"{container}"
        "workers:\n"
        '  - name: "claude-a"\n    type: "claudecode"\n    task_types: [explore]\n    max_running: 1\n    priority: 0\n'
        + ("    env:\n" + env_lines if env_lines else "")
        + "repair:\n"
        '  token_file: "/nonexistent/repair-dispatcher.token"\n'
        '  transcript_root: "/nonexistent/repair-transcripts"\n'
        "  reviewers:\n"
        '    - name: "r1"\n'
        "      env:\n"
        '        REVIEW_MODEL: "test-model"\n'
        '        REVIEW_BASE_URL: "http://127.0.0.1:11434/v1"\n'
        f"{key_line}"
        f"{second}"
    )


# --- reviewers on loopback --------------------------------------------------------------------------------------------


def decision_text(
    decision: str, reason: str = "made-up reason", evidence: Sequence[str] = (), next_step: str = ""
) -> str:
    return json.dumps({"decision": decision, "reason": reason, "evidence": list(evidence), "next_step": next_step})


def openai_answer(text: str, *, finish: str = "stop", usage: Any = None, model: str = MODEL) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100000, "completion_tokens": 50} if usage is None else usage,
    }


def anthropic_answer(text: str, *, stop: str = "end_turn", usage: Any = None, model: str = MODEL) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 100000, "output_tokens": 50} if usage is None else usage,
    }


@dataclass(frozen=True)
class Seen:
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)

    def digest(self) -> str:
        """The digest the request carries: the user message of either format."""
        body = self.json()
        messages = body["messages"]
        return messages[-1]["content"]


Answer = Callable[[Seen], tuple[int, Mapping[str, Any]]]


class ModelStub:
    """A reviewer endpoint on loopback: `openai_chat` (`POST /v1/chat/completions` under a base_url ending in /v1) or
    `anthropic_messages` (`POST /v1/messages` under a base_url without /v1). `answer` builds each reply from the
    request; every request is recorded."""

    def __init__(self, api_format: str, answer: Answer) -> None:
        self.api_format = api_format
        self.answer = answer
        self.seen: list[Seen] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                seen = Seen(self.path, {name.lower(): value for name, value in self.headers.items()}, body)
                with stub.lock:
                    stub.seen.append(seen)
                status, reply = stub.answer(seen)
                data = json.dumps(reply).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="model-stub", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/v1" if self.api_format == "openai_chat" else f"http://127.0.0.1:{port}"

    def requests(self) -> list[Seen]:
        with self.lock:
            return list(self.seen)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def stub_answer(api_format: str, text_for: Callable[[str], str], **reply: Any) -> Answer:
    """An answer built from the request's digest, in the stub's format."""

    def answer(seen: Seen) -> tuple[int, Mapping[str, Any]]:
        text = text_for(seen.digest())
        if api_format == "anthropic_messages":
            return 200, anthropic_answer(text, **reply)
        return 200, openai_answer(text, **reply)

    return answer


def continue_answer(_: str) -> str:
    return decision_text("continue", "made-up: nothing new lately")


def model_stub_factory() -> Iterator[Callable[..., ModelStub]]:
    """For a test module's `model_stubs` fixture: a maker of stubs, each closed when the test ends."""
    made: list[ModelStub] = []

    def make(api_format: str = "openai_chat", answer: Answer | None = None, **reply: Any) -> ModelStub:
        stub = ModelStub(api_format, answer or stub_answer(api_format, continue_answer, **reply))
        made.append(stub)
        return stub

    yield make
    for stub in made:
        stub.close()


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
