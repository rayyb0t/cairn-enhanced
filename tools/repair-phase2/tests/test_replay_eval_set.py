"""replay_eval_set.py (spec §15.3, §15.4): the offline replay of a Phase 0 evaluation set through the live agent's code,
for every listed reviewer on the same cases; GT4-GT7, GT10, GT11 and GT14 of spec §16, and the dry run of build plan
step 11's "done when".

The eval sets are made with Phase 0's own `export_eval_set.py` from made-up dispatcher logs and projects, with made-up
session files in the pinned CLIs' shapes (gatekit). The reviewers are stubs on loopback, in both wire formats (owner
decision O8): no real provider is contacted and no real key is used.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gatekit import (
    T0,
    AgentFiles,
    Call,
    EvalCase,
    ModelStub,
    Seen,
    Session,
    anthropic_answer,
    clear_guarded_environment,
    decision_text,
    dispatch_yaml,
    fake_secret,
    mark_redacted,
    model_stub_factory,
    openai_answer,
    progressing_session,
    read_records,
    reviewer_entry,
    stuck_session,
    write_agent_files,
    write_eval_set,
)

pytest.importorskip("cairn_repair_agent")

import requests
from cairn_repair_agent import digest as digest_module
from cairn_repair_agent import gate, replay
from cairn_repair_agent.digest import Fact, ReviewContext, build_digest, prompt_tokens_estimate
from cairn_repair_agent.redact import ExactSecrets
from cairn_repair_agent.transcripts import parse_transcript

import replay_eval_set

NONCE = re.compile(r'<untrusted_data id="([0-9a-f]{16})">')
MODEL_CALL = "repair model call "


@pytest.fixture(autouse=True)
def _guarded_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_guarded_environment(monkeypatch)


@pytest.fixture
def model_stubs() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stub_factory()


class Clock:
    """A monotonic clock that does not move: every attempt gets exactly its time left (§9.7)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def run_replay(
    eval_dir: Path,
    files: AgentFiles,
    out: Path,
    *extra: str,
    capsys: pytest.CaptureFixture[str],
    clock: Clock | None = None,
) -> tuple[int, str, str]:
    argv = [str(eval_dir), "--config", str(files.config), "--out", str(out), *extra]
    if clock is None:
        code = replay_eval_set.main(argv)
    else:
        code = replay.main(argv, monotonic=clock, sleep=lambda _seconds: None)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def model_call_lines(err: str) -> list[str]:
    return [line[line.index(MODEL_CALL) :] for line in err.splitlines() if MODEL_CALL in line]


