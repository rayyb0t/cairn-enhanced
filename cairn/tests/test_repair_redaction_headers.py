"""TC4's cases of header lists and header maps (build step RA2; the final review's SECRETS_AND_ISOLATION-2, the
review of RA2's SAFETY-1, and that fix's recheck). Each test's name starts with TC4's, test_redaction_covers_every_
worker_local_env_repair_token_and_proxy_userinfo, followed by the case.

Claude Code sends the headers of ANTHROPIC_CUSTOM_HEADERS ("Name: value" lines) with every API request, a gateway's
credential, and its telemetry exporter those of OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS
(comma-separated name=value pairs, URL-encoded), a telemetry backend's ingest key. No secret-name rule matches these
names, and the header that holds the key need not hold a secret word either (x-gateway-team, x-honeycomb-team). So
the exact-value redaction (spec 2.2, 8.8) reads these variables as header lists, in a worker's env, the dispatcher's
own environment and the env objects of Claude Code's .claude.json and settings.json, and takes each header value of
at least eight characters and the whole value; and it takes every value of the credential files' header maps,
whatever the header's name: an MCP server's headers in .claude.json, codex's http_headers tables in config.toml, and
pi's headers in models.json. A header map often takes a value from an environment variable instead: pi's headers name
the variable, Claude Code expands ${NAME} and ${NAME:-default} in an MCP server's headers, and codex's env_http_headers
tables name the variable whose value is sent. Those values are taken too, resolved in each worker's environment as its
CLI resolves them (for Claude Code with the env objects of .claude.json and settings.json added, and with every value
that a named variable has in any of those layers; the layers Claude Code adds after them, its managed settings among
them, are not read, and the code, the docs and the runbook say so, fix round 3 of RA2); a pi value that is a
'!command' is never run; and the whole values that one file's templates expand to are bounded together
(EXPANDED_HEADERS_MAX_CHARS), since a local worker can write both a template and the value it names. What it cannot
parse it skips, and it never raises.

Every value here is synthetic: the keys are obviously fake, the hosts loopback or under .invalid.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from repair_dispatcher_runs import ENV
from repair_dispatcher_support import FAKE_TOKEN, reset_repair_process_state
from test_repair_transcripts_support import (
    SESSION_IDS,
    FakeClock,
    FakeDockerApi,
    FakeFile,
    all_log_text,
    container_session_path,
    copy_config,
    fixture_bytes,
    install_docker,
    make_run,
    read_meta,
    read_transcript,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import redaction, transcripts
from cairn.dispatcher.repair.transcripts import CopySchedule

GATEWAY_KEY = "FAKE-gateway-team-canary-0201"  # fake
GATEWAY_KEY_2 = "FAKE-gateway-org-canary-0202"  # fake
INGEST_KEY = "FAKE/ingest=key canary-0203"  # fake; '/', '=' and a space: its URL-encoded form differs
TRACES_KEY = "FAKE-traces-ingest-canary-0204"  # fake
SHORT = "short7c"  # seven characters: below the rule's eight, left alone
MCP_KEY = "FAKE-mcp-team-canary-0205"  # fake
MCP_PROJECT_KEY = "FAKE-mcp-project-canary-0206"  # fake
CODEX_PROVIDER_KEY = "FAKE-codex-provider-hdr-canary-0207"  # fake
CODEX_MCP_KEY = "FAKE-codex-mcp-hdr-canary-0208"  # fake
PI_PROVIDER_KEY = "FAKE-pi-provider-hdr-canary-0209"  # fake
PI_MODEL_KEY = "FAKE-pi-model-hdr-canary-0210"  # fake
PI_OVERRIDE_KEY = "FAKE-pi-override-hdr-canary-0211"  # fake
DEEP_KEY = "FAKE-deep-mcp-hdr-canary-0212"  # fake
CODEX_ENV_HEADER_KEY = "FAKE-codex-env-header-canary-0222"  # fake; in the variable that env_http_headers names
# Header values that come from environment variables whose names hold no secret word (the review of RA2, SAFETY-1).
PI_TEAM_KEY = "FAKE-pi-gateway-team-canary-0301"  # fake
PI_MODEL_TEAM_KEY = "FAKE-pi-model-team-canary-0302"  # fake
MCP_TEAM_KEY = "FAKE-mcp-gateway-team-canary-0303"  # fake
MCP_BEARER_KEY = "FAKE-mcp-bearer-canary-0304"  # fake
MCP_DEFAULT_KEY = "FAKE-mcp-default-canary-0305"  # fake; the default of a ${NAME:-default} whose NAME is not set
MCP_SETTINGS_KEY = "FAKE-mcp-settings-env-canary-0306"  # fake; set only in settings.json env
MCP_CLAUDE_JSON_KEY = "FAKE-mcp-claude-json-env-canary-0307"  # fake; set only in .claude.json's env
CODEX_TEAM_KEY = "FAKE-codex-gateway-team-canary-0308"  # fake
CODEX_MCP_TEAM_KEY = "FAKE-codex-mcp-team-canary-0309"  # fake
PAIR_FIRST, PAIR_SECOND = "FAKE-ab", "cd-0310"  # fake; seven characters each, fourteen once expanded side by side
UNUSED_DEFAULT = "FAKE-unused-default-0311"  # the default of a ${NAME:-default} whose NAME is set: never sent
WORKER_KEYS = ("FAKE-first-worker-canary-0312", "FAKE-second-worker-canary-0313")  # fake; one value per worker
REFERENCE_SECRETS = [PI_TEAM_KEY, PI_MODEL_TEAM_KEY, MCP_TEAM_KEY, MCP_BEARER_KEY, MCP_DEFAULT_KEY, MCP_SETTINGS_KEY]
REFERENCE_SECRETS += [MCP_CLAUDE_JSON_KEY, CODEX_TEAM_KEY, CODEX_MCP_TEAM_KEY, PAIR_FIRST + PAIR_SECOND, *WORKER_KEYS]

CUSTOM_HEADERS = f"x-gateway-team: {GATEWAY_KEY}\r\nx-gateway-org:{GATEWAY_KEY_2}\nx-short: {SHORT}\n"
OTLP_HEADERS = f"x-honeycomb-team={quote(INGEST_KEY, safe='')},x-honeycomb-dataset=ds"
OTLP_TRACES_HEADERS = f"x-ingest = {TRACES_KEY};origin=lab, x-short={SHORT}"
HEADER_VARIABLES = {
    "ANTHROPIC_CUSTOM_HEADERS": CUSTOM_HEADERS,
    "OTEL_EXPORTER_OTLP_HEADERS": OTLP_HEADERS,
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": OTLP_TRACES_HEADERS,
}
# What the variables give, per variable: each header value of at least eight characters (an OTLP value also decoded,
# and before its ';'), and the whole value.
EXPECTED_VALUES = {
    "ANTHROPIC_CUSTOM_HEADERS": {GATEWAY_KEY, GATEWAY_KEY_2, CUSTOM_HEADERS},
    "OTEL_EXPORTER_OTLP_HEADERS": {quote(INGEST_KEY, safe=""), INGEST_KEY, OTLP_HEADERS},
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": {f"{TRACES_KEY};origin=lab", TRACES_KEY, OTLP_TRACES_HEADERS},
}
ENV_SECRETS = [GATEWAY_KEY, GATEWAY_KEY_2, INGEST_KEY, quote(INGEST_KEY, safe=""), TRACES_KEY]
FILE_SECRETS = [MCP_KEY, MCP_PROJECT_KEY, CODEX_PROVIDER_KEY, CODEX_MCP_KEY, PI_PROVIDER_KEY, PI_MODEL_KEY]
FILE_SECRETS += [PI_OVERRIDE_KEY, DEEP_KEY, CODEX_ENV_HEADER_KEY]

# A run that prints the variables and the headers it sends, as `env`, `printenv` and `curl -v` show them.
PRINTED = [
    *(f"{name}={value}" for name, value in HEADER_VARIABLES.items()),
    f"> x-gateway-team: {GATEWAY_KEY}",
    f"> x-gateway-org: {GATEWAY_KEY_2}",
    f"> x-honeycomb-team: {INGEST_KEY}",
    f"x-honeycomb-team={quote(INGEST_KEY, safe='')}",
    f"> x-ingest: {TRACES_KEY}",
    f"> x-short: {SHORT}",
]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fresh once-per-process state, no real Docker unless a test installs a fake one; no log record holds a key."""
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(redaction, "_oversized_logged", set())

    def no_docker() -> Any:
        raise AssertionError("the repair module reached for Docker")

    monkeypatch.setattr(transcripts, "docker_api", no_docker)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    text = all_log_text(caplog)
    for secret in [*ENV_SECRETS, *FILE_SECRETS, *REFERENCE_SECRETS, FAKE_TOKEN]:
        assert secret not in text, secret


