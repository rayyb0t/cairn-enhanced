"""Fixes from the early review of the Phase 2 branch (build step R1): URL passwords in the local CLIs' own settings
(SECURITY-3), and aliases in dispatch.yaml's repair: section (ROBUSTNESS-1).

SECURITY-3 is TC4's case of those settings (spec 2.2, 8.8): in local mode, a URL with a password among the values of
a local CLI's credential file, such as an HTTPS_PROXY in Claude Code's settings.json env or in codex's config.toml
shell_environment_policy.set, gives its user:pass and its password to the exact-value redaction, counted under the
file's label, as the same URL in the dispatcher's own environment does under the variable's name. The review of R1
added TC4's case of the reviewers' own URLs (SPEC-2): a password in a reviewer's REVIEW_BASE_URL or proxy is removed
too, counted under REVIEW_BASE_URL and REVIEW_PROXY; and its recheck the case of dispatch.yaml's server (RECHECK-2),
whose URL password is counted under server. TC4's other cases are in test_repair_transcripts.py and
test_repair_dispatcher_api_format.py.

ROBUSTNESS-1 has no spec 16 id: it guards DS27's parsing of the section and step 5's hiding of its values. A YAML
anchor used through many aliases in the repair: section costs about what the anchor holds while dispatch.yaml loads,
as it does in today's sections, and each error still names its key and prints no value (input_value=<hidden>). The
review of R1 added the REVIEW_API_KEY that the str field refuses once it is stripped (SAFETY-1), and its recheck a
price that validate_price refuses (RECHECK-1).

Every value here is synthetic: the passwords and keys are obviously fake, and the hosts are under .invalid.
"""

from __future__ import annotations

import base64
import gc
import json
import logging
import tracemalloc
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml
from pydantic import ValidationError
from repair_dispatcher_runs import run_config
from repair_dispatcher_support import (
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    dispatch_payload,
    reset_repair_process_state,
    write_token,
)
from test_repair_transcripts_support import (
    SESSION_IDS,
    FakeClock,
    all_log_text,
    copy_config,
    fixture_bytes,
    make_run,
    read_meta,
    read_transcript,
)

