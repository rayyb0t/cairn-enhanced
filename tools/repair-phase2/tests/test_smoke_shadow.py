"""smoke_shadow.py, the operator's end-to-end smoke of shadow mode (spec §16, XC5).

The full smoke takes about seven minutes (a 360 s explore budget): it runs only with CAIRN_REPAIR_SMOKE=1 in the
environment. The other tests check its parts against the real code they meet, quickly: the fake `claude` writes a
session file that the dispatcher's prompt-marker check accepts and that the agent's parser reads, and answers reason
and conclude runs in shapes the dispatcher's contracts accept; the stub reviewer answers in a shape the agent's model
client and validation accept; and the files it writes load as the dispatcher and the agent load them.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("cairn_repair_agent")
pytest.importorskip("cairn.dispatcher.repair.transcripts")

import requests
from cairn_repair_agent import config as agent_config
from cairn_repair_agent.decision import validate_answer
from cairn_repair_agent.model import openai_answer
from cairn_repair_agent.transcripts import parse_transcript

import smoke_shadow
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.contracts import (
    EXPLORE_TEXT_ENVELOPE,
    parse_json_output,
    parse_validated,
    validate_explore_payload,
    validate_reason_payload,
)
from cairn.dispatcher.repair.transcripts import claude_project_slug, holds_marker

MARKER = "/tmp/cairn-prompts/explore_execute-0123456789ab/graph.yaml"


@pytest.fixture
def layout(tmp_path: Path) -> smoke_shadow.Layout:
    found = smoke_shadow.Layout(base=tmp_path / "smoke", port=8123, reviewer_url="http://127.0.0.1:9/v1", budget=360)
    found.base.mkdir()
    found.write()
    return found


def run_fake(
    layout: smoke_shadow.Layout, cwd: Path, *argv: str, timeout: float = 20
) -> subprocess.CompletedProcess[str]:
    environment = smoke_shadow.clean_environment(
        HOME=str(layout.path("home")),
        CLAUDE_CONFIG_DIR=str(layout.path("claude-config")),
        SMOKE_STATE_DIR=str(layout.path("state")),
    )
    return subprocess.run(
        [str(layout.path("bin", "claude")), *argv],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_the_fake_cli_writes_a_session_file_the_dispatcher_and_the_agent_read(layout: smoke_shadow.Layout) -> None:
    """An explore run's session file is where Claude Code 2.1.98 writes it, holds the run's prompt marker in a user
    message before the first tool call (the dispatcher's check, spec §8.8), and reads, for the agent, as one finished
    call, a message and a call running at its end; the process sleeps until it is stopped."""
    workdir = layout.path("work", "proj_001")
    workdir.mkdir()
    session = "8f1c2d3e-0000-4000-8000-00000000abcd"
    prompt = f"# Task\n## Graph\nThe graph YAML snapshot is stored in this file:\n\n{MARKER}\n\n## Current Intent\n```\ni001\n```"
    environment = smoke_shadow.clean_environment(
        HOME=str(layout.path("home")), CLAUDE_CONFIG_DIR=str(layout.path("claude-config"))
    )
    process = subprocess.Popen(
        [
            str(layout.path("bin", "claude")),
            "--session-id",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ],
        cwd=workdir,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    path = layout.path("claude-config", "projects", claude_project_slug(os.path.realpath(workdir)), f"{session}.jsonl")
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (path.is_file() and path.read_bytes().count(b"\n") >= 5):
            time.sleep(0.05)
        assert process.poll() is None, "the fake CLI must keep running until it is stopped"
        data = path.read_bytes()
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert holds_marker(data[: 1 << 20], MARKER, "claudecode")
    assert not holds_marker(data, "/tmp/cairn-prompts/explore_execute-ffffffffffff/graph.yaml", "claudecode")
    transcript = parse_transcript("claudecode", data)
    assert [(call.command, call.status) for call in transcript.calls] == [
        ("ls -la lab", "ok"),
        ("tar -xf lab-archive.tar", None),
    ]
    assert transcript.started_at is not None and transcript.messages and transcript.conclude_at is None
    last = transcript.calls[-1].started_at
    assert [call.command for call in transcript.running_at(last)] == ["tar -xf lab-archive.tar"]


def test_the_fake_cli_answers_reason_and_conclude_runs_as_the_dispatcher_expects(layout: smoke_shadow.Layout) -> None:
    """A reason run proposes one intent, then completes the project; the conclude fallback answers with a fact; and the
    start-up check `claude --help` succeeds."""
    workdir = layout.path("work")
    reason_prompt = "# Task\n### Graph\n...\n### Open Intents\n(none)"
    first = run_fake(layout, workdir, "--session-id", "s-1", "-p", "--", reason_prompt)
    kind, intents = validate_reason_payload(parse_json_output(first.stdout), open_intents_empty=True, max_intents=3)
    assert (first.returncode, kind) == (0, "intents")
    assert isinstance(intents, list) and intents[0]["description"] == smoke_shadow.INTENT
    second = run_fake(layout, workdir, "--session-id", "s-2", "-p", "--", reason_prompt)
    kind, _ = validate_reason_payload(parse_json_output(second.stdout), open_intents_empty=True, max_intents=3)
    assert kind == "complete"
    conclude = run_fake(layout, workdir, "-r", "s-1", "-p", "--", "This is the conclude phase.")
    kind, description = parse_validated(conclude.stdout, validate_explore_payload, envelope=EXPLORE_TEXT_ENVELOPE)
    assert (conclude.returncode, kind) == (0, "fact") and description
    assert run_fake(layout, workdir, "--help").returncode == 0


def test_the_stub_reviewer_answers_as_the_agent_expects() -> None:
    """The stub reviewer's answer is a valid openai_chat answer: extend citing E1 when the digest lists E1, else
    continue, both of which pass the agent's validation (spec §9.8)."""
    reviewer = smoke_shadow.Reviewer()
    url = reviewer.start()
    try:
        with requests.Session() as session:
            session.trust_env = False
            for digest, decision, ids in (
                ("REVIEW CONTEXT\n<untrusted_data>\nE1 t+00:01 Bash (0 s, exit 0): ls\n", "extend", {"E1"}),
                ("REVIEW CONTEXT\n<untrusted_data>\nnothing\n", "continue", set()),
            ):
                body = {
                    "model": "m",
                    "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": digest}],
                }
                answer = session.post(url + "/chat/completions", json=body, timeout=10).json()
                exchange = openai_answer(answer, prompt_est=1000)
                assert exchange.outcome == "ok" and exchange.text is not None
                assert validate_answer(exchange.text, ids, set()).decision == decision
    finally:
        reviewer.stop()
    assert reviewer.requests == 2