def session_with(*texts: str) -> bytes:
    """codex's pinned session file with lines appended that hold texts, as a tool's output would."""
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "tool_output", "text": text}}) for text in texts]
    return fixture_bytes("codex") + "".join(line + "\n" for line in lines).encode()


def worker(name: str, cli: str, env: dict[str, str]) -> dict[str, Any]:
    return {"name": name, "type": cli, "task_types": ["explore"], "max_running": 1, "priority": 0, "env": env}


def assert_removed(copied: bytes, secrets: list[str]) -> None:
    """No secret is in the copy, raw or in any form that redact_exact replaces (JSON-escaped once or twice)."""
    for secret in secrets:
        for form in redaction._forms(secret):
            assert form not in copied, secret
    assert b"canary-02" not in copied


def local_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, codex_home: Path, *printed: str
) -> bytes:
    """One copy, in local mode, of a codex session in codex_home whose tool output prints printed."""
    sessions = codex_home / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
    source.write_bytes(session_with(*printed))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path / "work"), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    clock.at(1260)  # 70% of the run's 1800 s budget: the first copy
    schedule.tick()
    return read_transcript(config)


# --- header-list variables ---------------------------------------------------------------------------------------------


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_lists_in_container_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, container mode: a Claude Code worker's env holds ANTHROPIC_CUSTOM_HEADERS and two OTLP header variables
    (another worker runs: all workers' values are collected). A run that prints them with env and printenv, and the
    headers as curl -v shows them, leaves no header value of eight characters or more, raw, URL-encoded or decoded, in
    the copy; each is counted under its variable's name; the header names and a seven-character value stay."""
    claude_env = {**ENV["claudecode"], **HEADER_VARIABLES}
    config = copy_config(
        tmp_path, "codex", workers=[worker("w1", "codex", dict(ENV["codex"])), worker("w2", "claudecode", claude_env)]
    )
    values = redaction.secret_values(config, "container", {}, None)
    for name, expected in EXPECTED_VALUES.items():
        assert {value for value, counted in values.items() if counted == name} == expected, name
    assert SHORT not in values and "ds" not in values

    path = container_session_path("codex", SESSION_IDS["codex"])
    install_docker(monkeypatch, FakeDockerApi({path: FakeFile(session_with(*PRINTED))}))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    clock.at(1260)
    schedule.tick()
    copied = read_transcript(config)
    assert_removed(copied, ENV_SECRETS)
    assert b"> x-gateway-team: [REDACTED:ANTHROPIC_CUSTOM_HEADERS]" in copied
    assert b"> x-honeycomb-team: [REDACTED:OTEL_EXPORTER_OTLP_HEADERS]" in copied
    assert b"> x-ingest: [REDACTED:OTEL_EXPORTER_OTLP_TRACES_HEADERS]" in copied
    assert f"> x-short: {SHORT}".encode() in copied
    redactions = read_meta(config)["redactions"]
    # Each variable's whole value once (longest first), then each header line: two, two and one.
    assert redactions["ANTHROPIC_CUSTOM_HEADERS"] == 3
    assert redactions["OTEL_EXPORTER_OTLP_HEADERS"] == 3
    assert redactions["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == 2


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_lists_in_local_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, local mode: the variables in the dispatcher's own environment, which local workers inherit, are collected
    as in a worker's env, counted under each variable's name, and removed from the copy."""
    for name, value in HEADER_VARIABLES.items():
        monkeypatch.setenv(name, value)
    home = tmp_path / "home"
    codex_home = home / ".codex"
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", {"HOME": str(home)})])
    values = redaction.secret_values(config, "local", dict(os.environ), None)
    for name, expected in EXPECTED_VALUES.items():
        assert {value for value, counted in values.items() if counted == name} == expected, name
    copied = local_copy(tmp_path, monkeypatch, config, codex_home, *PRINTED)
    assert_removed(copied, ENV_SECRETS)
    redactions = read_meta(config)["redactions"]
    assert redactions["ANTHROPIC_CUSTOM_HEADERS"] == 3
    assert redactions["OTEL_EXPORTER_OTLP_HEADERS"] == 3
    assert redactions["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == 2


def write_header_maps(base: Path, *, deep: bool = True) -> dict[str, str]:
    """The four CLI folders under base, with header maps in .claude.json, config.toml and models.json and the header
    variables in settings.json env; the worker env that finds them, which also sets the variable that codex's
    env_http_headers names (a worker's env in dispatch.yaml, on top of the dispatcher's own environment)."""
    home, claude, codex, pi = base / "home", base / "claude-config", base / "codex-home", base / "pi-agent"
    for folder in (home, claude, codex, pi):
        folder.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({"env": {**HEADER_VARIABLES, "NO_PROXY": "127.0.0.1"}}))
    servers: dict[str, Any] = {"docs": {"type": "http", "url": "https://mcp.invalid/mcp"}}
    servers["docs"]["headers"] = {"X-Team-Id": MCP_KEY, "X-Short": SHORT, "X-Count": 5}
    claude_json: dict[str, Any] = {
        "mcpServers": servers,
        "projects": {"/srv/lab": {"mcpServers": {"wiki": {"headers": {"X-Wiki-Team": MCP_PROJECT_KEY}}}}},
        "theme": "dark",
    }
    text = json.dumps(claude_json)
    if deep:  # an MCP server's headers 5,000 levels down, which json.loads accepts and a recursive walk would not
        deepest = json.dumps({"mcpServers": {"deep": {"headers": {"X-Deep": DEEP_KEY}}}})
        text = text[:-1] + ', "history": ' + '{"n":' * 5000 + deepest + "}" * 5000 + "}"
    (claude / ".claude.json").write_text(text)
    (codex / "config.toml").write_text(
        "[model_providers.gw]\n"
        'name = "gw"\n'
        'base_url = "https://gw.invalid/v1"\n'
        f'http_headers = {{ "X-Gateway-Team" = "{CODEX_PROVIDER_KEY}", "X-Short" = "{SHORT}" }}\n'
        'env_http_headers = { "X-From-Env" = "LAB_HEADER_VARIABLE" }\n\n'
        "[mcp_servers.docs]\n"
        'url = "https://mcp.invalid/mcp"\n'
        f'http_headers = {{ "X-Docs-Team" = "{CODEX_MCP_KEY}" }}\n'
    )
    provider = {
        "baseUrl": "https://gw.invalid/v1",
        "api": "openai-completions",
        "headers": {"x-provider-team": PI_PROVIDER_KEY},
        "models": [{"id": "test-model", "headers": {"x-model-team": PI_MODEL_KEY}}],
        "modelOverrides": {"test-model": {"headers": {"x-override-team": PI_OVERRIDE_KEY}}},
    }
    (pi / "models.json").write_text(json.dumps({"providers": {"gw": provider}}))
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "PI_CODING_AGENT_DIR": str(pi),
        "LAB_HEADER_VARIABLE": CODEX_ENV_HEADER_KEY,  # no secret word in its name: only env_http_headers marks it
    }


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_maps_and_settings_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, local mode, the CLIs' own files: the header variables in settings.json env (which Claude Code applies to
    its environment), every value of an MCP server's headers in .claude.json (at the top level, under a project and
    5,000 levels down), of codex's http_headers tables (a model provider's and an MCP server's) and of pi's headers
    (a provider's, a model's and a model override's), whatever the header's name, are removed from the copy and
    counted under the files' labels; so is the value of the variable that codex's env_http_headers names, which is
    set in the worker's env, while the variable's name is not collected; a seven-character value and a number are not
    collected either."""
    worker_env = write_header_maps(tmp_path / "creds")
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    started = time.monotonic()
    pairs = set(redaction.credential_file_pairs(config, {}))
    assert time.monotonic() - started < 5
    by_label: dict[str, set[str]] = {}
    for label, value in pairs:
        by_label.setdefault(label, set()).add(value)
    expected_settings = set().union(*EXPECTED_VALUES.values())
    assert by_label["claude:settings.json"] == expected_settings
    assert by_label["claude:.claude.json"] == {MCP_KEY, MCP_PROJECT_KEY, DEEP_KEY}
    assert by_label["codex:config.toml"] == {CODEX_PROVIDER_KEY, CODEX_MCP_KEY, CODEX_ENV_HEADER_KEY}
    assert by_label["pi:models.json"] == {PI_PROVIDER_KEY, PI_MODEL_KEY, PI_OVERRIDE_KEY}
    assert not any(value in (SHORT, "LAB_HEADER_VARIABLE") for value in set().union(*by_label.values()))
    # The worker's env alone would not give it: the variable's name matches no secret-name rule.
    assert CODEX_ENV_HEADER_KEY not in redaction.environment_values(worker_env)

    printed = [*PRINTED, *(f"header value {secret}" for secret in FILE_SECRETS)]
    copied = local_copy(tmp_path, monkeypatch, config, Path(worker_env["CODEX_HOME"]), *printed)
    assert_removed(copied, [*ENV_SECRETS, *FILE_SECRETS])
    redactions = read_meta(config)["redactions"]
    assert redactions["claude:settings.json"] == 8  # the three whole values, then five header lines
    assert redactions["claude:.claude.json"] == 3
    assert redactions["codex:config.toml"] == 3
    assert redactions["pi:models.json"] == 3


# --- header values taken from environment variables (the review of RA2, SAFETY-1) -------------------------------------


def write_header_references(base: Path, command_marker: Path) -> dict[str, str]:
    """The four CLI folders under base, whose header maps take their values from environment variables, as each CLI
    writes it; the worker env that finds them. pi: provider and model headers that name a variable, one that names an
    unset variable, one an empty one, one a variable of seven characters, and a '!command' that would create
    command_marker. Claude Code: MCP server headers with ${NAME}, a 'Bearer ${NAME}', ${NAME:-default} with NAME unset
    and with NAME set, two references side by side, a short value, an unset reference, and under a project two that
    only settings.json env and .claude.json's env set. codex: env_http_headers tables of a model provider (with an
    unset and an empty variable) and of an MCP server."""
    home, claude, codex, pi = base / "home", base / "claude-config", base / "codex-home", base / "pi-agent"
    for folder in (home, claude, codex, pi):
        folder.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({"env": {"LAB_SETTINGS_ONLY": MCP_SETTINGS_KEY}}))
    headers = {
        "X-Team": "${LAB_MCP_TEAM}",
        "Authorization": "Bearer ${LAB_MCP_BEARER}",
        "X-Default": "${LAB_MCP_UNSET:-" + MCP_DEFAULT_KEY + "}",
        "X-Unused-Default": "${LAB_MCP_TEAM:-" + UNUSED_DEFAULT + "}",
        "X-Pair": "${LAB_PAIR_A}${LAB_PAIR_B}",
        "X-Short": "${LAB_SHORT}",
        "X-Missing": "${LAB_MCP_UNSET}",
    }
    project = {"wiki": {"headers": {"X-Settings": "${LAB_SETTINGS_ONLY}", "X-Global": "${LAB_CLAUDE_JSON_ONLY}"}}}
    claude_json = {
        "env": {"LAB_CLAUDE_JSON_ONLY": MCP_CLAUDE_JSON_KEY},
        "mcpServers": {"docs": {"type": "http", "url": "https://mcp.invalid/mcp", "headers": headers}},
        "projects": {"/srv/lab": {"mcpServers": project}},
    }
    (claude / ".claude.json").write_text(json.dumps(claude_json))
    (codex / "config.toml").write_text(
        "[model_providers.gw]\n"
        'base_url = "https://gw.invalid/v1"\n'
        'env_http_headers = { "X-Gateway-Team" = "LAB_CODEX_TEAM", "X-Unset" = "LAB_CODEX_UNSET", "X-Empty" = '
        '"LAB_EMPTY" }\n\n'
        "[mcp_servers.docs]\n"
        'url = "https://mcp.invalid/mcp"\n'
        'env_http_headers = { "X-Docs-Team" = "LAB_CODEX_MCP_TEAM" }\n'
    )
    provider_headers = {
        "x-gateway-team": "LAB_PI_TEAM",
        "x-unset": "LAB_PI_UNSET_NAME",
        "x-empty": "LAB_EMPTY",
        "x-short": "LAB_SHORT",
        "x-command": f"!touch {command_marker}",
    }
    provider = {
        "baseUrl": "https://gw.invalid/v1",
        "api": "openai-completions",
        "headers": provider_headers,
        "models": [{"id": "test-model", "headers": {"x-model-team": "LAB_PI_MODEL_TEAM"}}],
    }
    (pi / "models.json").write_text(json.dumps({"providers": {"gw": provider}}))
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "PI_CODING_AGENT_DIR": str(pi),
    }


