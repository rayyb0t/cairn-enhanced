"""The reviewer pin (owner decision O9, the Q18 safeguard; build step R3, A1-A9): AG80-AG86.

The live agent pins its first reviewer's name, base_url, api_format, model, proxy and ca_bundle in
`<state.dir>/reviewer-pin.json` at its first start (0600, through a temporary file and `os.replace`), starts again
while they match, and refuses to start (exit code 2, no traceback) when they changed, with one line that names the
changed fields and never a value, then the remedy; or when the pin cannot be used, with
`repair reviewer pin unusable path=%s reason=%s`. Its state folder is checked by the isolation check at its start
only; the replay and the gate tools never touch it.

These tests keep the pin between starts, as the agent does: they are marked `reviewer_pin`, which the conftest's
`_each_start_pins_afresh` leaves alone. Every value is synthetic; the server and the model are loopback stubs.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentkit import (
    EXTEND,
    INTENT_ID,
    PROJECT,
    CairnStub,
    ModelStub,
    Reply,
    cairn_stubs,
    chat_answer,
    configure,
    model_stubs,
    run_view,
    write_run_copy,
)
from conftest import (
    AGENT_GID,
    AGENT_TOKEN,
    AGENT_UID,
    IS_ROOT,
    OTHER_GID,
    OTHER_UID,
    REVIEWER_KEY,
    SECOND_REVIEWER_KEY,
    HostInstall,
    Layout,
    needs_root,
    reviewer_entry,
    set_mode,
    settings_data,
    write_bytes,
)
from records import CLAUDE, fixture_bytes

from cairn_repair_agent import cli, config, export, gate, isolation, model, pin, replay, server

pytestmark = pytest.mark.reviewer_pin

PIN = pin.PIN_FILE_NAME
REMEDY = (
    "repair reviewer pin: if you changed the reviewer yourself, remove the pin file as root and start the agent again"
)
FOLDER_REMEDY = (
    "repair reviewer pin: the agent cannot use its state folder; check state.dir in repair.yaml, and as root make it "
    "the agent's own folder with: install -d -m 0700 -o {user} -g {group} {folder}"
)
CA_PEM = "-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n"
PROXY = "http://127.0.0.1:3128"
TEMPORARY = re.compile(r"\.reviewer-pin\.json\.[0-9a-f]{16}\.tmp")


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture
def cairn_stub() -> Iterator[CairnStub]:
    yield from cairn_stubs()


def ca_file(base: Path, name: str) -> str:
    return str(write_bytes(base / name, CA_PEM, mode=0o644))


def first_reviewer(base: Path, stub: ModelStub, **changes: Any) -> dict[str, Any]:
    """The live reviewer: a key, a CA file and every pinned field but `proxy` set (a proxy would keep a review's
    request from the stub)."""
    entry = reviewer_entry(
        "reviewer_pinned",
        model="pinned-model-canary",
        base_url=stub.url + "/v1",
        api_key=REVIEWER_KEY,
        ca_bundle=ca_file(base, "pinned-ca.pem"),
    )
    entry.update(changes)
    return entry


def second_reviewer(stub: ModelStub, **changes: Any) -> dict[str, Any]:
    entry = reviewer_entry(
        "reviewer_second", model="second-model-canary", base_url=stub.url + "/v1", api_key=SECOND_REVIEWER_KEY
    )
    entry.update(changes)
    return entry


def records_of(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name.startswith("cairn_repair_agent")]


def start(layout: Layout, capsys: pytest.CaptureFixture[str], *extra: str, seconds: float = 60) -> tuple[int, str]:
    """`cairn-repair --config <repair.yaml> --allow-shared-user --once`, in a thread that must end within `seconds`
    (a start that blocks, on a FIFO for one, fails here instead of hanging): the exit code and standard error. The
    layout's own isolation reasons are not what these tests check, and the pin applies with the flag (A3)."""
    result: dict[str, int] = {}

    def run() -> None:
        result["code"] = cli.main(layout.argv("--allow-shared-user", "--once", *extra))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the agent's start blocked"
    return result["code"], capsys.readouterr().err


def pin_file(state: Path) -> Path:
    return state / PIN


def no_traceback(caplog: pytest.LogCaptureFixture, stderr: str) -> None:
    assert "Traceback" not in stderr
    assert all(record.exc_info is None for record in caplog.records)


def readable_settings(layout: Layout) -> None:
    """A settings file readable by others, which gives step 5's `repair agent settings file readable by others`."""
    set_mode(layout.settings_path, 0o644)


def warned_before_the_refusal(records: list[tuple[str, str]], layout: Layout) -> None:
    """§9.3 step 5: the pin is checked after the WARNINGs that say who can change the reviewer, so a refused start
    logs them first: the settings file readable by others (`readable_settings`), and, as any user but root, a settings
    file that is not root's."""
    first_error = next(index for index, (level, _) in enumerate(records) if level == "ERROR")
    before, path = records[:first_error], layout.settings_path
    assert ("WARNING", f"repair agent settings file readable by others path={path} mode=0644") in before
    if not IS_ROOT:
        writable = "repair reviewer settings are writable by the dispatcher's user, local workers can change them"
        assert ("WARNING", f"{writable} path={path}") in before


# --- AG80 -------------------------------------------------------------------------------------------------------------


# A8: the live agent pins with or without --dry-run, which still sends digests to the reviewer.
DRY_RUN = pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["live", "dry_run"])


