"""Final review of Phase 2, part A (build step RA1): step 0 of the gate replay and where the gate's files live.

- TOOLS_SECURITY-1, GT8 of spec §16 extended: `redact_eval_set.py` reads the repair: section's token_file as the
  dispatcher reads it and removes the dispatcher's repair token, counted under `repair_token`, as the live copy does; a
  token_file it cannot use gives one line naming the file, never a value.
- TOOLS_SECURITY-2, GT8 extended: the local mode's values, those of the environment and of the local CLIs' credential
  files, are removed whatever dispatch.yaml's `runtime.execution` says, since the mode that counts is each run's own;
  `redaction.json` names the value sets used.
- SECRETS_AND_ISOLATION-3: `.gitignore` ignores the gate's eval sets, replays and shadow exports at the checkout's root,
  and no file the repository tracks.

It runs in the cairn environment, like test_redact_eval_set.py. Every value is made up; the credential files and the
token file are written into temporary folders.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from gatekit import REPOSITORY, Call, EvalCase, Session, dispatch_yaml, fake_secret, write_eval_set

pytest.importorskip("cairn.dispatcher.repair.redaction")

from test_redact_eval_set import CANARIES, REVIEWER_KEY, write_credential_files

import redact_eval_set
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import redaction

# A made-up dispatcher token in the shape the runbook's `secrets.token_hex(32)` gives: 64 hex characters.
REPAIR_TOKEN = hashlib.sha256(b"made-up repair token of the final review tests").hexdigest()
ENVIRONMENT_KEY = fake_secret("lab-provider-env-key", 44)  # a secret-named variable of the shell that runs step 0
EVERY_SET = ["worker_env", "environment", "credential_files", "repair_token", "server_url", "reviewers"]
DISPATCH_TOKEN_FILE = "/nonexistent/repair-dispatcher.token"  # noqa: S105 - the path gatekit's dispatch.yaml names


@pytest.fixture(autouse=True)
def _own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of the test's own, so that no credential file of the machine is read."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def token_file(tmp_path: Path, content: bytes | None) -> Path:
    """The dispatcher's token file, with `content` (None: no file), mode 0600 as the runbook makes it."""
    folder = tmp_path / "secrets"
    folder.mkdir(exist_ok=True)
    path = folder / "repair-dispatcher.token"
    if content is not None:
        path.write_bytes(content)
        path.chmod(0o600)
    return path


def dispatch_file(tmp_path: Path, execution: str, token_path: Path) -> Path:
    path = tmp_path / "dispatch.yaml"
    text = dispatch_yaml(execution=execution, reviewer_key=REVIEWER_KEY)
    assert DISPATCH_TOKEN_FILE in text
    path.write_text(text.replace(DISPATCH_TOKEN_FILE, str(token_path)), encoding="utf-8")
    return path


def printing_case(*printed: str) -> EvalCase:
    """A local run whose calls print each text: a run made after Phase 2 was set up (spec §11.2: a local worker can
    read the dispatcher's token file)."""
    calls = tuple(
        Call(20 + 20 * number, 25 + 20 * number, f"cat lab-notes-{number}.txt", text)
        for number, text in enumerate(printed)
    )
    session = Session("claudecode", calls, conclude_at=1805, session_id="01a1dafd-0000-7000-8000-0000000000c1")
    return EvalCase("i001", "claudecode", 1800, "stuck", session=session)


def session_file(eval_dir: Path) -> Path:
    (found,) = (
        path for path in (eval_dir / "i001").iterdir() if path.name.endswith(".jsonl") and "redacted" not in path.name
    )
    return found