from cairn.dispatcher.config import DispatchConfig, RepairConfig
from cairn.dispatcher.repair import housekeeping, redaction, transcripts
from cairn.dispatcher.repair.transcripts import CopySchedule


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fresh once-per-process state; no real Docker; no record holds the token or a reviewer key."""
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
    assert FAKE_TOKEN not in text
    assert FAKE_REVIEWER_KEY not in text


# SECURITY-3: TC4's case of URL passwords in the local CLIs' own settings ----------------------------------------------

SETTINGS_PASSWORD = "settings-proxy-pass-canary-0101"  # noqa: S105 - fake
CODEX_PROXY_PASSWORD = "codex-proxy-pass-canary-0102"  # noqa: S105 - fake
CODEX_PROVIDER_PASSWORD = "codex-provider-pass/canary-0103"  # noqa: S105 - fake; written percent-encoded in its URL
CLAUDE_JSON_PASSWORD = "claude-json-mcp-pass-canary-0104"  # noqa: S105 - fake
PI_MODELS_PASSWORD = "pi-models-base-url-pass-canary-0105"  # noqa: S105 - fake
CODEX_AUTH_PASSWORD = "codex-auth-proxy-pass-canary-0106"  # noqa: S105 - fake
ENVIRONMENT_PASSWORD = "environment-proxy-pass-canary-0107"  # noqa: S105 - fake
SHORT_USERINFO = "u:short"  # seven characters, and a five-character password: below the eight of the rule

SETTINGS_URL = f"http://cuser:{SETTINGS_PASSWORD}@proxy.invalid:3128"
CODEX_PROXY_URL = f"http://xuser:{CODEX_PROXY_PASSWORD}@proxy.invalid:3128"
CODEX_PROVIDER_URL = f"https://puser:{quote(CODEX_PROVIDER_PASSWORD, safe='')}@provider.invalid/v1"
CLAUDE_JSON_URL = f"https://juser:{CLAUDE_JSON_PASSWORD}@mcp.invalid/mcp"
PI_MODELS_URL = f"https://muser:{PI_MODELS_PASSWORD}@provider.invalid/v1"
CODEX_AUTH_URL = f"http://auser:{CODEX_AUTH_PASSWORD}@proxy.invalid:3128"
ENVIRONMENT_URL = f"http://duser:{ENVIRONMENT_PASSWORD}@proxy.invalid:3128"


def _cli_homes_with_urls(base: Path) -> dict[str, str]:
    """Credential files of spec 8.8 that hold URLs with passwords, under base; the worker env that finds them.

    Claude Code's settings.json env and codex's config.toml shell_environment_policy.set give the CLI, and so every
    command the worker runs, an HTTPS_PROXY; config.toml also has a provider's base_url, .claude.json an MCP server's
    url, pi's models.json a provider's baseUrl, and codex's auth.json, a credential-only file, one URL more.
    """
    home, claude, codex, pi = base / "home", base / "claude-config", base / "codex-home", base / "pi-agent"
    for folder in (home, claude, codex, pi):
        folder.mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "HTTPS_PROXY": SETTINGS_URL,
                    "HTTP_PROXY": f"http://{SHORT_USERINFO}@proxy.invalid:3128",
                    "NO_PROXY": "127.0.0.1",
                }
            }
        )
    )
    (claude / ".claude.json").write_text(
        json.dumps({"mcpServers": {"docs": {"type": "http", "url": CLAUDE_JSON_URL}}, "theme": "dark"})
    )
    (codex / "config.toml").write_text(
        "[shell_environment_policy]\n"
        f'set = {{ HTTPS_PROXY = "{CODEX_PROXY_URL}" }}\n\n'
        "[model_providers.cairn]\n"
        'name = "cairn"\n'
        f'base_url = "{CODEX_PROVIDER_URL}"\n'
    )
    (codex / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "proxy": CODEX_AUTH_URL}))
    (pi / "models.json").write_text(
        json.dumps({"providers": {"cairn": {"baseUrl": PI_MODELS_URL, "api": "openai-completions"}}})
    )
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "PI_CODING_AGENT_DIR": str(pi),
    }


def _session_with(*texts: str) -> bytes:
    """codex's pinned session file with lines appended that hold texts, as a tool's output would."""
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "tool_output", "text": text}}) for text in texts]
    return fixture_bytes("codex") + "".join(line + "\n" for line in lines).encode()


def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, its case of URL passwords in the local CLIs' own settings (the early review's SECURITY-3). In local
    mode, a run that prints its environment (env, curl -v) and the bare passwords of the URLs that the local CLIs'
    credential files hold, an HTTPS_PROXY in settings.json env and in config.toml shell_environment_policy.set among
    them, leaves no user:pass and no password, decoded or not, in the copy or in any log record; meta.json counts them
    under the files' labels. Each such URL gives its user:pass and its password, as the same kind of URL in the
    dispatcher's own environment does under the variable's name; a password under eight characters is left alone.
    """
    worker_env = _cli_homes_with_urls(tmp_path / "creds")
    workers = [
        {"name": "w1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": worker_env}
    ]
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)

    pairs = set(redaction.credential_file_pairs(config, {}))
    expected = {
        ("claude:settings.json", f"cuser:{SETTINGS_PASSWORD}"),
        ("claude:settings.json", SETTINGS_PASSWORD),
        ("codex:config.toml", f"xuser:{CODEX_PROXY_PASSWORD}"),
        ("codex:config.toml", CODEX_PROXY_PASSWORD),
        ("codex:config.toml", f"puser:{quote(CODEX_PROVIDER_PASSWORD, safe='')}"),
        ("codex:config.toml", quote(CODEX_PROVIDER_PASSWORD, safe="")),
        ("codex:config.toml", CODEX_PROVIDER_PASSWORD),
        ("claude:.claude.json", f"juser:{CLAUDE_JSON_PASSWORD}"),
        ("claude:.claude.json", CLAUDE_JSON_PASSWORD),
        ("pi:models.json", f"muser:{PI_MODELS_PASSWORD}"),
        ("pi:models.json", PI_MODELS_PASSWORD),
        ("codex:auth.json", CODEX_AUTH_URL),  # a credential-only file: every string of at least 16 characters
        ("codex:auth.json", f"auser:{CODEX_AUTH_PASSWORD}"),
        ("codex:auth.json", CODEX_AUTH_PASSWORD),
    }
    assert pairs == expected, sorted(pairs ^ expected)

    # The same rule as for the dispatcher's own environment: a URL there gives its user:pass and its password too.
    values = redaction.secret_values(config, "local", {"HTTPS_PROXY": ENVIRONMENT_URL}, None)
    assert {value for value, name in values.items() if name == "HTTPS_PROXY"} == {
        f"duser:{ENVIRONMENT_PASSWORD}",
        ENVIRONMENT_PASSWORD,
    }
    assert {value for value, name in values.items() if name == "claude:settings.json"} == {
        f"cuser:{SETTINGS_PASSWORD}",
        SETTINGS_PASSWORD,
    }

    sessions = Path(worker_env["CODEX_HOME"]) / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
    printed = [
        f"HTTPS_PROXY={SETTINGS_URL}",  # env, in a claudecode worker that settings.json gives the proxy
        f"proxy password {SETTINGS_PASSWORD}",
        f"HTTPS_PROXY={CODEX_PROXY_URL}",  # env, in a codex worker that shell_environment_policy gives the proxy
        f"proxy password {CODEX_PROXY_PASSWORD}",
        f"base_url = {CODEX_PROVIDER_URL}",
        f"decoded {CODEX_PROVIDER_PASSWORD}",
        f"mcp server {CLAUDE_JSON_URL}",
        f"baseUrl {PI_MODELS_URL}",
        f"proxy password {CODEX_AUTH_PASSWORD}",
        f"HTTP_PROXY=http://{SHORT_USERINFO}@proxy.invalid:3128",
    ]
    source.write_bytes(_session_with(*printed))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path / "work"), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    clock.at(1260)  # 70% of the run's 1800 s budget: the first copy
    schedule.tick()

    copied = read_transcript(config)
    secrets = [
        SETTINGS_PASSWORD,
        CODEX_PROXY_PASSWORD,
        CODEX_PROVIDER_PASSWORD,
        quote(CODEX_PROVIDER_PASSWORD, safe=""),
        CLAUDE_JSON_PASSWORD,
        PI_MODELS_PASSWORD,
        CODEX_AUTH_PASSWORD,
    ]
    for secret in secrets:
        for form in redaction._forms(secret):
            assert form not in copied, secret
    assert b"canary-01" not in copied
    assert b"HTTPS_PROXY=http://[REDACTED:claude:settings.json]@proxy.invalid:3128" in copied
    assert f"HTTP_PROXY=http://{SHORT_USERINFO}@proxy.invalid:3128".encode() in copied  # under eight: left alone
    redactions = read_meta(config)["redactions"]
    # Each URL's user:pass once (longest first, so its password goes with it), and each bare password once.
    assert redactions["claude:settings.json"] == 2
    assert redactions["codex:config.toml"] == 4
    assert redactions["claude:.claude.json"] == 1
    assert redactions["pi:models.json"] == 1
    assert redactions["codex:auth.json"] == 1
    text = all_log_text(caplog)
    assert all(secret not in text for secret in secrets)


