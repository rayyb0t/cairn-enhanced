"""Transcript-copy tests of spec 16 for the removal of copies (build step 7, spec 8.8 Removal): TC18 and TC19.

The housekeeping thread's pass (repair/housekeeping.py remove_copies) runs against a fake Cairn server on 127.0.0.1,
on a fake clock where passes are counted, with copy folders made under a temporary copy root. Every value here is
synthetic.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from repair_dispatcher_runs import run_config
from repair_dispatcher_support import (
    FAKE_TOKEN,
    RecordedRequest,
    Reply,
    bypass_proxies,
    fake_server,
    json_reply,
    reset_repair_process_state,
    write_token,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.repair import client as repair_client
from cairn.dispatcher.repair import housekeeping, transcripts
from cairn.dispatcher.repair.client import RepairRunsClient
from cairn.dispatcher.scheduler import loop as loop_module
from cairn.dispatcher.scheduler.loop import DispatcherLoop

REMOVED = "repair shadow copies removed project=%s reason=%s count=%s"
NOT_REMOVED = "repair shadow copies not removed project=%s reason=%s error=%s"
NOT_FOUND_BODY = b'{"detail":"Project not found"}'
RUNS = ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_reporting", {})
    monkeypatch.setattr(housekeeping, "_not_removed_logged", set())
    monkeypatch.setattr(housekeeping, "_failed_steps", set())
    monkeypatch.setattr(loop_module, "_repair_module_failed", False)
    bypass_proxies(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cairn")


def summary(project_id: str, status: str = "active") -> dict[str, Any]:
    return {
        "id": project_id,
        "title": project_id,
        "status": status,
        "bootstrap_enabled": True,
        "created_at": "2026-11-14T10:00:00Z",
        "repair_enabled": True,
        "fact_count": 2,
        "intent_count": 0,
        "working_intent_count": 0,
        "unclaimed_intent_count": 0,
        "hint_count": 0,
    }


class CairnFake:
    """GET /projects and GET /projects/{id} as the housekeeping reads them; details maps an id to its answer."""

    def __init__(self, projects: list[dict[str, Any]] | None = None) -> None:
        self.projects = projects or []
        self.list_reply: Reply | None = None
        self.details: dict[str, Reply | Callable[[], Reply]] = {}

    def __call__(self, request: RecordedRequest) -> Reply:
        path = request.path.split("?", 1)[0]
        if path == "/projects":
            return self.list_reply or json_reply(200, self.projects)
        project_id = path.removeprefix("/projects/")
        answer = self.details.get(project_id, Reply(404, NOT_FOUND_BODY))
        return answer() if callable(answer) else answer


def make_copies(root: Path, project_id: str, runs: tuple[str, ...] = RUNS) -> Path:
    folder = root / project_id
    for run_id in runs:
        (folder / run_id).mkdir(parents=True)
        (folder / run_id / "transcript.jsonl").write_bytes(b'{"type":"x"}\n')
        (folder / run_id / "meta.json").write_bytes(b'{"version": 1}')
    (folder / runs[0] / "meta.json.tmp").write_bytes(b"{")  # a .tmp left by a failed write is a copy file too
    return folder


def config_for(tmp_path: Path, server: str, **kwargs: Any) -> DispatchConfig:
    token = write_token(tmp_path / "secrets" / "repair-dispatcher.token", FAKE_TOKEN)
    return run_config("codex", server, token, tmp_path / "copies", **kwargs)


def one_pass(config: DispatchConfig) -> None:
    client = RepairRunsClient(config.server)
    try:
        housekeeping.remove_copies(config, client)
    finally:
        client.close()


def detail_paths(requests_seen: list[RecordedRequest]) -> list[str]:
    return [request.path for request in requests_seen if request.path.startswith("/projects/")]


def records(caplog: pytest.LogCaptureFixture, fmt: str) -> list[tuple[Any, ...]]:
    return [tuple(record.args or ()) for record in caplog.records if record.msg == fmt]


class StopAfter:
    """housekeeping._STOP on a fake clock: each wait is one 60 s step, after which between() runs; n passes."""

    def __init__(self, passes: int, between: Callable[[int], None] | None = None) -> None:
        self.passes = passes
        self.between = between
        self.now = 0.0
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        self.now += float(timeout or 0.0)
        if len(self.waits) >= self.passes:
            return True
        if self.between is not None:
            self.between(len(self.waits))
        return False


# TC18 ----------------------------------------------------------------------------------------------------------------

KEPT_ANSWERS = {
    "not_found_route": Reply(404, b'{"detail":"Not Found"}'),
    "longer_body": Reply(404, NOT_FOUND_BODY + b" "),
    "unauthorized": Reply(401, b'{"detail":"no"}'),
    "server_error": Reply(500, NOT_FOUND_BODY),
}


def _tc18_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC18. proj_002, absent from GET /projects and answering GET /projects/proj_002 with a 404 and exactly
    {"detail":"Project not found"}, loses its folder at the next pass, with one "copies removed … reason=deleted
    count=<its run folders>". Neither request carries X-Cairn-Repair-Token; a project still in the list keeps its
    folder whatever its status; the passes run at start and then every 60 s on the fake clock.
    """
    root = tmp_path / "copies"
    fake = CairnFake([summary("proj_001", "stopped"), summary("proj_003", "completed")])
    with fake_server(fake) as server:
        config = config_for(tmp_path, server.base_url)  # container mode, completed_action: stop
        kept = [make_copies(root, "proj_001"), make_copies(root, "proj_003")]
        gone = make_copies(root, "proj_002")
        clock = StopAfter(3)
        monkeypatch.setattr(housekeeping, "_STOP", clock)
        housekeeping._housekeeping(config)  # token, shared keys, settings file, then 3 passes
        requests_seen = list(server.requests)
    assert not gone.exists() and all(folder.exists() for folder in kept)
    assert records(caplog, REMOVED) == [("proj_002", "deleted", 2)]
    assert clock.waits == [60.0, 60.0, 60.0]
    assert [request.path for request in requests_seen] == ["/projects", "/projects/proj_002", "/projects", "/projects"]
    assert all("x-cairn-repair-token" not in request.headers for request in requests_seen)
    assert sorted(os.listdir(root)) == ["agent-settings.json", "proj_001", "proj_003"]


