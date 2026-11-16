"""Two fixes of build step RA2 to the live agent: the class of a poll without an answer (the final review's
R2_ADVERSARIAL-2) and the WARNING for local workers' temporary output (SECRETS_AND_ISOLATION-1).

- The server client turns whatever a request raises into status 0 and records the exception's class (spec 9.3); the
  failed poll's line now names that class, never the message, so a local failure such as a CA file removed after the
  start (OSError) no longer reads as a server outage (ConnectionError): `repair agent poll failed status=0
  error=<class> retry_in=<s>s` (9.11).
- In local mode the CLIs that local workers run leave raw run output in /tmp folders and files made with the default
  mode: Claude Code's /tmp/claude-<uid> (its background jobs' output), Cairn's pi session folder /tmp/cairn-pi and pi's
  spill files /tmp/pi-bash-<16 hex>.log. At the live agent's start only, when the agent's user can list such a folder
  of the copy root's owner, or read such a file of that user, the agent logs one WARNING that names them and the
  runbook's step, and starts all the same: it is no isolation reason, so a live install keeps starting after the
  upgrade. The replay, gate and export tools do not check it.

Every value is synthetic: made-up tokens, loopback and .invalid hosts, temporary folders.
"""

from __future__ import annotations

import ast
import logging
import os
import signal
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    AGENT_GID,
    AGENT_TOKEN,
    DISPATCHER_UID,
    IS_ROOT,
    OTHER_GID,
    OTHER_UID,
    HostInstall,
    Layout,
    set_mode,
)
from test_review_fixes_late import bare_agent
from test_review_loop import FORMAT_RULES

from cairn_repair_agent import cli, isolation, loop
from cairn_repair_agent.server import RepairServerClient, ServerAnswer

POLL_LINE = "repair agent poll failed status=0 error={error} retry_in=5s"
TEMP_LINE = (
    "repair agent can read local workers' temporary output paths={paths}; give them private temp folders "
    "(runbook, host install, step 4 e)"
)
SPILL = "pi-bash-0123456789abcdef.log"


class _Answers:
    """A server client whose polls answer what it is given, in order (an exception is raised), and then set the stop,
    as a signal would, and answer 503."""

    def __init__(self, stop: loop.SignalStop, answers: list[Any]) -> None:
        self.stop, self.answers, self.polls = stop, list(answers), 0

    def list_runs(self, **_: Any) -> ServerAnswer:
        self.polls += 1
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        self.stop.signals.append(signal.SIGTERM)
        return ServerAnswer(503)


