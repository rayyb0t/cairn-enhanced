"""XC5 of spec §16, `smoke_shadow`: one switched-on explore run, end to end, on real processes and every part's own code.

- The server: `cairn serve` on a free loopback port, with both role tokens read from files at its start (§13 step 2).
- The dispatcher: its repair module, started from a dispatch.yaml whose repair: section lists one reviewer, through the
  scheduler's own touch points: `_start_repair_side`, whose housekeeping thread writes the agent's settings file, and
  `_explore_task_for`, which gives the switched-on project `run_explore_task_with_repair`. That task runs in local mode
  with a fake `claude` on PATH, which writes a session file in Claude Code 2.1.98's format under a temporary
  CLAUDE_CONFIG_DIR and sleeps; the budget is 360 s, so the window opens at 270 s and the copies start at 252 s.
- The agent: `cairn-repair` in its own process, which reads the settings file the dispatcher wrote.
- The reviewer: a stub on loopback, once OpenAI-compatible and once the Claude Messages API (owner decision O8); never a
  real provider. Its key is written in dispatch.yaml with spaces around it, which the dispatcher removes.

It checks the whole path: register, copy under <root>/<project_id>/<run_id>/, review, issue, /end, outcome. Marked
`slow` and opt-in: `uv run pytest -m slow tests/cross_component_smoke` (about five minutes per format). Every value is
synthetic: made-up tokens and keys, `test-model`, loopback addresses only.
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from agentkit import ModelStub, Reply, chat_answer, decision_text, messages_answer, text_block

from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import config as repair_config
from cairn.dispatcher.repair import explore as repair_explore
from cairn.dispatcher.repair import housekeeping, redaction, transcripts
from cairn.dispatcher.repair.explore import run_explore_task_with_repair
from cairn.dispatcher.repair.shadow import ShadowRun
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.scheduler import loop as scheduler_loop
from cairn.dispatcher.tasks import common
from cairn.server.repair_auth import AUTH_HEADER

pytestmark = pytest.mark.slow

# Spec §16 XC5's budget, just above the 350 s floor below which no run is reviewed (§5.4; owner decision O4, Q5):
# the window is 270-288 s (75-80% of the budget), and the copies start at 252 s (70%).
BUDGET = 360
WORKER = "claudecode-1"
REVIEWER = "reviewer_stub"
AGENT_TOKEN = "test-smoke-agent-token-not-a-secret-0001"  # noqa: S105 - fake; printable ASCII, 32+ characters
DISPATCHER_TOKEN = "test-smoke-dispatcher-token-not-a-secret-02"  # noqa: S105 - fake
REVIEWER_KEY = "test-smoke-reviewer-key-not-a-secret-0003"
SECRETS = (AGENT_TOKEN, DISPATCHER_TOKEN, REVIEWER_KEY)
LONG_COMMAND = "john --wordlist=wordlist.txt rootfs/etc/shadow"
FACT = "Two local accounts were cracked offline from the extracted shadow file."
STARTED_LINE = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:\d+)")

# The fake worker CLI: `claude --session-id <id> ... -p -- <prompt>` for the execute, `claude -r <id> ...` for the
# conclude fallback. The execute writes its session file as Claude Code 2.1.98 does, at
# <CLAUDE_CONFIG_DIR>/projects/<slug of its physical cwd>/<id>.jsonl: the prompt (which holds the run's prompt marker)
# and a long tool call, then sleeps until the test lets it finish (or MAX_SLEEP), appends the call's result and the
# final answer, and prints that answer.
FAKE_CLAUDE = r"""
import json, os, re, sys, time
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

args = sys.argv[1:]
if args[:1] == ["--help"]:
    raise SystemExit(0)
if args[:1] == ["-r"]:
    print(ANSWER)
    raise SystemExit(0)
session = args[args.index("--session-id") + 1]
prompt = args[args.index("--") + 1]
cwd = os.path.realpath(os.getcwd())
folder = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", re.sub(r"[^a-zA-Z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, session + ".jsonl")

def write(record):
    record = {"timestamp": now(), "sessionId": session, "cwd": cwd, **record}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

write({"type": "user", "message": {"role": "user", "content": prompt}})
call = {"type": "tool_use", "id": "toolu_smoke_0", "name": "Bash", "input": {"command": COMMAND}}
write({"type": "assistant", "message": {"role": "assistant", "model": "test-model", "content": [call]}})
deadline = time.monotonic() + MAX_SLEEP
while not os.path.exists(GO) and time.monotonic() < deadline:
    time.sleep(0.5)
