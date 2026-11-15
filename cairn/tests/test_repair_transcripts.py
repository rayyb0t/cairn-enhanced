"""Transcript-copy tests of spec 16 (build step 7): TC1-TC12, TC14 and TC17.

The copy (repair/transcripts.py, spec 8.8) is driven through a real ShadowRun and CopySchedule with a fake clock;
in container mode the repair module's Docker client is FakeDockerApi (test_repair_transcripts_support.py), and in
local mode the session files sit under temporary homes. TC2 and TC1 drive whole runs and a whole scheduler loop. The
removal of copies (TC18, TC19) is in test_repair_transcripts_removal.py, the writes' permissions and symlink refusals
(TC16, TC20) in test_repair_transcripts_writes.py, and the real-Docker bound (TC15) in
test_repair_transcripts_docker.py. Every value here is synthetic; TC12 writes or checks the golden copy fixture.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeLease, make_intent, make_project
from repair_dispatcher_runs import (
    CODEX_ID,
    PI_ID,
    RecordingClient,
    RunCapture,
    ScriptedBackend,
    normalize,
    repair_server,
    run_config,
    run_project,
    run_records,
    scripts_for,
)
from repair_dispatcher_support import (
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    RecordedRequest,
    Reply,
    bypass_proxies,
    fake_server,
    json_reply,
    repair_section,
    reset_repair_process_state,
    write_token,
)
from test_repair_transcripts_support import (
    CONTAINER,
    COPIED,
    COPY_FAILED,
    COPY_FIXTURE,
    MARKER,
    OTHER_MARKER,
    RUN_ID,
    SESSION_IDS,
    START,
    TOO_LARGE_LINE,
    WALL_START,
    FakeClock,
    FakeDockerApi,
    FakeFile,
    all_log_text,
    container_session_path,
    copy_config,
    copy_folder,
    copy_lines,
    fixture_bytes,
    install_docker,
    make_run,
    read_meta,
    read_transcript,
)

from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.repair import housekeeping, redaction, session_ids, shadow, transcripts
from cairn.dispatcher.repair.explore import run_explore_task_with_repair
from cairn.dispatcher.repair.transcripts import CopySchedule, holds_marker
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.server.models import ProjectDetail

META_KEYS = [
    "version",
    "run_id",
    "project_id",
    "intent_id",
    "worker",
    "cli_type",
    "status",
    "copied_at",
    "copied_elapsed_seconds",
    "checked_at",
    "checked_elapsed_seconds",
    "source_path",
    "source_bytes",
    "copied_bytes",
    "sha256",
    "redactions",
    "copies",
    "final",
    "located_by",
]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fresh once-per-process state; no real Docker; no record holds the token or a reviewer key."""
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(redaction, "_oversized_logged", set())
    monkeypatch.setattr(housekeeping, "_failed_steps", set())

    def no_docker() -> Any:
        raise AssertionError("the repair module reached for Docker")

    monkeypatch.setattr(transcripts, "docker_api", no_docker)
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    text = all_log_text(caplog)
    assert FAKE_TOKEN not in text
    assert FAKE_REVIEWER_KEY not in text


def tick_at(schedule: CopySchedule, clock: FakeClock, elapsed: float) -> None:
    clock.at(elapsed)
    schedule.tick()


def codex_api(data: bytes | None = None, **file: Any) -> tuple[FakeDockerApi, str]:
    """A fake container holding codex's session file of the pinned fixture."""
    path = container_session_path("codex", SESSION_IDS["codex"])
    api = FakeDockerApi({path: FakeFile(fixture_bytes("codex") if data is None else data, **file)})
    return api, path


# TC1 -----------------------------------------------------------------------------------------------------------------


class RecordingExecutor:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from concurrent.futures import Future

        self.submitted.append(fn)
        return Future()

    def shutdown(self, wait: bool = True) -> None:
        return None


def _summary(detail: ProjectDetail, status: str = "active") -> dict[str, Any]:
    unclaimed = sum(1 for intent in detail.intents if intent.to is None and intent.worker is None)
    return {
        **detail.project.model_dump(mode="json"),
        "status": status,
        "repair_enabled": False,
        "fact_count": len(detail.facts),
        "intent_count": len(detail.intents),
        "working_intent_count": len(detail.intents) - unclaimed,
        "unclaimed_intent_count": unclaimed,
        "hint_count": len(detail.hints),
    }


def _switched_off_server(project: ProjectDetail) -> Any:
    body = project.model_dump(mode="json", by_alias=True)
    body["project"]["repair_enabled"] = False

    def respond(request: RecordedRequest) -> Reply:
        path = request.path.split("?", 1)[0]
        if path == "/settings":
            return json_reply(200, {"intent_timeout": 300, "reason_timeout": 300})
        if path == "/projects":
            return json_reply(200, [_summary(project)])
        if path == "/projects/proj_001":
            return json_reply(200, body)
        if path == "/projects/proj_001/export":
            return Reply(200, b"facts: []\n", "text/plain")
        if path.startswith("/projects/proj_001/intents/") and path.endswith("/heartbeat"):
            return json_reply(200, body["intents"][0])
        return json_reply(404, {"detail": "Not Found"})

    return respond


def _loop_yaml(folder: Path, server: str, *, repair: bool) -> Path:
    payload: dict[str, Any] = {
        "server": server,
        "runtime": {
            "execution": "local",
            "interval": 5,
            "max_workers": 2,
            "max_running_projects": 1,
            "max_project_workers": 2,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "local": {"workspace_root": str(folder / "work")},
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 3},
            "explore": {"timeout": 1800, "conclude_timeout": 300},
        },
        "workers": [{"name": "mock-1", "type": "mock", "task_types": ["explore"], "max_running": 1, "priority": 0}],
    }
    if repair:
        token = write_token(folder / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
        payload["repair"] = repair_section(token_path=str(token), transcript_root=str(folder / "copies"))
    path = folder / "dispatch.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_switch_off_makes_no_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC1. Without a repair: section, a whole scheduler pass and a switched-on run through
    run_explore_task_with_repair create no file or folder and make no Docker call. With the section and every switch
    off, the copy root holds only agent-settings.json after the housekeeping's first pass, no Docker call is made,
    and the repair side sends no request: the server sees the same requests as without the section.
    """
    docker_calls: list[str] = []
    monkeypatch.setattr(transcripts, "docker_api", lambda: docker_calls.append("docker_api"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    stop = threading.Event()
    monkeypatch.setattr(housekeeping, "_STOP", stop)
    first_pass = threading.Event()
    real_removal = housekeeping.remove_copies

    def removal(config: DispatchConfig, client: Any) -> None:
        real_removal(config, client)
        first_pass.set()

    monkeypatch.setattr(housekeeping, "remove_copies", removal)
    results: dict[bool, tuple[list[Any], list[tuple[str, str]]]] = {}
    try:
        for repair in (False, True):
            folder = tmp_path / ("section" if repair else "none")
            folder.mkdir()
            project = make_project(intents=[make_intent()])
            project.intents[0].worker = None
            with fake_server(_switched_off_server(project)) as server:
                config_path = _loop_yaml(folder, server.base_url, repair=repair)
                before = _tree(tmp_path)
                loop = DispatcherLoop(config_path)
                executor = RecordingExecutor()
                loop.executor = executor  # type: ignore[assignment]
                loop.run(once=True)
                if repair:
                    assert first_pass.wait(10)
                else:
                    assert not first_pass.is_set()
                    assert _tree(tmp_path) == before  # no file or folder created
                requests_seen = [(request.method, request.path) for request in server.requests]
            results[repair] = (executor.submitted, requests_seen)
        assert results[False][0] == results[True][0] == [run_explore_task]  # every switch off: today's task
        assert results[True][1] == results[False][1]  # the housekeeping sent no request: no project folder
        assert sorted(os.listdir(tmp_path / "section" / "copies")) == ["agent-settings.json"]
    finally:
        stop.set()

    # A switched-on project dispatched while dispatch.yaml has no repair: section: today's task with the real backend,
    # no thread, no copy. (A fresh process: the loop above read the token.)
    reset_repair_process_state(monkeypatch)
    capture = RunCapture(monkeypatch)
    config = run_config("codex", "http://127.0.0.1:9", tmp_path / "missing.token", tmp_path / "copies", repair=False)
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())
    before = _tree(tmp_path)
    intent = make_intent()
    project = run_project(intent)
    outcome = run_explore_task_with_repair(
        config,
        RecordingClient(project),
        ScriptedBackend(scripts_for("codex", "success")),
        project,
        "g",
        intent,
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "success" and capture.runs == [None]
    assert _tree(tmp_path) == before
    assert not (home / ".local" / "share" / "cairn").exists()  # the default copy root was never created
    assert docker_calls == []


# TC2 -----------------------------------------------------------------------------------------------------------------

OUTPUT_IDS = {"codex": CODEX_ID, "pi": PI_ID}


def _observed_run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    config: DispatchConfig,
    scripts: list[dict[str, Any]],
    *,
    repair: bool,
) -> dict[str, Any]:
    """One explore run, today's task or its Repair version, and everything the run itself did."""
    intent = make_intent()
    project = run_project(intent, repair=repair)
    client = RecordingClient(project)
    backend = ScriptedBackend(scripts)
    lease = FakeLease()
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: lease)
    task = run_explore_task_with_repair if repair else run_explore_task
    caplog.clear()
    outcome = task(config, client, backend, project, "facts: []\n", intent, config.workers[0], TaskCancellation())
    names: dict[str, str] = {}
    return {
        "outcome": outcome,
        "backend": normalize(backend.log, names),
        "client": normalize(client.calls, names),
        "logs": normalize(run_records(caplog), names),
        "lease": (lease.started, lease.stopped),
    }


