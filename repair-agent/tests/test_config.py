"""The agent's configuration (spec §9.2, Appendix C-1, C-2): AG2, AG46, AG47, AG53 and AG54.

`repair.yaml` holds only the agent's own settings; the reviewers, the server URL and the `projects` list come from
the settings file that the dispatcher writes into the copy root. Every startup error stops the agent with exit code
2 and names a field and a rule, never a value: a token, a key or a model name never appears in one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    AGENT_GID,
    AGENT_TOKEN,
    DISPATCHER_UID,
    IS_ROOT,
    REVIEWER_KEY,
    SECOND_REVIEWER_KEY,
    HostInstall,
    Layout,
    reviewer_entry,
    set_mode,
    settings_data,
    spec_example_settings,
    write_bytes,
)

from cairn_repair_agent import cli, config

PROJECT = Path(__file__).resolve().parents[1]
MODEL_RULE = config.MODEL_NAME_RULE_TEXT
# Names of environment variables that hold the agent's credential in these tests (never a value).
CREDENTIAL_VARIABLE = "CAIRN_REPAIR_TEST_AGENT_CREDENTIAL"
UNSET_CREDENTIAL_VARIABLE = "CAIRN_REPAIR_TEST_AGENT_CREDENTIAL_UNSET"
COMPOSE_CREDENTIAL_PATH = "/run/secrets/repair-agent.token"
HOST_CREDENTIAL_PATH = "/etc/cairn-repair/agent.token"
# A lone surrogate outside surrogateescape's U+DC80-U+DCFF: a YAML or JSON escape gives one, and os.fsencode, so every
# os call, refuses it with UnicodeEncodeError (a ValueError), not OSError.
UNENCODABLE = "\ud800"


def run_main(layout: Layout, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    """`cairn-repair --config …` up to its startup's end; returns the exit code and standard error."""
    code = cli.main(layout.argv(*extra))
    return code, capsys.readouterr().err


def without(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in mapping.items() if name != key}


# --- AG2: repair.yaml -----------------------------------------------------------------------------------------------


AG2_CASES = {
    "unknown_key_a_pasted_workers_block": (
        lambda lay: lay.write_config(workers=[{"name": "codex-1", "env": {"OPENAI_API_KEY": "sk-not-real"}}]),
        "agent config invalid: {config}: workers: unknown key",
    ),
    "unknown_nested_key": (
        lambda lay: lay.write_config(transcripts={"root": str(lay.root), "retention_days": 3}),
        "agent config invalid: {config}: transcripts.retention_days: unknown key",
    ),
    "short_token": (
        lambda lay: write_bytes(lay.token_path, "t" * 31, mode=0o600),
        "agent token refused: token_file {token}: shorter_than_32",
    ),
    "no_token_source": (
        lambda lay: lay.write_config(without(lay.config, "token_file")),
        "agent config invalid: {config}: token_file: required: set token_file, or token_env",
    ),
    "two_token_sources": (
        lambda lay: lay.write_config(token_env=CREDENTIAL_VARIABLE),
        "agent config invalid: {config}: token_env: set token_file or token_env, not both",
    ),
    "token_file_missing": (
        lambda lay: lay.token_path.unlink(),
        "agent token unreadable: token_file {token}: ENOENT",
    ),
    "token_env_not_set": (
        lambda lay: lay.write_config(without(lay.config, "token_file"), token_env=UNSET_CREDENTIAL_VARIABLE),
        f"agent token missing: token_env {UNSET_CREDENTIAL_VARIABLE} is not set",
    ),
    "missing_dispatch_config_to_check_key": (
        lambda lay: lay.write_config(without(lay.config, "dispatch_config_to_check")),
        "agent config invalid: {config}: dispatch_config_to_check: required",
    ),
    "missing_transcripts_root": (
        lambda lay: lay.write_config(transcripts={}),
        "agent config invalid: {config}: transcripts.root: required",
    ),
    "transcripts_root_not_a_directory": (
        lambda lay: lay.write_config(transcripts={"root": str(lay.base / "nowhere")}),
        "agent config invalid: {config}: transcripts.root: required: an existing directory",
    ),
    "transcripts_root_the_file_system_cannot_encode": (
        lambda lay: lay.write_config(transcripts={"root": str(lay.base / UNENCODABLE)}),
        "agent config invalid: {config}: transcripts.root: required: an existing directory",
    ),
    "server_ca_bundle_the_file_system_cannot_encode": (
        lambda lay: lay.write_config(server_ca_bundle=str(lay.base / f"{UNENCODABLE}.pem")),
        "agent config invalid: {config}: server_ca_bundle: must be null or a readable file",
    ),
    "poll_interval_out_of_range": (
        lambda lay: lay.write_config(poll_interval=31),
        "agent config invalid: {config}: poll_interval: must be an integer from 2 to 30",
    ),
    "max_concurrent_reviews_out_of_range": (
        lambda lay: lay.write_config(max_concurrent_reviews=0),
        "agent config invalid: {config}: max_concurrent_reviews: must be an integer from 1 to 16",
    ),
    "projects_key": (
        lambda lay: lay.write_config(projects=["proj_001"]),
        "agent config invalid: {config}: projects: projects is set in the repair: section of dispatch.yaml now",
    ),
    "model_key": (
        lambda lay: lay.write_config(model={"name": "test-model", "base_url": "http://127.0.0.1:11434/v1"}),
        "agent config invalid: {config}: model: a model is configured as a reviewer entry under repair.reviewers, "
        "in the repair: section of dispatch.yaml",
    ),
    "server_key": (
        lambda lay: lay.write_config(server="http://127.0.0.1:8000"),
        "agent config invalid: {config}: server: the server is the top-level server of dispatch.yaml now",
    ),
}


