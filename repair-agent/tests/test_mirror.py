"""Reading the dispatcher's transcript copy (spec §9.5): AG6, AG21 and AG22.

The agent reads exactly `<root>/<project_id>/<run_id>/meta.json` and `transcript.jsonl`, both ids from the run's
`RunView`, and the settings file at the root; never the server's `locator`, never through a symlink, never a FIFO, and
never more than its limits. Every copy here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import IS_ROOT, OTHER_GID, OTHER_UID, Layout, run_in_child
from records import CODEX, GOLDEN_COPY_PROJECT, REPOSITORY, RUN_ID, fixture_bytes

from cairn_repair_agent import config, mirror, transcripts
from cairn_repair_agent.mirror import MirrorCopy, Unavailable, read_copy

PROJECT = "proj_001"
MAX_BYTES = 64 * 1024 * 1024
# Build step 7's tests of the dispatcher's copy, TC12 among them, which writes the fixture AG22 reads.
STEP_7_TESTS = REPOSITORY / "cairn" / "tests" / "test_repair_transcripts.py"


def write_copy(root: Path, project_id: str, run_id: str, data: bytes, /, **meta_changes: Any) -> Path:
    """A copy as the dispatcher writes it (§8.8): the transcript, then its meta.json."""
    folder = root / project_id / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.jsonl").write_bytes(data)
    meta: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "intent_id": "i014",
        "worker": "codex-1",
        "cli_type": "codex",
        "status": "ok",
        "copied_at": "2026-11-14T10:22:16Z",
        "copied_elapsed_seconds": 1336.2,
        "checked_at": "2026-11-14T10:22:31Z",
        "checked_elapsed_seconds": 1351.2,
        "source_path": "/home/kali/.codex/sessions/2026/11/14/rollout-x.jsonl",
        "source_bytes": len(data),
        "copied_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "redactions": {"OPENAI_API_KEY": 0},
        "copies": 6,
        "final": False,
        "located_by": "session_id",
    }
    meta.update(meta_changes)
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def unavailable(result: MirrorCopy | Unavailable) -> str:
    assert isinstance(result, Unavailable), result
    return result.reason


# --- AG21 -------------------------------------------------------------------------------------------------------------


def _bad_run_id(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x")
    return PROJECT, "../etc"


def _run_id_with_a_trailing_newline(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x")
    return PROJECT, RUN_ID + "\n"  # re.match with $ would accept it


def _project_id_with_a_slash(root: Path) -> tuple[str, str]:
    return "proj/001", RUN_ID


def _project_id_with_two_dots(root: Path) -> tuple[str, str]:
    return "proj..001", RUN_ID


def _project_id_with_a_trailing_newline(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x")
    return PROJECT + "\n", RUN_ID


def _missing(root: Path) -> tuple[str, str]:
    return PROJECT, RUN_ID


def _project_folder_is_a_symlink(root: Path) -> tuple[str, str]:
    write_copy(root.parent / "elsewhere", PROJECT, RUN_ID, b"x")
    os.symlink(root.parent / "elsewhere" / PROJECT, root / PROJECT)
    return PROJECT, RUN_ID


def _run_folder_is_a_symlink(root: Path) -> tuple[str, str]:
    target = write_copy(root.parent / "elsewhere", PROJECT, RUN_ID, b"x")
    (root / PROJECT).mkdir()
    os.symlink(target, root / PROJECT / RUN_ID)
    return PROJECT, RUN_ID


def _transcript_is_a_symlink(root: Path) -> tuple[str, str]:
    secret = root.parent / "models.json"
    secret.write_text('{"apiKey": "fake-not-for-the-agent"}')
    folder = write_copy(root, PROJECT, RUN_ID, secret.read_bytes())
    (folder / "transcript.jsonl").unlink()
    os.symlink(secret, folder / "transcript.jsonl")
    return PROJECT, RUN_ID


def _hash_mismatch(root: Path) -> tuple[str, str]:
    folder = write_copy(root, PROJECT, RUN_ID, b"x")
    (folder / "transcript.jsonl").write_bytes(b"y")
    return PROJECT, RUN_ID


def _too_large(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"z" * 2000)
    return PROJECT, RUN_ID


def _status_too_large(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", status="too_large")
    return PROJECT, RUN_ID


def _status_unknown(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", status="ok\nrepair review posted run=x")
    return PROJECT, RUN_ID


def _status_not_a_string(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", status=["error"])
    return PROJECT, RUN_ID


def _meta_of_another_run(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", run_id="ffffffffffffffffffffffffffffffff")
    return PROJECT, RUN_ID


def _meta_of_another_project(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", project_id="proj_002")
    return PROJECT, RUN_ID


def _meta_nested_30000_deep(root: Path) -> tuple[str, str]:
    folder = write_copy(root, PROJECT, RUN_ID, b"x")
    deep = "[" * 30_000 + "]" * 30_000
    assert len(deep) < mirror.META_MAX_BYTES
    (folder / "meta.json").write_text(deep)
    return PROJECT, RUN_ID


def _meta_version_true(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", version=True)
    return PROJECT, RUN_ID


def _checked_missing(root: Path) -> tuple[str, str]:
    folder = write_copy(root, PROJECT, RUN_ID, b"x")
    meta = json.loads((folder / "meta.json").read_text())
    del meta["checked_elapsed_seconds"]
    (folder / "meta.json").write_text(json.dumps(meta))
    return PROJECT, RUN_ID


def _checked_a_string(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds="1351.2")
    return PROJECT, RUN_ID


def _checked_a_boolean(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=True)
    return PROJECT, RUN_ID


def _copied_infinite(root: Path) -> tuple[str, str]:
    folder = write_copy(root, PROJECT, RUN_ID, b"x")
    meta = (
        (folder / "meta.json")
        .read_text()
        .replace('"copied_elapsed_seconds": 1336.2', '"copied_elapsed_seconds": Infinity')
    )
    (folder / "meta.json").write_text(meta)
    return PROJECT, RUN_ID


# JSON integers have no size limit, and json.loads reads up to 4,300 digits: one of 400 digits, far under meta.json's
# 64 KiB, is too large for a float, so it is no finite number (converting it raises OverflowError).
def _copied_400_digits(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", copied_elapsed_seconds=10**400)
    return PROJECT, RUN_ID


def _checked_400_digits(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=10**400)
    return PROJECT, RUN_ID


def _checked_400_digits_negative(root: Path) -> tuple[str, str]:
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=-(10**400))
    return PROJECT, RUN_ID


AG21_CASES = {
    "bad_run_id": (_bad_run_id, "bad_run_id"),
    "run_id_with_a_trailing_newline": (_run_id_with_a_trailing_newline, "bad_run_id"),
    "project_id_with_a_slash": (_project_id_with_a_slash, "bad_project_id"),
    "project_id_with_two_dots": (_project_id_with_two_dots, "bad_project_id"),
    "project_id_with_a_trailing_newline": (_project_id_with_a_trailing_newline, "bad_project_id"),
    "missing": (_missing, "missing"),
    "project_folder_is_a_symlink": (_project_folder_is_a_symlink, "unsafe_path"),
    "run_folder_is_a_symlink": (_run_folder_is_a_symlink, "unsafe_path"),
    "transcript_is_a_symlink": (_transcript_is_a_symlink, "unsafe_path"),
    "hash_mismatch": (_hash_mismatch, "hash_mismatch"),
    "too_large": (_too_large, "too_large"),
    "status_too_large": (_status_too_large, "not_ok:too_large"),
    "status_unknown": (_status_unknown, "not_ok:other"),
    "status_not_a_string": (_status_not_a_string, "not_ok:other"),
    "meta_of_another_run": (_meta_of_another_run, "meta_invalid"),
    "meta_of_another_project": (_meta_of_another_project, "meta_invalid"),
    "meta_nested_30000_deep": (_meta_nested_30000_deep, "meta_invalid"),
    "meta_version_true": (_meta_version_true, "meta_invalid"),
    "checked_missing": (_checked_missing, "meta_invalid"),
    "checked_a_string": (_checked_a_string, "meta_invalid"),
    "checked_a_boolean": (_checked_a_boolean, "meta_invalid"),
    "copied_infinite": (_copied_infinite, "meta_invalid"),
    "copied_400_digits": (_copied_400_digits, "meta_invalid"),
    "checked_400_digits": (_checked_400_digits, "meta_invalid"),
    "checked_400_digits_negative": (_checked_400_digits_negative, "meta_invalid"),
}


@pytest.mark.parametrize("case", list(AG21_CASES))
def test_mirror_refuses_unsafe_or_stale_copies(tmp_path: Path, case: str) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    prepare, reason = AG21_CASES[case]
    project_id, run_id = prepare(root)
    slept: list[float] = []
    result = read_copy(str(root), project_id, run_id, 1000, sleep=slept.append)
    assert unavailable(result) == reason
    # A mismatch is read again once, after a second, in case a copy landed between the two reads.
    assert slept == ([mirror.HASH_RETRY_SECONDS] if reason == "hash_mismatch" else [])


@pytest.mark.parametrize("name", ["transcript.jsonl", "meta.json"])
def test_mirror_refuses_unsafe_or_stale_copies_fifo(tmp_path: Path, name: str) -> None:
    """A FIFO is refused within 1 s: it is opened with O_NONBLOCK, so no writer is waited for."""
    root = tmp_path / "transcripts"
    folder = write_copy(root, PROJECT, RUN_ID, b"x")
    (folder / name).unlink()
    os.mkfifo(folder / name)
    result: list[MirrorCopy | Unavailable] = []
    began = time.monotonic()
    worker = threading.Thread(target=lambda: result.append(read_copy(str(root), PROJECT, RUN_ID, 1000)))
    worker.start()
    worker.join(timeout=5)
    elapsed = time.monotonic() - began
    if worker.is_alive():  # unblock the open before failing
        with open(folder / name, "wb"):
            pass
        worker.join(timeout=5)
        pytest.fail("reading a FIFO blocked")
    assert elapsed < 1.0
    assert unavailable(result[0]) == "unsafe_path"


def test_mirror_refuses_unsafe_or_stale_copies_a_landing_copy_is_read_again(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    folder = write_copy(root, PROJECT, RUN_ID, b"old")
    (folder / "transcript.jsonl").write_bytes(b"new")  # the dispatcher replaced the transcript; meta.json follows

    def meta_lands(seconds: float) -> None:
        write_copy(root, PROJECT, RUN_ID, b"new")

    result = read_copy(str(root), PROJECT, RUN_ID, 1000, sleep=meta_lands)
    assert isinstance(result, MirrorCopy) and result.data == b"new"


@pytest.mark.parametrize("name", ["meta.json", "transcript.jsonl", "run folder", "project folder"])
def test_mirror_refuses_unsafe_or_stale_copies_permission_denied(name: str) -> None:
    """A copy the agent's user may not open (it lacks the copy root's group, §8.8, §13). Root opens every file, so as
    root the read runs in a child that has dropped to another uid; otherwise it runs here."""
    base = Path(tempfile.mkdtemp(prefix="cairn-repair-mirror-test-"))
    try:
        base.chmod(0o755)
        root = base / "transcripts"
        folder = write_copy(root, PROJECT, RUN_ID, b"x")
        for path in (root, root / PROJECT, folder):
            path.chmod(0o755)
        for path in folder.iterdir():
            path.chmod(0o644)
        target = {"run folder": folder, "project folder": root / PROJECT}.get(name, folder / name)
        target.chmod(0)

        def read() -> str:
            return unavailable(read_copy(str(root), PROJECT, RUN_ID, 1000))

        reason = run_in_child(read, OTHER_UID, OTHER_GID, [OTHER_GID]) if IS_ROOT else read()
        assert reason == "permission_denied"
    finally:
        for path in base.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755)
        shutil.rmtree(base, ignore_errors=True)


def test_mirror_refuses_unsafe_or_stale_copies_freshness(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    # The run's window opened 1350 s after its start; max_staleness_seconds is 45: a fresh copy was checked at 1305 s
    # or later. An old copied_elapsed_seconds with a fresh checked_elapsed_seconds is accepted: one long tool call.
    floor = mirror.freshness_floor(1350.0, 45)
    assert floor == 1305.0
    write_copy(root, PROJECT, RUN_ID, b"x", copied_elapsed_seconds=200.0, checked_elapsed_seconds=1305.0)
    fresh = read_copy(str(root), PROJECT, RUN_ID, 1000, min_checked_elapsed=floor)
    assert isinstance(fresh, MirrorCopy)
    assert (fresh.copied_elapsed_seconds, fresh.checked_elapsed_seconds) == (200.0, 1305.0)
    write_copy(root, PROJECT, RUN_ID, b"x", copied_elapsed_seconds=1300.0, checked_elapsed_seconds=1304.5)
    stale = read_copy(str(root), PROJECT, RUN_ID, 1000, min_checked_elapsed=floor)
    assert stale == Unavailable("stale", checked_elapsed_seconds=1304.5)


def test_mirror_refuses_unsafe_or_stale_copies_a_stale_copy_is_read_again_until_its_deadline(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=1290.0)
    clock = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        if len(slept) == 2:  # the dispatcher's next tick finds the session file unchanged
            write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=1320.0)

    found = mirror.read_fresh_copy(
        str(root),
        PROJECT,
        RUN_ID,
        1000,
        min_checked_elapsed=1305.0,
        wait_until=112.0,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert isinstance(found, MirrorCopy) and found.checked_elapsed_seconds == 1320.0
    assert slept == [5.0, 5.0]
    write_copy(root, PROJECT, RUN_ID, b"x", checked_elapsed_seconds=1290.0)
    slept.clear()
    clock[0] = 100.0

    def wait(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    gave_up = mirror.read_fresh_copy(
        str(root),
        PROJECT,
        RUN_ID,
        1000,
        min_checked_elapsed=1305.0,
        wait_until=107.0,
        monotonic=lambda: clock[0],
        sleep=wait,
    )
    assert unavailable(gave_up) == "stale" and slept == [5.0, 2.0]


# --- AG22 -------------------------------------------------------------------------------------------------------------


def test_mirror_reads_the_golden_copy_fixture() -> None:
    """TC12's fixture, written by the dispatcher's copy (build step 7), read with read_copy(root, "proj_027", ...).

    Skipped only on a branch without step 7: where step 7's tests are, TC12 among them, its fixture must be too."""
    if not GOLDEN_COPY_PROJECT.is_dir():
        if STEP_7_TESTS.is_file():
            pytest.fail(f"{STEP_7_TESTS.name} (build step 7, TC12) is here, but not its fixture {GOLDEN_COPY_PROJECT}")
        pytest.skip("cairn/tests/fixtures/repair_copy/proj_027/ comes with build step 7's copy (TC12)")
    runs = [entry for entry in GOLDEN_COPY_PROJECT.iterdir() if entry.is_dir()]
    assert len(runs) == 1
    run_id = runs[0].name
    meta = json.loads((runs[0] / "meta.json").read_text(encoding="utf-8"))
    result = read_copy(str(GOLDEN_COPY_PROJECT.parent), "proj_027", run_id, MAX_BYTES)
    assert isinstance(result, MirrorCopy), result
    assert result.sha256 == meta["sha256"] == hashlib.sha256((runs[0] / "transcript.jsonl").read_bytes()).hexdigest()
    assert (result.copied_elapsed_seconds, result.checked_elapsed_seconds) == (
        meta["copied_elapsed_seconds"],
        meta["checked_elapsed_seconds"],
    )
    transcript = transcripts.parse_transcript(meta["cli_type"], result.data)
    assert transcript.entries > 0 and transcript.unreadable == 0 and transcript.calls