@pytest.mark.parametrize("cli", ["claudecode", "codex", "pi"])
@pytest.mark.parametrize("ending", ["success", "timeout_then_conclude"])
def test_run_is_identical_with_copying_on_or_failing(
    cli: str, ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC2. A switched-on run whose copies are made (every 50 ms from its start, then the final copy), or fail (Docker
    giving no answer, or a copy root that is a file), sends the same commands, requests and log lines and returns the
    same outcome as today's task, while the copies are made or fail on the reporter thread.
    """
    monkeypatch.setattr(session_ids, "PROBE_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(shadow, "RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(shadow, "TASK_DONE_GRACE_SECONDS", 5)
    monkeypatch.setattr(transcripts, "COPY_START_FRACTION", 0.0)  # due at once, and at every 50 ms while it runs
    monkeypatch.setattr(transcripts, "COPY_INTERVAL_SECONDS", 0.05)
    scripts = scripts_for(cli, ending)
    scripts[0] = {**scripts[0], "chunks": [*scripts[0]["chunks"], (0.4, "err", "")]}  # it runs about 0.4 s more
    token_file = write_token(tmp_path / "secrets" / "token", FAKE_TOKEN)
    capture = RunCapture(monkeypatch)
    api = FakeDockerApi()

    def serve_the_runs_file() -> None:
        observed = capture.observed
        run = observed[-1] if observed else None
        if run is None or run.prompt_marker is None:
            return
        session_id = run.argv_session if cli == "claudecode" else OUTPUT_IDS[cli]
        assert session_id is not None
        path = container_session_path(cli, session_id)
        api.files[path] = FakeFile(fixture_bytes(cli, run.prompt_marker), epoch=time.time())

    api.before = serve_the_runs_file
    observed: dict[str, dict[str, Any]] = {}
    with repair_server() as server:

        def config_for(root: Path) -> DispatchConfig:
            return run_config(cli, server.base_url, token_file, root, timeout=1800)

        try:
            observed["today"] = _observed_run(
                monkeypatch, caplog, config_for(tmp_path / "today"), scripts, repair=False
            )
            for variant in ("on", "no_answer", "unsafe_root"):
                root = tmp_path / variant
                if variant == "no_answer":
                    failing = FakeDockerApi()
                    failing.raise_on = {"head", "get", "exec_create", "inspect"}
                    install_docker(monkeypatch, failing)
                else:
                    install_docker(monkeypatch, api)
                if variant == "unsafe_root":
                    root.write_text("not a folder\n")
                config = config_for(root)
                observed[variant] = _observed_run(monkeypatch, caplog, config, scripts, repair=True)
                capture.wait()
                run = capture.observed[-1]
                assert server.kinds(run.run_id)[0] == "register"
                if variant == "on":
                    meta = read_meta(config, run.project_id, run.run_id)
                    assert meta["status"] == "ok" and meta["copies"] >= 1 and meta["final"] is True
                    assert read_transcript(config, run.project_id, run.run_id) == fixture_bytes(cli, run.prompt_marker)
                elif variant == "no_answer":
                    assert not root.exists()  # Docker never answered: nothing was written
                else:
                    assert root.read_text() == "not a folder\n"
                    assert [args[3] for args in copy_lines(caplog, COPY_FAILED)] == ["unsafe_path"]
        finally:
            capture.finish()
            api.close()
    assert observed["on"] == observed["today"]
    assert observed["no_answer"] == observed["today"]
    assert observed["unsafe_root"] == observed["today"]


# TC3 and TC4 ---------------------------------------------------------------------------------------------------------

RUN_WORKER_KEY = 'canary-run-worker-"quoted"\\path-\u00e9-7f3a91'


def _forms(value: str) -> list[bytes]:
    return [
        value.encode(),
        json.dumps(value)[1:-1].encode(),
        json.dumps(value, ensure_ascii=False)[1:-1].encode(),
    ]


def _session_with(cli: str, *texts: str) -> bytes:
    """The pinned session file with lines appended that hold texts, as a tool's output would."""
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "tool_output", "text": text}}) for text in texts]
    return fixture_bytes(cli) + "".join(line + "\n" for line in lines).encode()


