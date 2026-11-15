"""Dispatcher tests of spec 16 (DS ids) for the repair layer, Phase 2 (shadow mode).

Build step 5 adds four of them: DS12 (hostile session ids never reach a body or a log record), DS21 (the token
is read only from the configured file, once per process), DS22 (configuration errors are logged once per
process) and DS27 (the repair: section of dispatch.yaml is optional and strict). Step 6 adds the others. Unit
tests of the same modules that spec 16 does not number are in test_repair_dispatcher_{config,token,client,
session_ids}.py, and the helpers these files share are in repair_dispatcher_support.py. Every value here is
synthetic: the tokens and keys are obviously fake, and the ids are only names.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from conftest import make_config
from pydantic import ValidationError
from repair_dispatcher_support import (
    CONFIGURED_LINE,
    FAKE_REVIEWER_KEY,
    FAKE_TOKEN,
    IDS,
    OTHER_RUN_ID,
    READABLE_LINE,
    REGISTER_BODY,
    REJECTED_LINE,
    RUN_ID,
    SERVER_NOT_CONFIGURED_LINE,
    VALID_ID,
    FakeProcess,
    OpenSpy,
    bypass_proxies,
    dispatch_payload,
    fake_server,
    json_reply,
    register_body,
    repair_error,
    repair_lines,
    repair_records,
    repair_section,
    reset_repair_process_state,
    token_config,
    worker_config,
    write_dispatch_yaml,
    write_token,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import config as repair_config
from cairn.dispatcher.repair.client import Answer, RepairRunsClient
from cairn.dispatcher.repair.session_ids import (
    OutputSessionProbe,
    accept_session_id,
    argv_session_seed,
    valid_session_id,
)
from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, PiDriver


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    reset_repair_process_state(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")
    yield
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()


@pytest.fixture(autouse=True)
def direct_to_test_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake servers listen on 127.0.0.1: no proxy that the shell names may stand in between."""
    bypass_proxies(monkeypatch)


# DS12 ----------------------------------------------------------------------------------------------------------------

HOSTILE_IDS = [
    pytest.param("0199a1b2/../../etc/x", id="/"),
    pytest.param("0199a1b2..7c3d", id=".."),
    pytest.param("0199a1b2\nrepair shadow run ended project=proj_999 code=124 timed_out=True", id="\\n"),
    pytest.param("0199a1b2\rINJECTED", id="\\r"),
    pytest.param(VALID_ID + "\n", id="a valid id followed by \\n"),
    pytest.param(("0123456789abcdef" * 19)[:300], id="300 characters"),
]


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_session_ids_never_reach_a_body_or_a_log_record(hostile: str, caplog: pytest.LogCaptureFixture) -> None:
    """DS12. The rule refuses each id, and each place a session id comes from rejects it. The client, which every
    register and end body passes through, sends such an id as null whatever its caller passes: the run is
    registered without one (spec 8.7), and each rejection is one WARNING with the id's length and SHA-256 prefix.
    """
    assert not valid_session_id(hostile)
    if hostile.endswith("\n"):
        # re.match's $ also matches before a final newline, which is why every id rule is a full match.
        assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", hostile) is not None

    accepted: dict[str, str | None] = {}

    # Claude Code: the id is the argv's --session-id value (here a tampered one; the driver's own is a uuid4).
    argv = ClaudeCodeDriver().build_execute(worker_config("claudecode"), "explore the intent", hostile).argv
    assert argv_session_seed("claudecode", argv) == hostile
    accepted["claudecode"] = accept_session_id(argv_session_seed("claudecode", argv), **IDS)

    # pi: the first session event on stdout, which a login shell's ~/.profile can forge.
    event = json.dumps({"type": "session", "id": hostile}) + "\n"
    probe = OutputSessionProbe(PiDriver(), "pi", **IDS)
    accepted["pi"] = probe.probe(FakeProcess(stdout=[event, '{"type":"agent_start"}\n']))
    assert probe.rejected and not probe.active
    assert probe.final(event, "") is None  # the same id would be rejected again: the end-of-run parse is skipped

    # codex: the stderr header; its pattern captures only hex digits and dashes.
    header = f"session id: {hostile}\n"
    probe = OutputSessionProbe(CodexDriver(), "codex", **IDS)
    accepted["codex"] = probe.probe(FakeProcess(stderr=[header]))
    assert probe.final("", header) == accepted["codex"]  # the end-of-run parse gives the same answer

    assert accepted["claudecode"] is None
    assert accepted["pi"] is None
    for value in accepted.values():
        assert value is None or (valid_session_id(value) and value != hostile)

    # The client applies the rule last, to every body it sends. It is given the id itself first, as by a caller
    # that skipped the rule (a register call and a session call have this same body), and then the bodies built
    # from what the places above accepted.
    raw = {cli: register_body(cli, hostile) for cli in ("claudecode", "pi", "codex")}
    escaped = json.dumps(hostile)[1:-1]
    with fake_server(lambda _request: json_reply(201, {"created": True, "run": {}})) as server:
        client = RepairRunsClient(server.base_url, FAKE_TOKEN)
        try:
            for body in raw.values():
                assert client.register(RUN_ID, body).ok
                assert body["session_id"] == hostile  # the caller's body is left as it was
            for cli, value in accepted.items():
                assert client.register(RUN_ID, register_body(cli, value)).ok
        finally:
            client.close()
    sent = [json.loads(request.body) for request in server.requests]
    assert sent == [
        *({**body, "session_id": None} for body in raw.values()),
        *(register_body(cli, value) for cli, value in accepted.items()),
    ]
    for request in server.requests:
        text = request.body.decode()
        assert hostile not in text and escaped not in text
        assert (request.method, request.path) == ("PUT", f"/repair/runs/{RUN_ID}")

    # No log record holds it; each rejection is one WARNING with the length and the SHA-256 prefix only.
    length, digest = len(hostile), hashlib.sha256(hostile.encode()).hexdigest()[:12]
    expected_rejections = 2 + (accepted["codex"] is None) + len(raw)
    rejections = [record for record in repair_records(caplog) if record.msg == REJECTED_LINE]
    assert len(rejections) == expected_rejections
    for record in rejections:
        assert record.levelno == logging.WARNING
        assert record.args == ("proj_027", "i014", "worker-1", RUN_ID, length, digest)
    for record in caplog.records:
        message = record.getMessage()
        assert hostile not in message and escaped not in message
        assert "\n" not in message and "\r" not in message
        assert all(hostile not in str(arg) for arg in record.args or ())