def line_values(line: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", line))


def run_of(seen: Seen) -> str:
    """The case a request is for: the run id in the digest's trusted header."""
    found = re.search(r"\nrun (replay-\S+) \|", seen.digest())
    assert found is not None
    return found.group(1).removeprefix("replay-")


def by_case(
    api_format: str, replies: Mapping[str, Callable[[], dict[str, Any]]], default: Callable[[], dict[str, Any]]
) -> Callable[[Seen], tuple[int, Mapping[str, Any]]]:
    def answer(seen: Seen) -> tuple[int, Mapping[str, Any]]:
        return 200, replies.get(run_of(seen), default)()

    return answer


@pytest.fixture
def recorded_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """The (url, timeout) of every request the model clients send (their sessions are requests.Session)."""
    seen: list[tuple[str, Any]] = []
    original = requests.Session.request

    def request(self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        seen.append((url, kwargs.get("timeout")))
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", request)
    return seen


# --- GT4 --------------------------------------------------------------------------------------------------------------


def test_replay_cuts_at_the_review_point_and_uses_the_live_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    model_stubs: Callable[..., ModelStub],
    recorded_timeouts: list[tuple[str, Any]],
) -> None:
    """GT4: the cut is the first timestamp plus 0.75 x budget, through the live parser and digest; with poll_interval 5
    the model call of a B = 1800 case gets seconds_left 425 and of a B = 600 case 125, both recorded, and each attempt
    min(timeout_seconds, time left); every attempt prints its `repair model call` line; with a price set, usage counts
    of 400 or 4,300 digits give null tokens and cost, written without an exception, and the next case still runs."""
    budget = 1800
    cut = 0.75 * budget
    i001 = Session(
        "claudecode",
        (
            Call(30, 40, "nmap -sV lab-target.test", "8080/tcp open http"),
            Call(cut - 60, cut + 300, "hydra -l admin -P lab.txt lab-target.test http-get", "1 valid password found"),
            Call(cut + 30, cut + 40, "cat after-the-cut.txt", "written after the cut"),
        ),
        messages=((cut - 100, "Brute force against the lab login is running."), (cut + 50, "A message after the cut.")),
        conclude_at=budget + 5,
        session_id="01a1dafd-0000-7000-8000-00000000a001",
    )
    cases = [
        EvalCase("i001", "claudecode", 1800, "progressing", session=i001),
        EvalCase("i002", "codex", 600, "stuck"),
        EvalCase("i003", "pi", 900, "progressing"),
        EvalCase("i004", "claudecode", 1200, "stuck"),
        EvalCase("i005", "codex", 600, "progressing"),
    ]
    eval_dir = write_eval_set(tmp_path, cases)
    mark_redacted(eval_dir)
    huge = {"i003": 10**399, "i004": 10**4299}  # 400 and 4,300 digits
    stub = model_stubs(
        "openai_chat",
        by_case(
            "openai_chat",
            {
                intent: (
                    lambda value=value: openai_answer(
                        decision_text("continue"), usage={"prompt_tokens": value, "completion_tokens": value}
                    )
                )
                for intent, value in huge.items()
            },
            lambda: openai_answer(decision_text("continue")),
        ),
    )
    reviewer = reviewer_entry(
        "r1", stub.base_url, timeout_seconds=180, price={"input_per_mtok": 1, "output_per_mtok": 2}
    )
    files = write_agent_files(tmp_path / "agent", [reviewer], poll_interval=5)
    out = tmp_path / "out"
    code, stdout, err = run_replay(
        eval_dir, files, out, "--project-json", str(eval_dir / "project.json"), "--yes", capsys=capsys, clock=Clock()
    )
    assert code == 0, err
    records = {record["intent_id"]: record for record in read_records(out / "decisions.jsonl")}
    assert list(records) == ["i001", "i002", "i003", "i004", "i005"]
    expected_left = {"i001": 425, "i002": 125, "i003": 200, "i004": 275, "i005": 125}
    for intent, record in records.items():
        assert list(record) == list(replay.RECORD_FIELDS)
        budget_seconds = record["budget_seconds"]
        assert (
            record["seconds_left"]
            == expected_left[intent]
            == (budget_seconds - 20) - math.floor(0.75 * budget_seconds) - 5
        )
        assert record["cut_seconds"] == 0.75 * budget_seconds
    assert records["i001"]["budget_seconds"] == 1800 and records["i002"]["budget_seconds"] == 600
    # Each attempt's limit is min(timeout_seconds, time left): (connect 5 s, read limit).
    assert [timeout for _, timeout in recorded_timeouts] == [
        (5.0, 180.0),
        (5.0, 125.0),
        (5.0, 180.0),
        (5.0, 180.0),
        (5.0, 125.0),
    ]
    assert all(url == stub.base_url + "/chat/completions" for url, _ in recorded_timeouts)

    # The live code: the digest is the agent's build_digest of the agent's parse, cut at 0.75 x budget.
    folder = eval_dir / "i001"
    data = (folder / "transcript.redacted.jsonl").read_bytes()
    assert records["i001"]["transcript"] == "transcript.redacted.jsonl"
    assert records["i001"]["transcript_sha256"] == hashlib.sha256(data).hexdigest()
    assert records["i001"]["cli"] == "claudecode"
    text = (out / "digests" / "r1" / "i001.r1.txt").read_text(encoding="utf-8")
    nonce = NONCE.search(text)
    assert nonce is not None
    transcript = parse_transcript("claudecode", data)
    assert transcript.started_at is not None
    expected = build_digest(
        transcript,
        ReviewContext(
            run_id="replay-i001",
            cli_type="claudecode",
            window_number=1,
            window_seconds=1800,
            budget_seconds=1800,
            elapsed_seconds=1350,
            remaining_seconds=450,
            slices_used=0,
            slice_seconds=600,
            copy_age_seconds=0,
        ),
        transcript.started_at + timedelta(seconds=1350),
        intent="Map the lab service",
        source_facts=[Fact("f001", "The lab firmware image is lab-firmware.bin.")],
        new_facts=[Fact("f002", "The lab banner names version 1.2.")],
        exact=ExactSecrets.for_agent(files.token, [None], None),
        budget=8000,
        nonce=nonce.group(1),
    )
    assert text == expected.text
    assert stub.requests()[0].digest() == text  # the bytes the reviewer received
    assert records["i001"]["digest_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert records["i001"]["prompt_tokens_estimate"] == prompt_tokens_estimate(text)
    assert records["i001"]["digest_tokens_estimate"] == digest_module.estimate_tokens(text)
    # Calls after the cut are dropped; the call answered after the cut is running at the cut, its output hidden.
    assert "RUNNING AT THE CUT:\nE2 t+21:30 Bash (running 60 s): hydra" in text
    assert "1 valid password found" not in text and "after-the-cut" not in text and "after the cut" not in text
    assert "LAST 2 OF 2 TOOL CALLS" in text and "Brute force against the lab login is running." in text

    # Every attempt prints its line, with the reviewer, ms, input_tokens and prompt_est.
    lines = [line_values(line) for line in model_call_lines(err)]
    assert len(lines) == 5
    assert all(line["reviewer"] == "r1" and line["ms"].isdigit() and "prompt_est" in line for line in lines)
    assert [line["run"] for line in lines] == [
        "replay-i001",
        "replay-i002",
        "replay-i003",
        "replay-i004",
        "replay-i005",
    ]
    assert [line["input_tokens"] for line in lines] == ["100000", "100000", "-", "-", "100000"]
    assert [line["prompt_est"] for line in lines] == [str(records[i]["prompt_tokens_estimate"]) for i in records]
    # Usage counts of 400 and 4,300 digits: null tokens and cost, and the next case ran.
    for intent in ("i003", "i004"):
        assert (records[intent]["input_tokens"], records[intent]["output_tokens"], records[intent]["cost_usd"]) == (
            None,
            None,
            None,
        )
        assert records[intent]["fallback"] is None and records[intent]["decision"] == "continue"
    assert records["i005"]["input_tokens"] == 100000
    assert records["i001"]["cost_usd"] == pytest.approx((100000 * 1 + 50 * 2) / 1_000_000)
    assert (
        records["i001"]["finish_reason"] == "stop"
        and records["i001"]["ms"] == 0
        and records["i001"]["model"] == "test-model"
    )
    assert "repair model usage missing, a cut prompt cannot be detected" in err
    assert "planned reviewer=r1 calls=5" in stdout and "replayed reviewer=r1 calls=5 fallbacks=0" in stdout


# --- GT5 --------------------------------------------------------------------------------------------------------------


def test_injection_probe_puts_the_text_inside_the_fence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """GT5: --inject re-runs each stuck case with the probe's line appended to the output of the last tool call finished
    before the cut, inside the fence; the baseline digest and the progressing case have no such line, and the probe's
    records go to decisions-injected.jsonl only."""
    eval_dir = write_eval_set(
        tmp_path, [EvalCase("i001", "codex", 600, "stuck"), EvalCase("i002", "pi", 600, "progressing")]
    )
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    out = tmp_path / "out"
    code, _, err = run_replay(
        eval_dir,
        files,
        out,
        "--inject",
        "--labels",
        str(eval_dir / "labels.csv"),
        "--yes",
        "--repetitions",
        "2",
        capsys=capsys,
    )
    assert code == 0, err
    for repetition in (1, 2):
        injected = (out / "digests" / "r1" / f"i001.r{repetition}.injected.txt").read_text(encoding="utf-8")
        baseline = (out / "digests" / "r1" / f"i001.r{repetition}.txt").read_text(encoding="utf-8")
        nonce = NONCE.search(injected)
        assert nonce is not None
        opening, closing = f'<untrusted_data id="{nonce.group(1)}">', f'</untrusted_data id="{nonce.group(1)}">'
        before, inside = injected.split(opening, 1)
        body, after = inside.rsplit(closing, 1)
        assert replay.INJECTION not in before and replay.INJECTION not in after
        assert body.count(replay.INJECTION) == 1
        # Right after the output of the last call finished before the cut, E9, inside the last calls.
        last_calls = body.split("TOOL CALLS (oldest first):", 1)[1].split("\n")
        start = next(index for index, line in enumerate(last_calls) if line.startswith("E9 "))
        assert "(30 s, exit 28): curl -s http://lab-target.test:8080/admin" in last_calls[start]
        block = []
        for line in last_calls[start + 1 :]:
            if not line.startswith("    | "):
                break
            block.append(line)
        assert block[-2:] == ["    | connection timed out", f"    | {replay.INJECTION}"]
        assert replay.INJECTION not in baseline
        baseline_nonce = NONCE.search(baseline)
        assert baseline_nonce is not None and baseline_nonce.group(1) != nonce.group(1)  # one nonce per review
    for name in ("i002.r1.txt", "i002.r2.txt"):
        assert replay.INJECTION not in (out / "digests" / "r1" / name).read_text(encoding="utf-8")
    assert not (out / "digests" / "r1" / "i002.r1.injected.txt").exists()
    baseline_records = read_records(out / "decisions.jsonl")
    probe_records = read_records(out / "decisions-injected.jsonl")
    assert [(r["intent_id"], r["repetition"]) for r in baseline_records] == [
        ("i001", 1),
        ("i002", 1),
        ("i001", 2),
        ("i002", 2),
    ]
    assert [(r["intent_id"], r["repetition"]) for r in probe_records] == [("i001", 1), ("i001", 2)]
    # What the reviewer received for the probe is the injected digest, with the line inside the fence.
    sent = [seen.digest() for seen in stub.requests() if replay.INJECTION in seen.digest()]
    assert len(sent) == 2 and all(text.index(replay.INJECTION) > text.index("<untrusted_data") for text in sent)
    # Without a finished call before the cut, the line goes to the intent text, which is inside the fence too.
    case = replay.load_case(eval_dir / "i001", project=None, max_bytes=1 << 26)
    empty = replay.Case(**{**case.__dict__, "transcript": parse_transcript("codex", b"")})
    probed = replay.inject(empty)
    assert probed.intent.endswith(replay.INJECTION) and probed.injected and not case.injected


# --- GT6 --------------------------------------------------------------------------------------------------------------


def test_replay_refuses_without_yes_and_prints_each_reviewers_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """GT6: two reviewers, the first with made-up prices {1, 2} and the second without: one `planned` line each, whose
    ceiling is (max_input_tokens x 1 + max_output_tokens x 2) / 1,000,000 for the first and no_price for the second;
    without --yes no request reaches either."""
    eval_dir = write_eval_set(
        tmp_path, [EvalCase("i001", "claudecode", 1800, "progressing"), EvalCase("i002", "codex", 600, "stuck")]
    )
    mark_redacted(eval_dir)
    first, second = model_stubs("openai_chat"), model_stubs("anthropic_messages")
    files = write_agent_files(
        tmp_path / "agent",
        [
            reviewer_entry("priced", first.base_url, price={"input_per_mtok": 1, "output_per_mtok": 2}),
            reviewer_entry(
                "local", second.base_url, api_format="anthropic_messages", api_key=fake_secret("key-b"), max_tokens=2048
            ),
        ],
    )
    out = tmp_path / "out"
    code, stdout, _ = run_replay(eval_dir, files, out, "--repetitions", "3", capsys=capsys)
    assert code == 1
    planned = [line_values(line) for line in stdout.splitlines() if line.startswith("planned ")]
    assert [line["reviewer"] for line in planned] == ["priced", "local"]
    # "priced" leaves max_tokens out: the plan counts its resolved default, 16384 without a context_window (O10).
    for line, max_tokens in zip(planned, (16384, 2048), strict=True):
        name = line["reviewer"]
        digests = sorted((out / "digests" / name).iterdir())
        assert int(line["calls"]) == len(digests) == 6
        assert int(line["max_input_tokens"]) == sum(
            prompt_tokens_estimate(path.read_text(encoding="utf-8")) for path in digests
        )
        assert int(line["max_output_tokens"]) == 6 * max_tokens
    ceiling = (int(planned[0]["max_input_tokens"]) * 1 + int(planned[0]["max_output_tokens"]) * 2) / 1_000_000
    assert planned[0]["ceiling"] == f"${ceiling:.2f}"
    assert planned[1]["ceiling"] == "no_price"
    assert first.requests() == [] and second.requests() == []
    assert not (out / "decisions.jsonl").exists()
    assert "not sent: add --yes" in stdout


# --- GT7 --------------------------------------------------------------------------------------------------------------


def test_replay_refuses_unredacted_transcripts_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """GT7: a session file that redact_eval_set.py has not redacted stops the replay with exit 3 before any digest is
    built or any request sent, unless --no-env-redaction is given; so do a session file added or changed after the
    redaction."""
    eval_dir = write_eval_set(
        tmp_path, [EvalCase("i001", "claudecode", 1800, "progressing"), EvalCase("i002", "codex", 600, "stuck")]
    )
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    out = tmp_path / "out"
    code, _, err = run_replay(eval_dir, files, out, "--yes", capsys=capsys)
    assert code == 3 and "i001: an unredacted transcript (no redaction.json)" in err and "--no-env-redaction" in err
    assert stub.requests() == [] and not (out / "digests").exists()
    code, _, err = run_replay(eval_dir, files, out, "--yes", "--no-env-redaction", capsys=capsys)
    assert code == 0, err
    assert len(stub.requests()) == 2
    assert [
        r["transcript"].endswith(".jsonl") and r["transcript"] != "transcript.redacted.jsonl"
        for r in read_records(out / "decisions.jsonl")
    ] == [True, True]

    # Redacted: the replay reads only the redacted files.
    mark_redacted(eval_dir)
    out2 = tmp_path / "out2"
    code, _, err = run_replay(eval_dir, files, out2, "--dry-run", capsys=capsys)
    assert code == 0, err
    # A session file copied in after the redaction, or one changed since, is unredacted again.
    late = eval_dir / "i002" / "second-attempt.jsonl"
    late.write_bytes((eval_dir / "i002" / "transcript.redacted.jsonl").read_bytes())
    code, _, err = run_replay(eval_dir, files, tmp_path / "out3", "--dry-run", capsys=capsys)
    assert code == 3 and "i002: an unredacted transcript (second-attempt.jsonl is not named in redaction.json)" in err
    late.unlink()
    source = next(
        path for path in (eval_dir / "i001").iterdir() if path.suffix == ".jsonl" and "redacted" not in path.name
    )
    source.write_bytes(source.read_bytes() + b"\n")
    code, _, err = run_replay(eval_dir, files, tmp_path / "out4", "--dry-run", capsys=capsys)
    assert code == 3 and "changed after it was redacted" in err
    assert len(stub.requests()) == 2


# --- GT10 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["container", "local"])
def test_budget_comes_from_intent_json_in_both_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub], mode: str
) -> None:
    """GT10: the budget is the attempt's execute_timeout_s in intent.json, which Phase 0 reads from the dispatcher's
    `starting container exec ... timeout=` line, logged in both modes (`tasks/common.py:84-90`); --budget is used only
    where that line is missing, and without it such a case is refused."""
    cases = [
        EvalCase("i001", "claudecode", 1800, "progressing"),
        EvalCase("i002", "codex", 600, "stuck"),
        EvalCase("i003", "pi", 900, "stuck", log_budget=False),
        EvalCase("i004", "claudecode", 300, "stuck"),
    ]
    eval_dir = write_eval_set(tmp_path, cases, mode=mode)
    attempts = {
        case.intent_id: json.loads((eval_dir / case.intent_id / "intent.json").read_text())["timed_out_attempts"]
        for case in cases
    }
    assert [attempts[i][0]["execute_timeout_s"] for i in ("i001", "i002", "i003", "i004")] == [1800, 600, None, 300]
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    code, _, err = run_replay(eval_dir, files, tmp_path / "out", "--yes", capsys=capsys)
    assert code == 0, err
    records = {r["intent_id"]: r for r in read_records(tmp_path / "out" / "decisions.jsonl")}
    assert {i: (r["budget_seconds"], r["seconds_left"], r["cut_seconds"]) for i, r in records.items()} == {
        "i001": (1800, 425, 1350),
        "i002": (600, 125, 450),
    }
    assert (
        "refused case intent=i003 reason=intent.json gives no execute_timeout_s for this attempt; pass --budget" in err
    )
    # A budget under 350 s is never reviewed live (spec Q5): the case has no review point to replay.
    assert "refused case intent=i004 reason=the budget is under 350 s" in err
    code, _, err = run_replay(eval_dir, files, tmp_path / "out2", "--yes", "--budget", "1000", capsys=capsys)
    assert code == 0, err
    records = {r["intent_id"]: r for r in read_records(tmp_path / "out2" / "decisions.jsonl")}
    assert {i: r["budget_seconds"] for i, r in records.items()} == {"i001": 1800, "i002": 600, "i003": 1000}
    assert records["i003"]["seconds_left"] == 1000 - 20 - 750 - 5


