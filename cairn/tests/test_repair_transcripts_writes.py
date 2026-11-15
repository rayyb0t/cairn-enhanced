"""Transcript-copy tests of spec 16 for the writes below the copy root (build step 7, spec 8.8 Write): TC16 and TC20.

TC16 needs other users: it runs only as root, and runs the writer and the reader in forked children that take
other uids and groups. Every value here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repair_dispatcher_support import FAKE_TOKEN, reset_repair_process_state
from test_repair_transcripts_support import (
    COPY_FAILED,
    GROUP_NOT_SET,
    RUN_ID,
    SESSION_IDS,
    FakeClock,
    FakeDockerApi,
    FakeFile,
    container_session_path,
    copy_config,
    copy_lines,
    fixture_bytes,
    install_docker,
    make_run,
    read_meta,
)

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import housekeeping, redaction, transcripts
from cairn.dispatcher.repair.transcripts import CopySchedule


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reset_repair_process_state(monkeypatch)
    monkeypatch.setattr(transcripts, "_group_not_set_logged", False)
    monkeypatch.setattr(redaction, "_oversized_logged", set())
    caplog.set_level(logging.DEBUG, logger="cairn")


def local_codex(tmp_path: Path, *, root: Path | None = None) -> DispatchConfig:
    """A local-mode codex run's dispatch.yaml, with the run's session file in place under a temporary HOME."""
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
    sessions = home / ".codex" / "sessions" / "2026" / "11" / "14"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"rollout-x-{SESSION_IDS['codex']}.jsonl").write_bytes(fixture_bytes("codex"))
    config = copy_config(tmp_path, "codex", execution="local", workers=workers)
    if root is not None:
        assert config.repair is not None
        config.repair.transcript_root = str(root)
    return config


def tick(config: DispatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **run: Any) -> CopySchedule:
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(
        make_run(config, container=str(tmp_path), session_id=SESSION_IDS["codex"], **run), FAKE_TOKEN
    )
    clock.at(1260)
    schedule.tick()
    return schedule


# TC20 ----------------------------------------------------------------------------------------------------------------


def _tc20_symlinked_folder(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC20. A symlink at the root, at <root>/<project_id> or at <root>/<project_id>/<run_id>, each to a folder
    outside the root, refuses the write with "repair shadow transcript copy failed … reason=unsafe_path", and nothing
    is written outside the root.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "copies"
    if where == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif where == "project":
        root.mkdir()
        (root / "proj_027").symlink_to(outside, target_is_directory=True)
    else:
        (root / "proj_027").mkdir(parents=True)
        (root / "proj_027" / RUN_ID).symlink_to(outside, target_is_directory=True)
    config = local_codex(tmp_path, root=root)
    schedule = tick(config, tmp_path, monkeypatch)
    clock = FakeClock(monkeypatch)
    clock.at(1275)
    schedule.tick()  # tried again at the next tick, and refused again; logged once
    assert list(outside.iterdir()) == []
    assert copy_lines(caplog, COPY_FAILED) == [("proj_027", "i014", RUN_ID, "unsafe_path")]


def _tc20_symlinked_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TC20. A symlink in place of transcript.jsonl or meta.json is replaced by os.replace, and its target, a file
    outside the root, is unchanged; a symlink in place of a .tmp file is removed, never followed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    targets = {name: outside / f"{name}.target" for name in ("transcript.jsonl", "meta.json", "meta.json.tmp")}
    for target in targets.values():
        target.write_text("outside, unchanged\n")
    folder = tmp_path / "copies" / "proj_027" / RUN_ID
    folder.mkdir(parents=True)
    for name, target in targets.items():
        (folder / name).symlink_to(target)
    config = local_codex(tmp_path)
    tick(config, tmp_path, monkeypatch)
    for name, target in targets.items():
        assert target.read_text() == "outside, unchanged\n", name
    for name in ("transcript.jsonl", "meta.json"):
        assert not (folder / name).is_symlink() and stat.S_ISREG(os.lstat(folder / name).st_mode)
    assert not (folder / "meta.json.tmp").exists() and not (folder / "meta.json.tmp").is_symlink()
    assert (folder / "transcript.jsonl").read_bytes() == fixture_bytes("codex")
    assert read_meta(config)["status"] == "ok"


def _tc20_bad_project_id(
    project_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC20. A run whose project_id fails the id rule of spec 8.7 gets reason=bad_project_id and no copy: no locate,
    no Docker call, nothing written."""
    config = copy_config(tmp_path, "codex")
    api = install_docker(
        monkeypatch,
        FakeDockerApi({container_session_path("codex", SESSION_IDS["codex"]): FakeFile(fixture_bytes("codex"))}),
    )
    clock = FakeClock(monkeypatch)
    schedule = CopySchedule(make_run(config, project_id=project_id, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
    clock.at(1260)
    schedule.tick()
    assert schedule.next_due() is None
    run = schedule.run
    run.exited_monotonic = run.started_monotonic + 1500
    schedule.final_copy()
    assert api.calls == []
    assert not (tmp_path / "copies").exists()
    assert copy_lines(caplog, COPY_FAILED) == [(project_id, "i014", RUN_ID, "bad_project_id")]


TC20_CASES = [
    "root",
    "project",
    "run",
    "copy_files",
    *(f"bad_id-{value}" for value in ("../x", "a/b", ".hidden", "proj_027\n")),
]


@pytest.mark.parametrize("case", TC20_CASES)
def test_copy_writes_never_follow_a_symlink_below_the_root(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC20, case by case: a symlink at the root, the project folder or the run folder (_tc20_symlinked_folder); a
    symlink in place of a copy file (_tc20_symlinked_file); a project_id that fails the id rule
    (_tc20_bad_project_id)."""
    if case in ("root", "project", "run"):
        _tc20_symlinked_folder(case, tmp_path, monkeypatch, caplog)
    elif case == "copy_files":
        _tc20_symlinked_file(tmp_path, monkeypatch)
    else:
        _tc20_bad_project_id(case.removeprefix("bad_id-"), tmp_path, monkeypatch, caplog)


# TC16 ----------------------------------------------------------------------------------------------------------------

WRITER, READER, OUTSIDER = 61001, 61002, 61003
GROUP = 61100  # the copy root's group, which the agent's user holds


def as_user(uid: int, gid: int, groups: list[int], umask: int, function: Callable[[], Any]) -> Any:
    """function() in a forked child that runs as uid, gid and groups under umask; its JSON result, or AssertionError."""
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():  # the child only runs function: no lock another thread holds is taken
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:  # the child: report, and never return into pytest
        status = 1
        try:
            os.close(read_fd)
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            os.umask(umask)
            payload: Any = {"result": function()}
            status = 0
        except BaseException as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            os.write(write_fd, json.dumps(payload).encode())
        finally:
            os._exit(status)
    os.close(write_fd)
    chunks = []
    while chunk := os.read(read_fd, 65536):
        chunks.append(chunk)
    os.close(read_fd)
    _pid, status = os.waitpid(pid, 0)
    report = json.loads(b"".join(chunks) or b"{}")
    assert status == 0 and "result" in report, report
    return report["result"]


@pytest.fixture
def shared_base() -> Iterator[Path]:
    """A folder other users can reach (pytest's own tmp_path is root's alone)."""
    base = Path(tempfile.mkdtemp(prefix="cairn-tc16-", dir="/tmp"))
    os.chmod(base, 0o755)  # noqa: S103 - other test users must reach it
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def writer_copies(config: DispatchConfig, workdir: str) -> Callable[[], Any]:
    """In the writer's child: one tick of a copy, and the repair log lines it gave."""

    def write() -> Any:
        messages: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        logger = logging.getLogger("cairn.dispatcher.repair")
        logger.addHandler(Collect())
        schedule = CopySchedule(make_run(config, container=workdir, session_id=SESSION_IDS["codex"]), FAKE_TOKEN)
        schedule.tick()
        return messages

    return write


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to run the writer and the reader as other users")
@pytest.mark.parametrize("umask", [0o077, 0o022])
@pytest.mark.parametrize("root_mode", [0o2750, 0o750])
def test_copy_is_readable_by_the_roots_group(
    umask: int, root_mode: int, shared_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC16. With real uids: a copy root of another group (2750, and also 0750: setgid or not), and a writer that is
    in that group, under umask 077 and 022: the project and run folders are 0750 and the files 0640, all with the
    root's group, and a process holding only that group reads the transcript; the root itself is left as it was
    (spec 8.8 Write: an existing root is never changed). A writer outside the group gets one "repair shadow
    transcript copy group not set" line, no exception, and its copy is kept.
    """
    FakeClock(monkeypatch).at(1260)  # inherited by the children
    config = local_codex(shared_base)
    for path in [shared_base, *shared_base.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    root = shared_base / "copies"
    root.mkdir()
    os.chown(root, WRITER, GROUP)
    os.chmod(root, root_mode)
    messages = as_user(WRITER, WRITER, [GROUP], umask, writer_copies(config, str(shared_base)))
    assert not any("group not set" in message for message in messages)
    folder = root / "proj_027" / RUN_ID
    for item, mode in (
        (root, root_mode),  # unchanged, its setgid bit included
        (root / "proj_027", 0o750),
        (folder, 0o750),
        (folder / "transcript.jsonl", 0o640),
        (folder / "meta.json", 0o640),
    ):
        info = os.stat(item)
        assert (stat.S_IMODE(info.st_mode), info.st_gid, info.st_uid) == (mode, GROUP, WRITER), item

    def read() -> Any:
        data = (folder / "transcript.jsonl").read_bytes()
        meta = json.loads((folder / "meta.json").read_bytes())
        return [hashlib.sha256(data).hexdigest(), meta["sha256"]]

    digest, recorded = as_user(READER, GROUP, [], 0o022, read)
    assert digest == recorded == hashlib.sha256(fixture_bytes("codex")).hexdigest()

    # A writer outside the root's group: the group cannot be set, which is logged once; the copy is still made.
    other_root = shared_base / "other-copies"
    other_root.mkdir()
    os.chown(other_root, OUTSIDER, GROUP)
    os.chmod(other_root, 0o750)  # noqa: S103 - the copy root of the test, as spec 13 sets it
    assert config.repair is not None
    config.repair.transcript_root = str(other_root)
    messages = as_user(OUTSIDER, OUTSIDER, [], umask, writer_copies(config, str(shared_base)))
    assert [message for message in messages if "group not set" in message] == [
        f"repair shadow transcript copy group not set root_gid={GROUP}"
    ]
    kept = other_root / "proj_027" / RUN_ID
    assert (kept / "transcript.jsonl").read_bytes() == fixture_bytes("codex")
    assert os.stat(kept / "transcript.jsonl").st_gid == OUTSIDER
    assert json.loads((kept / "meta.json").read_bytes())["status"] == "ok"
    info = os.stat(other_root)
    assert (stat.S_IMODE(info.st_mode), info.st_gid, info.st_uid) == (0o750, GROUP, OUTSIDER)
    assert GROUP_NOT_SET.startswith("repair shadow transcript copy group not set")


# The copy root's own mode (spec 8.8 Write) --------------------------------------------------------------------------


@pytest.mark.parametrize("writer", ["copy", "settings"])
def test_a_copy_root_the_dispatcher_creates_is_0750_and_an_existing_one_is_left_as_it_is(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 8.8 Write, as any user (TC16 checks a 2750 root of another group, as root). A copy root that is missing
    is created 0750 whatever the umask, at the first copy or at start for the agent's settings file (spec 8.3); an
    existing root keeps its mode, here 0755, whatever the dispatcher writes in it."""
    for state, existing, expected in (("missing", None, 0o750), ("existing", 0o755, 0o755)):
        reset_repair_process_state(monkeypatch)
        base = tmp_path / state
        config = local_codex(base)
        assert config.repair is not None
        root = Path(config.repair.transcript_root)
        if existing is not None:
            root.mkdir()
            os.chmod(root, existing)
        previous = os.umask(0o077)
        try:
            if writer == "copy":
                tick(config, base, monkeypatch)
            else:
                housekeeping.write_agent_settings(config, None)
        finally:
            os.umask(previous)
        assert stat.S_IMODE(os.stat(root).st_mode) == expected, state
        if writer == "copy":
            assert read_meta(config)["status"] == "ok", state
        else:
            assert json.loads((root / transcripts.SETTINGS_NAME).read_bytes())["version"] == 1, state