# The review of R1, SPEC-2: TC4's case of URL passwords in the reviewers' own settings ---------------------------------

REVIEWER_URL_PASSWORD = "reviewer-base-url-pass-canary-0108"  # noqa: S105 - fake
REVIEWER_PROXY_PASSWORD = "reviewer-proxy-pass/canary-0109"  # noqa: S105 - fake; written percent-encoded in its URL
REVIEWER_URL = f"https://ruser:{REVIEWER_URL_PASSWORD}@llm.invalid/v1"
REVIEWER_PROXY = f"http://puser:{quote(REVIEWER_PROXY_PASSWORD, safe='')}@proxy.invalid:3128"


def test_redaction_covers_the_reviewers_url_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, its case of the reviewers' URLs (spec 2.2: the reviewers' keys and URL passwords). A reviewer's
    REVIEW_BASE_URL with a password (a model server behind basic auth) gives its user:pass and its password, counted
    under REVIEW_BASE_URL, and its proxy with a password (a corporate proxy) gives them under REVIEW_PROXY, in both
    modes, as its REVIEW_API_KEY is removed: a local worker can read dispatch.yaml and the agent's settings file and
    print them. A run that prints the settings file's reviewer entry and the bare passwords leaves none of them,
    decoded or not, in the copy or in any log record; a reviewer URL without a password gives nothing.
    """
    home = tmp_path / "home"  # no credential file of a local CLI: only the reviewers' values are collected
    worker_env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
    }
    workers = [
        {"name": "w1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": worker_env}
    ]
    with_passwords = {
        "name": "reviewer_url",
        "env": {"REVIEW_MODEL": "test-model", "REVIEW_API_KEY": FAKE_REVIEWER_KEY, "REVIEW_BASE_URL": REVIEWER_URL},
        "proxy": REVIEWER_PROXY,
    }
    without = {
        "name": "reviewer_plain",
        "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "https://plain-user@provider.invalid/v1"},
        "proxy": "http://proxy.invalid:3128",
    }
    config = copy_config(tmp_path, "codex", execution="local", workers=workers, reviewers=[with_passwords, without])

    expected = {
        FAKE_REVIEWER_KEY: "REVIEW_API_KEY",
        f"ruser:{REVIEWER_URL_PASSWORD}": "REVIEW_BASE_URL",
        REVIEWER_URL_PASSWORD: "REVIEW_BASE_URL",
        f"puser:{quote(REVIEWER_PROXY_PASSWORD, safe='')}": "REVIEW_PROXY",
        quote(REVIEWER_PROXY_PASSWORD, safe=""): "REVIEW_PROXY",
        REVIEWER_PROXY_PASSWORD: "REVIEW_PROXY",
    }
    for execution in ("container", "local"):
        assert redaction.secret_values(config, execution, {}, None) == expected, execution

    sessions = home / ".codex" / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
    settings_entry = {  # the reviewer as `cat agent-settings.json` prints it (spec 8.3, Content)
        "name": "reviewer_url",
        "model": "test-model",
        "base_url": REVIEWER_URL,
        "api_key": FAKE_REVIEWER_KEY,
        "api_format": "openai_chat",
        "proxy": REVIEWER_PROXY,
    }
    printed = [
        json.dumps(settings_entry),
        f"model server password {REVIEWER_URL_PASSWORD}",
        f"proxy password {REVIEWER_PROXY_PASSWORD}",
    ]
    source.write_bytes(_session_with(*printed))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path / "work"), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    clock.at(1260)  # 70% of the run's 1800 s budget: the first copy
    schedule.tick()

    copied = read_transcript(config)
    secrets = [REVIEWER_URL_PASSWORD, REVIEWER_PROXY_PASSWORD, quote(REVIEWER_PROXY_PASSWORD, safe="")]
    for secret in [*secrets, FAKE_REVIEWER_KEY]:
        for form in redaction._forms(secret):
            assert form not in copied, secret
    assert b"canary-01" not in copied
    assert b"https://[REDACTED:REVIEW_BASE_URL]@llm.invalid/v1" in copied
    assert b"http://[REDACTED:REVIEW_PROXY]@proxy.invalid:3128" in copied
    redactions = read_meta(config)["redactions"]
    # Each URL's user:pass once (longest first, so its password goes with it), and each bare password once.
    assert redactions["REVIEW_BASE_URL"] == 2
    assert redactions["REVIEW_PROXY"] == 2
    assert redactions["REVIEW_API_KEY"] == 1
    text = all_log_text(caplog)
    assert all(secret not in text for secret in secrets)


# The recheck of R1, RECHECK-2: TC4's case of a URL password in dispatch.yaml's server ---------------------------------

SERVER_PASSWORD = "server-url-pass/canary-0110"  # noqa: S105 - fake; written percent-encoded in its URL
SERVER_URL = f"https://suser:{quote(SERVER_PASSWORD, safe='')}@cairn.invalid"


def test_redaction_covers_the_server_url_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, its case of dispatch.yaml's server (spec 2.2: URL passwords). A server URL with a password (a Cairn
    server behind a proxy with basic auth, which requests sends from the URL) gives its user:pass and its password,
    counted under server, in both modes, as a reviewer's URLs give theirs: a local worker can read the URL in
    dispatch.yaml and in the agent's settings file, and print it. A run that prints the settings file and the bare
    password leaves neither, decoded or not, in the copy or in any log record; a server URL without a password, with
    or without a user, gives nothing.
    """
    token_file = write_token(tmp_path / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
    home = tmp_path / "home"  # no credential file of a local CLI
    worker_env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
    }
    workers = [
        {"name": "w1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": worker_env}
    ]

    def server_config(server: str, execution: str) -> DispatchConfig:
        local_workers = workers if execution == "local" else None  # a container worker needs its env keys
        return run_config("codex", server, token_file, tmp_path / "copies", execution=execution, workers=local_workers)

    expected = {
        f"suser:{quote(SERVER_PASSWORD, safe='')}": "server",
        quote(SERVER_PASSWORD, safe=""): "server",
        SERVER_PASSWORD: "server",
    }
    for execution in ("container", "local"):
        values = redaction.secret_values(server_config(SERVER_URL, execution), execution, {}, None)
        assert {value: name for value, name in values.items() if name == "server"} == expected, execution
        for server in ("http://127.0.0.1:9", "https://suser@cairn.invalid"):
            values = redaction.secret_values(server_config(server, execution), execution, {}, None)
            assert "server" not in values.values(), (execution, server)

    config = server_config(SERVER_URL, "local")
    settings = housekeeping.agent_settings(config)
    assert settings["server"] == SERVER_URL  # the agent's settings file holds the URL as dispatch.yaml has it
    sessions = home / ".codex" / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
    source.write_bytes(_session_with(json.dumps(settings), f"server password {SERVER_PASSWORD}"))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path / "work"), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    clock.at(1260)  # 70% of the run's 1800 s budget: the first copy
    schedule.tick()

    copied = read_transcript(config)
    secrets = [SERVER_PASSWORD, quote(SERVER_PASSWORD, safe="")]
    for secret in secrets:
        for form in redaction._forms(secret):
            assert form not in copied, secret
    assert b"canary-01" not in copied
    assert b"https://[REDACTED:server]@cairn.invalid" in copied
    # The URL's user:pass once (longest first, so its password goes with it), and the bare password once.
    assert read_meta(config)["redactions"]["server"] == 2
    text = all_log_text(caplog)
    assert all(secret not in text for secret in secrets)