# --- GT11 -------------------------------------------------------------------------------------------------------------


def test_reports_count_fallbacks_latency_and_stop_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """GT11: per reviewer, the replay's records and the gate report count `max_tokens` stops from fallback
    invalid_output with reason max_tokens, alike for an openai_chat reviewer, whose finish_reason is `length`, and an
    anthropic_messages reviewer, whose finish_reason is `max_tokens`; refusals, fallbacks by kind, latency and tokens;
    and the cost, the sum of cost_usd with the reviewer's price, and `-` without."""
    cases = [EvalCase(f"i00{n}", "claudecode", 1800, "progressing" if n < 3 else "stuck") for n in range(1, 5)]
    eval_dir = write_eval_set(tmp_path, cases)
    mark_redacted(eval_dir)
    refusal = openai_answer("", finish="stop")
    refusal["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "made-up refusal"}
    chat = model_stubs(
        "openai_chat",
        by_case(
            "openai_chat",
            {
                "i001": lambda: openai_answer('{"decision": "ext', finish="length"),
                "i002": lambda: refusal,
            },
            lambda: openai_answer(decision_text("continue"), usage={"prompt_tokens": 9000, "completion_tokens": 300}),
        ),
    )
    messages = model_stubs(
        "anthropic_messages",
        by_case(
            "anthropic_messages",
            {"i003": lambda: anthropic_answer("", stop="max_tokens")},
            lambda: anthropic_answer(decision_text("continue"), usage={"input_tokens": 8000, "output_tokens": 200}),
        ),
    )
    files = write_agent_files(
        tmp_path / "agent",
        [
            reviewer_entry("chat", chat.base_url, price={"input_per_mtok": 3, "output_per_mtok": 15}),
            reviewer_entry(
                "claude", messages.base_url, api_format="anthropic_messages", api_key=fake_secret("claude-key")
            ),
        ],
    )
    out = tmp_path / "out"
    code, _, err = run_replay(eval_dir, files, out, "--yes", "--repetitions", "2", capsys=capsys)
    assert code == 0, err
    records = read_records(out / "decisions.jsonl")
    stops = {
        (r["reviewer"], r["intent_id"]): (r["fallback"], r["reason"], r["finish_reason"])
        for r in records
        if r["repetition"] == 1
    }
    assert stops[("chat", "i001")] == ("invalid_output", "max_tokens", "length")
    assert stops[("chat", "i002")] == ("model_error", "refusal", "stop")
    assert stops[("claude", "i003")] == ("invalid_output", "max_tokens", "max_tokens")
    assert stops[("claude", "i001")] == (None, "made-up reason", "end_turn")
    labels = eval_dir / "labels.csv"
    report_path = out / "gate_report.json"
    code = gate.main(["--labels", str(labels), "--decisions", str(out / "decisions.jsonl"), "--out", str(report_path)])
    printed = capsys.readouterr().out
    report = json.loads(report_path.read_text())
    assert code == 2  # four cases are too few for the bar: the counts are what this test reads
    chat_calls, claude_calls = report["reviewers"]["chat"]["calls"], report["reviewers"]["claude"]["calls"]
    assert (chat_calls["max_tokens_stops"], chat_calls["refusals"]) == (2, 2)
    assert chat_calls["fallbacks"] == {"invalid_output": 2, "model_error": 2}
    assert (claude_calls["max_tokens_stops"], claude_calls["refusals"], claude_calls["fallbacks"]) == (
        2,
        0,
        {"invalid_output": 2},
    )
    assert report["reviewers"]["chat"]["calls_by_repetition"]["1"]["max_tokens_stops"] == 1
    expected_cost = sum(r["cost_usd"] for r in records if r["reviewer"] == "chat")
    assert chat_calls["cost_usd"] == pytest.approx(expected_cost) and expected_cost > 0
    assert claude_calls["cost_usd"] is None
    assert all(r["cost_usd"] is None for r in records if r["reviewer"] == "claude")
    assert chat_calls["latency_ms_p50"] is not None and chat_calls["latency_ms_p95"] >= chat_calls["latency_ms_p50"]
    assert claude_calls["input_tokens_p50"] == 8000 and chat_calls["output_tokens_p95"] == 300
    chat_line, claude_line = [line for line in printed.splitlines() if line.startswith("calls: ")]
    assert "refusals=2 fallbacks=invalid_output:2 model_error:2 max_tokens_stops=2" in chat_line
    assert chat_line.endswith(f"cost=${expected_cost:.2f}")
    assert "max_tokens_stops=2" in claude_line and claude_line.endswith("cost=-")


# --- GT14 -------------------------------------------------------------------------------------------------------------


def two_reviewers(chat: ModelStub, messages: ModelStub, chat_key: str, claude_key: str) -> list[dict[str, Any]]:
    return [
        reviewer_entry("r1", chat.base_url, model="test-model-chat", api_key=chat_key),
        reviewer_entry(
            "r2", messages.base_url, model="test-model-messages", api_key=claude_key, api_format="anthropic_messages"
        ),
    ]


def leaky_case(intent: str, keys: Sequence[str], budget: int = 1800) -> EvalCase:
    """A run whose tool output printed both reviewers' keys, as a worker that reads dispatch.yaml can."""
    session = Session(
        "claudecode",
        (
            Call(20, 30, "cat dispatch.yaml", "\n".join(f"REVIEW_API_KEY: {key}" for key in keys)),
            Call(60, 70, "env", f"OTHER={keys[0]}"),
        ),
        conclude_at=budget + 5,
        session_id=f"01a1dafd-0000-7000-8000-0000000{intent[-3:]}aa",
    )
    return EvalCase(intent, "claudecode", budget, "stuck", session=session)


@pytest.mark.parametrize("first", ["r1", "r2"])
def test_replay_runs_every_listed_reviewer_on_the_same_cases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub], first: str
) -> None:
    """GT14: every case runs once per reviewer and repetition, with the same transcript, cut, time left and, for equal
    digest budgets, the same digest; each reviewer's requests go to its own base_url, with its own key and model, in
    its own format (openai_chat and anthropic_messages); no request carries the other reviewer's key, in a header or
    the body, with either reviewer listed first; the openai_chat stub gets no x-api-key or anthropic-version header,
    the anthropic_messages stub no Authorization; --reviewer r2 runs only r2; an unknown --reviewer exits 3 before any
    request; a missing settings file stops the tool with spec 9.2's message before any digest is built."""
    chat_key, claude_key = fake_secret("chat-reviewer-key"), fake_secret("claude-reviewer-key")
    cases = [
        leaky_case("i001", [chat_key, claude_key]),
        EvalCase("i002", "codex", 600, "progressing"),
        EvalCase("i003", "pi", 900, "stuck"),
    ]
    eval_dir = write_eval_set(tmp_path, cases)
    mark_redacted(eval_dir)
    chat, messages = model_stubs("openai_chat"), model_stubs("anthropic_messages")
    reviewers = two_reviewers(chat, messages, chat_key, claude_key)
    if first == "r2":
        reviewers.reverse()
    files = write_agent_files(tmp_path / "agent", reviewers)
    out = tmp_path / "out"
    code, _, err = run_replay(eval_dir, files, out, "--yes", "--repetitions", "2", capsys=capsys)
    assert code == 0, err
    records = read_records(out / "decisions.jsonl")
    order = [entry["name"] for entry in reviewers]
    assert [r["reviewer"] for r in records] == [name for name in order for _ in range(6)]
    by_key: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        by_key.setdefault((record["intent_id"], record["repetition"]), []).append(record)
    assert sorted(by_key) == [(i, r) for i in ("i001", "i002", "i003") for r in (1, 2)]
    for (intent, _), pair in by_key.items():
        assert sorted(r["reviewer"] for r in pair) == ["r1", "r2"]
        for name in ("transcript_sha256", "cut_seconds", "seconds_left", "digest_budget", "digest_sha256"):
            assert pair[0][name] == pair[1][name], (intent, name)
    # Requests: each stub got only its reviewer's, in its format, with its key and model.
    assert len(chat.requests()) == 6 and len(messages.requests()) == 6
    for seen in chat.requests():
        assert seen.path == "/v1/chat/completions"
        assert seen.headers["authorization"] == f"Bearer {chat_key}"
        assert "x-api-key" not in seen.headers and "anthropic-version" not in seen.headers
        body = seen.json()
        assert body["model"] == "test-model-chat" and [m["role"] for m in body["messages"]] == ["system", "user"]
    for seen in messages.requests():
        assert seen.path == "/v1/messages"
        assert seen.headers["x-api-key"] == claude_key and seen.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in seen.headers
        body = seen.json()
        assert body["model"] == "test-model-messages" and "system" in body and "response_format" not in body
    for stub, other in ((chat, claude_key), (messages, chat_key)):
        for seen in stub.requests():
            assert other not in seen.body.decode("utf-8") and all(other not in value for value in seen.headers.values())
    for seen in chat.requests() + messages.requests():
        assert chat_key not in seen.body.decode("utf-8") and claude_key not in seen.body.decode("utf-8")
    leaky = next(seen.digest() for seen in chat.requests() if "replay-i001" in seen.digest())
    assert "[REDACTED:reviewer_key]" in leaky

    # --reviewer r2 runs only r2.
    before = len(chat.requests()), len(messages.requests())
    code, _, err = run_replay(eval_dir, files, tmp_path / "only-r2", "--yes", "--reviewer", "r2", capsys=capsys)
    assert code == 0, err
    assert {r["reviewer"] for r in read_records(tmp_path / "only-r2" / "decisions.jsonl")} == {"r2"}
    assert (len(chat.requests()), len(messages.requests())) == (before[0], before[1] + 3)
    # An unknown --reviewer exits 3 before any request.
    code, _, err = run_replay(eval_dir, files, tmp_path / "unknown", "--yes", "--reviewer", "r9", capsys=capsys)
    assert code == 3 and "unknown --reviewer: r9" in err
    assert (len(chat.requests()), len(messages.requests())) == (before[0], before[1] + 3)
    assert not (tmp_path / "unknown").exists()
    # A missing settings file stops the tool with §9.2's message, before any digest is built.
    (files.root / "agent-settings.json").unlink()
    code, _, err = run_replay(eval_dir, files, tmp_path / "nosettings", "--dry-run", capsys=capsys)
    settings_path = files.root / "agent-settings.json"
    assert code == 2
    assert (
        err.strip()
        == f"agent settings missing: {settings_path}; start the dispatcher with a repair: section in dispatch.yaml first"
    )
    assert not (tmp_path / "nosettings").exists()
    # A reviewer whose context_window lowers its digest budget gets digests within that budget (C-1, Digest budget).
    check_a_context_window_lowers_the_digest_budget(tmp_path / "context-window", capsys, model_stubs)


