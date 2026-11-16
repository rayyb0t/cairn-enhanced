"""redact_eval_set.py (spec §15.3 step 0, §8.8 Redaction): GT8 of spec §16.

It runs in the cairn environment and needs only cairn (`DispatchConfig` and `cairn.dispatcher.repair.redaction`), so
this file imports nothing of the repair agent. The secrets are made-up canaries; the credential files are written into
temporary folders found through HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and PI_CODING_AGENT_DIR.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from gatekit import Call, EvalCase, Session, dispatch_yaml, fake_secret, write_eval_set

pytest.importorskip("cairn.dispatcher.repair.redaction")

import redact_eval_set

REVIEWER_KEY = fake_secret("reviewer-key", 44)
SECOND_REVIEWER_KEY = fake_secret("second-reviewer-key", 46)  # r2, an anthropic_messages reviewer's own key
WORKER_TOKEN = fake_secret("worker-token")  # dispatch_yaml's container-mode worker key
CANARIES = {
    "claude:.credentials.json": "canary-claude-oauth-access-0001-xyz",
    "claude:.claude.json": "canary-claude-primary-api-key-0002",
    "claude:settings.json": "canary-claude-settings-env-0003-tok",
    "codex:auth.json": "canary-codex-auth-openai-key-0004",
    "codex:config.toml": "canary-codex-config-bearer-0005-tk",
    "pi:auth.json": "canary-pi-auth-key-value-0006-abcd",
    "pi:models.json": "canary-pi-models-api-key-0007-abcd",
}
PROXY_PASSWORD = "canary-proxy-password-0008"  # noqa: S105 - a made-up canary, not a password


def write_credential_files(claude_dir: Path, claude_json: Path, codex_dir: Path, pi_dir: Path) -> None:
    for folder in (claude_dir, codex_dir, pi_dir):
        folder.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": CANARIES["claude:.credentials.json"], "expiresAt": 1}})
    )
    claude_json.write_text(json.dumps({"primaryApiKey": CANARIES["claude:.claude.json"], "numStartups": 3}))
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": CANARIES["claude:settings.json"],
                    "HTTPS_PROXY": f"http://lab-user:{PROXY_PASSWORD}@proxy.lab.test:3128",
                }
            }
        )
    )
    (codex_dir / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": CANARIES["codex:auth.json"]}))
    (codex_dir / "config.toml").write_text(
        f'[model_providers.lab]\nname = "lab"\nexperimental_bearer_token = "{CANARIES["codex:config.toml"]}"\n'
    )
    (pi_dir / "auth.json").write_text(json.dumps({"lab": {"type": "api_key", "key": CANARIES["pi:auth.json"]}}))
    (pi_dir / "models.json").write_text(json.dumps({"providers": {"lab": {"apiKey": CANARIES["pi:models.json"]}}}))


def leaking_case() -> EvalCase:
    """A run whose tool output printed every secret: raw, inside a JSON string, and in a URL."""
    printed = "\n".join(
        [
            *(f"{label} -> {value}" for label, value in CANARIES.items()),
            json.dumps({"REVIEW_API_KEY": REVIEWER_KEY}),
            f"REVIEW_API_KEY: {SECOND_REVIEWER_KEY}",
            f"proxy http://lab-user:{PROXY_PASSWORD}@proxy.lab.test:3128",
            f"ANTHROPIC_AUTH_TOKEN={WORKER_TOKEN}",
        ]
    )
    session = Session(
        "claudecode",
        (Call(20, 30, "cat ~/.claude/.credentials.json dispatch.yaml", printed), Call(40, 45, "env", printed)),
        conclude_at=1805,
        session_id="01a1dafd-0000-7000-8000-0000000000aa",
    )
    return EvalCase("i001", "claudecode", 1800, "stuck", session=session)


@pytest.mark.parametrize("found_through", ["home", "variables"])
def test_redact_eval_set_uses_the_dispatcher_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], found_through: str
) -> None:
    """GT8: redact_eval_set.py loads dispatch.yaml with DispatchConfig and applies the dispatcher's secret_values and
    redact_exact: each reviewer's REVIEW_API_KEY is removed and counted under that name (two reviewers, the second an
    anthropic_messages one with its own key: both keys are gone, and the count is both keys' occurrences); with a
    local-mode dispatch.yaml, so are the values of the local CLIs' credential files of §8.8, found through a temporary
    HOME, or CLAUDE_CONFIG_DIR, CODEX_HOME and PI_CODING_AGENT_DIR: no canary is left in transcript.redacted.jsonl, and
    redaction.json counts each under its file's label, never with a value."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    if found_through == "home":
        write_credential_files(home / ".claude", home / ".claude.json", home / ".codex", home / ".pi" / "agent")
    else:
        claude_dir, codex_dir, pi_dir = tmp_path / "claude-config", tmp_path / "codex-home", tmp_path / "pi-agent"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))
        write_credential_files(claude_dir, claude_dir / ".claude.json", codex_dir, pi_dir)
        # The defaults under HOME hold other values, which the variables make the CLIs ignore: not read.
        write_credential_files(home / ".claude", home / ".claude.json", home / ".codex", home / ".pi" / "agent")
    eval_dir = write_eval_set(tmp_path, [leaking_case()], mode="local")
    folder = eval_dir / "i001"
    source = next(path for path in folder.iterdir() if path.suffix == ".jsonl")
    source.chmod(0o644)
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(
        dispatch_yaml(execution="local", reviewer_key=REVIEWER_KEY, second_reviewer_key=SECOND_REVIEWER_KEY),
        encoding="utf-8",
    )

    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    printed = capsys.readouterr().out
    redacted = (folder / "transcript.redacted.jsonl").read_bytes()
    original = source.read_bytes()
    for value in [*CANARIES.values(), REVIEWER_KEY, SECOND_REVIEWER_KEY, PROXY_PASSWORD]:
        assert value.encode() in original
        assert value.encode() not in redacted, value
    report = json.loads((folder / "redaction.json").read_text())
    counts = report["redactions"]
    for label in CANARIES:
        assert counts[label] >= 2, label  # printed by both calls
    # Both reviewers' keys, each printed by both calls, are counted under REVIEW_API_KEY: every occurrence of each.
    first_seen, second_seen = original.count(REVIEWER_KEY.encode()), original.count(SECOND_REVIEWER_KEY.encode())
    assert first_seen >= 2 and second_seen >= 2
    assert counts["REVIEW_API_KEY"] == first_seen + second_seen
    assert redacted.count(b"[REDACTED:REVIEW_API_KEY]") == first_seen + second_seen
    assert counts["claude:settings.json"] >= 4  # the token and the proxy URL's password, by both calls
    assert b"[REDACTED:REVIEW_API_KEY]" in redacted and b"[REDACTED:claude:.credentials.json]" in redacted
    info = source.stat()
    assert report["files"] == [
        {
            "source": source.name,
            "source_bytes": info.st_size,
            "source_mtime_ns": info.st_mtime_ns,
            "output": "transcript.redacted.jsonl",
            "output_sha256": report["files"][0]["output_sha256"],
            "redactions": report["files"][0]["redactions"],
        }
    ]
    every_value = [*CANARIES.values(), REVIEWER_KEY, SECOND_REVIEWER_KEY, PROXY_PASSWORD, WORKER_TOKEN]
    written = (folder / "redaction.json").read_text() + printed
    assert not any(value in written for value in every_value)
    assert "REVIEW_API_KEY: " in printed and "claude:.credentials.json: " in printed
    assert f"note: {source.name} is readable by others than you (mode 0644); chmod 0600 it" in printed
    assert "execution=local" in printed
    # The session file itself is left as it is; the redaction runs again as often as asked, from it.
    assert source.read_bytes() == original
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    assert (folder / "transcript.redacted.jsonl").read_bytes() == redacted


