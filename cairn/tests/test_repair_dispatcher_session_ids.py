"""Session ids (spec 8.7): the one id rule, Claude Code's argv seed, the output probe and the end-of-run parse.

A session id goes into a request body, a path or a log line only if it passes the rule, matched in full. These
tests use the real drivers (workers/adapters) and fake processes that carry the real process classes' buffer
attributes. DS12, which sends hostile ids through every place an id comes from and through the client, is in
test_repair_shadow.py.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from repair_dispatcher_support import (
    IDS,
    PROBE_FAILED_LINE,
    RUN_ID,
    VALID_ID,
    FakeProcess,
    repair_records,
    reset_repair_process_state,
    worker_config,
)

from cairn.dispatcher.repair.session_ids import (
    PROBE_MAX_CHARS,
    OutputSessionProbe,
    accept_session_id,
    argv_session_seed,
    complete_lines,
    valid_project_id,
    valid_run_id,
    valid_session_id,
)
from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, MockDriver, PiDriver


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    reset_repair_process_state(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield


@pytest.mark.parametrize(
    "value",
    [VALID_ID, "a", "A1", "x" * 128, "a.b_c-d", "0199a1b2", "sess.1"],
)
def test_session_id_rule_accepts_ids_of_the_pinned_clis(value: str) -> None:
    assert valid_session_id(value)
    assert valid_project_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".hidden",
        "-x",
        "_x",
        "a..b",
        "..",
        "a/b",
        "a\\b",
        "a b",
        "a\nb",
        "a\n",
        "\na",
        "x" * 129,
        "é",
        None,
        7,
        b"ab",
    ],
)
def test_session_id_rule_is_a_full_match_without_dot_dot(value: Any) -> None:
    assert not valid_session_id(value)
    assert not valid_project_id(value)
    assert accept_session_id(value, **IDS) is None


def test_run_id_rule_is_the_servers_full_match() -> None:
    assert valid_run_id(RUN_ID)
    assert valid_run_id("a" * 8) and valid_run_id("a" * 64)
    for value in ("a" * 7, "a" * 65, RUN_ID + "\n", "../" + RUN_ID, "-" + RUN_ID[1:], None):
        assert not valid_run_id(value)


def test_argv_seed_is_claude_codes_session_id_before_the_prompt() -> None:
    worker = worker_config("claudecode")
    driver = ClaudeCodeDriver()
    session = driver.prepare_session()
    assert session is not None
    argv = driver.build_execute(worker, "prompt --session-id forged", session).argv
    assert argv_session_seed("claudecode", argv) == session
    assert accept_session_id(argv_session_seed("claudecode", argv), **IDS) == session
    # The prompt follows "--", so a --session-id inside it is never read.
    assert argv_session_seed("claudecode", ["claude", "-p", "--", "--session-id", "forged"]) is None
    assert argv_session_seed("claudecode", ["claude", "--session-id"]) is None
    assert argv_session_seed("claudecode", ["claude", "--session-id", "--"]) is None
    for cli, other in (("codex", CodexDriver(local=True)), ("pi", PiDriver(local=True)), ("mock", MockDriver())):
        assert argv_session_seed(cli, other.build_execute(worker_config("mock"), "prompt", session).argv) is None
    assert argv_session_seed("claudecode", "claude --session-id x") is None


@pytest.mark.parametrize(("cli", "local"), [("codex", False), ("codex", True), ("pi", False), ("pi", True)])
def test_probe_reads_whole_lines_of_the_stream_its_driver_reads(cli: str, local: bool) -> None:
    driver = CodexDriver() if cli == "codex" else PiDriver()
    line = f"session id: {VALID_ID}\n" if cli == "codex" else json.dumps({"type": "session", "id": VALID_ID}) + "\n"
    cut = len(line) - 6
    buffer: list[str] = [line[:cut]]
    other: list[str] = [line]  # the stream the driver does not read holds a complete id: never used
    process = (
        FakeProcess(stdout=other, stderr=buffer, local=local)
        if cli == "codex"
        else FakeProcess(stdout=buffer, stderr=other, local=local)
    )
    probe = OutputSessionProbe(driver, cli, **IDS)

    assert probe.probe(process) is None  # only part of the line has arrived
    assert probe.active
    buffer.append(line[cut:])
    assert probe.probe(process) == VALID_ID
    assert probe.found == VALID_ID and not probe.active
    assert probe.probe(process) is None  # an early id is the final id: probing is over
    full = ("", line) if cli == "codex" else (line, "")
    assert probe.final(*full) == VALID_ID


def test_probe_only_looks_when_the_chunk_count_changes() -> None:
    calls: list[tuple[str, str]] = []

    class CountingDriver:
        def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
            calls.append((stdout, stderr))
            return None

    buffer = ["first line\n"]
    process = FakeProcess(stdout=buffer)
    probe = OutputSessionProbe(CountingDriver(), "pi", **IDS)
    for _ in range(3):
        probe.probe(process)
    buffer.append("second line\n")
    probe.probe(process)
    probe.probe(process)
    assert calls == [("first line\n", ""), ("first line\nsecond line\n", "")]


def test_probe_stops_at_the_cap_and_the_end_parse_still_runs() -> None:
    filler = ("x" * 1023 + "\n") * (PROBE_MAX_CHARS // 1024)
    line = json.dumps({"type": "session", "id": VALID_ID}) + "\n"
    process = FakeProcess(stdout=[filler, line])
    probe = OutputSessionProbe(PiDriver(), "pi", **IDS)

    assert probe.probe(process) is None
    assert probe.exhausted and not probe.active
    process._stdout.append("more\n")
    assert probe.probe(process) is None
    assert probe.final(filler + line, "") == VALID_ID  # the full output still holds the id

    text, seen = complete_lines([filler, line])
    assert len(text) == PROBE_MAX_CHARS and text.endswith("\n") and seen >= PROBE_MAX_CHARS


def test_complete_lines_never_returns_a_cut_line() -> None:
    assert complete_lines([]) == ("", 0)
    assert complete_lines(["no newline yet"]) == ("", 14)
    assert complete_lines(["a\nb", "c\nd"]) == ("a\nbc\n", 6)
    assert complete_lines(["abc\n" * 10], limit=10) == ("abc\nabc\n", 40)
    assert complete_lines(["a\n", b"bytes\n"]) == ("", 2)  # type: ignore[list-item]


def test_a_probe_that_raises_is_logged_once_and_skips_the_end_parse(caplog: pytest.LogCaptureFixture) -> None:
    deep = "[" * 200_000 + "]" * 200_000 + "\n"  # pi's parser catches only JSONDecodeError; this raises RecursionError
    line = json.dumps({"type": "session", "id": VALID_ID}) + "\n"
    buffer = [deep, line]
    probe = OutputSessionProbe(PiDriver(), "pi", **IDS)

    assert probe.probe(FakeProcess(stdout=buffer)) is None
    buffer.append("more\n")
    assert probe.probe(FakeProcess(stdout=buffer)) is None
    assert probe.final(deep + line, "") is None
    assert probe.failed and not probe.active

    lines = [(record.levelno, record.msg, record.args) for record in repair_records(caplog)]
    assert lines == [(logging.WARNING, PROBE_FAILED_LINE, ("proj_027", "i014", "worker-1", RUN_ID, "RecursionError"))]


def test_the_end_parse_alone_finds_a_late_id_and_applies_the_rule(caplog: pytest.LogCaptureFixture) -> None:
    probe = OutputSessionProbe(CodexDriver(local=True), "codex", **IDS)
    assert probe.final("", f"noise\nsession id: {VALID_ID}\n") == VALID_ID

    for cli in ("claudecode", "mock"):
        driver = ClaudeCodeDriver() if cli == "claudecode" else MockDriver()
        other = OutputSessionProbe(driver, cli, **IDS)
        assert not other.reads_output and not other.active
        assert other.probe(FakeProcess(stdout=[f"session id: {VALID_ID}\n"])) is None
        assert other.final(f"session id: {VALID_ID}\n", f"session id: {VALID_ID}\n") is None
    assert repair_records(caplog) == []
