"""Cross-component tests of spec §16 (build step 10): XC2, XC7 and XC8.

Each runs the dispatcher's own code and the agent's own code on the same files, in this environment, which has cairn as
a dev dependency (§9.1), so that the two parts can no longer drift apart unnoticed:

- XC2: the golden copy fixture, `cairn/tests/fixtures/repair_copy/proj_027/<run_id>/`, is what the dispatcher's copy
  writes (TC12's scenario, written again here and compared byte for byte) and what the agent's mirror reads (AG22).
- XC7: an 1800 s Claude Code run whose session file ends with a tool call written at 65% and does not change until 85%.
  The dispatcher's copy schedule copies it once at 70% and then only rewrites `meta.json`'s checked time; the agent's
  review at the window's opening, and every re-read until the window closes, accepts the copy and lists that call as
  running at the cut. Without the checked-time rewrite the same review gets `stale`.
- XC8: the agent reads the settings file that the dispatcher writes from spec §8.3's example `repair:` section, with an
  `openai_chat` and an `anthropic_messages` reviewer and keys written with spaces around them; a setting that the
  dispatcher's type check accepts and the agent refuses stops the agent with a message that names the reviewer and the
  setting.

XC1 (DS9), XC3 (SV36), XC4 (DS8) and XC6 (SV46) are the cairn tests of those ids; XC5, the opt-in smoke, is in
`tests/cross_component_smoke/`. Every value here is synthetic: made-up tokens and keys, `test-model`, loopback stubs
only.
"""

from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import io
import json
import logging
import os
import posixpath
import socket
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from agentkit import CairnStub, ModelStub, Reply, chat_answer, decision_text, run_view, start_agent
from conftest import Layout, fake_secret
from records import CODEX, FIXTURES, GOLDEN_COPY_PROJECT, fixture_bytes

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import config as repair_config
from cairn.dispatcher.repair import housekeeping, redaction, transcripts
from cairn.dispatcher.repair.shadow import ShadowRun
from cairn.dispatcher.repair.transcripts import CopySchedule
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.workers.registry import get_driver
from cairn_repair_agent import cli, mirror, model, review
from cairn_repair_agent import config as agent_config
from cairn_repair_agent.digest import LIVE_CUT_SLACK_SECONDS, ReviewContext, build_digest, live_cut
from cairn_repair_agent.mirror import MirrorCopy, Unavailable, freshness_floor, read_copy
from cairn_repair_agent.redact import NO_SECRETS
from cairn_repair_agent.transcripts import parse_transcript

DISPATCHER_TOKEN = fake_secret("dispatcher-token")
PROVIDER_KEY = fake_secret("provider-reviewer-key")
CLAUDE_KEY = fake_secret("claude-reviewer-key")
WORKER_KEY = fake_secret("worker-openai-key")
KEYS = (DISPATCHER_TOKEN, PROVIDER_KEY, CLAUDE_KEY, WORKER_KEY)
COPY_MAX_BYTES = 64 * 1024 * 1024

# The dispatcher's log lines these tests look for (spec §8.11).
SETTINGS_WRITTEN = "repair shadow agent settings written path=%s reviewers=%s"
COPIED = "repair shadow transcript copied project=%s intent=%s run=%s bytes=%s redactions=%s copy=%s"

# A reviewer's optional client settings (Appendix C-1), and the agent's default for each one left out or null.
OPTIONAL_SETTINGS = (
    "structured_output",
    "max_tokens",
    "context_window",
    "timeout_seconds",
    "max_attempts",
    "ca_bundle",
    "proxy",
    "price",
)
C1_DEFAULTS: dict[str, Any] = {
    "max_tokens": 16384,  # owner decision O10, for a reviewer without context_window, as every one left out is here
    "context_window": None,
    "timeout_seconds": 180,
    "max_attempts": 2,
    "ca_bundle": None,
    "proxy": None,
    "price": None,
}


@pytest.fixture(autouse=True)
def fresh_dispatcher_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """The dispatcher's once-per-process state, fresh for each test, as the cairn tests reset it; no real Docker; and
    no record of either part holds a token or a key."""
    monkeypatch.setattr(repair_config, "_LOADER", repair_config._Loader())
    monkeypatch.setattr(repair_client, "_SERVER_NOT_CONFIGURED", repair_client._Once())
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    monkeypatch.setattr(redaction, "_oversized_logged", set())

    def no_docker() -> Any:
        raise AssertionError("the dispatcher's copy reached for Docker")

    monkeypatch.setattr(transcripts, "docker_api", no_docker)
    caplog.set_level(logging.DEBUG, logger="cairn")
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    yield
    for record in caplog.records:
        text = record.getMessage()
        assert not any(key in text for key in KEYS), record.msg


def lines(caplog: pytest.LogCaptureFixture, fmt: str) -> list[tuple[Any, ...]]:
    return [tuple(record.args or ()) for record in caplog.records if record.msg == fmt]