# ROBUSTNESS-1: aliases in the repair: section -------------------------------------------------------------------------

BIG = 256 * 1024
ALIASES = 400
FEW_MIB = 4 * 1024 * 1024  # what validating any of these files may take; before the fix they took 50 to 200 MiB
BIG_MODEL = "big-model-canary-" + "m" * BIG
BIG_KEY = "big-key-canary-" + "k" * BIG
BIG_HEX = "0x" + "f" * BIG  # YAML 1.1 reads it as an int of BIG / 2 bytes: a hexadecimal int has no digit limit
BASE_URL = "http://127.0.0.1:11434/v1"
PRICE_RULE = "price must have exactly the keys input_per_mtok and output_per_mtok"


def _dispatch_text(section: str, common_env: str = "{}") -> str:
    """dispatch.yaml with a common_env written as YAML text, then conftest's payload, then a repair: section."""
    payload = dispatch_payload()
    payload.pop("common_env")
    return (
        f"common_env: {common_env}\n"
        + yaml.safe_dump(payload, sort_keys=False)
        + "repair:\n  token_file: /srv/cairn-test/repair.token\n"
        + section
    )


def _reviewers(entries: list[str]) -> str:
    return "  reviewers:\n" + "".join(f"    - {entry}\n" for entry in entries)


