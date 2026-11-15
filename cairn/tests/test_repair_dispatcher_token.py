"""The dispatcher token's loader and the repair side's "configured" state (spec 8.3, 8.11).

The token is read only from the repair: section's token_file, once per process, by whichever thread needs it
first (the housekeeping thread at start, or a run's reporter thread); the task thread only reads the cached
state and never waits for a read. These tests drive the loader alone, with no task and no repair thread: the
threads below only carry those threads' names. DS21 is in test_repair_shadow.py.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repair_dispatcher_support import (
    CONFIGURED_LINE,
    FAKE_TOKEN,
    NOT_CONFIGURED_LINE,
    READABLE_LINE,
    OpenSpy,
    dispatch_payload,
    repair_lines,
    reset_repair_process_state,
    token_config,
    write_token,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import config as repair_config


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    reset_repair_process_state(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()


def test_token_loader_logs_configured_once_for_a_valid_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    token_file = write_token(tmp_path / "dispatcher.token", "t" * 32 + "\n")
    config = token_config(token_file, projects=["proj_001", "proj_002", "proj_003"])

    states = [repair_config.load_state(config) for _ in range(3)]

    assert {(state.configured, state.reason, state.token) for state in states} == {(True, None, "t" * 32)}
    assert repair_config.cached_state(config) is states[0]
    assert repair_lines(caplog) == [(logging.INFO, CONFIGURED_LINE, ("/var/lib/cairn-repair/transcripts", 2, 3))]


def _fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def _around(before: str, after: str) -> bytes:
    """A 40-character token with before and after around it, UTF-8 encoded."""
    return (before + "a" * 40 + after).encode()


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        pytest.param(lambda d: d / "missing.token", "token_unreadable", id="missing"),
        pytest.param(lambda d: d, "token_unreadable", id="directory"),
        pytest.param(lambda d: _fifo(d / "fifo.token"), "token_unreadable", id="fifo"),
        pytest.param(lambda d: write_token(d / "t", "a" * 20 + "é" + "a" * 20), "not_ascii", id="non-ascii"),
        pytest.param(lambda d: write_token(d / "t", b"a" * 20 + b"\xff" + b"a" * 20), "not_ascii", id="not-utf8"),
        pytest.param(lambda d: write_token(d / "t", "a" * 20 + "\x7f" + "a" * 20), "not_ascii", id="del"),
        pytest.param(lambda d: write_token(d / "t", "a" * 20 + "\t" + "a" * 20), "not_ascii", id="inner-tab"),
        pytest.param(lambda d: write_token(d / "t", "\x01" * 8), "not_ascii", id="short-and-not-ascii"),
        # Only ASCII whitespace around the token is removed, as the agent and the server strip theirs (SEC-12):
        # characters that str.strip() would also remove are bytes outside 0x20-0x7E, so not_ascii.
        pytest.param(lambda d: write_token(d / "t", _around("", "\x1f")), "not_ascii", id="unit-separator-after"),
        pytest.param(lambda d: write_token(d / "t", _around("\x1c", "")), "not_ascii", id="file-separator-before"),
        pytest.param(lambda d: write_token(d / "t", _around("", "\u00a0")), "not_ascii", id="no-break-space-after"),
        pytest.param(lambda d: write_token(d / "t", _around("", "\u0085")), "not_ascii", id="next-line-after"),
        pytest.param(lambda d: write_token(d / "t", _around("", "\u2028\n")), "not_ascii", id="line-separator-after"),
        pytest.param(lambda d: write_token(d / "t", _around("\u3000", "")), "not_ascii", id="ideographic-space-before"),
        pytest.param(lambda d: write_token(d / "t", "a" * 31), "shorter_than_32", id="31-characters"),
        pytest.param(lambda d: write_token(d / "t", "  " + "a" * 31 + "\n\n"), "shorter_than_32", id="31-after-strip"),
        pytest.param(lambda d: write_token(d / "t", ""), "shorter_than_32", id="empty"),
    ],
)
def test_token_loader_logs_not_configured_once_per_process_with_its_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, make: Any, reason: str
) -> None:
    config = token_config(make(tmp_path))

    started = time.monotonic()
    states = [repair_config.load_state(config) for _ in range(3)]
    elapsed = time.monotonic() - started

    assert elapsed < 2.0  # a FIFO without a writer must not block the read
    assert {(state.configured, state.reason, state.token) for state in states} == {(False, reason, None)}
    assert repair_config.cached_state(config) is states[0]
    assert repair_lines(caplog) == [(logging.WARNING, NOT_CONFIGURED_LINE, (reason,))]


def test_every_kind_of_ascii_whitespace_around_the_token_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for whitespace in (" ", "\t", "\n", "\r", "\x0b", "\x0c", " \t\x0b\x0c\r\n"):
        reset_repair_process_state(monkeypatch)
        token_file = write_token(tmp_path / "dispatcher.token", whitespace + FAKE_TOKEN + whitespace + "\n")
        state = repair_config.load_state(token_config(token_file))
        assert (state.configured, state.reason, state.token) == (True, None, FAKE_TOKEN), repr(whitespace)


def test_a_token_file_larger_than_64_kib_is_unreadable_and_read_no_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cap = repair_config.TOKEN_FILE_MAX_BYTES
    assert cap == 65536  # the agent's cap for its secret files

    # A file of exactly the cap is read; its content is the token.
    token_file = write_token(tmp_path / "cap.token", "a" * cap)
    state = repair_config.load_state(token_config(token_file))
    assert (state.configured, state.reason, len(state.token or "")) == (True, None, cap)

    # One byte more, or far more, is token_unreadable, logged once; the read stops one byte after the cap.
    for size in (cap + 1, 8 * 1024 * 1024):
        reset_repair_process_state(monkeypatch)
        caplog.clear()
        token_file = write_token(tmp_path / f"large-{size}.token", b"a" * size)
        config = token_config(token_file)
        with monkeypatch.context() as patch:
            spy = OpenSpy(patch, token_file)
            states = [repair_config.load_state(config) for _ in range(3)]
        assert {(state.configured, state.reason, state.token) for state in states} == {
            (False, "token_unreadable", None)
        }, size
        assert spy.openers == ["MainThread"], size
        assert spy.bytes_read == cap + 1, size
        assert repair_lines(caplog) == [(logging.WARNING, NOT_CONFIGURED_LINE, ("token_unreadable",))], size


def test_no_repair_section_is_settled_without_io_and_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = DispatchConfig.model_validate(dispatch_payload())
    opened: list[str] = []
    real_open = os.open

    def spy_open(name: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(name))
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)

    first = repair_config.cached_state(config)
    again = [repair_config.cached_state(config), repair_config.load_state(config)]

    assert first == repair_config.RepairState(configured=False, reason="no_repair_section")
    assert all(state is first for state in again)
    assert opened == []
    assert repair_lines(caplog) == [(logging.WARNING, NOT_CONFIGURED_LINE, ("no_repair_section",))]


def test_no_repair_section_is_logged_once_when_threads_settle_it_at_the_same_moment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without repair: the state is settled without the read lock, by the first call that finds it unset.

    Here every thread has found it unset before any settles it (they wait for each other just before settling), so
    each one settles it: one keeps its state and logs, and the others get that state and log nothing.
    """
    config = DispatchConfig.model_validate(dispatch_payload())
    names = [f"repair-shadow-proj_00{n}-i001" for n in range(1, 5)] + [f"task-{n}" for n in range(1, 5)]
    loader = repair_config._LOADER
    real_settle = loader.settle
    together = threading.Barrier(len(names))
    settled: list[str] = []

    def settle_together(state: repair_config.RepairState) -> tuple[repair_config.RepairState, bool]:
        together.wait(5)
        settled.append(threading.current_thread().name)
        return real_settle(state)

    monkeypatch.setattr(loader, "settle", settle_together)
    start = threading.Barrier(len(names))
    results: list[repair_config.RepairState | None] = [None] * len(names)

    def run(index: int) -> None:
        start.wait(5)
        # Task threads ask cached_state; reporter threads ask load_state.
        ask = repair_config.load_state if index < 4 else repair_config.cached_state
        results[index] = ask(config)

    threads = [threading.Thread(target=run, args=(index,), name=name) for index, name in enumerate(names)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(settled) == sorted(names)  # every thread settled it
    first = results[0]
    assert first == repair_config.RepairState(configured=False, reason="no_repair_section")
    assert all(state is first for state in results)
    assert repair_lines(caplog) == [(logging.WARNING, NOT_CONFIGURED_LINE, ("no_repair_section",))]


class _SlowLoader(repair_config._Loader):
    """A loader whose state, while unset, takes 20 ms to read: long enough for every other thread to read it too."""

    def __init__(self) -> None:
        self._state: repair_config.RepairState | None = None
        super().__init__()

    @property
    def state(self) -> repair_config.RepairState | None:
        value = self._state
        if value is None:
            time.sleep(0.02)
        return value

    @state.setter
    def state(self, value: repair_config.RepairState | None) -> None:
        self._state = value


def test_the_state_is_kept_once_when_threads_read_it_unset_at_the_same_moment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Settling checks and keeps the state under one lock, so a second thread can never keep a second state."""
    config = DispatchConfig.model_validate(dispatch_payload())
    monkeypatch.setattr(repair_config, "_LOADER", _SlowLoader())
    start = threading.Barrier(8)
    results: list[repair_config.RepairState | None] = [None] * 8

    def run(index: int) -> None:
        start.wait(5)
        results[index] = (repair_config.load_state if index % 2 else repair_config.cached_state)(config)

    threads = [threading.Thread(target=run, args=(index,), name=f"settler-{index}") for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert results[0] is not None
    assert all(state is results[0] for state in results)
    assert repair_lines(caplog) == [(logging.WARNING, NOT_CONFIGURED_LINE, ("no_repair_section",))]


def test_cached_state_never_reads_or_waits_for_a_read_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = write_token(tmp_path / "dispatcher.token", FAKE_TOKEN)
    config = token_config(token_file)
    spy = OpenSpy(monkeypatch, token_file)
    spy.release.clear()
    reader = threading.Thread(target=repair_config.load_state, args=(config,), name="repair-housekeeping")
    reader.start()
    assert spy.reading.wait(5)

    started = time.monotonic()
    answers = [repair_config.cached_state(config) for _ in range(100)]
    elapsed = time.monotonic() - started

    spy.release.set()
    reader.join(5)
    assert answers == [None] * 100
    assert elapsed < 0.5
    assert spy.openers == ["repair-housekeeping"]
    state = repair_config.cached_state(config)
    assert state is not None
    assert (state.configured, state.token) == (True, FAKE_TOKEN)


def test_a_token_file_readable_by_others_is_warned_about_once_and_still_used(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = write_token(tmp_path / "dispatcher.token", FAKE_TOKEN, 0o604)
    config = token_config(token_file)

    states = [repair_config.load_state(config) for _ in range(3)]

    assert {(state.configured, state.token) for state in states} == {(True, FAKE_TOKEN)}
    assert repair_lines(caplog) == [
        (logging.WARNING, READABLE_LINE, (str(token_file), "0604")),
        (logging.INFO, CONFIGURED_LINE, ("/var/lib/cairn-repair/transcripts", 2, "all")),
    ]