@DRY_RUN
def test_the_first_start_pins_the_live_reviewer(
    flags: tuple[str, ...],
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AG80: the first start writes the pin, 0600 and the agent's own, through a temporary file in the state folder
    and `os.replace`, before any client is built; its content is exactly A6's, the six values as the agent read them
    and no key; and the INFO line shows the reviewer's name and no more of its URL than `scheme://host:port`. A first
    start with `--dry-run` pins as well (A8)."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub()
    base_url = f"http://user-canary:password-canary@{stub.url.removeprefix('http://')}/v1/deep-path-canary"
    reviewers = [first_reviewer(layout.base, stub, base_url=base_url, proxy=PROXY), second_reviewer(stub)]
    configure(layout, cairn_stub.url, reviewers=reviewers)

    replaced: list[tuple[str, str, bool, list[str], int]] = []
    real_replace = os.replace

    def watched_replace(src: Any, dst: Any, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
        listing = sorted(os.listdir(layout.state_dir))
        mode = stat.S_IMODE(os.stat(src, dir_fd=src_dir_fd).st_mode)
        same_folder = src_dir_fd is not None and src_dir_fd == dst_dir_fd
        replaced.append((os.fspath(src), os.fspath(dst), same_folder, listing, mode))
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    built: list[tuple[str, bool]] = []
    real_server, real_model = server.RepairServerClient, model.build_model_client

    def server_client(*args: Any, **kwargs: Any) -> Any:
        built.append(("server", pin_file(layout.state_dir).exists()))
        return real_server(*args, **kwargs)

    def model_client(reviewer: Any) -> Any:
        built.append(("model", pin_file(layout.state_dir).exists()))
        return real_model(reviewer)

    monkeypatch.setattr(os, "replace", watched_replace)
    monkeypatch.setattr(server, "RepairServerClient", server_client)
    monkeypatch.setattr(model, "build_model_client", model_client)
    before = datetime.now(UTC).replace(microsecond=0)
    code, stderr = start(layout, capsys, *flags)
    after = datetime.now(UTC)
    assert code == 0, stderr

    assert built == [("server", True), ("model", True)]  # pinned before any client is built
    ((temporary, target, same_folder, listing, mode),) = replaced
    assert TEMPORARY.fullmatch(temporary) and target == PIN and same_folder
    assert listing == [temporary] and mode == 0o600  # written whole as a temporary file, then renamed
    assert sorted(os.listdir(layout.state_dir)) == [PIN]  # no temporary file is left
    info = os.lstat(pin_file(layout.state_dir))
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()

    data = pin_file(layout.state_dir).read_bytes()
    document = json.loads(data)
    assert list(document) == ["version", "pinned_at", "reviewer"] and document["version"] == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", document["pinned_at"])
    pinned_at = datetime.strptime(document["pinned_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before <= pinned_at <= after
    assert list(document["reviewer"]) == list(pin.PINNED_FIELDS)
    assert document["reviewer"] == {
        "name": "reviewer_pinned",
        "base_url": base_url,
        "api_format": "openai_chat",  # as the agent read it: left out, it is openai_chat
        "model": "pinned-model-canary",
        "proxy": PROXY,
        "ca_bundle": reviewers[0]["ca_bundle"],
    }
    for secret in (REVIEWER_KEY, SECOND_REVIEWER_KEY, AGENT_TOKEN, "second-model-canary"):
        assert secret.encode() not in data

    records = records_of(caplog)
    assert [(level, message) for level, message in records if message.startswith("repair reviewer pinned")] == [
        ("INFO", f"repair reviewer pinned reviewer=reviewer_pinned endpoint={stub.url}")
    ]
    messages = [message for _, message in records]
    started = next(index for index, message in enumerate(messages) if message.startswith("repair agent started"))
    assert messages.index(f"repair reviewer pinned reviewer=reviewer_pinned endpoint={stub.url}") < started
    assert messages[started].endswith(f" dry_run={'true' if flags else 'false'}")
    text = "\n".join(messages) + stderr
    for hidden in ("user-canary", "password-canary", "deep-path-canary", PROXY, REVIEWER_KEY, SECOND_REVIEWER_KEY):
        assert hidden not in text


# --- AG81 -------------------------------------------------------------------------------------------------------------


def test_a_matching_pin_starts(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG81: a start whose first reviewer matches the pin starts with no extra line, leaves the pin as it was, and
    reviews with that reviewer. The pin holds the validated values: `api_format` written out as `openai_chat`, as the
    dispatcher writes it, is the same reviewer as one that leaves it out."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    reviewers = [first_reviewer(layout.base, stub), second_reviewer(stub)]
    configure(layout, cairn_stub.url, reviewers=reviewers)
    assert start(layout, capsys)[0] == 0
    pinned = pin_file(layout.state_dir).read_bytes()
    before = os.stat(pin_file(layout.state_dir))

    reviewers[0]["api_format"] = "openai_chat"
    configure(layout, cairn_stub.url, reviewers=reviewers)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    caplog.clear()
    code, stderr = start(layout, capsys)
    assert code == 0, stderr
    records = records_of(caplog)
    # No line of the pin's ("repair reviewer pinned", "... pin unusable", "... pin:", "... changed"). As any user but
    # root, step 5's "repair reviewer settings are writable ..." WARNING is logged too: the settings file is not root's.
    assert [
        message for _, message in records if message.startswith(("repair reviewer pin", "repair reviewer changed"))
    ] == []
    assert [message for level, message in records if level == "ERROR"] == []
    assert len([message for _, message in records if message.startswith("repair agent started")]) == 1
    assert len(stub.requests) == 1 and [body["decision"] for body in cairn_stub.posted()] == ["extend"]
    after = os.stat(pin_file(layout.state_dir))
    assert pin_file(layout.state_dir).read_bytes() == pinned
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert sorted(os.listdir(layout.state_dir)) == [PIN]


# --- AG82 -------------------------------------------------------------------------------------------------------------

Change = Callable[[Path, ModelStub], tuple[dict[str, Any], str]]
CHANGES: dict[str, Change] = {
    "name": lambda base, other: ({"name": "reviewer_renamed"}, "name"),
    "base_url": lambda base, other: ({"base_url": other.url + "/v1"}, "base_url"),
    "api_format": lambda base, other: ({"api_format": "anthropic_messages"}, "api_format"),
    "model": lambda base, other: ({"model": "renamed-model-canary"}, "model"),
    "proxy": lambda base, other: ({"proxy": PROXY}, "proxy"),
    "ca_bundle": lambda base, other: ({"ca_bundle": ca_file(base, "other-ca.pem")}, "ca_bundle"),
    "model_and_base_url": lambda base, other: (
        {"model": "renamed-model-canary", "base_url": other.url + "/v1"},
        "base_url,model",  # both named, in A1's order
    ),
}


@DRY_RUN
@pytest.mark.parametrize("case", [*CHANGES, "reviewers_reordered"])
def test_each_pinned_field_changed_refuses_with_its_name(
    case: str,
    flags: tuple[str, ...],
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG82: each pinned field changed, two at once, and a reorder of the reviewers that puts the other one first:
    exit code 2, one ERROR line that names the changed fields in A1's order and the pin file, then the remedy, and
    neither value anywhere; no request reaches the server stub or a model stub, although a run is listed for review;
    and the pin is left as it was. The same with `--dry-run` (A8). The refusal comes after step 5's WARNINGs, which
    say who can change the reviewer (§9.3 step 5; the settings file is readable by others here)."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub, other = model_stub(Reply(body=chat_answer(EXTEND))), model_stub(Reply(body=chat_answer(EXTEND)))
    reviewers = [first_reviewer(layout.base, stub), second_reviewer(other)]
    configure(layout, cairn_stub.url, reviewers=reviewers)
    assert start(layout, capsys, *flags)[0] == 0
    pinned = pin_file(layout.state_dir).read_bytes()
    old = json.loads(pinned)["reviewer"]

    if case == "reviewers_reordered":
        reviewers = [reviewers[1], reviewers[0]]
        fields = "name,base_url,model,ca_bundle"  # the second reviewer's api_format and proxy are the first's
        new = reviewers[0]
    else:
        changes, fields = CHANGES[case](layout.base, other)
        new = {**reviewers[0], **changes}
        reviewers[0] = new
    configure(layout, cairn_stub.url, reviewers=reviewers)
    readable_settings(layout)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    requests_before = len(cairn_stub.requests)
    caplog.clear()

    code, stderr = start(layout, capsys, *flags)
    assert code == 2
    no_traceback(caplog, stderr)
    records = records_of(caplog)
    assert [(level, message) for level, message in records if level == "ERROR"] == [
        ("ERROR", f"repair reviewer changed since it was pinned fields={fields} pin={pin_file(layout.state_dir)}"),
        ("ERROR", REMEDY),
    ]
    warned_before_the_refusal(records, layout)
    assert stderr == ""  # the refusal was logged; nothing more is printed
    text = "\n".join(message for _, message in records)
    for field in fields.split(","):
        values = [value for value in (old[field], new.get(field)) if value is not None]
        assert values and all(value not in text for value in values), (field, values)
    assert not any(message.startswith("repair agent started") for _, message in records)
    assert len(cairn_stub.requests) == requests_before
    assert stub.requests == [] and other.requests == []
    assert pin_file(layout.state_dir).read_bytes() == pinned


# --- AG83 -------------------------------------------------------------------------------------------------------------

UNPINNED: dict[str, dict[str, Any]] = {
    "api_key": {"api_key": SECOND_REVIEWER_KEY},  # the key alone
    "max_tokens": {"max_tokens": 2048},
    "context_window": {"context_window": 65536},
    "timeout_seconds": {"timeout_seconds": 60},
    "max_attempts": {"max_attempts": 1},
    "structured_output": {"structured_output": False},
    "second_reviewer": {},
}


@pytest.mark.parametrize("case", list(UNPINNED))
def test_a_change_of_an_unpinned_setting_starts(
    case: str,
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG83: the key alone, each of max_tokens, context_window, timeout_seconds, max_attempts and structured_output,
    and a second reviewer's entry, changed: the agent starts and reviews with the first reviewer, the pin unchanged."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub, other = model_stub(Reply(body=chat_answer(EXTEND))), model_stub(Reply(body=chat_answer(EXTEND)))
    reviewers = [first_reviewer(layout.base, stub), second_reviewer(other)]
    configure(layout, cairn_stub.url, reviewers=reviewers)
    assert start(layout, capsys)[0] == 0
    pinned = pin_file(layout.state_dir).read_bytes()

    if case == "second_reviewer":
        third = model_stub()
        reviewers[1] = second_reviewer(third, name="reviewer_third", model="third-model", api_key=REVIEWER_KEY)
    else:
        reviewers[0] = {**reviewers[0], **UNPINNED[case]}
    configure(layout, cairn_stub.url, reviewers=reviewers)
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    caplog.clear()
    code, stderr = start(layout, capsys)
    assert code == 0, stderr
    assert [message for level, message in records_of(caplog) if level == "ERROR"] == []
    assert len(stub.requests) == 1 and other.requests == []
    if case == "api_key":
        assert stub.requests[0].headers["Authorization"] == f"Bearer {SECOND_REVIEWER_KEY}"
    assert pin_file(layout.state_dir).read_bytes() == pinned


# --- AG84 -------------------------------------------------------------------------------------------------------------


def _rewrite(state: Path, data: bytes) -> None:
    path = pin_file(state)
    path.unlink()
    write_bytes(path, data, mode=0o600)


def _edited(state: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(pin_file(state).read_bytes())
    change(document)
    _rewrite(state, json.dumps(document, indent=2).encode())


def _symlink_to_a_matching_pin(state: Path) -> None:
    """The same, matching pin, owned by the agent's user and 0600, outside the folder: only O_NOFOLLOW refuses it."""
    elsewhere = state.parent / "elsewhere-pin.json"
    os.replace(pin_file(state), elsewhere)
    pin_file(state).symlink_to(elsewhere)


def _socket(state: Path) -> None:
    """A socket file in the pin's place, which no open can read: ENXIO, an OSError A5 names by its errno only."""
    pin_file(state).unlink()
    os.mknod(pin_file(state), stat.S_IFSOCK | 0o600)


def _too_large(state: Path) -> None:
    data = pin_file(state).read_bytes()
    _rewrite(state, data + b" " * (pin.PIN_MAX_BYTES + 1 - len(data)))  # still valid JSON, a byte too large


def _not_utf8(state: Path) -> None:
    _rewrite(state, pin_file(state).read_bytes().replace(b"pinned-model-canary", b"pinned-\xff\xfe-canary"))


def _drop(document: dict[str, Any], key: str) -> None:
    del document[key]


def _directory(state: Path) -> None:
    pin_file(state).unlink()
    pin_file(state).mkdir()


def _fifo(state: Path) -> None:
    pin_file(state).unlink()
    os.mkfifo(pin_file(state), 0o600)


UNUSABLE: dict[str, tuple[Callable[[Path], None], str]] = {
    "symlink": (_symlink_to_a_matching_pin, "not_a_regular_file"),
    "directory": (_directory, "not_a_regular_file"),
    "fifo": (_fifo, "not_a_regular_file"),
    "group_writable": (lambda state: set_mode(pin_file(state), 0o620), "owner_or_mode"),
    "world_writable": (lambda state: set_mode(pin_file(state), 0o602), "owner_or_mode"),
    "another_owner": (lambda state: os.chown(pin_file(state), OTHER_UID, OTHER_GID), "owner_or_mode"),
    "too_large": (_too_large, "too_large"),
    "not_json": (lambda state: _rewrite(state, b'{"version": 1, "pinned_at": '), "malformed"),
    "not_utf8": (_not_utf8, "malformed"),
    "deep_nesting": (lambda state: _rewrite(state, b"[" * 60000), "malformed"),
    "not_an_object": (lambda state: _rewrite(state, b"[]"), "malformed"),
    "unknown_key": (lambda state: _edited(state, lambda d: d.update(extra=1)), "malformed"),
    "missing_key": (lambda state: _edited(state, lambda d: _drop(d, "pinned_at")), "malformed"),
    "wrong_type": (lambda state: _edited(state, lambda d: d["reviewer"].update(name=5)), "malformed"),
    "pinned_at_not_a_string": (lambda state: _edited(state, lambda d: d.update(pinned_at=5)), "malformed"),
    "version_a_string": (lambda state: _edited(state, lambda d: d.update(version="1")), "malformed"),
    "version_true": (lambda state: _edited(state, lambda d: d.update(version=True)), "malformed"),
    "reviewer_field_missing": (lambda state: _edited(state, lambda d: _drop(d["reviewer"], "proxy")), "malformed"),
    "reviewer_with_a_key": (lambda state: _edited(state, lambda d: d["reviewer"].update(api_key="k")), "malformed"),
    "version_2": (lambda state: _edited(state, lambda d: d.update(version=2)), "wrong_version"),
    "version_0": (lambda state: _edited(state, lambda d: d.update(version=0)), "wrong_version"),
    "socket": (_socket, "unreadable:ENXIO"),
}


@pytest.mark.parametrize("case", list(UNUSABLE))
def test_an_unusable_pin_refuses_cleanly(
    case: str,
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG84: every A5 reason, with deep nesting, a non-UTF-8 file and a FIFO (which must not block): exit code 2,
    `repair reviewer pin unusable path=<pin> reason=<reason>` and the remedy, after step 5's WARNINGs, no traceback,
    and no request. A pin of exactly 64 KiB is read."""
    if case == "another_owner" and not IS_ROOT:
        pytest.skip("giving the pin another owner needs root")
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    configure(layout, cairn_stub.url, reviewers=[first_reviewer(layout.base, stub)])
    assert start(layout, capsys)[0] == 0
    setup, reason = UNUSABLE[case]
    setup(layout.state_dir)
    readable_settings(layout)
    requests_before = len(cairn_stub.requests)
    caplog.clear()

    code, stderr = start(layout, capsys, seconds=10)
    assert code == 2
    no_traceback(caplog, stderr)
    records = records_of(caplog)
    assert [(level, message) for level, message in records if level == "ERROR"] == [
        ("ERROR", f"repair reviewer pin unusable path={pin_file(layout.state_dir)} reason={reason}"),
        ("ERROR", REMEDY),
    ]
    warned_before_the_refusal(records, layout)
    assert len(cairn_stub.requests) == requests_before and stub.requests == []

    if case == "too_large":  # the limit is exact: 64 KiB is read, and the pin matches
        _rewrite(layout.state_dir, pin_file(layout.state_dir).read_bytes()[: pin.PIN_MAX_BYTES])
        assert start(layout, capsys)[0] == 0


# --- AG85 -------------------------------------------------------------------------------------------------------------


def test_the_state_folder_rules_a_missing_folder(
    layout: Layout, cairn_stub: CairnStub, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """AG85: a missing state folder stops the agent, with or without `--allow-shared-user`, and the line names the
    command that creates it for the agent's user and group; nothing is created, and no request is sent."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    absent = layout.base / "absent-state"
    configure(layout, cairn_stub.url)
    layout.write_config(state={"dir": str(absent)})
    code, stderr = start(layout, capsys)
    assert code == 2
    no_traceback(caplog, stderr)
    user, group = _name(pwd.getpwuid, os.geteuid()), _name(grp.getgrgid, os.getegid())  # numbers without an entry
    assert [message for level, message in records_of(caplog) if level == "ERROR"] == [
        f"repair reviewer pin unusable path={absent / PIN} reason=folder_missing; as root, create the folder with: "
        f"install -d -m 0700 -o {user} -g {group} {absent}"
    ]
    assert not absent.exists() and cairn_stub.requests == []


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("state", "must be an absolute path"),
        ("/var/lib/<agent>/state", "still holds a <...> placeholder"),
        (7, "must be the absolute path of the agent's state folder"),
    ],
)
def test_the_state_folder_rules_its_path(
    layout: Layout, capsys: pytest.CaptureFixture[str], value: Any, rule: str
) -> None:
    """AG85, A2: `state.dir` is validated like repair.yaml's other paths: a relative path, an unedited `<...>`
    placeholder or a value of another type is a startup error of §9.2 that names the field, which
    `--allow-shared-user` does not override; left out, it is /var/lib/cairn-repair/state."""
    layout.write_config(state={"dir": value})
    code, stderr = start(layout, capsys)
    assert code == 2
    assert stderr.startswith(f"agent config invalid: {layout.config_path}: state.dir: {rule}"), stderr
    without = {key: item for key, item in layout.config.items() if key != "state"}
    layout.write_config(without)
    assert config.load_config(str(layout.config_path)).state.dir == "/var/lib/cairn-repair/state"


@pytest.mark.parametrize("failing", ["write", "replace"])
def test_the_state_folder_rules_a_failed_write(
    failing: str,
    layout: Layout,
    cairn_stub: CairnStub,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AG85: a write of the pin that fails (a full disk while the temporary file is written, or the rename) is
    `write_failed`, exit code 2, and leaves neither a temporary file nor a pin."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    configure(layout, cairn_stub.url)
    temporary_fds: set[int] = set()
    real_open, real_write, real_replace = os.open, os.write, os.replace

    def watched_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if TEMPORARY.fullmatch(os.path.basename(os.fspath(path))):
            temporary_fds.add(fd)
        return fd

    def full_disk(fd: int, data: Any) -> int:
        if fd in temporary_fds:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    def failing_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        if os.fspath(dst) == PIN:
            raise OSError(5, "Input/output error")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "open", watched_open)
    if failing == "write":
        monkeypatch.setattr(os, "write", full_disk)
    else:
        monkeypatch.setattr(os, "replace", failing_replace)
    code, stderr = start(layout, capsys)
    assert code == 2
    no_traceback(caplog, stderr)
    errors = [message for level, message in records_of(caplog) if level == "ERROR"]
    assert errors[0] == f"repair reviewer pin unusable path={pin_file(layout.state_dir)} reason=write_failed"
    assert temporary_fds and os.listdir(layout.state_dir) == []
    assert cairn_stub.requests == []


@dataclass(frozen=True)
class FolderCase:
    """A state folder with this owner and mode, or a symlink to one (A3), and the reasons it gives."""

    uid: int
    gid: int
    mode: int
    reasons: list[str]
    symlink: bool = False

    def make(self, base: Path, name: str) -> Path:
        folder = base / f"{name}-target" if self.symlink else base / name
        folder.mkdir()
        os.chown(folder, self.uid, self.gid)
        set_mode(folder, self.mode)
        if not self.symlink:
            return folder
        link = base / name
        link.symlink_to(folder)
        return link


FOLDER_CASES = {
    "symlink": FolderCase(AGENT_UID, AGENT_GID, 0o700, ["state_dir_symlink"], symlink=True),
    "another_owner": FolderCase(OTHER_UID, OTHER_GID, 0o700, ["state_dir_owner"]),
    "group_writable": FolderCase(AGENT_UID, AGENT_GID, 0o770, ["state_dir_writable"]),
    "world_writable": FolderCase(AGENT_UID, AGENT_GID, 0o707, ["state_dir_writable"]),
    "all_three": FolderCase(
        OTHER_UID, OTHER_GID, 0o777, ["state_dir_symlink", "state_dir_owner", "state_dir_writable"], symlink=True
    ),
    # Another user's folder whose owner bits lack write, open to the agent through its other bits: the owner bits
    # bind its owner, not the agent, so only the agent's own folder must give its user rwx.
    "another_owner_open_to_others": FolderCase(OTHER_UID, OTHER_GID, 0o507, ["state_dir_owner", "state_dir_writable"]),
}


@needs_root
@pytest.mark.parametrize("case", list(FOLDER_CASES))
def test_the_state_folder_rules(host_install: HostInstall, case: str) -> None:
    """AG85, in the host install as the agent's user (no other reason holds there): a symlinked, foreign-owned,
    group-writable or world-writable state folder gives its reason, after `settings_writable`, in A3's order, and
    the agent refuses to start. With `--allow-shared-user` the reasons become the WARNING line and the pin still
    applies: the reviewer is pinned, and a changed reviewer is refused; a folder the agent cannot use stops it. The
    owner bits are checked for the agent's own folder only: another user's folder whose owner bits lack write, but
    whose other bits let the agent in, is used."""
    reasons = FOLDER_CASES[case].reasons
    folder = FOLDER_CASES[case].make(host_install.base, f"state-{case}")
    host_install.write_config(state={"dir": str(folder)})

    refused = host_install.start()
    assert not refused.started
    assert refused.messages("repair agent isolation check failed") == [
        f"repair agent isolation check failed reasons={','.join(reasons)}"
    ]
    assert not pin_file(folder).exists()

    allowed = host_install.start("--allow-shared-user")
    assert allowed.messages("repair agent running without isolation") == [
        f"repair agent running without isolation reasons={','.join(reasons)}"
    ]
    if case == "another_owner":  # 0700 and another user's: the agent cannot use it, and stops
        assert not allowed.started
        assert allowed.messages("repair reviewer pin") == [
            f"repair reviewer pin unusable path={pin_file(folder)} reason=unreadable:EACCES",
            FOLDER_REMEDY.format(
                user=_name(pwd.getpwuid, AGENT_UID), group=_name(grp.getgrgid, AGENT_GID), folder=folder
            ),
        ]
        return
    assert allowed.started, allowed.text()
    assert allowed.messages("repair reviewer pinned") == [
        "repair reviewer pinned reviewer=reviewer_local endpoint=http://127.0.0.1:11434"
    ]
    assert pin_file(folder).is_file() and os.stat(pin_file(folder)).st_uid == AGENT_UID
    host_install.write_settings(settings_data(reviewers=[reviewer_entry(base_url="http://127.0.0.1:11435/v1")]))
    changed = host_install.start("--allow-shared-user")
    assert not changed.started
    assert changed.messages("repair reviewer changed") == [
        f"repair reviewer changed since it was pinned fields=base_url pin={pin_file(folder)}"
    ]


@needs_root
def test_the_state_folder_rules_a_missing_folder_is_no_isolation_reason(host_install: HostInstall) -> None:
    """AG85: in the host install, a missing state folder is no isolation reason (no refusal of the isolation check)
    but stops the agent at its pin, naming the command with the agent's user and group (numbers without an entry)."""
    absent = host_install.base / "no-state-folder"
    host_install.write_config(state={"dir": str(absent)})
    run = host_install.start()
    assert not run.started
    assert run.messages("repair agent isolation check failed") == []
    assert run.messages("repair reviewer pin unusable") == [
        f"repair reviewer pin unusable path={absent / PIN} reason=folder_missing; as root, create the folder with: "
        f"install -d -m 0700 -o {_name(pwd.getpwuid, AGENT_UID)} -g {_name(grp.getgrgid, AGENT_GID)} {absent}"
    ]


@needs_root
def test_the_state_folder_rules_come_after_settings_writable(host_install: HostInstall) -> None:
    """AG85, A3's order, in the host install as the agent's user: a group-writable settings file and a group-writable
    state folder give `settings_writable,state_dir_writable`, in that order, in the refusal and in the
    `--allow-shared-user` WARNING (with which the agent starts and pins)."""
    folder = host_install.base / "state-group-writable"
    folder.mkdir()
    os.chown(folder, AGENT_UID, AGENT_GID)
    set_mode(folder, 0o770)
    host_install.write_config(state={"dir": str(folder)})
    host_install.write_settings(settings_data(), mode=0o660)

    refused = host_install.start()
    assert not refused.started
    assert refused.messages("repair agent isolation check failed") == [
        "repair agent isolation check failed reasons=settings_writable,state_dir_writable"
    ]
    allowed = host_install.start("--allow-shared-user")
    assert allowed.started, allowed.text()
    assert allowed.messages("repair agent running without isolation") == [
        "repair agent running without isolation reasons=settings_writable,state_dir_writable"
    ]
    assert allowed.messages("repair reviewer pinned") == [
        "repair reviewer pinned reviewer=reviewer_local endpoint=http://127.0.0.1:11434"
    ]


# A state folder of the test's own user that gives reasons whoever runs the tests: (its mode, a symlink to it, reasons).
OWN_FOLDER_CASES: dict[str, tuple[int, bool, list[str]]] = {
    "symlink": (0o700, True, ["state_dir_symlink"]),
    "group_writable": (0o770, False, ["state_dir_writable"]),
    "world_writable": (0o707, False, ["state_dir_writable"]),
    "symlink_to_a_writable_folder": (0o777, True, ["state_dir_symlink", "state_dir_writable"]),
}


@pytest.mark.parametrize("case", list(OWN_FOLDER_CASES))
def test_the_state_folder_rules_as_any_user(case: str, layout: Layout) -> None:
    """AG85 without root (only another owner needs it: `test_the_state_folder_rules`): a symlinked, group-writable or
    world-writable state folder of the test's own user gives its reasons (`isolation.state_dir_reasons`), after every
    other reason of the layout, in A3's order. Without `--allow-shared-user` the agent refuses to start and pins
    nothing; with it the reasons are in the WARNING line and the pin still applies: the reviewer is pinned, and a
    changed reviewer is refused."""
    mode, symlink, reasons = OWN_FOLDER_CASES[case]
    target = layout.base / f"state-{case}-target"
    target.mkdir()
    set_mode(target, mode)
    folder = layout.base / f"state-{case}" if symlink else target
    if symlink:
        folder.symlink_to(target)
    assert isolation.state_dir_reasons(str(folder)) == reasons
    assert isolation.state_dir_reasons(str(layout.state_dir)) == []  # 0700, the test user's own
    assert isolation.state_dir_reasons(str(layout.base / "absent")) == []  # a missing folder is no reason (A3)

    # The layout's own reasons, with its own state folder: these come first.
    (warning,) = layout.start("--allow-shared-user").messages("repair agent running without isolation reasons=")
    others = [reason for reason in warning.split("reasons=", 1)[1].split(",") if reason != "none"]
    assert not any(reason.startswith("state_dir_") for reason in others)
    os.unlink(pin_file(layout.state_dir))

    layout.write_config(state={"dir": str(folder)})
    refused = layout.start()
    assert not refused.started
    assert refused.messages("repair agent isolation check failed") == [
        f"repair agent isolation check failed reasons={','.join([*others, *reasons])}"
    ]
    assert not pin_file(folder).exists()
    allowed = layout.start("--allow-shared-user")
    assert allowed.started, allowed.text()
    assert allowed.messages("repair agent running without isolation") == [
        f"repair agent running without isolation reasons={','.join([*others, *reasons])}"
    ]
    assert allowed.messages("repair reviewer pinned") == [
        "repair reviewer pinned reviewer=reviewer_local endpoint=http://127.0.0.1:11434"
    ]
    assert pin_file(target).is_file()
    layout.write_settings(settings_data(reviewers=[reviewer_entry(base_url="http://127.0.0.1:11435/v1")]))
    changed = layout.start("--allow-shared-user")
    assert not changed.started
    assert changed.messages("repair reviewer changed") == [
        f"repair reviewer changed since it was pinned fields=base_url pin={pin_file(folder)}"
    ]


@pytest.mark.parametrize("where", ["a_file", "under_a_file"])
def test_the_state_folder_rules_a_folder_the_agent_cannot_open(
    where: str,
    layout: Layout,
    cairn_stub: CairnStub,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG85, A5: a `state.dir` that is there but no folder the agent can open (here a regular file, or a path under
    one: ENOTDIR; another user's 0700 folder, EACCES, in `test_the_state_folder_rules`) stops the agent with exit
    code 2, `reason=unreadable:<errno>`, and a second line that names the folder's remedy, the command that makes it
    the agent's own folder, never the pin file's: there is no pin file to remove. No traceback and no request."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    configure(layout, cairn_stub.url)
    a_file = write_bytes(layout.base / "state-file", "not a folder\n", mode=0o600)
    folder = a_file if where == "a_file" else a_file / "state"
    layout.write_config(state={"dir": str(folder)})
    code, stderr = start(layout, capsys)
    assert code == 2
    no_traceback(caplog, stderr)
    user, group = _name(pwd.getpwuid, os.geteuid()), _name(grp.getgrgid, os.getegid())  # numbers without an entry
    assert [message for level, message in records_of(caplog) if level == "ERROR"] == [
        f"repair reviewer pin unusable path={folder / PIN} reason=unreadable:ENOTDIR",
        FOLDER_REMEDY.format(user=user, group=group, folder=folder),
    ]
    assert a_file.read_bytes() == b"not a folder\n" and cairn_stub.requests == []


# The agent's own state folder with a mode that does not give its user read, search and write (u+rwx): 0600 can be
# listed but not searched, so no pin can be opened in it; 0500 can be searched but not written, so no pin can be made.
MODES_WITHOUT_RWX = [0o600, 0o500, 0o400, 0o300, 0o000]


@pytest.mark.parametrize("pinned", [False, True], ids=["empty", "pinned"])
@pytest.mark.parametrize("mode", MODES_WITHOUT_RWX, ids=lambda mode: f"{mode:04o}")
def test_the_state_folder_rules_a_folder_its_user_cannot_use(
    mode: int,
    pinned: bool,
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AG85, A5: the agent's own state folder whose mode lacks u+rwx is no isolation reason, but stops the agent
    before the pin is read or written, whoever the agent runs as: `reason=unreadable:EACCES`, the error a user
    without these bits gets, then the folder's remedy (its `install -d -m 0700` gives the folder its mode back), never
    the pin file's, which would send the owner to a pin file that is fine or not there. The same with a matching pin
    in the folder. Exit code 2, no traceback, no request, and the folder as it was."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    configure(layout, cairn_stub.url, reviewers=[first_reviewer(layout.base, stub)])
    if pinned:
        assert start(layout, capsys)[0] == 0
    before = {name: (layout.state_dir / name).read_bytes() for name in os.listdir(layout.state_dir)}
    assert bool(before) == pinned
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)
    requests_before = len(cairn_stub.requests)
    caplog.clear()

    set_mode(layout.state_dir, mode)
    try:
        assert isolation.state_dir_reasons(str(layout.state_dir)) == []
        code, stderr = start(layout, capsys)
    finally:
        set_mode(layout.state_dir, 0o700)
    assert code == 2
    no_traceback(caplog, stderr)
    user, group = _name(pwd.getpwuid, os.geteuid()), _name(grp.getgrgid, os.getegid())  # numbers without an entry
    assert [message for level, message in records_of(caplog) if level == "ERROR"] == [
        f"repair reviewer pin unusable path={pin_file(layout.state_dir)} reason=unreadable:EACCES",
        FOLDER_REMEDY.format(user=user, group=group, folder=layout.state_dir),
    ]
    assert len(cairn_stub.requests) == requests_before and stub.requests == []
    assert {name: (layout.state_dir / name).read_bytes() for name in os.listdir(layout.state_dir)} == before


@needs_root
@pytest.mark.parametrize("mode", [0o600, 0o500], ids=lambda mode: f"{mode:04o}")
def test_the_state_folder_rules_a_folder_its_user_cannot_use_in_the_host_install(
    mode: int, host_install: HostInstall
) -> None:
    """AG85, in the host install as the agent's user, whom the mode binds: its own state folder of mode 0600 (it can
    list it, not open a pin in it) or 0500 (it can open a pin, not write one) gives no isolation reason, and the
    agent stops with `reason=unreadable:EACCES` and the folder's remedy, before it reads or writes the pin."""
    set_mode(host_install.state_dir, mode)
    run = host_install.start()
    assert not run.started
    assert run.messages("repair agent isolation check failed") == []
    assert run.messages("repair reviewer pin") == [
        f"repair reviewer pin unusable path={pin_file(host_install.state_dir)} reason=unreadable:EACCES",
        FOLDER_REMEDY.format(
            user=_name(pwd.getpwuid, AGENT_UID), group=_name(grp.getgrgid, AGENT_GID), folder=host_install.state_dir
        ),
    ]
    assert os.listdir(host_install.state_dir) == []


def _name(lookup: Callable[[int], Any], number: int) -> str:
    try:
        entry = lookup(number)
    except KeyError:
        return str(number)
    return str(entry[0])


AGENT_DOC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "repair-agent.md"


def test_the_docs_point_to_the_startup_step_that_checks_the_pin() -> None:
    """docs/specs/repair-agent.md checks the pin at the end of step 5 of its startup section («## 启动»), after that
    step's WARNINGs, which say who can change the reviewer (§9.3 step 5), as `cli.start` does; and every line of the
    document that points to a startup step for the pin's refusals or limits names that step."""
    text = AGENT_DOC.read_text(encoding="utf-8")
    section = text.split("\n## 启动\n", 1)[1].split("\n## ", 1)[0]
    steps = dict(re.findall(r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", section, re.MULTILINE | re.DOTALL))
    (pin_step,) = [number for number, body in steps.items() if "核对**评审模型固定**" in body]
    body = steps[pin_step]
    assert pin_step == "5" and body.index("repair agent settings file readable by others") < body.index("核对**评审")
    references = [line for line in text.splitlines() if "「启动」第" in line and "评审模型固定" in line]
    assert len(references) >= 2  # the host install's paragraph and the runbook's step 4 check
    for line in references:
        assert re.findall(r"「启动」第 (\d+) 步", line) == [pin_step], line


# --- AG86 -------------------------------------------------------------------------------------------------------------

# Every file system event under the state folder while a tool runs: an audit hook (open, listing, change) and wrapped
# os.stat, os.lstat, os.access and os.statvfs, which raise no audit event. A hook cannot be removed, so it records
# nothing unless a test sets _WATCHED.
_WATCHED: list[str] | None = None
_EVENTS = {
    "open",
    "os.listdir",
    "os.scandir",
    "os.mkdir",
    "os.rename",
    "os.remove",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "os.utime",
    "os.truncate",
    "os.symlink",
    "os.link",
    "os.chdir",
    "shutil.copyfile",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
}


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WATCHED is not None and event in _EVENTS:
        _WATCHED.extend(os.fsdecode(arg) for arg in args if isinstance(arg, (str, bytes, os.PathLike)))


sys.addaudithook(_audit)


def write_eval_set(base: Path) -> Path:
    """A one-case eval set as redact_eval_set.py leaves it: `<intent>/intent.json` with the attempt's budget, the
    redacted transcript (the pinned Claude Code fixture, synthetic), and `labels.csv`."""
    folder = base / INTENT_ID
    folder.mkdir(parents=True)
    meta = {
        "intent_id": INTENT_ID,
        "description": "List the lab's configuration files.",
        "timed_out_attempts": [{"attempt": 1, "execute_timeout_s": 1800}],
    }
    (folder / "intent.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / replay.REDACTED_NAME).write_bytes(fixture_bytes(CLAUDE))
    (base / "labels.csv").write_text(f"intent_id,label,evidence,notes\n{INTENT_ID},progressing,E1,\n", encoding="utf-8")
    return base


@pytest.mark.parametrize("pinned", [False, True], ids=["no_pin", "pinned"])
def test_the_replay_and_gate_tools_never_pin(
    pinned: bool,
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AG86: the replay (a dry run, and a run with `--yes` against a stub), the gate report on its decisions, and the
    export of live shadow runs, all with the agent's repair.yaml, never open, list, stat or write anything under
    state.dir, with or without an existing pin; and the folder is as it was."""
    global _WATCHED
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    configure(layout, cairn_stub.url, reviewers=[first_reviewer(layout.base, stub)])
    if pinned:
        assert start(layout, capsys)[0] == 0
    before = {name: (layout.state_dir / name).read_bytes() for name in os.listdir(layout.state_dir)}
    assert bool(before) == pinned
    eval_dir = write_eval_set(layout.base / "eval")
    state = os.path.abspath(layout.state_dir)

    stats: list[str] = []
    watch = pytest.MonkeyPatch()
    for name in ("stat", "lstat", "access", "statvfs"):
        real = getattr(os, name)

        def watched(path: Any, *args: Any, _real: Callable[..., Any] = real, **kwargs: Any) -> Any:
            if isinstance(path, (str, bytes, os.PathLike)):
                stats.append(os.fsdecode(path))
            return _real(path, *args, **kwargs)

        watch.setattr(os, name, watched)
    _WATCHED = []
    try:
        dry = layout.base / "replay-dry"
        assert replay.main([str(eval_dir), "--config", str(layout.config_path), "--out", str(dry), "--dry-run"]) == 0
        sent = layout.base / "replay-sent"
        assert replay.main([str(eval_dir), "--config", str(layout.config_path), "--out", str(sent), "--yes"]) == 0
        decisions = sent / replay.DECISIONS_FILE
        report = layout.base / "gate_report.json"
        gate.main(["--labels", str(eval_dir / "labels.csv"), "--decisions", str(decisions), "--out", str(report)])
        exported = layout.base / "shadow"
        assert (
            export.main(
                [
                    *("--server", cairn_stub.url, "--project", PROJECT),
                    *("--agent-token-file", str(layout.token_path), "--config", str(layout.config_path)),
                    *("--out", str(exported)),
                ]
            )
            == 0
        )
        watched_paths = [*_WATCHED, *stats]
    finally:
        _WATCHED = None
        watch.undo()
    capsys.readouterr()

    assert len(stub.requests) == 1 and decisions.is_file() and report.is_file()  # the tools did run
    assert any(path.startswith(str(eval_dir)) for path in watched_paths)  # and the watch did see their files
    assert any(path == str(layout.config_path) for path in watched_paths)  # each read the agent's repair.yaml
    under = [os.path.abspath(path) for path in watched_paths]
    assert [path for path in under if path == state or path.startswith(state + os.sep)] == []
    assert {name: (layout.state_dir / name).read_bytes() for name in os.listdir(layout.state_dir)} == before