@pytest.mark.parametrize("execution", ["local", "container"])
def test_redact_eval_set_uses_the_dispatcher_redaction_repair_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], execution: str
) -> None:
    """GT8, extended for the final review's TOOLS_SECURITY-1: with a dispatch.yaml whose token_file holds a made-up
    token, with the line break and spaces a file may hold around it, a session file that prints that token bare, after
    a path and inside a JSON string is redacted to a transcript.redacted.jsonl that holds no token, and redaction.json
    counts every occurrence under repair_token, never with the value, as the live copy's redaction of the same bytes
    does; the reviewer's key is removed as before."""
    path = token_file(tmp_path, f"\n  {REPAIR_TOKEN}  \n".encode())
    case = printing_case(
        REPAIR_TOKEN, f"secrets/repair-dispatcher.token:{REPAIR_TOKEN}", json.dumps({"t": REPAIR_TOKEN})
    )
    eval_dir = write_eval_set(tmp_path, [case], mode=execution)
    dispatch = dispatch_file(tmp_path, execution, path)
    original = session_file(eval_dir).read_bytes()
    seen = original.count(REPAIR_TOKEN.encode())
    assert seen >= 3

    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    printed = capsys.readouterr().out
    redacted = (eval_dir / "i001" / "transcript.redacted.jsonl").read_bytes()
    assert REPAIR_TOKEN.encode() not in redacted
    assert redacted.count(b"[REDACTED:repair_token]") == seen
    report_text = (eval_dir / "i001" / "redaction.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["redactions"]["repair_token"] == seen
    assert report["files"][0]["redactions"]["repair_token"] == seen
    assert report["value_sets"] == EVERY_SET
    assert f"  repair_token: {seen}" in printed
    assert "value_sets=" + ",".join(EVERY_SET) in printed and f"execution={execution}" in printed
    assert REPAIR_TOKEN not in printed + report_text and "repair.token_file" not in printed
    # The live copy's redaction of the same bytes, for a run of this mode with the dispatcher's token (spec 8.8).
    config = DispatchConfig.load(dispatch)
    live, counts = redaction.redact_exact(
        original, redaction.secret_values(config, execution, os.environ, REPAIR_TOKEN)
    )
    assert counts["repair_token"] == seen and live == redacted


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "token_unreadable"),
        (b"made-up-short-token\n", "shorter_than_32"),
        ("made-up-token-with-a-non-ascii-é-character-0123456789".encode(), "not_ascii"),
    ],
    ids=["missing", "short", "not-ascii"],
)
def test_redact_eval_set_uses_the_dispatcher_redaction_repair_token_it_cannot_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes | None, reason: str
) -> None:
    """GT8, extended: a token_file that cannot be read, or whose content the dispatcher would refuse (the dispatcher's
    reasons), gives one line that names the file and the reason, never a value; the other values are removed all the
    same, the tool exits 0, and redaction.json's value_sets has no repair_token."""
    path = token_file(tmp_path, content)
    case = printing_case(f"REVIEW_API_KEY={REVIEWER_KEY}", "made-up-short-token")
    eval_dir = write_eval_set(tmp_path, [case], mode="local")
    dispatch = dispatch_file(tmp_path, "local", path)
    keys = session_file(eval_dir).read_bytes().count(REVIEWER_KEY.encode())
    assert keys >= 1

    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    captured = capsys.readouterr()
    notes = [line for line in captured.out.splitlines() if "repair.token_file" in line]
    assert notes == [
        f"note: repair.token_file {path}: {reason}; the dispatcher's repair token is not among the values removed"
    ]
    if content is not None:
        assert content.decode("utf-8").strip() not in captured.out + captured.err
    redacted = (eval_dir / "i001" / "transcript.redacted.jsonl").read_bytes()
    assert REVIEWER_KEY.encode() not in redacted and b"made-up-short-token" in redacted  # never a short value
    report = json.loads((eval_dir / "i001" / "redaction.json").read_text(encoding="utf-8"))
    assert report["value_sets"] == [name for name in EVERY_SET if name != "repair_token"]
    assert "repair_token" not in report["redactions"] and report["redactions"]["REVIEW_API_KEY"] == keys