def poll_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("repair agent poll failed")]


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        ("ConnectionError", "ConnectionError"),
        ("OSError", "OSError"),
        ("ReadTimeout", "ReadTimeout"),
        ("Weird name, with spaces", "other"),
        ("x" * 65, "other"),
        (None, "-"),
    ],
)
def test_a_poll_without_an_answer_names_the_recorded_class(
    caplog: pytest.LogCaptureFixture, error: str | None, shown: str
) -> None:
    """9.11's failed-poll line for status 0: `error=` is the class the server client recorded, `other` for one that
    is not a plain name (a class name of 1 to 64 letters, digits and underscores), `-` when none was recorded."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    agent = bare_agent(_Answers(loop.SignalStop(), [ServerAnswer(0, error=error)]))
    assert agent.poll_once() == 5
    assert poll_lines(caplog) == [POLL_LINE.format(error=shown)]
    for record in caplog.records:
        assert any(rule.fullmatch(record.getMessage()) for rule in FORMAT_RULES), record.getMessage()


def test_other_failed_polls_keep_their_lines(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A poll answered with another status, with a body that is not a poll's, or that raises, keeps its line: the
    status, or `status=error_<class>`, and no `error=`; an exception's message is never logged. The waits grow as
    before: 5, 10, 15, 15 s."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    stop = loop.SignalStop()
    answers: list[Any] = [
        ServerAnswer(503, {"detail": {"code": "token_not_configured"}}),
        ServerAnswer(200, {"runs": "not a list"}),
        OSError("canary-poll-message /canary/path"),
        ServerAnswer(0, error="ConnectionError"),
    ]
    agent = bare_agent(_Answers(stop, answers))
    waits: list[float] = []

    def stopped_within(seconds: float, stop_: loop.Stop) -> bool:
        waits.append(seconds)
        return stop_.is_set()

    monkeypatch.setattr(loop, "stopped_within", stopped_within)
    assert agent.run(once=False, stop=stop) == 0
    assert poll_lines(caplog) == [
        "repair agent poll failed status=503 retry_in=5s",
        "repair agent poll failed status=200 retry_in=10s",
        "repair agent poll failed status=error_OSError retry_in=15s",
        "repair agent poll failed status=0 error=ConnectionError retry_in=15s",
        "repair agent poll failed status=503 retry_in=15s",
    ]
    assert waits[:4] == [5, 10, 15, 15]
    assert all("canary" not in record.getMessage() for record in caplog.records)


def test_a_poll_with_the_real_client_names_the_class(caplog: pytest.LogCaptureFixture) -> None:
    """The real server client: a host it cannot encode gives LocationParseError, a closed loopback port a
    ConnectionError, each on the poll's line, and the URL never."""
    caplog.set_level(logging.INFO, logger="cairn_repair_agent")
    for server, error in (("http://a..invalid:8000", "LocationParseError"), ("http://127.0.0.1:9", "ConnectionError")):
        caplog.clear()
        assert bare_agent(RepairServerClient(server, AGENT_TOKEN)).poll_once() == 5
        assert poll_lines(caplog) == [POLL_LINE.format(error=error)]
        assert all(server not in record.getMessage() for record in caplog.records)


# --- the WARNING for local workers' temporary output ----------------------------------------------------------------


def make_temp(system: Path, owner: int, *, claude: int | None, pi: int | None, spill: int | None) -> Path:
    """A /tmp under the agent's system root holding, for each mode given, the copy root owner's Claude Code folder,
    Cairn's pi folder and a pi spill file (`None`: absent)."""
    tmp = system / "tmp"
    tmp.mkdir(exist_ok=True)
    set_mode(tmp, 0o1777)
    for name, mode, folder in ((f"claude-{owner}", claude, True), ("cairn-pi", pi, True), (SPILL, spill, False)):
        if mode is None:
            continue
        path = tmp / name
        if folder:
            path.mkdir()
            (path / "inside").write_text("raw run output\n")
        else:
            path.write_text("raw run output\n")
        if IS_ROOT:
            os.chown(path, owner, owner)
        set_mode(path, mode)
    return tmp


def temp_lines(run: Any) -> list[str]:
    return run.messages("repair agent can read local workers'")


def test_the_live_agent_warns_when_it_can_read_workers_temporary_output(layout: Layout, tmp_path: Path) -> None:
    """In this process (as root, or as the copy root's owner, who can list and read them): nothing in /tmp gives no
    line; Claude Code's folder of the copy root's owner, Cairn's pi folder and a pi spill file of that owner give one
    WARNING that names the three and the runbook's step, and the agent starts; a spill file of another user, or one
    whose name is not pi's, and a symlink named like one, are not named. The line is one of 9.11's formats."""
    system = Path(isolation.SYSTEM_ROOT)
    owner = os.stat(layout.root).st_uid
    run = layout.start("--allow-shared-user")
    assert run.started and temp_lines(run) == []
    tmp = make_temp(system, owner, claude=0o755, pi=0o755, spill=0o644)
    (tmp / "pi-bash-not-hex-at-all.log").write_text("x")
    if IS_ROOT:  # a spill file of another user
        other = tmp / "pi-bash-fedcba9876543210.log"
        other.write_text("x")
        os.chown(other, OTHER_UID, OTHER_GID)
    run = layout.start("--allow-shared-user")
    assert run.started, run.text()
    paths = f"/tmp/claude-{owner},/tmp/cairn-pi,/tmp/pi-bash-*.log"
    assert temp_lines(run) == [TEMP_LINE.format(paths=paths)]
    level = next(level for _, level, message in run.records if message.startswith("repair agent can read local"))
    assert level == "WARNING"
    assert all(any(rule.fullmatch(message) for rule in FORMAT_RULES) for _, _, message in run.records)
    (tmp / SPILL).unlink()
    (tmp / SPILL).symlink_to(tmp / "pi-bash-not-hex-at-all.log")
    assert temp_lines(layout.start("--allow-shared-user")) == [
        TEMP_LINE.format(paths=f"/tmp/claude-{owner},/tmp/cairn-pi")
    ]