# The variables, in the dispatcher's own environment, which local workers inherit.
REFERENCED = {
    "LAB_PI_TEAM": PI_TEAM_KEY,
    "LAB_PI_MODEL_TEAM": PI_MODEL_TEAM_KEY,
    "LAB_MCP_TEAM": MCP_TEAM_KEY,
    "LAB_MCP_BEARER": MCP_BEARER_KEY,
    "LAB_PAIR_A": PAIR_FIRST,
    "LAB_PAIR_B": PAIR_SECOND,
    "LAB_SHORT": SHORT,
    "LAB_EMPTY": "",
    "LAB_CODEX_TEAM": CODEX_TEAM_KEY,
    "LAB_CODEX_MCP_TEAM": CODEX_MCP_TEAM_KEY,
}
NOT_SET = ["LAB_MCP_UNSET", "LAB_PI_UNSET_NAME", "LAB_CODEX_UNSET", "LAB_SETTINGS_ONLY", "LAB_CLAUDE_JSON_ONLY"]


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, local mode, header values that the CLIs take from environment variables whose names hold no secret word,
    set in the dispatcher's own environment (the review of RA2, SAFETY-1): pi's headers that name a variable give its
    value, Claude Code's ${NAME} and ${NAME:-default} give what Claude Code sends (also from settings.json env and
    .claude.json's env, which it adds to its environment, and the whole value expanded), and codex's env_http_headers
    give the value of the variable each names. Each is counted under the file's label and removed from the copy, as
    curl -v prints it; a value under eight characters, an unset or empty variable, and the default of a set NAME give
    nothing, and pi's '!command' is never run."""
    for name, value in REFERENCED.items():
        monkeypatch.setenv(name, value)
    for name in NOT_SET:
        monkeypatch.delenv(name, raising=False)
    marker = tmp_path / "command-ran"
    worker_env = write_header_references(tmp_path / "creds", marker)
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    # Without the references, nothing would give these values: no variable's name holds a secret word.
    assert not set(REFERENCE_SECRETS) & redaction.environment_values(dict(os.environ))

    values = redaction.secret_values(config, "local", dict(os.environ), None)
    by_label: dict[str, set[str]] = {}
    for value, label in values.items():
        by_label.setdefault(label, set()).add(value)
    claude_written = {  # every header value of eight characters or more, as written (build step RA2's rule)
        "${LAB_MCP_TEAM}",
        "Bearer ${LAB_MCP_BEARER}",
        "${LAB_MCP_UNSET:-" + MCP_DEFAULT_KEY + "}",
        "${LAB_MCP_TEAM:-" + UNUSED_DEFAULT + "}",
        "${LAB_PAIR_A}${LAB_PAIR_B}",
        "${LAB_SHORT}",
        "${LAB_MCP_UNSET}",
        "${LAB_SETTINGS_ONLY}",
        "${LAB_CLAUDE_JSON_ONLY}",
    }
    claude_sent = {MCP_TEAM_KEY, MCP_BEARER_KEY, f"Bearer {MCP_BEARER_KEY}", MCP_DEFAULT_KEY, PAIR_FIRST + PAIR_SECOND}
    assert by_label["claude:.claude.json"] == claude_written | claude_sent | {MCP_SETTINGS_KEY, MCP_CLAUDE_JSON_KEY}
    pi_written = {"LAB_PI_TEAM", "LAB_PI_UNSET_NAME", "LAB_EMPTY", "LAB_SHORT", f"!touch {marker}", "LAB_PI_MODEL_TEAM"}
    assert by_label["pi:models.json"] == pi_written | {PI_TEAM_KEY, PI_MODEL_TEAM_KEY}
    assert by_label["codex:config.toml"] == {CODEX_TEAM_KEY, CODEX_MCP_TEAM_KEY}
    assert "claude:settings.json" not in by_label  # its env alone gives nothing: no name there holds a secret word
    for value in (SHORT, PAIR_FIRST, PAIR_SECOND, UNUSED_DEFAULT, "LAB_CODEX_TEAM", "LAB_CODEX_UNSET"):
        assert value not in values, value

    printed = [
        f"> x-gateway-team: {PI_TEAM_KEY}",
        f"> x-model-team: {PI_MODEL_TEAM_KEY}",
        f"> X-Team: {MCP_TEAM_KEY}",
        f"> Authorization: Bearer {MCP_BEARER_KEY}",
        f"> X-Default: {MCP_DEFAULT_KEY}",
        f"> X-Pair: {PAIR_FIRST}{PAIR_SECOND}",
        f"> X-Settings: {MCP_SETTINGS_KEY}",
        f"> X-Global: {MCP_CLAUDE_JSON_KEY}",
        f"> X-Gateway-Team: {CODEX_TEAM_KEY}",
        f"> X-Docs-Team: {CODEX_MCP_TEAM_KEY}",
        f"> X-Short: {SHORT}",
    ]
    copied = local_copy(tmp_path, monkeypatch, config, Path(worker_env["CODEX_HOME"]), *printed)
    assert_removed(copied, REFERENCE_SECRETS)
    assert b"canary-03" not in copied
    assert b"> Authorization: [REDACTED:claude:.claude.json]" in copied  # the whole expanded value, longest first
    assert f"> X-Short: {SHORT}".encode() in copied
    redactions = read_meta(config)["redactions"]
    assert redactions["claude:.claude.json"] == 6
    assert redactions["pi:models.json"] == 2
    assert redactions["codex:config.toml"] == 2
    assert not marker.exists()  # pi's '!command' was never run


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_references_per_worker(
    tmp_path: Path,
) -> None:
    """TC4, local mode: two workers whose CLIs share one models.json, and whose env in dispatch.yaml give the
    variable its header names a different value each, give both values: the file is read once, and its references
    are resolved in each worker's merged environment, as each worker's pi resolves them."""
    pi = tmp_path / "pi-agent"
    pi.mkdir()
    provider = {"baseUrl": "https://gw.invalid/v1", "headers": {"x-worker-team": "LAB_WORKER_TEAM"}}
    (pi / "models.json").write_text(json.dumps({"providers": {"gw": provider}}))
    workers = [
        worker(
            f"w{index}", "pi", {"HOME": str(tmp_path / "home"), "PI_CODING_AGENT_DIR": str(pi), "LAB_WORKER_TEAM": key}
        )
        for index, key in enumerate(WORKER_KEYS, start=1)
    ]
    config = copy_config(tmp_path, "pi", execution="local", workers=workers)
    pairs = set(redaction.credential_file_pairs(config, {}))
    assert pairs == {("pi:models.json", "LAB_WORKER_TEAM"), *(("pi:models.json", key) for key in WORKER_KEYS)}