def test_redact_eval_set_uses_the_dispatcher_redaction_whatever_the_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _own_home: Path
) -> None:
    """GT8, extended for the final review's TOOLS_SECURITY-2: a local-mode run of the set (Phase 0 records no mode),
    redacted with a container-mode dispatch.yaml, as after a move to Compose, loses the values of the environment that
    runs step 0 and of the local CLIs' credential files exactly as the live copy of that local run did: none is left,
    each is counted under its variable's name or its file's label, and redaction.json names the local mode's value
    sets, as the summary line does."""
    monkeypatch.setenv("LAB_PROVIDER_API_KEY", ENVIRONMENT_KEY)
    home = _own_home
    write_credential_files(home / ".claude", home / ".claude.json", home / ".codex", home / ".pi" / "agent")
    case = printing_case(ENVIRONMENT_KEY, *CANARIES.values())
    eval_dir = write_eval_set(tmp_path, [case], mode="local")
    dispatch = dispatch_file(tmp_path, "container", token_file(tmp_path, None))
    original = session_file(eval_dir).read_bytes()

    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    printed = capsys.readouterr().out
    redacted = (eval_dir / "i001" / "transcript.redacted.jsonl").read_bytes()
    for value in (ENVIRONMENT_KEY, *CANARIES.values()):
        assert value.encode() in original and value.encode() not in redacted, value
    report = json.loads((eval_dir / "i001" / "redaction.json").read_text(encoding="utf-8"))
    assert report["redactions"]["LAB_PROVIDER_API_KEY"] == original.count(ENVIRONMENT_KEY.encode())
    assert all(report["redactions"][label] >= original.count(CANARIES[label].encode()) for label in CANARIES)
    assert report["value_sets"][:3] == ["worker_env", "environment", "credential_files"]
    assert "execution=container, value_sets=worker_env,environment,credential_files," in printed
    # What the live copy of this local run removed (spec 8.8: secret_values with the run's own mode, local).
    config = DispatchConfig.load(dispatch)
    live, _ = redaction.redact_exact(original, redaction.secret_values(config, "local", os.environ, None))
    assert live == redacted
    # dispatch.yaml's mode alone, as before the fix, would have left them (the values the replay would send).
    alone, _ = redaction.redact_exact(original, redaction.secret_values(config, "container", os.environ, None))
    assert ENVIRONMENT_KEY.encode() in alone and CANARIES["claude:.credentials.json"].encode() in alone


# --- SECRETS_AND_ISOLATION-3 ------------------------------------------------------------------------------------------

GATE_FILES = [
    "eval-proj_027/i014/0199abcd-0000-7000-8000-000000000001.jsonl",
    "eval-proj_027/i014/transcript.redacted.jsonl",
    "eval-proj_027/i014/attempt-2/transcript.redacted.jsonl",
    "eval-proj_027/project.json",
    "eval-proj_027/labels.csv",
    "eval-proj_027-redacted/i014/transcript.redacted.jsonl",
    "replay-027/digests/r1/i014.r1.txt",
    "replay-027/decisions.jsonl",
    "replay-027/decisions-injected.jsonl",
    "replay-027/gate_report.json",
    "replay-027-smoke/decisions.jsonl",
    "shadow-proj_031/i014/digest.txt",
    "shadow-proj_031/decisions.jsonl",
    # Fix round 1 (SAFETY-4): the live gate of §15.6 run from the checkout's root without --out, and its labels there
    "gate_report.json",
    "gate_report.json.tmp",
    "P-labels.csv",
    "proj_031-labels.csv",
    "labels.csv",
]
NOT_GATE_FILES = [
    "tools/eval-notes/x.md",
    "docs/replay-guide/x.md",
    "cairn/shadow-mode/x.py",
    "eval-notes.md",
    "docs/gate_report.json",
    "tools/repair-phase2/tests/proj_031-labels.csv",
]


def ignored(repository: Path, paths: list[str]) -> set[str]:
    """The paths the given .gitignore ignores, by git itself in a scratch repository with no other exclude file."""
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    found = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )
    assert found.returncode in (0, 1), found.stderr  # 1: none of them is ignored
    return set(found.stdout.splitlines())


def test_the_gate_folders_at_the_checkout_root_are_ignored_by_git(tmp_path: Path) -> None:
    """SECRETS_AND_ISOLATION-3: the eval set (session files, project.json, redacted transcripts), its Compose copy, the
    replays (digests, decisions, the gate report) and the shadow exports, made where the documented commands make them,
    at the checkout's root, are ignored by the repository's .gitignore; the same names deeper in the tree are not (the
    patterns are anchored), and no file the repository tracks is ignored."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    scratch = tmp_path / "repository"
    scratch.mkdir()
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(scratch)], check=True, env=environment, timeout=60)
    shutil.copyfile(REPOSITORY / ".gitignore", scratch / ".gitignore")
    assert ignored(scratch, GATE_FILES) == set(GATE_FILES)
    assert ignored(scratch, NOT_GATE_FILES) == set()
    listed = subprocess.run(
        ["git", "-C", str(REPOSITORY), "ls-files"], capture_output=True, text=True, timeout=60, check=False
    )
    if listed.returncode != 0:
        pytest.skip("the repository is not a git checkout here")
    tracked = listed.stdout.splitlines()
    assert tracked and ignored(scratch, tracked) == set()
