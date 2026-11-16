"""The settings file's `api_format` (spec §9.2, owner decision O8): AG59, and AG54 again with §9.2's example, which
now lists an `anthropic_messages` reviewer.

`api_format` is `openai_chat` or `anthropic_messages`, `openai_chat` when left out or null. With
`anthropic_messages` the reviewer's key is required and `structured_output` must be left out, null or false. No
reviewer's key may start or end with a space. Every refusal names the reviewer and the field, never a value.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from conftest import REVIEWER_KEY, SECOND_REVIEWER_KEY, Layout, reviewer_entry, settings_data

from cairn_repair_agent import cli, config, model

CLAUDE_KEY = "fake-messages-api-key-" + "0123456789abcdef" * 2


def spec_example_with_a_claude_reviewer() -> dict[str, Any]:
    """§9.2's example, with its placeholders filled in: a local reviewer, a hosted one and a Claude one."""
    return {
        "version": 1,
        "server": "http://127.0.0.1:8000",
        "projects": None,
        "reviewers": [
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
                "base_url": "https://provider.example/v1",
                "api_key": SECOND_REVIEWER_KEY,
                "api_format": "openai_chat",
                "timeout_seconds": 180,
            },
            {
                "name": "reviewer_claude",
                "model": "test-model",
                "base_url": "https://api.anthropic.com",
                "api_key": CLAUDE_KEY,
                "api_format": "anthropic_messages",
            },
        ],
    }


# A server address where nothing listens: an agent that starts when it should not polls it once and exits 0 (the
# test then fails), where without --once it would poll forever (the test would hang).
NO_SERVER = "http://127.0.0.1:9"


