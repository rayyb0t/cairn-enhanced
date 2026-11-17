"""Final delta review of Phase 2 (build step RC1): an eval set that an earlier build's step 0 redacted.

- MERGE-1, GT7 of spec §16 extended: since build steps RA1 and RA2, step 0 (`redact_eval_set.py`) also removes the
  dispatcher's repair token, header secrets (header lists, header maps and the variables they name) and, whatever the
  mode, the local mode's values; an eval set it redacted before an upgrade kept the weaker redaction, and the replay
  read it as it was. Step 0 now writes `redaction.json` with `"version": 2`, and the replay refuses a report of any
  other version (but export_shadow_runs.py's, which names the dispatcher's own copy) with exit 3, naming the intent
  folder, before any digest is built or any request sent; the Compose copy of such a set too. `--no-env-redaction`
  reads such a folder as before. After this build's step 0 runs again (and the Compose copy is made again), the
  replay goes on, and no request holds what the earlier redaction left.

It needs the repair agent and the cairn side (step 0 loads dispatch.yaml with DispatchConfig), so it runs in the
agent's environment, whose dev group holds cairn. Every value is made up: the canaries, the token and the keys are
fake, the reviewer is a stub on loopback, and the model is `test-model`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from gatekit import (
    Call,
    EvalCase,
    ModelStub,
    Session,
    clear_guarded_environment,
    dispatch_yaml,
    fake_secret,
    model_stub_factory,
    read_records,
    reviewer_entry,
    write_agent_files,
    write_eval_set,
)

pytest.importorskip("cairn_repair_agent")
pytest.importorskip("cairn.dispatcher.repair.redaction")

from cairn_repair_agent import export, replay
from test_replay_eval_set import compose_copy, run_replay

import redact_eval_set
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import redaction

# What 7cdff8b's step 0 left in a run's output and this build's removes (the finding's probe, with made-up values):
# a header of ANTHROPIC_CUSTOM_HEADERS in the environment that runs step 0, an MCP server's header in ~/.claude.json
# (neither name holds a secret word) and the dispatcher's repair token, bare hex as `secrets.token_hex(32)` makes it.
ROUTE_HEADER_VALUE = "FAKE-lab-route-canary-0401"  # fake
GATEWAY_HEADER_VALUE = "FAKE-lab-gateway-canary-0402"  # fake
REPAIR_TOKEN = hashlib.sha256(b"made-up repair token of the final delta tests").hexdigest()
CANARIES = (ROUTE_HEADER_VALUE, GATEWAY_HEADER_VALUE, REPAIR_TOKEN)
WORKER_TOKEN = fake_secret("worker-token")  # gatekit's container worker's ANTHROPIC_AUTH_TOKEN: both builds remove it
DISPATCH_TOKEN_FILE = "/nonexistent/repair-dispatcher.token"  # noqa: S105 - the path gatekit's dispatch.yaml names
EVERY_SET = ["worker_env", "environment", "credential_files", "repair_token", "server_url", "reviewers"]
REFUSAL = (
    "redaction.json was written by an earlier build's step 0 (its version is not 2), which removes fewer values; "
    "run redact_eval_set.py again"
)


@pytest.fixture(autouse=True)
def _guarded_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_guarded_environment(monkeypatch)


@pytest.fixture
def model_stubs() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stub_factory()


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of the test's own whose ~/.claude.json gives an MCP server a header, ANTHROPIC_CUSTOM_HEADERS set in the
    environment that runs step 0, and a dispatch.yaml in container mode whose token_file holds the repair token.
    Returns that dispatch.yaml."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    servers = {
        "lab": {"type": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"X-Lab-Gateway": GATEWAY_HEADER_VALUE}}
    }
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Lab-Route: {ROUTE_HEADER_VALUE}")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    token = secrets / "repair-dispatcher.token"
    token.write_text(REPAIR_TOKEN + "\n", encoding="ascii")
    token.chmod(0o600)
    text = dispatch_yaml(execution="container")
    assert DISPATCH_TOKEN_FILE in text
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(text.replace(DISPATCH_TOKEN_FILE, str(token)), encoding="utf-8")
    return dispatch


def printing_case(intent: str, number: int) -> EvalCase:
    """A stuck run whose calls print the worker's token and each canary, before the cut of its 1800 s budget."""
    printed = (WORKER_TOKEN, *CANARIES)
    calls = tuple(
        Call(20 + 20 * index, 25 + 20 * index, f"cat lab-notes-{index}.txt", text) for index, text in enumerate(printed)
    )
    session = Session(
        "claudecode", calls, conclude_at=1805, session_id=f"01a1dafd-0000-7000-8000-0000000004{number:02d}"
    )
    return EvalCase(intent, "claudecode", 1800, "stuck", session=session)