def test_exact_value_redaction_of_the_runs_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC3. The run's worker's key, printed raw and JSON-escaped (with and without ensure_ascii), is replaced by
    [REDACTED:OPENAI_API_KEY] in every form and counted under that name in meta.json; no form is left in the copy
    and no log record holds it.
    """
    forms = _forms(RUN_WORKER_KEY)
    assert len(set(forms)) == 3  # the value has characters JSON escapes, so its three forms differ
    workers = [
        {
            "name": "codex-1",
            "type": "codex",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "test-model",
                "CODEX_BASE_URL": "http://127.0.0.1:9",
                "OPENAI_API_KEY": RUN_WORKER_KEY,
            },
        }
    ]
    config = copy_config(tmp_path, "codex", workers=workers)
    printed = fixture_bytes("codex") + b"".join(b"printed " + form + b" (env)\n" for form in forms)
    api, _path = codex_api(printed)
    install_docker(monkeypatch, api)
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    copied = read_transcript(config)
    assert all(form not in copied for form in forms)
    assert copied.count(b"[REDACTED:OPENAI_API_KEY]") == 3
    meta = read_meta(config)
    assert meta["redactions"]["OPENAI_API_KEY"] == 3
    assert copied == printed.replace(forms[0], b"[REDACTED:OPENAI_API_KEY]").replace(
        forms[1], b"[REDACTED:OPENAI_API_KEY]"
    ).replace(forms[2], b"[REDACTED:OPENAI_API_KEY]")
    assert meta["copied_bytes"] == len(copied) and meta["sha256"] == hashlib.sha256(copied).hexdigest()
    assert meta["source_bytes"] == len(printed)
    assert RUN_WORKER_KEY not in all_log_text(caplog)
    # redact_exact itself: a value that holds a shorter one is replaced whole, longest first.
    data, counts = redaction.redact_exact(b"a=secret-value-123 b=secret-v", {"secret-value-123": "A", "secret-v": "B"})
    assert data == b"a=[REDACTED:A] b=[REDACTED:B]" and counts == {"A": 1, "B": 1}


PI_KEY = "canary-other-worker-pi-key-9d1e07"
PROXY_PASSWORD = "canary-proxy-password-0c2d88"  # noqa: S105 - fake
REVIEWER_KEY_2 = 'canary-reviewer-key-two-"7a11"\\x'  # quotes and a backslash: its JSON-escaped form differs
SHORT_REVIEWER_KEY = "abc123"  # six characters: below the eight of the rule, so left alone

CANARIES = {
    "claude:.credentials.json": "canary-claude-credentials-access-0001",
    "claude:.claude.json": "canary-claude-json-primary-api-key-0002",
    "claude:settings.json": "canary-claude-settings-auth-token-0003",
    "codex:auth.json": "canary-codex-auth-access-token-0004",
    "codex:config.toml": "canary-codex-config-bearer-token-0005",
    "pi:auth.json": "canary-pi-auth-oauth-access-0006",
    "pi:auth.json/key": "canary-pi-auth-api-key-0007",
    "pi:models.json": "canary-pi-models-api-key-0008",
}
LOCAL_ENV_TOKEN = "canary-dispatcher-environment-token-0009"  # noqa: S105 - fake
ROTATED = "canary-codex-auth-rotated-token-0010"


def _credential_home(base: Path) -> dict[str, str]:
    """The seven credential files of spec 8.8 under base, each with its canaries; the worker env that finds them."""
    home, claude, codex, pi = base / "home", base / "claude-config", base / "codex-home", base / "pi-agent"
    for folder in (home, claude, codex, pi):
        folder.mkdir(parents=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": CANARIES["claude:.credentials.json"], "expiresAt": 1}})
    )
    (claude / ".claude.json").write_text(
        json.dumps({"primaryApiKey": CANARIES["claude:.claude.json"], "theme": "dark"})
    )
    (claude / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": CANARIES["claude:settings.json"]}})
    )
    (codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": CANARIES["codex:auth.json"]}}))
    (codex / "config.toml").write_text(
        '[model_providers.cairn]\nname = "cairn"\nexperimental_bearer_token = "' + CANARIES["codex:config.toml"] + '"\n'
    )
    (pi / "auth.json").write_text(
        json.dumps(
            {
                "x": {"type": "oauth", "access": CANARIES["pi:auth.json"]},
                "y": {"type": "api_key", "key": CANARIES["pi:auth.json/key"]},
            }
        )
    )
    (pi / "models.json").write_text(
        json.dumps({"providers": {"cairn": {"baseUrl": "http://127.0.0.1:9", "apiKey": CANARIES["pi:models.json"]}}})
    )
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "PI_CODING_AGENT_DIR": str(pi),
    }


def _tc4_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC4, container mode: another worker's PI_API_KEY (bare, in JSON and NUL-separated as /proc/<pid>/environ
    prints it), an HTTPS_PROXY password and its user:pass, the repair token, and each reviewer's REVIEW_API_KEY
    (bare and JSON-escaped) are all removed and counted under their names; a 6-character reviewer key is left alone.
    """
    workers = [
        {
            "name": "w1",
            "type": "codex",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "test-model",
                "CODEX_BASE_URL": "http://127.0.0.1:9",
                "OPENAI_API_KEY": "test-k-0001",
            },
        },
        {
            "name": "w2",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "test-model",
                "PI_BASE_URL": "http://127.0.0.1:9",
                "PI_API_KEY": PI_KEY,
                "PI_PROVIDER_API": "openai-completions",
            },
        },
    ]
    section = repair_section()
    section["reviewers"].append(
        {
            "name": "r3",
            "env": {
                "REVIEW_MODEL": "test-model",
                "REVIEW_BASE_URL": "http://x.invalid",
                "REVIEW_API_KEY": REVIEWER_KEY_2,
            },
        }
    )
    section["reviewers"].append(
        {
            "name": "r4",
            "env": {
                "REVIEW_MODEL": "test-model",
                "REVIEW_BASE_URL": "http://x.invalid",
                "REVIEW_API_KEY": SHORT_REVIEWER_KEY,
            },
        }
    )
    common = {"HTTPS_PROXY": f"http://proxyuser:{PROXY_PASSWORD}@127.0.0.1:3128"}
    config = copy_config(tmp_path, "codex", workers=workers, common_env=common, reviewers=section["reviewers"])
    environ = f"PI_API_KEY={PI_KEY}\0HOME=/home/kali\0".encode()
    printed = (
        _session_with(
            "codex",
            f"bare {PI_KEY}",
            json.dumps({"PI_API_KEY": PI_KEY}),
            f"proxy password {PROXY_PASSWORD}, userinfo proxyuser:{PROXY_PASSWORD}",
            f"token {FAKE_TOKEN}",
            f"reviewer {FAKE_REVIEWER_KEY} and {REVIEWER_KEY_2} and {json.dumps(REVIEWER_KEY_2)}",
            f"short key {SHORT_REVIEWER_KEY}",
        )
        + environ
    )
    api, _path = codex_api(printed)
    install_docker(monkeypatch, api)
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    copied = read_transcript(config)
    for value in (PI_KEY, PROXY_PASSWORD, f"proxyuser:{PROXY_PASSWORD}", FAKE_TOKEN, FAKE_REVIEWER_KEY, REVIEWER_KEY_2):
        assert value.encode() not in copied, value
        for form in _forms(value):
            assert form not in copied, value
    assert b"canary-reviewer-key-two" not in copied
    assert f"short key {SHORT_REVIEWER_KEY}".encode() in copied
    assert b"PI_API_KEY=[REDACTED:PI_API_KEY]\x00HOME" in copied
    redactions = read_meta(config)["redactions"]
    assert redactions["PI_API_KEY"] == 3
    assert redactions["HTTPS_PROXY"] == 2  # the user:pass (longest first), then the bare password
    assert redactions["repair_token"] == 1
    # FAKE_REVIEWER_KEY once; REVIEWER_KEY_2 bare, and JSON-escaped twice (the session line escapes it once, and the
    # printed json.dumps form is escaped again inside it).
    assert redactions["REVIEW_API_KEY"] == 3
    assert copied.count(b"[REDACTED:REVIEW_API_KEY]") == 3
    assert "abc123" not in json.dumps(redactions)