@pytest.mark.parametrize("case", list(AG2_CASES))
def test_config_refuses_unknown_keys_short_tokens_and_a_missing_key(
    layout: Layout, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """AG2: an unknown key (a pasted `workers:` block), a short or missing token, a missing
    `dispatch_config_to_check` key, and the keys repair.yaml held before O1, each with where it is set now. A
    `transcripts.root` or `server_ca_bundle` that the file system cannot encode gets its field's rule, never a
    traceback."""
    assert layout.start("--allow-shared-user").started  # the layout itself is valid
    change, expected = AG2_CASES[case]
    change(layout)
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr.startswith(expected.format(config=layout.config_path, token=layout.token_path)), stderr
    assert stderr.count("\n") == 1
    assert AGENT_TOKEN not in stderr and "t" * 31 not in stderr and "sk-not-real" not in stderr


# --- AG53: the token ----------------------------------------------------------------------------------------------


TOKEN_REFUSALS = {
    "31_characters": ("t" * 31, "shorter_than_32"),
    "a_delete_character": ("t" * 20 + "\x7f" + "t" * 20, "not_ascii"),
    "a_non_ascii_letter": ("t" * 20 + "é" + "t" * 20, "not_ascii"),
    "a_tab_inside": ("t" * 20 + "\t" + "t" * 20, "not_ascii"),
    "an_escape_inside": ("t" * 20 + "\x1b" + "t" * 20, "not_ascii"),
    "short_and_not_ascii_reports_not_ascii_first": ("ttt\x7fttt", "not_ascii"),  # the server's order (§7)
    "empty": ("", "shorter_than_32"),
}


@pytest.mark.parametrize("source", ["token_file", "token_env"])
@pytest.mark.parametrize("case", list(TOKEN_REFUSALS))
def test_agent_token_rules(
    layout: Layout, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, case: str, source: str
) -> None:
    """AG53: a token of 31 characters, or with a character outside 0x20-0x7E, is a startup error naming
    `shorter_than_32` or `not_ascii`, never the value."""
    value, reason = TOKEN_REFUSALS[case]
    if source == "token_file":
        write_bytes(layout.token_path, value.encode("utf-8") + b"\n", mode=0o600)
        expected = f"agent token refused: token_file {layout.token_path}: {reason}"
    else:
        monkeypatch.setenv(CREDENTIAL_VARIABLE, value)
        layout.write_config(without(layout.config, "token_file"), token_env=CREDENTIAL_VARIABLE)
        expected = f"agent token refused: token_env {CREDENTIAL_VARIABLE}: {reason}"
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr == expected + "\n"
    if value:
        assert value not in stderr
    special = value.strip("t")
    if special:
        assert special not in stderr


def test_agent_token_rules_file_modes_and_whitespace(layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    """AG53: a token file of mode 0640 logs `repair agent token file readable by others` once, with
    `mode=0640`, and its token is used; 0600 logs nothing; surrounding whitespace is removed. The model key
    file and its WARNING are gone (O1): no other line speaks of a readable key."""
    write_bytes(layout.token_path, f"  {AGENT_TOKEN} \n", mode=0o640)
    run = layout.start("--allow-shared-user")
    assert run.started and run.token == AGENT_TOKEN
    warnings = run.messages("repair agent token file readable by others")
    assert warnings == [f"repair agent token file readable by others file={layout.token_path} mode=0640"]
    assert [m for m in run.messages() if "readable by others" in m] == warnings
    assert AGENT_TOKEN not in run.text()

    set_mode(layout.token_path, 0o600)
    run = layout.start("--allow-shared-user")
    assert run.started and run.token == AGENT_TOKEN
    assert run.messages("repair agent token file readable by others") == []

    monkeypatch.setenv(CREDENTIAL_VARIABLE, f" {AGENT_TOKEN}\n")
    layout.write_config(without(layout.config, "token_file"), token_env=CREDENTIAL_VARIABLE)
    run = layout.start("--allow-shared-user")
    assert run.started and run.token == AGENT_TOKEN
    assert AGENT_TOKEN not in run.text()


# --- AG46 and AG47: reviewer settings -------------------------------------------------------------------------


def test_reviewer_settings_rules_defaults(layout: Layout) -> None:
    """AG46: every optional setting left out or null takes its default of C-1's Settings table; `api_key`
    is optional."""
    optional = ("structured_output", "max_tokens", "context_window", "timeout_seconds", "max_attempts")
    nulls = {name: None for name in (*optional, "ca_bundle", "proxy", "price", "api_key")}
    left_out = {key: value for key, value in reviewer_entry("left_out").items() if key != "api_key"}
    data = settings_data(reviewers=[left_out, reviewer_entry("all_null", **nulls)])
    settings = config.parse_settings(data, "agent-settings.json")
    for reviewer in settings.reviewers:
        assert reviewer.api_key is None
        assert reviewer.structured_output is True
        assert reviewer.max_tokens == 16384  # owner decision O10: 4096 before it, for a reviewer without context_window
        assert reviewer.context_window is None
        assert reviewer.timeout_seconds == 180
        assert reviewer.max_attempts == 2
        assert reviewer.ca_bundle is None and reviewer.proxy is None and reviewer.price is None
    layout.write_settings(data)
    assert layout.start("--allow-shared-user").started


REVIEWER_SETTING_REFUSALS = {
    "structured_output_yes": ("structured_output", "yes"),
    "structured_output_one": ("structured_output", 1),
    "max_tokens_0": ("max_tokens", 0),
    "max_tokens_a_string": ("max_tokens", "4096"),
    "context_window_0": ("context_window", 0),
    "timeout_seconds_0": ("timeout_seconds", 0),
    "max_attempts_0": ("max_attempts", 0),
    "max_attempts_true": ("max_attempts", True),
    "ca_bundle_missing": ("ca_bundle", "/nonexistent/cairn-repair-test/ca.pem"),
    "proxy_without_a_scheme": ("proxy", "127.0.0.1:3128"),
    "price_third_key": ("price", {"input_per_mtok": 1, "output_per_mtok": 2, "cached_per_mtok": 0}),
    "price_missing_key": ("price", {"input_per_mtok": 1}),
    "price_infinity": ("price", {"input_per_mtok": float("inf"), "output_per_mtok": 2}),
    "price_negative": ("price", {"input_per_mtok": 1, "output_per_mtok": -1}),
    "price_a_string": ("price", {"input_per_mtok": "1", "output_per_mtok": 2}),
    "provider": ("provider", "openai_compatible"),
    "effort": ("effort", "medium"),
    "server_fallbacks": ("server_fallbacks", False),
    "base_url_unedited_placeholder": ("base_url", "http://host.docker.internal:<port>/v1"),
}


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("case", list(REVIEWER_SETTING_REFUSALS))
def test_reviewer_settings_rules(layout: Layout, capsys: pytest.CaptureFixture[str], case: str, position: int) -> None:
    """AG46: each rule of C-1's Settings table refuses a bad value with a startup error that names the reviewer
    and the setting, for the reviewer in use and for one that is not; `provider`, `effort` and
    `server_fallbacks` are refused; an unedited placeholder in `base_url` names the field."""
    setting, value = REVIEWER_SETTING_REFUSALS[case]
    reviewers = [reviewer_entry("reviewer_local"), reviewer_entry("reviewer_second")]
    reviewers[position][setting] = value
    layout.write_settings(settings_data(reviewers=reviewers))
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    name = reviewers[position]["name"]
    prefix = f"agent settings invalid: {layout.settings_path}: reviewers[{position}].{setting}"
    assert stderr.startswith(prefix), stderr
    assert f": reviewer {name}: " in stderr
    if isinstance(value, str) and value not in {"yes", "1"}:
        assert value not in stderr


MODEL_REFUSALS = {
    "empty": "",
    "101_characters": "m" * 101,
    "a_space": "test model",
    "a_tab": "test\tmodel",
    "a_less_than_sign": "<model name>",
    "a_greater_than_sign": "test-model>",
    "a_newline": "test-model\n",
}


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("case", list(MODEL_REFUSALS))
def test_model_name_rules(layout: Layout, capsys: pytest.CaptureFixture[str], case: str, position: int) -> None:
    """AG47 (C-2): a reviewer's `model` that is empty, of 101 characters, or contains a space, a tab, `<` or `>`
    is a startup error with C-2's message, which names the reviewer; the second reviewer's is checked too."""
    reviewers = [reviewer_entry("reviewer_local"), reviewer_entry("reviewer_second")]
    reviewers[position]["model"] = MODEL_REFUSALS[case]
    layout.write_settings(settings_data(reviewers=reviewers))
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    name = reviewers[position]["name"]
    assert stderr == (
        f"agent settings invalid: {layout.settings_path}: reviewers[{position}].model: reviewer {name}: "
        "set REVIEW_MODEL in the repair: section of dispatch.yaml to the model your provider serves, at most 100 "
        "characters; serve a longer name under a shorter one, for example with vLLM's --served-model-name\n"
    )


def test_model_name_rules_a_100_character_name_starts(layout: Layout) -> None:
    """AG47: a 100-character name starts, and is the model of the started line."""
    name = "m" * 99 + "z"
    layout.write_settings(settings_data(reviewers=[reviewer_entry(model=name), reviewer_entry("second", model=name)]))
    run = layout.start("--allow-shared-user")
    assert run.started
    assert f" model={name} " in run.messages("repair agent started")[0]


# --- AG54: the settings file ----------------------------------------------------------------------------------


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


SETTINGS_REFUSALS: dict[str, tuple[Any, str]] = {
    "65537_bytes": (json.dumps(settings_data()).ljust(65537), "file: is larger than 65536 bytes"),
    "not_json": ('{"version": 1, "server": ', "file: is not valid JSON"),
    "nested_200000_deep": (_nested(200_000), "file: is larger than 65536 bytes"),
    "nested_30000_deep_under_64_kib": (_nested(30_000), "file: is not valid JSON"),
    "top_level_a_list": ([settings_data()], "file: the top level must be a JSON object"),
    "top_level_a_string": ('"agent settings"', "file: the top level must be a JSON object"),
    "missing_version": (without(settings_data(), "version"), "version: must be 1"),
    "version_2": (settings_data(version=2), "version: must be 1"),
    "version_true": (settings_data(version=True), "version: must be 1"),
    "unknown_top_level_key": (settings_data(common_env={"TSEC_SERVER_HOST": "x"}), "common_env: unknown key"),
    "server_without_a_scheme": (settings_data(server="127.0.0.1:8000"), "server: must be an http:// or https://"),
    "server_missing": (without(settings_data(), "server"), "server: required"),
    "projects_a_string": (settings_data(projects="proj_001"), "projects: must be null or a list of strings"),
    "projects_holds_a_number": (settings_data(projects=["proj_001", 2]), "projects[1]: must be null or a list"),
    "projects_an_empty_id": (settings_data(projects=[""]), "projects[0]: must be null or a list"),
    "no_reviewer": (settings_data(reviewers=[]), "reviewers: must be a list of at least one reviewer entry"),
    "reviewers_missing": (without(settings_data(), "reviewers"), "reviewers: required"),
    "duplicate_reviewer_name": (
        settings_data(reviewers=[reviewer_entry("same"), reviewer_entry("same")]),
        "reviewers[1].name: must be unique among reviewers",
    ),
    "reviewer_name_with_a_space": (
        settings_data(reviewers=[reviewer_entry("reviewer local")]),
        "reviewers[0].name: must be 1 to 64 letters",
    ),
    "model_fails_c2": (
        settings_data(reviewers=[reviewer_entry(model="<model name>")]),
        f"reviewers[0].model: reviewer reviewer_local: {MODEL_RULE}",
    ),
    "base_url_holds_port_placeholder": (
        settings_data(reviewers=[reviewer_entry(base_url="http://127.0.0.1:<port>/v1")]),
        "reviewers[0].base_url: reviewer reviewer_local: must be an http:// or https:// URL",
    ),
    "base_url_without_a_host": (
        settings_data(reviewers=[reviewer_entry(base_url="http:///v1")]),
        "reviewers[0].base_url: reviewer reviewer_local: must be an http:// or https:// URL",
    ),
    "base_url_port_0": (
        settings_data(reviewers=[reviewer_entry(base_url="http://127.0.0.1:0/v1")]),
        "reviewers[0].base_url: reviewer reviewer_local: must be an http:// or https:// URL",
    ),
    "api_key_empty": (
        settings_data(reviewers=[reviewer_entry(api_key="")]),
        "reviewers[0].api_key: reviewer reviewer_local: must be null or a non-empty string of printable ASCII",
    ),
    "api_key_with_a_delete_character": (
        settings_data(reviewers=[reviewer_entry(api_key=REVIEWER_KEY + "\x7f")]),
        "reviewers[0].api_key: reviewer reviewer_local: must be null or a non-empty string of printable ASCII",
    ),
    "api_key_non_ascii": (
        settings_data(reviewers=[reviewer_entry(api_key=REVIEWER_KEY + "é")]),
        "reviewers[0].api_key: reviewer reviewer_local: must be null or a non-empty string of printable ASCII",
    ),
    "reviewer_key_provider": (
        settings_data(reviewers=[reviewer_entry(provider="anthropic")]),
        "reviewers[0].provider: reviewer reviewer_local: unknown key: there is one model client",
    ),
    "reviewer_key_effort": (
        settings_data(reviewers=[reviewer_entry(effort="medium")]),
        "reviewers[0].effort: reviewer reviewer_local: unknown key: there is one model client",
    ),
    "reviewer_key_server_fallbacks": (
        settings_data(reviewers=[reviewer_entry(server_fallbacks=True)]),
        "reviewers[0].server_fallbacks: reviewer reviewer_local: unknown key: there is one model client",
    ),
    "ca_bundle_the_file_system_cannot_encode": (
        settings_data(reviewers=[reviewer_entry(ca_bundle=f"/nonexistent/cairn-repair-test/{UNENCODABLE}.pem")]),
        "reviewers[0].ca_bundle: reviewer reviewer_local: must be null or a readable file",
    ),
}


@pytest.mark.parametrize("case", list(SETTINGS_REFUSALS))
def test_settings_file_is_read_safely_and_strictly(
    layout: Layout, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """AG54: each of these stops the agent with exit 2 and `agent settings invalid: <path>: <field>: …`, naming
    the field and never a value."""
    content, expected = SETTINGS_REFUSALS[case]
    layout.write_settings(content)
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr.startswith(f"agent settings invalid: {layout.settings_path}: {expected}"), stderr
    assert stderr.count("\n") == 1
    for secret in (REVIEWER_KEY, "<model name>", "<port>", "TSEC_SERVER_HOST", "proj_001"):
        assert secret not in stderr


def test_settings_file_is_read_safely_and_strictly_missing_file(
    layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """AG54: a missing file stops the agent before it polls, with exit 2 and `agent settings missing: …`."""
    layout.settings_path.unlink()
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr == (
        f"agent settings missing: {layout.settings_path}; start the dispatcher with a repair: section in "
        "dispatch.yaml first\n"
    )


def test_settings_file_is_read_safely_and_strictly_symlink(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    """AG54: a symlink to a valid file is refused, never followed."""
    target = write_bytes(layout.base / "elsewhere.json", json.dumps(settings_data()))
    layout.settings_path.unlink()
    layout.settings_path.symlink_to(target)
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr == f"agent settings invalid: {layout.settings_path}: file: is a symlink, not a regular file\n"


def test_settings_file_is_read_safely_and_strictly_fifo(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    """AG54: a FIFO is refused within 1 s; the open never waits for a writer."""
    layout.settings_path.unlink()
    os.mkfifo(layout.settings_path, 0o640)
    result: list[int] = []
    began = time.monotonic()
    worker = threading.Thread(target=lambda: result.append(cli.main(layout.argv("--allow-shared-user"))))
    worker.start()
    worker.join(timeout=5)
    elapsed = time.monotonic() - began
    if worker.is_alive():  # unblock the open before failing
        with open(layout.settings_path, "wb"):
            pass
        worker.join(timeout=5)
        pytest.fail("reading a FIFO settings file blocked")
    assert elapsed < 1.0
    assert result == [2]
    assert capsys.readouterr().err == f"agent settings invalid: {layout.settings_path}: file: is not a regular file\n"


def test_settings_file_is_read_safely_and_strictly_unreadable_as_the_agent_user(host_install: HostInstall) -> None:
    """AG54: a settings file, or a copy root, that the agent's user may not open stops it with exit 2 and
    `agent settings unreadable: <path>: …` (run as the agent's user, which needs root to arrange)."""
    expected = (
        f"agent settings unreadable: {host_install.settings_path}: permission denied; the agent's user must hold the "
        "group of the copy root and of this file\n"
    )
    host_install.write_settings(settings_data(), mode=0o600)
    run = host_install.start()
    assert not run.started and run.stderr == expected
    host_install.write_settings(settings_data())
    set_mode(host_install.root, 0o700)
    run = host_install.start()
    assert not run.started and run.stderr == expected


def test_settings_file_is_read_safely_and_strictly_root_under_an_unsearchable_folder(host_install: HostInstall) -> None:
    """AG54: a copy root that exists under a folder the agent's user cannot search, such as a home of mode 0750 (the
    dispatcher's default transcript_root is under ~/.local/share), stops the agent with `agent settings unreadable:
    <path>: permission denied`, never with `transcripts.root: required: an existing directory`, which would send the
    operator looking for a folder that exists. Run as the agent's user, which needs root to arrange."""
    folder = host_install.home  # the dispatcher's user's own folder, 0755 so far
    root = folder / "transcripts"
    root.mkdir()
    os.chown(root, DISPATCHER_UID, AGENT_GID)
    set_mode(root, 0o2750)
    settings = write_bytes(root / config.SETTINGS_FILE_NAME, json.dumps(settings_data()))
    os.chown(settings, DISPATCHER_UID, AGENT_GID)
    set_mode(settings, 0o640)
    host_install.write_config(transcripts={"root": str(root)})
    run = host_install.start()
    assert run.started, run.text()  # the same copy root, reachable, starts

    set_mode(folder, 0o750)  # its group is the dispatcher's, which the agent's user does not hold
    run = host_install.start()
    assert not run.started
    assert run.stderr == (
        f"agent settings unreadable: {settings}: permission denied; the agent's user must hold the group of the copy "
        "root and of this file\n"
    )


@pytest.mark.skipif(IS_ROOT, reason="root opens every file")
def test_settings_file_is_read_safely_and_strictly_unreadable(
    layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """AG54: a file the agent may not open: exit 2 and `agent settings unreadable: <path>: …`."""
    set_mode(layout.settings_path, 0o000)
    code, stderr = run_main(layout, capsys, "--allow-shared-user")
    assert code == 2
    assert stderr.startswith(f"agent settings unreadable: {layout.settings_path}: permission denied")


def test_settings_file_is_read_safely_and_strictly_spec_example_starts_and_is_read_once(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG54: §9.2's example (placeholders filled) starts; every reviewer is read; the file is read once, so
    rewriting it after the start changes nothing until the agent restarts."""
    layout.write_settings(spec_example_settings())
    opened: list[str] = []
    real_open = os.open

    def counting_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.path.basename(os.fspath(path)) == config.SETTINGS_FILE_NAME:
            opened.append(os.fspath(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", counting_open)
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert opened == [config.SETTINGS_FILE_NAME]
    settings = agent.settings_file.settings
    assert settings.server == "http://127.0.0.1:8000" and settings.projects is None
    assert [r.name for r in settings.reviewers] == ["reviewer_local", "reviewer_provider"]
    assert settings.reviewers[1].api_key == SECOND_REVIEWER_KEY and settings.reviewers[1].timeout_seconds == 180
    assert agent.model.reviewer.name == "reviewer_local"
    assert agent.model.url == "http://127.0.0.1:11434/v1/chat/completions"

    layout.write_settings(settings_data(reviewers=[reviewer_entry("rewritten", base_url="http://127.0.0.2:9/v1")]))
    assert [r.name for r in agent.settings_file.settings.reviewers] == ["reviewer_local", "reviewer_provider"]
    assert agent.model.url == "http://127.0.0.1:11434/v1/chat/completions"
    with pytest.raises(Exception, match="frozen"):
        agent.settings_file.settings.reviewers[0].base_url = "http://127.0.0.2:9/v1"  # type: ignore[misc]
    restarted = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert restarted.model.reviewer.name == "rewritten"
    assert opened == [config.SETTINGS_FILE_NAME, config.SETTINGS_FILE_NAME]


# --- the example configurations --------------------------------------------------------------------------------


def test_example_configurations_hold_only_the_agents_own_settings(
    layout: Layout, capsys: pytest.CaptureFixture[str], stub_server: Any
) -> None:
    """§13: the two examples hold the agent's own settings only (no server, project, model or key); the Compose
    example parses as is (with its copy root moved here), and the host example's placeholders stop the agent.
    `cairn-repair` enters the review loop after its startup (§9.3 step 7), so it runs with `--once` here, against a
    stub server that lists no run."""
    layout.write_settings(
        settings_data(server=stub_server(body={"server_time": "2026-11-14T10:00:00Z", "runs": []}).url)
    )
    host = yaml.safe_load((PROJECT / "repair.example.yaml").read_text(encoding="utf-8"))
    compose = yaml.safe_load((PROJECT / "repair.compose.example.yaml").read_text(encoding="utf-8"))
    for example in (host, compose):
        assert not {"server", "projects", "model", "reviewers", "api_key"} & set(example)
        assert example["transcripts"]["root"] == "/var/lib/cairn-repair/transcripts"
    assert host.get("token_file") == HOST_CREDENTIAL_PATH
    assert "<" in host["dispatch_config_to_check"]
    assert all("<" in path for path in host["isolation"]["unreadable_paths"])
    assert compose.get("token_file") == COMPOSE_CREDENTIAL_PATH
    assert compose["dispatch_config_to_check"] is None
    assert "isolation" not in compose

    for name, example in (("compose", compose), ("host", host)):
        example = dict(example, token_file=str(layout.token_path))
        example["transcripts"] = dict(example["transcripts"], root=str(layout.root))
        example["state"] = dict(example["state"], dir=str(layout.state_dir))  # R3: the pin's folder, moved here too
        layout.write_config(example)
        code, stderr = run_main(layout, capsys, "--allow-shared-user", "--once")
        if name == "compose":
            assert code == 0, stderr
            assert "agent config invalid" not in stderr
        else:
            assert code == 2
            assert stderr.startswith(f"agent config invalid: {layout.config_path}: dispatch_config_to_check: ")
            assert "placeholder" in stderr


def test_config_paths_must_be_absolute_and_edited(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    """§9.2: a relative path, or an unedited `<…>` placeholder, in `dispatch_config_to_check` or
    `isolation.unreadable_paths` is a startup error that `--allow-shared-user` does not override."""
    cases = [
        ({"dispatch_config_to_check": "dispatch.yaml"}, "dispatch_config_to_check: must be an absolute path"),
        ({"dispatch_config_to_check": "/home/<you>/cairn/dispatch.yaml"}, "dispatch_config_to_check: still holds"),
        ({"isolation": {"unreadable_paths": ["secrets/x.token"]}}, "isolation.unreadable_paths[0]: must be an"),
        ({"isolation": {"unreadable_paths": ["/a", "/home/<you>/x"]}}, "isolation.unreadable_paths[1]: still holds"),
    ]
    original = dict(layout.config)
    for change, expected in cases:
        layout.write_config(original, **change)
        code, stderr = run_main(layout, capsys, "--allow-shared-user")
        assert code == 2
        assert stderr.startswith(f"agent config invalid: {layout.config_path}: {expected}"), stderr


def test_secrets_never_reach_a_repr(layout: Layout) -> None:
    """The started agent and its settings keep the token and the reviewers' keys out of their repr."""
    layout.write_settings(settings_data(reviewers=[reviewer_entry(api_key=REVIEWER_KEY)]))
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    for text in (repr(agent), repr(agent.settings_file), repr(agent.model)):
        assert AGENT_TOKEN not in text and REVIEWER_KEY not in text
