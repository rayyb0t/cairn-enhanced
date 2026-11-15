"""Dispatcher tests of spec 16 for owner decision O8 (build step 7, its second commit): DS31, DS32 and DS27 again
with spec 8.3's new example, and TC4's case of a reviewer key written with a space before and after it.

O8 gives a reviewer entry of dispatch.yaml's repair: section an optional api_format, openai_chat or
anthropic_messages. The dispatcher checks only that it is one of the two, and writes it into the agent's settings
file for every reviewer, openai_chat for an entry without it; the agent's own rules for anthropic_messages, and its
call, are build step 9's. When dispatch.yaml is loaded, the whitespace around REVIEW_API_KEY is removed as it is
around the dispatcher token, so that the settings file, the shared-key check and the removal from copies hold the key
as it is sent (spec 8.3).

DS27's checks that do not depend on the example are in test_repair_shadow.py, whose cases DS27 and DS31 replay here;
DS28 and DS29 are in test_repair_dispatcher_settings.py, and TC4's other cases in test_repair_transcripts.py. Every
value here is synthetic: the tokens and keys are obviously fake, and https://api.anthropic.com is spec 8.3's example
base URL, which nothing here contacts.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import make_config
from pydantic import ValidationError
from repair_dispatcher_runs import run_config
from repair_dispatcher_support import (
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    bypass_proxies,
    dispatch_payload,
    repair_section,
    reset_repair_process_state,
    write_dispatch_yaml,
    write_token,
)
from test_repair_dispatcher_settings import NOT_WRITTEN, SHARED, WRITTEN, lines, settings_path, worker
from test_repair_shadow import _errors, _file_opens_and_requests, _strict_cases
from test_repair_transcripts_support import (
    SESSION_IDS,
    FakeClock,
    FakeDockerApi,
    FakeFile,
    all_log_text,
    container_session_path,
    fixture_bytes,
    install_docker,
    make_run,
    read_meta,
    read_transcript,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import config as repair_config
from cairn.dispatcher.repair import housekeeping, redaction, transcripts
from cairn.dispatcher.repair.transcripts import CopySchedule

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

CLAUDE_KEY = "test-claude-api-key-not-a-secret-0003"
CLAUDE_BASE_URL = "https://api.anthropic.com"  # spec 8.3's example, written without /v1; never contacted here
C1_SETTINGS = (
    "structured_output",
    "max_tokens",
    "context_window",
    "timeout_seconds",
    "max_attempts",
    "ca_bundle",
    "proxy",
    "price",
)

# DS31: the canaries of the entry whose api_format is refused, and a bad value that is itself one.
CANARY_ENTRY: dict[str, Any] = {
    "name": "name-canary-0031",
    "env": {
        "REVIEW_MODEL": "model-canary-0032",
        "REVIEW_BASE_URL": "https://base-url-canary-0033.invalid/v1",
        "REVIEW_API_KEY": "key-canary-0034-not-a-secret",
    },
}
FORMAT_CANARY = "api-format-canary-0007"
DS31_CANARIES = ("name-canary-0031", "model-canary-0032", "base-url-canary-0033", "key-canary-0034", FORMAT_CANARY)
# DS31: pydantic's rule for api_format, which names every value it accepts: exactly these two (owner decision O8).
API_FORMAT_RULE = "Input should be 'openai_chat' or 'anthropic_messages'"

# DS32: keys written with whitespace around them, and the padded key that holds a control character.
SPACED_WORKER_KEY = "test-spaced-key-0011"  # a worker holds it without the spaces
SPACED_BOTH_KEY = "test-spaced-key-0012"  # a worker holds it with the same spaces
SPACED_LONE_KEY = "test-spaced-key-0013"  # no worker holds it
SPACED_ENVIRONMENT_KEY = "test-spaced-key-0014"  # the dispatcher's environment holds it, with spaces
UNICODE_SPACED_KEY = "test-spaced-key-0017"  # workers hold it with other whitespace around it, one with ASCII's
UNICODE_SPACED_ENVIRONMENT_KEY = "test-spaced-key-0018"  # the environment holds it with other whitespace only
LEAK_CANARY = "test-leak-canary-0008"

# TC4: quotes and a backslash, so that its JSON-escaped forms differ from it.
SPACED_COPY_KEY = 'test-spaced-reviewer-key-"0015"\\x'

BINARY_CANARY = "test-binary-key-canary-0016"  # a key written as YAML's !!binary

SECRETS = (
    FAKE_TOKEN,
    FAKE_REVIEWER_KEY,
    CLAUDE_KEY,
    SPACED_WORKER_KEY,
    SPACED_BOTH_KEY,
    SPACED_LONE_KEY,
    SPACED_ENVIRONMENT_KEY,
    UNICODE_SPACED_KEY,
    UNICODE_SPACED_ENVIRONMENT_KEY,
    LEAK_CANARY,
    SPACED_COPY_KEY,
    BINARY_CANARY,
    *DS31_CANARIES,
)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fresh once-per-process state; no real Docker; no log record holds a token, a key or a canary."""
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    monkeypatch.setattr(redaction, "_oversized_logged", set())

    def no_docker() -> Any:
        raise AssertionError("the repair module reached for Docker")

    monkeypatch.setattr(transcripts, "docker_api", no_docker)
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    text = all_log_text(caplog)
    for secret in SECRETS:
        assert secret not in text, secret