def write_token(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


CODEX_WORKER: dict[str, Any] = {
    "name": "codex-1",
    "type": "codex",
    "task_types": ["explore"],
    "max_running": 1,
    "priority": 0,
    "env": {"CODEX_MODEL": "test-model", "CODEX_BASE_URL": "http://127.0.0.1:9/v1", "OPENAI_API_KEY": WORKER_KEY},
}


def dispatch_yaml(
    path: Path,
    repair: dict[str, Any],
    *,
    server: str = "http://127.0.0.1:8000",
    execution: str = "container",
    workers: list[dict[str, Any]] | None = None,
    local: dict[str, Any] | None = None,
) -> Path:
    """A dispatch.yaml with this repair: section, written as spec §8.3 asks: repair: right after server:, before
    workers:. Container mode with one codex worker by default."""
    payload: dict[str, Any] = {
        "server": server,
        "repair": repair,
        "runtime": {
            "execution": execution,
            "max_workers": 1,
            "max_running_projects": 1,
            "max_project_workers": 1,
            "interval": 5,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "tasks": {
            "bootstrap": {"timeout": 60, "conclude_timeout": 30},
            "reason": {"timeout": 60, "max_intents": 3},
            "explore": {"timeout": 1800, "conclude_timeout": 300},
        },
    }
    if execution == "container":
        payload["container"] = {"image": "test-image", "network_mode": "host", "completed_action": "stop"}
    else:
        payload["local"] = local or {"completed_action": "keep"}
    payload["workers"] = copy.deepcopy(workers or [CODEX_WORKER])
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# --- XC8 --------------------------------------------------------------------------------------------------------------


def section_8_3(token_file: Path, root: Path) -> dict[str, Any]:
    """Spec §8.3's example repair: section, its placeholders filled in. Both keys are written with spaces around them,
    as a quoted YAML value can hold them: the dispatcher removes them (§8.3), and the agent refuses a key that starts or
    ends with a space (§9.2)."""
    return {
        "token_file": str(token_file),
        "transcript_root": str(root),
        "projects": None,
        "reviewers": [
            {
                "name": "reviewer_local",
                "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1"},
            },
            {
                "name": "reviewer_provider",
                "env": {
                    "REVIEW_MODEL": "test-model",
                    "REVIEW_API_KEY": f"  {PROVIDER_KEY}  ",
                    "REVIEW_BASE_URL": "https://provider.example/v1",
                },
                "timeout_seconds": 180,
            },
            {
                "name": "reviewer_claude",
                "api_format": "anthropic_messages",
                "env": {
                    "REVIEW_MODEL": "test-model",
                    "REVIEW_API_KEY": f" {CLAUDE_KEY} ",
                    "REVIEW_BASE_URL": "https://api.anthropic.com",
                },
            },
        ],
    }


def _every_setting(section: dict[str, Any], ca_file: Path) -> None:
    """Every optional setting of Appendix C-1 set on reviewer_provider, other ones on reviewer_claude, explicit nulls
    on reviewer_local, and a projects list: each must reach the agent as the section has it."""
    section["projects"] = ["proj_001", "proj_027"]
    section["reviewers"][0].update(max_tokens=None, structured_output=None, price=None)
    section["reviewers"][1].update(
        structured_output=False,
        max_tokens=2048,
        context_window=32768,
        timeout_seconds=120,
        max_attempts=3,
        ca_bundle=str(ca_file),
        proxy="http://127.0.0.1:3128",
        price={"input_per_mtok": 3, "output_per_mtok": 15},  # YAML integers: the dispatcher reads them as numbers
    )
    section["reviewers"][2].update(structured_output=False, max_tokens=8192, context_window=200000, max_attempts=1)


def _remove_claude_key(section: dict[str, Any], ca_file: Path) -> None:
    del section["reviewers"][2]["env"]["REVIEW_API_KEY"]


def _claude_key_of_spaces(section: dict[str, Any], ca_file: Path) -> None:
    section["reviewers"][2]["env"]["REVIEW_API_KEY"] = "   "  # the dispatcher writes it as null (§8.3)


def _set(index: int, **settings: Any) -> Any:
    def change(section: dict[str, Any], ca_file: Path) -> None:
        section["reviewers"][index].update(settings)

    return change


def _set_env(index: int, **env: str) -> Any:
    def change(section: dict[str, Any], ca_file: Path) -> None:
        section["reviewers"][index]["env"].update(env)

    return change


MODEL_CANARY = "canary model 0042"


@dataclass(frozen=True)
class SettingsCase:
    change: Any = None
    refused: tuple[int, str] | None = None  # (reviewer index, field) the agent's refusal names


XC8_CASES = {
    "spec_8_3_example": SettingsCase(),
    "every_optional_setting": SettingsCase(_every_setting),
    # The examples spec §16 names: C-1's digest budget, and §9.2's two rules of anthropic_messages.
    "context_window_1000_first_reviewer": SettingsCase(_set(0, context_window=1000), (0, "context_window")),
    "context_window_1000_second_reviewer": SettingsCase(_set(1, context_window=1000), (1, "context_window")),
    "anthropic_messages_without_key": SettingsCase(_remove_claude_key, (2, "api_key")),
    "anthropic_messages_key_of_spaces_only": SettingsCase(_claude_key_of_spaces, (2, "api_key")),
    "anthropic_messages_structured_output_true": SettingsCase(
        _set(2, structured_output=True), (2, "structured_output")
    ),
    # Other values that the dispatcher's type check accepts and the agent's start refuses.
    "context_window_8192": SettingsCase(_set(1, context_window=8192), (1, "context_window")),
    "model_with_a_space": SettingsCase(_set_env(1, REVIEW_MODEL=MODEL_CANARY), (1, "model")),
    "model_of_101_characters": SettingsCase(_set_env(0, REVIEW_MODEL="m" * 101), (0, "model")),
    "base_url_port_out_of_range": SettingsCase(
        _set_env(1, REVIEW_BASE_URL="https://provider.example:99999/v1"), (1, "base_url")
    ),
    "base_url_without_host": SettingsCase(_set_env(2, REVIEW_BASE_URL="https:///v1"), (2, "base_url")),
    "ca_bundle_missing": SettingsCase(_set(1, ca_bundle="/nonexistent-xc8/ca.pem"), (1, "ca_bundle")),
    "proxy_not_http": SettingsCase(_set(2, proxy="socks5://127.0.0.1:1080"), (2, "proxy")),
}


def _expected_optional(dispatcher_reviewer: Any, name: str) -> Any:
    """What the agent holds for an optional setting: the section's value when it is set and not null, else C-1's
    default; structured_output's default is true, and false with anthropic_messages (§9.2)."""
    value = getattr(dispatcher_reviewer, name)
    if value is not None:
        return dict(value) if isinstance(value, dict) else value
    if name == "structured_output":
        return dispatcher_reviewer.api_format != "anthropic_messages"
    return C1_DEFAULTS[name]


@pytest.mark.parametrize("case", list(XC8_CASES), ids=list(XC8_CASES))
def test_the_agent_reads_the_settings_file_the_dispatcher_writes(
    case: str, layout: Layout, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """XC8. Spec §8.3's example repair: section, placeholders filled, in a dispatch.yaml loaded with
    DispatchConfig.load; the dispatcher's writer (housekeeping.write_agent_settings, which DS28 tests) writes the
    settings file into the agent's copy root; the agent's reader accepts it, and its server, projects and every
    reviewer's name, model, base_url, api_key, api_format (openai_chat for the entries without it) and optional
    settings equal the section's, for the openai_chat and the anthropic_messages reviewers alike. Each key, written
    with spaces around it, arrives without them, which the agent requires. A setting that the dispatcher's type check
    accepts and the agent refuses stops the agent (exit 2) with a message that names that reviewer and setting, and no
    value."""
    spec = XC8_CASES[case]
    ca_file = layout.base / "reviewer-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n")
    ca_file.chmod(0o644)
    section = section_8_3(
        write_token(layout.base / "secrets" / "repair-dispatcher.token", DISPATCHER_TOKEN), layout.root
    )
    if spec.change is not None:
        spec.change(section, ca_file)
    path = dispatch_yaml(layout.dispatch_path, section)
    text = path.read_text(encoding="utf-8")
    if case == "spec_8_3_example":  # each key a quoted YAML value with spaces around it; repair: before workers:
        assert f"REVIEW_API_KEY: '  {PROVIDER_KEY}  '" in text and f"REVIEW_API_KEY: ' {CLAUDE_KEY} '" in text
        assert text.index("repair:") < text.index("workers:")

    # The dispatcher: its type check accepts the section, and its writer writes the file into the agent's copy root.
    config = DispatchConfig.load(path)
    repair = config.repair
    assert repair is not None
    layout.settings_path.unlink()  # the layout's own file: the agent must read the one the dispatcher writes
    housekeeping.write_agent_settings(config, None)
    assert lines(caplog, SETTINGS_WRITTEN) == [(str(layout.settings_path), len(repair.reviewers))]
    written = layout.settings_path.read_bytes()
    raw = json.loads(written)
    for key in (PROVIDER_KEY, CLAUDE_KEY):  # each key written without the spaces around it, or not at all
        assert f" {key}".encode() not in written and f"{key} ".encode() not in written

    if spec.refused is not None:
        index, name = spec.refused
        reviewer = repair.reviewers[index].name
        code = cli.main(layout.argv("--allow-shared-user", "--once"))
        err = capsys.readouterr().err
        assert code == 2, err
        assert err.startswith(f"agent settings invalid: {layout.settings_path}: reviewers[{index}].{name}: "), err
        assert f"reviewer {reviewer}: " in err, err
        for value in (*KEYS, MODEL_CANARY, "m" * 101, "99999", "socks5", "nonexistent-xc8"):
            assert value not in err
        return

    # The agent: its reader accepts the file, and holds exactly what the section holds.
    settings = agent_config.load_settings(str(layout.root)).settings
    assert settings.version == raw["version"] == 1
    assert settings.server == config.server
    assert settings.projects == (None if repair.projects is None else tuple(repair.projects))
    assert len(settings.reviewers) == len(repair.reviewers)
    for theirs, mine in zip(repair.reviewers, settings.reviewers, strict=True):
        assert (mine.name, mine.model, mine.base_url) == (
            theirs.name,
            theirs.env.REVIEW_MODEL,
            theirs.env.REVIEW_BASE_URL,
        )
        assert mine.api_key == theirs.env.REVIEW_API_KEY
        assert mine.api_format == (theirs.api_format or "openai_chat")
        for name in OPTIONAL_SETTINGS:
            value = getattr(mine, name)
            if name == "price" and value is not None:
                value = value.model_dump()
            assert value == _expected_optional(theirs, name), (theirs.name, name)
    # The keys, written with spaces around them in dispatch.yaml, arrive as they are sent (§8.3).
    assert [mine.api_key for mine in settings.reviewers] == [None, PROVIDER_KEY, CLAUDE_KEY]
    assert [mine.api_format for mine in settings.reviewers] == ["openai_chat", "openai_chat", "anthropic_messages"]

    # Each reviewer's client carries its key, as written, in its format's header only, and requests accepts it.
    for mine in settings.reviewers:
        client = model.build_model_client(mine)
        prepared = client.session.prepare_request(requests.Request("POST", client.url, json={}))
        if mine.api_format == "anthropic_messages":
            assert client.url == "https://api.anthropic.com/v1/messages"
            assert prepared.headers["x-api-key"] == mine.api_key
            assert prepared.headers["anthropic-version"] == model.ANTHROPIC_VERSION
            assert "Authorization" not in prepared.headers
        else:
            assert client.url == mine.base_url + "/chat/completions"
            assert "x-api-key" not in prepared.headers and "anthropic-version" not in prepared.headers
            if mine.api_key is None:
                assert "Authorization" not in prepared.headers
            else:
                assert prepared.headers["Authorization"] == f"Bearer {mine.api_key}"
        client.session.close()

    # A key with the spaces still around it is what the agent would refuse, for either format (§9.2).
    for index in (1, 2):
        padded = copy.deepcopy(raw)
        padded["reviewers"][index]["api_key"] = f" {padded['reviewers'][index]['api_key']} "
        with pytest.raises(agent_config.StartupError) as refused:
            agent_config.parse_settings(padded, str(layout.settings_path))
        message = str(refused.value)
        assert f"reviewers[{index}].api_key: reviewer {raw['reviewers'][index]['name']}: " in message
        assert not any(key in message for key in KEYS)

    # And the agent starts on the dispatcher's file, with its first reviewer.
    run = layout.start("--allow-shared-user")
    assert run.started, run.stderr
    assert run.reviewer == "reviewer_local" and run.model_url == "http://127.0.0.1:11434/v1/chat/completions"
    projects = "all" if repair.projects is None else str(len(repair.projects))
    [started] = run.messages("repair agent started ")
    assert " reviewer=reviewer_local model=test-model endpoint=http://127.0.0.1:11434 " in started
    assert f" projects={projects} " in started


# --- XC7 --------------------------------------------------------------------------------------------------------------

XC7_BUDGET = 1800
XC7_START = datetime(2026, 11, 14, 9, 0, 0, tzinfo=UTC)  # the run's started_at, on the server's clock
XC7_INTENT = "i007"
XC7_SESSION = "3b1e2a4c-5d6f-4a7b-8c9d-0e1f2a3b4c5d"  # Claude Code's --session-id
XC7_MARKER = "/tmp/cairn-prompts/explore_execute-5a6b7c8d9e0f/graph.yaml"  # the run's prompt marker
XC7_COMMAND = "john --wordlist=wordlist.txt rootfs/etc/shadow"
TOOL_CALL_AT = 0.65 * XC7_BUDGET  # 1170 s: the session file's last record, which nothing follows until 85%
OPENS, CLOSES = 1350, 1440  # the window opens at 75% and closes at 80% of the budget (§5.4)
DISPATCHER_START = 5000.0  # the dispatcher's monotonic time at the run's execute_started
AGENT_START = 20000.0  # the agent's monotonic time at the run's start


class Timeline:
    """One fake clock for both parts, in seconds since the run's start, moved only by the test and by the agent's
    sleep. As it moves, each copy tick of the dispatcher's schedule that falls due runs at its own time, in order, as
    the run's reporter thread runs them (spec §8.5 step 3). Nothing really sleeps."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.schedule: CopySchedule | None = None
        self.ticks: list[float] = []
        self.sleeps: list[float] = []

    def begin(self, schedule: CopySchedule) -> None:
        self.elapsed, self.schedule, self.ticks, self.sleeps = 0.0, schedule, [], []

    def dispatcher_monotonic(self) -> float:
        return DISPATCHER_START + self.elapsed

    def dispatcher_wall(self) -> float:
        return XC7_START.timestamp() + self.elapsed

    def agent_monotonic(self) -> float:
        return AGENT_START + self.elapsed

    def advance_to(self, elapsed: float) -> None:
        assert self.schedule is not None
        while (due := self.schedule.next_due()) is not None and due - DISPATCHER_START <= elapsed + 1e-6:
            self.elapsed = max(self.elapsed, due - DISPATCHER_START)
            self.ticks.append(round(self.elapsed, 1))
            self.schedule.tick()
        self.elapsed = max(self.elapsed, elapsed)

    def agent_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance_to(self.elapsed + seconds)


def _iso(seconds: float) -> str:
    return (XC7_START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def xc7_session() -> bytes:
    """A Claude Code session file in 2.1.98's record shapes (claudecode-2.1.98.jsonl): the prompt with the run's marker
    at its start, an extraction that finished, a message, and a password cracker started at 65% with no result."""
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "timestamp": _iso(0),
            "sessionId": XC7_SESSION,
            "message": {"role": "user", "content": f"# Task\nThe graph snapshot is in this file:\n\n{XC7_MARKER}\n"},
        },
        {
            "type": "assistant",
            "timestamp": _iso(30),
            "sessionId": XC7_SESSION,
            "message": {
                "role": "assistant",
                "model": "test-model",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_xc7_0",
                        "name": "Bash",
                        "input": {"command": "unsquashfs -d rootfs firmware.squashfs"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": _iso(95),
            "sessionId": XC7_SESSION,
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_xc7_0",
                        "type": "tool_result",
                        "content": "created 412 files",
                        "is_error": False,
                    }
                ],
            },
            "toolUseResult": {"stdout": "created 412 files", "stderr": "", "interrupted": False, "isImage": False},
        },
        {
            "type": "assistant",
            "timestamp": _iso(100),
            "sessionId": XC7_SESSION,
            "message": {
                "role": "assistant",
                "model": "test-model",
                "content": [{"type": "text", "text": "The root file system is out; next the shadow hashes, offline."}],
            },
        },
        {
            "type": "assistant",
            "timestamp": _iso(TOOL_CALL_AT),
            "sessionId": XC7_SESSION,
            "message": {
                "role": "assistant",
                "model": "test-model",
                "content": [
                    {"type": "tool_use", "id": "toolu_xc7_1", "name": "Bash", "input": {"command": XC7_COMMAND}}
                ],
            },
        },
    ]
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


@dataclass
class Xc7Run:
    project_id: str
    run_id: str
    folder: Path  # <root>/<project_id>/<run_id>/, the copy's folder
    view: dict[str, Any]
    poll: review.Poll
    window: review.Window
    deadlines: review.Deadlines


def start_xc7_run(base: Path, config: DispatchConfig, timeline: Timeline, project_id: str, run_id: str) -> Xc7Run:
    """A local-mode Claude Code run of 1800 s, registered, with its session file in place and its copy schedule on the
    timeline, as the run's reporter thread holds them (spec §8.5, §8.8); and the run's RunView as GET /repair/runs
    lists it when its window opens (§6.3)."""
    repair = config.repair
    assert repair is not None
    worker = config.workers[0]
    workdir = base / "work" / project_id
    workdir.mkdir(parents=True)
    projects = Path(worker.env["CLAUDE_CONFIG_DIR"]) / "projects"
    session = projects / transcripts.claude_project_slug(os.path.realpath(workdir)) / f"{XC7_SESSION}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_bytes(xc7_session())  # written by 65%, and not changed again until 85%
    run = ShadowRun(
        run_id=run_id,
        config=config,
        project_id=project_id,
        intent_id=XC7_INTENT,
        worker=worker,
        execution="local",
        driver=get_driver(worker.type, "local"),
        backend=None,
        cancellation=TaskCancellation(),
    )
    run.container = str(workdir)  # local mode: the project's work folder, the CLI's cwd
    run.budget_seconds = XC7_BUDGET
    run.prompt_marker = XC7_MARKER
    run.session_id = XC7_SESSION
    run.started_monotonic = DISPATCHER_START
    run.started_wall = XC7_START.timestamp()
    timeline.begin(CopySchedule(run, DISPATCHER_TOKEN))
    view = run_view(run_id, project_id, elapsed=OPENS, intent_id=XC7_INTENT, start=XC7_START)
    window = review.window_of(view)
    assert window is not None and window.opens_elapsed == OPENS
    poll = review.Poll(XC7_START + timedelta(seconds=OPENS), AGENT_START + OPENS, 600)
    return Xc7Run(
        project_id,
        run_id,
        Path(repair.transcript_root) / project_id / run_id,
        view,
        poll,
        window,
        review.Deadlines.of(window, poll),
    )


def running_section(digest_text: str) -> list[str]:
    """The RUNNING AT THE CUT lines of a digest, its heading first."""
    body = digest_text.split("\n")
    start = next(index for index, line in enumerate(body) if line.startswith("RUNNING AT THE CUT"))
    found = [body[start]]
    for line in body[start + 1 :]:
        if not line.startswith("E"):
            break
        found.append(line)
    return found


def last_tick(elapsed: float) -> float:
    """The time of the dispatcher's last tick at or before elapsed: 70% of the budget, then every 15 s (§8.8)."""
    return 1260.0 + 15 * ((elapsed - 1260) // 15)


def test_a_session_file_unchanged_from_65_to_85_percent_is_reviewed_as_running(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """XC7. An 1800 s claudecode run whose session file ends with a tool_use record written at 65% and does not change
    until 85%. The dispatcher's copy schedule (§8.8) copies once at 70% and then only rewrites meta.json. At the
    window's opening and at every re-read until it closes (every 5 s, §9.5 step 4), the agent's read_copy accepts the
    copy in <root>/<project_id>/<run_id>/, and the digest lists that call under RUNNING AT THE CUT, running from 65% to
    the live cut, checked_elapsed_seconds plus LIVE_CUT_SLACK_SECONDS (§9.6); the review at the opening cites it in a
    detach. With the checked-time rewrite removed, the
    same review gets stale: every re-read until the window closes finds the copy last checked at 70%, and the review is
    a transcript_unreadable continue with no model call. Fake monotonic clocks for both parts, no sleeping.
    """
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(layout.base / "home"))  # no real CLI's credential file is ever read
    timeline = Timeline()
    monkeypatch.setattr(transcripts, "_monotonic", timeline.dispatcher_monotonic)
    monkeypatch.setattr(transcripts, "_wall", timeline.dispatcher_wall)
    detach = decision_text(
        "detach",
        "The offline cracker is still working through its wordlist.",
        ("E2 john is running at the cut, offline",),
        "Let john finish the wordlist and report the cracked accounts.",
    )
    model_stub = ModelStub(Reply(body=chat_answer(content=detach)))
    cairn_stub = CairnStub()
    try:
        workers = [
            {
                "name": "claudecode-1",
                "type": "claudecode",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"CLAUDE_CONFIG_DIR": str(layout.base / "claude-config"), "HOME": str(layout.base / "home")},
            }
        ]
        section = {
            "token_file": str(write_token(layout.base / "secrets" / "repair-dispatcher.token", DISPATCHER_TOKEN)),
            "transcript_root": str(layout.root),
            "reviewers": [
                {
                    "name": "reviewer_stub",
                    "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": model_stub.url + "/v1"},
                }
            ],
        }
        path = dispatch_yaml(layout.dispatch_path, section, server=cairn_stub.url, execution="local", workers=workers)
        config = DispatchConfig.load(path)
        layout.settings_path.unlink()
        housekeeping.write_agent_settings(config, None)  # the agent's reviewer and server come from the dispatcher
        agent = start_agent(layout, monotonic=timeline.agent_monotonic, sleep=timeline.agent_sleep)
        environment = agent.loop.environment
        floor = freshness_floor(OPENS, agent.started.config.transcripts.max_staleness_seconds)
        assert floor == OPENS - 45

        # With the checked-time rewrite: the copy of 70% stays fresh through the window, and the call runs at the cut.
        run = start_xc7_run(layout.base, config, timeline, "proj_031", "7c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f")
        timeline.advance_to(OPENS)
        assert timeline.ticks == [1260.0 + 15 * index for index in range(7)]
        meta = json.loads((run.folder / "meta.json").read_bytes())
        assert (meta["status"], meta["copies"], meta["located_by"]) == ("ok", 1, "path")
        assert (meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (1260.0, 1350.0)
        reviewed = review.review_run(run.view, run.window, run.poll, run.deadlines, environment)
        assert (reviewed.decision, reviewed.fallback) == ("detach", None), reviewed.reason
        assert reviewed.evidence == ("E2 john is running at the cut, offline",)
        assert reviewed.transcript_cut_seconds == 1350.0
        assert reviewed.digest is not None
        assert running_section(reviewed.digest) == [
            "RUNNING AT THE CUT:",
            f"E2 t+19:30 Bash (running {int(OPENS + LIVE_CUT_SLACK_SECONDS - TOOL_CALL_AT)} s): {XC7_COMMAND}",
        ]
        [request] = model_stub.requests
        assert json.loads(request.body)["messages"][1]["content"] == reviewed.digest
        assert timeline.sleeps == []
        for elapsed in range(OPENS, CLOSES + 1, 5):  # every re-read until the window closes accepts the copy
            timeline.advance_to(elapsed)
            copy_now = read_copy(
                str(layout.root), run.project_id, run.run_id, COPY_MAX_BYTES, min_checked_elapsed=floor
            )
            assert isinstance(copy_now, MirrorCopy), (elapsed, copy_now)
            checked = last_tick(elapsed)
            assert (copy_now.copied_elapsed_seconds, copy_now.checked_elapsed_seconds) == (1260.0, checked)
            digest = build_digest(
                parse_transcript("claudecode", copy_now.data),
                ReviewContext.from_run(
                    {**run.view, "elapsed_seconds": elapsed, "remaining_seconds": XC7_BUDGET - elapsed},
                    slice_seconds=600,
                    copy_age_seconds=int(elapsed - checked),
                ),
                live_cut(XC7_START, copy_now.checked_elapsed_seconds),
                intent="Recover the device's local accounts.",
                source_facts=[],
                new_facts=[],
                exact=NO_SECRETS,
                budget=8000,
            )
            running = int(checked + LIVE_CUT_SLACK_SECONDS - TOOL_CALL_AT)  # from 65% to the live cut (§9.6)
            assert running_section(digest.text) == [
                "RUNNING AT THE CUT:",
                f"E2 t+19:30 Bash (running {running} s): {XC7_COMMAND}",
            ]
            assert digest.running_ids == frozenset({"E2"})
        meta = json.loads((run.folder / "meta.json").read_bytes())
        assert (meta["copies"], meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (1, 1260.0, 1440.0)
        assert [args[2] for args in lines(caplog, COPIED)] == [run.run_id]  # one copy; the ticks after it rewrote meta

        # Without it (the rewrite removed): the same review finds the copy last checked at 70%, at every re-read.
        reads: list[tuple[float, MirrorCopy | Unavailable]] = []
        real_read_copy = read_copy

        def recorded(*args: Any, **kwargs: Any) -> MirrorCopy | Unavailable:
            result = real_read_copy(*args, **kwargs)
            reads.append((timeline.elapsed, result))
            return result

        with monkeypatch.context() as patch:
            patch.setattr(CopySchedule, "_checked", lambda self, now, final: None)
            patch.setattr(mirror, "read_copy", recorded)
            stale = start_xc7_run(layout.base, config, timeline, "proj_032", "8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a")
            timeline.advance_to(OPENS)
            meta = json.loads((stale.folder / "meta.json").read_bytes())
            assert (meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (1260.0, 1260.0)
            reviewed = review.review_run(stale.view, stale.window, stale.poll, stale.deadlines, environment)
        assert (reviewed.decision, reviewed.fallback) == ("continue", "transcript_unreadable")
        assert reviewed.reason == "transcript copy stale: checked 90 s before the review opened"
        assert [elapsed for elapsed, _ in reads] == [float(elapsed) for elapsed in range(OPENS, CLOSES + 1, 5)]
        assert all(result == Unavailable("stale", checked_elapsed_seconds=1260.0) for _, result in reads)
        assert timeline.sleeps == [5.0] * ((CLOSES - OPENS) // 5)
        assert len(model_stub.requests) == 1  # no model call for the stale copy
    finally:
        model_stub.close()
        cairn_stub.close()


# --- XC2 --------------------------------------------------------------------------------------------------------------

# TC12's run (cairn/tests/test_repair_transcripts.py), whose copy is the golden fixture.
GOLDEN_RUN_ID = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"
GOLDEN_MARKER = "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml"  # the pinned fixtures' prompt marker
GOLDEN_SESSION = "01a1dafd-8482-73f3-bd0c-ea91c05a4ef5"  # codex-0.118.0.jsonl's session id
GOLDEN_SOURCE = f"/home/kali/.codex/sessions/2026/11/14/rollout-2026-11-14T16-56-50-{GOLDEN_SESSION}.jsonl"
GOLDEN_MONOTONIC_START = 1000.0
GOLDEN_WALL_START = 1794650399.8  # 2026-11-14T09:59:59.8Z, at the run's execute_started


@dataclass
class ArchiveResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    raw: io.BytesIO = field(default_factory=io.BytesIO)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class OneFileContainer:
    """The Docker calls of the dispatcher's container-mode copy (spec §8.8), over one file of the worker's container:
    the find exec that locates it, read through the exec's socket, and the archive endpoint's stat (HEAD) and tar
    stream (GET), with docker-py's attribute names."""

    base_url = "http+docker://localhost"
    api_version = "1.47"

    def __init__(self, path: str) -> None:
        self.path = path
        self.data = b""
        self.mtime = ""
        self.commands: dict[str, list[str]] = {}
        self.responses: list[ArchiveResponse] = []

    def head(self, url: str, params: dict[str, str], **_: Any) -> ArchiveResponse:
        if params["path"] != self.path:
            return self._respond(ArchiveResponse(404))
        stat = {
            "name": posixpath.basename(self.path),
            "size": len(self.data),
            "mode": 0o644,
            "mtime": self.mtime,
            "linkTarget": "",
        }
        header = base64.b64encode(json.dumps(stat).encode()).decode()
        return self._respond(ArchiveResponse(200, {"X-Docker-Container-Path-Stat": header}))

    def get(self, url: str, params: dict[str, str], **_: Any) -> ArchiveResponse:
        if params["path"] != self.path:
            return self._respond(ArchiveResponse(404))
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo(posixpath.basename(self.path))
            info.size = len(self.data)
            archive.addfile(info, io.BytesIO(self.data))
        return self._respond(ArchiveResponse(200, raw=io.BytesIO(body.getvalue())))

    def exec_create(self, container: str, cmd: list[str], **_: Any) -> dict[str, str]:
        exec_id = f"exec-{len(self.commands) + 1}"
        self.commands[exec_id] = list(cmd)
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, **_: Any) -> socket.socket:
        """The find's output in one stdout frame of Docker's multiplexed stream, then the end of the stream."""
        argv = self.commands[exec_id]
        argv = argv[argv.index("find") + 1 :]
        folder, options = argv[0].rstrip("/"), dict(zip(argv[1::2], argv[2::2], strict=False))
        name = posixpath.basename(self.path)
        found = self.path.startswith(folder + "/") and fnmatch.fnmatchcase(name, options.get("-name", "*"))
        payload = (self.path + "\n").encode() if found else b""
        ours, peer = socket.socketpair()
        peer.sendall(bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload)
        peer.close()
        return ours

    def _respond(self, response: ArchiveResponse) -> ArchiveResponse:
        self.responses.append(response)
        return response


def test_the_golden_copy_is_what_the_dispatcher_writes_and_what_the_agent_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XC2 = TC12 + AG22: one golden copy fixture, cairn/tests/fixtures/repair_copy/proj_027/<run_id>/, written by the
    dispatcher's copy and read by the agent's mirror. TC12's run is copied again here with the dispatcher's own code: a
    codex run in container mode whose session file grew at the ticks from 1260 s to 1335 s and was found unchanged at
    the 1350 s tick; the meta.json and transcript.jsonl it writes are byte for byte the committed ones. The agent's
    read_copy(root, "proj_027", run_id, ...) then reads that fresh copy and the committed fixture alike: the same
    transcript, sha256, copied and checked times as meta.json, and a transcript the parser reads whole."""
    golden = GOLDEN_COPY_PROJECT / GOLDEN_RUN_ID
    assert sorted(entry.name for entry in GOLDEN_COPY_PROJECT.iterdir()) == [GOLDEN_RUN_ID]
    assert GOLDEN_MARKER.encode() in fixture_bytes(CODEX) and (FIXTURES / CODEX).is_file()

    # TC12's dispatch.yaml: one codex worker with its key, spec §8.3's two reviewers, the dispatcher token.
    section = {
        "token_file": str(write_token(tmp_path / "secrets" / "repair-dispatcher.token", DISPATCHER_TOKEN)),
        "transcript_root": str(tmp_path / "copies"),
        "reviewers": [
            {
                "name": "reviewer_local",
                "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1"},
            },
            {
                "name": "reviewer_provider",
                "env": {
                    "REVIEW_MODEL": "test-model",
                    "REVIEW_API_KEY": PROVIDER_KEY,
                    "REVIEW_BASE_URL": "https://provider.example/v1",
                },
                "timeout_seconds": 180,
            },
        ],
    }
    config = DispatchConfig.load(dispatch_yaml(tmp_path / "dispatch.yaml", section, server="http://127.0.0.1:9"))
    container = OneFileContainer(GOLDEN_SOURCE)
    monkeypatch.setattr(transcripts, "docker_api", lambda: container)
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(transcripts, "_monotonic", lambda: GOLDEN_MONOTONIC_START + clock["elapsed"])
    monkeypatch.setattr(transcripts, "_wall", lambda: GOLDEN_WALL_START + clock["elapsed"])
    run = ShadowRun(
        run_id=GOLDEN_RUN_ID,
        config=config,
        project_id="proj_027",
        intent_id="i014",
        worker=config.workers[0],
        execution="container",
        driver=get_driver("codex", "container"),
        backend=None,
        cancellation=TaskCancellation(),
    )
    run.container = "cairn-dispatch-proj_027"
    run.budget_seconds = 1800
    run.prompt_marker = GOLDEN_MARKER
    run.session_id = GOLDEN_SESSION
    run.started_monotonic = GOLDEN_MONOTONIC_START
    run.started_wall = GOLDEN_WALL_START
    schedule = CopySchedule(run, DISPATCHER_TOKEN)
    pieces = fixture_bytes(CODEX).split(b"\n")
    for index, count in enumerate((7, 9, 11, 14, 17, 21)):  # the prompt is line 6
        container.data = b"\n".join(pieces[:count])
        container.mtime = f"2026-11-14T10:{20 + index:02d}:00.000000000Z"
        clock["elapsed"] = 1260 + 15 * index + 1.2
        schedule.tick()
    clock["elapsed"] = 1351.2
    schedule.tick()
    assert container.responses and all(response.closed for response in container.responses)

    written = tmp_path / "copies" / "proj_027" / GOLDEN_RUN_ID
    for name in ("meta.json", "transcript.jsonl"):
        assert (written / name).read_bytes() == (golden / name).read_bytes(), name

    meta = json.loads((golden / "meta.json").read_bytes())
    assert (meta["copies"], meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (6, 1336.2, 1351.2)
    copies: list[MirrorCopy] = []
    for root in (tmp_path / "copies", GOLDEN_COPY_PROJECT.parent):  # the copy just written, and the committed one
        result = read_copy(str(root), "proj_027", GOLDEN_RUN_ID, COPY_MAX_BYTES)
        assert isinstance(result, MirrorCopy), result
        assert result.sha256 == meta["sha256"] == hashlib.sha256(result.data).hexdigest()
        assert (result.copied_elapsed_seconds, result.checked_elapsed_seconds) == (1336.2, 1351.2)
        copies.append(result)
    assert copies[0].data == copies[1].data == (golden / "transcript.jsonl").read_bytes()
    transcript = parse_transcript(meta["cli_type"], copies[0].data)
    assert transcript.entries > 0 and transcript.unreadable == 0 and transcript.calls