def test_the_smoke_files_load_as_the_dispatcher_and_the_agent_load_them(layout: smoke_shadow.Layout) -> None:
    """dispatch.yaml (local mode, a repair: section with one reviewer) loads with DispatchConfig; repair.yaml with the
    agent's loader; the tokens are 64 hex characters, distinct, in 0600 files; no proxy reaches the processes."""
    config = DispatchConfig.load(layout.path("dispatch.yaml"))
    assert config.runtime.execution == "local" and config.tasks.explore.timeout == 360
    assert config.repair is not None and [r.name for r in config.repair.reviewers] == ["smoke-reviewer"]
    assert config.repair.transcript_root == str(layout.path("copies"))
    agent = agent_config.load_config(str(layout.path("repair.yaml")))
    assert agent.transcripts.root == str(layout.path("copies")) and agent.poll_interval == 2
    tokens = [layout.path("secrets", name).read_text().strip() for name in ("agent.token", "dispatcher.token")]
    assert all(len(token) == 64 for token in tokens) and tokens[0] != tokens[1]
    assert all(
        layout.path("secrets", name).stat().st_mode & 0o777 == 0o600 for name in ("agent.token", "dispatcher.token")
    )
    environment = smoke_shadow.clean_environment()
    # The variables that Python's HTTP clients, curl and the CLIs read; other tools' own names stay as they are.
    assert not [name for name in environment if name.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")]
    assert environment["NO_PROXY"] == "*"


def test_a_budget_under_the_reviewed_minimum_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        smoke_shadow.main(["--budget", "340"])
    assert stopped.value.code == 2 and "shorter runs are never reviewed" in capsys.readouterr().err


# --- nothing the smoke starts outlives it ------------------------------------------------------------------------------

# The smoke itself, with each process it starts (server, dispatcher, agent) replaced by a sleeper started the same way,
# through smoke_shadow.start; each sleeper's process id goes to a file. The sleeper never serves, and the smoke waits
# for it far longer than any test here, so it ends only when a signal stops it. Arguments: the tools folder, the id
# file, and optionally a file for the smoke's own process id.
DRIVER = """
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import smoke_shadow

ids = Path(sys.argv[2])
if len(sys.argv) > 3:
    Path(sys.argv[3]).write_text(f"{os.getpid()}\\n")
original = smoke_shadow.start
smoke_shadow.STARTUP_SECONDS = 600


def start(name, argv, layout, environment):
    process = original(name, [sys.executable, "-c", "import time; time.sleep(300)"], layout, environment)
    with ids.open("a") as handle:
        handle.write(f"{process.popen.pid}\\n")
    return process


smoke_shadow.start = start
sys.exit(smoke_shadow.main(["--timeout", "120"]))
"""


def running(pid: int) -> bool:
    """A process that has not exited: one whose /proc entry is gone, or a zombie no one has reaped yet, has."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def wait_until(condition: object, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():  # type: ignore[operator]
            return True
        time.sleep(0.05)
    return False


def start_driver(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int, Path]:
    """The smoke with a sleeper for its server, in a session of its own, as a terminal or a test runner starts it; and
    the sleeper's process id once it runs."""
    script, ids, temporary = tmp_path / "driver.py", tmp_path / "ids", tmp_path / "tmp"
    script.write_text(DRIVER, encoding="utf-8")
    temporary.mkdir()
    environment = {**os.environ, "TMPDIR": str(temporary)}
    output = (tmp_path / "driver.out").open("wb")
    with output:
        driver = subprocess.Popen(
            [sys.executable, str(script), str(Path(smoke_shadow.__file__).parent), str(ids)],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    assert wait_until(lambda: ids.is_file() and ids.read_text().strip(), 30), "the smoke started no process"
    child = int(ids.read_text().split()[0])
    assert wait_until(lambda: running(child), 10)
    time.sleep(0.5)  # the sleeper has exec'd past the launcher
    return driver, child, temporary


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_sigterm_to_the_smoke_alone_stops_what_it_started_and_removes_its_folder(tmp_path: Path) -> None:
    """SIGTERM sent to the smoke's process alone (not its group) runs its cleanup: every process it started is stopped
    and its temporary folder removed, exit 143."""
    driver, child, temporary = start_driver(tmp_path)
    try:
        assert list(temporary.glob("cairn-repair-smoke-*")), "the smoke made no folder"
        driver.send_signal(signal.SIGTERM)
        assert driver.wait(timeout=60) == 128 + signal.SIGTERM
        assert wait_until(lambda: not running(child), 10), "a process the smoke started outlived it"
        assert not list(temporary.glob("cairn-repair-smoke-*"))
        assert "stopped by SIGTERM" in (tmp_path / "driver.out").read_text()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(driver.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        driver.wait(timeout=10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl(PR_SET_PDEATHSIG) and /proc are Linux's")
def test_a_sigkill_of_the_smoke_alone_still_stops_what_it_started(tmp_path: Path) -> None:
    """SIGKILL, which no handler sees, sent to the smoke's process alone: each process it started gets SIGTERM from
    the kernel when the smoke dies (PR_SET_PDEATHSIG), so none is left running."""
    driver, child, _ = start_driver(tmp_path)
    try:
        driver.send_signal(signal.SIGKILL)
        assert driver.wait(timeout=10) == -signal.SIGKILL
        assert wait_until(lambda: not running(child), 10), "a process the smoke started outlived it"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


def run_bound_to_pytest(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """argv in a process group of its own, started through smoke_shadow.DIE_WITH_PARENT as the smoke starts its own
    processes, so nothing it starts outlives this process, however this process ends:
    - On Linux the kernel sends it SIGTERM when this process dies (PR_SET_PDEATHSIG): timeout(1) or a CI job's timeout
      stopping pytest, a SIGKILL of pytest alone, or pytest's end after Ctrl-C. The smoke's SIGTERM handler stops what
      it started and removes its folder.
    - On its timeout, or Ctrl-C here (KeyboardInterrupt), this sends SIGTERM to its whole group at once, which runs that
      cleanup and reaches every process in the group, then SIGKILL to the group if it has not ended 120 s later."""
    launcher = [sys.executable, "-c", smoke_shadow.DIE_WITH_PARENT, str(os.getpid()), *argv]
    with subprocess.Popen(
        launcher, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    ) as process:
        try:
            out, err = process.communicate(timeout=timeout)
        except BaseException as stopped:  # the timeout, or Ctrl-C (KeyboardInterrupt) in this process
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                out, err = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                out, err = process.communicate()
            if isinstance(stopped, subprocess.TimeoutExpired):
                raise AssertionError(f"the smoke did not finish within {timeout} s:\n{out}{err}") from None
            raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


# A stand-in for pytest running the end-to-end test: it runs a command as test_smoke_shadow_end_to_end runs the smoke,
# through run_bound_to_pytest. Arguments: this tests folder, the tools folder, then the command.
PYTEST_STAND_IN = """
import sys

sys.path[:0] = [sys.argv[1], sys.argv[2]]
import test_smoke_shadow

test_smoke_shadow.run_bound_to_pytest(sys.argv[3:], timeout=600)
"""


def driver_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The driver's script, its sleepers' id file, the file of the smoke's own process id, and the smoke's TMPDIR."""
    script, temporary = tmp_path / "driver.py", tmp_path / "tmp"
    script.write_text(DRIVER, encoding="utf-8")
    temporary.mkdir()
    return script, tmp_path / "ids", tmp_path / "smoke.pid", temporary


def kill_leftovers(*files: Path) -> None:
    """SIGKILL to each process whose id the files hold while it still runs the driver or a sleeper, so that a failed
    test leaves nothing running, and a process id that has since been reused is left alone."""
    pids: list[str] = []
    for path in files:
        with contextlib.suppress(OSError):
            pids += path.read_text().split()
    for pid in pids:
        with contextlib.suppress(OSError, ValueError):
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"driver.py" in command or b"time.sleep(300)" in command:
                os.kill(int(pid), signal.SIGKILL)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl(PR_SET_PDEATHSIG) and /proc are Linux's")
@pytest.mark.parametrize(
    "stop",
    [
        "SIGTERM to the group of pytest",  # timeout(1) around pytest, a CI job's timeout
        "SIGKILL to pytest alone",
        "SIGINT to the group of pytest",  # Ctrl-C at a terminal
    ],
)
def test_the_smoke_of_the_end_to_end_test_does_not_outlive_pytest(tmp_path: Path, stop: str) -> None:
    """The end-to-end test runs the smoke through run_bound_to_pytest. When pytest is stopped from outside, the smoke
    still runs its cleanup: every process it started is stopped and its folder removed. A stand-in for pytest runs the
    driver above that way, in a session of its own (so that its group is not this pytest's), and is then stopped."""
    tools = Path(smoke_shadow.__file__).resolve().parent
    script, ids, pid_file, temporary = driver_files(tmp_path)
    command = [sys.executable, str(script), str(tools), str(ids), str(pid_file)]
    with (tmp_path / "stand-in.out").open("wb") as output:
        stand_in = subprocess.Popen(
            [sys.executable, "-c", PYTEST_STAND_IN, str(Path(__file__).resolve().parent), str(tools), *command],
            env={**os.environ, "TMPDIR": str(temporary)},
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        assert wait_until(
            lambda: pid_file.is_file() and pid_file.read_text().strip() and ids.is_file() and ids.read_text().strip(),
            60,
        ), "the smoke started no process"
        smoke, child = int(pid_file.read_text()), int(ids.read_text().split()[0])
        assert wait_until(lambda: running(child), 10)
        time.sleep(0.5)  # the sleeper has exec'd past the launcher
        assert list(temporary.glob("cairn-repair-smoke-*")), "the smoke made no folder"
        if stop == "SIGKILL to pytest alone":
            os.kill(stand_in.pid, signal.SIGKILL)
        else:
            os.killpg(stand_in.pid, signal.SIGTERM if stop.startswith("SIGTERM") else signal.SIGINT)
        stand_in.wait(timeout=60)
        if stop.startswith("SIGINT"):
            # Ctrl-C reaches pytest as KeyboardInterrupt: the test stops the smoke and waits for its cleanup before
            # pytest goes on, so this holds on any system, without PR_SET_PDEATHSIG.
            assert not running(smoke) and not list(temporary.glob("cairn-repair-smoke-*")), "pytest ended first"
        assert wait_until(lambda: not running(smoke), 30), "the smoke outlived pytest"
        assert wait_until(lambda: not running(child), 10), "a process the smoke started outlived pytest"
        assert not list(temporary.glob("cairn-repair-smoke-*")), "the smoke's folder outlived pytest"
    finally:
        if stand_in.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(stand_in.pid, signal.SIGKILL)
            stand_in.wait(timeout=10)
        kill_leftovers(pid_file, ids)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_on_its_timeout_the_end_to_end_test_stops_the_smoke_and_what_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A smoke that has not finished within the end-to-end test's timeout gets SIGTERM, with its group: it stops what it
    started and removes its folder before the test fails."""
    tools = Path(smoke_shadow.__file__).resolve().parent
    script, ids, pid_file, temporary = driver_files(tmp_path)
    monkeypatch.setenv("TMPDIR", str(temporary))
    try:
        with pytest.raises(AssertionError, match="the smoke did not finish within 10 s") as failed:
            run_bound_to_pytest([sys.executable, str(script), str(tools), str(ids), str(pid_file)], timeout=10)
        assert "stopped by SIGTERM" in str(failed.value)
        child = int(ids.read_text().split()[0])
        assert wait_until(lambda: not running(child), 10), "a process the smoke started outlived the test"
        assert not list(temporary.glob("cairn-repair-smoke-*"))
    finally:
        kill_leftovers(pid_file, ids)


@pytest.mark.skipif(os.environ.get("CAIRN_REPAIR_SMOKE") != "1", reason="the full smoke takes about seven minutes")
def test_smoke_shadow_end_to_end() -> None:
    """XC5's steps through the real server, dispatcher and agent: register, copy, review, issue, /end, outcome."""
    result = run_bound_to_pytest([sys.executable, str(Path(smoke_shadow.__file__))], timeout=1200)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every step of the shadow path showed" in result.stdout
    assert json.dumps("smoke-test-model")[1:-1] in result.stdout  # the reviewer's model, logged by the agent