# DS21 ----------------------------------------------------------------------------------------------------------------


def _load_in_threads(config: DispatchConfig, names: list[str]) -> list[repair_config.RepairState]:
    barrier = threading.Barrier(len(names))
    results: list[repair_config.RepairState | None] = [None] * len(names)

    def run(index: int) -> None:
        barrier.wait(5)
        results[index] = repair_config.load_state(config)

    threads = [threading.Thread(target=run, args=(index,), name=name) for index, name in enumerate(names)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert all(result is not None for result in results)
    return [result for result in results if result is not None]


def test_token_is_read_only_from_the_configured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = write_token(tmp_path / "secrets" / "repair-dispatcher.token", f"\n  {FAKE_TOKEN}  \n", 0o640)
    other_file = write_token(tmp_path / "other.token", "test-other-token-must-not-be-used-0006")
    # Variables a token could be read from on the server or the agent: the dispatcher never reads any of them.
    monkeypatch.setenv("CAIRN_REPAIR_DISPATCHER_TOKEN", "test-env-token-must-not-be-used-0007")
    monkeypatch.setenv("CAIRN_REPAIR_DISPATCHER_TOKEN_FILE", str(other_file))
    monkeypatch.setenv("CAIRN_REPAIR_AGENT_TOKEN", "test-env-token-must-not-be-used-0008")
    config = token_config(token_file)

    orders = {
        "housekeeping first": [["repair-housekeeping"], ["repair-shadow-proj_001-i001"]],
        "reporter first": [["repair-shadow-proj_001-i001"], ["repair-housekeeping"], ["repair-shadow-proj_001-i002"]],
        "all at once": [["repair-housekeeping", *(f"repair-shadow-proj_001-i00{n}" for n in range(1, 6))]],
    }
    for order, waves in orders.items():
        reset_repair_process_state(monkeypatch)
        caplog.clear()
        with monkeypatch.context() as patch:
            spy = OpenSpy(patch, token_file, read_delay=0.05)

            # The task thread asks first: no state yet, and nothing is opened.
            assert repair_config.cached_state(config) is None
            assert spy.openers == []

            states = [state for names in waves for state in _load_in_threads(config, names)]
        assert {(state.configured, state.reason, state.token) for state in states} == {(True, None, FAKE_TOKEN)}, order
        assert len(spy.openers) == 1, (order, spy.openers)
        assert spy.openers[0] == waves[0][0] or len(waves[0]) > 1, order
        assert spy.openers[0] != threading.current_thread().name, order
        assert repair_config.cached_state(config) is states[0]
        assert repair_lines(caplog) == [
            (logging.WARNING, READABLE_LINE, (str(token_file), "0640")),
            (logging.INFO, CONFIGURED_LINE, ("/var/lib/cairn-repair/transcripts", 2, "all")),
        ], order
        assert FAKE_TOKEN not in repr(states[0])


# DS22 ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        pytest.param(401, repair_error("unauthorized"), "unauthorized", id="401"),
        pytest.param(503, repair_error("token_not_configured"), "token_not_configured", id="503"),
        pytest.param(404, {"detail": "Not Found"}, "-", id="404 Not Found"),
    ],
)
def test_configuration_errors_are_logged_once_per_process(
    caplog: pytest.LogCaptureFixture, status: int, payload: dict[str, Any], code: str
) -> None:
    replies = {"current": json_reply(status, payload)}
    with fake_server(lambda _request: replies["current"]) as server:
        answers: list[Answer] = []

        def one_run(run_id: str) -> None:
            client = RepairRunsClient(server.base_url, FAKE_TOKEN)
            try:
                answers.append(client.register(run_id, REGISTER_BODY))
                answers.append(client.register(run_id, {**REGISTER_BODY, "session_id": "0199a1b2-7c3d-4e5f"}))
                answers.append(
                    client.end(
                        run_id, {"timed_out": False, "cancelled": False, "returncode": 0, "exited_seconds_ago": 0.2}
                    )
                )
                answers.append(client.end(run_id, {"outcome": "success"}))
            finally:
                client.close()

        runs = [threading.Thread(target=one_run, args=(run_id,)) for run_id in (RUN_ID, OTHER_RUN_ID)]
        for run in runs:
            run.start()
        for run in runs:
            run.join(30)
        # Another configuration error later in the same process is not logged again.
        replies["current"] = (
            json_reply(401, repair_error("unauthorized"))
            if status != 401
            else json_reply(503, repair_error("token_not_configured"))
        )
        one_run(RUN_ID)

    assert len(answers) == 12
    assert all(answer.kind == "not_configured" for answer in answers)
    lines = [(record.levelno, record.msg, record.args) for record in repair_records(caplog)]
    assert lines == [(logging.WARNING, SERVER_NOT_CONFIGURED_LINE, (status, code))]
    assert len(server.requests) == 12
    for request in server.requests:
        assert request.headers["x-cairn-repair-token"] == FAKE_TOKEN
        assert request.path in {
            f"/repair/runs/{run_id}{end}" for run_id in (RUN_ID, OTHER_RUN_ID) for end in ("", "/end")
        }