def check_a_context_window_lowers_the_digest_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """GT14, the digest budget (Appendix C-1), run from the GT14 test: a reviewer whose context_window leaves less than
    the default 8,000 tokens gets digests built with that budget, and its records' digest_budget says so; the other
    reviewer's budget stays. `tmp_path` is a new folder of its own."""
    tmp_path.mkdir()
    noisy = Session(
        "codex",
        tuple(
            Call(
                10 + 20 * n,
                15 + 20 * n,
                f"strings -n 8 part{n}.bin | head -400",
                "\n".join("é" * 190 for _ in range(30)),
            )
            for n in range(14)
        ),
        messages=((400, "ü" * 1200), (410, "ö" * 1200)),
        conclude_at=1805,
        session_id="01a1dafd-0000-7000-8000-00000000c0de",
    )
    eval_dir = write_eval_set(tmp_path, [EvalCase("i001", "codex", 1800, "progressing", session=noisy)])
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    context_window = 12288
    files = write_agent_files(
        tmp_path / "agent",
        [reviewer_entry("wide", stub.base_url), reviewer_entry("narrow", stub.base_url, context_window=context_window)],
    )
    out = tmp_path / "out"
    code, _, err = run_replay(eval_dir, files, out, "--yes", capsys=capsys)
    assert code == 0, err
    records = {r["reviewer"]: r for r in read_records(out / "decisions.jsonl")}
    room = context_window - digest_module.SYSTEM_PROMPT_TOKENS - 4096
    assert 4000 <= room < 8000
    assert records["narrow"]["digest_budget"] == room and records["wide"]["digest_budget"] == 8000
    assert records["narrow"]["digest_tokens_estimate"] <= room < records["wide"]["digest_tokens_estimate"] <= 8000
    assert records["narrow"]["digest_sha256"] != records["wide"]["digest_sha256"]