def test_container_mode_removes_worker_and_reviewer_keys_and_reads_no_credential_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In container mode the values are the workers' env (common_env included) and the reviewers' keys; the host's
    CLI credential files are not read, as the live copy does not read them for a container run (§8.8)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_credential_files(home / ".claude", home / ".claude.json", home / ".codex", home / ".pi" / "agent")
    eval_dir = write_eval_set(tmp_path, [leaking_case()], mode="container")
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    capsys.readouterr()
    redacted = (eval_dir / "i001" / "transcript.redacted.jsonl").read_bytes()
    assert WORKER_TOKEN.encode() not in redacted and REVIEWER_KEY.encode() not in redacted
    assert CANARIES["claude:.credentials.json"].encode() in redacted  # only the agent's pattern layer sees these
    counts = json.loads((eval_dir / "i001" / "redaction.json").read_text())["redactions"]
    assert counts["ANTHROPIC_AUTH_TOKEN"] >= 2 and counts["REVIEW_API_KEY"] >= 2
    assert not any(label in counts for label in CANARIES)


def test_several_session_files_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Several session files of one intent (several timed-out attempts) each get attempt-<k>/transcript.redacted.jsonl,
    k in name order, so that every output is named as the Compose copy of spec 15.3 carries it; an output of an
    earlier run whose session file is gone is removed, its attempt folder too; a dispatch.yaml that does not load is
    exit 2, with no value printed; an eval set without intent folders is exit 3."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    eval_dir = write_eval_set(tmp_path, [leaking_case()], mode="container")
    folder = eval_dir / "i001"
    first = next(path for path in folder.iterdir() if path.suffix == ".jsonl")
    (folder / "zz-second-attempt.jsonl").write_bytes(first.read_bytes())
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    report = json.loads((folder / "redaction.json").read_text())
    assert [(entry["source"], entry["output"]) for entry in report["files"]] == [
        (first.name, "attempt-1/transcript.redacted.jsonl"),
        ("zz-second-attempt.jsonl", "attempt-2/transcript.redacted.jsonl"),
    ]
    assert report["written_by"] == "redact_eval_set.py"
    assert not (folder / "transcript.redacted.jsonl").exists()
    for attempt in ("attempt-1", "attempt-2"):
        redacted = (folder / attempt / "transcript.redacted.jsonl").read_bytes()
        assert REVIEWER_KEY.encode() not in redacted and b"[REDACTED:REVIEW_API_KEY]" in redacted
    (folder / "zz-second-attempt.jsonl").unlink()
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    assert "removed attempt-1/transcript.redacted.jsonl: its session file is gone" in capsys.readouterr().out
    assert sorted(path.name for path in folder.iterdir() if "redacted" in path.name or "attempt" in path.name) == [
        "transcript.redacted.jsonl"
    ]
    broken = tmp_path / "broken.yaml"
    broken.write_text(dispatch_yaml(execution="container").replace("max_workers: 1", f'max_workers: "{REVIEWER_KEY}"'))
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(broken)]) == 2
    err = capsys.readouterr().err
    assert "dispatch.yaml cannot be loaded: runtime.max_workers" in err and REVIEWER_KEY not in err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert redact_eval_set.main([str(empty), "--dispatch-config", str(dispatch)]) == 3