def _tc18_kept(answer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC18. The folder is kept on a 404 with {"detail":"Not Found"} or a longer body, a 401, a 500, a connection
    error or a timeout; a 200 with a 10 MiB body keeps it too, and none of that body is read."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    fake = CairnFake([])
    responses: list[Any] = []
    if answer in KEPT_ANSWERS:
        fake.details["proj_002"] = KEPT_ANSWERS[answer]
    elif answer == "connection_error":

        def drop() -> Reply:
            raise ConnectionAbortedError("dropped without an answer")

        fake.details["proj_002"] = drop
    elif answer == "timeout":
        monkeypatch.setattr(repair_client, "PROJECT_READ_TIMEOUT_SECONDS", (2.0, 0.3))

        def slow() -> Reply:
            time.sleep(1.0)
            return Reply(404, NOT_FOUND_BODY)

        fake.details["proj_002"] = slow
    else:
        fake.details["proj_002"] = Reply(200, b"x" * (10 * 1024 * 1024))
        real_requests = RepairRunsClient._requests

        def recording(self: RepairRunsClient) -> Any:
            session = real_requests(self)
            if not getattr(session, "_recording", False):
                real_get = session.get

                def get(*args: Any, **kwargs: Any) -> Any:
                    response = real_get(*args, **kwargs)
                    responses.append(response)
                    return response

                session.get = get  # type: ignore[method-assign]
                session._recording = True  # type: ignore[attr-defined]
            return session

        monkeypatch.setattr(RepairRunsClient, "_requests", recording)
    with fake_server(fake) as server:
        one_pass(config_for(tmp_path, server.base_url))
        assert detail_paths(server.requests) == ["/projects/proj_002"]
    assert folder.exists() and sorted(os.listdir(folder)) == sorted(RUNS)
    assert records(caplog, REMOVED) == [] and records(caplog, NOT_REMOVED) == []
    if answer == "big_200":
        [_listing, detail] = responses
        assert detail.status_code == 200 and detail.raw.tell() == 0  # not one byte of the body was read


def _tc18_list_fails(listing: str, tmp_path: Path) -> None:
    """TC18. When GET /projects fails, or returns a body that does not parse as list[ProjectSummary], every folder is
    kept and no GET /projects/{id} is sent."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    fake = CairnFake([])
    fake.list_reply = {
        "status_500": Reply(500, b"[]"),
        "not_a_list": json_reply(200, {"projects": []}),
        "bad_summary": json_reply(200, [{"id": "proj_001"}]),
        "no_server": Reply(200, b"[]"),
    }[listing]
    with fake_server(fake) as server:
        base_url = "http://127.0.0.1:9" if listing == "no_server" else server.base_url
        one_pass(config_for(tmp_path, base_url))
        assert detail_paths(server.requests) == []
    assert folder.exists() and sorted(os.listdir(folder)) == sorted(RUNS)


def _tc18_reporting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC18. A deleted project's folder is kept while one of its reporter threads runs (between steps 1 and 9 of
    spec 8.5), with no request for it, and removed at the first pass after that thread's step 9."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    with fake_server(CairnFake([])) as server:
        config = config_for(tmp_path, server.base_url)
        transcripts.join_reporting("proj_002")
        seen_between: list[bool] = []

        def between(step: int) -> None:
            seen_between.append(folder.exists())
            if step == 1:
                transcripts.leave_reporting("proj_002")  # the reporter's step 9, before the second pass

        monkeypatch.setattr(housekeeping, "_STOP", StopAfter(3, between))
        housekeeping._housekeeping(config)
        requests_seen = [request.path for request in server.requests]
    assert seen_between == [True, False]
    assert not folder.exists()
    assert requests_seen == ["/projects", "/projects", "/projects/proj_002"]  # then no project folder: no request
    assert records(caplog, REMOVED) == [("proj_002", "deleted", 2)]


def _tc18_reporter_joins_meanwhile(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """TC18 (spec 8.8 Removal: the check and the removal hold the reporters' lock). A reporter thread of proj_002 that
    joins after the pass has planned the removal, here while GET /projects/proj_002 is answered, keeps the folder,
    with every copy in it; it is removed at the first pass after that thread's step 9."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    before = sorted(str(path) for path in folder.rglob("*"))
    fake = CairnFake([])
    answers = {"count": 0}

    def join_then_gone() -> Reply:
        answers["count"] += 1
        if answers["count"] == 1:
            transcripts.join_reporting("proj_002")  # the reporter's step 1, after the plan and before the removal
        return Reply(404, NOT_FOUND_BODY)

    fake.details["proj_002"] = join_then_gone
    with fake_server(fake) as server:
        config = config_for(tmp_path, server.base_url)
        one_pass(config)
        assert sorted(str(path) for path in folder.rglob("*")) == before
        one_pass(config)  # still being reported: kept, and not asked about
        assert sorted(str(path) for path in folder.rglob("*")) == before
        transcripts.leave_reporting("proj_002")  # the reporter's step 9
        one_pass(config)
        asked = detail_paths(server.requests)
    assert not folder.exists()
    assert asked == ["/projects/proj_002", "/projects/proj_002"]
    assert records(caplog, REMOVED) == [("proj_002", "deleted", 2)] and records(caplog, NOT_REMOVED) == []


def _tc18_removal_holds_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC18 (spec 8.8 Removal). The removal holds the lock reporter threads take to join the count: a reporter
    thread of proj_002 that starts while the folder is being removed joins only once the removal has ended."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    real_remove = housekeeping._remove_planned
    seen: dict[str, Any] = {}

    def removing(*args: Any) -> None:
        joined = threading.Event()

        def reporter() -> None:
            transcripts.join_reporting("proj_002")
            joined.set()

        thread = threading.Thread(target=reporter, name="tc18-reporter", daemon=True)
        seen["thread"], seen["joined"] = thread, joined
        thread.start()
        seen["joined_during_removal"] = joined.wait(0.3)
        real_remove(*args)

    monkeypatch.setattr(housekeeping, "_remove_planned", removing)
    with fake_server(CairnFake([])) as server:
        one_pass(config_for(tmp_path, server.base_url))
    seen["thread"].join(5)
    assert seen["joined_during_removal"] is False  # it waited for the removal to end
    assert seen["joined"].is_set() and transcripts.reporting_count("proj_002") == 1
    assert not folder.exists()


def _tc18_entry_added_meanwhile(where: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """TC18 (spec 8.8 Removal: exactly the files checked are unlinked, and folders are removed with rmdir). An entry
    added to a run folder, or a run folder added to the project folder, after the pass has planned the removal (here
    while GET /projects/proj_002 is answered) makes rmdir fail: the folder is kept with the added entry, with one
    "copies not removed … error=OSError" line."""
    root = tmp_path / "copies"
    folder = make_copies(root, "proj_002")
    if where == "file":
        added = folder / RUNS[1] / "notes.txt"
    else:
        added = folder / "00000000000000000000000000000000" / "meta.json"
    fake = CairnFake([])

    def add_then_gone() -> Reply:
        if not added.exists():
            added.parent.mkdir(exist_ok=True)
            added.write_text("added after the plan\n")
        return Reply(404, NOT_FOUND_BODY)

    fake.details["proj_002"] = add_then_gone
    with fake_server(fake) as server:
        one_pass(config_for(tmp_path, server.base_url))
        assert detail_paths(server.requests) == ["/projects/proj_002"]
    assert folder.is_dir() and added.read_text() == "added after the plan\n"
    assert records(caplog, NOT_REMOVED) == [("proj_002", "deleted", "OSError")]
    assert records(caplog, REMOVED) == []


def _tc18_only_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC18. A symlink named proj_003 that points outside the root, a file named like a project, a folder whose name
    fails the id rule and agent-settings.json are left alone. A symlink inside a project or run folder keeps that
    folder (not_copies) and its target is unchanged. eval-proj_027 holding labels.csv and i014/intent.json, docs
    holding a file, and a deleted project's folder whose run folder holds an extra file are each kept with one
    error=not_copies line, and no GET /projects/{id} is sent for any of them. A removal that raises logs "copies not
    removed" once and is retried at the next pass. Without repair:, nothing is requested or removed.
    """
    root = tmp_path / "copies"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside\n")
    root.mkdir()
    (root / "proj_003").symlink_to(outside, target_is_directory=True)
    (root / "proj_004").write_text("a file named like a project\n")
    make_copies(root, ".hidden")
    make_copies(root, "bad..name")
    (root / "agent-settings.json").write_text('{"version": 1}')
    (root / "agent-settings.json.tmp").write_text("{")
    eval_set = root / "eval-proj_027"
    (eval_set / "i014").mkdir(parents=True)
    (eval_set / "labels.csv").write_text("intent,label\n")
    (eval_set / "i014" / "intent.json").write_text("{}")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("notes\n")
    extra = make_copies(root, "proj_005")
    (extra / RUNS[1] / "notes.txt").write_text("not a copy\n")
    linked_run = make_copies(root, "proj_006", RUNS[:1])
    (linked_run / RUNS[1]).symlink_to(outside, target_is_directory=True)
    linked_file = make_copies(root, "proj_007", RUNS[:1])
    (linked_file / RUNS[0] / "transcript.jsonl").unlink()
    (linked_file / RUNS[0] / "transcript.jsonl").symlink_to(outside / "keep.txt")
    failing = make_copies(root, "proj_008")
    before = sorted(str(path) for path in root.rglob("*"))
    calls = {"remove": 0}
    real_remove = housekeeping._remove_planned

    def flaky(*args: Any) -> None:
        calls["remove"] += 1
        if calls["remove"] <= 2:
            raise PermissionError("injected")
        real_remove(*args)

    monkeypatch.setattr(housekeeping, "_remove_planned", flaky)
    with fake_server(CairnFake([])) as server:
        config = config_for(tmp_path, server.base_url)
        for _ in range(3):
            one_pass(config)
        asked = detail_paths(server.requests)
    assert asked == ["/projects/proj_008"] * 3  # only the folder that holds nothing but copies is asked about
    assert not failing.exists()
    after = sorted(str(path) for path in root.rglob("*"))
    assert after == [path for path in before if not path.startswith(str(failing))]
    assert (outside / "keep.txt").read_text() == "outside\n" and sorted(os.listdir(outside)) == ["keep.txt"]
    not_removed = records(caplog, NOT_REMOVED)
    assert sorted(not_removed) == sorted(
        [
            ("docs", "deleted", "not_copies"),
            ("eval-proj_027", "deleted", "not_copies"),
            ("proj_005", "deleted", "not_copies"),
            ("proj_006", "deleted", "not_copies"),
            ("proj_007", "deleted", "not_copies"),
            ("proj_008", "deleted", "PermissionError"),
        ]
    )  # each once, over three passes
    assert records(caplog, REMOVED) == [("proj_008", "deleted", 2)]

    # Without repair:, nothing is requested and nothing is removed.
    make_copies(root, "proj_009")
    with fake_server(CairnFake([])) as server:
        without = run_config("codex", server.base_url, tmp_path / "none", root, repair=False)
        one_pass(without)
        assert server.requests == []
    assert (root / "proj_009").exists()


def _tc18_work_folder(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """TC18. With transcript_root equal to local.workspace_root, a deleted project's work folder, which today's code
    keeps, is kept: it holds more than copies, and no GET /projects/{id} is sent for it."""
    root = tmp_path / "copies"
    work = root / "proj_010"
    (work / "src").mkdir(parents=True)
    (work / "src" / "exploit.py").write_text("print('x')\n")
    (work / "notes.txt").write_text("found\n")
    with fake_server(CairnFake([])) as server:
        config = config_for(tmp_path, server.base_url, execution="local")
        assert config.local is not None
        config.local.workspace_root = str(root)
        one_pass(config)
        assert detail_paths(server.requests) == []
    assert (work / "src" / "exploit.py").exists() and (work / "notes.txt").exists()
    assert records(caplog, NOT_REMOVED) == [("proj_010", "deleted", "not_copies")]


def _tc18_empty_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """TC18 (spec 8.8 Removal: a transcript_root that names a folder holding other data loses nothing). A folder with
    no entry holds no copies: nothing in it shows that this dispatcher made it. Empty folders whose names pass the id
    rule and that are no project of the server, such as a home folder's Music and Templates or, with transcript_root
    equal to local.workspace_root, a deleted project's empty work folder, are kept, with no line, and no
    GET /projects/{id} is sent for them; while the root holds no other such folder, a pass sends no request at all. A
    deleted project's copy folder beside them is still removed, and a folder that held an entry when the root was
    listed and holds none when its removal is planned is kept too.
    """
    root = tmp_path / "copies"
    empty = [root / name for name in ("Music", "Templates", "proj_014")]
    for folder in empty:
        folder.mkdir(parents=True)
    (root / ".cache").mkdir()
    with fake_server(CairnFake([])) as server:
        config = config_for(tmp_path, server.base_url, execution="local")
        assert config.local is not None
        config.local.workspace_root = str(root)
        for _ in range(2):
            one_pass(config)
        assert server.requests == []  # no folder that can hold copies: no request
        gone = make_copies(root, "proj_002")
        one_pass(config)
        assert [request.path for request in server.requests] == ["/projects", "/projects/proj_002"]
        assert not gone.exists()
        # Emptied between the root's listing and the plan: kept, and not asked about.
        monkeypatch.setattr(housekeeping, "_project_folders", lambda root_fd: ["Music", "proj_014"])
        one_pass(config)
        asked = detail_paths(server.requests)
    assert asked == ["/projects/proj_002"]
    assert all(folder.is_dir() and not any(folder.iterdir()) for folder in empty)
    assert sorted(os.listdir(root)) == [".cache", "Music", "Templates", "proj_014"]
    assert records(caplog, REMOVED) == [("proj_002", "deleted", 2)]
    assert records(caplog, NOT_REMOVED) == []


TC18_CASES = [
    "removed",
    *(f"kept-{answer}" for answer in (*KEPT_ANSWERS, "connection_error", "timeout", "big_200")),
    *(f"list-{listing}" for listing in ("status_500", "not_a_list", "bad_summary", "no_server")),
    "reporting",
    "race-reporter_joins",
    "race-removal_holds_lock",
    "race-added_file",
    "race-added_run",
    "only_copies",
    "work_folder",
    "empty_folders",
]


@pytest.mark.parametrize("case", TC18_CASES)
def test_copies_of_a_deleted_project_are_removed_after_its_404(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC18, case by case: removed after the exact 404 (_tc18_removed); kept on any other answer (_tc18_kept) or when
    the project list fails (_tc18_list_fails); kept while a run of the project is being reported (_tc18_reporting),
    also when its reporter joins after the removal was planned (_tc18_reporter_joins_meanwhile), and a reporter that
    starts during the removal waits for it (_tc18_removal_holds_the_lock); an entry added after the plan keeps the
    folder (_tc18_entry_added_meanwhile); only folders that hold nothing but copies, never a symlink, a file or
    another folder (_tc18_only_copies); a work folder in the copy root kept (_tc18_work_folder); empty folders kept and
    never asked about (_tc18_empty_folders)."""
    if case == "removed":
        _tc18_removed(tmp_path, monkeypatch, caplog)
    elif case.startswith("kept-"):
        _tc18_kept(case.removeprefix("kept-"), tmp_path, monkeypatch, caplog)
    elif case.startswith("list-"):
        _tc18_list_fails(case.removeprefix("list-"), tmp_path)
    elif case == "reporting":
        _tc18_reporting(tmp_path, monkeypatch, caplog)
    elif case == "race-reporter_joins":
        _tc18_reporter_joins_meanwhile(tmp_path, caplog)
    elif case == "race-removal_holds_lock":
        _tc18_removal_holds_the_lock(tmp_path, monkeypatch)
    elif case.startswith("race-added_"):
        _tc18_entry_added_meanwhile(case.removeprefix("race-added_"), tmp_path, caplog)
    elif case == "only_copies":
        _tc18_only_copies(tmp_path, monkeypatch, caplog)
    elif case == "empty_folders":
        _tc18_empty_folders(tmp_path, monkeypatch, caplog)
    else:
        _tc18_work_folder(tmp_path, caplog)


# TC19 ----------------------------------------------------------------------------------------------------------------


class RecordingBackend:
    """A ContainerManager or LocalBackend stand-in that records the cleanup calls the scheduler makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def container_name(self, project_id: str) -> str:
        self.calls.append(("container_name", project_id))
        return f"cairn-dispatch-{project_id}"

    def needs_completed_cleanup(self, project_id: str) -> bool:
        self.calls.append(("needs_completed_cleanup", project_id))
        return True

    def cleanup_completed(self, project_id: str) -> bool:
        self.calls.append(("cleanup_completed", project_id))
        return True

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        self.calls.append(("needs_stopped_cleanup", project_id))
        return False

    def cleanup_stopped(self, project_id: str) -> bool:
        self.calls.append(("cleanup_stopped", project_id))
        return True


def scheduler_cleanups(config: DispatchConfig, *, iterations: int = 2) -> list[tuple[str, str]]:
    """The cleanup part of DispatcherLoop.run, iterations times, with a recording backend: _start_repair_side, then
    per iteration _reap_cleanup_futures, the project list, _refresh_runtime_projects, _cancel_inactive_tasks and
    _queue_container_cleanups."""
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = config
    loop.client = CairnClient(config.server)
    backend = RecordingBackend()
    loop.container_manager = backend  # type: ignore[assignment]
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.runtime_project_ids = set()
    loop._inactive_cleanup_done = {}
    loop._cleanup_pending = set()
    loop._log_state = {}
    try:
        loop_module._start_repair_side(config)
        for _ in range(iterations):
            loop._reap_cleanup_futures()
            summaries = loop.client.list_projects()
            loop._refresh_runtime_projects(summaries)
            loop._cancel_inactive_tasks(summaries)
            loop._queue_container_cleanups(summaries)
            for future in list(loop.cleanup_futures):
                future.result(10)
    finally:
        loop.cleanup_executor.shutdown(wait=True)
        loop.client.close()
    return backend.calls


@pytest.mark.parametrize("mode", ["container-remove", "container-stop", "local-remove", "local-keep"])
def test_copies_of_a_completed_project_follow_completed_action(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC19. A completed project's folder is removed, with reason completed, under completed_action: remove only,
    and kept under stop and keep, with no GET /projects/{id}; a stopped project keeps its folder, and so does a
    project reopened before the pass (active again). The scheduler's cleanup calls on a recording backend are
    exactly those of the same loop run without repair:.
    """
    execution, action = mode.split("-")
    root = tmp_path / "copies"
    completed = make_copies(root, "proj_011")
    stopped = make_copies(root, "proj_012")
    reopened = make_copies(root, "proj_013")
    fake = CairnFake([summary("proj_011", "completed"), summary("proj_012", "stopped"), summary("proj_013", "active")])
    first_pass = threading.Event()
    stop = threading.Event()
    real_removal = housekeeping.remove_copies

    def removal(config: DispatchConfig, client: Any) -> None:
        real_removal(config, client)
        first_pass.set()

    monkeypatch.setattr(housekeeping, "remove_copies", removal)
    monkeypatch.setattr(housekeeping, "_STOP", stop)
    calls: dict[bool, list[tuple[str, str]]] = {}
    with fake_server(fake) as server:
        for repair in (False, True):
            config = config_for(tmp_path, server.base_url, execution=execution, repair=repair)
            if execution == "container":
                assert config.container is not None
                config.container.completed_action = action  # type: ignore[assignment]
            else:
                assert config.local is not None
                config.local.completed_action = action  # type: ignore[assignment]
            try:
                calls[repair] = scheduler_cleanups(config)
                if repair:
                    assert first_pass.wait(10)
            finally:
                stop.set()
        asked = detail_paths(server.requests)
    assert calls[True] == calls[False]
    assert ("cleanup_completed", "proj_011") in calls[False]
    assert asked == []
    assert stopped.exists() and reopened.exists()
    if action == "remove":
        assert not completed.exists()
        assert records(caplog, REMOVED) == [("proj_011", "completed", 2)]
    else:
        assert completed.exists() and records(caplog, REMOVED) == []


@pytest.fixture(autouse=True)
def _no_threads_left() -> Iterator[None]:
    yield
    deadline = time.monotonic() + 5
    while any(thread.name == "repair-housekeeping" for thread in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.01)