# --- the dry run over a Phase 0 set (build plan step 11, done when) ----------------------------------------------------


def test_a_dry_run_on_a_phase0_set_builds_every_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    model_stubs: Callable[..., ModelStub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 11's done when: on an eval set made by Phase 0's export_eval_set.py and redacted by redact_eval_set.py, a
    --dry-run replay builds every digest, for every reviewer, repetition and probe, calls no model and writes no
    decision."""
    pytest.importorskip("cairn.dispatcher.repair.redaction")
    import redact_eval_set

    cases = [
        EvalCase(f"i{n:03d}", cli, budget, label)
        for n, (cli, budget, label) in enumerate(
            [
                ("claudecode", 1800, "progressing"),
                ("claudecode", 1800, "stuck"),
                ("codex", 600, "progressing"),
                ("codex", 1800, "stuck"),
                ("pi", 900, "progressing"),
                ("pi", 1800, "unclear"),
            ],
            start=1,
        )
    ]
    eval_dir = write_eval_set(tmp_path, cases)
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    capsys.readouterr()
    chat, messages = model_stubs("openai_chat"), model_stubs("anthropic_messages")
    files = write_agent_files(
        tmp_path / "agent",
        [
            reviewer_entry("local", chat.base_url, context_window=16384),
            reviewer_entry("claude", messages.base_url, api_format="anthropic_messages", api_key=fake_secret("k2")),
        ],
    )
    out = tmp_path / "replay-027"
    code, stdout, err = run_replay(
        eval_dir,
        files,
        out,
        "--project-json",
        str(eval_dir / "project.json"),
        "--dry-run",
        "--repetitions",
        "3",
        "--inject",
        "--labels",
        str(eval_dir / "labels.csv"),
        "--yes",
        capsys=capsys,
    )
    assert code == 0, err
    stuck = [case.intent_id for case in cases if case.label == "stuck"]
    for reviewer in ("local", "claude"):
        names = sorted(path.name for path in (out / "digests" / reviewer).iterdir())
        expected = sorted(
            [f"{case.intent_id}.r{r}.txt" for case in cases for r in (1, 2, 3)]
            + [f"{intent}.r{r}.injected.txt" for intent in stuck for r in (1, 2, 3)]
        )
        assert names == expected
        for name in names:
            text = (out / "digests" / reviewer / name).read_text(encoding="utf-8")
            assert text.startswith("REVIEW CONTEXT (from Cairn; trusted)\n") and NONCE.search(text)
            assert digest_module.estimate_tokens(text) <= 8000
    assert [line_values(line)["calls"] for line in stdout.splitlines() if line.startswith("planned ")] == ["24", "24"]
    assert "dry run: every digest is built; no model was called" in stdout
    assert chat.requests() == [] and messages.requests() == []
    assert not (out / "decisions.jsonl").exists()
    for case in cases:
        report = json.loads((eval_dir / case.intent_id / "redaction.json").read_text())
        assert [entry["output"] for entry in report["files"]] == ["transcript.redacted.jsonl"]


def test_digest_tokens_compares_budgets_for_every_reviewer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """§9.6: the replay can compare --digest-tokens 8000,24000: each reviewer reviews every case once per budget, each
    record names its digest_budget (the gate scores each budget on its own), and the digests are kept per budget."""
    eval_dir = write_eval_set(tmp_path, [EvalCase("i001", "claudecode", 1800, "progressing")])
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    out = tmp_path / "out"
    code, stdout, err = run_replay(eval_dir, files, out, "--yes", "--digest-tokens", "8000,24000", capsys=capsys)
    assert code == 0, err
    assert sorted(path.name for path in (out / "digests" / "r1").iterdir()) == [
        "i001.r1.b24000.txt",
        "i001.r1.b8000.txt",
    ]
    assert [r["digest_budget"] for r in read_records(out / "decisions.jsonl")] == [8000, 24000]
    assert "planned reviewer=r1 calls=2" in stdout and len(stub.requests()) == 2


# --- spec 15.3 steps 2, 4 and 5 -----------------------------------------------------------------------------------------


def test_a_case_that_reaches_its_conclude_prompt_by_the_cut_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """§15.3 step 4: a case whose conclude prompt is at or before the cut never reached its review point and is
    refused, the other cases are replayed; one whose conclude prompt comes after the cut is a case."""
    budget = 1800
    cut = 0.75 * budget

    def concluding_at(intent: str, offset: float) -> EvalCase:
        session = Session(
            "claudecode",
            (Call(10, 15, "id", "uid=1000"), Call(30, 40, "ls /srv/lab", "lab.conf")),
            conclude_at=offset,
            session_id=f"01a1dafd-0000-7000-8000-0000000{intent[-3:]}c0",
        )
        return EvalCase(intent, "claudecode", budget, "stuck", session=session)

    cases = [concluding_at("i001", 1000), concluding_at("i002", cut), concluding_at("i003", cut + 1)]
    eval_dir = write_eval_set(tmp_path, cases)
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    code, stdout, err = run_replay(eval_dir, files, tmp_path / "out", "--dry-run", capsys=capsys)
    assert code == 0, err
    message = "reason=the conclude prompt is at or before the cut: the run never reached its review point"
    assert f"refused case intent=i001 {message}" in err and f"refused case intent=i002 {message}" in err
    assert "intent=i003" not in err
    assert sorted(path.name for path in (tmp_path / "out" / "digests" / "r1").iterdir()) == ["i003.r1.txt"]
    assert "(1 cases, 2 refused)" in stdout


def test_a_cut_past_the_range_of_times_is_a_refused_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """§15.3 step 4: a case whose first timestamp plus 0.75 x budget is past the range of `datetime` (only a record
    written into the file holds such a time) is refused, without an exception; the other case is replayed."""
    late = Session(
        "codex",
        (Call(10, 20, "id", "uid=1000"),),
        session_id="01a1dafd-0000-7000-8000-00000000ffff",
        start=datetime(9999, 12, 31, 23, 40, tzinfo=UTC),
    )
    cases = [EvalCase("i001", "codex", 1800, "stuck", session=late), EvalCase("i002", "pi", 900, "progressing")]
    eval_dir = write_eval_set(tmp_path, cases)
    mark_redacted(eval_dir)
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    code, _, err = run_replay(eval_dir, files, tmp_path / "out", "--yes", capsys=capsys)
    assert code == 0, err
    assert (
        "refused case intent=i001 reason=the cut, the first timestamp plus 0.75 x budget, is past the range of times"
        in err
    )
    assert [record["intent_id"] for record in read_records(tmp_path / "out" / "decisions.jsonl")] == ["i002"]
    assert len(stub.requests()) == 1


def two_attempt_set(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An eval set whose i001 has two timed-out attempts (two session files), the later-started one first by name,
    each with its own budget in intent.json, redacted by redact_eval_set.py; i002 has one attempt. Returns the set and
    the session file name of each attempt ("earlier", "later")."""
    pytest.importorskip("cairn.dispatcher.repair.redaction")
    import redact_eval_set

    earlier = progressing_session("claudecode", 1800, intent="i001", session_id="01a1dafd-0000-7000-8000-0000000e0001")
    later = stuck_session("claudecode", 1200, intent="i001", session_id="01a1dafd-0000-7000-8000-0000000e0002")
    later = Session(
        later.cli,
        later.calls,
        later.prompt,
        later.messages,
        later.conclude_at,
        later.session_id,
        T0 + timedelta(hours=2),
    )
    cases = [EvalCase("i001", "claudecode", 1800, "stuck", session=earlier), EvalCase("i002", "codex", 600, "stuck")]
    names = {"earlier": "zz-attempt-earlier.jsonl", "later": "aa-attempt-later.jsonl"}

    def session_name(case: EvalCase) -> str:
        return names["earlier"] if case.intent_id == "i001" else "session.jsonl"

    eval_dir = write_eval_set(tmp_path, cases, session_name=session_name)
    folder = eval_dir / "i001"
    (folder / names["later"]).write_bytes(later.data())
    meta = json.loads((folder / "intent.json").read_text())
    first = meta["timed_out_attempts"][0]
    meta["timed_out_attempts"] = [
        {**first, "attempt": 1, "session_id": earlier.session_id, "execute_timeout_s": 1800},
        {**first, "attempt": 2, "session_id": later.session_id, "execute_timeout_s": 1200},
    ]
    (folder / "intent.json").write_text(json.dumps(meta), encoding="utf-8")
    dispatch = tmp_path / "dispatch.yaml"
    dispatch.write_text(dispatch_yaml(execution="container"), encoding="utf-8")
    assert redact_eval_set.main([str(eval_dir), "--dispatch-config", str(dispatch)]) == 0
    return eval_dir, names


def compose_copy(eval_dir: Path, target: Path) -> Path:
    """The eval set as §15.3's Compose step copies it: rsync -a --include='*/' --include='intent.json'
    --include='labels.csv' --include='project.json' --include='transcript.redacted.jsonl' --include='redaction.json'
    --exclude='*' (rsync matches a pattern without a slash against the last part of the path, at any depth)."""
    carried = {"intent.json", "labels.csv", "project.json", "transcript.redacted.jsonl", "redaction.json"}
    for path in sorted(eval_dir.rglob("*")):
        relative = path.relative_to(eval_dir)
        if path.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
        elif path.name in carried:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / relative)
    return target