result = {"tool_use_id": "toolu_smoke_0", "type": "tool_result", "content": "2 password hashes cracked", "is_error": False}
done = {"stdout": "2 password hashes cracked", "stderr": "", "interrupted": False, "isImage": False}
write({"type": "user", "message": {"role": "user", "content": [result]}, "toolUseResult": done})
write({"type": "assistant", "message": {"role": "assistant", "model": "test-model", "content": [{"type": "text", "text": ANSWER}]}})
print(ANSWER)
"""


@dataclass
class Served:
    url: str
    db_path: Path
    output: list[str]


@contextmanager
def cairn_serve(base: Path) -> Iterator[Served]:
    """`cairn serve` on 127.0.0.1 and a free port, with both repair tokens read from files at its start."""
    secrets = base / "secrets"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CAIRN_REPAIR_") and key != "FORWARDED_ALLOW_IPS"
    }
    env.update(
        HOME=str(base / "server-home"),
        PYTHONUNBUFFERED="1",
        CAIRN_REPAIR_AGENT_TOKEN_FILE=str(secrets / "repair-agent.token"),
        CAIRN_REPAIR_DISPATCHER_TOKEN_FILE=str(secrets / "repair-dispatcher.token"),
    )
    db_path = base / "cairn.db"
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
        assert ready.wait(60) and found, "".join(output)
        yield Served(found[0], db_path, output)
    finally:
        process.terminate()
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(15)
        reader.join(5)


def write_secret(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def reviewer_stub(api_format: str) -> ModelStub:
    """The reviewer: it answers detach, citing the call that the digest lists first under RUNNING AT THE CUT."""

    def answer(payload: bytes) -> Reply:
        request = json.loads(payload)
        digest = request["messages"][-1]["content"]
        running = re.search(r"^RUNNING AT THE CUT:\n(E\d+) ", digest, re.MULTILINE)
        cited = running.group(1) if running is not None else "E0"
        text = decision_text(
            "detach",
            "An offline password cracker is still working through its wordlist.",
            (f"{cited} john runs offline at the cut",),
            "Let john finish the wordlist and report the cracked accounts.",
        )
        if api_format == "anthropic_messages":
            return Reply(body=messages_answer([text_block(text)]))
        return Reply(body=chat_answer(content=text))

    return ModelStub(route=answer)


def wait_until(condition: Callable[[], Any], timeout: float, what: str, diagnostics: Callable[[], str]) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(1.0)
    pytest.fail(f"timed out waiting for {what}\n{diagnostics()}")


def stored(db_path: Path, query: str, *args: Any) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, args).fetchall()]
    finally:
        connection.close()


@pytest.mark.parametrize("api_format", ["openai_chat", "anthropic_messages"])
def test_smoke_shadow(
    api_format: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """XC5. Local mode, a fake claude that writes a claudecode-format session file under a temporary
    CLAUDE_CONFIG_DIR and sleeps, a 360 s budget, the real server, the real dispatcher repair module started from a
    dispatch.yaml whose repair: section lists one reviewer on a stub (OpenAI-compatible, then Claude Messages), and the
    agent, which reads the settings file the dispatcher wrote: register, copy under <root>/<project_id>/<run_id>/,
    review, issue, /end, outcome."""
    caplog.set_level("INFO", logger="cairn")
    # The dispatcher's once-per-process state, fresh; its own requests go straight to the loopback servers.
    monkeypatch.setattr(repair_config, "_LOADER", repair_config._Loader())
    monkeypatch.setattr(repair_client, "_SERVER_NOT_CONFIGURED", repair_client._Once())
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    monkeypatch.setattr(housekeeping, "_STOP", threading.Event())
    monkeypatch.setattr(redaction, "_oversized_logged", set())
    monkeypatch.setattr(scheduler_loop, "_repair_module_failed", False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no real CLI's credential file is read for the redaction
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))  # the run's prompt marker lives here
    go = tmp_path / "let-the-worker-finish"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        f"#!{sys.executable}\nANSWER = {json.dumps({'accepted': True, 'data': {'description': FACT}})!r}\n"
        f"COMMAND = {LONG_COMMAND!r}\nGO = {str(go)!r}\nMAX_SLEEP = {BUDGET - 30}\n{FAKE_CLAUDE}",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    write_secret(tmp_path / "secrets" / "repair-agent.token", AGENT_TOKEN)
    dispatcher_token = write_secret(tmp_path / "secrets" / "repair-dispatcher.token", DISPATCHER_TOKEN)
    root = tmp_path / "transcripts"
    runs: list[ShadowRun] = []
    real_new_shadow_run = repair_explore._new_shadow_run

    def capture(*args: Any, **kwargs: Any) -> ShadowRun | None:
        run = real_new_shadow_run(*args, **kwargs)
        if run is not None:
            runs.append(run)
        return run

    monkeypatch.setattr(repair_explore, "_new_shadow_run", capture)

    stub = reviewer_stub(api_format)
    agent: subprocess.Popen[str] | None = None
    task: threading.Thread | None = None
    agent_log = tmp_path / "agent.log"
    try:
        with cairn_serve(tmp_path) as server:
            # dispatch.yaml, with repair: after server: and before workers:, and the reviewer's key written with spaces
            # around it, as a quoted YAML value can hold it.
            reviewer: dict[str, Any] = {"name": REVIEWER}
            if api_format == "anthropic_messages":
                reviewer["api_format"] = "anthropic_messages"
            base_url = stub.url if api_format == "anthropic_messages" else stub.url + "/v1"
            reviewer["env"] = {
                "REVIEW_MODEL": "test-model",
                "REVIEW_API_KEY": f"  {REVIEWER_KEY}  ",
                "REVIEW_BASE_URL": base_url,
            }
            dispatch = {
                "server": server.url,
                "repair": {"token_file": str(dispatcher_token), "transcript_root": str(root), "reviewers": [reviewer]},
                "runtime": {
                    "execution": "local",
                    "worker_healthcheck": "disabled",
                    "max_workers": 1,
                    "max_running_projects": 1,
                    "max_project_workers": 1,
                    "interval": 3,
                    "healthcheck_timeout": 15,
                    "prompt_group": "default",
                },
                "tasks": {
                    "bootstrap": {"timeout": 120, "conclude_timeout": 30},
                    "reason": {"timeout": 45},
                    "explore": {"timeout": BUDGET, "conclude_timeout": 30},
                },
                "local": {"workspace_root": str(tmp_path / "work"), "completed_action": "keep"},
                "workers": [
                    {
                        "name": WORKER,
                        "type": "claudecode",
                        "task_types": ["explore"],
                        "max_running": 1,
                        "priority": 0,
                        "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config")},
                    }
                ],
            }
            dispatch_path = tmp_path / "dispatch.yaml"
            dispatch_path.write_text(yaml.safe_dump(dispatch, sort_keys=False), encoding="utf-8")
            dispatch_path.chmod(0o600)
            config = DispatchConfig.load(dispatch_path)

            # The dispatcher's repair side starts as DispatcherLoop.run starts it, and writes the agent's settings.
            scheduler_loop._start_repair_side(config)
            settings_path = root / "agent-settings.json"
            wait_until(settings_path.is_file, 30, "the agent's settings file", lambda: caplog.text)
            settings = json.loads(settings_path.read_bytes())
            assert settings["server"] == server.url and settings["projects"] is None
            assert settings["reviewers"] == [
                {
                    "name": REVIEWER,
                    "model": "test-model",
                    "base_url": base_url,
                    "api_key": REVIEWER_KEY,
                    "api_format": api_format,
                }
            ]

            # The agent, as cairn-repair runs, on its own repair.yaml and the dispatcher's settings file.
            repair_yaml = tmp_path / "repair.yaml"
            repair_yaml.write_text(
                yaml.safe_dump(
                    {
                        "token_file": str(tmp_path / "secrets" / "repair-agent.token"),
                        "dispatch_config_to_check": str(dispatch_path),
                        "poll_interval": 2,
                        "transcripts": {"root": str(root)},
                    }
                ),
                encoding="utf-8",
            )
            repair_yaml.chmod(0o600)
            entry_point = Path(sys.executable).with_name("cairn-repair")
            assert entry_point.is_file(), entry_point
            agent_env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_REPAIR_")}
            agent_env["PYTHONUNBUFFERED"] = "1"
            with agent_log.open("w", encoding="utf-8") as log_file:
                agent = subprocess.Popen(
                    [str(entry_point), "--config", str(repair_yaml), "--allow-shared-user", "--log-level", "INFO"],
                    env=agent_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

            def agent_text() -> str:
                return agent_log.read_text(encoding="utf-8", errors="replace")

            def diagnostics() -> str:
                return f"--- agent\n{agent_text()[-4000:]}\n--- server\n{''.join(server.output)[-4000:]}"

            started = wait_until(
                lambda: re.search(r"repair agent started (.*)", agent_text()), 60, "the agent's start", diagnostics
            )
            assert agent.poll() is None, diagnostics()
            assert f"reviewer={REVIEWER} model=test-model endpoint={stub.url} " in started.group(1)
            assert f"projects=all transcripts={root} dry_run=false" in started.group(1)

            # A project switched on with the Repair switch (§13 step 5), and an explore intent its worker has claimed.
            http = requests.Session()
            http.trust_env = False
            created = http.post(
                f"{server.url}/projects",
                json={"title": "smoke", "origin": "a firmware image", "goal": "local accounts"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["project"]["id"]
            switched = http.put(f"{server.url}/projects/{project_id}/repair", json={"enabled": True})
            assert switched.status_code == 200 and switched.json()["repair_enabled"] is True, switched.text
            intent_body = {
                "from": ["origin"],
                "description": "Recover the device's local accounts offline.",
                "creator": "reasoner",
                "worker": None,
            }
            created = http.post(f"{server.url}/projects/{project_id}/intents", json=intent_body)
            assert created.status_code == 201, created.text
            intent_id = created.json()["id"]
            claimed = http.post(
                f"{server.url}/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": WORKER}
            )
            assert claimed.status_code == 200, claimed.text

            # The scheduler's touch point picks the repair task for the switched-on project, which then runs.
            client = CairnClient(server.url)
            project = client.get_project(project_id)
            [intent] = [item for item in project.intents if item.id == intent_id]
            explore = scheduler_loop._explore_task_for(project)
            assert explore is run_explore_task_with_repair
            outcome: list[str] = []
            export_yaml = client.export_project(project_id)
            backend = LocalBackend(config.local or LocalConfig())

            def run_task() -> None:
                cancellation = TaskCancellation()
                outcome.append(
                    explore(config, client, backend, project, export_yaml, intent, config.workers[0], cancellation)
                )

            task = threading.Thread(target=run_task, name="explore-task", daemon=True)
            task.start()
            [run] = wait_until(lambda: runs, 30, "the run's ShadowRun", diagnostics)

            # The review: the agent posts it in the window, and the server records it as the intent's issue.
            def reviewed() -> list[dict[str, Any]]:
                answer = http.get(
                    f"{server.url}/repair/runs/{run.run_id}", headers={AUTH_HEADER: AGENT_TOKEN}, timeout=10
                )
                return answer.json()["reviews"] if answer.status_code == 200 else []

            wait_until(reviewed, BUDGET, "the agent's review", diagnostics)
            go.touch()  # the worker may finish now
            task.join(120)
            assert not task.is_alive(), diagnostics()
            assert run.thread is not None
            run.thread.join(60)
            assert not run.thread.is_alive(), diagnostics()
            client.close()

            assert outcome == ["success"]
            # Register, /end and outcome: the stored run.
            [row] = stored(server.db_path, "SELECT * FROM repair_runs WHERE run_id = ?", run.run_id)
            assert (row["project_id"], row["intent_id"], row["worker"]) == (project_id, intent_id, WORKER)
            assert (row["cli_type"], row["execution"], row["budget_seconds"]) == ("claudecode", "local", BUDGET)
            assert row["session_id"] == run.session_id and run.session_id is not None
            assert (row["state"], row["end_reason"], row["outcome"]) == ("ended", "dispatcher", "success")
            assert json.loads(row["outcome_json"]) == {"timed_out": False, "cancelled": False, "returncode": 0}
            project_answer = http.get(f"{server.url}/projects/{project_id}").json()
            [answered] = [item for item in project_answer["intents"] if item["id"] == intent_id]
            assert answered["to"] is not None and row["fact_id"] == answered["to"]
            assert [fact["description"] for fact in project_answer["facts"] if fact["id"] == answered["to"]] == [FACT]

            # The copy, under <root>/<project_id>/<run_id>/: found at Claude Code's own path, and its final copy.
            folder = root / project_id / run.run_id
            meta = json.loads((folder / "meta.json").read_bytes())
            assert (meta["status"], meta["located_by"], meta["final"]) == ("ok", "path", True)
            assert (meta["project_id"], meta["run_id"], meta["cli_type"]) == (project_id, run.run_id, "claudecode")
            assert meta["copies"] >= 2 and meta["copied_elapsed_seconds"] >= 0.70 * BUDGET
            session_file = Path(meta["source_path"])
            assert session_file.parent.parent == tmp_path / "claude-config" / "projects"
            assert (folder / "transcript.jsonl").read_bytes() == session_file.read_bytes()

            # The review: the stub got one request in its format, with the stripped key in its format's header only.
            [request] = stub.requests
            body = json.loads(request.body)
            headers = {name.lower(): value for name, value in request.headers.items()}
            if api_format == "anthropic_messages":
                assert request.path == "/v1/messages"
                assert headers["x-api-key"] == REVIEWER_KEY and headers["anthropic-version"] == "2023-06-01"
                assert "authorization" not in headers
                assert sorted(body) == ["max_tokens", "messages", "model", "system"]
            else:
                assert request.path == "/v1/chat/completions"
                assert headers["authorization"] == f"Bearer {REVIEWER_KEY}"
                assert "x-api-key" not in headers and "anthropic-version" not in headers
            assert body["model"] == "test-model"
            digest = body["messages"][-1]["content"]
            assert f"run {run.run_id} | worker CLI claudecode | window 1 ({BUDGET} s)" in digest
            running = rf"^RUNNING AT THE CUT:\nE1 t\+\d\d:\d\d Bash \(running \d+ s\): {re.escape(LONG_COMMAND)}$"
            assert re.search(running, digest, re.MULTILINE), digest
            assert not any(secret in request.body.decode() for secret in SECRETS)

            [review_row] = stored(server.db_path, "SELECT * FROM repair_reviews WHERE run_id = ?", run.run_id)
            detail = json.loads(review_row["detail_json"])
            assert (review_row["source"], review_row["decision"], review_row["window_no"]) == ("agent", "detach", 1)
            assert (detail["fallback"], detail["model"], detail["applied"]) == (None, "test-model", False)
            assert detail["evidence"] == ["E1 john runs offline at the cut"]
            assert 0.70 * BUDGET <= detail["transcript_cut_seconds"] <= 0.75 * BUDGET
            assert review_row["digest"] == digest

            # The issue: a shadow repair_review on the intent, stamped with the run's end and its outcome.
            [issue] = [item for item in answered["issues"] if item["kind"] == "repair_review"]
            assert issue["run_id"] == run.run_id
            assert (issue["detail"]["decision"], issue["detail"]["mode"], issue["detail"]["applied"]) == (
                "detach",
                "shadow",
                False,
            )
            assert (issue["detail"]["end_reason"], issue["detail"]["outcome"]) == ("dispatcher", "success")
            http.close()

            # Both parts' logs: the agent posted its review; the dispatcher registered, copied, ended and reported.
            text = agent_text()
            assert f"repair review posted run={run.run_id} window=1 decision=detach fallback=- status=201" in text
            assert (
                f"repair model call run={run.run_id} reviewer={REVIEWER} model=test-model attempt=1 outcome=ok" in text
            )
            messages = [record.getMessage() for record in caplog.records if record.name.startswith("cairn.dispatcher")]
            for line in (
                f"repair shadow agent settings written path={settings_path} reviewers=1",
                f"repair shadow run registered project={project_id} intent={intent_id} worker={WORKER} "
                f"run={run.run_id} cli=claudecode budget={BUDGET}s session=yes",
                f"repair shadow outcome reported project={project_id} intent={intent_id} run={run.run_id} "
                "outcome=success",
            ):
                assert line in messages, line
            for secret in SECRETS:
                assert secret not in text and all(secret not in message for message in messages)
    finally:
        go.touch()
        if task is not None:
            task.join(60)
        housekeeping._STOP.set()
        for thread in threading.enumerate():
            if thread.name == housekeeping.THREAD_NAME:
                thread.join(10)
        if agent is not None:
            agent.terminate()
            with suppress(subprocess.TimeoutExpired):
                agent.wait(30)
            if agent.poll() is None:
                agent.kill()
                agent.wait(10)
        stub.close()