def test_the_temp_warning_is_never_an_isolation_reason(host_install: HostInstall) -> None:
    """The host install of §13 step 4, the agent as its own user: with the dispatcher's user's Claude Code folder
    0755, its pi folder 0755 and a spill file 0644 in /tmp, the agent starts without --allow-shared-user, logs no
    isolation reason and one WARNING naming the three. With the folders 0700 and the file 0600 (the runbook's step
    4 e) it logs none; a folder listable through the agent's group is named, one that is searchable only is not."""
    tmp = make_temp(host_install.system, DISPATCHER_UID, claude=0o755, pi=0o755, spill=0o644)
    run = host_install.start()
    assert run.started, run.text()
    assert run.messages("repair agent isolation check failed") == []
    paths = f"/tmp/claude-{DISPATCHER_UID},/tmp/cairn-pi,/tmp/pi-bash-*.log"
    assert temp_lines(run) == [TEMP_LINE.format(paths=paths)]
    for name, mode in ((f"claude-{DISPATCHER_UID}", 0o700), ("cairn-pi", 0o700), (SPILL, 0o600)):
        set_mode(tmp / name, mode)
    run = host_install.start()
    assert run.started, run.text()
    assert temp_lines(run) == []
    set_mode(tmp / "cairn-pi", 0o711)  # searchable but not listable: the folder cannot be listed
    set_mode(tmp / f"claude-{DISPATCHER_UID}", 0o750)
    os.chown(tmp / f"claude-{DISPATCHER_UID}", DISPATCHER_UID, AGENT_GID)  # listable through the agent's group
    run = host_install.start()
    assert temp_lines(run) == [TEMP_LINE.format(paths=f"/tmp/claude-{DISPATCHER_UID}")]
    assert run.started and run.messages("repair agent isolation check failed") == []


def test_the_temp_check_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A copy root that cannot be examined, and a /tmp that cannot be listed, give no path and raise nothing."""
    assert isolation.worker_temp_paths_readable(str(tmp_path / "missing")) == []
    assert isolation.worker_temp_paths_readable("\0") == []
    monkeypatch.setattr(isolation, "SYSTEM_ROOT", str(tmp_path / "no-such-root"))
    assert isolation.worker_temp_paths_readable(str(tmp_path)) == []

    def scandir_fails(path: Any) -> Any:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(isolation.os, "scandir", scandir_fails)
    assert isolation._spill_file_readable(str(tmp_path), os.getuid()) is False


def test_only_the_live_agents_start_checks_workers_temporary_output() -> None:
    """The check is the live agent's alone: in the agent's package only cli.start calls it; the replay, the gate
    report and the export tool never do."""
    package = Path(cli.__file__).parent
    callers = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "worker_temp_paths_readable":
                callers.append(path.name)
            if isinstance(node, ast.Name) and node.id == "worker_temp_paths_readable":
                callers.append(path.name)
    assert callers == ["cli.py"]
    tools = Path(__file__).resolve().parents[2] / "tools" / "repair-phase2"
    for path in sorted(tools.glob("*.py")):
        assert "worker_temp_paths_readable" not in path.read_text(encoding="utf-8"), path.name
