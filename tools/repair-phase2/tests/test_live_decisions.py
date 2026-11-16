"""Live shadow decisions (spec §15.6): gate_report.py --server (GT9) and export_shadow_runs.py, against the real server.

The server is this repository's app (`cairn.server.app`, through the repair agent's `dev` dependency on `../cairn`),
served by uvicorn on loopback in this process, on a temporary database, with both role tokens configured and its clock
frozen and moved by the test; its lifespan is not run. Runs and reviews are registered and posted through the server's
own endpoints, as the dispatcher and the agent do. Every value is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from gatekit import clear_guarded_environment, dispatch_yaml, fake_secret, write_agent_files, write_labels

pytest.importorskip("cairn_repair_agent")
pytest.importorskip("fastapi")

import requests
import uvicorn
from cairn_repair_agent import export, replay

import export_shadow_runs
import gate_report
from cairn.server import db, repair, repair_auth, services
from cairn.server.app import app
from cairn.server.routers import hints as hints_router
from cairn.server.routers import intents as intents_router
from cairn.server.routers import projects as projects_router

AGENT_TOKEN = fake_secret("agent-token", 48)
DISPATCHER_TOKEN = fake_secret("dispatcher-token", 48)
SETUP_HEADER = "X-Test-Setup"
START = datetime(2026, 11, 14, 17, 19, 20, tzinfo=UTC)  # 1360 s after the runs' start: their windows are open
RUN_IDS = {f"i00{n}": hashlib.sha256(f"run-{n}".encode()).hexdigest()[:32] for n in range(1, 6)}
NEWER_I002 = hashlib.sha256(b"run-2b").hexdigest()[:32]


@pytest.fixture(autouse=True)
def _guarded_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_guarded_environment(monkeypatch)


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, list[str]]
    test_setup: bool


@dataclass
class Recorder:
    app: Any
    seen: list[Seen] = field(default_factory=list)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {name.decode("latin-1").lower() for name, _ in scope["headers"]}
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            self.seen.append(Seen(scope["method"], scope["path"], query, SETUP_HEADER.lower() in headers))
        await self.app(scope, receive, send)

    def tool_calls(self) -> list[Seen]:
        return [seen for seen in self.seen if not seen.test_setup]


@dataclass
class Clock:
    now: datetime = START

    def text(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RealServer:
    url: str
    recorder: Recorder
    session: requests.Session
    clock: Clock

    def call(self, method: str, path: str, *, role: str | None = None, **kwargs: Any) -> requests.Response:
        headers = {SETUP_HEADER: "1"}
        if role is not None:
            headers[repair_auth.AUTH_HEADER] = AGENT_TOKEN if role == "agent" else DISPATCHER_TOKEN
        return self.session.request(method, self.url + path, headers=headers, timeout=30, **kwargs)

    def project(self, intents: int) -> str:
        response = self.call(
            "POST",
            "/projects",
            json={
                "title": "lab",
                "origin": "start",
                "goal": "finish",
                "bootstrap_enabled": False,
                "repair_enabled": True,
            },
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["project"]["id"]
        for number in range(1, intents + 1):
            created = self.call(
                "POST",
                f"/projects/{project_id}/intents",
                json={
                    "from": ["origin"],
                    "description": f"Map lab service {number}",
                    "creator": "reasoner",
                    "worker": None,
                },
            )
            assert created.status_code == 201, created.text
            claimed = self.call(
                "POST", f"/projects/{project_id}/intents/i{number:03d}/heartbeat", json={"worker": f"w{number}"}
            )
            assert claimed.status_code == 200, claimed.text
        return project_id

    def register(self, run_id: str, project_id: str, intent: str, worker: str) -> None:
        body = {
            "project_id": project_id,
            "intent_id": intent,
            "worker": worker,
            "cli_type": "claudecode",
            "execution": "container",
            "budget_seconds": 1800,
            "session_id": None,
            "started_seconds_ago": 1360,
            "locator": {"container": "cairn-dispatch-test", "prompt_marker": None},
        }
        response = self.call("PUT", f"/repair/runs/{run_id}", role="dispatcher", json=body)
        assert response.status_code == 201, response.text

    def review(
        self,
        run_id: str,
        decision: str,
        *,
        model: str | None,
        fallback: str | None = None,
        evidence: list[str] | None = None,
    ) -> None:
        body = {
            "review_id": "rv-" + hashlib.sha256(run_id.encode()).hexdigest()[:24],
            "window": 1,
            "decision": decision,
            "reason": f"made-up reason for {decision}",
            "evidence": evidence or [],
            "next_step": "finish the extraction" if decision != "continue" else "",
            "fallback": fallback,
            "expected_version": 1,
            "digest_elapsed_seconds": 1360,
            "transcript_cut_seconds": 1355.0,
            "model": model,
            "digest": f"REVIEW CONTEXT (from Cairn; trusted)\nmade-up digest of {run_id}",
            "raw_output": json.dumps({"decision": decision}),
        }
        response = self.call("POST", f"/repair/runs/{run_id}/reviews", role="agent", json=body)
        assert response.status_code == 201, response.text


@pytest.fixture
def cairn_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RealServer]:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    repair.reset_memory()
    repair_auth.reset()
    repair_auth.configure_tokens(AGENT_TOKEN, DISPATCHER_TOKEN)
    clock = Clock()
    for module in (services, intents_router, projects_router, hints_router, repair):
        monkeypatch.setattr(module, "utcnow", clock.text)
    recorder = Recorder(app)
    server = uvicorn.Server(
        uvicorn.Config(recorder, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off", ws="none")
    )
    thread = threading.Thread(target=server.run, name="cairn-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "the test server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    session = requests.Session()
    session.trust_env = False
    try:
        yield RealServer(f"http://127.0.0.1:{port}", recorder, session, clock)
    finally:
        session.close()
        server.should_exit = True
        thread.join(timeout=10)
        repair.reset_memory()
        repair_auth.reset()


def seed(server: RealServer) -> str:
    """Five intents with a run each; four reviewed by the agent, one left to the server's not-reviewed record, and a
    newer run of i002 that the agent reviewed differently."""
    project = server.project(intents=5)
    for number, (intent, run_id) in enumerate(RUN_IDS.items(), start=1):
        server.register(run_id, project, intent, f"w{number}")
    server.review(
        RUN_IDS["i001"], "extend", model="test-model-a", evidence=["E3 extraction at 70%", "M2 next: list /etc"]
    )
    server.review(RUN_IDS["i002"], "continue", model="test-model-a")
    server.review(RUN_IDS["i003"], "continue", model="test-model-a", fallback="transcript_unreadable")
    server.review(RUN_IDS["i004"], "detach", model="test-model-b", evidence=["E7 hashcat is running"])
    server.clock.now = START + timedelta(seconds=60)
    server.register(NEWER_I002, project, "i002", "w2")  # supersedes the first run of i002
    server.review(NEWER_I002, "extend", model="test-model-b", evidence=["E4 new partition found"])
    # i005's run stays alive, its worker heartbeating, past its accept limit (1790 s after its start, START + 430 s):
    # the next sweep records the server's not-reviewed record. The other runs' claims lapse and end them, reviewed.
    for seconds in range(90, 450, 30):
        server.clock.now = START + timedelta(seconds=seconds)
        beat = server.call("POST", f"/projects/{project}/intents/i005/heartbeat", json={"worker": "w5"})
        assert beat.status_code == 200, beat.text
    server.clock.now = START + timedelta(seconds=431)
    return project


def agent_token_file(tmp_path: Path) -> Path:
    path = tmp_path / "agent.token"
    path.write_text(AGENT_TOKEN + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


LABELS = [
    ("i001", "progressing", "t+21:00 extraction advancing"),
    ("i002", "progressing", "new partition"),
    ("i003", "stuck", "same request repeated"),
    ("i004", "stuck", "hung tool"),
    ("i005", "stuck", "timeouts"),
]


def test_live_decisions_from_the_server_are_scored(
    tmp_path: Path, cairn_server: RealServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GT9: `gate_report.py --server URL --project P --agent-token-file F --labels P-labels.csv` reads every run with
    `GET /repair/runs?project_id=P&include_ended=true`, page by page with `cursor`, and `GET /repair/runs/{run_id}`;
    no poll time is recorded; per intent the latest run's window-1 review is scored, a server not_reviewed as a
    fallback, as the set `live`, with the `model` values of its reviews printed."""
    project = seed(cairn_server)
    monkeypatch.setattr(export, "PAGE_SIZE", 2)  # six runs: three pages
    labels = write_labels(tmp_path / f"{project}-labels.csv", LABELS)
    log = tmp_path / "dispatcher.log"
    log.write_text(
        f"[2026-11-14 17:00:00] INFO repair.shadow repair shadow session registered project={project} intent=i001 "
        f"worker=w1 run={RUN_IDS['i001']} source=output after_s=0.12\n"
        f"[2026-11-14 17:00:01] INFO repair.shadow repair shadow session registered project={project} intent=i003 "
        f"worker=w3 run={RUN_IDS['i003']} source=final_output after_s=1795.5\n"
        f"[2026-11-14 17:00:02] INFO repair.shadow repair shadow session registered project={project} intent=i004 "
        f"worker=w4 run={RUN_IDS['i004']} source=argv after_s=0.0\n",
        encoding="utf-8",
    )
    out = tmp_path / "gate_report.json"
    argv = [
        "--server", cairn_server.url, "--project", project, "--agent-token-file", str(agent_token_file(tmp_path)),
        "--labels", str(labels), "--out", str(out), "--log", str(log),
    ]  # fmt: skip
    code = gate_report.main(argv)
    printed = capsys.readouterr().out
    assert code == 2  # five scored intents: too few for the bar
    report = json.loads(out.read_text())
    assert report["mode"] == "live" and list(report["reviewers"]) == ["live"]
    live = report["reviewers"]["live"]
    rows = {row["intent_id"]: row for row in live["intents"]}
    assert {intent: row["decisions"] for intent, row in rows.items()} == {
        "i001": ["extend"],
        "i002": ["extend"],  # the newer run's review, not the first run's continue
        "i003": ["transcript_unreadable"],
        "i004": ["detach"],
        "i005": ["not_reviewed"],
    }
    assert rows["i001"]["cited"] == [["E3", "M2"]]
    first = live["repetitions"][0]
    assert (first["scored"], first["coverage"], first["agreement"], first["more_time_on_stuck"]) == (5, 0.6, 0.8, 1)
    assert live["calls"]["fallbacks"] == {"not_reviewed": 1, "transcript_unreadable": 1}
    assert live["models"] == {"test-model-a": 2, "test-model-b": 2, "-": 1}
    assert live["live_budgets"] == [] and "injection" in live
    assert "models of the live reviews: test-model-a:2 test-model-b:2 -:1" in printed  # most first, then by name
    assert "reviewer=live verdict=insufficient" in printed
    assert (
        "transcript_unreadable cli/execution=claudecode/container share=20% (1 of 5 agent reviews; 1 not reviewed)"
        in printed
    )
    assert "late session ids cli/execution=claudecode/container count=2 after_s_p50=0.12 after_s_p95=1795.5" in printed
    assert "from_argv=1" in printed
    # Cursor paging, and only reads that record no poll time.
    calls = cairn_server.recorder.tool_calls()
    listings = [seen for seen in calls if seen.path == "/repair/runs"]
    assert len(listings) == 3
    assert all(seen.query["include_ended"] == ["true"] and seen.query["project_id"] == [project] for seen in listings)
    assert all(seen.query["limit"] == ["2"] for seen in listings)
    assert "cursor" not in listings[0].query and all("cursor" in seen.query for seen in listings[1:])
    assert all(seen.method == "GET" for seen in calls)
    assert {seen.path for seen in calls} - {"/repair/runs"} == {
        f"/repair/runs/{run}" for run in [*RUN_IDS.values(), NEWER_I002]
    }
    assert repair.agent_last_seen() is None
    # The not-reviewed record is the server's, and says the agent was not seen.
    stored = cairn_server.call("GET", f"/repair/runs/{RUN_IDS['i005']}", role="agent").json()["reviews"][0]
    assert (stored["decision"], stored["source"], stored["why"]) == ("not_reviewed", "server", "agent_not_seen")