def main_stderr(layout: Layout, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """`cairn-repair` with this layout, as far as its exit: 2 for a refusal."""
    code = cli.main(layout.argv("--allow-shared-user", "--once"))
    return code, capsys.readouterr().err


# --- AG59 -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("position", [0, 1])
def test_settings_file_api_format_rules(layout: Layout, position: int) -> None:
    """AG59: a reviewer without `api_format`, or with it null, reads as `openai_chat`; `openai_chat` and
    `anthropic_messages` start; with `anthropic_messages`, `structured_output` false, null or left out starts and is
    false, so no schema is sent."""
    variants: list[dict[str, Any]] = [
        {},
        {"api_format": None},
        {"api_format": "openai_chat"},
        {"api_format": "anthropic_messages", "api_key": REVIEWER_KEY},
        {"api_format": "anthropic_messages", "api_key": REVIEWER_KEY, "structured_output": False},
        {"api_format": "anthropic_messages", "api_key": REVIEWER_KEY, "structured_output": None},
    ]
    for variant in variants:
        reviewers = [reviewer_entry("reviewer_local"), reviewer_entry("reviewer_second")]
        reviewers[position].update(variant)
        layout.write_settings(settings_data(reviewers=reviewers))
        run = layout.start("--allow-shared-user")
        assert run.started, (variant, run.stderr)
        settings = config.parse_settings(settings_data(reviewers=reviewers), "agent-settings.json")
        reviewer = settings.reviewers[position]
        expected_format = variant.get("api_format") or "openai_chat"
        assert reviewer.api_format == expected_format
        assert reviewer.structured_output is (expected_format == "openai_chat")
        body = model.request_body(reviewer, "a digest")
        if expected_format == "anthropic_messages":
            assert set(body) == {"model", "max_tokens", "system", "messages"}
        else:
            assert "response_format" in body


API_FORMAT_REFUSALS: dict[str, tuple[dict[str, Any], str, str]] = {
    "unknown_anthropic": ({"api_format": "anthropic"}, "api_format", config.SETTINGS_RULES["api_format"]),
    "unknown_chat": ({"api_format": "chat"}, "api_format", config.SETTINGS_RULES["api_format"]),
    "wrong_case": ({"api_format": "Anthropic_Messages"}, "api_format", config.SETTINGS_RULES["api_format"]),
    "empty": ({"api_format": ""}, "api_format", config.SETTINGS_RULES["api_format"]),
    "a_number": ({"api_format": 1}, "api_format", config.SETTINGS_RULES["api_format"]),
    "true": ({"api_format": True}, "api_format", config.SETTINGS_RULES["api_format"]),
    "a_list": ({"api_format": ["openai_chat"]}, "api_format", config.SETTINGS_RULES["api_format"]),
    "anthropic_with_a_null_key": (
        {"api_format": "anthropic_messages", "api_key": None},
        "api_key",
        "required with api_format anthropic_messages",
    ),
    "anthropic_without_a_key": (
        {"api_format": "anthropic_messages", "api_key": "<left out>"},
        "api_key",
        "required with api_format anthropic_messages",
    ),
    "openai_key_with_a_leading_space": ({"api_key": " " + REVIEWER_KEY}, "api_key", config.SETTINGS_RULES["api_key"]),
    "openai_key_with_a_trailing_space": ({"api_key": REVIEWER_KEY + " "}, "api_key", config.SETTINGS_RULES["api_key"]),
    "anthropic_key_with_a_leading_space": (
        {"api_format": "anthropic_messages", "api_key": " " + REVIEWER_KEY},
        "api_key",
        config.SETTINGS_RULES["api_key"],
    ),
    "anthropic_key_with_a_trailing_space": (
        {"api_format": "anthropic_messages", "api_key": REVIEWER_KEY + " "},
        "api_key",
        config.SETTINGS_RULES["api_key"],
    ),
    "anthropic_with_structured_output_true": (
        {"api_format": "anthropic_messages", "api_key": REVIEWER_KEY, "structured_output": True},
        "structured_output",
        "must be left out, null or false with api_format anthropic_messages",
    ),
}


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("case", list(API_FORMAT_REFUSALS))
def test_settings_file_api_format_rules_refusals(
    layout: Layout, capsys: pytest.CaptureFixture[str], case: str, position: int
) -> None:
    """AG59: each of these stops the agent with exit 2 and `agent settings invalid: <path>: reviewers[<i>].<field>:
    reviewer <name>: <rule>`, naming the reviewer and the field and never a value, for the first reviewer and for the
    second: an unknown `api_format` or one that is not a string; `anthropic_messages` with `api_key` null or left out;
    an `api_key` that starts or ends with a space, with either format; and `anthropic_messages` with
    `structured_output: true`."""
    change, field, rule = API_FORMAT_REFUSALS[case]
    reviewers = [reviewer_entry("reviewer_local"), reviewer_entry("reviewer_second")]
    reviewers[position].update(change)
    if reviewers[position].get("api_key") == "<left out>":
        del reviewers[position]["api_key"]
    layout.write_settings(settings_data(server=NO_SERVER, reviewers=reviewers))
    code, stderr = main_stderr(layout, capsys)
    name = reviewers[position]["name"]
    assert code == 2
    assert (
        stderr
        == f"agent settings invalid: {layout.settings_path}: reviewers[{position}].{field}: reviewer {name}: {rule}\n"
    )
    assert REVIEWER_KEY not in stderr


def test_settings_file_api_format_rules_texts() -> None:
    """AG59 and build step 9: the `api_key` rule keeps the start the earlier tests check and names the spaces; the
    rules of anthropic_messages have texts of their own, since the shared ones state rules that null and true meet;
    the `base_url` rule names each format's path; and the refusal of the removed Anthropic client's keys says that the
    one client makes the call `api_format` names, and that a Claude API key takes `api_format: anthropic_messages`."""
    rules = config.SETTINGS_RULES
    assert rules["api_key"] == (
        "must be null or a non-empty string of printable ASCII (0x20-0x7E) that neither starts nor ends with a space"
    )
    assert "/chat/completions for openai_chat" in rules["base_url"]
    assert "/v1/messages for anthropic_messages" in rules["base_url"]
    for key in ("provider", "effort", "server_fallbacks"):
        with pytest.raises(config.StartupError) as caught:
            config.parse_settings(settings_data(reviewers=[reviewer_entry(**{key: "x"})]), "f")
        message = str(caught.value)
        assert message.startswith(
            f"agent settings invalid: f: reviewers[0].{key}: reviewer reviewer_local: unknown key"
        )
        assert "makes the call that the reviewer's api_format names" in message
        assert "a Claude API key takes api_format: anthropic_messages" in message
        assert "OpenAI-compatible one" not in message


# --- AG54 with §9.2's example ---------------------------------------------------------------------------------------


def test_settings_file_is_read_safely_and_strictly_spec_example_with_a_claude_reviewer(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG54 with §9.2's example, which lists a Claude reviewer: it starts, every reviewer is read with its format, the
    live agent's client is the first reviewer's, and the file is read once, so rewriting it after the start changes
    nothing until the agent restarts. No request is sent at startup."""
    layout.write_settings(spec_example_with_a_claude_reviewer())
    opened: list[str] = []
    real_open = os.open

    def counting_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.path.basename(os.fspath(path)) == config.SETTINGS_FILE_NAME:
            opened.append(os.fspath(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", counting_open)
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert opened == [config.SETTINGS_FILE_NAME]
    reviewers = agent.settings_file.settings.reviewers
    assert [(r.name, r.api_format) for r in reviewers] == [
        ("reviewer_local", "openai_chat"),
        ("reviewer_provider", "openai_chat"),
        ("reviewer_claude", "anthropic_messages"),
    ]
    assert reviewers[2].api_key == CLAUDE_KEY and reviewers[2].structured_output is False
    assert agent.model.reviewer.name == "reviewer_local"
    assert agent.model.url == "http://127.0.0.1:11434/v1/chat/completions"
    assert "Authorization" not in agent.model.session.headers
    claude = model.build_model_client(reviewers[2])  # the replay's client for it; no request is sent here
    assert claude.url == "https://api.anthropic.com/v1/messages"
    assert claude.session.headers["x-api-key"] == CLAUDE_KEY
    assert claude.session.headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in claude.session.headers
    assert CLAUDE_KEY not in repr(agent) and CLAUDE_KEY not in repr(claude)

    layout.write_settings(settings_data(reviewers=[reviewer_entry("rewritten", base_url="http://127.0.0.2:9/v1")]))
    assert agent.settings_file.settings.reviewers[0].name == "reviewer_local"
    restarted = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert restarted.model.reviewer.name == "rewritten"
    assert opened == [config.SETTINGS_FILE_NAME, config.SETTINGS_FILE_NAME]
    assert json.loads(layout.settings_path.read_text())["reviewers"][0]["name"] == "rewritten"