def _tc4_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC4, local mode: a canary in each of the seven credential files of spec 8.8, found through a temporary HOME,
    CLAUDE_CONFIG_DIR, CODEX_HOME and PI_CODING_AGENT_DIR in worker.env (pi's auth.json as
    {"x":{"type":"oauth","access":…},"y":{"type":"api_key","key":…}}, and an ANTHROPIC_AUTH_TOKEN set only in a
    settings.json env block), and a variable of the dispatcher's own environment, printed by the run: none is in the
    copy or in any log record, and meta.json counts them under the files' labels. A value added to a credential file
    between two copies is redacted from the second.
    """
    worker_env = _credential_home(tmp_path / "creds")
    monkeypatch.setenv("CAIRN_TEST_LOCAL_TOKEN", LOCAL_ENV_TOKEN)
    workers = [
        {"name": "w1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": worker_env}
    ]
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)
    sessions = Path(worker_env["CODEX_HOME"]) / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-2026-11-14T16-56-50-{SESSION_IDS['codex']}.jsonl"
    printed_values = [*CANARIES.values(), LOCAL_ENV_TOKEN]
    auth_content = (Path(worker_env["PI_CODING_AGENT_DIR"]) / "auth.json").read_text()  # what `cat auth.json` prints
    source.write_bytes(_session_with("codex", *printed_values, auth_content))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path / "work"), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    tick_at(schedule, clock, 1260)
    copied = read_transcript(config)
    for value in printed_values:
        assert value.encode() not in copied, value
    redactions = read_meta(config)["redactions"]
    for label in ("claude:.credentials.json", "claude:.claude.json", "claude:settings.json", "codex:auth.json"):
        assert redactions[label] == 1, label
    assert redactions["codex:config.toml"] == 1
    assert redactions["pi:auth.json"] == 4 and redactions["pi:models.json"] == 1  # bare, and in the cat output
    assert redactions["CAIRN_TEST_LOCAL_TOKEN"] == 1
    assert read_meta(config)["located_by"] == "session_id"

    # A token added to a credential file after the first copy is removed from the second.
    auth = Path(worker_env["CODEX_HOME"]) / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": CANARIES["codex:auth.json"], "refresh_token": ROTATED}}))
    with source.open("ab") as handle:
        handle.write(_session_with("codex", ROTATED)[len(fixture_bytes("codex")) :])
    tick_at(schedule, clock, 1275)
    second = read_transcript(config)
    assert ROTATED.encode() not in second and read_meta(config)["copies"] == 2
    assert read_meta(config)["redactions"]["codex:auth.json"] == 2
    text = all_log_text(caplog)
    assert all(value not in text for value in [*printed_values, ROTATED])
    # The paths come from each worker's merged environment, ~ being its HOME.
    assert redaction.credential_paths({"HOME": "/h"}) == [
        ("claude:.credentials.json", "/h/.claude/.credentials.json"),
        ("codex:auth.json", "/h/.codex/auth.json"),
        ("pi:auth.json", "/h/.pi/agent/auth.json"),
        ("claude:.claude.json", "/h/.claude.json"),
        ("claude:settings.json", "/h/.claude/settings.json"),
        ("codex:config.toml", "/h/.codex/config.toml"),
        ("pi:models.json", "/h/.pi/agent/models.json"),
    ]
    assert ("claude:.claude.json", "/c/.claude.json") in redaction.credential_paths(
        {"HOME": "/h", "CLAUDE_CONFIG_DIR": "/c"}
    )


def _tc4_unusable_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC4: a missing, unparsable, 200,000-deep, FIFO or over-16-MiB credential file is skipped without an exception,
    the oversized one with one WARNING (once per process); a credential-only file whose canary sits 5,000 levels
    deep still gives the canary; a settings.json whose top level is an array, or whose env is null or a list, gives
    no values.
    """
    base = tmp_path / "creds"
    env = _credential_home(base)
    claude, codex, pi = Path(env["CLAUDE_CONFIG_DIR"]), Path(env["CODEX_HOME"]), Path(env["PI_CODING_AGENT_DIR"])
    (claude / ".credentials.json").unlink()  # missing
    (claude / ".claude.json").write_text("{not json")  # unparsable
    (pi / "models.json").write_text("[" * 200_000 + "]" * 200_000)  # 200,000 deep
    os.unlink(pi / "auth.json")
    os.mkfifo(pi / "auth.json")  # a FIFO: O_NONBLOCK, then fstat
    with (codex / "config.toml").open("wb") as handle:
        handle.truncate(redaction.CREDENTIAL_FILE_MAX_BYTES + 1)  # over 16 MiB (sparse)
    deep_canary = "canary-five-thousand-levels-deep-0011"
    (codex / "auth.json").write_text('{"a":' * 5000 + json.dumps(deep_canary) + "}" * 5000)
    workers = [{"name": "w1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": env}]
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)
    started = time.monotonic()
    pairs = redaction.credential_file_pairs(config, {})
    pairs_again = redaction.credential_file_pairs(config, {})
    assert time.monotonic() - started < 5
    assert pairs == pairs_again
    assert ("codex:auth.json", deep_canary) in pairs
    labels = {label for label, _value in pairs}
    assert labels == {"codex:auth.json", "claude:settings.json"}
    oversized = copy_lines(caplog, TOO_LARGE_LINE)
    assert oversized == [("codex:config.toml", redaction.CREDENTIAL_FILE_MAX_BYTES + 1)]
    for settings in ([1, 2], {"env": None}, {"env": ["ANTHROPIC_AUTH_TOKEN", "x" * 20]}):
        (claude / "settings.json").write_text(json.dumps(settings))
        assert {label for label, _value in redaction.credential_file_pairs(config, {})} == {"codex:auth.json"}
    sessions = codex / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-x-{SESSION_IDS['codex']}.jsonl").write_bytes(_session_with("codex", f"deep {deep_canary}"))
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, container=str(tmp_path), session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)  # the copy goes on despite the unusable files
    assert deep_canary.encode() not in read_transcript(config)
    assert read_meta(config)["redactions"]["codex:auth.json"] == 1