def snapshot(folder: Path) -> dict[str, bytes]:
    return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}


def test_step_0_never_removes_or_replaces_what_it_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 0 run over folders that hold only redacted outputs leaves them as they are: a folder written by
    export_shadow_runs.py (the dispatcher's redacted copy, which may not be exportable again, spec 8.8 Removal) and a
    copy of the eval set made without its session files (spec 15.3, Compose). A folder whose session files it cannot
    redact without replacing what it did not write (an export folder, an output no redaction.json of this tool names,
    a redaction.json it cannot read) is left as it is too, and the tool exits 3 after the other folders."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    # A redacted eval set, then its Compose copy without the session files.
    (tmp_path / "set").mkdir()
    eval_dir = write_eval_set(tmp_path / "set", [leaking_case()], mode="container")
    (eval_dir / "i001" / "zz-second-attempt.jsonl").write_bytes(leaking_case().session_file().data())
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    copy = tmp_path / "copy"
    for path in eval_dir.rglob("*"):
        if path.is_file() and path.name in ("intent.json", "transcript.redacted.jsonl", "redaction.json"):
            (copy / path.relative_to(eval_dir)).parent.mkdir(parents=True, exist_ok=True)
            (copy / path.relative_to(eval_dir)).write_bytes(path.read_bytes())
    # An export_shadow_runs.py folder, in the shape it writes (its redaction.json names the dispatcher's copy).
    export_folder = copy / "i002"
    export_folder.mkdir()
    (export_folder / "intent.json").write_text(json.dumps({"intent_id": "i002", "timed_out_attempts": []}))
    (export_folder / "transcript.redacted.jsonl").write_bytes(leaking_case().session_file().data())
    (export_folder / "redaction.json").write_text(
        json.dumps(
            {
                "version": 1,
                "written_by": "export_shadow_runs.py",
                "files": [
                    {
                        "source": "dispatcher copy proj_027/0123456789abcdef0123456789abcdef/transcript.jsonl",
                        "output": "transcript.redacted.jsonl",
                        "redacted_by": "dispatcher (spec 8.8); counts in that copy's meta.json",
                    }
                ],
            }
        )
    )
    capsys.readouterr()
    before = snapshot(copy)
    assert redact_eval_set.main([str(copy), "--dispatch-config", str(dispatch)]) == 0
    printed = capsys.readouterr().out
    assert snapshot(copy) == before
    for intent in ("i001", "i002"):
        assert f"left intent={intent}: no session file: its redacted transcripts and redaction.json are kept" in printed
    assert "redacted 0 intent folders, left 2 as they were" in printed

    # Session files beside what this tool did not write: the folder is left as it is, and the exit code is 3.
    session = leaking_case().session_file().data()
    (export_folder / "late-attempt.jsonl").write_bytes(session)
    stray = copy / "i003"
    stray.mkdir()
    (stray / "intent.json").write_text(json.dumps({"intent_id": "i003", "timed_out_attempts": []}))
    (stray / "attempt.jsonl").write_bytes(session)
    (stray / "transcript.redacted.jsonl").write_bytes(b"not written by redact_eval_set.py\n")
    unreadable = copy / "i004"
    unreadable.mkdir()
    (unreadable / "intent.json").write_text(json.dumps({"intent_id": "i004", "timed_out_attempts": []}))
    (unreadable / "attempt.jsonl").write_bytes(session)
    (unreadable / "redaction.json").write_text("{not json")
    before = snapshot(copy)
    assert redact_eval_set.main([str(copy), "--dispatch-config", str(dispatch)]) == 3
    captured = capsys.readouterr()
    assert snapshot(copy) == before
    assert "left intent=i002: its redaction.json was written by export_shadow_runs.py" in captured.out
    assert "left intent=i003: it holds transcript.redacted.jsonl, which no redaction.json of this tool names" in (
        captured.out
    )
    assert "left intent=i004: its redaction.json cannot be read" in captured.out
    assert "session files left unredacted in 3 intent folders: i002, i003, i004" in captured.err
    assert REVIEWER_KEY not in captured.out + captured.err

    # A symlink where an attempt folder goes is never followed: nothing is written through it.
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "linked").mkdir()
    linked = write_eval_set(tmp_path / "linked", [leaking_case()], mode="container")
    (linked / "i001" / "zz-second-attempt.jsonl").write_bytes(session)
    (linked / "i001" / "attempt-2").symlink_to(target, target_is_directory=True)
    assert redact_eval_set.main([str(linked), "--dispatch-config", str(dispatch)]) == 3
    assert "attempt-2 is not a folder (a symlink is never followed)" in capsys.readouterr().err
    assert list(target.iterdir()) == []