def _scalar_in_projects() -> str:
    aliases = ", ".join(["*s"] * (2 * ALIASES))
    return _dispatch_text(
        f"  projects: [{aliases}]\n"
        + _reviewers([f"{{name: r0, env: {{REVIEW_MODEL: m, REVIEW_BASE_URL: '{BASE_URL}'}}}}"]),
        common_env=f'{{BIG: &s "{BIG_MODEL}"}}',
    )


def _aliased_reviewer(model: str) -> str:
    first = f'&r {{name: r0, env: {{REVIEW_MODEL: "{model}", REVIEW_BASE_URL: "{BASE_URL}"}}}}'
    return _dispatch_text(_reviewers([first] + ["*r"] * ALIASES))


def _shared_env() -> str:
    first = f'{{name: r0, env: &e {{REVIEW_MODEL: "{BIG_MODEL}", REVIEW_BASE_URL: "{BASE_URL}"}}}}'
    return _dispatch_text(_reviewers([first] + [f"{{name: r{i}, env: *e}}" for i in range(1, ALIASES)]))


def _set_as_env() -> str:
    """A !!set of 2,000 strings as every reviewer's env: refused in each place, as no mapping."""
    items = ", ".join(f"set-canary-{i}" for i in range(2000))
    first = f"{{name: r0, env: &e !!set {{{items}}}}}"
    return _dispatch_text(_reviewers([first] + [f"{{name: r{i}, env: *e}}" for i in range(1, ALIASES)]))


def _shared_scalar(first_env: str, other_env: str, extra_first: str = "", extra_other: str = "") -> str:
    first = f'{{name: r0, env: {{{first_env}, REVIEW_BASE_URL: "{BASE_URL}"}}{extra_first}}}'
    others = [
        f'{{name: r{i}, env: {{{other_env}, REVIEW_BASE_URL: "{BASE_URL}"}}{extra_other}}}' for i in range(1, ALIASES)
    ]
    return _dispatch_text(_reviewers([first, *others]))


def _validated(text: str) -> tuple[DispatchConfig | ValidationError, int]:
    """DispatchConfig.model_validate of the parsed text: what it gave or raised, and the Python memory it took at
    its peak, beyond what the parsed YAML already held."""
    data = yaml.safe_load(text)
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        try:
            outcome: DispatchConfig | ValidationError = DispatchConfig.model_validate(data)
        except ValidationError as error:
            outcome = error
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        if started:
            tracemalloc.stop()
    return outcome, peak