@pytest.mark.parametrize("case", ["container", "local", "unusable_files"])
def test_redaction_covers_every_worker_local_env_repair_token_and_proxy_userinfo(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC4, in three cases: every worker's secrets, the repair token, the reviewers' keys and proxy userinfo in
    container mode (_tc4_container); the local CLIs' credential files and the dispatcher's own environment in local
    mode, with a rotated token (_tc4_local); and the credential files that are skipped (_tc4_unusable_files)."""
    cases = {"container": _tc4_container, "local": _tc4_local, "unusable_files": _tc4_unusable_files}
    cases[case](tmp_path, monkeypatch, caplog)


# TC5 -----------------------------------------------------------------------------------------------------------------


def test_copy_refuses_symlinks_fifos_oversize_and_non_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC5. Local mode: once the session file has been copied, a symlink, a FIFO (refused within 1 s), a folder or
    a file over COPY_MAX_BYTES in its place is refused (not_regular, too_large), meta.json says so, and the previous
    good transcript stays. Container mode: a stat with a link target, a FIFO's mode or a size over the cap is refused
    without a GET, and a GET whose member is a symlink, or that holds two members, is refused too.
    """
    clock = FakeClock(monkeypatch)
    home = tmp_path / "home"
    workers = [
        {
            "name": "w1",
            "type": "codex",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {"HOME": str(home)},
        }
    ]
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)
    sessions = home / ".codex" / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    source = sessions / f"rollout-x-{SESSION_IDS['codex']}.jsonl"
    source.write_bytes(fixture_bytes("codex"))
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(fixture_bytes("codex") + b'{"type":"outside"}\n')
    schedule = CopySchedule(make_run(config, container=str(tmp_path), session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    good = read_transcript(config)
    assert good == fixture_bytes("codex")

    def replace_with(kind: str) -> None:
        source.unlink() if not source.is_dir() else source.rmdir()
        if kind == "symlink":
            source.symlink_to(outside)
        elif kind == "fifo":
            os.mkfifo(source)
        elif kind == "folder":
            source.mkdir()
        else:
            with source.open("wb") as handle:
                handle.truncate(transcripts.COPY_MAX_BYTES + 1)

    elapsed = 1260
    for kind, status in (
        ("symlink", "not_regular"),
        ("fifo", "not_regular"),
        ("folder", "not_regular"),
        ("big", "too_large"),
    ):
        replace_with(kind)
        elapsed += 15
        started = time.monotonic()
        tick_at(schedule, clock, elapsed)
        assert time.monotonic() - started < 1.0, kind
        meta = read_meta(config)
        assert meta["status"] == status, kind
        assert meta["copies"] == 1 and meta["copied_elapsed_seconds"] == 1260.0
        assert read_transcript(config) == good
    assert {args[3] for args in copy_lines(caplog, COPY_FAILED)} == {"not_regular", "too_large"}

    # Container mode, through the archive endpoint's stat and stream.
    container_config = copy_config(tmp_path / "container", "codex")
    path = container_session_path("codex", SESSION_IDS["codex"])
    later = "2026-11-14T10:30:00.000000000Z"
    cases = [
        (FakeFile(fixture_bytes("codex"), kind="symlink", target="/etc/passwd", mtime=later), "not_regular", False),
        (FakeFile(b"", kind="fifo", mtime=later), "not_regular", False),
        (FakeFile(b"x", size=transcripts.COPY_MAX_BYTES + 1, mtime=later), "too_large", False),
        (FakeFile(fixture_bytes("codex"), member="symlink", mtime=later), "not_regular", True),
        (FakeFile(fixture_bytes("codex"), member="two", mtime=later), "error", True),
    ]
    for index, (refused, status, gets) in enumerate(cases):
        api = install_docker(monkeypatch, FakeDockerApi({path: FakeFile(fixture_bytes("codex"))}))
        run_id = f"{index:032x}"
        schedule = CopySchedule(make_run(container_config, session_id=SESSION_IDS["codex"], run_id=run_id), FAKE_TOKEN)
        tick_at(schedule, clock, 1260)
        api.files[path] = refused
        before = len(api.calls_of("get"))
        tick_at(schedule, clock, 1275)
        meta = read_meta(container_config, run_id=run_id)
        assert meta["status"] == status, (index, meta["status"])
        assert read_transcript(container_config, run_id=run_id) == fixture_bytes("codex")
        assert (len(api.calls_of("get")) > before) is gets
        assert all(response.closed for response in api.responses)


# TC6, TC7, TC8 -------------------------------------------------------------------------------------------------------

DEEP_LINE = "[" * 200_000 + "]" * 200_000

# The intent's text in each pinned file's records that hold the prompt, and the same text with U+2028, U+2029 and
# U+0085 in it, written raw as the pinned CLIs write them inside a JSON string (spec Appendix B, the sepprompt probes).
INTENT_TEXT = b"Fixture intent: list the configuration files."
SEPARATED_INTENT = "Fixture intent: list the configuration files.\u2028line two\u2029para\u0085next".encode()


def _bad_lines(cli: str) -> list[str]:
    """Lines that are not user messages: nested too deep, not objects, and a record of the right type whose message
    (codex: payload) is a list."""
    wrong_shape = (
        {"type": "response_item", "payload": [{"type": "message", "role": "user", "content": MARKER}]}
        if cli == "codex"
        else {"type": "user" if cli == "claudecode" else "message", "message": [{"role": "user", "content": MARKER}]}
    )
    return [DEEP_LINE, "[]", "null", json.dumps("a string " + MARKER), json.dumps(wrong_shape)]


def _prompt_line_index(cli: str) -> int:
    lines = fixture_bytes(cli).split(b"\n")
    return next(i for i, line in enumerate(lines) if MARKER.encode() in line and holds_marker(line, MARKER, cli))


@pytest.mark.parametrize("cli", ["claudecode", "codex", "pi"])
def test_locate_by_id_requires_the_marker_in_an_early_user_message(
    cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC6. With the pinned fixtures, a file found by the run's session id is taken only when the run's marker is in
    a user message before the first tool call, within the first 50 lines: lines nested 200,000 deep, or decoding to
    [], null or a string, or a record whose message (codex: payload) is a list, before the prompt's user message are
    not user messages and the file is still found; the marker only in a tool's output, after the first tool call,
    past line 50, or another run's marker, is not found.

    The file is decoded and split into lines as the agent reads a copy (spec 8.8 Locate, 9.5): as UTF-8 with
    replacement, at \\n only. With U+2028, U+2029 and U+0085 written raw in the intent's text of each record that holds
    the prompt, the marker is still found, where a reading that splits with str.splitlines finds no user message that
    holds it; empty pieces are skipped and not counted, so 50 of them before the prompt still find it, while 50 pieces
    of one space are lines and push it past line 50; and a byte that is not UTF-8 in the prompt's record keeps that
    record a user message.
    """
    original = fixture_bytes(cli)
    lines = original.split(b"\n")
    at = _prompt_line_index(cli)
    noisy = b"\n".join([*lines[:at], *(line.encode() for line in _bad_lines(cli)), *lines[at:]])
    assert holds_marker(original, MARKER, cli) and holds_marker(noisy, MARKER, cli)
    assert not holds_marker(original, OTHER_MARKER, cli)
    pushed = b"\n".join([*lines[:at], *([b"{}"] * 50), *lines[at:]])
    assert not holds_marker(pushed, MARKER, cli)  # past line 50

    separated = original.replace(INTENT_TEXT, SEPARATED_INTENT)
    assert separated.count(SEPARATED_INTENT) == original.count(MARKER.encode())  # each record that holds the prompt
    assert holds_marker(separated, MARKER, cli)
    pieces = separated.decode("utf-8", "replace").splitlines()
    assert len(pieces) > len(original.decode("utf-8").splitlines())  # str.splitlines cuts the prompt's records
    assert not any(holds_marker(piece.encode(), MARKER, cli) for piece in pieces)
    blank = b"\n".join([*lines[:at], *([b""] * 50), *lines[at:]])
    assert holds_marker(blank, MARKER, cli)  # empty pieces are not counted
    spaced = b"\n".join([*lines[:at], *([b" "] * 50), *lines[at:]])
    assert not holds_marker(spaced, MARKER, cli)  # a piece of one space is a line: past line 50
    undecodable = original.replace(INTENT_TEXT, INTENT_TEXT + b"\xff\xfe")
    assert holds_marker(undecodable, MARKER, cli)  # decoded with replacement, the record holds the marker
    first_call = next(
        index
        for index, line in enumerate(lines)
        if any(word in line for word in (b'"tool_use"', b'"function_call"', b'"toolCall"'))
    )
    moved = b"\n".join([*lines[:at], *lines[at + 1 : first_call + 1], lines[at], *lines[first_call + 1 :]])
    if cli == "codex":  # codex also writes the prompt as an event_msg user_message right after it: move both
        moved = b"\n".join(line for line in moved.split(b"\n") if b'"user_message"' not in line) + b"\n" + lines[at + 1]
    assert not holds_marker(moved, MARKER, cli)  # the prompt only after the first tool call

    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, cli)
    path = container_session_path(cli, SESSION_IDS[cli])
    for index, (content, found) in enumerate(
        (
            (noisy, True),
            (separated, True),
            (blank, True),
            (undecodable, True),
            (pushed, False),
            (spaced, False),
            (moved, False),
            (fixture_bytes(cli, OTHER_MARKER), False),
        )
    ):
        install_docker(monkeypatch, FakeDockerApi({path: FakeFile(content)}))
        run_id = f"{index + 1:032x}"
        schedule = CopySchedule(make_run(config, cli=cli, session_id=SESSION_IDS[cli], run_id=run_id), FAKE_TOKEN)
        tick_at(schedule, clock, 1260)
        meta = read_meta(config, run_id=run_id)
        if found:
            assert meta["status"] == "ok" and meta["source_path"] == path
            assert meta["located_by"] == ("path" if cli == "claudecode" else "session_id")
            assert read_transcript(config, run_id=run_id) == content
        else:
            assert meta["status"] == "not_found" and meta["source_path"] is None and meta["copies"] == 0


def _codex_folder(tmp_path: Path) -> tuple[DispatchConfig, Path]:
    home = tmp_path / "home"
    workers = [
        {
            "name": "w1",
            "type": "codex",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {"HOME": str(home)},
        }
    ]
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)
    sessions = home / ".codex" / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True)
    return config, sessions


def test_locate_by_marker_finds_the_codex_file_whose_first_user_message_is_environment_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC7. With no session id known, the codex file whose first user message is <environment_context> and whose
    second holds the run's marker is found by marker (located_by prompt_marker), among newer files of other runs; a
    file modified before the run's start minus 60 s is not a candidate. The same through find in a container.
    """
    lines = fixture_bytes("codex").split(b"\n")
    assert b"<environment_context>" in lines[3] and MARKER.encode() in lines[5]  # the pinned order
    config, sessions = _codex_folder(tmp_path)
    ours = sessions / "rollout-2026-11-14T16-56-50-ours.jsonl"
    ours.write_bytes(fixture_bytes("codex"))
    other = sessions / "rollout-2026-11-14T16-57-00-other.jsonl"
    other.write_bytes(fixture_bytes("codex", OTHER_MARKER))
    old = sessions / "rollout-2026-11-14T10-00-00-old.jsonl"
    old.write_bytes(fixture_bytes("codex"))
    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(other, (now + 5, now + 5))  # newest: looked at first
    clock = FakeClock(monkeypatch, wall=now)
    run = make_run(config, container=str(tmp_path), session_id=None)
    run.started_wall = now
    schedule = CopySchedule(run, FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    meta = read_meta(config)
    assert (meta["status"], meta["located_by"], meta["source_path"]) == ("ok", "prompt_marker", str(ours))
    ours.unlink()  # once found, the file is kept: nothing else is taken for it
    tick_at(schedule, clock, 1275)
    assert read_meta(config)["status"] == "not_found" and read_meta(config)["source_path"] == str(ours)

    # In a container: one find -newermt @<start - 60 s> -printf, then the candidates' heads, newest first.
    container_config = copy_config(tmp_path / "container", "codex")
    folder = "/home/kali/.codex/sessions/2026/11/14"
    api = install_docker(
        monkeypatch,
        FakeDockerApi(
            {
                f"{folder}/rollout-a-ours.jsonl": FakeFile(fixture_bytes("codex"), epoch=WALL_START + 5),
                f"{folder}/rollout-b-other.jsonl": FakeFile(fixture_bytes("codex", OTHER_MARKER), epoch=WALL_START + 9),
                f"{folder}/rollout-c-old.jsonl": FakeFile(fixture_bytes("codex"), epoch=WALL_START - 61),
            }
        ),
    )
    schedule = CopySchedule(make_run(container_config, session_id=None), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    meta = read_meta(container_config)
    assert (meta["status"], meta["located_by"], meta["source_path"]) == (
        "ok",
        "prompt_marker",
        f"{folder}/rollout-a-ours.jsonl",
    )
    [(_kind, container, command, _kwargs)] = api.calls_of("exec_create")
    assert container == CONTAINER
    since = int(WALL_START - 60)
    find = ["timeout", "-k", "1", "5", "find", "/home/kali/.codex/sessions", "-maxdepth", "4", "-type", "f"]
    assert command == [
        *find,
        "-name",
        "*.jsonl",
        "-newermt",
        f"@{since}",
        "-printf",
        "%T@ %p\\n",
    ]
    assert [call[2] for call in api.calls_of("get")][:2] == [
        f"{folder}/rollout-b-other.jsonl",
        f"{folder}/rollout-a-ours.jsonl",
    ]


TOOL_OUTPUT = {
    "claudecode": lambda text: {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]},
    },
    "codex": lambda text: {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": text},
    },
    "pi": lambda text: {
        "type": "message",
        "message": {"role": "toolResult", "content": [{"type": "text", "text": text}]},
    },
}


@pytest.mark.parametrize("cli", ["claudecode", "codex", "pi"])
def test_marker_in_tool_output_does_not_count(cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC8. Another run's file that shows the marker in a tool's output (ls /tmp/cairn-prompts, say) before its own
    first tool call is not taken for this run, whichever way it is found; this run's own file is.
    """
    lines = fixture_bytes(cli, OTHER_MARKER).split(b"\n")
    at = _prompt_line_index(cli)
    listing = json.dumps(TOOL_OUTPUT[cli](f"ls /tmp/cairn-prompts\n{MARKER}\n")).encode()
    foreign = b"\n".join([*lines[: at + 1], listing, *lines[at + 1 :]])
    assert MARKER.encode() in foreign and not holds_marker(foreign, MARKER, cli)
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, cli)
    path = container_session_path(cli, SESSION_IDS[cli])
    api = install_docker(monkeypatch, FakeDockerApi({path: FakeFile(foreign)}))
    schedule = CopySchedule(make_run(config, cli=cli, session_id=SESSION_IDS[cli]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    assert read_meta(config)["status"] == "not_found"
    api.files[path] = FakeFile(fixture_bytes(cli))
    tick_at(schedule, clock, 1275)
    assert read_meta(config)["status"] == "ok" and read_meta(config)["source_path"] == path
    if cli != "claudecode":  # by marker, among the newer files of the CLI's folder
        sessions = path.rsplit("/", 1)[0]
        api = install_docker(
            monkeypatch,
            FakeDockerApi(
                {
                    f"{sessions}/zz-foreign.jsonl": FakeFile(foreign, epoch=WALL_START + 9),
                    f"{sessions}/aa-ours.jsonl": FakeFile(fixture_bytes(cli), epoch=WALL_START + 1),
                }
            ),
        )
        schedule = CopySchedule(make_run(config, cli=cli, session_id=None, run_id="1" * 32), FAKE_TOKEN)
        tick_at(schedule, clock, 1260)
        meta = read_meta(config, run_id="1" * 32)
        assert (meta["located_by"], meta["source_path"]) == ("prompt_marker", f"{sessions}/aa-ours.jsonl")


# TC9 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("workspace", ["relative", "symlinked"])
def test_local_claudecode_path_uses_the_merged_env_and_realpath(
    workspace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC9. Local mode: Claude Code's session file is <CLAUDE_CONFIG_DIR>/projects/<slug(realpath(workdir))>/<id>.jsonl,
    with CLAUDE_CONFIG_DIR, or HOME, from the merged environment {**os.environ, **worker.env}, and the work folder's
    physical path, whether workspace_root is relative or a symlink: the file is found at that exact path
    (located_by path), not by the fallback search.
    """
    real = tmp_path / "real-root"
    real.mkdir()
    if workspace == "relative":
        monkeypatch.chdir(real)
        workspace_root = "workspaces"
    else:
        (tmp_path / "link-root").symlink_to(real, target_is_directory=True)
        workspace_root = str(tmp_path / "link-root" / "workspaces")
    backend = LocalBackend(LocalConfig(workspace_root=workspace_root))
    workdir = backend.ensure_running("proj_027")
    physical = os.path.realpath(workdir)
    assert physical.startswith(str(real)) and (workspace == "relative" or physical != workdir)
    clock = FakeClock(monkeypatch)
    session_id = SESSION_IDS["claudecode"]
    for variant in ("config_dir", "home"):
        config_dir = tmp_path / variant / ".claude"
        env = {"CLAUDE_CONFIG_DIR": str(config_dir)} if variant == "config_dir" else {"HOME": str(tmp_path / variant)}
        if variant == "home":
            monkeypatch.setenv("HOME", str(tmp_path / "not-the-workers-home"))
        workers = [
            {"name": "w1", "type": "claudecode", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": env}
        ]
        config = copy_config(tmp_path / f"copies-{variant}", "claudecode", execution="local", workers=workers)
        folder = config_dir / "projects" / transcripts.claude_project_slug(physical)
        folder.mkdir(parents=True)
        (folder / f"{session_id}.jsonl").write_bytes(fixture_bytes("claudecode"))
        if workspace == "symlinked":  # a decoy at the unresolved path's slug is never looked at
            decoy = config_dir / "projects" / transcripts.claude_project_slug(workdir)
            decoy.mkdir(parents=True)
            (decoy / f"{session_id}.jsonl").write_bytes(fixture_bytes("claudecode", OTHER_MARKER))
        schedule = CopySchedule(make_run(config, container=workdir, session_id=session_id), FAKE_TOKEN)
        tick_at(schedule, clock, 1260)
        meta = read_meta(config)
        assert (meta["status"], meta["located_by"]) == ("ok", "path")
        assert meta["source_path"] == str(folder / f"{session_id}.jsonl")


# TC10, TC11 ----------------------------------------------------------------------------------------------------------


def test_copy_schedule_runs_from_70_to_80_percent_and_once_after_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC10. For an 1800 s budget the ticks are due at 1260 s (70%) and then every 15 s until 1455 s (80% + 15 s):
    14 of them; the final copy follows the task once, only if the run reached 70% and its task was not cancelled.
    Budgets under 350 s and the mock CLI are never copied; a 350 s budget has 4 ticks.
    """
    clock = FakeClock(monkeypatch)
    api, path = codex_api()
    install_docker(monkeypatch, api)
    config = copy_config(tmp_path, "codex")
    run = make_run(config, session_id=SESSION_IDS["codex"])
    schedule = CopySchedule(run, FAKE_TOKEN)
    dues = []
    while (due := schedule.next_due()) is not None:
        dues.append(round(due - START, 6))
        api.files[path] = FakeFile(fixture_bytes("codex") + b"\n" * len(dues), mtime=f"t{len(dues)}")
        tick_at(schedule, clock, due - START)
    assert dues == [1260.0 + 15 * index for index in range(14)]
    assert len(api.calls_of("head")) == 14 and read_meta(config)["copies"] == 14
    assert schedule.next_due() is None
    run.exited_monotonic = START + 1500
    clock.at(1520)
    gets = len(api.calls_of("get"))
    schedule.final_copy()
    assert len(api.calls_of("get")) == gets  # unchanged since the last tick: meta.json only
    meta = read_meta(config)
    assert meta["final"] is True and meta["checked_elapsed_seconds"] == 1520.0 and meta["copies"] == 14
    calls = len(api.calls)
    schedule.final_copy()
    schedule.tick()
    assert len(api.calls) == calls  # once after the task, and nothing after it

    def ticks(budget: int, cli: str = "codex") -> list[float]:
        schedule = CopySchedule(make_run(config, cli=cli, budget=budget, run_id="2" * 32), FAKE_TOKEN)
        found = []
        while (due := schedule.next_due()) is not None:
            found.append(round(due - START, 6))
            clock.at(due - START)
            schedule.tick()
        return found

    assert ticks(349) == [] and ticks(1800, "mock") == []
    assert ticks(350) == [245.0, 260.0, 275.0, 290.0]

    # No final copy for a run that ended before 70% of its budget, for a cancelled task, or for a short budget.
    for exited, cancelled, budget in ((1000, False, 1800), (1500, True, 1800), (300, False, 349)):
        api = install_docker(monkeypatch, codex_api()[0])
        cancellation = TaskCancellation()
        if cancelled:
            cancellation.cancel("stopped")
        run = make_run(
            config, budget=budget, cancellation=cancellation, run_id="3" * 32, session_id=SESSION_IDS["codex"]
        )
        run.exited_monotonic = START + exited
        clock.at(exited + 100)
        CopySchedule(run, FAKE_TOKEN).final_copy()
        assert api.calls == []


def test_unchanged_files_are_not_copied_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC11. An unchanged tick copies nothing and rewrites only meta.json, with a new checked_elapsed_seconds and
    checked_at and the old copied_elapsed_seconds and copied_at; a stat with no answer (a connection error, a 503)
    writes nothing; an unchanged final copy rewrites meta.json with final: true. The same in local mode.
    """
    clock = FakeClock(monkeypatch)
    api, _path = codex_api()
    install_docker(monkeypatch, api)
    config = copy_config(tmp_path, "codex")
    run = make_run(config, session_id=SESSION_IDS["codex"])
    schedule = CopySchedule(run, FAKE_TOKEN)
    tick_at(schedule, clock, 1260.4)
    gets = len(api.calls_of("get"))  # the marker check's read of the file's head, then the copy
    folder = copy_folder(config)
    transcript_stat = os.stat(folder / "transcript.jsonl")
    first = read_meta(config)
    assert (first["copied_elapsed_seconds"], first["checked_elapsed_seconds"]) == (1260.4, 1260.4)
    tick_at(schedule, clock, 1275.6)
    assert len(api.calls_of("get")) == gets  # the stat showed the file unchanged
    second = read_meta(config)
    assert os.stat(folder / "transcript.jsonl").st_mtime_ns == transcript_stat.st_mtime_ns
    assert os.stat(folder / "transcript.jsonl").st_ino == transcript_stat.st_ino
    assert second == {**first, "checked_elapsed_seconds": 1275.6, "checked_at": _stamp(1275.6)}
    assert second["copied_at"] == first["copied_at"] == _stamp(1260.4)

    meta_bytes = (folder / "meta.json").read_bytes()
    api.raise_on = {"head"}
    tick_at(schedule, clock, 1290)
    api.raise_on = set()
    api.head_status = 503
    tick_at(schedule, clock, 1305)
    assert (folder / "meta.json").read_bytes() == meta_bytes  # no answer: nothing written
    api.head_status = None
    run.exited_monotonic = START + 1400
    clock.at(1500.2)
    schedule.final_copy()
    final = read_meta(config)
    assert final == {**second, "checked_elapsed_seconds": 1500.2, "checked_at": _stamp(1500.2), "final": True}
    assert len(api.calls_of("get")) == gets

    # Local mode: the same, with the source's size and modification time.
    local_config, sessions = _codex_folder(tmp_path / "local")
    source = sessions / f"rollout-x-{SESSION_IDS['codex']}.jsonl"
    source.write_bytes(fixture_bytes("codex"))
    schedule = CopySchedule(
        make_run(local_config, container=str(tmp_path), session_id=SESSION_IDS["codex"]), FAKE_TOKEN
    )
    tick_at(schedule, clock, 1260)
    tick_at(schedule, clock, 1275)
    meta = read_meta(local_config)
    assert (meta["copies"], meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (1, 1260.0, 1275.0)
    with source.open("ab") as handle:
        handle.write(b'{"type":"event_msg","payload":{"type":"token_count"}}\n')
    tick_at(schedule, clock, 1290)
    meta = read_meta(local_config)
    assert (meta["copies"], meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (2, 1290.0, 1290.0)


def _stamp(elapsed: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(WALL_START + elapsed))


# TC12 ----------------------------------------------------------------------------------------------------------------

GOLDEN_RUN = COPY_FIXTURE / "proj_027" / RUN_ID


def test_meta_json_matches_the_golden_copy_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC12. The dispatcher's writer makes the golden copy of cairn/tests/fixtures/repair_copy/proj_027/<run_id>/
    (the agent's AG22 reads it): a codex run copied six times while its session file grew, at 1260 s to 1335 s, and
    found unchanged at the 1350 s tick, so meta.json has both the copied_* and the checked_* keys. The copy is written
    under umask 077: the copy root it creates is 0750 (spec 8.8 Write), its project and run folders are 0750 and its
    files 0640. Each copy replaces transcript.jsonl and then meta.json, so meta.json never describes a transcript
    other than the one on disk (spec 8.8 Write). With CAIRN_WRITE_GOLDEN_COPY=1, or when the fixture is missing, the
    test writes it; otherwise the bytes must match.
    """
    workers = [
        {
            "name": "codex-1",
            "type": "codex",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "test-model",
                "CODEX_BASE_URL": "http://127.0.0.1:9",
                "OPENAI_API_KEY": "test-key-0001",
            },
        }
    ]
    config = copy_config(tmp_path, "codex", workers=workers)
    assert config.repair is not None
    root = Path(config.repair.transcript_root)
    assert not root.exists()  # the dispatcher creates it, at the first copy
    lines = fixture_bytes("codex").split(b"\n")
    api, path = codex_api()
    install_docker(monkeypatch, api)
    clock = FakeClock(monkeypatch)
    writes: list[str] = []
    stale_meta: list[str] = []
    real_write_file = transcripts.write_file

    def write_file(folder_fd: int, name: str, data: bytes, gid: int) -> None:
        if name == transcripts.META_NAME:  # the transcript on disk must already be the one meta.json describes
            try:
                fd = os.open(transcripts.TRANSCRIPT_NAME, os.O_RDONLY, dir_fd=folder_fd)
            except FileNotFoundError:
                stale_meta.append("no transcript.jsonl yet")
            else:
                with os.fdopen(fd, "rb") as handle:
                    on_disk = hashlib.sha256(handle.read()).hexdigest()
                if on_disk != json.loads(data)["sha256"]:
                    stale_meta.append(f"meta.json of copy {json.loads(data)['copies']} before its transcript.jsonl")
        real_write_file(folder_fd, name, data, gid)
        writes.append(name)

    monkeypatch.setattr(transcripts, "write_file", write_file)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    previous = os.umask(0o077)
    try:
        for index, count in enumerate((7, 9, 11, 14, 17, 21)):  # the prompt is line 6
            mtime = f"2026-11-14T10:{20 + index:02d}:00.000000000Z"
            api.files[path] = FakeFile(b"\n".join(lines[:count]), mtime=mtime)
            tick_at(schedule, clock, 1260 + 15 * index + 1.2)
        tick_at(schedule, clock, 1351.2)
    finally:
        os.umask(previous)
    folder = copy_folder(config)
    meta = read_meta(config)
    assert list(meta) == META_KEYS
    assert meta["copied_at"] == "2026-11-14T10:22:16Z" and meta["checked_at"] == "2026-11-14T10:22:31Z"
    assert (meta["copied_elapsed_seconds"], meta["checked_elapsed_seconds"]) == (1336.2, 1351.2)
    assert (meta["copies"], meta["status"], meta["final"], meta["located_by"]) == (6, "ok", False, "session_id")
    assert meta["source_path"] == path and meta["source_bytes"] == len(fixture_bytes("codex"))
    transcript = (folder / "transcript.jsonl").read_bytes()
    assert transcript == fixture_bytes("codex") and meta["sha256"] == hashlib.sha256(transcript).hexdigest()
    assert writes == ["transcript.jsonl", "meta.json"] * 6 + ["meta.json"] and stale_meta == []
    for item, mode in (
        (root, 0o750),
        (root / "proj_027", 0o750),
        (folder, 0o750),
        (folder / "meta.json", 0o640),
        (folder / "transcript.jsonl", 0o640),
    ):
        assert stat.S_IMODE(os.stat(item).st_mode) == mode, item
    if os.environ.get("CAIRN_WRITE_GOLDEN_COPY") == "1" or not GOLDEN_RUN.exists():
        GOLDEN_RUN.mkdir(parents=True, exist_ok=True)
        for name in ("meta.json", "transcript.jsonl"):
            shutil.copyfile(folder / name, GOLDEN_RUN / name)
    for name in ("meta.json", "transcript.jsonl"):
        assert (folder / name).read_bytes() == (GOLDEN_RUN / name).read_bytes(), name


# TC14 ----------------------------------------------------------------------------------------------------------------


def test_container_copy_uses_timed_archive_reads_and_closes_every_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC14. Every archive request carries a (5, 10) s timeout; the copy's GET streams with Accept-Encoding identity;
    every response is closed, refused ones included. The find exec is timeout -k 1 5 find …, with no environment,
    read through exec_start's socket: one that never sends and one that floods each end that tick's locate as
    not_found within FIND_READ_SECONDS plus 1 s, the flood read no further than FIND_MAX_BYTES, and the socket is
    closed.
    """
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, "codex")
    api, path = codex_api()
    install_docker(monkeypatch, api)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    assert read_meta(config)["status"] == "ok"
    [(_kind, container, command, kwargs)] = api.calls_of("exec_create")
    assert container == CONTAINER
    find = ["timeout", "-k", "1", "5", "find", "/home/kali/.codex/sessions", "-maxdepth", "4", "-type", "f"]
    assert command == [*find, "-name", f"rollout-*-{SESSION_IDS['codex']}.jsonl"]
    assert kwargs == {"stdout": True, "stderr": True, "stdin": False, "tty": False}  # no environment of its own
    assert [call[2:] for call in api.calls_of("exec_start")] == [(True, {})]
    url = f"http+docker://localhost/v1.47/containers/{CONTAINER}/archive"
    for call in api.calls_of("head"):
        assert call[1:4] == (url, path, (5, 10))
    for call in api.calls_of("get"):
        assert call[1:6] == (url, path, (5, 10), True, {"Accept-Encoding": "identity"})
    assert all(response.closed for response in api.responses)
    assert all(sock.closed for sock in api.sockets)

    for mode in ("silent", "flood"):
        api = install_docker(monkeypatch, FakeDockerApi({path: FakeFile(fixture_bytes("codex"))}))
        api.find_mode = mode
        run_id = ("4" if mode == "silent" else "5") * 32
        schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"], run_id=run_id), FAKE_TOKEN)
        started = time.monotonic()
        try:
            tick_at(schedule, clock, 1260)
        finally:
            api.close()
        elapsed = time.monotonic() - started
        assert elapsed < transcripts.FIND_READ_SECONDS + 1, (mode, elapsed)
        if mode == "silent":
            assert elapsed >= transcripts.FIND_READ_SECONDS - 0.1
        [sock] = api.sockets
        assert sock.closed and sock.shut
        assert sock.received <= transcripts.FIND_MAX_BYTES
        if mode == "flood":
            assert sock.received > 0
        assert read_meta(config, run_id=run_id)["status"] == "not_found"
        assert api.calls_of("get") == []


# TC17 ----------------------------------------------------------------------------------------------------------------


def test_a_cancelled_task_closes_its_copy_at_once_and_gets_no_final_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC17. The task's cancellation, set while a copy streams, closes the response before the next chunk read, and
    the previous good transcript and meta.json stay byte for byte; no stat, copy or meta.json write follows, and the
    final copy makes none. A run whose task is not cancelled still gets its final copy.
    """
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, "codex")
    big = fixture_bytes("codex") + b'{"type":"event_msg","payload":{"type":"pad","text":"' + b"x" * 400_000 + b'"}}\n'
    api, path = codex_api(big)
    api.chunk = 16 * 1024
    install_docker(monkeypatch, api)
    cancellation = TaskCancellation()
    run = make_run(config, session_id=SESSION_IDS["codex"], cancellation=cancellation)
    schedule = CopySchedule(run, FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    folder = copy_folder(config)
    before = {name: (folder / name).read_bytes() for name in ("meta.json", "transcript.jsonl")}
    api.files[path] = FakeFile(big + b'{"type":"more"}\n', mtime="later")

    def cancel_after_the_first_chunk(reads: int) -> None:
        if reads == 1:
            cancellation.cancel("stopped")

    api.on_chunk = cancel_after_the_first_chunk
    tick_at(schedule, clock, 1275)
    streamed = api.responses[-1]
    assert streamed.closed and streamed.raw is not None and streamed.raw.reads == 1
    assert {name: (folder / name).read_bytes() for name in before} == before
    calls = len(api.calls)
    tick_at(schedule, clock, 1290)
    run.exited_monotonic = START + 1300
    schedule.final_copy()
    assert schedule.next_due() is None and len(api.calls) == calls
    assert {name: (folder / name).read_bytes() for name in before} == before

    # Not cancelled: the final copy is made after the task has returned.
    api = install_docker(monkeypatch, FakeDockerApi({path: FakeFile(fixture_bytes("codex"))}))
    run = make_run(config, session_id=SESSION_IDS["codex"], run_id="6" * 32)
    schedule = CopySchedule(run, FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    api.files[path] = FakeFile(fixture_bytes("codex") + b'{"type":"conclude"}\n', mtime="after the conclude")
    run.exited_monotonic = START + 1400
    clock.at(1700)
    schedule.final_copy()
    meta = read_meta(config, run_id="6" * 32)
    assert (meta["final"], meta["copies"], meta["copied_elapsed_seconds"]) == (True, 2, 1700.0)
    assert read_transcript(config, run_id="6" * 32).endswith(b'{"type":"conclude"}\n')


def test_copy_log_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Spec 8.11: the first copy logs repair shadow transcript copied at INFO and later ones at DEBUG; a failure logs
    repair shadow transcript copy failed once per run and reason; none holds a path, an id or a value."""
    clock = FakeClock(monkeypatch)
    config = copy_config(tmp_path, "codex")
    api, path = codex_api()
    install_docker(monkeypatch, api)
    schedule = CopySchedule(make_run(config, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    tick_at(schedule, clock, 1260)
    api.files[path] = FakeFile(fixture_bytes("codex") + b"\n", mtime="2")
    tick_at(schedule, clock, 1275)
    del api.files[path]
    tick_at(schedule, clock, 1290)
    tick_at(schedule, clock, 1305)
    copied = [(record.levelno, record.args) for record in caplog.records if record.msg == COPIED]
    size = len(fixture_bytes("codex"))
    assert copied == [
        (logging.INFO, ("proj_027", "i014", RUN_ID, size, 0, 1)),
        (logging.DEBUG, ("proj_027", "i014", RUN_ID, size + 1, 0, 2)),
    ]
    assert copy_lines(caplog, COPY_FAILED) == [("proj_027", "i014", RUN_ID, "not_found")]
    assert SESSION_IDS["codex"] not in all_log_text(caplog)