# --- AG6 --------------------------------------------------------------------------------------------------------------


# Every file open of the process (os.open and the built-in open alike raise the "open" audit event), recorded while a
# test sets AUDITED. An audit hook cannot be removed, so it stays installed and records nothing otherwise.
AUDITED: list[str] | None = None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if AUDITED is not None and event == "open":
        AUDITED.append(os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else repr(args[0]))


sys.addaudithook(_audit)


def test_agent_reads_only_its_copy_folder(layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    global AUDITED
    root = layout.root
    data = fixture_bytes(CODEX)
    write_copy(root, PROJECT, RUN_ID, data)
    write_copy(root, PROJECT, "a" * 32, b"another run of the project")
    write_copy(root, "proj_002", "b" * 32, b"another project's run")
    elsewhere = layout.base / "not-for-the-agent.txt"
    elsewhere.write_text("the locator's file")
    run_view = {
        "run_id": RUN_ID,
        "project_id": PROJECT,
        "cli_type": "codex",
        "session_id": "0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e44",
        "locator": {
            "container": "cairn-dispatch-proj_001",
            "prompt_marker": "/etc/passwd",
            "path": str(elsewhere),
            "source_path": "/etc/passwd",
        },
    }
    opened: list[str] = []
    folders: dict[int, str] = {}
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        name = os.fspath(path)
        full = os.path.join(folders[dir_fd], name) if dir_fd is not None else name
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        folders[descriptor] = full
        opened.append(full)
        return descriptor

    monkeypatch.setattr(os, "open", spy)
    AUDITED = []
    try:
        config.load_settings(str(root))
        result = mirror.read_run_copy(str(root), run_view, MAX_BYTES)
    finally:
        audited, AUDITED = AUDITED, None
        monkeypatch.undo()
    assert isinstance(result, MirrorCopy) and result.data == data
    run_folder = os.path.join(str(root), PROJECT, RUN_ID)
    assert opened == [
        str(root),
        os.path.join(str(root), config.SETTINGS_FILE_NAME),
        str(root),
        os.path.join(str(root), PROJECT),
        run_folder,
        os.path.join(run_folder, "meta.json"),
        os.path.join(run_folder, "transcript.jsonl"),
    ]
    # Nothing else is opened, by any means: the same seven opens, and never the locator's paths.
    assert audited == [
        str(root),
        config.SETTINGS_FILE_NAME,
        str(root),
        PROJECT,
        RUN_ID,
        "meta.json",
        "transcript.jsonl",
    ]


@pytest.mark.parametrize(
    ("field", "value", "reason"), [("run_id", 7, "bad_run_id"), ("project_id", None, "bad_project_id")]
)
def test_agent_reads_only_its_copy_folder_ids_of_another_type(
    tmp_path: Path, field: str, value: Any, reason: str
) -> None:
    run_view: dict[str, Any] = {"run_id": RUN_ID, "project_id": PROJECT, field: value}
    assert unavailable(mirror.read_run_copy(str(tmp_path), run_view, MAX_BYTES)) == reason