def _refused(outcome: DispatchConfig | ValidationError) -> ValidationError:
    assert isinstance(outcome, ValidationError), outcome
    message = str(outcome)
    assert "canary" not in message, message[:2000]
    for error in outcome.errors():
        assert "canary" not in repr(error["input"])
    return outcome


def _loaded(outcome: DispatchConfig | ValidationError) -> list[Any]:
    assert isinstance(outcome, DispatchConfig), str(outcome)[:2000]
    assert outcome.repair is not None
    return outcome.repair.reviewers


@pytest.mark.parametrize(
    "case",
    [
        "scalar-in-projects",
        "aliased-reviewer",
        "aliased-reviewer-that-fails",
        "set-as-env",
        "shared-env",
        "shared-model",
        "shared-binary-key",
        "shared-int",
        "refused-binary-key",
        "refused-surrogate-key",
        "refused-price",
    ],
)
def test_aliases_in_the_repair_section_cost_no_more_than_their_anchors(case: str) -> None:
    """The early review's ROBUSTNESS-1. A 256 KiB YAML anchor used through hundreds of aliases in the repair:
    section, as a scalar (a string, a !!binary key, an int) or as a reviewer's mapping, env or price, takes a few MiB
    to validate, not one copy per alias (50 to 200 MiB before the fix). Where validation refuses the section, each
    place still gets its error, which names its key and prints its input hidden (input_value=<hidden> for a scalar),
    and all of them hold one hidden copy; where it loads, every place the anchor fills holds one plain value, as a
    section of today's dispatch.yaml does.

    The review of R1 (SAFETY-1) added the two keys that the str field refuses only once the whitespace around them is
    removed (spec 8.3): a !!binary key that is not UTF-8, and a string key with a lone surrogate. Each was 100 and
    200 MiB, one stripped copy for each place and its error. The recheck of R1 (RECHECK-1) added a price mapping that
    validate_price refuses, 2,000 keys more than its two: each place's error kept the plain dict that pydantic made of
    it for that place, about 40 MiB in all.
    """
    if case == "scalar-in-projects":
        outcome, peak = _validated(_scalar_in_projects())
        refused = _refused(outcome)
        errors = refused.errors()
        assert [error["loc"] for error in errors] == [("repair", "projects", i) for i in range(2 * ALIASES)]
        assert {error["type"] for error in errors} == {"string_too_long"}
        assert len({id(error["input"]) for error in errors}) == 1
        assert str(refused).count("input_value=<hidden>") == 2 * ALIASES
    elif case == "aliased-reviewer":
        outcome, peak = _validated(_aliased_reviewer(BIG_MODEL))
        errors = _refused(outcome).errors()
        assert [(error["loc"], error["msg"]) for error in errors] == [
            (("repair", "reviewers"), "Value error, reviewer names must be unique")
        ]
    elif case == "aliased-reviewer-that-fails":
        outcome, peak = _validated(_aliased_reviewer("<" + BIG_MODEL + ">"))
        refused = _refused(outcome)
        errors = refused.errors()
        assert [error["loc"] for error in errors] == [
            ("repair", "reviewers", i, "env", "REVIEW_MODEL") for i in range(ALIASES + 1)
        ]
        assert len({id(error["input"]) for error in errors}) == 1
        assert str(refused).count("input_value=<hidden>") == ALIASES + 1
    elif case == "set-as-env":
        outcome, peak = _validated(_set_as_env())
        refused = _refused(outcome)
        errors = refused.errors()
        assert [(error["loc"], error["type"]) for error in errors] == [
            (("repair", "reviewers", i, "env"), "model_type") for i in range(ALIASES)
        ]
        assert len({id(error["input"]) for error in errors}) == 1
        assert str(refused).count("input_value={<hidden>, <hidden>,") == ALIASES
    elif case == "shared-env":
        outcome, peak = _validated(_shared_env())
        values = [reviewer.env.REVIEW_MODEL for reviewer in _loaded(outcome)]
        assert len(values) == ALIASES and values[0] == BIG_MODEL and type(values[0]) is str
        assert len({id(value) for value in values}) == 1
    elif case == "shared-model":
        outcome, peak = _validated(_shared_scalar(f'REVIEW_MODEL: &m "{BIG_MODEL}"', "REVIEW_MODEL: *m"))
        values = [reviewer.env.REVIEW_MODEL for reviewer in _loaded(outcome)]
        assert len(values) == ALIASES and values[0] == BIG_MODEL and type(values[0]) is str
        assert len({id(value) for value in values}) == 1
    elif case == "shared-binary-key":
        encoded = base64.b64encode(BIG_KEY.encode()).decode()
        outcome, peak = _validated(
            _shared_scalar(
                f"REVIEW_MODEL: m, REVIEW_API_KEY: &k !!binary {encoded}", "REVIEW_MODEL: m, REVIEW_API_KEY: *k"
            )
        )
        values = [reviewer.env.REVIEW_API_KEY for reviewer in _loaded(outcome)]
        assert len(values) == ALIASES and values[0] == BIG_KEY and type(values[0]) is str
        assert len({id(value) for value in values}) == 1
    elif case in ("refused-binary-key", "refused-surrogate-key"):
        if case == "refused-binary-key":
            key = "!!binary " + base64.b64encode(b"\xff" + BIG_KEY.encode()).decode()  # not UTF-8
        else:
            key = f'" {BIG_KEY}\\ud800 "'  # a YAML escape that gives a lone surrogate, with spaces to remove
        outcome, peak = _validated(
            _shared_scalar(f"REVIEW_MODEL: m, REVIEW_API_KEY: &k {key}", "REVIEW_MODEL: m, REVIEW_API_KEY: *k")
        )
        refused = _refused(outcome)
        errors = refused.errors()
        assert [(error["loc"], error["type"]) for error in errors] == [
            (("repair", "reviewers", i, "env", "REVIEW_API_KEY"), "string_unicode") for i in range(ALIASES)
        ]
        assert len({id(error["input"]) for error in errors}) == 1
        assert str(refused).count("input_value=<hidden>") == ALIASES
    elif case == "refused-price":
        keys = ", ".join(f"k{i}: 0.5" for i in range(2000))
        outcome, peak = _validated(
            _shared_scalar(
                "REVIEW_MODEL: m",
                "REVIEW_MODEL: m",
                f", price: &p {{input_per_mtok: 1.0, output_per_mtok: 2.0, {keys}}}",
                ", price: *p",
            )
        )
        refused = _refused(outcome)
        errors = refused.errors()
        assert [(error["loc"], error["msg"]) for error in errors] == [
            (("repair", "reviewers", i, "price"), "Value error, " + PRICE_RULE) for i in range(ALIASES)
        ]
        assert len({id(error["input"]) for error in errors}) == 1
        assert str(refused).count("'k1999': <hidden>}") == ALIASES  # the end of each input, its values hidden
    else:
        outcome, peak = _validated(
            _shared_scalar("REVIEW_MODEL: m", "REVIEW_MODEL: m", f", max_tokens: &n {BIG_HEX}", ", max_tokens: *n")
        )
        values = [reviewer.max_tokens for reviewer in _loaded(outcome)]
        assert len(values) == ALIASES and values[0] == int(BIG_HEX, 16) and type(values[0]) is int
        assert len({id(value) for value in values}) == 1
    assert peak < FEW_MIB, f"{case}: {peak / 2**20:.1f} MiB"


