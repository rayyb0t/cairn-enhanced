"""Dispatcher tests of spec 16 for the repair agent's settings file and the shared-key check: DS28 and DS29.

Build step 7: the housekeeping thread's steps 2 and 3 (repair/housekeeping.py, spec 8.3). DS28's last case runs a
switched-on project's explore against the real server, started as `cairn serve` the way
test_repair_dispatcher_server.py starts it, with the repair module's Docker client replaced by a fake. Every value
here is synthetic: the tokens and keys are obviously fake.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import FakeLease
from repair_dispatcher_runs import (
    CODEX_ID,
    OK_JSON,
    RunCapture,
    ScriptedBackend,
    run_config,
    session_lines,
)
from repair_dispatcher_support import (
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    bypass_proxies,
    repair_section,
    reset_repair_process_state,
    write_token,
)
from test_repair_transcripts_support import (
    FakeDockerApi,
    FakeFile,
    container_session_path,
    fixture_bytes,
    install_docker,
    read_meta,
    read_transcript,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair import housekeeping, redaction, session_ids, shadow, transcripts
from cairn.dispatcher.repair.config import load_state
from cairn.dispatcher.repair.explore import run_explore_task_with_repair
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.scheduler import loop as loop_module

WRITTEN = "repair shadow agent settings written path=%s reviewers=%s"
NOT_WRITTEN = "repair shadow agent settings not written path=%s error=%s"
SHARED = "repair reviewer shares a worker key reviewer=%s worker=%s"
HOUSEKEEPING_FAILED = "repair shadow housekeeping failed step=%s error=%s"

WORKER_CANARY = "canary-worker-env-openai-key-31d7"
COMMON_CANARY = "canary-common-env-secret-token-58aa"
TOKEN_CANARY = "canary-dispatcher-token-not-a-secret-000000000a"  # noqa: S105 - fake; 46 printable characters


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    monkeypatch.setattr(redaction, "_oversized_logged", set())
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    for record in caplog.records:
        text = record.getMessage()
        for secret in (FAKE_TOKEN, FAKE_REVIEWER_KEY, TOKEN_CANARY, WORKER_CANARY, COMMON_CANARY):
            assert secret not in text


def lines(caplog: pytest.LogCaptureFixture, fmt: str) -> list[tuple[Any, ...]]:
    return [tuple(record.args or ()) for record in caplog.records if record.msg == fmt]


def worker(name: str, cli: str = "codex", **env: str) -> dict[str, Any]:
    base = {
        "codex": {
            "CODEX_MODEL": "test-model",
            "CODEX_BASE_URL": "http://127.0.0.1:9",
            "OPENAI_API_KEY": "test-key-0001",
        },
        "pi": {
            "PI_MODEL": "test-model",
            "PI_BASE_URL": "http://127.0.0.1:9",
            "PI_API_KEY": "test-key-0002",
            "PI_PROVIDER_API": "openai-completions",
        },
    }[cli]
    return {
        "name": name,
        "type": cli,
        "task_types": ["explore"],
        "max_running": 1,
        "priority": 0,
        "env": {**base, **env},
    }


def settings_config(
    tmp_path: Path,
    *,
    token: str = FAKE_TOKEN,
    execution: str = "container",
    workers: list[dict[str, Any]] | None = None,
    common_env: dict[str, str] | None = None,
    root: Path | None = None,
    **section: Any,
) -> DispatchConfig:
    token_file = write_token(tmp_path / "secrets" / "repair-dispatcher.token", token)
    return run_config(
        "codex",
        "http://127.0.0.1:8000",
        token_file,
        root or tmp_path / "copies",
        execution=execution,
        workers=workers,
        common_env=common_env,
        **section,
    )


def write_settings(config: DispatchConfig) -> None:
    """The housekeeping thread's steps 1 to 3, as at a dispatcher start (a fresh process each time)."""
    state = load_state(config)
    housekeeping.check_shared_keys(config)
    housekeeping.write_agent_settings(config, state)


def settings_path(config: DispatchConfig) -> Path:
    assert config.repair is not None
    return Path(config.repair.transcript_root) / "agent-settings.json"


# DS28 ----------------------------------------------------------------------------------------------------------------