@pytest.mark.parametrize(
    ("how", "text", "env", "expected"),
    [
        ("name", "LAB_X", {"LAB_X": "FAKE-value-0401"}, ["FAKE-value-0401"]),
        ("name", "LAB_X", {"LAB_X": ""}, []),  # empty: pi sends the name itself, codex no header
        ("name", "LAB_X", {}, []),
        ("name", "LAB_X", {"LAB_X": SHORT}, []),
        ("name", "LAB_X", {"LAB_X": " \t" * 6}, []),  # white space only: no credential, and common in output
        ("template", "${LAB_X}${LAB_Y}", {"LAB_X": " " * 8, "LAB_Y": "\t" * 8}, []),
        ("name", "!echo FAKE-0402", {"!echo FAKE-0402": "FAKE-value-0403"}, []),  # a command to pi: never looked up
        ("template", "${LAB_X}", {"LAB_X": "FAKE-value-0404"}, ["FAKE-value-0404"]),
        (
            "template",
            "pre ${LAB_X} post",
            {"LAB_X": "FAKE-value-0405"},
            ["FAKE-value-0405", "pre FAKE-value-0405 post"],
        ),
        ("template", "${LAB_X:-FAKE-default-0406}", {}, ["FAKE-default-0406"]),
        ("template", "${LAB_X:-FAKE-default-0407}", {"LAB_X": ""}, []),  # set, even empty: the default is not used
        ("template", "${LAB_X:-FAKE-default-0408}", {"LAB_X": "FAKE-value-0409"}, ["FAKE-value-0409"]),
        ("template", "${LAB_X:-a:-FAKE-0410}", {}, ["a:-FAKE-0410"]),  # the first ':-' ends the name
        ("template", "${LAB_X}", {}, []),  # unset, no default: the reference stays as it is written
        ("template", "${LAB_A}${LAB_B}", {"LAB_A": "FAKE-ab", "LAB_B": "cd-0411"}, ["FAKE-abcd-0411"]),
        ("template", "$LAB_X ${LAB_X ${} {LAB_X} ${LAB_X", {"LAB_X": "FAKE-value-0412"}, []),  # no such reference
    ],
)
def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_reference_rules(
    how: str, text: str, env: dict[str, str], expected: list[str]
) -> None:
    """TC4, the reference rules (redaction._resolved): a name gives its variable's value when that is set and not
    empty, and a pi '!command' is never looked up or run; a template gives what Claude Code 2.1.98 puts in place of
    each ${NAME} and ${NAME:-default}, then the whole value expanded; each of eight characters or more."""
    assert redaction._resolved([(how, text)], env) == expected


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_unclosed_header_references() -> None:
    """TC4: a header value of a reference followed by 50,000 '${' with no '}' after them (a local worker can write
    .claude.json) is expanded within a second: the text after the last '}' is not searched, so it is not scanned
    again from each '${' to its end, which took over ten seconds at this length."""
    tail = "${" * 50_000
    started = time.monotonic()
    found = redaction._resolved([("template", "${LAB_X}" + tail)], {"LAB_X": "FAKE-value-0413"})
    assert time.monotonic() - started < 1
    assert found == ["FAKE-value-0413", "FAKE-value-0413" + tail]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("ANTHROPIC_CUSTOM_HEADERS", "no header here at all", {"no header here at all"}),
        (
            "ANTHROPIC_CUSTOM_HEADERS",
            ": value-without-a-name\n\n:\n",
            {"value-without-a-name", ": value-without-a-name\n\n:\n"},
        ),
        ("ANTHROPIC_CUSTOM_HEADERS", "x: short", {"x: short"}),
        ("ANTHROPIC_CUSTOM_HEADERS", "x:", set()),
        (
            "OTEL_EXPORTER_OTLP_HEADERS",
            "noequals,,=,k=%E0%A4-truncated,j=%ZZ-not-escaped",
            {
                "%E0%A4-truncated",
                "\ufffd-truncated",  # decoded: a truncated UTF-8 sequence becomes U+FFFD
                "%ZZ-not-escaped",
                "noequals,,=,k=%E0%A4-truncated,j=%ZZ-not-escaped",
            },
        ),
        ("OTEL_EXPORTER_OTLP_METRICS_HEADERS", "k=" + "\udcff" * 9, {"\udcff" * 9, "k=" + "\udcff" * 9}),
        ("OTEL_EXPORTER_OTLP_HEADERS_EXTRA", "k=FAKE-not-a-header-list-0213", set()),
        ("otel_exporter_otlp_headers", "k=FAKE-lower-case-name-0214", set()),
        ("ANTHROPIC_CUSTOM_HEADER", "x: FAKE-singular-name-0215", set()),
    ],
)
def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_unparsable_header_lists(
    name: str, value: str, expected: set[str]
) -> None:
    """TC4, malformed header lists: a value with no header gives only itself (when it has eight characters), an item
    without a name or '=' and an escape that does not decode are taken as they are, a lone surrogate (as os.environ
    holds an undecodable byte) does not raise, and other names, close or in another case, give nothing."""
    assert set(redaction.header_list_values(name, value)) == expected
    assert set(value for _name, value in redaction.environment_pairs({name: value})) == expected


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_unusable_header_maps(
    tmp_path: Path,
) -> None:
    """TC4, malformed header maps: an mcpServers or a headers that is not an object, a header value that is not a
    string, an http_headers that is a string, and a headers that is a list give nothing and raise nothing; the
    files' other values are still collected."""
    worker_env = write_header_maps(tmp_path / "creds", deep=False)
    claude, codex, pi = (Path(worker_env[key]) for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"))
    (claude / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": ["docs", {"headers": {"X-Team-Id": "FAKE-in-a-list-not-a-server-0216"}}],
                "projects": {"/srv/lab": {"mcpServers": {"wiki": {"headers": "FAKE-headers-as-a-string-0217"}}}},
                "other": {"mcpServers": {"x": {"headers": {"X-Team": ["FAKE-in-a-list-0218"], "X-N": None}}}},
                "primaryApiKey": "FAKE-primary-api-key-0219",
            }
        )
    )
    (codex / "config.toml").write_text('[model_providers.gw]\nhttp_headers = "FAKE-a-string-table-0220"\n')
    (pi / "models.json").write_text(json.dumps({"providers": {"gw": {"headers": ["FAKE-a-list-0221"]}}}))
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    pairs = set(redaction.credential_file_pairs(config, {}))
    assert ("claude:.claude.json", "FAKE-primary-api-key-0219") in pairs  # the keyed rule, as before
    assert not any(
        value.startswith("FAKE-") and value.endswith(("0216", "0217", "0218", "0220", "0221")) for _, value in pairs
    )
    for unusable in ("[1, 2]", '{"mcpServers": 5}', '{"providers": {"gw": {"headers": {"x": {"y": 1}}}}}'):
        (pi / "models.json").write_text(unusable)
        assert all(label != "pi:models.json" for label, _ in redaction.credential_file_pairs(config, {}))