def test_hidden_copies_each_value_once_and_gives_back_its_own_copies() -> None:
    """hidden() copies each string, number and container once per call, however many times it is met, and gives
    back what it made as it is, so the nested models' validators copy nothing again. Each hidden value still prints
    as <hidden> and equals the value it hides; the input is left as it was."""
    text = "test-leak-canary-0013"
    number = 10**40
    data: dict[str, Any] = {"a": text, "b": [text, number, text], "c": {"d": text, "n": number}, "e": [2.5]}
    data["f"] = data["c"]
    hidden = RepairConfig.hidden(data)

    assert hidden == data and repr(hidden).count("<hidden>") == 9 and "canary" not in repr(hidden)
    assert hidden["a"] is hidden["b"][0] is hidden["b"][2] is hidden["c"]["d"]
    assert hidden["b"][1] is hidden["c"]["n"] and hidden["f"] is hidden["c"]
    for made in (hidden, hidden["a"], hidden["b"], hidden["b"][1], hidden["c"], hidden["e"], hidden["e"][0]):
        assert RepairConfig.hidden(made) is made
    again = RepairConfig.hidden({"x": hidden["a"], "y": hidden["c"], "z": text})
    assert again["x"] is hidden["a"] and again["y"] is hidden["c"]
    assert again["z"] is not hidden["a"] and repr(again["z"]) == "<hidden>"  # a new call copies a plain value anew
    assert type(data["a"]) is str and data["f"] is data["c"] and type(data["b"]) is list  # the input as it was


