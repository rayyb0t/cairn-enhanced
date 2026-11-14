"""Intent issues (the JSON-repair badge): recorded with the conclude, shown on the intent, never a failure.

Recording an issue is additive. A conclude without parse_repair, every other endpoint and the graph
export that workers read must behave exactly as before, and nothing about an issue may ever make a
conclude or a project read fail.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeContainerManager, FakeDriver, FakeLease, make_config
from fastapi.testclient import TestClient

from cairn.dispatcher.contracts import (
    EXPLORE_TEXT_ENVELOPE,
    RescueReport,
    parse_validated,
    validate_explore_payload,
)
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import explore
from cairn.dispatcher.tasks.common import write_conclude_result
from cairn.server import db, services
from cairn.server.app import app
from cairn.server.models import ProjectDetail
from cairn.server.services import record_intent_issue

REPAIR = {"strategy": "envelope_repair", "error": "no JSON object found in output"}
TIMED_OUT = ProcessResult(124, "", "", timed_out=True)


@pytest.fixture
def server(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _project_with_intents(server: TestClient, count: int = 1) -> str:
    response = server.post("/projects", json={"title": "t", "origin": "start", "goal": "finish"})
    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    for _ in range(count):
        response = server.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
        )
        assert response.status_code == 201
    return project_id


def _conclude(server: TestClient, project_id: str, intent_id: str, **extra: Any):
    body = {"worker": "explorer", "description": f"fact for {intent_id}", **extra}
    return server.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json=body)


def _intent(server: TestClient, project_id: str, intent_id: str) -> dict[str, Any]:
    response = server.get(f"/projects/{project_id}")
    assert response.status_code == 200
    return next(intent for intent in response.json()["intents"] if intent["id"] == intent_id)


def _fact_ids(server: TestClient, project_id: str) -> set[str]:
    return {fact["id"] for fact in server.get(f"/projects/{project_id}").json()["facts"]}


# --------------------------------------------------------------------------- server


def test_conclude_with_parse_repair_records_one_json_repaired_issue(server: TestClient) -> None:
    project_id = _project_with_intents(server, count=2)
    response = _conclude(server, project_id, "i001", parse_repair=REPAIR)
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "fact for i001"}

    issues = response.json()["intent"]["issues"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["kind"] == "json_repaired"
    assert issue["detail"] == REPAIR
    assert issue["run_id"] is None
    assert issue["created_at"] == issue["updated_at"] == response.json()["intent"]["concluded_at"]

    assert _intent(server, project_id, "i001")["issues"] == issues
    assert _intent(server, project_id, "i002")["issues"] == []


def test_conclude_without_parse_repair_is_unchanged_and_records_nothing(server: TestClient) -> None:
    project_id = _project_with_intents(server)
    response = _conclude(server, project_id, "i001")
    assert response.status_code == 200
    payload = response.json()
    assert payload["fact"] == {"id": "f001", "description": "fact for i001"}
    assert payload["intent"]["to"] == "f001"
    assert payload["intent"]["issues"] == []
    assert _intent(server, project_id, "i001")["issues"] == []


@pytest.mark.parametrize(
    ("value", "recorded"),
    [
        pytest.param(None, None, id="null"),
        pytest.param("envelope_repair", None, id="bare-string"),
        pytest.param(42, None, id="number"),
        pytest.param([REPAIR], None, id="list"),
        pytest.param({}, None, id="empty-object"),
        pytest.param({"strategy": None}, None, id="null-strategy"),
        pytest.param({"strategy": ["envelope_repair"]}, None, id="unhashable-strategy"),
        pytest.param({"strategy": {"name": "x"}}, None, id="object-strategy"),
        pytest.param({"strategy": "future_strategy", "error": "x"}, None, id="unknown-strategy"),
        pytest.param({"strategy": "lenient_decode", "error": 5}, {"strategy": "lenient_decode"}, id="non-text-error"),
        pytest.param({"strategy": "lenient_decode", "error": "  "}, {"strategy": "lenient_decode"}, id="blank-error"),
        pytest.param(
            {"strategy": "lenient_decode", "error": "e" * 1000, "extra": {"nested": [1, 2]}},
            {"strategy": "lenient_decode", "error": "e" * 300},
            id="long-error-and-extra-keys",
        ),
    ],
)
def test_any_parse_repair_value_is_accepted_and_only_understood_ones_are_recorded(
    server: TestClient, value: Any, recorded: dict[str, str] | None
) -> None:
    project_id = _project_with_intents(server)
    response = _conclude(server, project_id, "i001", parse_repair=value)
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == "f001"
    issues = _intent(server, project_id, "i001")["issues"]
    assert [issue["detail"] for issue in issues] == ([] if recorded is None else [recorded])


def test_a_failing_issue_insert_never_fails_the_conclude(server: TestClient, caplog) -> None:
    project_id = _project_with_intents(server)
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER refuse_issues BEFORE INSERT ON intent_issues BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
    caplog.set_level(logging.WARNING, logger="cairn.server.services")

    response = _conclude(server, project_id, "i001", parse_repair=REPAIR)

    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "fact for i001"}
    intent = _intent(server, project_id, "i001")
    assert intent["to"] == "f001"
    assert intent["worker"] == "explorer"
    assert intent["issues"] == []
    assert "f001" in _fact_ids(server, project_id)
    assert any("intent issue not recorded" in record.getMessage() for record in caplog.records)


def test_a_missing_issues_table_never_fails_any_endpoint(server: TestClient, caplog) -> None:
    project_id = _project_with_intents(server, count=2)
    with db.get_conn() as conn:
        conn.execute("DROP TABLE intent_issues")
    caplog.set_level(logging.WARNING, logger="cairn.server.services")

    heartbeat = server.post(f"/projects/{project_id}/intents/i002/heartbeat", json={"worker": "explorer"})
    assert heartbeat.status_code == 200
    assert heartbeat.json()["issues"] == []
    response = _conclude(server, project_id, "i001", parse_repair=REPAIR)
    assert response.status_code == 200
    assert response.json()["intent"]["issues"] == []

    detail = server.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert [intent["issues"] for intent in detail.json()["intents"]] == [[], []]
    assert "f001" in _fact_ids(server, project_id)
    assert any("intent issues not loaded" in record.getMessage() for record in caplog.records)


def test_an_issue_write_that_ends_its_transaction_never_fails_the_conclude(server: TestClient, caplog) -> None:
    # RAISE(ROLLBACK) ends the whole transaction it runs in, like SQLITE_FULL or an I/O error would. The
    # issue is written in its own transaction after the conclude commits, so the fact is never lost.
    project_id = _project_with_intents(server)
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER end_transaction BEFORE INSERT ON intent_issues"
            " BEGIN SELECT RAISE(ROLLBACK, 'ends the transaction'); END"
        )
    caplog.set_level(logging.WARNING, logger="cairn.server.services")

    response = _conclude(server, project_id, "i001", parse_repair=REPAIR)

    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "fact for i001"}
    assert response.json()["intent"]["issues"] == []
    intent = _intent(server, project_id, "i001")
    assert (intent["to"], intent["issues"]) == ("f001", [])
    assert "f001" in _fact_ids(server, project_id)
    assert any("intent issue not recorded" in record.getMessage() for record in caplog.records)


def test_any_error_while_recording_never_fails_the_conclude(server: TestClient, monkeypatch) -> None:
    project_id = _project_with_intents(server)

    def broken_connection(*_args: Any, **_kwargs: Any):
        raise RuntimeError("no connection for issues")

    monkeypatch.setattr(services, "get_conn", broken_connection)
    response = _conclude(server, project_id, "i001", parse_repair=REPAIR)
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == "f001"
    assert "f001" in _fact_ids(server, project_id)


def test_deep_or_undecodable_issue_details_never_fail_reads(server: TestClient) -> None:
    project_id = _project_with_intents(server, count=2)
    assert _conclude(server, project_id, "i001", parse_repair=REPAIR).status_code == 200
    too_deep = '{"a": ' * 50 + "1" + "}" * 50  # valid JSON, too deep to serialize safely
    recursion = '{"a": ' * 200_000 + "1" + "}" * 200_000  # json.loads raises RecursionError
    with db.get_conn() as conn:
        for intent_id, detail in (("i001", too_deep), ("i002", too_deep), ("i002", recursion)):
            conn.execute(
                "INSERT INTO intent_issues (project_id, intent_id, kind, detail_json, created_at, updated_at)"
                " VALUES (?, ?, 'deep', ?, 'x', 'x')",
                (project_id, intent_id, detail),
            )
    detail = server.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    heartbeat = server.post(f"/projects/{project_id}/intents/i002/heartbeat", json={"worker": "explorer"})
    assert heartbeat.status_code == 200
    assert [issue["detail"] for issue in heartbeat.json()["issues"]] == [{}, {}]
    assert [(i["kind"], i["detail"]) for i in _intent(server, project_id, "i001")["issues"]] == [
        ("deep", {}),
        ("json_repaired", REPAIR),
    ]


@pytest.mark.parametrize(
    "issues",
    [
        pytest.param([{"id": "uuid-1", "kind": "k", "detail": {}, "created_at": "x", "updated_at": "x"}], id="text-id"),
        pytest.param([{"id": 1, "kind": "k", "detail": [], "created_at": "x", "updated_at": "x"}], id="list-detail"),
        pytest.param([{"id": 1, "kind": "k", "detail": {}, "created_at": "x"}], id="missing-updated-at"),
        pytest.param([{"id": 1, "kind": "k", "run_id": 7, "created_at": "x", "updated_at": "x"}], id="number-run-id"),
        pytest.param("not a list", id="not-a-list"),
        pytest.param(None, id="null"),
    ],
)
def test_shared_models_drop_issue_shapes_they_cannot_read(issues: Any) -> None:
    # The dispatcher parses project reads with these models; a newer server's issue shape must never
    # make that parse fail. Readable issues next to an unreadable one are kept.
    readable = {"id": 2, "kind": "json_repaired", "detail": REPAIR, "created_at": "x", "updated_at": "x"}
    payload = {
        "project": {"id": "p", "title": "t", "status": "active", "bootstrap_enabled": True, "created_at": "x"},
        "facts": [],
        "hints": [],
        "intents": [
            {"id": "i1", "from": ["origin"], "description": "d", "creator": "c", "created_at": "x", "issues": issues},
            {
                "id": "i2",
                "from": ["origin"],
                "description": "d",
                "creator": "c",
                "created_at": "x",
                "issues": [*(issues if isinstance(issues, list) else []), readable],
            },
        ],
    }
    project = ProjectDetail.model_validate(payload)
    assert project.intents[0].issues == []
    assert [issue.id for issue in project.intents[1].issues] == [2]


def test_issues_are_listed_newest_first(server: TestClient) -> None:
    project_id = _project_with_intents(server)
    assert _conclude(server, project_id, "i001", parse_repair=REPAIR).status_code == 200
    assert record_intent_issue(project_id, "i001", "later_kind", {"n": 2}, "2030-01-01T00:00:00Z") is not None
    issues = _intent(server, project_id, "i001")["issues"]
    assert [issue["kind"] for issue in issues] == ["later_kind", "json_repaired"]
    assert issues[0]["id"] > issues[1]["id"]


def test_unreadable_issue_rows_are_skipped_or_emptied(server: TestClient) -> None:
    project_id = _project_with_intents(server)
    assert _conclude(server, project_id, "i001", parse_repair=REPAIR).status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intent_issues (project_id, intent_id, kind, detail_json, created_at, updated_at)"
            " VALUES (?, 'i001', 'bad_detail', 'not json', 'x', 'x')",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO intent_issues (project_id, intent_id, kind, detail_json, created_at, updated_at)"
            " VALUES (?, 'i001', 'list_detail', '[1]', 'x', 'x')",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO intent_issues (project_id, intent_id, kind, detail_json, created_at, updated_at)"
            " VALUES (?, 'i001', X'FF', '{}', 'x', 'x')",  # a kind that is not text: the row is skipped
            (project_id,),
        )
    response = server.get(f"/projects/{project_id}")
    assert response.status_code == 200
    issues = _intent(server, project_id, "i001")["issues"]
    assert [(issue["kind"], issue["detail"]) for issue in issues] == [
        ("list_detail", {}),
        ("bad_detail", {}),
        ("json_repaired", REPAIR),
    ]


def test_issues_are_deleted_with_their_project(server: TestClient) -> None:
    project_id = _project_with_intents(server)
    assert _conclude(server, project_id, "i001", parse_repair=REPAIR).status_code == 200
    assert server.delete(f"/projects/{project_id}").status_code == 204
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM intent_issues").fetchone()[0] == 0


def test_the_graph_export_workers_read_never_contains_issues(server: TestClient) -> None:
    project_id = _project_with_intents(server, count=2)
    assert _conclude(server, project_id, "i001", parse_repair=REPAIR).status_code == 200
    before = server.get(f"/projects/{project_id}/export", params={"format": "yaml"}).text
    assert record_intent_issue(project_id, "i002", "json_repaired", {"strategy": "lenient_decode"}, "x") is not None
    after = server.get(f"/projects/{project_id}/export", params={"format": "yaml"}).text
    timeline = server.get(f"/projects/{project_id}/export", params={"format": "timeline"}).text
    assert before == after
    for text in (after, timeline):
        assert "issues" not in text
        assert "json_repaired" not in text
        assert "envelope_repair" not in text


def test_configure_adds_the_issues_table_to_an_existing_database(tmp_path, monkeypatch) -> None:
    path = tmp_path / "before.db"
    schema_before = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS intent_issues")[0]
    with sqlite3.connect(path) as conn:
        conn.executescript(schema_before)
        conn.execute("INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'old', '2026-01-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at)"
            " VALUES ('i001', 'proj_001', 'old intent', 'reasoner', '2026-01-01T00:00:00Z')"
        )
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with TestClient(app) as server:
        assert _intent(server, "proj_001", "i001")["issues"] == []
        response = server.post(
            "/projects/proj_001/intents/i001/conclude",
            json={"worker": "explorer", "description": "done", "parse_repair": REPAIR},
        )
        assert response.status_code == 200
        assert [issue["detail"] for issue in _intent(server, "proj_001", "i001")["issues"]] == [REPAIR]


# --------------------------------------------------------------------------- dispatcher


def test_rescue_report_is_set_only_when_the_rescue_produced_the_result() -> None:
    strict = RescueReport()
    result = parse_validated(
        '{"accepted": true, "data": {"description": "ok"}}',
        validate_explore_payload,
        envelope=EXPLORE_TEXT_ENVELOPE,
        on_rescue=strict,
    )
    assert result == ("fact", "ok")
    assert strict.parse_repair is None

    rescued = RescueReport()
    result = parse_validated(
        '{"accepted": true, "data": {"description": "a "quoted" word"}}',
        validate_explore_payload,
        envelope=EXPLORE_TEXT_ENVELOPE,
        on_rescue=rescued,
    )
    assert result == ("fact", 'a "quoted" word')
    assert rescued.parse_repair == REPAIR

    refused = RescueReport()
    with pytest.raises(ValueError, match="no JSON object found in output"):
        parse_validated(
            '{"accepted": true, "data": {"description": "cut',
            validate_explore_payload,
            envelope=EXPLORE_TEXT_ENVELOPE,
            on_rescue=refused,
        )
    assert refused.parse_repair is None


def test_a_failing_rescue_report_never_changes_the_parse_result(caplog) -> None:
    def broken(_strategy: str, _error: str) -> None:
        raise RuntimeError("report failed")

    caplog.set_level(logging.WARNING, logger="cairn.dispatcher.contracts")
    result = parse_validated(
        '{"accepted": true, "data": {"description": "a "quoted" word"}}',
        validate_explore_payload,
        envelope=EXPLORE_TEXT_ENVELOPE,
        context="phase=explore_conclude project=p intent=i worker=w",
        on_rescue=broken,
    )
    assert result == ("fact", 'a "quoted" word')
    assert any("json output rescue report failed" in record.getMessage() for record in caplog.records)


def test_client_sends_parse_repair_only_when_there_is_one(monkeypatch) -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def fake_request_json(_self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        sent.append((method, path, json))
        return ApiResult(200, {})

    monkeypatch.setattr(CairnClient, "_request_json", fake_request_json)
    client = CairnClient("http://cairn.invalid")
    client.conclude("proj_001", "i001", "explorer", "fact")
    client.conclude("proj_001", "i001", "explorer", "fact", parse_repair=REPAIR)
    assert sent == [
        ("POST", "/projects/proj_001/intents/i001/conclude", {"worker": "explorer", "description": "fact"}),
        (
            "POST",
            "/projects/proj_001/intents/i001/conclude",
            {"worker": "explorer", "description": "fact", "parse_repair": REPAIR},
        ),
    ]


class _ClientWithTodaysSignature:
    def __init__(self) -> None:
        self.concluded: list[tuple[str, str, str, str]] = []

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        self.concluded.append((project_id, intent_id, worker, description))
        return ApiResult(200, {"fact": {"id": "f009"}})


def test_write_conclude_result_makes_todays_call_when_nothing_was_rescued() -> None:
    client = _ClientWithTodaysSignature()
    status = write_conclude_result(
        client,  # type: ignore[arg-type]
        "proj_001",
        "i001",
        "explorer",
        "fact",
        source="explore_conclude",
        phase_ms=5,
    )
    assert status == "success"
    assert client.concluded == [("proj_001", "i001", "explorer", "fact")]


# --------------------------------------------------------------------------- end to end


def _client_for(server: TestClient, monkeypatch) -> CairnClient:
    # Route the dispatcher's real client to the in-process server.
    monkeypatch.setattr(CairnClient, "_session", lambda _self: server)
    return CairnClient(str(server.base_url))


def _run_timed_out_explore(server: TestClient, monkeypatch, conclude_output: str) -> tuple[str, str]:
    project_id = _project_with_intents(server)
    client = _client_for(server, monkeypatch)
    project = client.get_project(project_id)
    config = make_config()
    results = iter([TIMED_OUT, ProcessResult(0, conclude_output, "")])
    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *_a, **_k: FakeLease())
    monkeypatch.setattr(explore, "_run_process", lambda *_a, **_k: next(results))
    outcome = explore.run_explore_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "facts: []\n",
        project.intents[0],
        config.workers[0],
        TaskCancellation(),
    )
    return outcome, project_id


def test_a_rescued_explore_conclude_shows_the_json_repaired_issue(server: TestClient, monkeypatch) -> None:
    outcome, project_id = _run_timed_out_explore(
        server, monkeypatch, '{"accepted": true, "data": {"description": "a "quoted" word"}}'
    )
    assert outcome == "success"
    detail = server.get(f"/projects/{project_id}").json()
    assert {"id": "f001", "description": 'a "quoted" word'} in detail["facts"]
    intent = detail["intents"][0]
    assert intent["to"] == "f001"
    assert [(issue["kind"], issue["detail"]) for issue in intent["issues"]] == [("json_repaired", REPAIR)]


def test_a_strictly_parsed_explore_conclude_records_no_issue(server: TestClient, monkeypatch) -> None:
    outcome, project_id = _run_timed_out_explore(
        server, monkeypatch, '{"accepted": true, "data": {"description": "plain"}}'
    )
    assert outcome == "success"
    detail = server.get(f"/projects/{project_id}").json()
    assert {"id": "f001", "description": "plain"} in detail["facts"]
    assert detail["intents"][0]["issues"] == []