# --- the recheck of RA2's fix: .claude.json's env, the layers of Claude Code's environment, the bound ----------------

# LAB_LAYERED is set in all three layers of Claude Code's environment that redaction.py reads, LAB_LOWER in the first
# two (fake values).
LAYER_ENVIRONMENT = "FAKE-layer-environment-canary-0501"  # LAB_LAYERED in the dispatcher's own environment
LAYER_CLAUDE_JSON = "FAKE-layer-claude-json-canary-0502"  # LAB_LAYERED in .claude.json's env
LAYER_SETTINGS = "FAKE-layer-settings-canary-0503"  # LAB_LAYERED in settings.json's env: what Claude Code sends
LOWER_ENVIRONMENT = "FAKE-lower-environment-canary-0504"  # LAB_LOWER in the dispatcher's own environment
LOWER_CLAUDE_JSON = "FAKE-lower-claude-json-canary-0505"  # LAB_LOWER in .claude.json's env: what Claude Code sends
BIG_VALUE = ("FAKE-big-env-value-canary-0506-" * 2200)[:65_536]  # fake; 64 KiB, in .claude.json's own env
RECHECK_SECRETS = [
    LAYER_ENVIRONMENT,
    LAYER_CLAUDE_JSON,
    LAYER_SETTINGS,
    LOWER_ENVIRONMENT,
    LOWER_CLAUDE_JSON,
    BIG_VALUE,
]


