"""One token rule for the server and the dispatcher (spec 7, 8.3).

Spec 7 has the dispatcher (8.3) and the agent (9.2) apply the server's rule to their token files, and the dispatcher's
loader writes it as one definition (SEC-12): only the ASCII whitespace around the token is removed, what bytes.strip()
removes, and then every character must be printable ASCII (0x20-0x7E), 32 or more of them. A character that str.strip()
would also remove (0x1C-0x1F, U+0085, U+00A0, U+2028, U+3000, ...) therefore stays, and the printable-ASCII rule
refuses it, on the server as on the dispatcher. A server that removed it would show a dispatcher token as configured in
GET /repair/settings (roles_configured, the runbook's check) that the dispatcher itself refuses, and no run would be
reported.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from repair_dispatcher_support import reset_repair_process_state, token_config, write_token
from test_repair_server_support import AUTH_LOGGER, records

from cairn.dispatcher.repair import config as repair_config
from cairn.server import repair_auth

TOKEN = "test-token-rule-not-a-secret-0123456789"  # noqa: S105 (a fake token for tests; 39 printable ASCII characters)
REJECTED = "repair token rejected role=dispatcher reason="

# What bytes.strip() removes.
ASCII_WHITESPACE = (" ", "\t", "\n", "\r", "\x0b", "\x0c", " \t\x0b\x0c\r\n")
# What str.strip() removes and bytes.strip() does not. None of it is printable ASCII.
OTHER_WHITESPACE = (
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x85",
    "\xa0",
    "\u1680",
    "\u2000",
    "\u200a",
    "\u2028",
    "\u2029",
    "\u202f",
    "\u205f",
    "\u3000",
)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.INFO, logger=AUTH_LOGGER)
    caplog.set_level(logging.INFO, logger="cairn.dispatcher.repair")
    repair_auth.reset()
    reset_repair_process_state(monkeypatch)
    yield
    repair_auth.reset()
    for record in caplog.records:
        assert TOKEN not in record.getMessage()


def _server_verdict(caplog: pytest.LogCaptureFixture, environ: dict[str, str]) -> str:
    """The server's verdict on the dispatcher role's token, as its startup read gives it: configured, or the reason."""
    repair_auth.reset()
    caplog.clear()
    repair_auth.load_tokens(environ)
    if repair_auth.roles_configured()["dispatcher"]:
        return "configured"
    reasons = [message.removeprefix(REJECTED) for _, message in records(caplog, AUTH_LOGGER) if REJECTED in message]
    assert len(reasons) == 1, records(caplog, AUTH_LOGGER)
    return reasons[0]


def _dispatcher_verdict(monkeypatch: pytest.MonkeyPatch, token_file: Path) -> str:
    """The dispatcher's verdict on its token_file: configured, or the reason, named as the server names it."""
    reset_repair_process_state(monkeypatch)
    state = repair_config.load_state(token_config(token_file))
    if state.configured:
        assert state.token == TOKEN
        return "configured"
    return "file_unreadable" if state.reason == repair_config.UNREADABLE else str(state.reason)


def test_the_server_removes_only_ascii_whitespace_around_a_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = tmp_path / "repair-dispatcher.token"
    for whitespace in ASCII_WHITESPACE:
        write_token(token_file, whitespace + TOKEN + whitespace + "\n")
        for environ in (
            {"CAIRN_REPAIR_DISPATCHER_TOKEN_FILE": str(token_file)},
            {"CAIRN_REPAIR_DISPATCHER_TOKEN": whitespace + TOKEN + whitespace},
        ):
            assert _server_verdict(caplog, environ) == "configured", (repr(whitespace), list(environ))
            repair_auth.verify("dispatcher", "runs.register", TOKEN)  # the token, without the whitespace

    for character in OTHER_WHITESPACE:
        for before, after in ((character, ""), ("", character), (character, character + "\n")):
            write_token(token_file, before + TOKEN + after)
            for environ in (
                {"CAIRN_REPAIR_DISPATCHER_TOKEN_FILE": str(token_file)},
                {"CAIRN_REPAIR_DISPATCHER_TOKEN": before + TOKEN + after},
            ):
                assert _server_verdict(caplog, environ) == "not_ascii", (repr(before), repr(after), list(environ))
                assert repair_auth.roles_configured() == {"agent": False, "dispatcher": False}


def test_the_server_and_the_dispatcher_give_one_verdict_on_each_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cases: list[tuple[bytes, str]] = [
        (TOKEN.encode(), "configured"),
        (b"  " + TOKEN.encode() + b"\r\n", "configured"),
        *[((ws + TOKEN + ws + "\n").encode(), "configured") for ws in ASCII_WHITESPACE],
        *[((character + TOKEN).encode(), "not_ascii") for character in OTHER_WHITESPACE],
        *[((TOKEN + character + "\n").encode(), "not_ascii") for character in OTHER_WHITESPACE],
        (b"\xff" + TOKEN.encode(), "not_ascii"),  # not UTF-8
        (TOKEN[:20].encode() + b"\t" + TOKEN[20:].encode(), "not_ascii"),  # inside, whitespace is no longer removed
        (TOKEN[:20].encode() + b"\x7f" + TOKEN[20:].encode(), "not_ascii"),
        (b" " + b"a" * 31 + b"\n\n", "shorter_than_32"),
        (b"", "shorter_than_32"),
        (b"a" * (repair_auth.MAX_TOKEN_FILE_BYTES + 1), "file_unreadable"),
    ]
    token_file = tmp_path / "repair-dispatcher.token"
    for content, expected in cases:
        write_token(token_file, content)
        server = _server_verdict(caplog, {"CAIRN_REPAIR_DISPATCHER_TOKEN_FILE": str(token_file)})
        dispatcher = _dispatcher_verdict(monkeypatch, token_file)
        assert (server, dispatcher) == (expected, expected), content[:48]
