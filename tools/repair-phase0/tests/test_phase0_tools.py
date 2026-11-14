"""Tests for the Phase 0 tools, fed with synthetic log lines and a made-up project."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import baseline_from_logs as baseline
import export_eval_set
import kill_check

TOOL_DIR = Path(__file__).resolve().parents[1]

# Made-up runs on a made-up project. Line 3 has a "docker compose logs" prefix, line 12 ANSI colours.
LOG = """\
[2026-01-05 10:00:00] INFO scheduler.loop dispatched explore project=proj_900 intent=i001 worker=claude-a
[2026-01-05 10:00:01] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=claude-a phase=explore_execute timeout=300s
cairn-dispatcher  | [2026-01-05 10:00:02] INFO scheduler.loop dispatched explore project=proj_900 intent=i002 worker=codex-b
[2026-01-05 10:00:02] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=codex-b phase=explore_execute timeout=300s
[2026-01-05 10:02:10] INFO tasks.common intent concluded project=proj_900 intent=i002 worker=codex-b source=explore_execute phase_ms=128000 total_ms=128400
[2026-01-05 10:05:01] WARNING tasks.explore explore timed out project=proj_900 intent=i001 worker=claude-a execute_ms=300100 total_ms=300200 stdout_preview= stderr_preview=
[2026-01-05 10:05:02] INFO tasks.explore starting conclude fallback project=proj_900 intent=i001 worker=claude-a
[2026-01-05 10:05:02] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=claude-a phase=explore_conclude timeout=90s
[2026-01-05 10:05:40] WARNING contracts json output rescued strategy=envelope_repair phase=explore_conclude project=proj_900 intent=i001 worker=claude-a strict_error=no JSON object found in output description_chars=812
[2026-01-05 10:05:40] INFO tasks.common intent concluded project=proj_900 intent=i001 worker=claude-a source=explore_conclude phase_ms=38000
[2026-01-05 10:06:00] INFO scheduler.loop dispatched explore project=proj_900 intent=i003 worker=codex-b
\x1b[33m[2026-01-05 10:06:01] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=codex-b phase=explore_execute timeout=300s\x1b[0m
[2026-01-05 10:11:01] WARNING tasks.explore explore timed out project=proj_900 intent=i003 worker=codex-b execute_ms=300050 total_ms=300100 stdout_preview= stderr_preview=OpenAI Codex v0.0.0 -------- workdir: /home/kali/workspace session id: 0198a7c2-1111-7000-8000-000000000001 -------- user ## Graph
[2026-01-05 10:11:02] INFO tasks.explore starting conclude fallback project=proj_900 intent=i003 worker=codex-b
[2026-01-05 10:11:02] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=codex-b phase=explore_conclude timeout=90s
[2026-01-05 10:11:30] WARNING tasks.explore conclude parse failed project=proj_900 intent=i003 worker=codex-b error=no JSON object found in output conclude_ms=28000 stdout_preview=SYNTHETIC-PREVIEW-TEXT the lab service answers stderr_preview=
[2026-01-05 10:11:30] INFO tasks.common released intent project=proj_900 intent=i003 worker=codex-b
[2026-01-05 10:11:31] WARNING scheduler.loop task finished project=proj_900 task=explore worker=codex-b outcome=failed
Traceback (most recent call last):
[2026-01-05 10:12:00] INFO scheduler.loop dispatched bootstrap project=proj_901 intent=i001 worker=pi-c
[2026-01-05 10:12:01] INFO tasks.common starting container exec container=cairn-dispatch-proj_901 worker=pi-c phase=bootstrap timeout=300s
[2026-01-05 10:17:01] WARNING tasks.bootstrap bootstrap timed out project=proj_901 intent=i001 worker=pi-c execute_ms=300000 total_ms=300050 stdout_preview={"type":"session","version":3,"id":"pi-session-42","timestamp":"2026-01-05T10:12:01Z"} {"type":"agent_start"} stderr_preview=
[2026-01-05 10:17:02] INFO tasks.bootstrap starting bootstrap conclude fallback project=proj_901 intent=i001 worker=pi-c
[2026-01-05 10:17:02] INFO tasks.common starting container exec container=cairn-dispatch-proj_901 worker=pi-c phase=bootstrap_conclude timeout=90s
[2026-01-05 10:17:30] INFO tasks.common intent concluded project=proj_901 intent=i001 worker=pi-c source=bootstrap_conclude phase_ms=28000
[2026-01-05 10:20:00] INFO scheduler.loop dispatched reason project=proj_900 worker=claude-a trigger=initial
[2026-01-05 10:20:01] INFO tasks.common starting container exec container=cairn-dispatch-proj_900 worker=claude-a phase=reason_execute timeout=300s
"""

PROJECT = {
    "project": {
        "id": "proj_900",
        "title": "Lab",
        "status": "active",
        "bootstrap_enabled": True,
        "created_at": "2026-01-05T09:59:00Z",
    },
    "facts": [
        {"id": "origin", "description": "A lab VM runs a small web service."},
        {"id": "goal", "description": "Find the admin password."},
        {"id": "f001", "description": "The service answers on ports 8080 and 8443."},
    ],
    "intents": [
        {
            "id": "i001",
            "from": ["origin"],
            "to": "f001",
            "description": "Map which ports the lab service listens on",
            "creator": "reason",
            "created_at": "2026-01-05T09:59:30Z",
            "concluded_at": "2026-01-05T10:05:40Z",
        },
        {
            "id": "i002",
            "from": ["origin"],
            "to": None,
            "description": "Read the service banner",
            "creator": "reason",
            "created_at": "2026-01-05T09:59:31Z",
            "concluded_at": None,
        },
        {
            "id": "i003",
            "from": ["f001"],
            "to": None,
            "description": "Check whether the admin page on 8443 accepts the default lab credentials",
            "creator": "reason",
            "created_at": "2026-01-05T10:05:50Z",
            "concluded_at": None,
        },
    ],
    "hints": [],
}


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "dispatcher.log"
    path.write_text(LOG, encoding="utf-8")
    return path


def _attempt(result: baseline.ParseResult, project: str, intent: str) -> baseline.Attempt:
    (found,) = [a for a in result.attempts if a.project == project and a.intent == intent]
    return found


# --- baseline_from_logs ---------------------------------------------------------------------


def test_patterns_match_the_dispatcher_source() -> None:
    if not (baseline.DEFAULT_CAIRN_SRC / "cairn" / "dispatcher").is_dir():
        pytest.skip("no cairn checkout next to the tools")
    assert baseline.check_formats(baseline.DEFAULT_CAIRN_SRC) == []


def test_check_formats_reports_a_changed_message(tmp_path: Path) -> None:
    dispatcher = tmp_path / "cairn" / "dispatcher"
    dispatcher.mkdir(parents=True)
    (dispatcher / "logging.py").write_text(f"fmt={baseline.HEADER_FORMAT!r}\ndatefmt={baseline.HEADER_DATEFMT!r}\n")
    (dispatcher / "tasks.py").write_text('LOG.info("released intent project=%s intent=%s worker=%s", a, b, c)\n')
    problems = baseline.check_formats(tmp_path)
    assert len(problems) == len(baseline.EVENT_SPECS) - 1
    assert not any(p.startswith("released_intent:") for p in problems)


@pytest.mark.parametrize("spec", baseline.EVENT_SPECS, ids=lambda s: f"{s.name}:{s.fmt.count('%')}")
def test_each_pattern_reads_its_own_message(spec: baseline.EventSpec) -> None:
    # Values may hold spaces, except where two placeholders are separated by a bare space
    # ("strategy=%s %s": the strategy is one word, the context follows).
    gap = "_" if "%s %s" in spec.fmt else " "
    values = iter(f"value{i}{gap}with{gap}spaces" for i in range(20))
    message = re.sub(r"%[sr]", lambda _: next(values), spec.fmt)
    matched = baseline.match_event(message)
    assert matched is not None
    name, fields = matched
    assert name == spec.name
    assert all(v.startswith("value") for v in fields.values())
    assert len(fields) == spec.fmt.count("%s") + spec.fmt.count("%r")


def test_timeline_follows_each_attempt(log_file: Path) -> None:
    result = baseline.parse_logs([str(log_file)])

    i001 = _attempt(result, "proj_900", "i001")
    assert (i001.task, i001.worker, i001.dispatched_at) == ("explore", "claude-a", "2026-01-05 10:00:00")
    assert (i001.execute_timeout_s, i001.execute_outcome, i001.execute_ms) == (300, "timed_out", 300100)
    assert (i001.conclude_timeout_s, i001.conclude_outcome, i001.conclude_ms) == (90, "concluded", 38000)
    assert i001.rescue_strategy == "envelope_repair"
    assert (i001.final, i001.duration_s) == ("concluded_by_fallback", 340.0)

    i002 = _attempt(result, "proj_900", "i002")
    assert (i002.execute_outcome, i002.execute_ms, i002.final) == ("concluded", 128000, "concluded_at_execute")
    assert i002.concluded_total_ms == 128400

    i003 = _attempt(result, "proj_900", "i003")
    assert (i003.execute_timeout_s, i003.execute_outcome) == (300, "timed_out")
    assert (i003.conclude_outcome, i003.conclude_error) == ("parse_failed", "no JSON object found in output")
    assert (i003.final, i003.final_reason, i003.released) == ("failed", "conclude_parse_failed", True)

    boot = _attempt(result, "proj_901", "i001")
    assert (boot.task, boot.execute_timeout_s, boot.execute_outcome) == ("bootstrap", 300, "timed_out")
    assert (boot.conclude_timeout_s, boot.conclude_outcome, boot.final) == (90, "concluded", "concluded_by_fallback")


def test_summary_counts(log_file: Path) -> None:
    result = baseline.parse_logs([str(log_file)])
    summary = baseline.summarize(result)
    explore = summary["explore"]
    assert (explore["attempts"], explore["intents"], explore["hit_timeout"]) == (3, 3, 2)
    assert explore["hit_timeout_pct"] == 66.7
    assert explore["configured_timeout_s"] == {"execute": {300: 3}, "conclude": {90: 2}}
    assert explore["conclude_fallback"]["outcomes"] == {"concluded": 1, "parse_failed": 1}
    assert explore["after_timeout_final"] == {"concluded_by_fallback": 1, "failed": 1}
    assert explore["duration_s"]["execute_timed_out"]["n"] == 2
    assert summary["bootstrap"]["hit_timeout"] == 1
    assert summary["json_rescues"]["by_strategy"] == {"envelope_repair": 1}
    assert summary["tasks"]["finished_not_success"] == {"explore:failed": 1}
    assert summary["reason"]["runs_started"] == 1
    assert summary["inputs"]["unparsed_lines"] == 1  # the traceback line
    assert summary["exec_start_linking"]["unlinked"] == 0


def test_project_filter(log_file: Path) -> None:
    result = baseline.parse_logs([str(log_file)], ["proj_901"])
    assert [(a.project, a.intent) for a in result.attempts] == [("proj_901", "i001")]
    assert result.rescues == []
    assert dict(result.exec_starts) == {"bootstrap": 1, "bootstrap_conclude": 1}


def test_late_lines_from_an_expired_run_stay_with_that_run(tmp_path: Path) -> None:
    path = tmp_path / "late.log"
    path.write_text(
        "[2026-01-05 11:00:00] INFO scheduler.loop dispatched explore project=p intent=i001 worker=w-a\n"
        "[2026-01-05 11:05:00] INFO scheduler.loop dispatched explore project=p intent=i001 worker=w-b\n"
        "[2026-01-05 11:06:00] WARNING tasks.explore heartbeat lost during explore project=p intent=i001 worker=w-a status=409 execute_ms=360000\n"
    )
    result = baseline.parse_logs([str(path)])
    first, second = result.attempts
    assert (first.worker, first.execute_outcome, first.final) == ("w-a", "heartbeat_lost", "failed")
    assert (second.worker, second.execute_outcome, second.final) == ("w-b", None, "open")


def test_outputs_leave_out_previews(log_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert baseline.main([str(log_file), "--out-dir", str(out)]) == 0
    timeline = (out / "baseline_timeline.csv").read_text()
    summary = (out / "baseline_summary.json").read_text()
    assert "SYNTHETIC-PREVIEW-TEXT" not in timeline + summary
    assert "preview" not in next(csv.reader(timeline.splitlines()))[0:]
    assert json.loads(summary)["explore"]["attempts"] == 3


def test_percentiles() -> None:
    assert baseline.percentiles([]) == {"n": 0, "p50": None, "p75": None, "p90": None, "p95": None, "max": None}
    stats = baseline.percentiles([1.0, 2.0, 3.0, 4.0])
    assert (stats["n"], stats["p50"], stats["max"]) == (4, 2.5, 4.0)


# --- export_eval_set ------------------------------------------------------------------------


def test_export_builds_the_evaluation_set(log_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "eval"
    manifest = export_eval_set.export(
        "proj_900",
        [str(log_file)],
        PROJECT,
        out,
        execution="container",
        worker_types={"claude-a": "claudecode", "codex-b": "codex"},
    )
    assert [item["intent_id"] for item in manifest["intents"]] == ["i001", "i003"]
    assert (manifest["timed_out_attempts"], manifest["attempts_in_logs"]) == (2, 3)

    with (out / "labels.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["intent_id"] for r in rows] == ["i001", "i003"]
    assert list(rows[0]) == ["intent_id", "label", "evidence", "notes"]
    assert all(r["label"] == "" for r in rows)

    i001 = json.loads((out / "i001" / "intent.json").read_text())
    assert i001["produced_fact"] == {"id": "f001", "description": "The service answers on ports 8080 and 8443."}
    (attempt,) = i001["timed_out_attempts"]
    assert attempt["review_window"] == {
        "from_s": 225,
        "to_s": 240,
        "dispatcher_clock": ["2026-01-05 10:03:46", "2026-01-05 10:04:01"],
    }
    assert (attempt["worker_type"], attempt["final"], attempt["rescue_strategy"]) == (
        "claudecode",
        "concluded_by_fallback",
        "envelope_repair",
    )
    assert attempt["session_id"] is None

    i003 = json.loads((out / "i003" / "intent.json").read_text())
    assert i003["timed_out_attempts"][0]["session_id"] == "0198a7c2-1111-7000-8000-000000000001"
    assert "SYNTHETIC-PREVIEW-TEXT" in (out / "i003" / "conclude_output_preview.txt").read_text()
    assert not (out / "i001" / "conclude_output_preview.txt").exists()
    assert (out / "LABELLING_GUIDE.md").exists()

    guide = (out / "i003" / "transcript.md").read_text()
    assert "docker exec cairn-dispatch-proj_900 sh -c " in guide
    assert '"$HOME/.codex/sessions"' in guide
    assert "'```\\ni003\\n```'" in guide
    assert "0198a7c2-1111-7000-8000-000000000001" in guide
    assert "~/.claude" not in guide  # container mode only, codex only


def test_session_ids_from_previews() -> None:
    attempt = baseline.Attempt("p", "i001", "explore", 1, "w")
    attempt.execute_stdout_preview = (
        '{"type":"session","version":3,"id":"pi-session-42","timestamp":"x"} {"type":"agent_start"}'
    )
    assert export_eval_set.session_id_from_previews(attempt)[0] == "pi-session-42"
    attempt.execute_stderr_preview = "header session id: 0198a7c2-aaaa-7000-8000-000000000002 more"
    assert export_eval_set.session_id_from_previews(attempt)[0] == "0198a7c2-aaaa-7000-8000-000000000002"


def test_description_snippet() -> None:
    assert export_eval_set.description_snippet("Check whether 'admin' on 8443 accepts the default lab credentials") == (
        "on 8443 accepts the default lab credentials"
    )
    assert export_eval_set.description_snippet("short") is None


@pytest.mark.skipif(not all(shutil.which(c) for c in ("bash", "grep", "xargs")), reason="needs bash, grep, xargs")
def test_local_find_command_lists_only_the_matching_session(tmp_path: Path) -> None:
    sessions = tmp_path / "home" / ".claude" / "projects" / "-work"
    sessions.mkdir(parents=True)
    prompt = "## Current Intent\n```\n{id}\n```\n\n## Current Intent Description\n```\n{d}\n```\n"
    description = "Check whether the admin page on 8443 accepts the default lab credentials"
    (sessions / "match.jsonl").write_text(json.dumps({"content": prompt.format(id="i003", d=description)}) + "\n")
    (sessions / "other.jsonl").write_text(json.dumps({"content": prompt.format(id="i004", d="Read the banner")}) + "\n")
    command = export_eval_set.find_command(
        "local",
        "claudecode",
        "w",
        "unused",
        export_eval_set.intent_marker("i003"),
        export_eval_set.description_snippet(description),
    )
    found = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert found.stdout.strip().splitlines() == [str(sessions / "match.jsonl")]


# --- kill_check -----------------------------------------------------------------------------


def test_listing_roles() -> None:
    listing = "\n".join(
        [
            "1\tsleep infinity ",
            f"7\ttimeout -k 2s 4s sh -c sleep 3001 & ... {kill_check.AGENT_TAG} ",
            f"8\tsh -c sleep 3001 & ... {kill_check.AGENT_TAG} ",
            "9\tsleep 3001 ",
            "10\tsleep 3002 ",
            "11\tsleep 3003 ",
            f"12\tsh -c for d in /proc ... {kill_check.LISTER_TAG} ",
            "not a line",
        ]
    )
    processes = kill_check.parse_listing(listing)
    assert kill_check.roles(processes) == ["wrapper", "agent", "plain", "noterm", "setsid"]
    assert [p["pid"] for p in processes if p["role"] is None] == [1, 12]


def test_top_and_kill_attempts() -> None:
    top = {
        "Titles": ["UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"],
        "Processes": [["root", "5096", "5000", "0", "x", "?", "0", "timeout -k 2s 4s sh"]],
    }
    assert kill_check.parse_top(top) == {5096: "timeout -k 2s 4s sh"}
    assert kill_check.parse_top({"Titles": ["USER"], "Processes": []}) == {}
    attempts = [
        {"command": ["kill", "-KILL", "5096"], "exit_code": 127},
        {"command": ["/bin/sh", "-lc", "kill -KILL 5096"], "exit_code": None},
    ]
    assert kill_check.kill_pid_accepted(attempts) == attempts[1]
    assert kill_check.kill_pid_accepted([{"command": ["kill"], "exit_code": 127}]) is None


def test_conclusions_describe_a_missed_kill() -> None:
    results = {
        "managed_kill": {
            "exec_running_after_kill": True,
            "kill_pid_accepted_attempt": {"command": ["/bin/sh", "-lc", "kill -KILL 5096"], "exit_code": None},
            "exec_inspect_pid": 5096,
            "exec_pid_host_command": "timeout -k 2s 120s sh -c ...",
            "exec_pid_visible_inside_container": False,
            "kill_replay_with_output": {"exit_code": 1, "output": "kill: No such process"},
            "survivors": ["wrapper"],
        },
        "cancel": {"communicate_returned_after_cancel_s": 11.7, "wrapper_timeout_s": 10, "survivors": ["setsid"]},
    }
    text = " ".join(kill_check.conclusions(results))
    assert "did not stop the run" in text
    assert "host PID" in text
    assert "no process has that PID inside the container" in text
    assert "cancel did not stop the run" in text


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_agent_scripts_are_valid_sh() -> None:
    for ignore_term in (False, True):
        checked = subprocess.run(
            ["sh", "-n", "-c", kill_check.agent_script(ignore_term=ignore_term)], capture_output=True, check=False
        )
        assert checked.returncode == 0, checked.stderr
    assert subprocess.run(["sh", "-n", "-c", kill_check.LIST_SCRIPT], capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("flag", ["--help", "--dry-run"])
def test_kill_check_runs_without_docker(flag: str) -> None:
    # -I: no site-packages and no PYTHONPATH, so nothing beyond the standard library is importable.
    done = subprocess.run(
        [sys.executable, "-I", str(TOOL_DIR / "kill_check.py"), flag], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "kill" in done.stdout
