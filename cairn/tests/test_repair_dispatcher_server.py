"""Dispatcher tests of spec 16 against the real server: DS9 (= XC1) and DS25.

The real Cairn app runs under uvicorn on 127.0.0.1 and a free port, started as `cairn serve` in its own process with
both repair tokens in its environment, so that its lifespan reads them as on a real deployment. The dispatcher side
runs in this process: run_explore_task_with_repair with a fake codex process (DS9) or a fake codex CLI in local
mode (DS25). These tests need the server's repair routes (build steps 1-3); until they are in this tree they skip.
Every value here is synthetic.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cairn.server.routers.repair", reason="server repair routes are not in this tree yet")

import requests
from repair_dispatcher_runs import (
    CODEX_ID,
    OK_JSON,
    RunCapture,
    ScriptedBackend,
    make_token_file,
    run_config,
    session_lines,
)
from repair_dispatcher_support import FAKE_TOKEN, bypass_proxies, reset_repair_process_state

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import session_ids, shadow, transcripts
from cairn.dispatcher.repair.explore import run_explore_task_with_repair
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.tasks import common

CAIRN_SRC = Path(__file__).resolve().parents[1] / "src"
AGENT_TOKEN = "test-agent-token-not-a-secret-00000004"  # noqa: S105 - fake; 38 printable ASCII characters
STARTED_LINE = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:\d+)")


@dataclass
class RealServer:
    base_url: str
    db_path: Path
    process: subprocess.Popen[str]
    output: list[str]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(session_ids, "PROBE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(shadow, "RETRY_DELAY_SECONDS", 0.05)
    bypass_proxies(monkeypatch)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[RealServer]:
    """`cairn serve` on 127.0.0.1 and a free port, with both repair tokens configured."""
    home = tmp_path / "server-home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CAIRN_REPAIR_") and key != "FORWARDED_ALLOW_IPS"
    }
    env.update(
        HOME=str(home),
        PYTHONPATH=str(CAIRN_SRC),
        PYTHONUNBUFFERED="1",
        CAIRN_REPAIR_AGENT_TOKEN=AGENT_TOKEN,
        CAIRN_REPAIR_DISPATCHER_TOKEN=FAKE_TOKEN,
    )
    db_path = tmp_path / "cairn.db"
    command = [sys.executable, "-c", "from cairn.cli import main; main()", "serve", "--host", "127.0.0.1"]
    command += ["--port", "0", "--db-path", str(db_path), "--no-access-log"]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output: list[str] = []
    found: list[str] = []
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

    reader = threading.Thread(target=read, name="cairn-serve-output", daemon=True)
    reader.start()
    try:
        assert ready.wait(30) and found, "".join(output)
        yield RealServer(found[0], db_path, process, output)
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        reader.join(5)


@pytest.fixture
def runs(server: RealServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunCapture]:
    capture = RunCapture(monkeypatch)
    yield capture
    capture.finish()


class CallRecorder:
    """Every register and end call of the reporter, with the body sent and the server's answer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        for name in ("register", "end"):
            real = getattr(repair_client.RepairRunsClient, name)

            def record(self_: Any, run_id: str, body: dict[str, Any], _real: Any = real, _name: str = name) -> Any:
                answer = _real(self_, run_id, body)
                self.calls.append((_name, json.loads(json.dumps(body)), answer.status))
                return answer

            monkeypatch.setattr(repair_client.RepairRunsClient, name, record)