def test_an_anchor_in_several_fields_is_validated_for_each() -> None:
    """A hidden value is made once per field, not once for all: an anchor that fills fields with other rules gets
    each field's own result. REVIEW_MODEL keeps the spaces around a value and REVIEW_API_KEY removes them (spec 8.3);
    a reviewer's name used as another's REVIEW_MODEL is a model there; and a value that is a valid REVIEW_MODEL and
    an invalid REVIEW_BASE_URL is refused as the latter, in each place that it fills one."""
    padded = " padded-value-0014 "
    first, second = _loaded(
        _validated(
            _dispatch_text(
                _reviewers(
                    [
                        f'{{name: &n r0, env: {{REVIEW_MODEL: &v "{padded}", REVIEW_BASE_URL: "{BASE_URL}", '
                        "REVIEW_API_KEY: *v}}",
                        f'{{name: r1, env: {{REVIEW_MODEL: *n, REVIEW_BASE_URL: "{BASE_URL}", REVIEW_API_KEY: *v}}}}',
                    ]
                )
            )
        )[0]
    )
    made = [(reviewer.name, reviewer.env.REVIEW_MODEL, reviewer.env.REVIEW_API_KEY) for reviewer in (first, second)]
    assert made == [("r0", padded, padded.strip()), ("r1", "r0", padded.strip())]

    outcome = _validated(
        _dispatch_text(
            _reviewers(
                [
                    '{name: r0, env: {REVIEW_MODEL: &m test-model, REVIEW_BASE_URL: "http://127.0.0.1:9"}}',
                    "{name: r1, env: {REVIEW_MODEL: *m, REVIEW_BASE_URL: *m}}",
                    "{name: r2, env: {REVIEW_MODEL: *m, REVIEW_BASE_URL: *m}}",
                ]
            )
        )
    )[0]
    assert [(error["loc"], error["msg"]) for error in _refused(outcome).errors()] == [
        (("repair", "reviewers", i, "env", "REVIEW_BASE_URL"), "Value error, " + BASE_URL_RULE) for i in (1, 2)
    ]


BASE_URL_RULE = "REVIEW_BASE_URL must start with http:// or https://"


def test_a_refused_value_is_freed_with_its_error() -> None:
    """A hidden value keeps what validation made of it, never an error: pydantic's error holds its input where the
    garbage collector does not look, so an error kept on the value it holds would never be freed. Here one refused
    value fills three places; each gets its error, and once the errors are dropped the value is gone at once, with
    the collector off: validating it leaves no cycle that holds it."""
    env = {"REVIEW_MODEL": "<test-model>", "REVIEW_BASE_URL": "http://127.0.0.1:9"}
    section = RepairConfig.hidden(
        {"token_file": "/srv/cairn-test/repair.token", "reviewers": [{"name": "r", "env": env}] * 3}
    )
    refused = weakref.ref(section["reviewers"][0]["env"]["REVIEW_MODEL"])
    collecting = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(ValidationError) as caught:
            RepairConfig.model_validate(section)
        assert [error["loc"] for error in caught.value.errors()] == [
            ("reviewers", i, "env", "REVIEW_MODEL") for i in range(3)
        ]
        del section, caught
        assert refused() is None
    finally:
        if collecting:
            gc.enable()


def test_a_refused_key_and_its_stripped_copy_are_freed_with_their_error() -> None:
    """The review of R1 (SAFETY-1). A REVIEW_API_KEY is stripped once, however many places it fills, and its stripped
    copy is kept on it (spec 8.3: the whitespace around the key is removed before it is checked). A key that the str
    field refuses once stripped, here one with a lone surrogate, filling three places, gets an error in each, all
    naming that one stripped copy, printed as <hidden>; once the errors are dropped, the key and its stripped copy are
    gone at once, with the collector off: keeping the copy on the key leaves no cycle that holds either."""
    env = {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:9", "REVIEW_API_KEY": " key-0015\ud800 "}
    section = RepairConfig.hidden(
        {"token_file": "/srv/cairn-test/repair.token", "reviewers": [{"name": "r", "env": env}] * 3}
    )
    key = weakref.ref(section["reviewers"][0]["env"]["REVIEW_API_KEY"])
    collecting = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(ValidationError) as caught:
            RepairConfig.model_validate(section)
        errors = caught.value.errors()
        assert [(error["loc"], error["type"]) for error in errors] == [
            (("reviewers", i, "env", "REVIEW_API_KEY"), "string_unicode") for i in range(3)
        ]
        assert len({id(error["input"]) for error in errors}) == 1
        assert errors[0]["input"] == "key-0015\ud800" and repr(errors[0]["input"]) == "<hidden>"
        stripped = weakref.ref(errors[0]["input"])
        assert str(caught.value).count("input_value=<hidden>") == 3
        del section, caught, errors
        assert key() is None and stripped() is None
    finally:
        if collecting:
            gc.enable()