# --- a run stopped on its way: the next run recovers -----------------------------------------------------------------


def listing(folder: Path) -> list[str]:
    """Every entry under folder, attempt folders, FIFOs and temporary dot files included, relative to it."""
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


def outputs_all_named(folder: Path) -> bool:
    """Whether every redacted output in folder is named by this tool's redaction.json there (none without one): the
    state in which the next run redacts the folder rather than leaving it for you."""
    previous = redact_eval_set.previous_report(folder)
    named = previous.outputs if previous is not None and previous.ours else frozenset()
    return set(redact_eval_set.present_outputs(folder)) <= named


def fresh_folder(tmp_path: Path, label: str) -> Path:
    """The intent folder of a new eval set with one session file, i001's."""
    (tmp_path / label).mkdir()
    return write_eval_set(tmp_path / label, [leaking_case()], mode="container") / "i001"


def assert_redacted(path: Path) -> bytes:
    data = path.read_bytes()
    assert REVIEWER_KEY.encode() not in data and b"[REDACTED:REVIEW_API_KEY]" in data
    return data


def test_a_session_file_it_cannot_read_or_an_interrupt_leaves_the_folder_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder is redacted whole or not at all: a later session file that cannot be read (a symlink on the folder's
    first run, a FIFO on a run over an earlier redaction) or an interrupt, after an earlier session file of the same
    folder was redacted, stops the run with the folder exactly as it was: no output and no attempt folder of that run,
    no temporary file, the earlier output and redaction.json untouched. Once the cause is gone, the next run redacts
    the folder; it never finds its own output named by no redaction.json and leaves the folder to you."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    session = leaking_case().session_file().data()

    def run(folder: Path) -> int:
        return redact_eval_set.main([str(folder.parent), "--dispatch-config", str(dispatch)])

    # A symlink as the second session file, on the folder's first run.
    folder = fresh_folder(tmp_path, "symlink")
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(session)
    second = folder / "zz-second-attempt.jsonl"
    second.symlink_to(elsewhere)
    before = listing(folder)
    assert run(folder) == 3
    assert "zz-second-attempt.jsonl cannot be read" in capsys.readouterr().err
    assert listing(folder) == before
    second.unlink()
    second.write_bytes(session)
    assert run(folder) == 0
    for attempt in ("attempt-1", "attempt-2"):
        assert_redacted(folder / attempt / "transcript.redacted.jsonl")

    # A FIFO as the second session file, over an earlier redaction of the first alone.
    folder = fresh_folder(tmp_path, "fifo")
    assert run(folder) == 0
    second = folder / "zz-second-attempt.jsonl"
    os.mkfifo(second)
    earlier = snapshot(folder)  # every regular file, the FIFO aside
    before = listing(folder)
    assert "transcript.redacted.jsonl" in before and "redaction.json" in before and second.name in before
    capsys.readouterr()
    assert run(folder) == 3
    assert "zz-second-attempt.jsonl is not a regular file" in capsys.readouterr().err
    assert listing(folder) == before and snapshot(folder) == earlier
    second.unlink()
    second.write_bytes(session)
    assert run(folder) == 0
    assert "removed transcript.redacted.jsonl" in capsys.readouterr().out
    assert not (folder / "transcript.redacted.jsonl").exists()
    report = json.loads((folder / "redaction.json").read_text())
    assert [entry["output"] for entry in report["files"]] == [
        "attempt-1/transcript.redacted.jsonl",
        "attempt-2/transcript.redacted.jsonl",
    ]
    for entry in report["files"]:
        data = assert_redacted(folder / entry["output"])
        assert hashlib.sha256(data).hexdigest() == entry["output_sha256"]

    # An interrupt while the second session file is read.
    folder = fresh_folder(tmp_path, "interrupt")
    (folder / "zz-second-attempt.jsonl").write_bytes(session)
    before = listing(folder)
    read_source = redact_eval_set.read_source
    seen: list[Path] = []

    def interrupted(path: Path) -> tuple[bytes, os.stat_result]:
        seen.append(path)
        if len(seen) == 2:
            raise KeyboardInterrupt
        return read_source(path)

    monkeypatch.setattr(redact_eval_set, "read_source", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(folder)
    assert listing(folder) == before
    monkeypatch.setattr(redact_eval_set, "read_source", read_source)
    assert run(folder) == 0
    assert outputs_all_named(folder) and len(redact_eval_set.present_outputs(folder)) == 2


def test_a_run_stopped_while_it_moves_its_outputs_into_place_leaves_a_folder_the_next_run_redacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once every session file is redacted, the earlier outputs go, then the new redaction.json is written, then the
    new outputs move into place. A full disk at redaction.json leaves no output that the folder's redaction.json does
    not name (the earlier ones are gone, and it still names them); a failure while the outputs move leaves none either,
    and no earlier output where the new redaction.json names a new one. Either way the next run redacts the folder."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    session = leaking_case().session_file().data()

    def run(folder: Path) -> int:
        return redact_eval_set.main([str(folder.parent), "--dispatch-config", str(dispatch)])

    # A full disk at redaction.json, on a run that turns one session file into two.
    folder = fresh_folder(tmp_path, "full-disk")
    assert run(folder) == 0
    earlier_report = (folder / "redaction.json").read_bytes()
    (folder / "zz-second-attempt.jsonl").write_bytes(session)
    write_atomic = redact_eval_set.write_atomic

    def full_disk(target: Path, name: str, data: bytes) -> None:
        if name == "redaction.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        write_atomic(target, name, data)

    monkeypatch.setattr(redact_eval_set, "write_atomic", full_disk)
    capsys.readouterr()
    assert run(folder) == 3
    assert "No space left on device" in capsys.readouterr().err
    assert outputs_all_named(folder)
    assert (folder / "redaction.json").read_bytes() == earlier_report
    assert not (folder / "transcript.redacted.jsonl").exists()
    assert not [name for name in listing(folder) if name.startswith("attempt-") or name.endswith(".tmp")]
    monkeypatch.setattr(redact_eval_set, "write_atomic", write_atomic)
    assert run(folder) == 0
    assert redact_eval_set.present_outputs(folder) == [
        "attempt-1/transcript.redacted.jsonl",
        "attempt-2/transcript.redacted.jsonl",
    ]

    # A failure while the output moves into place, after its session file changed: the earlier output, which
    # redacts the session file as it was, is not left where the new redaction.json names the new one.
    folder = fresh_folder(tmp_path, "move")
    (source,) = (path for path in folder.iterdir() if path.suffix == ".jsonl")  # before the run writes its output
    assert run(folder) == 0
    source.write_bytes(source.read_bytes() + json.dumps({"type": "user", "note": REVIEWER_KEY}).encode() + b"\n")
    replace = os.replace

    def failing_replace(source_path: Any, target: Any, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "transcript.redacted.jsonl" and tmp_path in Path(target).parents:
            raise OSError(errno.EIO, "made-up")
        replace(source_path, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    try:
        assert run(folder) == 3
    finally:
        monkeypatch.setattr(os, "replace", replace)
    assert not (folder / "transcript.redacted.jsonl").exists()
    assert outputs_all_named(folder)
    assert json.loads((folder / "redaction.json").read_text())["files"][0]["source_bytes"] == source.stat().st_size
    assert not [name for name in listing(folder) if name.endswith(".tmp")]
    assert run(folder) == 0
    report = json.loads((folder / "redaction.json").read_text())
    data = assert_redacted(folder / "transcript.redacted.jsonl")
    assert hashlib.sha256(data).hexdigest() == report["files"][0]["output_sha256"]
    assert report["files"][0]["source_bytes"] == source.stat().st_size and b'"note": "[REDACTED:' in data


# --- a run killed on its way: the next run recovers ------------------------------------------------------------------

# redact_eval_set.py in a process of its own that SIGKILLs itself right after the first call of a step for a file name:
# once write_temporary has written that file's temporary file, or once os.replace has moved a file onto that name.
# Under a SIGKILL, as under the OOM killer or a power loss, no except and no finally runs, so the temporary files of
# that run stay. -I keeps PYTHONPATH and the other PYTHON* variables, the current folder and the user site out of its
# imports (the tool and cairn come from the folders it is given); -B writes no bytecode.
KILLED_RUN = r"""
import os
import signal
import sys
from pathlib import Path

tool_folder, cairn_folder, eval_dir, dispatch, step, name = sys.argv[1:]
sys.path[:0] = [tool_folder, cairn_folder]
import redact_eval_set


def killed_after(function, name_of):
    def call(*args, **kwargs):
        result = function(*args, **kwargs)
        if name_of(*args) == name:
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    return call


if step == "write_temporary":
    redact_eval_set.write_temporary = killed_after(redact_eval_set.write_temporary, lambda folder, file, data: file)
else:
    os.replace = killed_after(os.replace, lambda source, target: Path(target).name)
sys.exit(redact_eval_set.main([eval_dir, "--dispatch-config", dispatch]))
"""
IN_FOLDER = ".transcript.redacted.jsonl.tmp"
IN_ATTEMPT_1 = "attempt-1/.transcript.redacted.jsonl.tmp"
IN_ATTEMPT_2 = "attempt-2/.transcript.redacted.jsonl.tmp"
REPORT_TEMPORARY = ".redaction.json.tmp"


def run_killed(eval_dir: Path, dispatch: Path, step: str, name: str) -> None:
    """redact_eval_set.py on eval_dir, SIGKILLed right after its first `step` for `name` (KILLED_RUN), with the tool and
    the cairn that this test imports."""
    import cairn

    found = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            KILLED_RUN,
            str(Path(redact_eval_set.__file__).parent),
            str(Path(cairn.__file__).parents[1]),
            str(eval_dir),
            str(dispatch),
            step,
            name,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert found.returncode == -signal.SIGKILL, found.stderr  # killed there, neither finished nor stopped by an error


def temporaries(folder: Path) -> list[str]:
    return [name for name in listing(folder) if name.endswith(".tmp")]


def assert_outputs_as_named(folder: Path) -> None:
    """What the module's docstring promises at any point of a run: the folder holds no redacted output that its
    redaction.json does not name, nor an earlier output where it names a new one (each output has the output_sha256
    that it names), since the replay reads the outputs that it names without hashing them."""
    assert outputs_all_named(folder)
    present = redact_eval_set.present_outputs(folder)
    if present:
        report = json.loads((folder / "redaction.json").read_text())
        named = {entry["output"]: entry["output_sha256"] for entry in report["files"]}
        for name in present:
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == named[name], name


def assert_redacted_whole(folder: Path) -> None:
    """What a run that finished leaves: redaction.json names every session file of the folder, with its size and
    modification time now, and every redacted output the folder holds, each with its output_sha256; no temporary file
    is left."""
    report = json.loads((folder / "redaction.json").read_text())
    assert sorted(entry["source"] for entry in report["files"]) == redact_eval_set.session_files(folder)
    assert sorted(entry["output"] for entry in report["files"]) == redact_eval_set.present_outputs(folder)
    for entry in report["files"]:
        info = (folder / entry["source"]).stat()
        assert (entry["source_bytes"], entry["source_mtime_ns"]) == (info.st_size, info.st_mtime_ns)
        data = assert_redacted(folder / entry["output"])
        assert hashlib.sha256(data).hexdigest() == entry["output_sha256"]
    assert temporaries(folder) == []


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="SIGKILL is POSIX")
@pytest.mark.parametrize(
    ("earlier", "files", "step", "name", "left"),
    [
        pytest.param(
            False,
            1,
            "write_temporary",
            "redaction.json",
            [REPORT_TEMPORARY, IN_FOLDER],
            id="first-run-one-file-killed-after-the-temporary-redaction.json",
        ),
        pytest.param(
            False,
            2,
            "write_temporary",
            "transcript.redacted.jsonl",
            [IN_ATTEMPT_1],
            id="first-run-two-files-killed-after-the-first-temporary-file",
        ),
        pytest.param(
            False,
            2,
            "os.replace",
            "redaction.json",
            [IN_ATTEMPT_1, IN_ATTEMPT_2],
            id="first-run-two-files-killed-after-redaction.json",
        ),
        pytest.param(
            True,
            2,
            "write_temporary",
            "transcript.redacted.jsonl",
            [IN_ATTEMPT_1],
            id="earlier-redaction-second-file-killed-after-the-first-temporary-file",
        ),
        pytest.param(
            True,
            2,
            "write_temporary",
            "redaction.json",
            [REPORT_TEMPORARY, IN_ATTEMPT_1, IN_ATTEMPT_2],
            id="earlier-redaction-second-file-killed-after-the-temporary-redaction.json",
        ),
        pytest.param(
            True,
            2,
            "os.replace",
            "transcript.redacted.jsonl",
            [IN_ATTEMPT_2],
            id="earlier-redaction-second-file-killed-after-the-first-output-moved",
        ),
        pytest.param(
            True,
            1,
            "os.replace",
            "redaction.json",
            [IN_FOLDER],
            id="earlier-redaction-changed-file-killed-after-redaction.json",
        ),
    ],
)
def test_a_run_killed_on_its_way_leaves_a_folder_the_next_run_redacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    earlier: bool,
    files: int,
    step: str,
    name: str,
    left: list[str],
) -> None:
    """A run killed with SIGKILL, as by the OOM killer or a power loss, runs no cleanup, so the temporary files it wrote
    stay (with two session files, killed after its first one, in an attempt folder that holds nothing else). Killed on
    a first run, or over an earlier redaction of one session file that has since been joined by a second one or
    changed, right after its first temporary file, the temporary redaction.json, redaction.json itself or its first
    output, it leaves the temporary files `left` and, as at any point, no output that the folder's redaction.json does
    not name, nor an earlier output where it names a new one. The next run over the same session files exits 0:
    redaction.json names every output with its output_sha256 and every session file with its size and modification
    time, and no temporary file is left. That rests on write_temporary removing a leftover before its exclusive create:
    without it, this run and every later one would exit 3 (File exists) until you deleted the file by hand, and the
    replay would refuse the case meanwhile."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container", reviewer_key=REVIEWER_KEY), encoding="utf-8")
    folder = fresh_folder(tmp_path, "killed")
    (source,) = (path for path in folder.iterdir() if path.suffix == ".jsonl")

    def run() -> int:
        return redact_eval_set.main([str(folder.parent), "--dispatch-config", str(dispatch)])

    if earlier:
        assert run() == 0  # the earlier redaction, of the one session file
    if files == 2:
        (folder / "zz-second-attempt.jsonl").write_bytes(source.read_bytes())
    elif earlier:
        source.write_bytes(source.read_bytes() + json.dumps({"type": "user", "note": REVIEWER_KEY}).encode() + b"\n")
    run_killed(folder.parent, dispatch, step, name)
    assert temporaries(folder) == left
    assert_outputs_as_named(folder)
    capsys.readouterr()
    assert run() == 0, capsys.readouterr().err
    assert_redacted_whole(folder)