def switched_on_claimed_intent(base_url: str, worker: str) -> tuple[str, str]:
    """A project with its Repair switch on and one explore intent, claimed by worker."""
    http = requests.Session()
    response = http.post(f"{base_url}/projects", json={"title": "shadow", "origin": "start", "goal": "finish"})
    assert response.status_code == 201, response.text
    project_id = response.json()["project"]["id"]
    response = http.put(f"{base_url}/projects/{project_id}/repair", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["repair_enabled"] is True
    response = http.post(
        f"{base_url}/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201, response.text
    intent_id = response.json()["id"]
    response = http.post(f"{base_url}/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": worker})
    assert response.status_code == 200, response.text
    http.close()
    return project_id, intent_id


def stored_row(db_path: Path, run_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM repair_runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def server_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def run_view(base_url: str, run_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{base_url}/repair/runs/{run_id}", headers={"X-Cairn-Repair-Token": AGENT_TOKEN}, timeout=10
    )
    assert response.status_code == 200, response.text
    return response.json()


# DS9 = XC1 -----------------------------------------------------------------------------------------------------------


def test_dispatcher_registers_against_the_real_server(
    server: RealServer, runs: RunCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS9. A switched-on, claimed intent run through run_explore_task_with_repair with a fake codex process: the
    stored row has the register's fields, the session id filled by the session call, the exit fields of the first
    /end, the outcome and fact of the second, and last_alive_at at the process's exit.
    """
    project_id, intent_id = switched_on_claimed_intent(server.base_url, "codex-1")
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
    config = run_config(
        "codex", server.base_url, make_token_file(tmp_path), tmp_path / "copies", workers=workers, timeout=1800
    )
    recorder = CallRecorder(monkeypatch)
    client = CairnClient(server.base_url)
    try:
        project = client.get_project(project_id)
        assert getattr(project.project, "repair_enabled", None) is True
        [intent] = [item for item in project.intents if item.id == intent_id]
        export_yaml = client.export_project(project_id)
        chunks = [*session_lines("codex"), (1.5, "out", OK_JSON)]
        backend = ScriptedBackend([{"chunks": chunks}])
        outcome = run_explore_task_with_repair(
            config, client, backend, project, export_yaml, intent, config.workers[0], TaskCancellation()
        )
        assert outcome == "success"
        runs.wait()
        [concluded] = [item for item in client.get_project(project_id).intents if item.id == intent_id]
    finally:
        client.close()
    [run] = runs.observed
    assert [(name, status) for name, _body, status in recorder.calls] == [
        ("register", 201),
        ("register", 200),
        ("end", 200),
        ("end", 200),
    ]
    register, session, exit_end, outcome_end = (body for _name, body, _status in recorder.calls)
    assert register["session_id"] is None and session == {**register, "session_id": CODEX_ID}

    row = stored_row(server.db_path, run.run_id)
    assert {key: row[key] for key in ("project_id", "intent_id", "worker", "cli_type", "execution")} == {
        "project_id": project_id,
        "intent_id": intent_id,
        "worker": "codex-1",
        "cli_type": "codex",
        "execution": "container",
    }
    assert row["budget_seconds"] == 1800
    assert json.loads(row["locator_json"]) == register["locator"]
    assert row["session_id"] == CODEX_ID
    assert (row["version"], row["slices_used"], row["state"]) == (1, 0, "ended")
    assert row["end_reason"] == "dispatcher"
    assert json.loads(row["outcome_json"]) == {"timed_out": False, "cancelled": False, "returncode": 0}
    assert exit_end == {
        "timed_out": False,
        "cancelled": False,
        "returncode": 0,
        "exited_seconds_ago": exit_end["exited_seconds_ago"],
    }
    assert row["outcome"] == "success" and outcome_end == {"outcome": "success"}
    assert concluded.to is not None and row["fact_id"] == concluded.to
    started, alive, ended = (server_time(row[key]) for key in ("started_at", "last_alive_at", "ended_at"))
    assert started <= alive <= ended
    assert 0 <= (alive - started).total_seconds() <= 5  # the process exited about 1.5 s after its start
    assert (server_time(row["deadline_at"]) - started).total_seconds() == 1800

    view = run_view(server.base_url, run.run_id)
    assert view["session_id"] == CODEX_ID and view["outcome"] == "success" and view["fact_id"] == row["fact_id"]
    assert view["last_alive_at"] == row["last_alive_at"] and view["end_reason"] == "dispatcher"


# DS25 ----------------------------------------------------------------------------------------------------------------


def write_slow_codex(bin_dir: Path) -> None:
    """A codex on PATH whose execute writes its session id to stderr (under 64 KiB, so today's 64 KiB reader delivers
    it only at exit) and then runs on; its conclude (`codex exec resume <id> ...`) answers at once.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if sys.argv[1:3] == ['exec', 'resume']:\n"
        f"    print({OK_JSON!r})\n"
        "    raise SystemExit(0)\n"
        f"sys.stderr.write('OpenAI Codex v0.118.0\\nsession id: {CODEX_ID}\\n'); sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_a_timed_out_local_run_records_its_late_session_id_and_both_ends(
    server: RealServer, runs: RunCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS25. Local mode, a fake codex that runs past a 2 s budget: its id reaches the dispatcher only at exit. The
    session call, the accepted register body plus session_id, is answered 200; both /end calls arrive; the stored
    row has the session id, timed_out true and the outcome.
    """
    write_slow_codex(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))
    project_id, intent_id = switched_on_claimed_intent(server.base_url, "codex-1")
    workers = [{"name": "codex-1", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 0}]
    config = run_config(
        "codex",
        server.base_url,
        make_token_file(tmp_path),
        tmp_path / "copies",
        execution="local",
        workers=workers,
        timeout=2,
        conclude_timeout=30,
    )
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    recorder = CallRecorder(monkeypatch)
    client = CairnClient(server.base_url)
    try:
        project = client.get_project(project_id)
        [intent] = [item for item in project.intents if item.id == intent_id]
        started = time.monotonic()
        outcome = run_explore_task_with_repair(
            config,
            client,
            backend,
            project,
            client.export_project(project_id),
            intent,
            config.workers[0],
            TaskCancellation(),
        )
        assert outcome == "success"  # the conclude fallback turned the timed-out run into a fact
        assert time.monotonic() - started >= 2
        runs.wait(30)
    finally:
        client.close()
    [run] = runs.observed
    assert [(name, status) for name, _body, status in recorder.calls] == [
        ("register", 201),
        ("register", 200),
        ("end", 200),
        ("end", 200),
    ]
    register, session, exit_end, outcome_end = (body for _name, body, _status in recorder.calls)
    assert register["session_id"] is None and register["budget_seconds"] == 2
    assert session == {**register, "session_id": CODEX_ID}  # the accepted body: started_seconds_ago not recomputed
    assert exit_end["timed_out"] is True and exit_end["cancelled"] is False
    assert outcome_end == {"outcome": "success"}
    row = stored_row(server.db_path, run.run_id)
    assert row["session_id"] == CODEX_ID
    assert json.loads(row["outcome_json"])["timed_out"] is True
    assert row["outcome"] == "success"
    assert row["ended_at"] is not None