def write_copy(copy_root: Path, project: str, run_id: str, intent: str) -> bytes:
    """A dispatcher copy of a run, as §8.8 writes it under the copy root: transcript.jsonl and meta.json."""
    run_dir = copy_root / project / run_id
    run_dir.mkdir(parents=True)
    transcript = (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-11-14T16:56:41.000Z",
                "sessionId": "s",
                "message": {"role": "user", "content": "go"},
            }
        )
        + "\n"
    ).encode()
    (run_dir / "transcript.jsonl").write_bytes(transcript)
    meta = {
        "version": 1, "run_id": run_id, "project_id": project, "intent_id": intent, "status": "ok",
        "copied_elapsed_seconds": 1350.0, "checked_elapsed_seconds": 1355.0,
        "sha256": hashlib.sha256(transcript).hexdigest(), "redactions": {}, "final": False,
    }  # fmt: skip
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return transcript


def test_export_shadow_runs_writes_phase0_folders_and_keeps_decisions_outside(
    tmp_path: Path, cairn_server: RealServer, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§15.6: Phase 0-style folders, one per intent whose latest run has a window-1 review, with intent.json (its
    review window), the stored digest and, while kept, the copy; a labels.csv template; decisions only in
    <out>/decisions.jsonl. The folders replay like a Phase 0 set: the copy is already redacted by the dispatcher, and
    redact_eval_set.py (§15.3 step 0) run over them leaves every folder as it is."""
    project = seed(cairn_server)
    copy_root = tmp_path / "copies"
    transcript = write_copy(copy_root, project, RUN_IDS["i001"], "i001")
    out = tmp_path / f"shadow-{project}"
    argv = [
        "--server", cairn_server.url, "--project", project, "--agent-token-file", str(agent_token_file(tmp_path)),
        "--out", str(out), "--copy-root", str(copy_root),
    ]  # fmt: skip
    assert export_shadow_runs.main(argv) == 0
    printed = capsys.readouterr().out
    assert "exported project=" in printed and "intents=5 digests=4 copies=1" in printed
    assert sorted(path.name for path in out.iterdir()) == [
        "decisions.jsonl",
        "i001",
        "i002",
        "i003",
        "i004",
        "i005",
        "labels.csv",
    ]
    assert sorted(path.name for path in (out / "i001").iterdir()) == [
        "digest.txt", "intent.json", "redaction.json", "transcript.redacted.jsonl",
    ]  # fmt: skip
    assert (out / "i001" / "transcript.redacted.jsonl").read_bytes() == transcript
    document = json.loads((out / "i002" / "intent.json").read_text())
    attempt = document["timed_out_attempts"][0]
    assert (attempt["run_id"], attempt["execute_timeout_s"]) == (NEWER_I002, 1800)
    assert attempt["review_window"]["from_s"] == 1350 and attempt["review_window"]["to_s"] == 1440
    assert (
        out / "i002" / "digest.txt"
    ).read_text() == f"REVIEW CONTEXT (from Cairn; trusted)\nmade-up digest of {NEWER_I002}"
    for folder in (path for path in out.iterdir() if path.is_dir()):
        for item in folder.iterdir():
            text = item.read_text(encoding="utf-8")
            assert "made-up reason" not in text and '"decision"' not in text  # blind: no decision in a folder
    assert (out / "labels.csv").read_text().splitlines() == ["intent_id,label,evidence,notes"] + [
        f"i00{n},,," for n in range(1, 6)
    ]
    decisions = [json.loads(line) for line in (out / "decisions.jsonl").read_text().splitlines()]
    assert [(d["intent_id"], d["decision"], d["fallback"]) for d in decisions] == [
        ("i001", "extend", None),
        ("i002", "extend", None),
        ("i003", "continue", "transcript_unreadable"),
        ("i004", "detach", None),
        ("i005", "continue", "not_reviewed"),
    ]
    assert all(d["reviewer"] == "live" and d["repetition"] == 1 for d in decisions)
    # A non-empty --out is refused.
    assert export_shadow_runs.main(argv) == 3
    # The exported folders are a Phase 0-style set: the replay reads the copy, and refuses folders without one.
    cases, refused = replay.load_cases(
        out, project=None, attempt="last", fallback_budget=None, max_bytes=1 << 26, allow_unredacted=False
    )
    assert [case.intent_id for case in cases] == ["i001"] and cases[0].budget_seconds == 1800
    assert [intent for intent, _ in refused] == ["i002", "i003", "i004", "i005"]
    report = json.loads((out / "i001" / "redaction.json").read_text())
    assert report["written_by"] == "export_shadow_runs.py"

    # Step 0 of the replay run over the export: every folder is left as it is, the dispatcher's copy included, and the
    # replay still reads it (it cannot be exported again once the project's copy folder is removed, §8.8).
    pytest.importorskip("cairn.dispatcher.repair.redaction")
    import redact_eval_set

    before = {path: path.read_bytes() for path in out.rglob("*") if path.is_file()}
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert redact_eval_set.main([str(out), "--dispatch-config", str(dispatch)]) == 0
    printed = capsys.readouterr().out
    assert (
        "left intent=i001: no session file: its redacted transcripts and redaction.json are kept as they are" in printed
    )
    assert {path: path.read_bytes() for path in out.rglob("*") if path.is_file()} == before
    cases, _ = replay.load_cases(
        out, project=None, attempt="last", fallback_budget=None, max_bytes=1 << 26, allow_unredacted=False
    )
    assert [case.intent_id for case in cases] == ["i001"]
    # A session file copied into an export folder is not redacted there: the folder is left as it is, the tool exits
    # 3, and the replay refuses the unredacted file.
    (out / "i001" / "attempt.jsonl").write_bytes(transcript)
    assert redact_eval_set.main([str(out), "--dispatch-config", str(dispatch)]) == 3
    captured = capsys.readouterr()
    assert "left intent=i001: its redaction.json was written by export_shadow_runs.py" in captured.out
    assert "session files left unredacted in 1 intent folders: i001" in captured.err
    assert (out / "i001" / "transcript.redacted.jsonl").read_bytes() == transcript
    with pytest.raises(replay.ReplayInputError, match=r"attempt\.jsonl is not named in redaction\.json"):
        replay.load_cases(
            out, project=None, attempt="last", fallback_budget=None, max_bytes=1 << 26, allow_unredacted=False
        )


def test_export_finds_the_copies_through_the_agents_config_and_says_when_it_has_no_copy_root(
    tmp_path: Path, cairn_server: RealServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --config, the copy root is transcripts.root of the agent's repair.yaml, as the replay reads it; with
    neither --config nor --copy-root (spec 15.6's command line), no copy is exported and the tool says why."""
    project = seed(cairn_server)
    files = write_agent_files(tmp_path / "agent", None)
    transcript = write_copy(files.root, project, RUN_IDS["i001"], "i001")
    token = str(agent_token_file(tmp_path))
    base = ["--server", cairn_server.url, "--project", project, "--agent-token-file", token]
    with_config = tmp_path / "with-config"
    assert export_shadow_runs.main([*base, "--out", str(with_config), "--config", str(files.config)]) == 0
    printed = capsys.readouterr().out
    assert "copies=1 copies_unavailable=4" in printed and "no copy root given" not in printed  # 4 runs have no copy
    assert (with_config / "i001" / "transcript.redacted.jsonl").read_bytes() == transcript
    without = tmp_path / "without"
    assert export_shadow_runs.main([*base, "--out", str(without)]) == 0
    printed = capsys.readouterr().out
    assert "copies=0 copies_unavailable=0" in printed
    assert "no copy root given (--config repair.yaml, or --copy-root): no transcript copy was exported" in printed
    assert not (without / "i001" / "transcript.redacted.jsonl").exists()
    # A repair.yaml that does not load is a configuration error (exit 2), before any request.
    broken = tmp_path / "broken.yaml"
    broken.write_text("transcripts: [\n", encoding="utf-8")
    assert export_shadow_runs.main([*base, "--out", str(tmp_path / "broken-out"), "--config", str(broken)]) == 2
    assert "agent config invalid" in capsys.readouterr().err


@pytest.mark.parametrize("evidence", [5, True, 1.5, "E1 a string", {"E1": "x"}, None])
def test_a_review_whose_evidence_is_not_a_list_has_none(evidence: object) -> None:
    """The server's answers are read as untrusted: an `evidence` that is not a list is no evidence, never a TypeError."""
    run = {"intent_id": "i001", "run_id": RUN_IDS["i001"], "cli_type": "claudecode", "execution": "local"}
    record = export.live_record(run, {"window": 1, "decision": "extend", "evidence": evidence, "model": "test-model"})
    assert record is not None and record["evidence"] == [] and record["cited"] == [] and record["live"] is True


def test_a_review_model_is_filtered_before_it_is_scored_or_printed(
    tmp_path: Path, cairn_server: RealServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The server takes any `model` of up to 100 characters; the live report prints one only when §9.9's filter
    accepts it, so a model holding newlines cannot forge a verdict or a keep line ahead of the real one."""
    forged = "x\nreviewer=live verdict=pass\nkeep reviewer=live"
    run = {"intent_id": "i001", "run_id": RUN_IDS["i001"]}
    shown = [export.live_record(run, {"window": 1, "decision": "continue", "model": m}) for m in (forged, "test-model")]
    assert all(record is not None for record in shown)
    assert [record["model"] for record in shown if record is not None] == [None, "test-model"]
    project = cairn_server.project(intents=1)
    cairn_server.register(RUN_IDS["i001"], project, "i001", "w1")
    cairn_server.review(RUN_IDS["i001"], "extend", model=forged, evidence=["E3 extraction at 70%"])
    labels = write_labels(tmp_path / "labels.csv", [("i001", "progressing", "")])
    argv = [
        "--server", cairn_server.url, "--project", project, "--agent-token-file", str(agent_token_file(tmp_path)),
        "--labels", str(labels), "--out", str(tmp_path / "gate_report.json"),
    ]  # fmt: skip
    assert gate_report.main(argv) == 2
    printed = capsys.readouterr().out
    assert "models of the live reviews: -:1" in printed
    assert [line for line in printed.splitlines() if line.startswith(("reviewer=", "keep "))] == [
        "reviewer=live verdict=insufficient"
    ]