# DS27 ----------------------------------------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# Today's DispatchConfig, captured at 279d2df (dispatcher/config.py is the same at ee0f227) before the repair
# field was added: its fields, the digest of each committed example's parsed model, and the errors for invalid
# files (type, loc, msg), each from the payload that _todays_invalid_payloads builds under the same name.
TODAYS_FIELDS = {
    "server": ("<class 'str'>", True, "PydanticUndefined"),
    "runtime": ("<class 'cairn.dispatcher.config.RuntimeConfig'>", True, "PydanticUndefined"),
    "tasks": ("<class 'cairn.dispatcher.config.TasksConfig'>", True, "PydanticUndefined"),
    "container": ("cairn.dispatcher.config.ContainerConfig | None", False, "None"),
    "local": ("cairn.dispatcher.config.LocalConfig | None", False, "None"),
    "common_env": ("dict[str, str]", False, "PydanticUndefined"),
    "workers": ("list[cairn.dispatcher.config.WorkerConfig]", True, "PydanticUndefined"),
}

TODAYS_EXAMPLE_DIGESTS = {
    "dispatch.example.yaml": "ee53d20280c4b1666d37bc3edcff969d71d17b81913bd96c53894a80e34f4e29",
    "dispatch.local.example.yaml": "80269fe8c67be7dc0b9ae440bb3a938c8debd8e9bbba056320f289c19391465c",
    "dispatch_mock.yaml": "2c4ec867d836957e2fada6a8ae07717de31d495de30e2082f9c2b9cbf5007aa2",
}

TODAYS_ERRORS: dict[str, list[tuple[str, tuple[str | int, ...], str]]] = {
    "duplicate_worker_names": [("value_error", (), "Value error, worker names must be unique")],
    "empty_workers": [("value_error", (), "Value error, workers must not be empty")],
    "project_workers_exceed_max": [("value_error", (), "Value error, max_project_workers cannot exceed max_workers")],
    "container_block_missing": [
        ("value_error", (), "Value error, container config is required when runtime.execution is container")
    ],
    "container_worker_missing_env": [
        ("value_error", (), "Value error, worker test-worker missing env keys: CODEX_BASE_URL, OPENAI_API_KEY")
    ],
    "empty_task_types": [("value_error", ("workers", 0, "task_types"), "Value error, task_types must not be empty")],
    "duplicate_task_types": [("value_error", ("workers", 0, "task_types"), "Value error, task_types must be unique")],
    "pi_context_window_not_int": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker env PI_MODEL_CONTEXT_WINDOW must be an integer",
        )
    ],
    "pi_context_window_zero": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker env PI_MODEL_CONTEXT_WINDOW must be greater than 0",
        )
    ],
    "mock_unknown_env": [
        ("value_error", ("workers", 0), "Value error, worker test-worker has unsupported mock env keys: MOCK_UNKNOWN")
    ],
    "mock_phase_not_json": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON must be a JSON object")
    ],
    "mock_phase_not_object": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON must be a JSON object")
    ],
    "mock_delay_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.delay must be a two-element number array",
        )
    ],
    "mock_delay_negative": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON.delay[0] must be non-negative")
    ],
    "mock_delay_order": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.delay[1] must be greater than or equal to delay[0]",
        )
    ],
    "mock_delay_not_number": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON.delay[0] must be a number")
    ],
    "mock_outcomes_not_object": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON.outcomes must be an object")
    ],
    "mock_outcomes_unknown": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.outcomes has unsupported keys: bogus",
        )
    ],
    "mock_probability_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.outcomes.intent must be a decimal probability",
        )
    ],
    "mock_probability_range": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.outcomes.intent must be between 0 and 1",
        )
    ],
    "mock_probability_sum": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.outcomes probabilities must sum to 1.0, got 0.5",
        )
    ],
    "mock_rules_not_array": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON.rules must be an array")
    ],
    "mock_rule_not_object": [
        ("value_error", ("workers", 0), "Value error, worker test-worker MOCK_REASON.rules[0] must be an object")
    ],
    "mock_rule_force_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.rules[0].force must be one of: command_fail, complete, "
            "intent, invalid_json, invalid_payload, noop, rejected",
        )
    ],
    "mock_rule_gte_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.rules[0].fact_ids_gte must be a non-negative integer",
        )
    ],
    "mock_rule_lte_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.rules[0].fact_ids_lte must be a non-negative integer",
        )
    ],
    "mock_rule_empty_bad": [
        (
            "value_error",
            ("workers", 0),
            "Value error, worker test-worker MOCK_REASON.rules[0].open_intents_empty must be boolean",
        )
    ],
    "unknown_top_level_key": [("extra_forbidden", ("bogus",), "Extra inputs are not permitted")],
    "unknown_worker_key": [("extra_forbidden", ("workers", 0, "bogus"), "Extra inputs are not permitted")],
    "bad_worker_type": [
        ("literal_error", ("workers", 0, "type"), "Input should be 'claudecode', 'codex', 'pi' or 'mock'")
    ],
    "bad_execution": [("literal_error", ("runtime", "execution"), "Input should be 'container' or 'local'")],
    "bad_worker_healthcheck": [
        (
            "literal_error",
            ("runtime", "worker_healthcheck"),
            "Input should be 'startup_and_task', 'startup_only' or 'disabled'",
        )
    ],
    "runtime_field_missing": [("missing", ("runtime", "interval"), "Field required")],
    "timeout_zero": [("greater_than", ("tasks", "explore", "timeout"), "Input should be greater than 0")],
    "bad_local_completed_action": [
        ("literal_error", ("local", "completed_action"), "Input should be 'keep' or 'remove'")
    ],
    "common_env_not_dict": [("dict_type", ("common_env",), "Input should be a valid dictionary")],
    "worker_env_value_not_str": [("string_type", ("workers", 0, "env", "MOCK_X"), "Input should be a valid string")],
    "server_missing": [("missing", ("server",), "Field required")],
    "empty_document": [
        ("missing", ("server",), "Field required"),
        ("missing", ("runtime",), "Field required"),
        ("missing", ("tasks",), "Field required"),
        ("missing", ("workers",), "Field required"),
    ],
}