def _ds28_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """DS28. With spec 8.3's example, a worker env, a common_env and a token file each holding a distinctive canary,
    agent-settings.json parses to exactly version (first), server, projects and reviewers; each reviewer has name,
    model, base_url and api_key (null when left out) plus only the optional settings that are set and not null; no
    canary, nor the token_file path, is in its bytes. The file is 0640 with the root's group (as root, a root of
    another group). Without repair: nothing is written and no root is created.
    """
    section = repair_section()
    section["reviewers"].append(
        {
            "name": "reviewer_full",
            "env": {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "https://full.invalid/v1", "REVIEW_API_KEY": ""},
            "structured_output": False,
            "max_tokens": 4096,
            "context_window": 32768,
            "timeout_seconds": None,
            "max_attempts": 2,
            "ca_bundle": "/etc/ssl/certs/test-ca.pem",
            "proxy": "http://127.0.0.1:3128",
            "price": {"input_per_mtok": 1, "output_per_mtok": 2.5},
        }
    )
    config = settings_config(
        tmp_path,
        token=TOKEN_CANARY,
        workers=[worker("codex-1", OPENAI_API_KEY=WORKER_CANARY)],
        common_env={"COMMON_SECRET_TOKEN": COMMON_CANARY},
        reviewers=section["reviewers"],
    )
    root = tmp_path / "copies"
    if os.geteuid() == 0:
        root.mkdir(mode=0o750)
        os.chown(root, -1, 4242)  # a group of its own
    previous = os.umask(0o077)
    try:
        write_settings(config)
    finally:
        os.umask(previous)
    path = settings_path(config)
    raw = path.read_bytes()
    content = json.loads(raw)
    assert list(content) == ["version", "server", "projects", "reviewers"]
    assert content == {
        "version": 1,
        "server": "http://127.0.0.1:8000",
        "projects": None,
        "reviewers": [
            {"name": "reviewer_local", "model": "test-model", "base_url": "http://127.0.0.1:11434/v1", "api_key": None},
            {
                "name": "reviewer_provider",
                "model": "test-model",
                "base_url": "https://provider.invalid/v1",
                "api_key": FAKE_REVIEWER_KEY,
                "timeout_seconds": 180,
            },
            {
                "name": "reviewer_full",
                "model": "test-model",
                "base_url": "https://full.invalid/v1",
                "api_key": None,
                "structured_output": False,
                "max_tokens": 4096,
                "context_window": 32768,
                "max_attempts": 2,
                "ca_bundle": "/etc/ssl/certs/test-ca.pem",
                "proxy": "http://127.0.0.1:3128",
                "price": {"input_per_mtok": 1.0, "output_per_mtok": 2.5},
            },
        ],
    }
    assert config.repair is not None
    for forbidden in (
        WORKER_CANARY,
        COMMON_CANARY,
        TOKEN_CANARY,
        config.repair.token_file,
        "transcript_root",
        "workers",
    ):
        assert forbidden.encode() not in raw, forbidden
    info = os.stat(path)
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_gid == os.stat(root).st_gid
    if os.geteuid() == 0:
        assert info.st_gid == 4242
    assert lines(caplog, WRITTEN) == [(str(path), 3)]

    # projects as configured: a list stays a list.
    reset_repair_process_state(monkeypatch)
    listed = settings_config(tmp_path / "listed", projects=["proj_001"])
    write_settings(listed)
    assert json.loads(settings_path(listed).read_bytes())["projects"] == ["proj_001"]

    # Without repair:, nothing is written and the root is not created.
    reset_repair_process_state(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    without = run_config("codex", "http://127.0.0.1:9", tmp_path / "none.token", tmp_path / "unused", repair=False)
    before = sorted(str(item) for item in tmp_path.rglob("*"))
    housekeeping.write_agent_settings(without, None)
    loop_module._start_repair_side(without)
    assert sorted(str(item) for item in tmp_path.rglob("*")) == before


def _ds28_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """DS28. The file is written through agent-settings.json.tmp and os.replace: a reader polling during 200 rewrites
    never reads a partial or empty file, and a leftover .tmp is removed first. An agent-settings.json that is a
    symlink to a file outside the root is replaced and that file is unchanged; a root that is a symlink is refused
    with one "not written … error=unsafe_path" and nothing is written. A second start with a changed repair: replaces
    the content. A write that fails after the root is opened (agent-settings.json.tmp is a directory), with an
    earlier agent-settings.json present, logs "not written" once and leaves no agent-settings.json.
    """
    first = settings_config(tmp_path, projects=None)
    second = settings_config(tmp_path, projects=["proj_001", "proj_002"])
    write_settings(first)
    path = settings_path(first)
    expected = {path.read_bytes()}
    housekeeping.write_agent_settings(second, load_state(second))
    expected.add(path.read_bytes())
    assert len(expected) == 2

    seen: list[bytes] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                seen.append(path.read_bytes())
            except FileNotFoundError:
                seen.append(b"<missing>")

    reader = threading.Thread(target=poll, name="settings-reader", daemon=True)
    reader.start()
    try:
        for index in range(200):
            housekeeping.write_agent_settings(first if index % 2 else second, load_state(first))
    finally:
        stop.set()
        reader.join(5)
    assert seen and set(seen) <= expected, {item[:40] for item in set(seen) - expected}

    # A leftover .tmp is removed first.
    temporary = path.with_name("agent-settings.json.tmp")
    temporary.write_bytes(b'{"version": 1, "half')
    housekeeping.write_agent_settings(first, load_state(first))
    assert not temporary.exists() and json.loads(path.read_bytes())["projects"] is None

    # A symlink at agent-settings.json is replaced, never followed.
    outside = tmp_path / "outside.json"
    outside.write_text("keep me\n")
    path.unlink()
    path.symlink_to(outside)
    housekeeping.write_agent_settings(second, load_state(second))
    assert not path.is_symlink() and json.loads(path.read_bytes())["projects"] == ["proj_001", "proj_002"]
    assert outside.read_text() == "keep me\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640

    # A root that is a symlink is refused, and nothing is written there.
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(target, target_is_directory=True)
    linked = settings_config(tmp_path, root=linked_root)
    caplog.clear()
    housekeeping.write_agent_settings(linked, load_state(linked))
    assert lines(caplog, NOT_WRITTEN) == [(str(linked_root / "agent-settings.json"), "unsafe_path")]
    assert list(target.iterdir()) == []

    # A write that fails after the root was opened removes the earlier file, which dispatch.yaml may no longer match.
    caplog.clear()
    assert path.exists()
    temporary.mkdir()
    housekeeping.write_agent_settings(first, load_state(first))
    assert lines(caplog, NOT_WRITTEN) == [(str(path), "IsADirectoryError")]
    assert not path.exists() and temporary.is_dir()
    assert lines(caplog, WRITTEN) == []


def _ds29_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """DS28, DS29. A REVIEW_API_KEY equal to the dispatcher token: no shared-key line, no settings file (error
    reviewer_key_is_token), and an earlier settings file is removed; a bad token (not configured) never blocks the
    file."""
    clean = settings_config(tmp_path)
    write_settings(clean)
    path = settings_path(clean)
    assert path.exists()
    reviewers = repair_section()["reviewers"]
    reviewers[1]["env"]["REVIEW_API_KEY"] = FAKE_TOKEN
    shared = settings_config(tmp_path, reviewers=reviewers)
    caplog.clear()
    write_settings(shared)
    assert not path.exists()
    assert lines(caplog, NOT_WRITTEN) == [(str(path), "reviewer_key_is_token")]
    assert lines(caplog, SHARED) == []


# DS29 ----------------------------------------------------------------------------------------------------------------


def _ds29_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """DS29. One "repair reviewer shares a worker key reviewer=<name> worker=<names>" per reviewer whose key equals a
    worker's OPENAI_API_KEY (that worker), a common_env value (every worker, in dispatch.yaml order) or, in local
    mode, a variable of the dispatcher's environment (ending with environment); none for a key equal to the key in
    pi's auth.json of a temporary HOME, which is not opened at start, for a key no worker holds, or for a reviewer
    without a key. No record holds a key. The dispatcher's housekeeping starts in every case and writes the file.
    """
    w1_key = "test-shared-openai-key-0101"
    common_key = "test-shared-common-token-0202"
    env_key = "test-shared-environment-token-0303"
    pi_auth_key = "test-shared-pi-auth-key-0404"
    lonely_key = "test-reviewer-own-key-0505"
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    pi_auth = home / ".pi" / "agent" / "auth.json"
    pi_auth.write_text(json.dumps({"y": {"type": "api_key", "key": pi_auth_key}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CAIRN_TEST_SHARED_TOKEN", env_key)

    def reviewer(name: str, key: str | None) -> dict[str, Any]:
        env = {"REVIEW_MODEL": "test-model", "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1"}
        return {"name": name, "env": env if key is None else {**env, "REVIEW_API_KEY": key}}

    reviewers = [
        reviewer("r_worker", w1_key),
        reviewer("r_common", common_key),
        reviewer("r_environment", env_key),
        reviewer("r_pi_auth", pi_auth_key),
        reviewer("r_own", lonely_key),
        reviewer("r_none", None),
    ]
    results: dict[str, list[tuple[Any, ...]]] = {}
    for execution in ("container", "local"):
        reset_repair_process_state(monkeypatch)
        monkeypatch.setattr(housekeeping, "_STOP", _StopAfter(1))
        folder = tmp_path / execution
        workers = [worker("w1", OPENAI_API_KEY=w1_key), worker("w2", "pi")]
        if execution == "local":
            workers = [{**item, "env": {}} for item in workers]
            workers[0]["env"] = {"OPENAI_API_KEY": w1_key}
        config = settings_config(
            folder,
            execution=execution,
            workers=workers,
            common_env={"SHARED_COMMON_TOKEN": common_key},
            reviewers=reviewers,
        )
        opened: list[str] = []
        real_open = os.open

        def spy(name: Any, *args: Any, _opened: list[str] = opened, _open: Any = real_open, **kwargs: Any) -> int:
            _opened.append(os.fsdecode(name))
            return _open(name, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy)
        caplog.clear()
        try:
            housekeeping._housekeeping(config)  # the thread's body: token, shared keys, settings, one removal pass
        finally:
            monkeypatch.setattr(os, "open", real_open)
        assert str(pi_auth) not in opened  # credential files are not read for this check
        assert lines(caplog, HOUSEKEEPING_FAILED) == []
        assert json.loads(settings_path(config).read_bytes())["reviewers"][0]["api_key"] == w1_key
        results[execution] = lines(caplog, SHARED)
    assert results["container"] == [("r_worker", "w1"), ("r_common", "w1,w2")]
    assert results["local"] == [("r_worker", "w1"), ("r_common", "w1,w2"), ("r_environment", "environment")]
    for record in caplog.records:
        for key in (w1_key, common_key, env_key, pi_auth_key, lonely_key):
            assert key not in record.getMessage()
    assert all(record.levelno == logging.WARNING for record in caplog.records if record.msg == SHARED)

    # A worker that holds the environment's value too: the worker first, then environment.
    reset_repair_process_state(monkeypatch)
    monkeypatch.setenv("SHARED_ENV_TOKEN", w1_key)
    config = settings_config(
        tmp_path / "both",
        execution="local",
        workers=[{**worker("w1"), "env": {"OPENAI_API_KEY": w1_key}}],
        reviewers=[reviewer("r_worker", w1_key)],
    )
    caplog.clear()
    housekeeping.check_shared_keys(config)
    assert lines(caplog, SHARED) == [("r_worker", "w1,environment")]


class _StopAfter:
    def __init__(self, passes: int) -> None:
        self.passes = passes
        self.waits = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        return self.waits >= self.passes


# DS28: projects narrows only the agent -------------------------------------------------------------------------------

CAIRN_SRC = Path(__file__).resolve().parents[1] / "src"
AGENT_TOKEN = "test-agent-token-not-a-secret-00000004"  # noqa: S105 - fake; 38 printable ASCII characters
STARTED_LINE = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:\d+)")


@pytest.fixture
def real_server(tmp_path: Path) -> Iterator[str]:
    """`cairn serve` on 127.0.0.1 and a free port, with both repair tokens configured (as DS9 starts it)."""
    pytest.importorskip("cairn.server.routers.repair", reason="server repair routes are not in this tree yet")
    home = tmp_path / "server-home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_REPAIR_")}
    env.update(
        HOME=str(home),
        PYTHONPATH=str(CAIRN_SRC),
        PYTHONUNBUFFERED="1",
        CAIRN_REPAIR_AGENT_TOKEN=AGENT_TOKEN,
        CAIRN_REPAIR_DISPATCHER_TOKEN=FAKE_TOKEN,
    )
    command = [sys.executable, "-c", "from cairn.cli import main; main()", "serve", "--host", "127.0.0.1"]
    command += ["--port", "0", "--db-path", str(tmp_path / "cairn.db"), "--no-access-log"]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    found: list[str] = []
    output: list[str] = []
    ready = threading.Event()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            match = STARTED_LINE.search(line)
            if match:
                found.append(match.group(1))
                ready.set()
        ready.set()

    thread = threading.Thread(target=read, name="cairn-serve-output", daemon=True)
    thread.start()
    try:
        assert ready.wait(30) and found, "".join(output)
        yield found[0]
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        thread.join(5)


def _ds28_projects(
    real_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DS28. With projects: [proj_001] in the repair: section, a switched-on proj_002 is still registered and copied
    (DS9's fake codex run, against the real server): the list only reaches the agent, through the settings file.
    """
    monkeypatch.setattr(session_ids, "PROBE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(shadow, "RETRY_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(transcripts, "COPY_START_FRACTION", 0.0)  # due while the 1.5 s run goes on
    monkeypatch.setattr(transcripts, "COPY_INTERVAL_SECONDS", 0.1)
    http = requests.Session()
    ids = []
    for title in ("first", "second"):
        response = http.post(f"{real_server}/projects", json={"title": title, "origin": "start", "goal": "finish"})
        assert response.status_code == 201, response.text
        ids.append(response.json()["project"]["id"])
    assert ids == ["proj_001", "proj_002"]
    assert http.put(f"{real_server}/projects/proj_002/repair", json={"enabled": True}).status_code == 200
    response = http.post(
        f"{real_server}/projects/proj_002/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_id = response.json()["id"]
    assert http.post(f"{real_server}/projects/proj_002/intents/{intent_id}/heartbeat", json={"worker": "w1"}).ok
    http.close()

    token_file = write_token(tmp_path / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
    config = run_config("codex", real_server, token_file, tmp_path / "copies", timeout=1800, projects=["proj_001"])
    write_settings(config)
    assert json.loads(settings_path(config).read_bytes())["projects"] == ["proj_001"]
    capture = RunCapture(monkeypatch)
    api = FakeDockerApi()

    def serve() -> None:
        observed = capture.observed
        if observed and observed[-1].prompt_marker:
            path = container_session_path("codex", CODEX_ID)
            api.files[path] = FakeFile(fixture_bytes("codex", observed[-1].prompt_marker), epoch=time.time())

    api.before = serve
    install_docker(monkeypatch, api)
    monkeypatch.setattr(HeartbeatLease, "for_intent", lambda *_args, **_kwargs: FakeLease())
    client = CairnClient(real_server)
    try:
        project = client.get_project("proj_002")
        assert project.project.repair_enabled is True
        [intent] = [item for item in project.intents if item.id == intent_id]
        backend = ScriptedBackend([{"chunks": [*session_lines("codex"), (1.5, "out", OK_JSON)]}])
        outcome = run_explore_task_with_repair(
            config,
            client,
            backend,
            project,
            client.export_project("proj_002"),
            intent,
            config.workers[0],
            TaskCancellation(),
        )
        assert outcome == "success"
        capture.wait()
    finally:
        client.close()
        capture.finish()
        api.close()
    [run] = capture.observed
    response = requests.get(
        f"{real_server}/repair/runs/{run.run_id}", headers={"X-Cairn-Repair-Token": AGENT_TOKEN}, timeout=10
    )
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "proj_002" and response.json()["session_id"] == CODEX_ID
    meta = read_meta(config, "proj_002", run.run_id)
    assert meta["status"] == "ok" and meta["copies"] >= 1
    assert read_transcript(config, "proj_002", run.run_id) == fixture_bytes("codex", run.prompt_marker or "")


@pytest.mark.parametrize("case", ["content", "atomic", "projects"])
def test_agent_settings_file_holds_only_what_the_agent_needs(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, request: Any
) -> None:
    """DS28, case by case: the content, mode and group (_ds28_content); the atomic replacement and the symlink
    refusals (_ds28_atomic); projects reaching only the agent, against the real server (_ds28_projects)."""
    if case == "content":
        _ds28_content(tmp_path, monkeypatch, caplog)
    elif case == "atomic":
        _ds28_atomic(tmp_path, monkeypatch, caplog)
    else:
        _ds28_projects(request.getfixturevalue("real_server"), tmp_path, monkeypatch, caplog)


@pytest.mark.parametrize("case", ["shared", "token"])
def test_a_reviewer_key_shared_with_a_worker_is_logged_by_name_only(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DS29, case by case: the shared-key lines (_ds29_shared), and a key equal to the dispatcher token (_ds29_token)."""
    if case == "shared":
        _ds29_shared(tmp_path, monkeypatch, caplog)
    else:
        _ds29_token(tmp_path, caplog)