def session_file(folder: Path) -> Path:
    (found,) = (path for path in folder.iterdir() if path.name.endswith(".jsonl") and "redacted" not in path.name)
    return found


def earlier_step_0(eval_dir: Path, dispatch: Path) -> None:
    """Each intent folder as 7cdff8b's step 0 left it: transcript.redacted.jsonl with that build's values (the
    workers' env, the reviewers' keys and URL passwords; the local mode's values only with a local-mode dispatch.yaml,
    and neither the repair token nor header secrets), and its redaction.json, of version 1 and without value_sets."""
    values = redaction.secret_values(DispatchConfig.load(dispatch), "container", {}, None)
    for folder in sorted(path for path in eval_dir.iterdir() if (path / "intent.json").is_file()):
        source = session_file(folder)
        data = source.read_bytes()
        redacted, counts = redaction.redact_exact(data, values)
        assert counts["ANTHROPIC_AUTH_TOKEN"] >= 1  # it removed the worker's token, as both builds do
        (folder / "transcript.redacted.jsonl").write_bytes(redacted)
        info = source.stat()
        entry = {
            "source": source.name,
            "source_bytes": info.st_size,
            "source_mtime_ns": info.st_mtime_ns,
            "output": "transcript.redacted.jsonl",
            "output_sha256": hashlib.sha256(redacted).hexdigest(),
            "redactions": dict(sorted(counts.items())),
        }
        report = {"version": 1, "written_by": "redact_eval_set.py", "files": [entry], "redactions": entry["redactions"]}
        (folder / "redaction.json").write_text(json.dumps(report, indent=2) + "\n", encoding="ascii")


def holds_a_canary(data: bytes) -> list[str]:
    return [canary for canary in CANARIES if canary.encode() in data]


def sent(stub: ModelStub) -> list[bytes]:
    return [seen.body for seen in stub.requests()]


def digests(out: Path) -> Sequence[Path]:
    folder = out / "digests"
    return sorted(path for path in folder.rglob("*") if path.is_file()) if folder.exists() else []