def _todays_base_payload() -> dict[str, Any]:
    return {
        "server": "http://127.0.0.1:8000",
        "runtime": {
            "interval": 60,
            "max_workers": 2,
            "max_running_projects": 1,
            "max_project_workers": 2,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 3},
            "explore": {"timeout": 10, "conclude_timeout": 5},
        },
        "container": {"image": "test-image", "network_mode": "host", "completed_action": "stop"},
        "workers": [
            {
                "name": "test-worker",
                "type": "mock",
                "task_types": ["bootstrap", "reason", "explore"],
                "max_running": 1,
                "priority": 0,
            }
        ],
    }


def _todays_invalid_payloads() -> dict[str, dict[str, Any]]:
    def mutated(change: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        payload = _todays_base_payload()
        change(payload)
        return payload

    def worker(payload: dict[str, Any]) -> dict[str, Any]:
        return payload["workers"][0]

    def mock_reason(behaviour: dict[str, Any] | str) -> dict[str, Any]:
        text = behaviour if isinstance(behaviour, str) else json.dumps(behaviour)
        return mutated(lambda p: worker(p).__setitem__("env", {"MOCK_REASON": text}))

    def pi_env(context_window: str) -> dict[str, str]:
        return {
            "PI_MODEL": "m",
            "PI_BASE_URL": "http://api",
            "PI_API_KEY": "k",
            "PI_PROVIDER_API": "x",
            "PI_MODEL_CONTEXT_WINDOW": context_window,
        }

    one = {"delay": [0, 1], "outcomes": {"intent": "1"}}
    return {
        "duplicate_worker_names": mutated(lambda p: p["workers"].append(copy.deepcopy(worker(p)))),
        "empty_workers": mutated(lambda p: p.__setitem__("workers", [])),
        "project_workers_exceed_max": mutated(lambda p: p["runtime"].__setitem__("max_project_workers", 3)),
        "container_block_missing": mutated(lambda p: p.__setitem__("container", None)),
        "container_worker_missing_env": mutated(lambda p: worker(p).update(type="codex", env={"CODEX_MODEL": "m"})),
        "empty_task_types": mutated(lambda p: worker(p).__setitem__("task_types", [])),
        "duplicate_task_types": mutated(lambda p: worker(p).__setitem__("task_types", ["reason", "reason"])),
        "pi_context_window_not_int": mutated(lambda p: worker(p).update(type="pi", env=pi_env("abc"))),
        "pi_context_window_zero": mutated(lambda p: worker(p).update(type="pi", env=pi_env("0"))),
        "mock_unknown_env": mutated(lambda p: worker(p).__setitem__("env", {"MOCK_UNKNOWN": "{}"})),
        "mock_phase_not_json": mock_reason("not json"),
        "mock_phase_not_object": mock_reason("[]"),
        "mock_delay_bad": mock_reason({"delay": [1], "outcomes": {"intent": "1.0"}}),
        "mock_delay_negative": mock_reason({"delay": [-1, 1], "outcomes": {"intent": "1.0"}}),
        "mock_delay_order": mock_reason({"delay": [2, 1], "outcomes": {"intent": "1.0"}}),
        "mock_delay_not_number": mock_reason({"delay": [True, 1], "outcomes": {"intent": "1.0"}}),
        "mock_outcomes_not_object": mock_reason({"delay": [0, 1], "outcomes": []}),
        "mock_outcomes_unknown": mock_reason({"delay": [0, 1], "outcomes": {"bogus": "1"}}),
        "mock_probability_bad": mock_reason({"delay": [0, 1], "outcomes": {"intent": "x"}}),
        "mock_probability_range": mock_reason({"delay": [0, 1], "outcomes": {"intent": "2"}}),
        "mock_probability_sum": mock_reason({"delay": [0, 1], "outcomes": {"intent": "0.5"}}),
        "mock_rules_not_array": mock_reason({**one, "rules": {}}),
        "mock_rule_not_object": mock_reason({**one, "rules": [1]}),
        "mock_rule_force_bad": mock_reason({**one, "rules": [{"force": "x"}]}),
        "mock_rule_gte_bad": mock_reason({**one, "rules": [{"force": "intent", "fact_ids_gte": -1}]}),
        "mock_rule_lte_bad": mock_reason({**one, "rules": [{"force": "intent", "fact_ids_lte": "x"}]}),
        "mock_rule_empty_bad": mock_reason({**one, "rules": [{"force": "intent", "open_intents_empty": 1}]}),
        "unknown_top_level_key": mutated(lambda p: p.__setitem__("bogus", 1)),
        "unknown_worker_key": mutated(lambda p: worker(p).__setitem__("bogus", 1)),
        "bad_worker_type": mutated(lambda p: worker(p).__setitem__("type", "bogus")),
        "bad_execution": mutated(lambda p: p["runtime"].__setitem__("execution", "bogus")),
        "bad_worker_healthcheck": mutated(lambda p: p["runtime"].__setitem__("worker_healthcheck", "sometimes")),
        "runtime_field_missing": mutated(lambda p: p["runtime"].pop("interval")),
        "timeout_zero": mutated(lambda p: p["tasks"]["explore"].__setitem__("timeout", 0)),
        "bad_local_completed_action": mutated(
            lambda p: p.update(runtime={**p["runtime"], "execution": "local"}, local={"completed_action": "stop"})
        ),
        "common_env_not_dict": mutated(lambda p: p.__setitem__("common_env", ["x"])),
        "worker_env_value_not_str": mutated(lambda p: worker(p).__setitem__("env", {"MOCK_X": 1})),
        "server_missing": mutated(lambda p: p.pop("server")),
        "empty_document": {},
    }


def _strict_cases() -> list[tuple[str, Callable[[dict[str, Any]], Any], str]]:
    """(what is wrong, how the valid section is changed, the key the message must name)."""

    def reviewer(section: dict[str, Any], index: int = 0) -> dict[str, Any]:
        return section["reviewers"][index]

    def env(section: dict[str, Any], index: int = 0) -> dict[str, Any]:
        return reviewer(section, index)["env"]

    first = "repair.reviewers.0"
    return [
        ("unknown key in repair", lambda s: s.__setitem__("server", "http://127.0.0.1:8000"), "repair.server"),
        (
            "removed retention key",
            lambda s: s.__setitem__("transcript_retention_days", 7),
            "repair.transcript_retention_days",
        ),
        ("unknown key in a reviewer", lambda s: reviewer(s).__setitem__("temperature", 0), f"{first}.temperature"),
        ("removed provider key", lambda s: reviewer(s).__setitem__("provider", "test-provider"), f"{first}.provider"),
        ("removed effort key", lambda s: reviewer(s).__setitem__("effort", "high"), f"{first}.effort"),
        (
            "removed server_fallbacks key",
            lambda s: reviewer(s).__setitem__("server_fallbacks", []),
            f"{first}.server_fallbacks",
        ),
        (
            "unknown key in a reviewer's env",
            lambda s: env(s).__setitem__("OPENAI_API_KEY", "x" * 12),
            f"{first}.env.OPENAI_API_KEY",
        ),
        ("no reviewer", lambda s: s.__setitem__("reviewers", []), "repair.reviewers"),
        ("reviewers left out", lambda s: s.pop("reviewers"), "repair.reviewers"),
        ("duplicate reviewer name", lambda s: reviewer(s, 1).__setitem__("name", "reviewer_local"), "repair.reviewers"),
        ("reviewer name with a space", lambda s: reviewer(s).__setitem__("name", "reviewer local"), f"{first}.name"),
        ("reviewer name with =", lambda s: reviewer(s).__setitem__("name", "reviewer=local"), f"{first}.name"),
        (
            "reviewer name with a newline",
            lambda s: reviewer(s).__setitem__("name", "reviewer_local\n"),
            f"{first}.name",
        ),
        ("reviewer name of 65 characters", lambda s: reviewer(s).__setitem__("name", "r" * 65), f"{first}.name"),
        ("REVIEW_MODEL missing", lambda s: env(s).pop("REVIEW_MODEL"), f"{first}.env.REVIEW_MODEL"),
        ("REVIEW_MODEL empty", lambda s: env(s).__setitem__("REVIEW_MODEL", ""), f"{first}.env.REVIEW_MODEL"),
        ("REVIEW_BASE_URL missing", lambda s: env(s).pop("REVIEW_BASE_URL"), f"{first}.env.REVIEW_BASE_URL"),
        (
            "REVIEW_BASE_URL without a scheme",
            lambda s: env(s).__setitem__("REVIEW_BASE_URL", "127.0.0.1:11434/v1"),
            f"{first}.env.REVIEW_BASE_URL",
        ),
        (
            "REVIEW_BASE_URL with another scheme",
            lambda s: env(s).__setitem__("REVIEW_BASE_URL", "ftp://127.0.0.1/v1"),
            f"{first}.env.REVIEW_BASE_URL",
        ),
        (
            "REVIEW_API_KEY with DEL",
            lambda s: env(s).__setitem__("REVIEW_API_KEY", "test-key-\x7f-0123"),
            f"{first}.env.REVIEW_API_KEY",
        ),
        (
            "REVIEW_API_KEY with a tab",
            lambda s: env(s).__setitem__("REVIEW_API_KEY", "test-key-\t-0123"),
            f"{first}.env.REVIEW_API_KEY",
        ),
        (
            "REVIEW_API_KEY not ASCII",
            lambda s: env(s).__setitem__("REVIEW_API_KEY", "test-key-é-0123"),
            f"{first}.env.REVIEW_API_KEY",
        ),
        (
            "placeholder REVIEW_MODEL",
            lambda s: env(s).__setitem__("REVIEW_MODEL", "<model name>"),
            f"{first}.env.REVIEW_MODEL",
        ),
        (
            "placeholder REVIEW_BASE_URL",
            lambda s: env(s).__setitem__("REVIEW_BASE_URL", "https://<your provider>/v1"),
            f"{first}.env.REVIEW_BASE_URL",
        ),
        (
            "placeholder REVIEW_API_KEY",
            lambda s: env(s).__setitem__("REVIEW_API_KEY", "<the reviewer's own key>"),
            f"{first}.env.REVIEW_API_KEY",
        ),
        (
            "placeholder token_file",
            lambda s: s.__setitem__("token_file", "/home/<you>/cairn/secrets/repair-dispatcher.token"),
            "repair.token_file",
        ),
        (
            "placeholder base URL port",
            lambda s: env(s).__setitem__("REVIEW_BASE_URL", "http://host.docker.internal:<port>/v1"),
            f"{first}.env.REVIEW_BASE_URL",
        ),
        (
            "relative token_file",
            lambda s: s.__setitem__("token_file", "secrets/repair-dispatcher.token"),
            "repair.token_file",
        ),
        ("token_file left out", lambda s: s.pop("token_file"), "repair.token_file"),
        (
            "relative transcript_root",
            lambda s: s.__setitem__("transcript_root", "repair/transcripts"),
            "repair.transcript_root",
        ),
        (
            "~user that does not exist",
            lambda s: s.__setitem__("transcript_root", "~no-such-user-for-cairn-tests/x"),
            "repair.transcript_root",
        ),
        ("timeout_seconds of 0", lambda s: reviewer(s).__setitem__("timeout_seconds", 0), f"{first}.timeout_seconds"),
        ("max_tokens of 0", lambda s: reviewer(s).__setitem__("max_tokens", 0), f"{first}.max_tokens"),
        ("max_attempts of 0", lambda s: reviewer(s).__setitem__("max_attempts", 0), f"{first}.max_attempts"),
        ("context_window of 0", lambda s: reviewer(s).__setitem__("context_window", 0), f"{first}.context_window"),
        ("max_tokens as a string", lambda s: reviewer(s).__setitem__("max_tokens", "4096"), f"{first}.max_tokens"),
        (
            "structured_output not a bool",
            lambda s: reviewer(s).__setitem__("structured_output", "yes"),
            f"{first}.structured_output",
        ),
        ("empty ca_bundle", lambda s: reviewer(s).__setitem__("ca_bundle", ""), f"{first}.ca_bundle"),
        ("empty proxy", lambda s: reviewer(s).__setitem__("proxy", ""), f"{first}.proxy"),
        (
            "price of .inf",
            lambda s: reviewer(s).__setitem__("price", {"input_per_mtok": float("inf"), "output_per_mtok": 1}),
            f"{first}.price.input_per_mtok",
        ),
        (
            "price of -1",
            lambda s: reviewer(s).__setitem__("price", {"input_per_mtok": 1, "output_per_mtok": -1}),
            f"{first}.price.output_per_mtok",
        ),
        (
            "price with another key",
            lambda s: reviewer(s).__setitem__(
                "price", {"input_per_mtok": 1, "output_per_mtok": 1, "cached_per_mtok": 1}
            ),
            f"{first}.price",
        ),
        (
            "price without output_per_mtok",
            lambda s: reviewer(s).__setitem__("price", {"input_per_mtok": 1}),
            f"{first}.price",
        ),
        ("projects not a list", lambda s: s.__setitem__("projects", "proj_001"), "repair.projects"),
        ("projects with an empty id", lambda s: s.__setitem__("projects", [""]), "repair.projects.0"),
        ("projects with a 129-character id", lambda s: s.__setitem__("projects", ["p" * 129]), "repair.projects.0"),
        ("placeholder project id", lambda s: s.__setitem__("projects", ["<project id>"]), "repair.projects"),
    ]


def _errors(exc: ValidationError) -> list[tuple[str, tuple[str | int, ...], str]]:
    return [(error["type"], tuple(error["loc"]), error["msg"]) for error in exc.errors()]


def _model_digest(config: DispatchConfig) -> str:
    dump = json.dumps(config.model_dump(mode="json", exclude={"repair"}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dump.encode()).hexdigest()


def _committed_examples() -> list[str] | None:
    """The dispatch*.yaml files that git tracks at the repository root; None where git cannot tell (no git, or no
    checkout). Only tracked files are examples: README's setup copies one to dispatch.yaml at the root, which
    .gitignore ignores and docker-compose.yaml mounts, and an operator's own configuration is no test input."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", ":(glob)dispatch*.yaml"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted(name for name in os.fsdecode(listed).split("\0") if name) or None


@contextmanager
def _file_opens_and_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[list[str], list[str]]]:
    """Record every file opened (open, io.open, os.open) and fail every attempt to connect anywhere."""
    opened: list[str] = []
    connects: list[str] = []
    real_open = io.open
    real_os_open = os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fsdecode(file) if isinstance(file, (str, bytes, os.PathLike)) else repr(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path))
        return real_os_open(path, *args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        connects.append(repr(args[1:] if args else kwargs))
        raise OSError("no request may be made while dispatch.yaml is loaded")

    with monkeypatch.context() as patch:
        patch.setattr(io, "open", spy_open)
        patch.setattr(builtins, "open", spy_open)
        patch.setattr(os, "open", spy_os_open)
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket.socket, "connect_ex", refuse)
        patch.setattr(socket, "create_connection", refuse)
        patch.setattr(requests.Session, "request", refuse)
        yield opened, connects


def test_repair_section_is_optional_and_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    committed = _committed_examples()  # before HOME changes, so that git reads the user's own git configuration
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # DispatchConfig differs from today's by the repair field alone: optional, default None, after workers.
    fields = {
        name: (repr(info.annotation), info.is_required(), repr(info.default))
        for name, info in DispatchConfig.model_fields.items()
    }
    assert list(fields) == [*TODAYS_FIELDS, "repair"]
    assert {name: value for name, value in fields.items() if name != "repair"} == TODAYS_FIELDS
    assert fields["repair"] == ("cairn.dispatcher.config.RepairConfig | None", False, "None")
    assert DispatchConfig.model_config.get("extra") == "forbid"

    # Every committed dispatch*.yaml example parses to today's model, with repair None. The committed ones are
    # today's three, which git confirms where it can; files that git does not track, such as an operator's
    # dispatch.yaml at the root, are never read.
    if committed is not None:
        assert committed == sorted(TODAYS_EXAMPLE_DIGESTS)
    examples = sorted(TODAYS_EXAMPLE_DIGESTS)
    assert [name for name in examples if not (REPO_ROOT / name).is_file()] == []
    for name in examples:
        config = DispatchConfig.load(REPO_ROOT / name)
        assert config.repair is None, name
        assert _model_digest(config) == TODAYS_EXAMPLE_DIGESTS[name], name
    assert make_config().repair is None

    # Every invalid file of today fails with today's errors, word for word, through DispatchConfig.load.
    invalid = _todays_invalid_payloads()
    assert sorted(invalid) == sorted(TODAYS_ERRORS)
    for name, payload in invalid.items():
        path = tmp_path / f"today-{name}.yaml"
        path.write_text(yaml.safe_dump(payload) if payload else "", encoding="utf-8")
        with pytest.raises(ValidationError) as caught:
            DispatchConfig.load(path)
        assert _errors(caught.value) == TODAYS_ERRORS[name], name
    payload = _todays_base_payload()
    payload["runtime"]["prompt_group"] = "nope"
    with pytest.raises(ValueError, match=r"^missing prompt group: nope$"):
        DispatchConfig.load(write_dispatch_yaml(tmp_path / "prompt-group.yaml", payload))

    # Spec 8.3's example, its placeholders replaced, loads; ~ is expanded in both paths.
    section = repair_section(token_path="~/cairn/secrets/repair-dispatcher.token")  # noqa: S106 - a path
    config = DispatchConfig.load(write_dispatch_yaml(tmp_path / "with-repair.yaml", dispatch_payload(section)))
    repair = config.repair
    assert repair is not None
    assert repair.token_file == f"{home}/cairn/secrets/repair-dispatcher.token"
    assert repair.transcript_root == "/var/lib/cairn-repair/transcripts"
    assert repair.projects is None
    assert [reviewer.name for reviewer in repair.reviewers] == ["reviewer_local", "reviewer_provider"]
    local, provider = repair.reviewers
    assert local.env.model_dump() == {
        "REVIEW_MODEL": "test-model",
        "REVIEW_BASE_URL": "http://127.0.0.1:11434/v1",
        "REVIEW_API_KEY": None,
    }
    assert provider.env.REVIEW_API_KEY == FAKE_REVIEWER_KEY
    assert provider.timeout_seconds == 180
    for reviewer in (local, provider):
        for setting in (
            "structured_output",
            "max_tokens",
            "context_window",
            "max_attempts",
            "ca_bundle",
            "proxy",
            "price",
        ):
            assert getattr(reviewer, setting) is None, setting
    assert local.timeout_seconds is None
    assert config.model_dump(exclude={"repair"}) == make_config().model_dump(exclude={"repair"})

    # transcript_root's default is expanded too; REVIEW_API_KEY "" is the same as left out.
    section = repair_section(projects=["proj_001", "proj_027"])
    section.pop("transcript_root")
    section["reviewers"][1]["env"]["REVIEW_API_KEY"] = ""
    repair = DispatchConfig.load(write_dispatch_yaml(tmp_path / "defaults.yaml", dispatch_payload(section))).repair
    assert repair is not None
    assert repair.transcript_root == f"{home}/.local/share/cairn/repair/transcripts"
    assert repair.projects == ["proj_001", "proj_027"]
    assert repair.reviewers[1].env.REVIEW_API_KEY is None

    # Appendix C-1's reviewer entry, with every optional setting at its default, and a price.
    settings = {
        "structured_output": True,
        "max_tokens": 4096,
        "context_window": None,
        "timeout_seconds": 180,
        "max_attempts": 2,
        "ca_bundle": None,
        "proxy": None,
        "price": None,
    }
    section = repair_section()
    section["reviewers"][0].update(settings)
    section["reviewers"][1]["price"] = {"input_per_mtok": 0.5, "output_per_mtok": 2}
    section["reviewers"][1]["ca_bundle"] = "/etc/ssl/certs/test-ca.pem"
    section["reviewers"][1]["proxy"] = "http://127.0.0.1:3128"
    repair = DispatchConfig.load(write_dispatch_yaml(tmp_path / "c1.yaml", dispatch_payload(section))).repair
    assert repair is not None
    assert {name: getattr(repair.reviewers[0], name) for name in settings} == settings
    assert repair.reviewers[1].price == {"input_per_mtok": 0.5, "output_per_mtok": 2.0}

    # A repair: key with no value, or with every key under it commented out, is no section.
    base_text = yaml.safe_dump(dispatch_payload(), sort_keys=False)
    for suffix in ("repair:\n", "repair:\n  # token_file: /srv/x.token\n  # reviewers: []\n", "repair: null\n"):
        path = tmp_path / "empty-repair.yaml"
        path.write_text(base_text + suffix, encoding="utf-8")
        empty = DispatchConfig.load(path)
        assert empty.repair is None
        assert _model_digest(empty) == _model_digest(make_config())

    # common_env reaches the workers only: every reviewer's env keeps its own keys.
    payload = dispatch_payload(repair_section())
    payload["common_env"] = {"OPENAI_API_KEY": "test-common-key-0003", "SHARED": "1"}
    config = DispatchConfig.load(write_dispatch_yaml(tmp_path / "common-env.yaml", payload))
    assert config.repair is not None
    assert config.workers[0].env["OPENAI_API_KEY"] == "test-common-key-0003"
    assert [set(reviewer.env.model_dump()) for reviewer in config.repair.reviewers] == [
        {"REVIEW_MODEL", "REVIEW_BASE_URL", "REVIEW_API_KEY"}
    ] * 2
    for reviewer in config.repair.reviewers:
        assert "test-common-key-0003" not in json.dumps(reviewer.model_dump())

    # Loading opens the same files with and without the section, makes no request, and touches neither the
    # token file nor the copy root.
    token_file = tmp_path / "secrets" / "repair-dispatcher.token"
    token_file.parent.mkdir()
    token_file.write_text("x" * 40, encoding="utf-8")
    copy_root = tmp_path / "copies"
    without = write_dispatch_yaml(tmp_path / "plain.yaml", dispatch_payload())
    with_section = write_dispatch_yaml(
        tmp_path / "section.yaml",
        dispatch_payload(repair_section(token_path=str(token_file), transcript_root=str(copy_root))),
    )
    DispatchConfig.load(without)
    DispatchConfig.load(with_section)  # once each first, so that no lazy first-use work differs between the two
    with _file_opens_and_requests(monkeypatch) as (opened, connects):
        DispatchConfig.load(without)
        opened_without = [path.replace(str(without), "<dispatch.yaml>") for path in opened]
        opened.clear()
        loaded = DispatchConfig.load(with_section)
        opened_with = [path.replace(str(with_section), "<dispatch.yaml>") for path in opened]
    assert loaded.repair is not None
    assert opened_with == opened_without
    assert "<dispatch.yaml>" in opened_with
    assert connects == []
    assert not copy_root.exists()

    # Loading a file with the section imports nothing under cairn.dispatcher.repair (fresh interpreter).
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from cairn.dispatcher.config import DispatchConfig\n"
        "config = DispatchConfig.load(Path(sys.argv[1]))\n"
        "assert config.repair is not None\n"
        "print(sorted(name for name in sys.modules if name.startswith('cairn.dispatcher.repair')))\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", probe, str(with_section)], capture_output=True, text=True, env=environment, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"

    # Each malformed section raises from DispatchConfig.load, and the message names the key.
    problems: list[str] = []
    payloads = []
    for what, change, key in _strict_cases():
        section = repair_section()
        change(section)
        payloads.append((what, dispatch_payload(section), key))
    not_a_mapping = dispatch_payload()
    not_a_mapping["repair"] = "token_file"
    payloads.append(("repair as a string", not_a_mapping, "repair"))
    for index, (what, payload, key) in enumerate(payloads):
        path = write_dispatch_yaml(tmp_path / f"strict-{index}.yaml", payload)
        try:
            DispatchConfig.load(path)
        except ValidationError as exc:
            message = str(exc)
            if f"\n{key}\n" not in message:
                problems.append(f"{what}: the message does not name {key}:\n{message}")
        else:
            problems.append(f"{what}: loaded")
    assert not problems, "\n".join(problems)