def claude_reviewer() -> dict[str, Any]:
    """Spec 8.3's third reviewer, reviewer_claude, with its placeholders replaced."""
    return {
        "name": "reviewer_claude",
        "api_format": "anthropic_messages",
        "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": CLAUDE_BASE_URL, "REVIEW_API_KEY": CLAUDE_KEY},
    }


def o8_section(**overrides: Any) -> dict[str, Any]:
    """Spec 8.3's example repair: section as O8 left it, its placeholders replaced: repair_section()'s
    reviewer_local and reviewer_provider, then reviewer_claude."""
    section = repair_section(**overrides)
    if "reviewers" not in overrides:
        section["reviewers"].append(claude_reviewer())
    return section


def o8_config(
    folder: Path,
    *,
    server: str = "http://127.0.0.1:8000",
    execution: str = "container",
    workers: list[dict[str, Any]] | None = None,
    common_env: dict[str, str] | None = None,
    reviewers: list[dict[str, Any]] | None = None,
) -> DispatchConfig:
    """folder/dispatch.yaml with a codex worker (or the workers given) and spec 8.3's example repair: section (or
    the reviewers given), its token file holding FAKE_TOKEN and its copy root at folder/copies, written as YAML and
    loaded with DispatchConfig.load, as the dispatcher loads it."""
    token_file = write_token(folder / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
    root = folder / "copies"
    base = run_config(
        "codex", server, token_file, root, execution=execution, workers=workers, common_env=common_env, repair=False
    )
    payload = base.model_dump(exclude={"repair"})
    section = o8_section(token_path=str(token_file), transcript_root=str(root))
    if reviewers is not None:
        section["reviewers"] = reviewers
    payload["repair"] = section
    return DispatchConfig.load(write_dispatch_yaml(folder / "dispatch.yaml", payload))


def reviewer_entry(name: str, key: str | None = None, **settings: Any) -> dict[str, Any]:
    env = {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1"}
    if key is not None:
        env["REVIEW_API_KEY"] = key
    return {"name": name, "env": env, **settings}


def refused(path: Path, payload: dict[str, Any]) -> ValidationError:
    """The ValidationError that DispatchConfig.load raises for payload written as dispatch.yaml at path."""
    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(write_dispatch_yaml(path, payload))
    return caught.value


def _ds27_cases_give_the_same_errors(tmp_path: Path, build: Callable[[], dict[str, Any]]) -> None:
    """Each of DS27's cases (test_repair_shadow.py), applied to build()'s section, raises from DispatchConfig.load
    with the errors (type, location, message) that it gives applied to the example before O8, repair_section(), and
    with a message that names the case's key."""
    problems: list[str] = []
    for index, (what, change, key) in enumerate(_strict_cases()):
        results: list[Any] = []
        for label, section in (("before", repair_section()), ("o8", build())):
            change(section)
            try:
                DispatchConfig.load(
                    write_dispatch_yaml(tmp_path / f"case-{index}-{label}.yaml", dispatch_payload(section))
                )
            except ValidationError as exc:
                results.append((_errors(exc), f"\n{key}\n" in str(exc)))
            else:
                results.append(None)
        before, o8 = results
        if o8 is None:
            problems.append(f"{what}: loaded")
        elif o8 != before or not o8[1]:
            problems.append(f"{what}: {o8} where the example before O8 gives {before}")
    assert not problems, "\n".join(problems)


def _loading_opens_no_file_and_makes_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading a dispatch.yaml whose section has api_format opens the files that loading one without the section
    opens, makes no request, and touches neither the token file nor the copy root."""
    token_file = tmp_path / "secrets" / "repair-dispatcher.token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("x" * 40, encoding="utf-8")
    copy_root = tmp_path / "copies"
    without = write_dispatch_yaml(tmp_path / "plain.yaml", dispatch_payload())
    section = o8_section(token_path=str(token_file), transcript_root=str(copy_root))
    section["reviewers"][0]["api_format"] = "openai_chat"
    with_section = write_dispatch_yaml(tmp_path / "section.yaml", dispatch_payload(section))
    DispatchConfig.load(without)
    DispatchConfig.load(with_section)  # once each first, so that no lazy first-use work differs between the two
    with _file_opens_and_requests(monkeypatch) as (opened, connects):
        DispatchConfig.load(without)
        opened_without = [path.replace(str(without), "<dispatch.yaml>") for path in opened]
        opened.clear()
        loaded = DispatchConfig.load(with_section)
        opened_with = [path.replace(str(with_section), "<dispatch.yaml>") for path in opened]
    assert loaded.repair is not None
    assert [reviewer.api_format for reviewer in loaded.repair.reviewers] == ["openai_chat", None, "anthropic_messages"]
    assert opened_with == opened_without
    assert "<dispatch.yaml>" in opened_with
    assert connects == []
    assert not copy_root.exists()


# DS27 again, with spec 8.3's new example ------------------------------------------------------------------------------


def test_repair_section_is_optional_and_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DS27 again, with spec 8.3's example as O8 left it, whose third reviewer, reviewer_claude, has api_format
    anthropic_messages and its own key. The example, its placeholders replaced, loads; each of DS27's cases gives the
    errors it gives with the example before O8, and the same cases on reviewer_claude name its keys; a REVIEW_API_KEY
    that still holds a byte outside 0x20-0x7E once the whitespace around it is removed raises with a message that
    names the key, and one with only whitespace around it loads without it; a common_env holding OPENAI_API_KEY
    leaves every reviewer's env with its own keys only; and loading opens the same files with and without the
    section, makes no request and imports nothing under cairn.dispatcher.repair. DS27's checks that do not depend on
    the example (today's examples and errors, DispatchConfig's fields, a repair: key with no value) are in
    test_repair_shadow.py.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Spec 8.3's example, its placeholders replaced, loads.
    section = o8_section(token_path="~/cairn/secrets/repair-dispatcher.token")  # noqa: S106 - a path
    config = DispatchConfig.load(write_dispatch_yaml(tmp_path / "example.yaml", dispatch_payload(section)))
    repair = config.repair
    assert repair is not None
    assert repair.token_file == f"{home}/cairn/secrets/repair-dispatcher.token"
    assert repair.transcript_root == "/var/lib/cairn-repair/transcripts"
    assert repair.projects is None
    assert [reviewer.name for reviewer in repair.reviewers] == [
        "reviewer_local",
        "reviewer_provider",
        "reviewer_claude",
    ]
    local, provider, claude = repair.reviewers
    assert [local.api_format, provider.api_format, claude.api_format] == [None, None, "anthropic_messages"]
    assert type(claude.api_format) is str
    assert claude.env.model_dump() == {
        "REVIEW_MODEL": "test-model",
        "REVIEW_BASE_URL": CLAUDE_BASE_URL,
        "REVIEW_API_KEY": CLAUDE_KEY,
    }
    assert type(claude.env.REVIEW_API_KEY) is str
    assert local.env.REVIEW_API_KEY is None and provider.env.REVIEW_API_KEY == FAKE_REVIEWER_KEY
    assert provider.timeout_seconds == 180
    assert [setting for setting in C1_SETTINGS if getattr(claude, setting) is not None] == []
    assert config.model_dump(exclude={"repair"}) == make_config().model_dump(exclude={"repair"})

    # Each of DS27's cases gives, with this example, the errors it gives with the example before O8.
    _ds27_cases_give_the_same_errors(tmp_path, o8_section)

    # The same rules hold for reviewer_claude: the earlier design's Anthropic SDK settings and keys are unknown.
    third = "repair.reviewers.2"
    cases: list[tuple[Callable[[dict[str, Any]], Any], str]] = [
        (lambda s: s["reviewers"][2].__setitem__("provider", "anthropic"), f"{third}.provider"),
        (lambda s: s["reviewers"][2].__setitem__("effort", "high"), f"{third}.effort"),
        (lambda s: s["reviewers"][2].__setitem__("server_fallbacks", []), f"{third}.server_fallbacks"),
        (
            lambda s: s["reviewers"][2]["env"].__setitem__("ANTHROPIC_API_KEY", "x" * 12),
            f"{third}.env.ANTHROPIC_API_KEY",
        ),
        (lambda s: s["reviewers"][2]["env"].pop("REVIEW_BASE_URL"), f"{third}.env.REVIEW_BASE_URL"),
        (
            lambda s: s["reviewers"][2]["env"].__setitem__("REVIEW_API_KEY", "<the reviewer's own Claude API key>"),
            f"{third}.env.REVIEW_API_KEY",
        ),
        (lambda s: s["reviewers"][2].__setitem__("name", "reviewer claude"), f"{third}.name"),
    ]
    for index, (change, key) in enumerate(cases):
        section = o8_section()
        change(section)
        message = str(refused(tmp_path / f"third-{index}.yaml", dispatch_payload(section)))
        assert f"\n{key}\n" in message, message

    # A byte outside 0x20-0x7E that is still there once the whitespace around the key is removed is refused, the
    # message naming the key and printing no value; only the whitespace around a key is removed.
    for index, bad in enumerate(
        [
            " test-key-0123\x7f ",
            "\ttest-key-0123\x1f\n",
            " test-key-é-0123 ",
            "\r\ntest-key-\x00-0123\r\n",
            "\xa0test-key-0123\xa0",
            "\x1ftest-key-0123",
        ]
    ):
        section = o8_section()
        section["reviewers"][2]["env"]["REVIEW_API_KEY"] = bad
        caught = refused(tmp_path / f"bad-key-{index}.yaml", dispatch_payload(section))
        message = str(caught)
        assert [error["loc"] for error in caught.errors()] == [("repair", "reviewers", 2, "env", "REVIEW_API_KEY")]
        assert f"\n{third}.env.REVIEW_API_KEY\n" in message and "input_value=<hidden>," in message, message
        assert "test-key" not in message, message
    for index, (padded, kept) in enumerate(
        [
            (" test-key-0123 ", "test-key-0123"),
            ("\t\n test-key-0123\r\x0b\x0c", "test-key-0123"),
            ("   test key 0123   ", "test key 0123"),
        ]
    ):
        section = o8_section()
        section["reviewers"][2]["env"]["REVIEW_API_KEY"] = padded
        loaded = DispatchConfig.load(write_dispatch_yaml(tmp_path / f"padded-{index}.yaml", dispatch_payload(section)))
        assert loaded.repair is not None
        assert kept == loaded.repair.reviewers[2].env.REVIEW_API_KEY

    # common_env reaches the workers only: every reviewer's env keeps its own keys.
    payload = dispatch_payload(o8_section())
    payload["common_env"] = {"OPENAI_API_KEY": "test-common-key-0003", "SHARED": "1"}
    config = DispatchConfig.load(write_dispatch_yaml(tmp_path / "common-env.yaml", payload))
    assert config.repair is not None
    assert config.workers[0].env["OPENAI_API_KEY"] == "test-common-key-0003"
    assert [set(reviewer.env.model_dump()) for reviewer in config.repair.reviewers] == [
        {"REVIEW_MODEL", "REVIEW_BASE_URL", "REVIEW_API_KEY"}
    ] * 3
    for reviewer in config.repair.reviewers:
        assert "test-common-key-0003" not in json.dumps(reviewer.model_dump())
    assert config.repair.reviewers[2].env.REVIEW_API_KEY == CLAUDE_KEY

    # Loading opens the same files with and without the section and makes no request.
    _loading_opens_no_file_and_makes_no_request(tmp_path / "opens", monkeypatch)

    # Loading a file with this section imports nothing under cairn.dispatcher.repair (fresh interpreter).
    path = write_dispatch_yaml(tmp_path / "fresh.yaml", dispatch_payload(o8_section()))
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from cairn.dispatcher.config import DispatchConfig\n"
        "config = DispatchConfig.load(Path(sys.argv[1]))\n"
        "assert config.repair is not None and config.repair.reviewers[2].api_format == 'anthropic_messages'\n"
        "print(sorted(name for name in sys.modules if name.startswith('cairn.dispatcher.repair')))\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", probe, str(path)], capture_output=True, text=True, env=environment, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_whitespace_removed_around_a_reviewer_key_is_the_tokens(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec 8.3: the whitespace around REVIEW_API_KEY is removed as from the token, that is only the ASCII
    whitespace that bytes.strip() removes (repair/config.py). A key that another character still starts and ends
    once that whitespace is removed, such as 0x1F or a no-break space, is refused, as a token file holding the same
    is not_ascii. The shared-key check removes the same characters from worker values, and no others: of workers
    that each hold the key with one of the same characters before and after it, only those whose character is
    removed share it. A key written as YAML's !!binary follows the same rule, and one that is not UTF-8 is refused
    with its value hidden, as before O8: the removal hands the str field a value that still prints as <hidden>."""
    core = "test-key-" + "k" * 32  # long enough for a token too
    candidates = [chr(code) for code in range(0x21)] + ["\x7f", "\x85", "\xa0", "\u2000", "\u2028", "\u3000", "\ufeff"]
    token_file = tmp_path / "token"
    stripped_by_bytes = {chr(code) for code in range(256) if bytes([code]).strip() == b""}
    assert set(housekeeping.KEY_WHITESPACE) == stripped_by_bytes == set(" \t\n\r\x0b\x0c")
    for index, character in enumerate(candidates):
        padded = character + core + character
        assert yaml.safe_load(yaml.safe_dump({"k": padded})) == {"k": padded}  # YAML keeps the character
        token_file.write_bytes(padded.encode("utf-8"))
        state = repair_config._read_token(str(token_file))
        section = repair_section()
        section["reviewers"][1]["env"]["REVIEW_API_KEY"] = padded
        path = write_dispatch_yaml(tmp_path / f"key-{index}.yaml", dispatch_payload(section))
        try:
            repair = DispatchConfig.load(path).repair
        except ValidationError as exc:
            assert "\nrepair.reviewers.1.env.REVIEW_API_KEY\n" in str(exc), (hex(ord(character)), str(exc))
            loaded = None
        else:
            assert repair is not None
            loaded = repair.reviewers[1].env.REVIEW_API_KEY
        expected = core if character in stripped_by_bytes else None
        assert loaded == expected, hex(ord(character))
        assert (state.token if state.configured else None) == expected, hex(ord(character))

    # The shared-key check compares each worker value as written and with the same whitespace removed (spec 8.3,
    # Shared keys): one worker per character holds the key with it before and after it, and the one line names
    # exactly the workers whose character the key's rule removes. str.strip() would also name those of 0x1C-0x1F,
    # U+0085, U+00A0, U+2000, U+2028 and U+3000, characters that the key's rule does not remove.
    workers = [
        worker(f"w{index:02d}", OPENAI_API_KEY=character + core + character)
        for index, character in enumerate(candidates)
    ]
    config = o8_config(tmp_path / "shared", workers=workers, reviewers=[reviewer_entry("r_core", core)])
    assert [loaded_worker.env["OPENAI_API_KEY"] for loaded_worker in config.workers] == [
        character + core + character for character in candidates
    ]
    caplog.clear()
    housekeeping.check_shared_keys(config)
    holders = [f"w{index:02d}" for index, character in enumerate(candidates) if character in stripped_by_bytes]
    assert len(holders) == len(stripped_by_bytes)
    assert lines(caplog, SHARED) == [("r_core", ",".join(holders))]

    # YAML's !!binary gives bytes, which the str field decodes: the same rule holds for them, and one that is not
    # UTF-8 is refused with its value hidden, the whitespace around it removed or not.
    text = yaml.safe_dump(dispatch_payload(repair_section()), sort_keys=False)
    marker = f"REVIEW_API_KEY: {FAKE_REVIEWER_KEY}"
    assert text.count(marker) == 1

    def binary_key(name: str, raw: bytes) -> Path:
        path = tmp_path / name
        encoded = base64.b64encode(raw).decode() or '""'
        path.write_text(text.replace(marker, f"REVIEW_API_KEY: !!binary {encoded}"), encoding="utf-8")
        return path

    for index, (raw, expected) in enumerate([(b" \t" + core.encode() + b"\n", core), (b" \t\r\n", None), (b"", None)]):
        repair = DispatchConfig.load(binary_key(f"binary-{index}.yaml", raw)).repair
        assert repair is not None
        assert expected == repair.reviewers[1].env.REVIEW_API_KEY, raw
    for index, raw in enumerate([b" " + BINARY_CANARY.encode() + b"\xff ", BINARY_CANARY.encode() + b"\xff"]):
        with pytest.raises(ValidationError) as caught:
            DispatchConfig.load(binary_key(f"not-utf8-{index}.yaml", raw))
        message = str(caught.value)
        assert [error["loc"] for error in caught.value.errors()] == [
            ("repair", "reviewers", 1, "env", "REVIEW_API_KEY")
        ]
        assert "\nrepair.reviewers.1.env.REVIEW_API_KEY\n" in message and "input_value=<hidden>," in message, message
        assert BINARY_CANARY not in message and BINARY_CANARY not in str(caught.value.errors()), message


# DS31 ----------------------------------------------------------------------------------------------------------------


def _ds31_loads(tmp_path: Path) -> None:
    """A reviewer entry without api_format, or with it null, loads with api_format None; openai_chat and
    anthropic_messages load as written, the latter also without REVIEW_API_KEY or with structured_output: true,
    which the agent refuses (spec 9.2), not the dispatcher."""
    section = o8_section()
    section["reviewers"][0]["api_format"] = None
    section["reviewers"] += [
        reviewer_entry("reviewer_openai_chat", "test-openai-chat-key-0021", api_format="openai_chat"),
        reviewer_entry("reviewer_claude_without_key", api_format="anthropic_messages"),
        reviewer_entry(
            "reviewer_claude_structured",
            "test-claude-key-0022",
            api_format="anthropic_messages",
            structured_output=True,
        ),
    ]
    path = write_dispatch_yaml(tmp_path / "loads.yaml", dispatch_payload(section))
    assert "api_format: null" in path.read_text(encoding="utf-8")
    repair = DispatchConfig.load(path).repair
    assert repair is not None
    assert {reviewer.name: reviewer.api_format for reviewer in repair.reviewers} == {
        "reviewer_local": None,
        "reviewer_provider": None,
        "reviewer_claude": "anthropic_messages",
        "reviewer_openai_chat": "openai_chat",
        "reviewer_claude_without_key": "anthropic_messages",
        "reviewer_claude_structured": "anthropic_messages",
    }
    assert all(type(reviewer.api_format) is str for reviewer in repair.reviewers if reviewer.api_format is not None)
    assert repair.reviewers[4].env.REVIEW_API_KEY is None
    assert repair.reviewers[5].structured_output is True


def _ds31_refused(tmp_path: Path) -> None:
    """Each bad api_format raises from DispatchConfig.load with one error, whose message names
    repair.reviewers.<i>.api_format and shows the input as the section shows every value; the entry's name, model,
    base URL and key are canaries, and so is one bad value: no canary is in any message, or in the errors' inputs.
    The messages are not searched for the other bad values, which they hold as text of their own (the rule names
    openai_chat and anthropic_messages, and pydantic's message starts with "1 validation error").

    The error is pydantic's literal_error, whose rule names every value that loads: exactly openai_chat and
    anthropic_messages (API_FORMAT_RULE), so a third value that loaded would change it, whichever value it is. Among
    the bad values is openai_responses, the format of the O7 draft, which O8 withdrew from Phase 2."""
    bad_values: list[tuple[Any, str]] = [
        ("anthropic", "<hidden>"),
        ("chat", "<hidden>"),
        ("Anthropic_Messages", "<hidden>"),
        ("", "<hidden>"),
        (1, "<hidden>"),
        (True, "True"),  # a boolean, which hidden() prints as it is: it is no secret
        (["anthropic_messages"], "[<hidden>]"),
        (FORMAT_CANARY, "<hidden>"),
        ("openai_responses", "<hidden>"),  # the format of the withdrawn O7 draft
        ("openai", "<hidden>"),
    ]
    problems: list[str] = []
    for position in ("first", "last"):
        for number, (value, shown) in enumerate(bad_values):
            section = o8_section()
            entry = {**copy.deepcopy(CANARY_ENTRY), "api_format": value}
            if position == "first":
                section["reviewers"].insert(0, entry)
            else:
                section["reviewers"].append(entry)
            index = section["reviewers"].index(entry)
            caught = refused(tmp_path / f"refused-{position}-{number}.yaml", dispatch_payload(section))
            message = str(caught)
            errors = caught.errors()
            if [error["loc"] for error in errors] != [("repair", "reviewers", index, "api_format")]:
                problems.append(f"{value!r} at {index}: {[error['loc'] for error in errors]}")
            if [(error["type"], error["msg"]) for error in errors] != [("literal_error", API_FORMAT_RULE)]:
                problems.append(f"{value!r} at {index}: {[(error['type'], error['msg']) for error in errors]}")
            if f"\n  {API_FORMAT_RULE} [type=literal_error, input_value={shown}, " not in message:
                problems.append(f"{value!r} at {index}: the message does not state the two values' rule:\n{message}")
            if not message.startswith("1 validation error"):
                problems.append(f"{value!r} at {index}: {message}")
            if f"\nrepair.reviewers.{index}.api_format\n" not in message:
                problems.append(f"{value!r} at {index}: the message does not name the key:\n{message}")
            if f"input_value={shown}," not in message:
                problems.append(f"{value!r} at {index}: the input is not shown as {shown}:\n{message}")
            for canary in DS31_CANARIES:
                if canary in message or canary in str(errors):
                    problems.append(f"{value!r} at {index}: {canary} is printed:\n{message}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case", ["loads", "refused", "ds27_cases"])
def test_reviewer_api_format_is_one_of_two_values(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DS31, case by case: what loads (_ds31_loads); what is refused, and what its messages show (_ds31_refused);
    and, with api_format added to entries, DS27's cases give the same results, and loading still opens no file and
    makes no request (ds27_cases)."""
    if case == "loads":
        _ds31_loads(tmp_path)
    elif case == "refused":
        _ds31_refused(tmp_path)
    else:

        def with_api_format() -> dict[str, Any]:
            section = repair_section()
            section["reviewers"][0]["api_format"] = "openai_chat"
            section["reviewers"][1]["api_format"] = "anthropic_messages"
            return section

        _ds27_cases_give_the_same_errors(tmp_path, with_api_format)
        _loading_opens_no_file_and_makes_no_request(tmp_path / "opens", monkeypatch)


# DS32 ----------------------------------------------------------------------------------------------------------------


def _start(config: DispatchConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The housekeeping thread's body at a dispatcher start, in a fresh process state: the token, the shared keys,
    the settings file, and one removal pass."""
    reset_repair_process_state(monkeypatch)
    stop = threading.Event()
    stop.set()  # the first wait after the first removal pass ends the thread's body
    monkeypatch.setattr(housekeeping, "_STOP", stop)
    housekeeping._housekeeping(config)


def _ds32_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """With spec 8.3's example and one more entry whose api_format is null, every reviewer of agent-settings.json
    has api_format: openai_chat for the entries without it and for the null one, and anthropic_messages for
    reviewer_claude, whose api_key is written as any reviewer's; no other key is added (DS28), and each reviewer's
    keys come in the order of spec 8.3's example of the file."""
    reviewers = o8_section()["reviewers"] + [reviewer_entry("reviewer_null_format", api_format=None)]
    config = o8_config(tmp_path, reviewers=reviewers)
    _start(config, monkeypatch)
    path = settings_path(config)
    content = json.loads(path.read_bytes())
    assert list(content) == ["version", "server", "projects", "reviewers"]
    assert content["version"] == 1 and content["server"] == "http://127.0.0.1:8000" and content["projects"] is None
    assert content["reviewers"] == [
        {
            "name": "reviewer_local",
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": None,
            "api_format": "openai_chat",
        },
        {
            "name": "reviewer_provider",
            "model": "test-model",
            "base_url": "https://provider.invalid/v1",
            "api_key": FAKE_REVIEWER_KEY,
            "api_format": "openai_chat",
            "timeout_seconds": 180,
        },
        {
            "name": "reviewer_claude",
            "model": "test-model",
            "base_url": CLAUDE_BASE_URL,
            "api_key": CLAUDE_KEY,
            "api_format": "anthropic_messages",
        },
        {
            "name": "reviewer_null_format",
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": None,
            "api_format": "openai_chat",
        },
    ]
    first_keys = ["name", "model", "base_url", "api_key", "api_format"]
    assert [list(reviewer) for reviewer in content["reviewers"]] == [
        first_keys,
        [*first_keys, "timeout_seconds"],
        first_keys,
        first_keys,
    ]
    assert lines(caplog, WRITTEN) == [(str(path), 4)]
    assert lines(caplog, NOT_WRITTEN) == []


def _ds32_start_up_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """An anthropic_messages reviewer meets the start-up checks as any reviewer does: its key equal to a worker's
    logs the shared-key line with reviewer=reviewer_claude, and the file is written; its key equal to the dispatcher
    token, as written or with whitespace around it, writes no file (error=reviewer_key_is_token), removes the
    earlier one, and logs no shared-key line."""
    workers = [worker("codex-1", OPENAI_API_KEY=CLAUDE_KEY), worker("pi-1", "pi"), worker("codex-2")]
    config = o8_config(tmp_path / "shared", workers=workers)
    caplog.clear()
    _start(config, monkeypatch)
    assert lines(caplog, SHARED) == [("reviewer_claude", "codex-1")]
    written = json.loads(settings_path(config).read_bytes())["reviewers"]
    assert written[2] == {
        "name": "reviewer_claude",
        "model": "test-model",
        "base_url": CLAUDE_BASE_URL,
        "api_key": CLAUDE_KEY,
        "api_format": "anthropic_messages",
    }

    for index, key in enumerate([FAKE_TOKEN, f" {FAKE_TOKEN} ", f"\t{FAKE_TOKEN}\n"]):
        folder = tmp_path / f"token-{index}"
        earlier = o8_config(folder)
        _start(earlier, monkeypatch)
        path = settings_path(earlier)
        assert path.exists()
        reviewers = o8_section()["reviewers"]
        reviewers[2]["env"]["REVIEW_API_KEY"] = key
        config = o8_config(folder, reviewers=reviewers)
        caplog.clear()
        _start(config, monkeypatch)
        assert not path.exists()
        assert lines(caplog, NOT_WRITTEN) == [(str(path), "reviewer_key_is_token")]
        assert lines(caplog, SHARED) == []
        assert lines(caplog, WRITTEN) == []
        assert config.repair is not None and config.repair.reviewers[2].env.REVIEW_API_KEY == FAKE_TOKEN


def _ds32_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A REVIEW_API_KEY written with a space before and after it (a quoted YAML value) is written to the settings
    file without them; a worker env value equal to it without them logs the shared-key line, and so does one written
    with the same spaces; one of spaces only is written as null. Only the whitespace removed around the key is
    removed around a worker value: one that holds a key with a no-break space, 0x1F, U+2000 or U+3000 around it,
    which str.strip() would remove, does not share it, while one with ASCII whitespace around it does. In local mode
    a variable of the dispatcher's environment written with spaces around a key matches it too, and one with a
    no-break space or 0x1F around it does not (spec 8.3, Shared keys: each value compared as written and with its
    surrounding whitespace removed, as the reviewer key is)."""
    reviewers = [
        reviewer_entry("r_worker_bare", f" {SPACED_WORKER_KEY} "),
        reviewer_entry("r_worker_spaced", f" {SPACED_BOTH_KEY} ", api_format="anthropic_messages"),
        reviewer_entry("r_nobody", f"  {SPACED_LONE_KEY}  "),
        reviewer_entry("r_spaces_only", "   "),
        reviewer_entry("r_unicode", f" {UNICODE_SPACED_KEY} "),
    ]
    unicode_padded = [
        f"\xa0{UNICODE_SPACED_KEY}\xa0",  # a no-break space
        f"\x1f{UNICODE_SPACED_KEY}\x1f",  # the unit separator, which str.isspace() counts as whitespace
        f"\u2000{UNICODE_SPACED_KEY}\u3000",  # an en quad and an ideographic space
    ]
    workers = [
        worker("w1", OPENAI_API_KEY=SPACED_WORKER_KEY),
        worker("w2", "pi", PI_API_KEY=f" {SPACED_BOTH_KEY} "),
        worker("w3", OPENAI_API_KEY=f"x{SPACED_LONE_KEY}"),
        worker("w4", OPENAI_API_KEY=unicode_padded[0]),
        worker("w5", "pi", PI_API_KEY=unicode_padded[1]),
        worker("w6", OPENAI_API_KEY=unicode_padded[2]),
        worker("w7", OPENAI_API_KEY=f"\t{UNICODE_SPACED_KEY}\r\n"),  # ASCII whitespace, which the key's rule removes
    ]
    config = o8_config(tmp_path / "container", workers=workers, reviewers=reviewers)
    text = (tmp_path / "container" / "dispatch.yaml").read_text(encoding="utf-8")
    assert f"REVIEW_API_KEY: ' {SPACED_WORKER_KEY} '" in text  # a quoted YAML value
    assert f"PI_API_KEY: ' {SPACED_BOTH_KEY} '" in text
    assert config.workers[1].env["PI_API_KEY"] == f" {SPACED_BOTH_KEY} "  # worker values load as today: as written
    # The Unicode-padded values load as written, and are among the values that the check compares.
    for loaded, value in zip(config.workers[3:6], unicode_padded, strict=True):
        assert value in redaction.environment_values(loaded.env)
    caplog.clear()
    _start(config, monkeypatch)
    raw = settings_path(config).read_bytes()
    written = {reviewer["name"]: reviewer["api_key"] for reviewer in json.loads(raw)["reviewers"]}
    # The settings file and the shared-key lines, compared together, so that a failure shows both.
    assert (written, lines(caplog, SHARED)) == (
        {
            "r_worker_bare": SPACED_WORKER_KEY,
            "r_worker_spaced": SPACED_BOTH_KEY,
            "r_nobody": SPACED_LONE_KEY,
            "r_spaces_only": None,
            "r_unicode": UNICODE_SPACED_KEY,
        },
        [("r_worker_bare", "w1"), ("r_worker_spaced", "w2"), ("r_unicode", "w7")],
    )
    for key in (SPACED_WORKER_KEY, SPACED_BOTH_KEY, SPACED_LONE_KEY, UNICODE_SPACED_KEY):
        assert f'"{key}"'.encode() in raw

    monkeypatch.setenv("CAIRN_TEST_SPACED_TOKEN", f"  {SPACED_ENVIRONMENT_KEY}\t")
    environment_padded = [f"\xa0{UNICODE_SPACED_ENVIRONMENT_KEY}\xa0", f"\x1f{UNICODE_SPACED_ENVIRONMENT_KEY}\x1f"]
    monkeypatch.setenv("CAIRN_TEST_NBSP_TOKEN", environment_padded[0])
    monkeypatch.setenv("CAIRN_TEST_UNIT_SEPARATOR_TOKEN", environment_padded[1])
    assert set(environment_padded) <= redaction.environment_values(os.environ)
    local_workers = [{**worker("w1"), "env": {}}]
    local = o8_config(
        tmp_path / "local",
        execution="local",
        workers=local_workers,
        reviewers=[
            reviewer_entry("r_environment", SPACED_ENVIRONMENT_KEY),
            reviewer_entry("r_environment_spaced", f" {SPACED_ENVIRONMENT_KEY} "),
            reviewer_entry("r_environment_unicode", UNICODE_SPACED_ENVIRONMENT_KEY),
        ],
    )
    caplog.clear()
    _start(local, monkeypatch)
    assert lines(caplog, SHARED) == [("r_environment", "environment"), ("r_environment_spaced", "environment")]


def _ds32_refusal(tmp_path: Path) -> None:
    """A REVIEW_API_KEY of " test-leak-canary-0008\\x01 ", padded and holding a control character, raises from
    DispatchConfig.load with a message that names repair.reviewers.<i>.env.REVIEW_API_KEY, shows
    input_value=<hidden> and holds no canary, as step 5's test_repair_section_errors_name_the_key_but_print_no_value
    checks for a key without spaces; for an openai_chat and an anthropic_messages reviewer alike."""
    for index in (1, 2):
        section = o8_section()
        section["reviewers"][index]["env"]["REVIEW_API_KEY"] = f" {LEAK_CANARY}\x01 "
        caught = refused(tmp_path / f"leak-{index}.yaml", dispatch_payload(section))
        message = str(caught)
        assert [error["loc"] for error in caught.errors()] == [("repair", "reviewers", index, "env", "REVIEW_API_KEY")]
        assert f"\nrepair.reviewers.{index}.env.REVIEW_API_KEY\n" in message, message
        assert "input_value=<hidden>," in message, message
        assert LEAK_CANARY not in message, message
        assert LEAK_CANARY not in str(caught.errors()), caught.errors()


@pytest.mark.parametrize("case", ["content", "start_up_checks", "spaces", "refusal"])
def test_settings_file_carries_each_reviewers_api_format(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DS32, case by case: the api_format of every reviewer in the settings file (_ds32_content); the start-up checks
    of an anthropic_messages reviewer (_ds32_start_up_checks); keys written with whitespace around them
    (_ds32_spaces); and the refusal of a padded key that holds a control character (_ds32_refusal)."""
    if case == "content":
        _ds32_content(tmp_path, monkeypatch, caplog)
    elif case == "start_up_checks":
        _ds32_start_up_checks(tmp_path, monkeypatch, caplog)
    elif case == "spaces":
        _ds32_spaces(tmp_path, monkeypatch, caplog)
    else:
        _ds32_refusal(tmp_path)


# TC4: a reviewer key written with spaces around it ---------------------------------------------------------------------


def _printed(*texts: str) -> bytes:
    """codex's pinned session file with lines appended that hold texts, as a tool's output would."""
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "tool_output", "text": text}}) for text in texts]
    return fixture_bytes("codex") + "".join(line + "\n" for line in lines).encode()


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, its case of O8: a REVIEW_API_KEY written with a space before and after it (a quoted YAML value) and
    printed by a worker without them, bare and JSON-escaped, becomes [REDACTED:REVIEW_API_KEY] in every form and is
    counted under that name. TC4's other cases are in test_repair_transcripts.py."""
    reviewers = [reviewer_entry("reviewer_spaced", f" {SPACED_COPY_KEY} ")]
    config = o8_config(tmp_path, server="http://127.0.0.1:9", reviewers=reviewers)
    assert f"REVIEW_API_KEY: ' {SPACED_COPY_KEY} '" in (tmp_path / "dispatch.yaml").read_text(encoding="utf-8")
    printed = _printed(f"reviewer key {SPACED_COPY_KEY}", json.dumps({"REVIEW_API_KEY": SPACED_COPY_KEY}))
    path = container_session_path("codex", SESSION_IDS["codex"])
    api = install_docker(monkeypatch, FakeDockerApi({path: FakeFile(printed)}))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    try:
        clock.at(1260)  # 70% of the run's 1800 s budget: the first copy
        schedule.tick()
    finally:
        api.close()
    copied = read_transcript(config)
    forms = redaction._forms(SPACED_COPY_KEY)
    assert len(forms) >= 3  # the raw value and its JSON-escaped forms differ
    for form in forms:
        assert form not in copied, form
    assert b"test-spaced-reviewer-key" not in copied
    # Once in the line that holds it bare (escaped once by the session line), once in the printed json.dumps form
    # (escaped once more inside the line).
    assert copied.count(b"[REDACTED:REVIEW_API_KEY]") == 2
    assert read_meta(config)["redactions"]["REVIEW_API_KEY"] == 2
    assert config.repair is not None and config.repair.reviewers[0].env.REVIEW_API_KEY == SPACED_COPY_KEY