def test_replay_refuses_unredacted_transcripts_without_the_flag_earlier_step_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub], lab: Path
) -> None:
    """GT7 extended (final delta review MERGE-1): a folder whose redaction.json is version 1 without value_sets, as
    7cdff8b's step 0 writes it, and whose output still holds what this build's step 0 removes, makes
    replay_eval_set.py exit 3 with the reason, naming the folder, before any digest is built or any request sent;
    --no-env-redaction reads the folder as before. This build's step 0, run on it, writes version 2 with its value
    sets and removes every canary; the replay then goes on, and no request holds a canary."""
    assert replay.REDACTION_VERSION == redact_eval_set.REPORT_VERSION == 2
    assert replay.EXPORT_WRITTEN_BY == export.WRITTEN_BY
    eval_dir = write_eval_set(tmp_path, [printing_case("i001", 1)])
    folder = eval_dir / "i001"
    earlier_step_0(eval_dir, lab)
    earlier = (folder / "transcript.redacted.jsonl").read_bytes()
    assert holds_a_canary(earlier) == list(CANARIES) and WORKER_TOKEN.encode() not in earlier
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])

    for extra in (("--yes",), ("--dry-run",)):
        out = tmp_path / f"refused{extra[0]}"
        code, stdout, err = run_replay(eval_dir, files, out, *extra, capsys=capsys)
        assert code == 3
        assert f"replay_eval_set: i001: {REFUSAL}" in err
        assert "planned " not in stdout and digests(out) == [] and not (out / "decisions.jsonl").exists()
    assert stub.requests() == []

    # --no-env-redaction reads the folder as it did before: the output that redaction.json names.
    assert replay.transcript_files(folder, allow_unredacted=True) == [folder / "transcript.redacted.jsonl"]
    code, _, err = run_replay(eval_dir, files, tmp_path / "flag", "--dry-run", "--no-env-redaction", capsys=capsys)
    assert code == 0, err
    assert stub.requests() == []

    # This build's step 0 on the same folder: its own earlier report, so it replaces the report and the output.
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(lab)]) == 0
    printed = capsys.readouterr().out
    report = json.loads((folder / "redaction.json").read_text(encoding="utf-8"))
    assert report["version"] == 2 and report["written_by"] == "redact_eval_set.py"
    assert report["value_sets"] == EVERY_SET
    assert [entry["output"] for entry in report["files"]] == ["transcript.redacted.jsonl"]
    for label in ("ANTHROPIC_CUSTOM_HEADERS", "claude:.claude.json", "repair_token", "ANTHROPIC_AUTH_TOKEN"):
        assert report["redactions"][label] >= 1, label
    output = (folder / "transcript.redacted.jsonl").read_bytes()
    assert holds_a_canary(output) == [] and WORKER_TOKEN.encode() not in output
    assert not any(canary in printed for canary in CANARIES)

    out = tmp_path / "again"
    code, _, err = run_replay(eval_dir, files, out, "--yes", capsys=capsys)
    assert code == 0, err
    bodies = sent(stub)
    assert len(bodies) == 1
    assert [holds_a_canary(body) for body in bodies] == [[]]
    assert [holds_a_canary(path.read_bytes()) for path in digests(out)] == [[]]
    (record,) = read_records(out / "decisions.jsonl")
    assert (record["intent_id"], record["transcript"]) == ("i001", "transcript.redacted.jsonl")
    assert record["transcript_sha256"] == hashlib.sha256(output).hexdigest()


def test_replay_refuses_unredacted_transcripts_without_the_flag_earlier_step_0_compose_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub], lab: Path
) -> None:
    """GT7 extended (MERGE-1), Compose: the copy of §15.3, made with its rsync include list from an eval set that an
    earlier build's step 0 redacted, holds no session file and is refused all the same, exit 3, naming the folder,
    before any request; also where the copy lacks an output, which alone refuses only its case, so the whole replay
    stops rather than that case. Step 0 run again on the eval set does not change the copy made before it; the copy
    made again is replayed, and no request holds a canary."""
    eval_dir = write_eval_set(tmp_path, [printing_case("i001", 1), printing_case("i002", 2)])
    earlier_step_0(eval_dir, lab)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    copy = compose_copy(eval_dir, tmp_path / "eval-proj_027-redacted")
    assert sorted(path.name for path in (copy / "i001").iterdir()) == [
        "intent.json", "redaction.json", "transcript.redacted.jsonl",
    ]  # fmt: skip

    code, _, err = run_replay(copy, files, tmp_path / "old-copy", "--yes", capsys=capsys)
    assert code == 3 and f"replay_eval_set: i001: {REFUSAL}" in err
    (copy / "i001" / "transcript.redacted.jsonl").unlink()  # a copy that lacks an output, too
    code, _, err = run_replay(copy, files, tmp_path / "old-copy-partial", "--yes", capsys=capsys)
    assert code == 3 and f"replay_eval_set: i001: {REFUSAL}" in err and "refused case" not in err
    assert stub.requests() == []

    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(lab)]) == 0
    capsys.readouterr()
    code, _, err = run_replay(copy, files, tmp_path / "copy-not-made-again", "--yes", capsys=capsys)
    assert code == 3 and REFUSAL in err
    assert stub.requests() == []

    shutil.rmtree(copy)
    copy = compose_copy(eval_dir, copy)
    out = tmp_path / "copy-made-again"
    code, _, err = run_replay(copy, files, out, "--yes", capsys=capsys)
    assert code == 0, err
    bodies = sent(stub)
    assert len(bodies) == 2
    assert [holds_a_canary(body) for body in bodies] == [[], []]
    assert sorted(record["intent_id"] for record in read_records(out / "decisions.jsonl")) == ["i001", "i002"]