def claude_folders(
    base: Path, claude_json: dict[str, Any], settings_env: dict[str, str] | None = None
) -> dict[str, str]:
    """A Claude Code config folder under base with this .claude.json (and settings.json env), and a codex home for
    the copied session; the worker env that finds them."""
    home, claude, codex = base / "home", base / "claude-config", base / "codex-home"
    for folder in (home, claude, codex):
        folder.mkdir(parents=True)
    (claude / ".claude.json").write_text(json.dumps(claude_json))
    if settings_env is not None:
        (claude / "settings.json").write_text(json.dumps({"env": settings_env}))
    return {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}


def assert_no_recheck_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    text = all_log_text(caplog)
    for secret in RECHECK_SECRETS:
        assert secret not in text, secret[:40]


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_lists_in_claude_json_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC4, local mode: the header variables in .claude.json's env object, which Claude Code applies to its
    environment as it applies settings.json env, just before it (the recheck of RA2), are read as header lists as in
    settings.json env: each header value of eight characters or more and the whole value, counted under
    claude:.claude.json and removed from a copy that prints the variables and the headers."""
    for name in HEADER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    worker_env = claude_folders(tmp_path / "creds", {"env": {**HEADER_VARIABLES, "NO_PROXY": "127.0.0.1"}})
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    pairs = set(redaction.credential_file_pairs(config, {}))
    assert pairs == {("claude:.claude.json", value) for value in set().union(*EXPECTED_VALUES.values())}

    copied = local_copy(tmp_path, monkeypatch, config, Path(worker_env["CODEX_HOME"]), *PRINTED)
    assert_removed(copied, ENV_SECRETS)
    assert b"> x-gateway-team: [REDACTED:claude:.claude.json]" in copied
    assert b"> x-honeycomb-team: [REDACTED:claude:.claude.json]" in copied
    assert f"> x-short: {SHORT}".encode() in copied
    assert read_meta(config)["redactions"]["claude:.claude.json"] == 8  # the three whole values, then five headers


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_reference_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, local mode, a variable that MCP headers name, set in more than one layer of Claude Code's environment
    (the recheck of RA2): LAB_LAYERED in the dispatcher's environment, in .claude.json's env and in settings.json's
    env, LAB_LOWER in the first two. A whole header value is expanded as Claude Code 2.1.98 expands it on a host with no
    managed settings (which redaction.py does not read), settings.json's env over .claude.json's over the environment,
    so each whole value it sends is taken and no whole value it does not send; and every value of a named variable is
    taken, whichever layer holds it, since a hidden one is still in a file
    or an environment that a local worker can print. All are counted under claude:.claude.json and removed from a copy
    that prints the headers, another worker's environment and .claude.json itself."""
    monkeypatch.setenv("LAB_LAYERED", LAYER_ENVIRONMENT)
    monkeypatch.setenv("LAB_LOWER", LOWER_ENVIRONMENT)
    headers = {"X-Team": "${LAB_LAYERED}", "Authorization": "Bearer ${LAB_LAYERED}", "X-Org": "org ${LAB_LOWER}"}
    claude_json = {
        "env": {"LAB_LAYERED": LAYER_CLAUDE_JSON, "LAB_LOWER": LOWER_CLAUDE_JSON},
        "mcpServers": {"docs": {"type": "http", "url": "https://mcp.invalid/mcp", "headers": headers}},
    }
    worker_env = claude_folders(tmp_path / "creds", claude_json, {"LAB_LAYERED": LAYER_SETTINGS})
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    # Without the headers, nothing would give these values: no variable's name holds a secret word.
    assert not set(RECHECK_SECRETS) & redaction.environment_values(dict(os.environ))

    values = redaction.secret_values(config, "local", dict(os.environ), None)
    sent = {LAYER_SETTINGS, f"Bearer {LAYER_SETTINGS}", LOWER_CLAUDE_JSON, f"org {LOWER_CLAUDE_JSON}"}
    hidden = {LAYER_ENVIRONMENT, LAYER_CLAUDE_JSON, LOWER_ENVIRONMENT}
    assert {value for value, label in values.items() if label == "claude:.claude.json"} == {
        *headers.values(),  # as written (build step RA2's rule)
        *sent,
        *hidden,
    }
    for not_sent in (f"Bearer {LAYER_CLAUDE_JSON}", f"Bearer {LAYER_ENVIRONMENT}", f"org {LOWER_ENVIRONMENT}"):
        assert not_sent not in values, not_sent

    printed = [
        f"> X-Team: {LAYER_SETTINGS}",
        f"> Authorization: Bearer {LAYER_SETTINGS}",
        f"> X-Org: org {LOWER_CLAUDE_JSON}",
        f"LAB_LAYERED={LAYER_ENVIRONMENT}",  # another local worker's environment, which Claude Code's layers miss
        f"LAB_LOWER={LOWER_ENVIRONMENT}",
        json.dumps(claude_json),  # cat ~/.claude.json
    ]
    copied = local_copy(tmp_path, monkeypatch, config, Path(worker_env["CODEX_HOME"]), *printed)
    assert_removed(copied, [*sent, *hidden])
    assert b"canary-05" not in copied
    assert b"> Authorization: [REDACTED:claude:.claude.json]" in copied  # the whole value, longest first
    assert b"> X-Org: [REDACTED:claude:.claude.json]" in copied
    # Five printed lines, then .claude.json's two env values and its three header values as written.
    assert read_meta(config)["redactions"]["claude:.claude.json"] == 10
    assert_no_recheck_secret_logged(caplog)


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_header_reference_layer_rules() -> None:
    """TC4, the layers (redaction._resolved): a template's whole value takes each NAME from env, into which Claude
    Code merges the layers; its parts add the value that the NAME has in each layer, each once, short and blank ones
    left out as everywhere; a NAME that no layer sets gives its default."""
    layers = [
        {"LAB_X": "FAKE-environment-0601", "LAB_SHORT": "short7c"},
        {"LAB_X": "FAKE-json-env-0602", "LAB_BLANK": " " * 9},
        {"LAB_X": "FAKE-settings-0603", "LAB_BLANK": "FAKE-settings-0604"},
    ]
    env = {name: value for layer in layers for name, value in layer.items()}
    assert redaction._resolved([("template", "Bearer ${LAB_X}")], env, layers) == [
        "FAKE-settings-0603",
        "FAKE-environment-0601",
        "FAKE-json-env-0602",
        "Bearer FAKE-settings-0603",
    ]
    assert redaction._resolved([("template", "${LAB_BLANK}${LAB_SHORT}")], env, layers) == [
        "FAKE-settings-0604",
        "FAKE-settings-0604short7c",
    ]
    assert redaction._resolved([("template", "${LAB_UNSET:-FAKE-default-0605}")], env, layers) == ["FAKE-default-0605"]


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_expanded_header_values_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, local mode: an MCP header that names a 64 KiB value of .claude.json's own env 4,096 times, in a file of
    about 106 KB (a local worker can write both; the recheck of RA2). Expanded whole, it would be 268,435,456
    characters, which every copy would hold and escape: seconds and gigabytes per tick, or a MemoryError that fails
    the copy. The whole is not built: the file resolves within a second, no value taken is longer than the file, the
    variable's own value is still taken, and a copy ends ok with that value removed."""
    references = "${LAB_BIG}" * 4096
    claude_json = {
        "env": {"LAB_BIG": BIG_VALUE},
        "mcpServers": {"docs": {"type": "http", "url": "https://mcp.invalid/mcp", "headers": {"X-Team": references}}},
    }
    worker_env = claude_folders(tmp_path / "creds", claude_json)
    size = (Path(worker_env["CLAUDE_CONFIG_DIR"]) / ".claude.json").stat().st_size
    assert 100_000 < size < 110_000
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    started = time.monotonic()
    pairs = redaction.credential_file_pairs(config, {})
    assert time.monotonic() - started < 1
    assert max(len(value) for _label, value in pairs) <= size
    assert set(pairs) == {("claude:.claude.json", BIG_VALUE), ("claude:.claude.json", references)}

    started = time.monotonic()
    copied = local_copy(tmp_path, monkeypatch, config, Path(worker_env["CODEX_HOME"]), f"> X-Team: {BIG_VALUE}")
    assert time.monotonic() - started < 5
    meta = read_meta(config)
    assert meta["status"] == "ok"
    assert meta["redactions"]["claude:.claude.json"] == 1
    assert b"> X-Team: [REDACTED:claude:.claude.json]" in copied
    assert b"canary-0506" not in copied
    assert_no_recheck_secret_logged(caplog)


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_expanded_header_values_total_bounded(
    tmp_path: Path,
) -> None:
    """TC4, local mode: 64 MCP headers that each name the 64 KiB value once, each with its own suffix (the recheck of
    RA2). A bound on each whole value would leave all 64, 4 MB together, and a larger file more: the whole values are
    taken in the file's order while they come to at most EXPANDED_HEADERS_MAX_CHARS together, and the variable's own
    value is taken whatever the bound."""
    headers = {f"X-Team-{index}": "${LAB_BIG}-" + str(index) for index in range(64)}
    claude_json = {
        "env": {"LAB_BIG": BIG_VALUE},
        "mcpServers": {"docs": {"type": "http", "url": "https://mcp.invalid/mcp", "headers": headers}},
    }
    worker_env = claude_folders(tmp_path / "creds", claude_json)
    config = copy_config(tmp_path, "codex", execution="local", workers=[worker("w1", "codex", worker_env)])
    values = [value for label, value in redaction.credential_file_pairs(config, {}) if label == "claude:.claude.json"]
    assert BIG_VALUE in values
    wholes = [value for value in values if value.startswith(BIG_VALUE + "-")]
    assert wholes == [f"{BIG_VALUE}-{index}" for index in range(len(wholes))]  # in the file's order
    taken = sum(len(whole) for whole in wholes)
    assert 0 < taken <= redaction.EXPANDED_HEADERS_MAX_CHARS < taken + len(f"{BIG_VALUE}-{len(wholes)}")


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_expanded_header_value_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC4, the bound's rules (redaction._resolved), with EXPANDED_HEADERS_MAX_CHARS lowered here: whole values are
    taken in order while they fit together, one that does not fit is skipped and a later one that fits is still
    taken; a template met again is counted once; the parts are taken whatever the bound; and no whole value is built
    from a text longer than the bound, however short it would be."""
    env = {"LAB_X": "FAKE-0611", "LAB_EMPTY": ""}
    first, longer, last = "a ${LAB_X}", "b ${LAB_X} ${LAB_X} ${LAB_X}", "c ${LAB_X}"  # 11, 31 and 11 when expanded
    monkeypatch.setattr(redaction, "EXPANDED_HEADERS_MAX_CHARS", 40)
    references = [("template", text) for text in (first, longer, last)]
    assert redaction._resolved(references, env) == ["FAKE-0611", "a FAKE-0611", "c FAKE-0611"]
    monkeypatch.setattr(redaction, "EXPANDED_HEADERS_MAX_CHARS", 22)
    references = [("template", text) for text in (first, first, first, last)]
    assert redaction._resolved(references, env) == ["FAKE-0611", "a FAKE-0611", "c FAKE-0611"]
    monkeypatch.setattr(redaction, "EXPANDED_HEADERS_MAX_CHARS", 8)
    assert redaction._resolved([("template", "Bearer ${LAB_X}")], env) == ["FAKE-0611"]
    padded = "${LAB_EMPTY}" * 4 + "FAKE-tail-0612"  # 62 characters, 14 when expanded
    monkeypatch.setattr(redaction, "EXPANDED_HEADERS_MAX_CHARS", 40)
    assert redaction._resolved([("template", padded)], env) == []
    monkeypatch.setattr(redaction, "EXPANDED_HEADERS_MAX_CHARS", 62)
    assert redaction._resolved([("template", padded)], env) == ["FAKE-tail-0612"]


# --- fix round 3 of RA2: what the exact-value redaction does not read ------------------------------------------------

REPOSITORY = Path(__file__).resolve().parents[2]
# The places from which Claude Code 2.1.98 takes environment variables or MCP headers and that redaction.py does not
# read, and codex 0.118.0's configuration layers besides $CODEX_HOME/config.toml, as every document names them.
NOT_READ = (
    "/etc/claude-code",
    "/Library/Application Support/ClaudeCode",
    "managed-settings.json",
    "managed-settings.d",
    "remote-settings.json",
    "managed-mcp.json",
    ".mcp.json",
    ".config.json",
    ".claude-custom-oauth.json",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "/etc/codex/config.toml",
    "/etc/codex/managed_config.toml",
    ".codex/config.toml",
)


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        (
            "cairn/src/cairn/dispatcher/repair/redaction.py",
            "# What is not read",
            "# Header lists in an environment",
        ),
        (
            "tools/repair-phase2/README.md",
            "**Local mode: what the dispatcher's redaction does not read.**",
            "**f. Start the agent**",
        ),
        ("docs/specs/repair-agent.md", "  local 模式下 Dispatcher 按值删除的", "- f. 以 `cairn-repair` 启动"),
        ("docs/specs/dispatcher-design.md", "    - 不读取的来源", "    JSON 文件用 `json.loads` 解析"),
    ],
    ids=["redaction-py", "runbook", "short-runbook", "dispatcher-design"],
)
def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo_names_what_it_does_not_read(
    path: str, start: str, end: str
) -> None:
    """TC4's boundary, fix round 3 of RA2 (the recheck's RECHECK-4): the exact-value set reads the seven credential
    files and the environments of spec 8.8, and the three layers of Claude Code's environment that redaction.py reads
    are not all of it. Claude Code 2.1.98 applies the env of its managed settings last, over settings.json's (the
    server-managed settings cached in remote-settings.json, a macOS configuration profile, or managed-settings.json and
    managed-settings.d in /etc/claude-code), reads MCP headers from managed-mcp.json and from the .mcp.json of the work
    folder and the folders above it, and reads its global config from a legacy .config.json or, with
    CLAUDE_CODE_CUSTOM_OAUTH_URL, from .claude-custom-oauth.json; codex 0.118.0 loads further configuration layers. A
    secret set only there gets only the agent's pattern layer, so the module's comment, both runbooks and the design
    doc name every one of these places, and neither the module nor the design doc says any longer that Claude Code's
    environment has those three layers alone."""
    text = (REPOSITORY / path).read_text(encoding="utf-8")
    first = text.index(start)
    section = text[first : text.index(end, first)]
    assert [place for place in NOT_READ if place not in section] == []
    for claim in ("Claude Code's environment has three layers", "Claude Code 的环境有三层"):
        assert claim not in text, claim