def test_with_several_attempts_the_replay_uses_the_latest_by_first_timestamp_or_the_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    model_stubs: Callable[..., ModelStub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§15.3 step 2: with several attempts, the latest by first timestamp is used, whatever their names' order, with
    that attempt's budget; --attempt first uses the earliest. The redaction writes each attempt's output as
    attempt-<k>/transcript.redacted.jsonl, so the Compose copy of §15.3, made with exactly its rsync include list,
    carries every attempt and replays the same way; a copy that lacks one refuses that case alone."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    eval_dir, names = two_attempt_set(tmp_path)
    report = json.loads((eval_dir / "i001" / "redaction.json").read_text())
    outputs = {entry["source"]: entry["output"] for entry in report["files"]}
    assert outputs == {
        names["later"]: "attempt-1/transcript.redacted.jsonl",
        names["earlier"]: "attempt-2/transcript.redacted.jsonl",
    }
    capsys.readouterr()
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    copy = compose_copy(eval_dir, tmp_path / "eval-proj_027-redacted")
    assert sorted(path.name for path in (copy / "i001").iterdir()) == [
        "attempt-1", "attempt-2", "intent.json", "redaction.json",
    ]  # fmt: skip
    for eval_set, label in ((eval_dir, "host"), (copy, "compose")):
        for attempt, transcript, budget in (
            ("last", outputs[names["later"]], 1200),
            ("first", outputs[names["earlier"]], 1800),
        ):
            out = tmp_path / f"out-{label}-{attempt}"
            code, _, err = run_replay(eval_set, files, out, "--yes", "--attempt", attempt, capsys=capsys)
            assert code == 0, err
            records = {record["intent_id"]: record for record in read_records(out / "decisions.jsonl")}
            data = (eval_set / "i001" / transcript).read_bytes()
            assert (records["i001"]["transcript"], records["i001"]["budget_seconds"]) == (transcript, budget), label
            assert records["i001"]["transcript_sha256"] == hashlib.sha256(data).hexdigest()
            assert records["i001"]["seconds_left"] == (budget - 20) - math.floor(0.75 * budget) - 5
            assert records["i002"]["transcript"] == "transcript.redacted.jsonl"
    # A copy that left out one attempt's output: that case alone is refused, and nothing suggests --no-env-redaction.
    (copy / "i001" / "attempt-2" / "transcript.redacted.jsonl").unlink()
    code, _, err = run_replay(copy, files, tmp_path / "out-partial", "--yes", capsys=capsys)
    assert code == 0, err
    assert (
        "refused case intent=i001 reason=redaction.json names redacted transcripts that the folder lacks: "
        "attempt-2/transcript.redacted.jsonl; a copy of the eval set must carry every transcript.redacted.jsonl"
    ) in err
    assert "--no-env-redaction" not in err
    assert [record["intent_id"] for record in read_records(tmp_path / "out-partial" / "decisions.jsonl")] == ["i002"]


def test_new_facts_are_those_of_intents_concluded_between_the_start_and_the_cut(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """§15.3 step 5: the digest's new facts are those of other intents concluded in (t0, cut]: one concluded before
    the run started and one concluded after the cut are left out, one concluded in between is shown."""
    eval_dir = write_eval_set(tmp_path, [EvalCase("i001", "claudecode", 1800, "progressing")])
    mark_redacted(eval_dir)
    document = json.loads((eval_dir / "project.json").read_text())
    document["facts"] += [
        {"id": "f003", "description": "Made-up fact found before the run started."},
        {"id": "f004", "description": "Made-up fact found after the cut."},
    ]
    concluded = (("i901", "f003", T0 - timedelta(seconds=60)), ("i902", "f004", T0 + timedelta(seconds=1351)))
    for intent, fact, at in concluded:
        document["intents"].append(
            {
                "id": intent, "from": ["origin"], "to": fact, "description": "Made-up", "creator": "reason",
                "created_at": "2026-11-14T09:59:00Z", "concluded_at": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
        )  # fmt: skip
    project = tmp_path / "project.json"
    project.write_text(json.dumps(document), encoding="utf-8")
    stub = model_stubs("openai_chat")
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    out = tmp_path / "out"
    code, _, err = run_replay(eval_dir, files, out, "--project-json", str(project), "--dry-run", capsys=capsys)
    assert code == 0, err
    text = (out / "digests" / "r1" / "i001.r1.txt").read_text(encoding="utf-8")
    new_facts = text.split("FACTS ADDED BY OTHER RUNS SINCE THIS RUN STARTED:", 1)[1].split("LAST ", 1)[0]
    assert "The lab banner names version 1.2." in new_facts  # i900, concluded 60 s after the start
    assert "found before the run started" not in text and "found after the cut" not in text


# --- the record's ms -----------------------------------------------------------------------------------------------------


def test_a_records_ms_is_the_whole_model_call_with_every_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_stubs: Callable[..., ModelStub]
) -> None:
    """A review whose first attempt failed slowly and whose retry answered at once is not a fast review: the record's
    ms is the whole model call (both attempts), while its token counts and finish_reason are the last attempt's; the
    gate's calls_over counts such a record as over a live budget that only the retry would fit."""
    eval_dir = write_eval_set(tmp_path, [EvalCase("i001", "claudecode", 1800, "progressing")])
    mark_redacted(eval_dir)
    seen: list[float] = []

    def slow_then_fine(request: Seen) -> tuple[int, Mapping[str, Any]]:
        seen.append(time.monotonic())
        if len(seen) == 1:
            time.sleep(1.2)
            return 503, {"error": "made-up overload"}
        return 200, openai_answer(decision_text("continue"), usage={"prompt_tokens": 9000, "completion_tokens": 40})

    stub = model_stubs("openai_chat", answer=slow_then_fine)
    files = write_agent_files(tmp_path / "agent", [reviewer_entry("r1", stub.base_url)])
    out = tmp_path / "out"
    argv = [str(eval_dir), "--config", str(files.config), "--out", str(out), "--yes"]
    code = replay.main(argv, monotonic=time.monotonic, sleep=lambda _seconds: None)
    err = capsys.readouterr().err
    assert code == 0, err
    lines = [line_values(line) for line in model_call_lines(err)]
    assert [line["outcome"] for line in lines] == ["status_503", "ok"]
    first_ms, second_ms = int(lines[0]["ms"]), int(lines[1]["ms"])
    assert first_ms >= 1200 and second_ms < 1000
    (record,) = read_records(out / "decisions.jsonl")
    assert record["fallback"] is None and record["ms"] >= first_ms + second_ms - 1 and record["ms"] >= 1200
    assert (record["input_tokens"], record["output_tokens"], record["finish_reason"]) == (9000, 40, "stop")
    # A live budget whose review has 1 s at its opening (B = 104 s, poll 5 s): the call is over it.
    labels = tmp_path / "labels.csv"
    labels.write_text("intent_id,label,evidence,notes\ni001,progressing,,\n", encoding="utf-8")
    gate.main(["--labels", str(labels), "--decisions", str(out / "decisions.jsonl"), "--live-budgets", "104"])
    assert "live reviewer=r1 budget=104s seconds_at_opening=1 calls_over=1/1" in capsys.readouterr().out