SESSION_NAME = "01a1dafd-0000-7000-8000-000000000499.jsonl"
EXPORT_SOURCE = "dispatcher copy proj_027/run-0001-made-up/transcript.jsonl"  # export_shadow_runs.py's shape


def write_folder(folder: Path, report: dict[str, Any], *, session: bool, source: str = SESSION_NAME) -> Path:
    """An intent folder with its redacted output and `report` naming it with `source`, as step 0 names a session
    file; with `session`, that session file too (else a copy without session files, or an export's folder)."""
    folder.mkdir(parents=True)
    (folder / "intent.json").write_text("{}", encoding="utf-8")
    output = folder / "transcript.redacted.jsonl"
    output.write_bytes(b'{"type": "made-up redacted line"}\n')
    entry: dict[str, Any] = {"source": source, "source_bytes": 33, "source_mtime_ns": 0, "output": output.name}
    if session:
        path = folder / source
        path.write_bytes(b'{"type": "made-up session line"}\n')
        info = path.stat()
        entry.update(source_bytes=info.st_size, source_mtime_ns=info.st_mtime_ns)
    (folder / "redaction.json").write_text(json.dumps({**report, "files": [entry]}), encoding="utf-8")
    return output


STEP_0 = {"written_by": "redact_eval_set.py", "redactions": {}}
EXPORT = {"version": 1, "written_by": "export_shadow_runs.py"}


@pytest.mark.parametrize(
    "report",
    [
        pytest.param({**STEP_0, "version": 1}, id="7cdff8b-version-1-without-value-sets"),
        pytest.param({**STEP_0, "version": 1, "value_sets": EVERY_SET}, id="version-1-with-value-sets"),
        pytest.param({**STEP_0}, id="no-version"),
        pytest.param({**STEP_0, "version": 3}, id="version-3"),
        pytest.param({**STEP_0, "version": "2"}, id="version-string"),
        pytest.param({**STEP_0, "version": 2.0}, id="version-float"),
        pytest.param({**STEP_0, "version": True}, id="version-true"),
        pytest.param({"version": 1, "redactions": {}}, id="no-written-by-version-1"),
        pytest.param({"version": 1, "written_by": "another-tool"}, id="another-writer-version-1"),
    ],
)
@pytest.mark.parametrize("session", [True, False], ids=["eval-set", "copy-without-session-files"])
def test_replay_refuses_unredacted_transcripts_without_the_flag_report_versions(
    tmp_path: Path, report: dict[str, Any], session: bool
) -> None:
    """GT7 extended (MERGE-1): every report but version 2 (an int) and export_shadow_runs.py's stops the replay with
    the earlier step 0's reason, naming the folder, in the eval set and in a copy without session files alike;
    --no-env-redaction reads the output that the report names, as before."""
    folder = tmp_path / "eval" / "i001"
    output = write_folder(folder, report, session=session)
    with pytest.raises(replay.ReplayInputError) as refused:
        replay.transcript_files(folder, allow_unredacted=False)
    assert str(refused.value) == (
        f"i001: {REFUSAL}; run redact_eval_set.py on the eval set first, or pass --no-env-redaction"
    )
    assert replay.transcript_files(folder, allow_unredacted=True) == [output]


@pytest.mark.parametrize(
    ("report", "session", "source"),
    [
        pytest.param({**STEP_0, "version": 2, "value_sets": EVERY_SET}, True, SESSION_NAME, id="this-build-eval-set"),
        pytest.param({**STEP_0, "version": 2, "value_sets": EVERY_SET}, False, SESSION_NAME, id="this-build-copy"),
        pytest.param(EXPORT, False, EXPORT_SOURCE, id="export-version-1"),
    ],
)
def test_replay_reads_a_report_this_build_writes(
    tmp_path: Path, report: dict[str, Any], session: bool, source: str
) -> None:
    """MERGE-1: a report of this build's step 0, in the eval set or its copy, and export_shadow_runs.py's (version 1,
    the dispatcher's own copy, which step 0 never redacts again) are read as before."""
    folder = tmp_path / "eval" / "i001"
    output = write_folder(folder, report, session=session, source=source)
    assert replay.transcript_files(folder, allow_unredacted=False) == [output]
