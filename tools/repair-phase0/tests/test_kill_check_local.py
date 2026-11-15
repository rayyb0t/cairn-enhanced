"""Tests for kill_check.py's local mode: its logic, its rules for touching processes, and real runs."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

import kill_check

TOOL_DIR = Path(__file__).resolve().parents[1]
MARKER = kill_check.LOCAL_MARKER_PREFIX + "0123456789abcdef"
OTHER_MARKER = kill_check.LOCAL_MARKER_PREFIX + "fedcba9876543210"
TAG = f"{MARKER}.timeout_expiry"
POSIX = os.name == "posix" and Path("/bin/sh").exists()
needs_posix = pytest.mark.skipif(not POSIX, reason="local mode needs Linux or macOS")
STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


def tails_for(
    marker: str, name: str = "timeout_expiry", *, tool_pid: int = 4321, lifetime_s: int = 304, ignore_term: bool = False
) -> dict[str, str]:
    return kill_check.owned_tails(marker, name, ignore_term=ignore_term, tool_pid=tool_pid, lifetime_s=lifetime_s)


def known(marker: str, pids: dict[int, str], name: str = "timeout_expiry") -> kill_check.Started:
    """A run as the tool knows it, with these PIDs already learned (the tool learns them from the run itself)."""
    return kill_check.Started(name, Path("/nonexistent/cairn-killcheck-test"), tails_for(marker, name), dict(pids))


def child_cmdline(tag: str, role: str, argv0: str = "/usr/bin/python3") -> str:
    return " ".join([argv0, *kill_check.local_child_args(tag, role, tool_pid=4321, lifetime_s=304)])


def agent_cmdline(tag: str, argv0: str = "/bin/sh") -> str:
    command = kill_check.local_agent_command(tag, ignore_term=False, tool_pid=4321, lifetime_s=304)
    return " ".join([argv0, *command[1:]])


# --- what the tool starts --------------------------------------------------------------------


@needs_posix
@pytest.mark.parametrize("ignore_term", [False, True])
def test_local_agent_is_one_line_of_valid_sh(ignore_term: bool) -> None:
    script = kill_check.local_agent_script(ignore_term=ignore_term)
    assert "\n" not in script
    assert script.startswith("trap '' TERM;") is ignore_term
    checked = subprocess.run(["/bin/sh", "-n", "-c", script], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr


def test_every_argument_reads_back_as_built() -> None:
    # The tool recognises its processes by reading their command lines back, and `ps` on macOS shows a newline, a
    # tab or a non-ASCII character in an argument differently. Backslashes are left out too.
    compile(kill_check.LOCAL_CHILD_SOURCE, "<child>", "exec")
    compile(kill_check.LOCAL_CHILD_CODE, "<child>", "exec")
    agent = kill_check.local_agent_command(TAG, ignore_term=True, tool_pid=1, lifetime_s=9)
    child = kill_check.local_child_args(TAG, "noterm", tool_pid=1, lifetime_s=9)
    for argument in [*agent, *child]:
        assert re.fullmatch(r"[ -~]+", argument), argument
        assert "\\" not in argument, argument
    assert agent[:2] == ["/bin/sh", "-c"]
    assert agent[3] == f"{TAG}.agent"
    # The agent runs only Python children and shell builtins: no setsid binary (macOS has none). The path of that
    # Python, which may hold any character, comes in the environment rather than as an argument.
    commands = [part.split()[0] for part in re.split(r"[;&]", agent[2]) if part.strip()]
    assert set(commands) == {"trap", f'"${kill_check.PYTHON_ENV}"', "echo", "wait"}
    for unfit in ("print('a\\tb')", 'print("a")', "x = ~1", "print('é')", "print('a\tb')"):
        with pytest.raises(ValueError, match="cannot contain"):
            kill_check.child_code(unfit)


@needs_posix
def test_the_agent_starts_each_child_with_exactly_the_arguments_the_tool_expects(tmp_path: Path) -> None:
    # A stand-in for Python that writes down the arguments it got: one file per child, named after its tag.
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$4.args"\n', encoding="utf-8")
    recorder.chmod(0o755)
    command = kill_check.local_agent_command(TAG, ignore_term=False, tool_pid=4321, lifetime_s=304)
    env = {**os.environ, kill_check.PYTHON_ENV: str(recorder)}
    done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    expected = {label: tail for tail, label in tails_for(MARKER).items()}
    for role in kill_check.LOCAL_CHILD_ROLES:
        recorded = (tmp_path / f"{TAG}.{role}.args").read_text(encoding="utf-8").removesuffix("\n")
        assert recorded == expected[f"timeout_expiry.{role}"]
    assert " ".join(command[1:]) == expected["timeout_expiry.agent"]


def test_markers_are_unique_and_never_match_container_mode() -> None:
    markers = {kill_check.new_marker() for _ in range(50)}
    assert len(markers) == 50
    assert all(re.fullmatch(re.escape(kill_check.LOCAL_MARKER_PREFIX) + "[0-9a-f]{16}", m) for m in markers)
    container_agent = " ".join(["timeout", "-k", "2s", "4s", *kill_check.agent_command()])
    assert not any(f"{m}." in container_agent for m in markers)


def test_child_lifetime_outlasts_every_scenario() -> None:
    assert kill_check.child_lifetime_s(4, 2) > kill_check.LONG_TIMEOUT_S + 2 + 30
    assert kill_check.child_lifetime_s(600, 5) > 600 + 5 + 30


# --- recognising this run's processes ----------------------------------------------------------


def test_the_command_line_check_takes_only_the_exact_command_lines_the_tool_built() -> None:
    # The command-line half of the check; the PID half is tested below.
    tails = tails_for(MARKER)
    # Each process of the run counts, whatever its argv[0] reads back as: the macOS framework Python replaces it.
    for argv0 in ("/bin/sh", "sh"):
        assert kill_check.owned_label(agent_cmdline(TAG, argv0), tails) == "timeout_expiry.agent"
    for argv0 in (
        "/usr/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python",
        "/home/a user/.venv/bin/python",
    ):
        for role in kill_check.LOCAL_CHILD_ROLES:
            assert kill_check.owned_label(child_cmdline(TAG, role, argv0), tails) == f"timeout_expiry.{role}"
    child = child_cmdline(TAG, "plain")
    not_ours = [
        # What someone watching the run might start: the marker, a tag or a role in the arguments is not enough.
        f"grep -c {TAG}.agent",
        f"pgrep -fl {MARKER}",
        f'sh -c while sleep 0.5; do pgrep -f "$0" >/dev/null; done {TAG}',
        f"watch pgrep -f {TAG}.plain 4321 304",
        f"/usr/bin/python3 -I -c import time; time.sleep(60) {TAG}.agent {TAG}.plain 4321 304",
        child + " --and-more",  # the whole command line, then something else
        child.split(" ", 1)[1],  # no argv[0] in front of it
        child_cmdline(f"{OTHER_MARKER}.timeout_expiry", "plain"),  # the same tool, another run
        child_cmdline(f"{TAG}_stubborn", "plain"),  # this run, another scenario
        child.replace(" 4321 ", " 4322 "),  # another tool process
        child.replace(" 304", " 305"),
    ]
    for cmdline in not_ours:
        assert kill_check.owned_label(cmdline, tails) is None, cmdline


def test_a_process_is_the_tools_only_with_a_learned_pid_and_its_exact_command_line() -> None:
    agent, noterm, plain = agent_cmdline(TAG), child_cmdline(TAG, "noterm"), child_cmdline(TAG, "plain")
    sleeper = "/usr/bin/python3 -I -c import time; time.sleep(300)"
    run = known(
        MARKER,
        {
            42: "timeout_expiry.agent",
            43: "timeout_expiry.noterm",
            46: "timeout_expiry.plain",
            47: "timeout_expiry.setsid",
            os.getpid(): "timeout_expiry.agent",
        },
    )
    listing = {
        1: "/sbin/init",
        42: agent,
        43: noterm,
        os.getpid(): agent,  # whatever it has learned, the tool's own process is never one
        44: f"grep {TAG}.noterm",
        45: child_cmdline(f"{OTHER_MARKER}.timeout_expiry", "noterm"),
        46: "/usr/bin/vim notes.txt",  # learned, but the process ended and another one took over its PID
        47: plain,  # learned for one role, and now with the command line of another
        # Not learned, though each ends with exactly one of the run's command lines (what decoys D5 to D7 did):
        50: f"{sleeper} {agent.split(' ', 1)[1]}",  # the agent's argv[1:] at the end
        51: f"sh -c while :; do sleep 1; done sh {plain}",  # a whole command line as the last argument
        52: f"{sleeper} {plain.split(' ', 1)[1]}",  # a child's argv[1:] at the end
        53: plain,  # the same command line, started again by someone else
    }
    assert kill_check.owned_processes([run], lambda: listing) == {
        42: "timeout_expiry.agent",
        43: "timeout_expiry.noterm",
    }
    assert kill_check.owned_processes([run], lambda: None) is None


def test_the_children_pids_are_learned_from_their_pid_files_and_kept(tmp_path: Path) -> None:
    workdir = tmp_path / "cancel"
    workdir.mkdir()
    run = kill_check.Started("cancel", workdir, tails_for(MARKER, "cancel"))
    (workdir / "pid-plain").write_text("4242", encoding="ascii")
    (workdir / "pid-noterm").write_text("", encoding="ascii")  # not written yet
    (workdir / "pid-setsid.new").write_text("4244", encoding="ascii")  # not renamed into place yet
    (workdir / "pid-agent").write_text("4245", encoding="ascii")  # the agent's PID comes from Popen alone
    run.learn()
    assert run.pids == {4242: "cancel.plain"}
    (workdir / "pid-noterm").write_text("4243", encoding="ascii")
    (workdir / "pid-setsid").write_text("12ab", encoding="ascii")
    run.learn()
    assert run.pids == {4242: "cancel.plain", 4243: "cancel.noterm"}
    shutil.rmtree(workdir)  # once learned, a PID outlives the working directory
    run.learn()
    assert run.pids == {4242: "cancel.plain", 4243: "cancel.noterm"}


def test_parse_ps_output() -> None:
    output = f"    1 /sbin/launchd\n  612 /bin/sh -c x {TAG}.agent\n\nnot a pid line\n   77   \n"
    assert kill_check.parse_ps(output) == {1: "/sbin/launchd", 612: f"/bin/sh -c x {TAG}.agent"}


def test_cleanup_says_when_it_cannot_list_processes() -> None:
    runs = [known(MARKER, {42: "timeout_expiry.agent"})]
    assert kill_check.sweep(runs, listing=lambda: None) == {"killed": [], "left": None, "verified": False}
    assert kill_check.sweep(runs, listing=dict)["verified"] is True
    report = kill_check.stop_owned(runs, listing=lambda: None)
    assert report["running"] is None
    assert report["verified"] is False
    text = "\n".join(kill_check.describe_cleanup(report, MARKER, {"running": 0, "exited": 0, "killed": 0}))
    assert "WARNING: could not list processes" in text
    assert kill_check.pgrep_command(MARKER) in text


def test_kill_if_owned_never_signals_this_tool_or_a_pid_it_did_not_learn(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []
    monkeypatch.setattr(kill_check, "command_line", lambda pid: child_cmdline(TAG, "plain"))
    monkeypatch.setattr(kill_check, "_open_pidfd", lambda pid: None)
    monkeypatch.setattr(kill_check.os, "kill", lambda pid, sig: sent.append(pid))
    learned = {os.getpid(): "timeout_expiry.plain", 4_000_000_001: "timeout_expiry.plain"}
    assert kill_check.kill_if_owned(os.getpid(), [known(MARKER, learned)]) is False
    assert kill_check.kill_if_owned(4_000_000_001, [known(OTHER_MARKER, learned)]) is False  # not its command line
    assert kill_check.kill_if_owned(4_000_000_002, [known(MARKER, learned)]) is False  # its command line, not its PID
    assert kill_check.kill_if_owned(4_000_000_001, [known(MARKER, learned)]) is True
    assert sent == [4_000_000_001]


FAKE_PIDFD = 1_000_001  # never a real descriptor


def test_kill_if_owned_opens_the_pidfd_before_the_check_and_signals_through_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # The order is what makes a reused PID harmless on Linux: the pidfd pins the process before its command line
    # is read, and the signal goes through that pidfd, never to the bare PID.
    calls: list[tuple[Any, ...]] = []
    current = {"cmdline": child_cmdline(TAG, "plain"), "pidfd_open": "works"}
    real_close = os.close

    def pidfd_open(pid: int, flags: int = 0) -> int:
        calls.append(("pidfd_open", pid))
        if current["pidfd_open"] == "gone":
            raise ProcessLookupError(errno.ESRCH, "No such process")
        if current["pidfd_open"] == "unsupported":
            raise OSError(errno.ENOSYS, "Function not implemented")
        return FAKE_PIDFD

    def read_command_line(pid: int) -> str:
        calls.append(("command_line", pid))
        return current["cmdline"]

    def close(fd: int) -> None:
        if fd != FAKE_PIDFD:
            real_close(fd)
        else:
            calls.append(("close", fd))

    monkeypatch.setattr(kill_check.os, "pidfd_open", pidfd_open, raising=False)
    monkeypatch.setattr(
        kill_check.signal,
        "pidfd_send_signal",
        lambda fd, sig, *rest: calls.append(("pidfd_send_signal", fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(kill_check, "command_line", read_command_line)
    monkeypatch.setattr(kill_check.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(kill_check.os, "close", close)
    runs = [known(MARKER, {4242: "timeout_expiry.plain"})]

    assert kill_check.kill_if_owned(4242, runs) is True
    assert calls == [
        ("pidfd_open", 4242),
        ("command_line", 4242),
        ("pidfd_send_signal", FAKE_PIDFD, signal.SIGKILL),
        ("close", FAKE_PIDFD),
    ]

    calls.clear()
    current["cmdline"] = "/usr/bin/another-program --flag"  # the PID now belongs to some other program
    assert kill_check.kill_if_owned(4242, runs) is False
    assert calls == [("pidfd_open", 4242), ("command_line", 4242), ("close", FAKE_PIDFD)]

    calls.clear()
    current["pidfd_open"] = "gone"  # the process ended: nothing is read, nothing is sent
    assert kill_check.kill_if_owned(4242, runs) is False
    assert calls == [("pidfd_open", 4242)]

    calls.clear()
    current.update(pidfd_open="unsupported", cmdline=child_cmdline(TAG, "plain"))  # an old kernel: plain kill
    assert kill_check.kill_if_owned(4242, runs) is True
    assert calls == [("pidfd_open", 4242), ("command_line", 4242), ("kill", 4242, signal.SIGKILL)]


def test_kill_if_owned_never_signals_a_group_everything_init_or_itself_whatever_the_check_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its own guard, apart from the ownership check (which here takes every process): kill(0) would signal this
    # tool's whole process group, kill(-1) every process this user may signal, and a negative PID a whole group.
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(kill_check, "owned_label_of", lambda pid, cmdline, runs: "timeout_expiry.plain")
    monkeypatch.setattr(kill_check, "command_line", lambda pid: child_cmdline(TAG, "plain"))
    monkeypatch.setattr(kill_check, "_open_pidfd", lambda pid: calls.append(("pidfd_open", pid)))
    monkeypatch.setattr(kill_check.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    runs = [known(MARKER, {})]
    for pid in (0, 1, -1, -4242, os.getpid()):
        assert kill_check.kill_if_owned(pid, runs) is False, pid
    assert calls == []
    assert kill_check.kill_if_owned(4_000_000_001, runs) is True  # the stand-ins do kill: the guard said no above
    assert calls == [("pidfd_open", 4_000_000_001), ("kill", 4_000_000_001, signal.SIGKILL)]


def test_read_pid_takes_only_a_whole_pid_above_1(tmp_path: Path) -> None:
    # 0 and 1 would make kill_if_owned refuse at best; a PID file is never trusted for them.
    path = tmp_path / "pid-plain"
    cases = {"4242": 4242, "2": 2, "1": None, "0": None, "00": None, "-5": None, "": None, " 42": None, "42\n": None}
    for text, expected in cases.items():
        path.write_text(text, encoding="ascii")
        assert kill_check.read_pid(path) == expected, repr(text)
    path.write_text("²", encoding="utf-8")  # isdigit() says yes, int() says no
    assert kill_check.read_pid(path) is None
    assert kill_check.read_pid(tmp_path / "missing") is None


def test_ps_runs_in_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ctrl-C in a terminal goes to the whole foreground process group. In that group, ps would die with it and cut a
    # listing short: the cleanup would take "no listing" for "could not list" just when it matters.
    seen: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs, args=args)
        return subprocess.CompletedProcess(args, 0, stdout="    1 /sbin/launchd\n", stderr="")

    monkeypatch.setattr(kill_check.subprocess, "run", run)
    assert kill_check.ps_processes() == {1: "/sbin/launchd"}
    assert seen["args"][0] == "ps"
    assert seen["start_new_session"] is True


def test_an_interrupted_sweep_keeps_what_it_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = known(MARKER, {4242: "timeout_expiry.plain"})
    plain = child_cmdline(TAG, "plain")
    listings = {"n": 0}

    def listing() -> dict[int, str]:
        listings["n"] += 1
        if listings["n"] == 2:  # Ctrl-C after the first kill, before the sweep has finished
            raise KeyboardInterrupt
        return {4242: plain}

    monkeypatch.setattr(kill_check, "kill_if_owned", lambda pid, runs: True)
    killed: dict[int, str] = {}
    with pytest.raises(KeyboardInterrupt):
        kill_check.sweep([run], killed=killed, listing=listing)
    assert killed == {4242: "timeout_expiry.plain"}


class FakeAgent:
    """Stands in for the agent's Popen: poll() gives its exit status, None while it runs."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_children_whose_pids_are_unknown_count_until_the_agent_has_exited_by_itself() -> None:
    run = known(MARKER, {42: "timeout_expiry.agent", 43: "timeout_expiry.plain"})
    assert run.unaccounted() == []  # no agent started, so no children
    agent = FakeAgent(None)
    run.agent = cast(Any, agent)
    unknown = ["timeout_expiry.noterm", "timeout_expiry.setsid"]
    assert run.unaccounted() == unknown  # still running: they may be starting
    agent.returncode = -signal.SIGKILL
    assert run.unaccounted() == unknown  # killed: they may live on
    agent.returncode = 0
    assert run.unaccounted() == []  # exited by itself: its `wait` returned, so all its children have ended
    agent.returncode = None
    run.pids.update({44: "timeout_expiry.noterm", 45: "timeout_expiry.setsid"})
    assert run.unaccounted() == []  # all known: a listing finds them


def test_a_cleanup_waits_for_children_that_may_still_start_and_never_confirms_without_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kill_check, "READY_WAIT_S", 0.4)
    run = known(MARKER, {42: "timeout_expiry.agent"})
    agent = FakeAgent(None)
    run.agent, run.agent_started_at = cast(Any, agent), time.monotonic()
    started = time.monotonic()
    report = kill_check.stop_owned([run], wait_s=0, listing=dict)  # the agent's children never make themselves known
    assert time.monotonic() - started >= 0.35  # it waited for them until READY_WAIT_S after the agent started
    assert report["left"] == []  # no known process is left...
    assert report["unknown"] == ["timeout_expiry.plain", "timeout_expiry.noterm", "timeout_expiry.setsid"]
    assert report["verified"] is False  # ...but that is no confirmation

    run.agent_started_at = time.monotonic()
    exits = threading.Timer(0.1, setattr, args=(agent, "returncode", 0))  # the agent then exits by itself
    exits.start()
    started = time.monotonic()
    report = kill_check.stop_owned([run], wait_s=0, listing=dict)
    exits.join()
    assert time.monotonic() - started < 0.35  # no need to wait any longer
    assert report["unknown"] == []
    assert report["verified"] is True


# --- what ended each test process ------------------------------------------------------------------

AGENT_GROUP, SETSID_GROUP = 100, 103
GROUPS = {"agent": AGENT_GROUP, "plain": AGENT_GROUP, "noterm": AGENT_GROUP, "setsid": SETSID_GROUP}
ALL_ROLES = frozenset(kill_check.LOCAL_ROLES)


def listing(start: float, *shown: str) -> kill_check.Listing:
    """A listing that took 10 ms and showed these roles."""
    return kill_check.Listing(start, start + 0.01, frozenset(shown))


def lost(
    listings: list[kill_check.Listing],
    sent: list[kill_check.Sent],
    *,
    ignore_term: bool = False,
    agent_returncode: int | None = -signal.SIGTERM,
) -> list[str]:
    """kill_check.unexplained, with the survivors taken from the last listing, as the scenarios count them."""
    survivors = [role for role in kill_check.LOCAL_ROLES if role in listings[-1].roles]
    return kill_check.unexplained(
        listings, sent, GROUPS, survivors, ignore_term=ignore_term, agent_returncode=agent_returncode
    )


def sigterm_at(at: float, group: int | None = AGENT_GROUP, *, reaped: bool = False) -> kill_check.Sent:
    return kill_check.Sent(at, signal.SIGTERM, group, reaped=reaped)


def sigkill_at(at: float, group: int | None = AGENT_GROUP, *, reaped: bool = False) -> kill_check.Sent:
    return kill_check.Sent(at, signal.SIGKILL, group, reaped=reaped)


def test_which_signal_ends_which_test_process_by_construction() -> None:
    ends = kill_check.ends_by_construction
    for role in kill_check.LOCAL_ROLES:
        assert ends(role, signal.SIGKILL, ignore_term=False) and ends(role, signal.SIGKILL, ignore_term=True), role
        assert not ends(role, signal.SIGTERM, ignore_term=True), role  # the stubborn run: everyone ignores it
        assert not ends(role, signal.SIGINT, ignore_term=False), role  # LocalProcess sends no other signal
    assert [role for role in kill_check.LOCAL_ROLES if ends(role, signal.SIGTERM, ignore_term=False)] == [
        "agent",
        "plain",
        "setsid",
    ]


def test_an_undisturbed_run_is_explained_by_the_signals_the_kill_path_sent() -> None:
    # timeout_expiry: SIGTERM to the agent's group at 4.0 ends the agent and plain; noterm and setsid survive.
    listings = [listing(3.8, *ALL_ROLES), listing(4.05, "noterm", "setsid"), listing(4.25, "noterm", "setsid")]
    assert lost(listings, [sigterm_at(4.0)]) == []
    # The stubborn run: SIGTERM at 4.0 does nothing, SIGKILL to the group at 6.0 ends all but setsid.
    stubborn = [listing(3.8, *ALL_ROLES), listing(5.9, *ALL_ROLES), listing(6.05, "setsid")]
    sent = [sigterm_at(4.0), sigkill_at(6.0)]
    assert lost(stubborn, sent, ignore_term=True, agent_returncode=-signal.SIGKILL) == []
    # A process that is still listed for a moment after the signal, as on a busy machine, is still explained by it.
    lagging = [listing(3.8, *ALL_ROLES), listing(4.3, *ALL_ROLES), listing(4.5, "noterm", "setsid")]
    assert lost(lagging, [sigterm_at(4.0)]) == []
    # What survives needs no explanation, whatever was sent.
    assert lost([listing(3.8, *ALL_ROLES), listing(4.25, *ALL_ROLES)], []) == []


def test_only_a_signal_to_its_own_process_group_explains_the_end_of_a_test_process() -> None:
    # setsid, ended from outside 0.15 s after the SIGTERM that ended the agent: no signal of the path went its way.
    listings = [listing(3.8, *ALL_ROLES), listing(4.05, "noterm", "setsid"), listing(4.25, "noterm")]
    assert lost(listings, [sigterm_at(4.0)]) == ["setsid"]
    assert lost(listings, [sigterm_at(4.0), sigkill_at(4.1)]) == ["setsid"]
    assert lost(listings, [sigterm_at(4.0), sigkill_at(4.1, SETSID_GROUP)]) == []  # had the path signalled its group
    # A signal that went to no group (the agent was gone) explains nothing.
    gone = [listing(3.8, *ALL_ROLES), listing(4.05, "noterm", "setsid")]
    assert lost(gone, [sigterm_at(4.0, None)]) == ["agent", "plain"]


def test_a_survivor_that_a_noted_signal_to_its_group_ends_is_caught() -> None:
    outlived = kill_check.outlived
    # plain is still listed at the end, after a SIGTERM noted for its group while the agent had already been reaped:
    # LocalProcess found no group then, and that SIGTERM never reached it.
    assert outlived([sigterm_at(4.0, reaped=True)], GROUPS, ["plain", "noterm", "setsid"], ignore_term=False) == [
        "plain"
    ]
    # noterm ignores SIGTERM, and setsid is in no group that the path signals: their survival is the answer.
    assert outlived([sigterm_at(4.0, reaped=True)], GROUPS, ["noterm", "setsid"], ignore_term=False) == []
    # The stubborn run ignores SIGTERM, but its SIGKILL ends all of the agent's group.
    stubborn = outlived(
        [sigterm_at(4.0, reaped=True)], GROUPS, ["agent", "plain", "noterm", "setsid"], ignore_term=True
    )
    assert stubborn == []
    after_sigkill = outlived(
        [sigterm_at(4.0), sigkill_at(6.0, reaped=True)], GROUPS, ["plain", "noterm", "setsid"], ignore_term=True
    )
    assert after_sigkill == ["plain", "noterm"]
    # A signal noted with no group (the agent was gone), or with another group, says nothing about a survivor.
    noted_elsewhere = [sigkill_at(6.0, None, reaped=True), sigkill_at(6.0, 999, reaped=True)]
    assert outlived(noted_elsewhere, GROUPS, ["plain", "noterm"], ignore_term=False) == []
    # Nor does a role whose group the tool does not know.
    assert outlived([sigkill_at(6.0, reaped=True)], {**GROUPS, "plain": None}, ["plain"], ignore_term=False) == []
    # A signal sent while the agent was not yet reaped went out as LocalProcess sends it: what survives it is the
    # answer, even where a changed LocalProcess signals the agent alone.
    assert outlived([sigterm_at(4.0)], GROUPS, ["plain", "noterm", "setsid"], ignore_term=False) == []
    assert kill_check.outlived_problem(["plain"]) == (
        "plain outlived a signal that was noted for its process group and that ends it"
    )
    assert kill_check.outlived_problem(["plain", "noterm"]) == (
        "plain, noterm outlived a signal that was noted for their process group and that ends them"
    )


def test_only_a_signal_that_ends_a_test_process_explains_its_end() -> None:
    # noterm, ended from outside right after the SIGTERM to its group, which it ignores.
    listings = [listing(3.8, *ALL_ROLES), listing(4.05, "noterm", "setsid"), listing(4.25, "setsid")]
    assert lost(listings, [sigterm_at(4.0)]) == ["noterm"]
    # The stubborn run, in its grace period: plain ended right after the SIGTERM, which it ignores too.
    stubborn = [listing(3.8, *ALL_ROLES), listing(4.05, *ALL_ROLES), listing(4.25, "agent", "noterm", "setsid")]
    assert lost(stubborn, [sigterm_at(4.0)], ignore_term=True, agent_returncode=None) == ["plain"]


def test_a_signal_explains_the_end_of_a_test_process_only_at_the_right_time() -> None:
    # Sent no earlier than half a second (EXIT_LAG_S, as the README says) before the start of the last listing that
    # still showed the process: one listed later than that after the signal outlived it, and something else ended it.
    for shown_at, expected in ((4.49, []), (4.51, ["plain"])):
        listings = [
            listing(3.8, *ALL_ROLES),
            listing(4.05, "plain", "noterm", "setsid"),
            listing(shown_at, "plain", "noterm", "setsid"),
            listing(5.0, "noterm", "setsid"),
        ]
        assert lost(listings, [sigterm_at(4.0)]) == expected, shown_at
    # ...and before the end of the first listing after that which no longer showed it: plain was gone before the
    # SIGTERM went out.
    early = [listing(3.8, *ALL_ROLES), listing(3.9, "agent", "noterm", "setsid"), listing(4.25, "noterm", "setsid")]
    assert lost(early, [sigterm_at(4.0)]) == ["plain"]
    assert lost(early, [sigterm_at(3.905)]) == []  # sent while that listing ran: it may have come in time
    # A process that no listing showed, or that no listing showed gone, is never explained.
    never_shown = [listing(3.8, "agent", "noterm", "setsid"), listing(4.25, "noterm", "setsid")]
    assert lost(never_shown, [sigterm_at(4.0)]) == ["plain"]
    only_shown = [listing(3.8, *ALL_ROLES)]
    unexplained = kill_check.unexplained(
        only_shown, [sigterm_at(4.0)], GROUPS, ["noterm", "setsid"], ignore_term=False, agent_returncode=-15
    )
    assert unexplained == ["agent", "plain"]


def test_the_agent_is_explained_only_by_the_signal_its_exit_status_names() -> None:
    listings = [listing(3.8, *ALL_ROLES), listing(4.05, "noterm", "setsid")]
    assert lost(listings, [sigterm_at(4.0)], agent_returncode=-signal.SIGTERM) == []
    # Killed from outside the moment before the path's SIGTERM went out, or exited by itself, or not reaped.
    for returncode in (-signal.SIGKILL, 0, 1, None):
        assert lost(listings, [sigterm_at(4.0)], agent_returncode=returncode) == ["agent"], returncode
    # The stubborn agent, killed from outside during the grace: LocalProcess then sends no SIGKILL at all.
    stubborn = [listing(3.8, *ALL_ROLES), listing(5.0, *ALL_ROLES), listing(5.2, "plain", "noterm", "setsid")]
    assert lost(stubborn, [sigterm_at(4.0)], ignore_term=True, agent_returncode=-signal.SIGKILL) == ["agent"]


def test_what_the_timeout_met_is_the_last_listing_that_ended_before_it_could_fire() -> None:
    listings = [
        kill_check.Listing(1.0, 1.1, ALL_ROLES),
        kill_check.Listing(1.8, 2.05, frozenset({"agent"})),  # still running when the timeout could fire
        kill_check.Listing(1.9, 1.95, frozenset({"agent", "plain"})),
    ]
    assert kill_check.shown_before(listings, 2.0) == ["agent", "plain"]
    assert kill_check.shown_before(listings, 1.5) == list(kill_check.LOCAL_ROLES)
    assert kill_check.shown_before(listings, 1.0) == []  # nothing listed yet: nothing is known to run


def test_the_monitor_lists_until_stopped_and_outlives_a_failed_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kill_check, "POLL_S", 0.01)
    calls: list[float] = []

    def list_roles() -> None:
        calls.append(time.monotonic())
        if len(calls) == 2:
            raise RuntimeError("could not list the processes on this machine")

    monitor = kill_check.Monitor(list_roles)
    assert wait_for(lambda: len(calls) >= 5, timeout=5)
    monitor.stop()
    count = len(calls)
    time.sleep(0.1)
    assert len(calls) == count


# --- reports ------------------------------------------------------------------------------------

LOCAL_RESULTS = {
    "timeout_expiry": {
        "timeout_s": 4,
        "returncode": -15,
        "timed_out": True,
        "cancelled": False,
        "did_timeout": True,
        "exec_ended_after_s": 4.0,
        "ended_after_s": 14.0,
        "exec_running_after": False,
        "survivors": ["noterm", "setsid"],
    },
    "timeout_expiry_stubborn": {
        "timeout_s": 4,
        "returncode": -9,
        "timed_out": True,
        "cancelled": False,
        "did_timeout": True,
        "exec_ended_after_s": 6.0,
        "ended_after_s": 16.0,
        "exec_running_after": False,
        "survivors": ["setsid"],
    },
    "local_kill": {
        "returncode": -15,
        "timed_out": False,
        "cancelled": False,
        "did_timeout": False,
        "kill_call_s": 0.0,
        "exec_running_after_kill": False,
        "stopped_after_s": 0.0,
        "communicate_returned_after_kill_s": 10.0,
        "survivors": ["noterm", "setsid"],
    },
    "cancel": {
        "returncode": -15,
        "timed_out": False,
        "cancelled": True,
        "cancel_reason": "kill-check: project stopped",
        "did_timeout": False,
        "exec_ended_after_cancel_s": 0.0,
        "communicate_returned_after_cancel_s": 10.0,
        "exec_running_after": False,
        "survivors": ["noterm", "setsid"],
    },
}


def test_local_conclusions() -> None:
    lines = kill_check.local_conclusions(LOCAL_RESULTS)
    text = " ".join(lines)
    assert "timeout_expiry_stubborn: LocalProcess's own timeout (4 s) ended the run with exit -9" in text
    assert "SIGKILL to its process group after the grace period" in text
    assert "having waited 10.0 s for output pipes that surviving children still held open" in text
    assert "local_kill: LocalProcess.kill(), which a lost heartbeat lease calls, stopped the agent 0.0 s" in text
    assert "cancelled=True ('kill-check: project stopped')" in text
    assert "A child that ignores SIGTERM survived timeout_expiry, local_kill, cancel." in text
    assert "survived every scenario" in text
    assert "nothing in LocalBackend stops them later" in text
    assert "did_timeout() rests on the timed_out flag alone" in text
    # Failed or missing scenarios are left out rather than breaking the report, and "every scenario" then is not.
    assert kill_check.local_conclusions({"cancel": {"error": "RuntimeError: boom"}}) == []
    with_error = {**LOCAL_RESULTS, "broken": {"error": "setsid did not start within 15 s; results not valid"}}
    assert not any("survived every scenario" in line for line in kill_check.local_conclusions(with_error))
    survivors_gone = {name: {**r, "survivors": []} for name, r in LOCAL_RESULTS.items()}
    assert not any("survived" in line for line in kill_check.local_conclusions(survivors_gone))


# The container table exactly as it printed before local mode existed.
CONTAINER_RESULTS = {
    "wrapper_expiry": {
        "returncode": 124,
        "ended_after_s": 6.1,
        "exec_running_after": False,
        "survivors": ["noterm", "setsid"],
    },
    "managed_kill": {
        "stopped_after_s": None,
        "exec_running_after_kill": True,
        "survivors": ["wrapper", "agent", "plain", "noterm", "setsid"],
    },
    "communicate_timeout": {
        "returncode": 137,
        "communicate_returned_after_s": 7.1,
        "exec_running_after": True,
        "survivors": [],
    },
    "cancel": {
        "returncode": 124,
        "communicate_returned_after_cancel_s": 11.7,
        "exec_running_after": False,
        "survivors": ["setsid"],
    },
    "broken": {"error": "APIError: boom"},
}
CONTAINER_TABLE = """
scenario                    exit  ended/returned  run still going survivors
wrapper_expiry               124           6.1 s  no              noterm, setsid
managed_kill                   -               -  yes             wrapper, agent, plain, noterm, setsid
communicate_timeout          137           7.1 s  yes             nothing
cancel                       124          11.7 s  no              setsid
broken                      error: APIError: boom
"""


def test_container_table_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    kill_check.print_table(CONTAINER_RESULTS)
    assert capsys.readouterr().out == CONTAINER_TABLE


def test_local_table_adds_the_flags(capsys: pytest.CaptureFixture[str]) -> None:
    kill_check.print_table(LOCAL_RESULTS, show_flags=True)
    out = capsys.readouterr().out
    assert out.splitlines()[1].split() == [
        "scenario",
        "exit",
        "flags",
        "ended/returned",
        "run",
        "still",
        "going",
        "survivors",
    ]
    assert re.search(r"^timeout_expiry +-15  timed_out +14\.0 s  no +noterm, setsid$", out, re.M)
    assert re.search(r"^timeout_expiry_stubborn +-9  timed_out +16\.0 s  no +setsid$", out, re.M)
    assert re.search(r"^local_kill +-15  - +10\.0 s  no +noterm, setsid$", out, re.M)
    assert re.search(r"^cancel +-15  cancelled +10\.0 s  no +noterm, setsid$", out, re.M)


def test_cleanup_summary_counts_what_exited_and_what_was_killed() -> None:
    clean = {"running": [], "exited": [], "killed": [], "left": [], "verified": True}
    assert kill_check.describe_cleanup(clean, MARKER, {"running": 7, "exited": 6, "killed": 1}) == [
        "Cleanup: 7 test process(es) were still running when their scenario ended "
        f"(6 exited when asked to stop, 1 had to be killed); nothing with marker {MARKER} is running now."
    ]
    after_ctrl_c = {
        "running": [{"pid": 11, "process": "cancel.agent"}, {"pid": 12, "process": "cancel.setsid"}],
        "exited": [{"pid": 11, "process": "cancel.agent"}, {"pid": 12, "process": "cancel.setsid"}],
        "killed": [],
        "left": [],
        "verified": True,
    }
    assert kill_check.describe_cleanup(after_ctrl_c, MARKER, {"running": 0, "exited": 0, "killed": 0}) == [
        "Cleanup: the final cleanup found 2 (cancel.agent, cancel.setsid): 2 exited when asked to stop, "
        f"0 had to be killed; nothing with marker {MARKER} is running now."
    ]
    assert kill_check.describe_cleanup(clean, MARKER, {"running": 0, "exited": 0, "killed": 0}) == [
        f"Cleanup: no test process was still running at the end; nothing with marker {MARKER} is running now."
    ]


def test_leftover_advice_names_no_pid_to_kill_later() -> None:
    # A PID printed now may belong to another process by the time someone acts on it.
    left = [{"pid": 4242, "process": "cancel.noterm"}]
    final = {"running": left, "exited": [], "killed": [], "left": left, "verified": False}
    text = "\n".join(kill_check.describe_cleanup(final, MARKER, {"running": 0, "exited": 0, "killed": 0}))
    assert "WARNING: still running: cancel.noterm." in text
    assert "4242" not in text
    assert kill_check.pgrep_command(MARKER) in text
    assert f"pkill -9 -f '{kill_check.kill_pattern(MARKER)}'" in text


def test_the_pkill_pattern_matches_only_this_runs_command_lines() -> None:
    pattern = re.compile(kill_check.kill_pattern(MARKER))  # an extended regex that Python reads the same way
    tails: dict[str, str] = {}
    for name in kill_check.LOCAL_SCENARIOS:
        for ignore_term in (False, True):
            tails.update(tails_for(MARKER, name, ignore_term=ignore_term))
    for tail in tails:
        assert pattern.search("/usr/bin/python3 " + tail), tail
    for cmdline in (
        f"pgrep -fl {MARKER}",
        f"grep -c {TAG}.agent",
        f"watch pgrep -f {TAG}.plain",
        f"vim {TAG}.plain.txt",
        child_cmdline(f"{OTHER_MARKER}.cancel", "plain"),
    ):
        assert not pattern.search(cmdline), cmdline


def test_the_pgrep_advice_asks_for_whole_command_lines_on_each_platform() -> None:
    # procps (Linux) prints them with -a and only the process name with -l; BSD pgrep (macOS) prints them with -l when
    # -f is given, and its -a means "include ancestors".
    assert kill_check.pgrep_command(MARKER, "linux") == f"pgrep -fa {MARKER}"
    assert kill_check.pgrep_command(MARKER, "darwin") == f"pgrep -fl {MARKER}"
    assert kill_check.pgrep_command(MARKER, "freebsd14") == f"pgrep -fl {MARKER}"
    assert kill_check.pgrep_command(MARKER) == kill_check.pgrep_command(MARKER, sys.platform)


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("pgrep"), reason="needs pgrep on Linux")
def test_the_pgrep_advice_shows_whole_command_lines_here() -> None:
    marker = kill_check.new_marker()
    argv = [sys.executable, "-I", "-c", "import time; time.sleep(30)", f"{marker}.cancel.plain", "4321", "304"]
    sleeper = subprocess.Popen(argv)  # a process of this test's own, which only mentions the marker
    try:
        assert shows(sleeper.pid, marker)
        command = kill_check.pgrep_command(marker).split()
        listed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False).stdout
        assert f"{sleeper.pid} {' '.join(argv)}" in listed.splitlines()
    finally:
        stop(sleeper)


def test_a_cleanup_that_never_learned_a_childs_pid_is_not_confirmed() -> None:
    final = {
        "running": [{"pid": 11, "process": "cancel.agent"}],
        "exited": [],
        "killed": [{"pid": 11, "process": "cancel.agent"}],
        "left": [],
        "unknown": ["cancel.noterm", "cancel.setsid"],
        "verified": False,
    }
    text = "\n".join(kill_check.describe_cleanup(final, MARKER, {"running": 0, "exited": 0, "killed": 0}))
    assert "WARNING: cannot confirm that cancel.noterm, cancel.setsid stopped" in text
    assert "is running now" not in text
    assert kill_check.pgrep_command(MARKER) in text
    assert f"pkill -9 -f '{kill_check.kill_pattern(MARKER)}'" in text


def test_a_test_process_that_stopped_outside_the_kill_path_is_named() -> None:
    assert kill_check.stopped_before(list(kill_check.LOCAL_ROLES)) is None
    assert kill_check.stopped_before(["agent", "noterm"]) == f"plain, setsid {kill_check.STOPPED_BEFORE}"
    assert kill_check.stopped_after(None, []) is None  # the path never ended: nothing to compare
    assert kill_check.stopped_after(["noterm", "setsid"], ["noterm", "setsid"]) is None
    assert kill_check.stopped_after(["noterm", "setsid"], ["noterm"]) == f"setsid {kill_check.STOPPED_AFTER}"


# --- command line ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "local", "--image", "debian:bookworm-slim"],
        ["--mode", "local", "--config", "dispatch.yaml"],
        ["--mode", "local", "--scenario", "managed_kill"],
        ["--mode", "local", "--timeout", "0"],
        ["--scenario", "local_kill"],  # container mode is the default
    ],
)
def test_options_that_do_not_fit_the_mode_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        kill_check.main([*argv, "--dry-run"])
    assert refused.value.code == 2


def test_each_local_scenario_runs_once(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--mode", "local", "--scenario", "cancel", "--scenario", "local_kill", "--scenario", "cancel", "--dry-run"]
    assert kill_check.main(argv) == 0
    assert "\nscenarios: cancel, local_kill\n" in capsys.readouterr().out


def test_local_dry_run_needs_nothing_beyond_the_standard_library() -> None:
    done = subprocess.run(
        [sys.executable, "-I", str(TOOL_DIR / "kill_check.py"), "--mode", "local", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "no Docker" in done.stdout
    assert "os.setsid()" in done.stdout
    assert f"with {kill_check.PYTHON_ENV}=<this python>" in done.stdout
    assert "scenarios: timeout_expiry, timeout_expiry_stubborn, local_kill, cancel" in done.stdout


# --- with real processes ------------------------------------------------------------------------


def start_child(tmp_path: Path, marker: str, name: str, role: str, *, lifetime_s: int = 60) -> subprocess.Popen[bytes]:
    """One test child, started exactly as the agent starts it (this test process standing in for the tool), in
    the working directory `tmp_path / name`. It runs for a minute at most."""
    workdir = tmp_path / name
    workdir.mkdir(parents=True, exist_ok=True)
    args = kill_check.local_child_args(f"{marker}.{name}", role, tool_pid=os.getpid(), lifetime_s=lifetime_s)
    return subprocess.Popen([sys.executable, *args], cwd=workdir)


def child_run(tmp_path: Path, marker: str, name: str) -> kill_check.Started:
    """How the tool would know the children that start_child starts in `tmp_path / name`: it learns their PIDs
    from the pid files they write there."""
    return kill_check.Started(name, tmp_path / name, tails_for(marker, name, tool_pid=os.getpid(), lifetime_s=60))


def wait_for(condition: Any, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def owned(pid: int, run: kill_check.Started) -> bool:
    """Waits until `pid` shows up as one of the tool's (once it has written its pid file)."""
    return wait_for(lambda: pid in (kill_check.owned_processes([run]) or {}))


def shows(pid: int, text: str) -> bool:
    """Waits until the command line of `pid` contains `text`."""
    return wait_for(lambda: text in (kill_check.command_line(pid) or ""))


def mentioning(marker: str) -> dict[int, str]:
    """Every process whose command line mentions `marker`. For checks only: the tests never signal on this."""
    return {pid: cmdline for pid, cmdline in (kill_check.all_processes() or {}).items() if marker in cmdline}


def stop(*processes: subprocess.Popen[bytes]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


LISTING_METHODS = [
    pytest.param(
        "proc", marks=pytest.mark.skipif(not kill_check.PROC.joinpath("self", "cmdline").is_file(), reason="no /proc")
    ),
    pytest.param("ps", marks=pytest.mark.skipif(shutil.which("ps") is None, reason="no ps")),
]


@needs_posix
@pytest.mark.parametrize("method", LISTING_METHODS)
def test_both_listings_read_the_command_line_back_exactly(
    method: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Linux reads /proc and kills through a pidfd. macOS has neither: the "ps" case takes its path, with ps for
    # listing and for the check before the kill, and a plain os.kill.
    monkeypatch.setattr(kill_check, "listing_method", lambda: method)
    if method == "ps":
        monkeypatch.setattr(kill_check, "_open_pidfd", lambda pid: None)
    marker = kill_check.new_marker()
    run = child_run(tmp_path, marker, "listing")
    child = start_child(tmp_path, marker, "listing", "noterm")
    try:
        assert owned(child.pid, run)
        assert run.pids == {child.pid: "listing.noterm"}  # learned from the child's own pid file
        assert kill_check.owned_processes([run]) == {child.pid: "listing.noterm"}
        assert kill_check.owned_label(kill_check.command_line(child.pid) or "", run.tails) == "listing.noterm"
        report = kill_check.sweep([run])
        assert child.wait(timeout=5) == -signal.SIGKILL
        assert report == {"killed": [{"pid": child.pid, "process": "listing.noterm"}], "left": [], "verified": True}
    finally:
        stop(child)


@needs_posix
@pytest.mark.parametrize("method", LISTING_METHODS)
def test_sweep_never_touches_a_process_that_it_did_not_start(
    method: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kill_check, "listing_method", lambda: method)
    if method == "ps":
        monkeypatch.setattr(kill_check, "_open_pidfd", lambda pid: None)
    marker, other = kill_check.new_marker(), kill_check.new_marker()
    run = child_run(tmp_path, marker, "sweep")
    mine = start_child(tmp_path, marker, "sweep", "plain")
    theirs = start_child(tmp_path / "other", other, "sweep", "plain")  # the same tool's child, from another run
    assert owned(mine.pid, run)
    assert owned(theirs.pid, child_run(tmp_path / "other", other, "sweep"))
    mine_cmdline = kill_check.command_line(mine.pid)
    assert mine_cmdline is not None
    # What someone watching the run might start, D1 to D7 of the review: the marker, the tags, the tool's PID, and
    # at the end of D5 to D7 exactly one of the run's own command lines, which the command-line check alone takes.
    tag = f"{marker}.sweep"
    agent_argv = kill_check.local_agent_command(tag, ignore_term=False, tool_pid=os.getpid(), lifetime_s=60)
    child_argv = kill_check.local_child_args(tag, "plain", tool_pid=os.getpid(), lifetime_s=60)
    sleeper = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
    feed_r, feed_w = os.pipe()
    decoys = {
        "D1": subprocess.Popen(["grep", "-c", f"{tag}.agent"], stdin=feed_r, stdout=subprocess.DEVNULL),
        "D2": subprocess.Popen(["sh", "-c", 'while sleep 0.5; do pgrep -f "$0" >/dev/null; done', tag]),
        "D3": subprocess.Popen([*sleeper, f"{tag}.agent", f"{tag}.plain", tag, str(os.getpid()), "60"]),
        "D4": subprocess.Popen([*sleeper, *agent_argv[1:], "--watching"]),
        "D5": subprocess.Popen([*sleeper, *agent_argv[1:]]),
        "D6": subprocess.Popen(["sh", "-c", "while :; do sleep 1; done", "sh", mine_cmdline]),
        "D7": subprocess.Popen([*sleeper, *child_argv]),
    }
    os.close(feed_r)
    try:
        for name, decoy in decoys.items():
            assert shows(decoy.pid, marker), name
        for name in ("D5", "D6", "D7"):
            assert kill_check.owned_label(kill_check.command_line(decoys[name].pid) or "", run.tails), name
        report = kill_check.sweep([run])
        assert mine.wait(timeout=5) == -signal.SIGKILL
        assert theirs.poll() is None
        for name, decoy in decoys.items():
            assert decoy.poll() is None, name
        assert report == {"killed": [{"pid": mine.pid, "process": "sweep.plain"}], "left": [], "verified": True}
    finally:
        os.close(feed_w)
        stop(mine, theirs, *decoys.values())


@needs_posix
def test_sweep_reads_each_command_line_again_before_the_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = kill_check.new_marker()
    run = child_run(tmp_path, marker, "recheck")
    child = start_child(tmp_path, marker, "recheck", "plain")
    try:
        assert owned(child.pid, run)
        # As if the process had ended and its PID now belonged to some other program at the moment of the kill.
        monkeypatch.setattr(kill_check, "command_line", lambda pid: "/usr/bin/another-program --flag")
        report = kill_check.sweep([run], deadline_s=0.5)
        assert child.poll() is None
        assert report == {"killed": [], "left": [{"pid": child.pid, "process": "recheck.plain"}], "verified": False}
    finally:
        stop(child)


@needs_posix
@pytest.mark.parametrize("how", ["stop file", "workspace gone", "lifetime"])
@pytest.mark.parametrize("role", kill_check.LOCAL_CHILD_ROLES)
def test_a_test_child_exits_by_itself_and_says_so_where_it_can(tmp_path: Path, role: str, how: str) -> None:
    child = start_child(tmp_path, kill_check.new_marker(), "polite", role, lifetime_s=1 if how == "lifetime" else 60)
    workdir = tmp_path / "polite"
    try:
        assert wait_for(lambda: (workdir / f"ready-{role}").exists())
        assert (workdir / f"pid-{role}").read_text(encoding="ascii") == str(child.pid)
        assert not (workdir / f"exited-{role}").exists()
        if how == "stop file":
            (workdir / kill_check.STOP_FILE).touch()
        elif how == "workspace gone":
            shutil.rmtree(workdir)
        assert child.wait(timeout=kill_check.STOP_WAIT_S + 2) == 0
        # The tool takes either sign as an exit that the path under test did not cause (see disturbance).
        if how == "workspace gone":
            assert not workdir.exists()  # it could not say so, and the tool sees the directory gone instead
        else:
            assert (workdir / f"exited-{role}").exists()
    finally:
        stop(child)


@needs_posix
@pytest.mark.parametrize("role", kill_check.LOCAL_CHILD_ROLES)
def test_a_test_child_exits_by_itself_once_the_tool_is_gone(tmp_path: Path, role: str) -> None:
    # All that stops the children after `kill -9` of the tool: no cleanup runs, and the workspace stays.
    stand_in = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])  # plays the tool
    workdir = tmp_path / "orphan"
    workdir.mkdir()
    args = kill_check.local_child_args(f"{MARKER}.orphan", role, tool_pid=stand_in.pid, lifetime_s=60)
    child = subprocess.Popen([sys.executable, *args], cwd=workdir)
    try:
        assert wait_for(lambda: (workdir / f"ready-{role}").exists())
        time.sleep(0.3)
        assert child.poll() is None  # the tool is still there: the child waits
        stand_in.kill()
        stand_in.wait()  # gone and reaped, as a shell reaps a command it ran
        assert child.wait(timeout=kill_check.STOP_WAIT_S + 2) == 0
        assert (workdir / f"exited-{role}").exists()
    finally:
        stop(child, stand_in)


@needs_posix
def test_stop_owned_asks_first_and_kills_only_what_did_not_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A noterm child that never gets to look for the stop file, as a stuck process would.
    setup = "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    source = kill_check.LOCAL_CHILD_SOURCE
    assert source.count(setup) == 1
    monkeypatch.setattr(
        kill_check,
        "LOCAL_CHILD_CODE",
        kill_check.child_code(source.replace(setup, setup + "    time.sleep(lifetime)\n")),
    )
    marker = kill_check.new_marker()
    run = child_run(tmp_path, marker, "stop")
    polite = start_child(tmp_path, marker, "stop", "plain")
    stuck = start_child(tmp_path, marker, "stop", "noterm")
    try:
        assert owned(polite.pid, run)
        assert owned(stuck.pid, run)
        assert wait_for(lambda: (tmp_path / "stop" / "ready-plain").exists())
        started = time.monotonic()
        report = kill_check.stop_owned([run])
        assert time.monotonic() - started >= kill_check.STOP_WAIT_S  # the stuck one had its chance first
        assert polite.wait(timeout=5) == 0
        assert stuck.wait(timeout=5) == -signal.SIGKILL
        assert report["running"] == [
            {"pid": pid, "process": label}
            for pid, label in sorted({polite.pid: "stop.plain", stuck.pid: "stop.noterm"}.items())
        ]
        assert report["exited"] == [{"pid": polite.pid, "process": "stop.plain"}]
        assert report["killed"] == [{"pid": stuck.pid, "process": "stop.noterm"}]
        assert report["verified"] is True
    finally:
        stop(polite, stuck)


def _cannot_run_local_mode() -> str | None:
    """Why a real local run cannot happen here, or None when it can."""
    if not POSIX:
        return "local mode needs Linux or macOS"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import cairn.dispatcher.runtime.local_backend, cairn.dispatcher.runtime.cancellation, "
            "cairn.dispatcher.tasks.common",
            str(kill_check.CAIRN_SRC),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        last = (probe.stderr.strip().splitlines() or ["?"])[-1]
        return f"the dispatcher code does not import with this Python: {last}"
    return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[kill_check.LocalHarness]:
    """The real LocalHarness in this test process, which stands in for the tool's."""
    reason = _cannot_run_local_mode()
    if reason:
        pytest.skip(reason)
    monkeypatch.syspath_prepend(str(kill_check.CAIRN_SRC))
    local = kill_check.LocalHarness(1, 1, kill_check.new_marker())
    try:
        yield local
    finally:
        local.close()


BROKEN_CHILDREN = [
    pytest.param("    os.setsid()\n", "    raise SystemExit('simulated: os.setsid() failed')\n", "setsid", id="exits"),
    pytest.param(
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n",
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n    time.sleep(lifetime)\n",
        "noterm",
        id="never-ready",
    ),
]


@needs_posix
@pytest.mark.parametrize(("old", "new", "role"), BROKEN_CHILDREN)
def test_a_child_that_does_not_start_makes_the_scenario_an_error(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
    role: str,
) -> None:
    # Reported as a child that died, it would give a wrong answer; the scenario must give none.
    source = kill_check.LOCAL_CHILD_SOURCE
    assert source.count(old) == 1
    monkeypatch.setattr(kill_check, "LOCAL_CHILD_CODE", kill_check.child_code(source.replace(old, new)))
    monkeypatch.setattr(kill_check, "READY_WAIT_S", 2.0)
    result = harness.timeout_expiry()
    assert_error_row(result, f"{role} did not start within 2 s; results not valid", harness, capsys)


@needs_posix
def test_an_agent_that_exits_at_once_is_named_as_such(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # It did start: "did not start within 15 s" after a fraction of a second would send you looking in the wrong place.
    # Status 0, so that the cleanup knows at once that it left no children (see Started.unaccounted).
    monkeypatch.setattr(kill_check, "local_agent_script", lambda *, ignore_term: "exit 0")
    started = time.monotonic()
    result = harness.timeout_expiry()
    assert time.monotonic() - started < kill_check.READY_WAIT_S
    error = "the agent exited (status 0) before plain, noterm, setsid were ready; results not valid"
    assert_error_row(result, error, harness, capsys)


def assert_error_row(
    result: dict[str, Any], error: str, harness: kill_check.LocalHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """`result` is an error with no results in it, its cleanup left nothing running, and it prints as an error."""
    assert result["error"] == error
    assert "survivors" not in result
    assert "returncode" not in result
    assert result["cleanup"]["verified"] is True
    assert kill_check.owned_processes(harness.started) == {}
    assert wait_for(lambda: mentioning(harness.marker) == {})
    assert kill_check.local_conclusions({"timeout_expiry": result}) == []
    kill_check.print_table({"timeout_expiry": result}, show_flags=True)
    assert f"\n{'timeout_expiry':<26}  error: {error}\n" in capsys.readouterr().out


@needs_posix
@pytest.mark.parametrize(
    ("when", "delay_s"),
    [("while the children start", None), ("before the kill", 0.0), ("after the kill", 1.5)],
)
def test_a_scenario_whose_working_directory_is_removed_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: str,
    delay_s: float | None,
) -> None:
    # Its test children exit once their working directory is gone. Taken for the work of the kill path, that gave
    # wrong answers: no survivors at all, after any kill path. The timeout here is 1 s, so "after the kill" removes
    # it while the children that survived the SIGTERM keep communicate() waiting.
    removals: list[threading.Timer] = []
    if delay_s is None:
        running_roles = harness.running_roles

        def remove_once_all_run(run: kill_check.LocalRun) -> list[str]:
            found = running_roles(run)
            if len(found) == len(kill_check.LOCAL_ROLES):
                shutil.rmtree(run.workdir, ignore_errors=True)
            return found

        monkeypatch.setattr(harness, "running_roles", remove_once_all_run)
    else:
        wait_for_children, delay = harness.wait_for_children, delay_s

        def remove_later(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
            outcome = wait_for_children(run)
            removals.append(threading.Timer(delay, shutil.rmtree, args=(run.workdir,), kwargs={"ignore_errors": True}))
            removals[-1].start()
            return outcome

        monkeypatch.setattr(harness, "wait_for_children", remove_later)
    try:
        result = harness.timeout_expiry()
    finally:
        for removal in removals:
            removal.join()
    assert_error_row(result, f"{kill_check.WORKDIR_REMOVED}; results not valid", harness, capsys)


@needs_posix
def test_a_child_that_exits_on_its_own_during_the_scenario_gives_no_results(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # As when the tool was suspended (Ctrl-Z) past the children's lifetime: the children are gone by the time the
    # kill path runs (the timeout, 4 s here), and must not count as stopped by it.
    monkeypatch.setattr(harness, "lifetime_s", 2)
    monkeypatch.setattr(harness, "timeout_s", 4)
    result = harness.timeout_expiry()
    error = "plain, noterm, setsid exited on their own during the scenario; results not valid"
    assert_error_row(result, error, harness, capsys)


@needs_posix
def test_the_final_cleanup_covers_a_run_that_failed_right_after_it_started(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_watcher(popen: Any) -> Any:
        raise RuntimeError("simulated: the run cannot be watched")

    monkeypatch.setattr(kill_check, "LocalExitWatcher", broken_watcher)
    with pytest.raises(RuntimeError, match="simulated"):
        harness.timeout_expiry()
    # The run is going, and the scenario never got hold of it; only what the harness registered before start() knows.
    everyone = {f"timeout_expiry.{role}" for role in kill_check.LOCAL_ROLES}
    assert wait_for(lambda: set((kill_check.owned_processes(harness.started) or {}).values()) == everyone)
    final = harness.close()
    assert {p["process"] for p in final["running"]} == everyone
    assert final["verified"] is True
    assert kill_check.owned_processes(harness.started) == {}
    assert wait_for(lambda: mentioning(harness.marker) == {})


@needs_posix
def test_ctrl_c_while_an_agent_starts_cannot_lose_its_pid(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    started: list[int] = []

    def popen_then_ctrl_c(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        if not started:
            started.append(process.pid)
            signal.raise_signal(signal.SIGINT)  # Ctrl-C right after the fork, while Popen() has not returned
        return process

    previous = signal.signal(signal.SIGINT, kill_check.StopRequest())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", popen_then_ctrl_c)
            with pytest.raises(KeyboardInterrupt):
                harness.timeout_expiry()
    finally:
        signal.signal(signal.SIGINT, previous)
    (agent,) = started
    (run,) = harness.started
    assert run.pids == {agent: "timeout_expiry.agent"}  # noted before the interrupt was let through
    final = harness.close()
    assert "timeout_expiry.agent" in {p["process"] for p in final["running"]}
    assert final["verified"] is True
    assert wait_for(lambda: mentioning(harness.marker) == {})


@needs_posix
def test_the_final_cleanup_knows_an_agent_whose_start_failed_after_popen(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.dispatcher.runtime import local_process

    class NoThreads:
        """LocalProcess.start() gets as far as Popen, then cannot start its output readers."""

        Lock = threading.Lock

        class Thread:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("simulated: can't start new thread")

    monkeypatch.setattr(local_process, "threading", NoThreads)
    with pytest.raises(RuntimeError, match="simulated"):
        harness.timeout_expiry()
    (run,) = harness.started
    assert run.agent is not None  # noted although start() raised
    assert run.pids[run.agent.pid] == "timeout_expiry.agent"
    everyone = {f"timeout_expiry.{role}" for role in kill_check.LOCAL_ROLES}
    assert wait_for(lambda: set((kill_check.owned_processes(harness.started) or {}).values()) == everyone)
    final = harness.close()
    assert {p["process"] for p in final["running"]} == everyone
    assert final["verified"] is True
    assert wait_for(lambda: mentioning(harness.marker) == {})


def signal_own(run: kill_check.Started, role: str, sig: int) -> None:
    """Sends `sig` to this run's `role` process, as something outside the tool might: only to the PID the tool learned,
    checked to be that process right before, and through a pidfd where there are pidfds (as kill_if_owned does)."""
    pid = next(pid for pid, label in run.pids.items() if label == f"{run.name}.{role}")
    pidfd = kill_check._open_pidfd(pid)
    try:
        assert kill_check.owned_label_of(pid, kill_check.command_line(pid) or "", [run]) == f"{run.name}.{role}"
        if pidfd is None:
            os.kill(pid, sig)
        else:
            signal.pidfd_send_signal(pidfd, sig)
    finally:
        if pidfd is not None:
            os.close(pidfd)


OUTSIDE_STOPS = {
    "oom-kill": {"noterm": signal.SIGKILL},  # the OOM killer, or a kill -9, takes one test child
    "pkill": dict.fromkeys(kill_check.LOCAL_ROLES, signal.SIGTERM),  # `pkill -f cairn-killcheck-local-` elsewhere
}
STOPPED_FROM_OUTSIDE = [
    *(pytest.param(name, "oom-kill", id=f"{name}-oom-kill") for name in kill_check.LOCAL_SCENARIOS),
    # Not timeout_expiry_stubborn: its agent's `trap '' TERM` is inherited by its children, so a pkill stops nothing.
    *(pytest.param(name, "pkill", id=f"{name}-pkill") for name in ("timeout_expiry", "local_kill", "cancel")),
]


@needs_posix
@pytest.mark.parametrize(("scenario", "how"), STOPPED_FROM_OUTSIDE)
def test_a_test_process_stopped_from_outside_before_the_kill_path_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    how: str,
) -> None:
    # After the children were ready and before the path ran (the timeout is 1 s here). Counted as stopped by the path,
    # that gave a wrong answer with exit 0: a child that died by a signal leaves no exited-<role> file.
    signals = OUTSIDE_STOPS[how]
    gone = [role for role, sig in signals.items() if sig == signal.SIGKILL or role != "noterm"]
    wait_for_children = harness.wait_for_children

    def then_stop_from_outside(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
        outcome = wait_for_children(run)
        for role, sig in signals.items():
            signal_own(run.started, role, sig)
        assert wait_for(lambda: not set(gone) & set(harness.running_roles(run)))
        return outcome

    monkeypatch.setattr(harness, "wait_for_children", then_stop_from_outside)
    result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    assert_error_row(result, f"{', '.join(gone)} {kill_check.STOPPED_BEFORE}; results not valid", harness, capsys)


@needs_posix
def test_a_child_stopped_from_outside_while_the_others_start_is_named_at_once(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # noterm said it was ready, then was killed while setsid was still setting itself up. That gave "noterm did not
    # start within 15 s", after waiting all that time for a child that had started.
    ready = "open('ready-' + role, 'w').close()\n"
    source = kill_check.LOCAL_CHILD_SOURCE
    assert source.count(ready) == 1
    slow_setsid = source.replace(ready, f"time.sleep(1.5 if role == 'setsid' else 0)\n{ready}")
    monkeypatch.setattr(kill_check, "LOCAL_CHILD_CODE", kill_check.child_code(slow_setsid))
    monkeypatch.setattr(kill_check, "READY_WAIT_S", 4.0)
    running_roles = harness.running_roles
    killed: list[str] = []

    def kill_noterm_once_ready(run: kill_check.LocalRun) -> list[str]:
        if not killed and (run.workdir / "ready-noterm").exists():
            run.started.learn()  # its pid file came before its ready file
            signal_own(run.started, "noterm", signal.SIGKILL)
            killed.append("noterm")
            assert wait_for(lambda: "noterm" not in running_roles(run))
        return running_roles(run)

    monkeypatch.setattr(harness, "running_roles", kill_noterm_once_ready)
    started = time.monotonic()
    result = harness.cancel()
    assert killed == ["noterm"]
    assert time.monotonic() - started < 3.5  # it did not wait for READY_WAIT_S
    assert_error_row(result, f"noterm {kill_check.STOPPED_BEFORE}; results not valid", harness, capsys)


@needs_posix
@pytest.mark.parametrize("scenario", list(kill_check.LOCAL_SCENARIOS))
def test_a_survivor_stopped_from_outside_after_the_kill_path_gives_no_results(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scenario: str
) -> None:
    # Once the path has run, LocalProcess signals nothing more, while communicate() can wait 10 s more for the output
    # pipes that survivors hold. A survivor killed from outside then would be missing from the survivors.
    stopped: list[int] = []
    join = kill_check.Communicator.join

    def stop_survivors_then_join(self: kill_check.Communicator, timeout: float) -> bool:
        if not stopped:
            for run in harness.started:
                for pid, label in list(run.pids.items()):
                    if label.endswith((".noterm", ".setsid")) and kill_check.kill_if_owned(pid, [run]):
                        stopped.append(pid)
        return join(self, timeout)

    monkeypatch.setattr(kill_check.Communicator, "join", stop_survivors_then_join)
    result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    assert stopped
    lost = "setsid" if scenario == "timeout_expiry_stubborn" else "noterm, setsid"  # the stubborn run's noterm died
    assert_error_row(result, f"{lost} {kill_check.STOPPED_AFTER}; results not valid", harness, capsys)


def strike_at(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    moment: Any,
    roles: list[str],
    sig: int = signal.SIGKILL,
) -> list[str]:
    """Once a run's test processes have all started, sends `sig` to its `roles` (see signal_own) at the time.monotonic()
    that `moment(run)` gives, as soon as it gives one, as something outside the tool might. Returns the roles struck."""
    struck: list[str] = []
    wait_for_children = harness.wait_for_children

    def then_strike(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
        outcome = wait_for_children(run)

        def strike() -> None:
            give_up = time.monotonic() + 60
            while (at := moment(run)) is None:
                if time.monotonic() > give_up:
                    return
                time.sleep(0.005)
            time.sleep(max(0.0, at - time.monotonic()))
            for role in roles:
                signal_own(run.started, role, sig)
                struck.append(role)

        threading.Thread(target=strike, daemon=True).start()
        return outcome

    monkeypatch.setattr(harness, "wait_for_children", then_strike)
    return struck


def after_the_agent_exits(delay_s: float) -> Any:
    return lambda run: None if run.watcher.exited_at is None else run.watcher.exited_at + delay_s


def after_the_first_signal(delay_s: float) -> Any:
    return lambda run: run.sent[0].at + delay_s if run.sent else None


def before_the_timeout(timeout_s: float, delay_s: float) -> Any:
    return lambda run: None if run.waiter is None else run.waiter.started_at + timeout_s - delay_s


@needs_posix
@pytest.mark.parametrize(
    ("scenario", "role"),
    [
        ("timeout_expiry", "setsid"),
        ("timeout_expiry", "noterm"),
        ("timeout_expiry_stubborn", "setsid"),  # its noterm died with the agent, by the SIGKILL to their group
        ("local_kill", "setsid"),
        ("local_kill", "noterm"),
        ("cancel", "setsid"),
        ("cancel", "noterm"),
    ],
)
def test_a_survivor_stopped_from_outside_right_after_the_kill_path_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    role: str,
) -> None:
    # 0.15 s after the agent exited, before the tool lists what the path left running. That passed for the path's work:
    # a wrong answer with exit 0. No signal of the path explains it: setsid is in a group of its own, and noterm
    # ignores the SIGTERM that went to its group.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    struck = strike_at(harness, monkeypatch, after_the_agent_exits(0.15), [role])
    result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    assert struck == [role]
    assert_error_row(result, f"{role} {kill_check.STOPPED_OTHERWISE}; results not valid", harness, capsys)


@needs_posix
@pytest.mark.parametrize(
    ("role", "delay_s"),
    [("setsid", 1.0), ("agent", 1.0), ("plain", 1.0), ("noterm", 1.0), ("plain", 0.15)],
    ids=["setsid", "agent", "plain", "noterm", "plain-at-once"],
)
def test_a_test_process_stopped_from_outside_during_the_grace_period_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
    delay_s: float,
) -> None:
    # timeout_expiry_stubborn with a 2 s grace: its SIGTERM ends nothing, and its SIGKILL comes 2 s later. A kill from
    # outside in between passed for the path's work. Once the agent is gone, LocalProcess sends no SIGKILL at all,
    # and plain and noterm then outlive the run.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    monkeypatch.setattr(harness, "kill_after_s", 2)
    monkeypatch.setattr(harness, "term_grace_s", 2)
    struck = strike_at(harness, monkeypatch, after_the_first_signal(delay_s), [role])
    result = harness.timeout_expiry(ignore_term=True)
    assert struck == [role]
    assert_error_row(result, f"{role} {kill_check.STOPPED_OTHERWISE}; results not valid", harness, capsys)


@needs_posix
@pytest.mark.parametrize(("role", "delay_s"), [("setsid", 0.6), ("noterm", 0.6), ("setsid", 0.4)])
def test_a_test_process_stopped_from_outside_shortly_before_the_timeout_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
    delay_s: float,
) -> None:
    # Shortly before the timeout could fire: what the timeout met is what the last listing to end before then showed.
    monkeypatch.setattr(harness, "timeout_s", 2)
    struck = strike_at(harness, monkeypatch, before_the_timeout(2, delay_s), [role])
    result = harness.timeout_expiry()
    assert struck == [role]
    assert_error_row(result, f"{role} {kill_check.STOPPED_BEFORE}; results not valid", harness, capsys)


@needs_posix
def test_a_survivor_stopped_from_outside_after_it_was_listed_as_left_running_is_named_so(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A second after the agent exited: half a second after it, the tool listed setsid among what the path left running.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    struck = strike_at(harness, monkeypatch, after_the_agent_exits(1.0), ["setsid"])
    result = harness.local_kill()
    assert struck == ["setsid"]
    assert_error_row(result, f"setsid {kill_check.STOPPED_AFTER}; results not valid", harness, capsys)


@needs_posix
def test_an_agent_killed_from_outside_as_the_kill_path_signals_it_gives_no_results(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The agent is SIGKILLed from outside in the instant between LocalProcess's check that it still runs and its own
    # SIGTERM. That SIGTERM went to the agent's group in time, but the agent's exit status (-9) shows that it did not
    # end the agent.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    send = local_process.LocalProcess._signal_group
    struck: list[str] = []

    def killed_from_outside_first(popen: subprocess.Popen[str], sig: int) -> None:
        if not struck:
            (run,) = harness.started
            signal_own(run, "agent", signal.SIGKILL)
            struck.append("agent")
            assert wait_for(lambda: kill_check.command_line(popen.pid) is None)  # dead before the SIGTERM goes out
        send(popen, sig)

    monkeypatch.setattr(local_process.LocalProcess, "_signal_group", staticmethod(killed_from_outside_first))
    result = harness.timeout_expiry()
    assert struck == ["agent"]
    assert_error_row(result, f"agent {kill_check.STOPPED_OTHERWISE}; results not valid", harness, capsys)


@needs_posix
@pytest.mark.parametrize(
    ("scenario", "sig", "survivors"),
    [
        ("timeout_expiry", signal.SIGTERM, ["plain"]),
        ("local_kill", signal.SIGTERM, ["plain"]),
        ("cancel", signal.SIGTERM, ["plain"]),
        ("timeout_expiry_stubborn", signal.SIGKILL, ["plain", "noterm"]),
    ],
)
def test_a_group_signal_noted_but_never_delivered_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    sig: int,
    survivors: list[str],
) -> None:
    # The agent is ended from outside, with the very signal the path is about to send, and reaped, in the instant
    # between record_signals noting that signal for its group and LocalProcess sending it. LocalProcess then finds no
    # group and sends nothing, so the rest of the group never gets the path's signal: counted as survivors of the
    # path, they would be a wrong answer.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    send = local_process.LocalProcess._signal_group
    struck: list[str] = []

    def ended_and_reaped_first(popen: subprocess.Popen[str], s: int) -> None:
        if not struck and s == sig:  # the stubborn run's SIGTERM, which its agent ignores, goes out as usual
            (run,) = harness.started
            signal_own(run, "agent", sig)
            struck.append("agent")
            assert wait_for(lambda: popen.returncode is not None)  # reaped by the tool's exit watcher meanwhile
        send(popen, s)

    monkeypatch.setattr(local_process.LocalProcess, "_signal_group", staticmethod(ended_and_reaped_first))
    result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    assert struck == ["agent"]
    assert_error_row(result, f"{kill_check.outlived_problem(survivors)}; results not valid", harness, capsys)


@needs_posix
@pytest.mark.parametrize(
    ("scenario", "how"),
    [("timeout_expiry", "hangs"), ("local_kill", "hangs"), ("cancel", "hangs"), ("local_kill", "raises")],
)
def test_a_communicate_that_does_not_return_gives_no_results(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    how: str,
) -> None:
    # As a regression in LocalProcess would make it: in the dispatcher the task thread would hang. That must be an
    # error, never a row without results in a run that exits 0.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    monkeypatch.setattr(harness, "budget_s", lambda timeout_s: timeout_s + 3)  # likewise
    real = local_process.LocalProcess.communicate
    release = threading.Event()

    def broken(self: Any, timeout: float | None) -> Any:
        result = real(self, timeout)
        if how == "raises":
            raise RuntimeError("simulated")
        release.wait(60)
        return result

    monkeypatch.setattr(local_process.LocalProcess, "communicate", broken)
    try:
        result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    finally:
        release.set()
    if how == "raises":
        error = "communicate() raised RuntimeError: simulated"
    else:
        budget, after = {
            "timeout_expiry": (harness.timeout_s + 3, "it was called"),
            "local_kill": (3, "kill()"),
            "cancel": (3, "the cancel"),
        }[scenario]
        error = f"communicate() had not returned {budget:g} s after {after}, so the task thread would hang"
    assert_error_row(result, f"{error}; results not valid", harness, capsys)


@needs_posix
def test_record_signals_notes_each_signal_local_process_sends_and_still_sends_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reason = _cannot_run_local_mode()
    if reason:
        pytest.skip(reason)
    monkeypatch.syspath_prepend(str(kill_check.CAIRN_SRC))
    from cairn.dispatcher.runtime.local_process import LocalProcess

    sleeper = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
    process = LocalProcess(sleeper, cwd=str(tmp_path), env=dict(os.environ), timeout_seconds=60, term_grace_seconds=1)
    sent: list[kill_check.Sent] = []
    kill_check.record_signals(process, sent)
    process.start()
    popen = process._process
    assert popen is not None
    try:
        before = time.monotonic()
        process.kill()  # SIGTERM to its process group, which ends it
        assert popen.wait(timeout=10) == -signal.SIGTERM
        # its own group (a new session); not reaped when the send returned, since the SIGTERM had only just gone out
        assert sent == [kill_check.Sent(sent[0].at, signal.SIGTERM, popen.pid, reaped=False)]
        assert before <= sent[0].at <= time.monotonic()
        # Once it is gone and reaped, a signal goes to no group, and LocalProcess sends nothing.
        process._signal_group(popen, signal.SIGKILL)
        assert sent[1:] == [kill_check.Sent(sent[1].at, signal.SIGKILL, None, reaped=True)]
        # Only this instance notes what it sends.
        assert LocalProcess(sleeper, cwd=str(tmp_path), env={})._signal_group is LocalProcess._signal_group
    finally:
        if popen.poll() is None:
            popen.kill()
        process.communicate(timeout=10)


@needs_posix
def test_the_process_groups_of_the_processes_the_tool_knows_are_noted(tmp_path: Path) -> None:
    leader = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], start_new_session=True)
    gone = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    gone.wait()  # and reaped
    pids = {leader.pid: "cancel.setsid", os.getpid(): "cancel.agent", gone.pid: "cancel.plain"}
    try:
        groups = kill_check.process_groups(kill_check.Started("cancel", tmp_path, {}, pids))
        assert groups == {"setsid": leader.pid, "agent": os.getpgrp(), "plain": None}
    finally:
        stop(leader)


@needs_posix
@pytest.mark.parametrize(
    ("scenario", "survivors"),
    [
        ("timeout_expiry", ["noterm", "setsid"]),
        ("timeout_expiry_stubborn", ["setsid"]),  # SIGTERM does nothing there; plain dies by the SIGKILL to the group
        ("local_kill", ["noterm", "setsid"]),
        ("cancel", ["noterm", "setsid"]),
    ],
)
def test_each_scenario_gives_results_when_nothing_else_stops_its_processes(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, scenario: str, survivors: list[str]
) -> None:
    # The checks against outside stops must not fire on an undisturbed run (test_real_local_run checks this too).
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    assert "error" not in result, result
    assert result["started"] == list(kill_check.LOCAL_ROLES)
    assert result["survivors"] == survivors
    assert result["cleanup"]["verified"] is True
    assert sorted(p["process"] for p in result["cleanup"]["running"]) == sorted(f"{scenario}.{r}" for r in survivors)


@needs_posix
def test_an_agent_gone_right_after_the_last_listing_before_the_timeout_gives_no_results(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait_for_children = harness.wait_for_children

    def then_kill_the_agent(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
        outcome = wait_for_children(run)
        signal_own(run.started, "agent", signal.SIGKILL)
        assert wait_for(lambda: run.watcher.exited_at is not None)
        # Every listing until the timeout as if taken in the instant before the agent died.
        monkeypatch.setattr(harness, "running_roles", lambda run: list(kill_check.LOCAL_ROLES))
        return outcome

    monkeypatch.setattr(harness, "wait_for_children", then_kill_the_agent)
    result = harness.timeout_expiry()
    assert_error_row(result, f"agent {kill_check.STOPPED_BEFORE}; results not valid", harness, capsys)


@needs_posix
def test_a_timeout_scenario_whose_run_did_not_time_out_gives_no_results(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The backstop for an agent that died in the instant before the timeout and was noticed only after it: the timeout
    # never fired, and communicate() says so with timed_out=False. Here it says so outright.
    from cairn.dispatcher.runtime import local_process

    communicate = local_process.LocalProcess.communicate

    def not_timed_out(self: Any, timeout: float | None) -> Any:
        return dataclasses.replace(communicate(self, timeout), timed_out=False)

    monkeypatch.setattr(local_process.LocalProcess, "communicate", not_timed_out)
    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    result = harness.timeout_expiry()
    assert_error_row(result, f"{kill_check.NOT_TIMED_OUT}; results not valid", harness, capsys)


def slow_start(delay_s: float) -> str:
    """The test child's code, delayed by `delay_s` before it writes its pid file, as on a heavily loaded machine."""
    first = "import os, signal, sys, time\n"
    source = kill_check.LOCAL_CHILD_SOURCE
    assert source.startswith(first)
    return kill_check.child_code(f"{first}time.sleep({delay_s})\n{source[len(first) :]}")


def ctrl_c_right_away(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
    raise KeyboardInterrupt  # right after the agent started, before any child has written its pid file


@needs_posix
@pytest.mark.parametrize(("delay_s", "ready_wait_s"), [(1.5, None), (4.0, 0.5)], ids=["start-in-time", "too-slow"])
def test_a_cleanup_never_confirms_while_a_test_child_may_still_be_starting(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, delay_s: float, ready_wait_s: float | None
) -> None:
    # The cleanup knows the children only by the PIDs they write; killing the agent then left them running unknown,
    # and the tool said that nothing was running.
    monkeypatch.setattr(kill_check, "LOCAL_CHILD_CODE", slow_start(delay_s))
    if ready_wait_s is not None:
        monkeypatch.setattr(kill_check, "READY_WAIT_S", ready_wait_s)
    monkeypatch.setattr(harness, "wait_for_children", ctrl_c_right_away)
    with pytest.raises(KeyboardInterrupt):
        harness.timeout_expiry()
    final = harness.close()
    alive = mentioning(harness.marker)
    assert not (final["verified"] and alive)
    (run,) = harness.started
    if ready_wait_s is None:
        # They started within READY_WAIT_S: the cleanup waited for them, they stopped when asked, and the agent ended
        # by itself once they had.
        assert final["verified"] is True
        assert alive == {}
        assert run.agent is not None and run.agent.poll() == 0
    else:
        # They did not: the agent had to be killed, and the summary says what it cannot confirm.
        everyone = [f"timeout_expiry.{role}" for role in kill_check.LOCAL_CHILD_ROLES]
        assert final["verified"] is False
        assert final["unknown"] == everyone
        assert sorted(alive) and all(f"{harness.marker}.timeout_expiry." in cmdline for cmdline in alive.values())
        text = "\n".join(kill_check.describe_cleanup(final, harness.marker, harness.scenario_cleanup()))
        assert f"WARNING: cannot confirm that {', '.join(everyone)} stopped" in text
        assert kill_check.pgrep_command(harness.marker) in text
        assert wait_for(lambda: mentioning(harness.marker) == {}, timeout=delay_s + 5)  # they end once started


def stuck_noterm_child() -> str:
    """The test child's code, with a noterm child that never looks at the stop file: a cleanup has to kill it."""
    check = "    if os.path.exists(os.path.join(here, 'stop')) or not os.path.isdir(here):\n"
    stuck = "    if (role != 'noterm' and os.path.exists(os.path.join(here, 'stop'))) or not os.path.isdir(here):\n"
    source = kill_check.LOCAL_CHILD_SOURCE
    assert source.count(check) == 1
    return kill_check.child_code(source.replace(check, stuck))


@needs_posix
@pytest.mark.parametrize("when", ["before the sweep", "right after a kill"])
def test_ctrl_c_during_a_scenarios_cleanup_waits_until_it_has_recorded_what_it_found(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    # Interrupted, that cleanup ran again from the start, found nothing still running, and the summary said that no
    # test process had been running, or lost a kill it had just made.
    from cairn.dispatcher.runtime import local_process

    # Only to save time: communicate() waits this long for each output pipe that the survivors hold.
    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)
    interrupted: list[str] = []

    def ctrl_c_once() -> None:
        if not interrupted:
            interrupted.append(when)
            signal.raise_signal(signal.SIGINT)

    if when == "before the sweep":  # by then noterm and setsid, the survivors, have exited when asked
        sweep = kill_check.sweep

        def ctrl_c_then_sweep(*args: Any, **kwargs: Any) -> dict[str, Any]:
            ctrl_c_once()
            return sweep(*args, **kwargs)

        monkeypatch.setattr(kill_check, "sweep", ctrl_c_then_sweep)
        expected = {"running": 2, "exited": 2, "killed": 0}
    else:
        monkeypatch.setattr(kill_check, "LOCAL_CHILD_CODE", stuck_noterm_child())
        kill_if_owned = kill_check.kill_if_owned

        def kill_then_ctrl_c(pid: int, runs: Any) -> bool:
            done = kill_if_owned(pid, runs)
            if done:
                ctrl_c_once()
            return done

        monkeypatch.setattr(kill_check, "kill_if_owned", kill_then_ctrl_c)
        expected = {"running": 2, "exited": 1, "killed": 1}
    previous = signal.signal(signal.SIGINT, kill_check.StopRequest())
    try:
        with pytest.raises(KeyboardInterrupt):
            harness.timeout_expiry()
    finally:
        signal.signal(signal.SIGINT, previous)
    assert interrupted == [when]
    assert harness.scenario_cleanup() == expected
    (run,) = harness._runs
    assert run.cleanup is not None and run.cleanup["verified"] is True


def test_only_the_first_stop_signal_interrupts() -> None:
    stop = kill_check.StopRequest()
    with pytest.raises(KeyboardInterrupt):
        stop(signal.SIGINT, None)
    try:
        stop(signal.SIGTERM, None)  # noted only: nothing can cut short the cleanup that the first one started
        stop(signal.SIGINT, None)
    except KeyboardInterrupt:
        pytest.fail("a stop signal after the first one raised KeyboardInterrupt")
    assert stop.received == [signal.SIGINT, signal.SIGTERM, signal.SIGINT]


@needs_posix
def test_interrupts_held_delivers_them_after_the_block_to_the_handlers_from_before() -> None:
    stop = kill_check.StopRequest()
    before = {signum: signal.signal(signum, stop) for signum in kill_check.STOP_SIGNALS}
    reached: list[str] = []
    try:
        with pytest.raises(KeyboardInterrupt), kill_check.interrupts_held():
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
            reached.append("the end of the block")
        assert reached == ["the end of the block"]
        assert stop.received == [signal.SIGTERM]  # the first one stops the run; the rest would only be noted
        assert all(signal.getsignal(signum) is stop for signum in kill_check.STOP_SIGNALS)
        with kill_check.interrupts_held():  # nothing held, nothing delivered
            pass
        assert stop.received == [signal.SIGTERM]
    finally:
        for signum, handler in before.items():
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


class InterruptedCleanupHarness:
    """Stands in for LocalHarness: its close() is interrupted once, as by a second Ctrl-C that comes right before
    the cleanup could ignore it."""

    instances: ClassVar[list[InterruptedCleanupHarness]] = []

    def __init__(self, timeout_s: int, kill_after_s: int, marker: str) -> None:
        self.marker = marker
        self.closes = 0
        self.handlers_during_close: dict[str, Any] = {}
        InterruptedCleanupHarness.instances.append(self)

    def close(self) -> dict[str, Any]:
        self.closes += 1
        if self.closes == 1:
            raise KeyboardInterrupt
        self.handlers_during_close = {name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS}
        return {"running": [], "exited": [], "killed": [], "left": [], "verified": True}

    def scenario_cleanup(self) -> dict[str, int]:
        return {"running": 0, "exited": 0, "killed": 0}


def ctrl_c_during_the_scenario(harness: Any) -> dict[str, Any]:
    raise KeyboardInterrupt


@needs_posix
@pytest.mark.parametrize("restore_signals", [True, False], ids=["in-process", "command-line"])
def test_ctrl_c_during_the_cleanup_starts_it_again_and_the_summary_still_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, restore_signals: bool
) -> None:
    before = {name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS}
    during_summary: dict[str, Any] = {}
    describe = kill_check.describe_cleanup

    def describe_and_note_the_handlers(*args: Any) -> list[str]:
        during_summary.update({name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS})
        return describe(*args)

    InterruptedCleanupHarness.instances.clear()
    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(kill_check, "LocalHarness", InterruptedCleanupHarness)
    monkeypatch.setitem(kill_check.LOCAL_SCENARIOS, "cancel", ctrl_c_during_the_scenario)
    monkeypatch.setattr(kill_check, "describe_cleanup", describe_and_note_the_handlers)
    argv = ["--mode", "local", "--scenario", "cancel", "--json-out", str(tmp_path / "out.json")]
    try:
        code = kill_check.main(argv, restore_signals=restore_signals)
        after = {name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS}
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), signal.SIG_DFL if handler is None else handler)
    out = capsys.readouterr().out
    assert code == 130
    (harness,) = InterruptedCleanupHarness.instances
    assert harness.closes == 2
    assert harness.handlers_during_close == dict.fromkeys(STOP_SIGNALS, signal.SIG_IGN)
    assert during_summary == dict.fromkeys(STOP_SIGNALS, signal.SIG_IGN)  # still ignored while the summary prints
    assert "interrupted; nothing written" in out
    assert f"nothing with marker {harness.marker} is running now" in out
    assert not (tmp_path / "out.json").exists()
    # Given back to a caller in this process; on the command line they stay ignored until the process exits.
    assert after == (before if restore_signals else dict.fromkeys(STOP_SIGNALS, signal.SIG_IGN))


def main_in_process(argv: list[str]) -> int:
    """kill_check.main(argv) in this process, with the signal handlers put back whatever happens. A Ctrl-C that
    escapes main() fails the test (rather than stopping the whole test session, as pytest would)."""
    before = {name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS}
    try:
        return kill_check.main(argv)
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped main(): the tool would end with a traceback")
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), signal.SIG_DFL if handler is None else handler)


class FakeHarness:
    """Stands in for LocalHarness, with a final cleanup that is confirmed or not."""

    verified = True

    def __init__(self, timeout_s: int, kill_after_s: int, marker: str) -> None:
        self.term_grace_s, self.lifetime_s = 1, 1

    def close(self) -> dict[str, Any]:
        left = [] if self.verified else None
        return {"running": [], "exited": [], "killed": [], "left": left, "unknown": [], "verified": self.verified}

    def scenario_cleanup(self) -> dict[str, int]:
        return {"running": 0, "exited": 0, "killed": 0}


CANCEL_RESULT = {
    "returncode": -15,
    "timed_out": False,
    "cancelled": True,
    "cancel_reason": kill_check.CANCEL_REASON,
    "did_timeout": False,
    "survivors": ["setsid"],
}


@pytest.mark.parametrize(("scenario_error", "verified", "code"), [(False, True, 0), (True, True, 1), (False, False, 1)])
def test_the_exit_code_says_whether_the_results_can_be_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario_error: bool, verified: bool, code: int
) -> None:
    # 0 only when every scenario gave results and the cleanup is confirmed.
    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(FakeHarness, "verified", verified)
    monkeypatch.setattr(kill_check, "LocalHarness", FakeHarness)
    result = {"error": "setsid did not start within 15 s; results not valid"} if scenario_error else CANCEL_RESULT
    monkeypatch.setitem(kill_check.LOCAL_SCENARIOS, "cancel", lambda harness: dict(result))
    out = tmp_path / "out.json"
    assert main_in_process(["--mode", "local", "--scenario", "cancel", "--json-out", str(out)]) == code
    assert json.loads(out.read_text(encoding="utf-8"))["cleanup"]["verified"] is verified


def nothing_may_start(*args: Any) -> Any:
    pytest.fail("the run started")


@pytest.mark.parametrize("where", ["in a missing directory", "a directory"])
def test_local_mode_refuses_a_json_out_it_cannot_write_before_it_starts_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, where: str
) -> None:
    # The results are written last: a run of a minute or more ended in a traceback, its results lost.
    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(kill_check, "LocalHarness", nothing_may_start)
    target = tmp_path / "missing" / "out.json" if where == "in a missing directory" else tmp_path
    with pytest.raises(SystemExit) as refused:
        main_in_process(["--mode", "local", "--scenario", "cancel", "--json-out", str(target)])
    assert refused.value.code == 2
    assert "--json-out" in capsys.readouterr().err
    # Container mode is as it was: it checks no such thing.
    assert kill_check.main(["--json-out", str(target), "--dry-run"]) == 0


def test_results_that_still_cannot_be_written_end_the_run_with_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def cancel_and_remove_the_directory(harness: Any) -> dict[str, Any]:
        out_dir.rmdir()  # gone while the run went on
        return dict(CANCEL_RESULT)

    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(kill_check, "LocalHarness", FakeHarness)
    monkeypatch.setitem(kill_check.LOCAL_SCENARIOS, "cancel", cancel_and_remove_the_directory)
    assert main_in_process(["--mode", "local", "--scenario", "cancel", "--json-out", str(out_dir / "out.json")]) == 1
    captured = capsys.readouterr()
    assert f"could not write {out_dir / 'out.json'}" in captured.err
    assert re.search(r"^cancel +-15  cancelled", captured.out, re.M)  # the table still printed
    assert "Traceback" not in captured.err


def test_ctrl_c_before_anything_started_ends_the_run_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    handlers: dict[str, Any] = {}

    def ctrl_c_while_importing(*args: Any) -> Any:
        handlers.update({name: signal.getsignal(getattr(signal, name)) for name in STOP_SIGNALS})
        raise KeyboardInterrupt  # as a Ctrl-C while LocalHarness imports the dispatcher code

    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(kill_check, "LocalHarness", ctrl_c_while_importing)
    assert main_in_process(["--mode", "local", "--json-out", str(tmp_path / "out.json")]) == 130
    # Already in place then: SIGTERM or SIGHUP there would otherwise end the tool at once, without its summary.
    assert all(isinstance(handler, kill_check.StopRequest) for handler in handlers.values()), handlers
    out = capsys.readouterr().out
    assert "interrupted; nothing written" in out
    assert re.search(
        r"^Cleanup: no test process was still running at the end; nothing with marker \S+ is running", out, re.M
    )
    assert list(tmp_path.iterdir()) == []


@needs_posix
def test_ctrl_c_right_after_the_workspace_is_created_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    reason = _cannot_run_local_mode()
    if reason:
        pytest.skip(reason)
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp_then_ctrl_c(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        signal.raise_signal(signal.SIGINT)  # the moment it exists
        return path

    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_then_ctrl_c)
    assert main_in_process(["--mode", "local", "--scenario", "cancel", "--json-out", str(tmp_path / "out.json")]) == 130
    out = capsys.readouterr().out
    assert "interrupted; nothing written" in out
    assert re.search(r"nothing with marker \S+ is running now", out)
    assert list(tmp_path.iterdir()) == []  # no workspace left, and no results


def run_processes(marker: str, tool_pid: int, timeout_s: int = 4, kill_after_s: int = 2) -> dict[int, str]:
    """PID -> `<scenario>.<role>` of the processes whose command lines are those of a tool run with this marker and
    these settings. For checks only: a command line alone does not make a process the tool's, and the tests never
    signal on it."""
    lifetime_s = kill_check.child_lifetime_s(timeout_s, kill_after_s)
    tails: dict[str, str] = {}
    for name in kill_check.LOCAL_SCENARIOS:
        for ignore_term in (False, True):
            tails.update(tails_for(marker, name, ignore_term=ignore_term, tool_pid=tool_pid, lifetime_s=lifetime_s))
    listing = kill_check.all_processes() or {}
    return {pid: label for pid, cmdline in listing.items() if (label := kill_check.owned_label(cmdline, tails))}


def start_local_run(tmp_path: Path, *extra: str) -> tuple[subprocess.Popen[str], str]:
    """The tool, run as on the command line, with its temporary directory in `tmp_path / "tmp"`."""
    reason = _cannot_run_local_mode()
    if reason:
        pytest.skip(reason)
    (tmp_path / "tmp").mkdir()
    tool = subprocess.Popen(
        [sys.executable, str(TOOL_DIR / "kill_check.py"), "--mode", "local", *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path / "tmp")},
    )
    assert tool.stdout is not None
    first = tool.stdout.readline()
    found = re.match(r"marker: (\S+)", first)
    if not found:
        tool.kill()
        pytest.fail(f"no marker line: {first!r} {tool.communicate()}")
    return tool, found.group(1)


def end_local_run(tool: subprocess.Popen[str], marker: str, decoys: list[subprocess.Popen[bytes]]) -> None:
    """Makes sure the tool has exited, and waits for its test processes to go, which they do by themselves once
    it has (the tests signal only processes they started themselves)."""
    if tool.poll() is None:
        tool.kill()
        tool.communicate()
    wait_for(lambda: set(mentioning(marker)) <= {decoy.pid for decoy in decoys})


@needs_posix
@pytest.mark.parametrize(
    ("signum", "count"),
    [(signal.SIGINT, 1), (signal.SIGTERM, 1), (signal.SIGINT, 6)],
    ids=["ctrl-c", "sigterm", "ctrl-c-burst"],
)
def test_an_interrupted_run_leaves_nothing_behind(tmp_path: Path, signum: int, count: int) -> None:
    tool, marker = start_local_run(tmp_path, "--scenario", "timeout_expiry_stubborn", "--json-out", "out.json")
    everyone = {f"timeout_expiry_stubborn.{role}" for role in kill_check.LOCAL_ROLES}
    try:
        assert wait_for(lambda: set(run_processes(marker, tool.pid).values()) >= everyone, timeout=20)
        for _ in range(count):  # Ctrl-C sends SIGINT to the tool; its test processes are in other sessions
            if tool.poll() is None:
                tool.send_signal(signum)
                time.sleep(0.03)
        out, err = tool.communicate(timeout=60)
        assert tool.returncode == 130, out + err
        assert "interrupted; nothing written" in out
        assert f"nothing with marker {marker} is running now" in out
        assert "Traceback" not in err, err
        assert mentioning(marker) == {}
        assert not (tmp_path / "out.json").exists()
        assert list((tmp_path / "tmp").iterdir()) == []  # its temporary directory is gone too
    finally:
        end_local_run(tool, marker, [])


@needs_posix
def test_after_kill_9_of_the_tool_its_test_processes_end_by_themselves(tmp_path: Path) -> None:
    tool, marker = start_local_run(tmp_path, "--scenario", "local_kill", "--json-out", "out.json")
    everyone = {f"local_kill.{role}" for role in kill_check.LOCAL_ROLES}
    try:
        assert wait_for(lambda: set(run_processes(marker, tool.pid).values()) >= everyone, timeout=20)
        tool.kill()  # no cleanup at all
        tool.communicate()  # reaped, as a shell reaps a command it ran
        assert wait_for(lambda: mentioning(marker) == {}, timeout=kill_check.STOP_WAIT_S + 3)
        # Only a fatal signal leaves the workspace behind (its few small files).
        assert [path.name.startswith("cairn-phase0-local-") for path in (tmp_path / "tmp").iterdir()] == [True]
    finally:
        end_local_run(tool, marker, [])


@needs_posix
def test_real_local_run(tmp_path: Path) -> None:
    # About 50 s: survivors keep the run's output pipes open, so each communicate() waits for them.
    out = tmp_path / "local.json"
    tool, marker = start_local_run(tmp_path, "--timeout", "1", "--kill-after", "1", "--json-out", str(out))
    lifetime_s = kill_check.child_lifetime_s(1, 1)
    # Processes that only mention the run, as someone watching it might start, the tool must leave alone. D5 to D7
    # (from the review) each end with exactly one of the command lines the tool builds: they differ from its own
    # processes only in their PIDs.
    sleeper = [sys.executable, "-I", "-c", "import time; time.sleep(300)"]
    feed_r, feed_w = os.pipe()
    decoys = [
        subprocess.Popen(["grep", "-c", f"{marker}.timeout_expiry.agent"], stdin=feed_r, stdout=subprocess.DEVNULL),
        subprocess.Popen([*sleeper, f"{marker}.cancel.noterm", str(tool.pid)]),
    ]
    os.close(feed_r)
    for name in kill_check.LOCAL_SCENARIOS:
        tag = f"{marker}.{name}"
        stubborn = name == "timeout_expiry_stubborn"
        agent = kill_check.local_agent_command(tag, ignore_term=stubborn, tool_pid=tool.pid, lifetime_s=lifetime_s)
        noterm = kill_check.local_child_args(tag, "noterm", tool_pid=tool.pid, lifetime_s=lifetime_s)
        plain = kill_check.local_child_args(tag, "plain", tool_pid=tool.pid, lifetime_s=lifetime_s)
        decoys += [
            subprocess.Popen([*sleeper, *agent[1:]]),  # D5
            subprocess.Popen(["sh", "-c", "while :; do sleep 1; done", "sh", " ".join([sys.executable, *noterm])]),
            subprocess.Popen([*sleeper, *plain]),  # D7
        ]
    decoy_pids = {decoy.pid for decoy in decoys}
    try:
        stdout, stderr = tool.communicate(timeout=600)
        assert tool.returncode == 0, stdout + stderr
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["mode"] == "local"
        assert report["marker"] == marker
        assert list(report["scenarios"]) == list(kill_check.LOCAL_SCENARIOS)
        for name, result in report["scenarios"].items():
            assert "error" not in result, (name, result)
            assert result["started"] == list(kill_check.LOCAL_ROLES), name
            assert isinstance(result["returncode"], int), name
            # Each scenario's cleanup found exactly the processes it reported as survivors, and stopped them all.
            cleanup = result["cleanup"]
            running = sorted(p["process"] for p in cleanup["running"])
            assert running == sorted(f"{name}.{role}" for role in result["survivors"]), name
            assert sorted(p["process"] for p in cleanup["exited"] + cleanup["killed"]) == running, name
            assert not {p["pid"] for p in cleanup["running"] + cleanup["killed"]} & decoy_pids, name
            assert cleanup["verified"] is True, name
        # Every kill path leaves the setsid child running, whatever the decoys' command lines say.
        assert all("setsid" in result["survivors"] for result in report["scenarios"].values())
        assert report["scenarios"]["timeout_expiry"]["timed_out"] is True
        assert report["scenarios"]["timeout_expiry_stubborn"]["timed_out"] is True
        assert report["scenarios"]["cancel"]["cancelled"] is True
        assert report["scenarios"]["cancel"]["cancel_reason"] == kill_check.CANCEL_REASON
        assert report["cleanup"] == {
            "running": [],
            "exited": [],
            "killed": [],
            "left": [],
            "unknown": [],
            "verified": True,
        }
        for decoy in decoys:
            assert decoy.poll() is None, decoy.args
        assert set(mentioning(marker)) == decoy_pids
        assert list((tmp_path / "tmp").iterdir()) == []
        assert re.search(r"^scenario +exit  flags", stdout, re.M)
        assert f"nothing with marker {marker} is running now" in stdout
    finally:
        os.close(feed_w)
        stop(*decoys)
        end_local_run(tool, marker, [])


# --- round-7 verifier's additions ---
def test_outlived_skips_a_role_whose_group_is_unknown_whatever_was_noted() -> None:
    # A signal noted with no group went nowhere (LocalProcess found no agent), so it says nothing about a role whose
    # group the tool does not know either: None is not a group.
    unknown = {**GROUPS, "plain": None}
    assert kill_check.outlived([sigkill_at(6.0, None, reaped=True)], unknown, ["plain"], ignore_term=False) == []


def test_outlived_names_the_agent_too() -> None:
    assert kill_check.outlived([sigkill_at(6.0, reaped=True)], GROUPS, ["agent", "setsid"], ignore_term=False) == [
        "agent"
    ]
    all_roles = list(kill_check.LOCAL_ROLES)
    assert kill_check.outlived([sigterm_at(4.0, reaped=True)], GROUPS, all_roles, ignore_term=False) == [
        "agent",
        "plain",
    ]


@needs_posix
def test_the_not_returned_message_prints_the_budget_as_the_scenarios_compute_it(
    harness: kill_check.LocalHarness,
) -> None:
    # budget_s() is a float (LocalProcess's timeouts are floats): the message says "23 s", not "23.0 s".
    limit = harness.budget_s(0)
    assert isinstance(limit, float) and limit == round(limit)
    waiter = cast(Any, types.SimpleNamespace(result=None, error=None))
    message = harness.not_returned(None, waiter, limit, "kill()")
    assert message == f"communicate() had not returned {round(limit)} s after kill(), so the task thread would hang"


@needs_posix
def test_a_stubborn_run_that_local_process_never_sigkills_is_measured_not_an_error(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As a LocalProcess that lost its SIGKILL step would behave: the SIGTERM noted for the group is ignored by
    # everyone in the stubborn run, so all four outliving it is the answer to report, not a signal that went missing.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time

    def sigterm_only(self: Any) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, signal.SIGTERM)

    monkeypatch.setattr(local_process.LocalProcess, "_terminate", sigterm_only)
    result = harness.timeout_expiry(ignore_term=True)
    assert "error" not in result, result
    assert result["survivors"] == list(kill_check.LOCAL_ROLES)
    assert result["exec_running_after"] is True
    assert result["cleanup"]["verified"] is True


@needs_posix
def test_an_agent_killed_and_reaped_before_the_path_signals_is_named_before_what_it_left(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # SIGKILLed from outside and reaped in the instant before the path's SIGTERM: that SIGTERM then goes nowhere. Both
    # checks apply (the agent's -9 is no signal the path sent; plain never got the SIGTERM noted for its group), and
    # the agent, the cause, is named.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    send = local_process.LocalProcess._signal_group
    struck: list[str] = []

    def killed_and_reaped_first(popen: subprocess.Popen[str], sig: int) -> None:
        if not struck:
            (run,) = harness.started
            signal_own(run, "agent", signal.SIGKILL)
            struck.append("agent")
            assert wait_for(lambda: popen.returncode is not None)  # reaped by the tool's exit watcher meanwhile
        send(popen, sig)

    monkeypatch.setattr(local_process.LocalProcess, "_signal_group", staticmethod(killed_and_reaped_first))
    result = harness.timeout_expiry()
    assert struck == ["agent"]
    assert_error_row(result, f"agent {kill_check.STOPPED_OTHERWISE}; results not valid", harness, capsys)


def all_ready(run: kill_check.LocalRun) -> bool:
    return all((run.workdir / f"ready-{role}").exists() for role in kill_check.LOCAL_CHILD_ROLES)


def start_once_all_are_ready(harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """wait_for_children starts only once every child has said it is ready, so that its first look sees them all."""
    wait_for_children = harness.wait_for_children

    def once_all_are_ready(run: kill_check.LocalRun) -> tuple[list[str], str | None]:
        assert wait_for(lambda: all_ready(run))
        return wait_for_children(run)

    monkeypatch.setattr(harness, "wait_for_children", once_all_are_ready)


@needs_posix
def test_an_agent_gone_once_its_children_are_ready_is_named_at_once(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    start_once_all_are_ready(harness, monkeypatch)
    running_roles = harness.running_roles
    killed: list[str] = []

    def kill_the_agent_first(run: kill_check.LocalRun) -> list[str]:
        if not killed:
            signal_own(run.started, "agent", signal.SIGKILL)
            killed.append("agent")
            assert wait_for(lambda: run.popen.returncode is not None)
        return running_roles(run)

    monkeypatch.setattr(harness, "running_roles", kill_the_agent_first)
    started = time.monotonic()
    result = harness.cancel()
    assert killed == ["agent"]
    assert time.monotonic() - started < 5
    assert_error_row(result, "the agent exited (status -9) before the scenario; results not valid", harness, capsys)


@needs_posix
def test_children_stopped_from_outside_are_named_before_the_agent_that_then_exits(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The cause (the children were stopped) comes before its consequence (the agent's `wait` returned, so it exited 0).
    start_once_all_are_ready(harness, monkeypatch)
    running_roles = harness.running_roles
    killed: list[str] = []

    def kill_the_children_first(run: kill_check.LocalRun) -> list[str]:
        if not killed:
            run.started.learn()
            for role in kill_check.LOCAL_CHILD_ROLES:
                signal_own(run.started, role, signal.SIGKILL)
                killed.append(role)
            assert wait_for(lambda: run.popen.returncode is not None)
        return running_roles(run)

    monkeypatch.setattr(harness, "running_roles", kill_the_children_first)
    result = harness.local_kill()
    assert killed == list(kill_check.LOCAL_CHILD_ROLES)
    error = f"plain, noterm, setsid {kill_check.STOPPED_BEFORE}; results not valid"
    assert_error_row(result, error, harness, capsys)


@needs_posix
def test_a_working_directory_removed_while_the_children_start_is_named_once_the_agent_has_exited(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The cause (the directory is gone, so the children exited) comes before its consequence (the agent's `wait`
    # returned, so it exited 0), even when the listing that follows shows none of them any more.
    start_once_all_are_ready(harness, monkeypatch)
    running_roles = harness.running_roles
    removed: list[str] = []

    def remove_it_first(run: kill_check.LocalRun) -> list[str]:
        if not removed:
            shutil.rmtree(run.workdir, ignore_errors=True)
            removed.append(str(run.workdir))
            assert wait_for(lambda: run.popen.returncode is not None)  # its children exit, and then so does it
        return running_roles(run)

    monkeypatch.setattr(harness, "running_roles", remove_it_first)
    result = harness.timeout_expiry()
    assert removed
    assert_error_row(result, f"{kill_check.WORKDIR_REMOVED}; results not valid", harness, capsys)


@needs_posix
def test_an_agent_that_exits_soon_after_it_started_is_named_within_moments(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Still running at wait_for_children's first look, gone at a later one: the error comes within moments, not after
    # a long wait. (An agent that exits at once may already be gone at the first look, which leaves the waiting loop
    # unexercised.)
    monkeypatch.setattr(kill_check, "local_agent_script", lambda *, ignore_term: "sleep 0.5; exit 0")
    started = time.monotonic()
    result = harness.timeout_expiry()
    assert time.monotonic() - started < 3
    error = "the agent exited (status 0) before plain, noterm, setsid were ready; results not valid"
    assert_error_row(result, error, harness, capsys)


@needs_posix
@pytest.mark.parametrize(("scenario", "after"), [("local_kill", "kill()"), ("timeout_expiry", "it was called")])
def test_a_communicate_that_does_not_return_is_named_before_survivors_stopped_after_the_path(
    harness: kill_check.LocalHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    after: str,
) -> None:
    # Both apply: communicate() never returns, and the survivors are stopped from outside after the path had run. The
    # dispatcher's own hang is named first.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    monkeypatch.setattr(harness, "budget_s", lambda timeout_s: timeout_s + 3)  # likewise
    real = local_process.LocalProcess.communicate
    release = threading.Event()

    def hangs(self: Any, timeout: float | None) -> Any:
        result = real(self, timeout)
        release.wait(60)
        return result

    monkeypatch.setattr(local_process.LocalProcess, "communicate", hangs)
    stopped: list[int] = []
    join = kill_check.Communicator.join

    def stop_survivors_then_join(self: kill_check.Communicator, timeout: float) -> bool:
        if not stopped:
            for run in harness.started:
                for pid, label in list(run.pids.items()):
                    if label.endswith((".noterm", ".setsid")) and kill_check.kill_if_owned(pid, [run]):
                        stopped.append(pid)
        return join(self, timeout)

    monkeypatch.setattr(kill_check.Communicator, "join", stop_survivors_then_join)
    try:
        result = kill_check.LOCAL_SCENARIOS[scenario](harness)
    finally:
        release.set()
    assert stopped
    limit = harness.budget_s(0 if scenario == "local_kill" else harness.timeout_s)
    error = f"communicate() had not returned {limit:g} s after {after}, so the task thread would hang"
    assert_error_row(result, f"{error}; results not valid", harness, capsys)


@needs_posix
def test_a_local_process_that_signals_the_agent_alone_is_measured_not_an_error(
    harness: kill_check.LocalHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Were LocalProcess changed to signal only the agent's PID, plain would really survive kill(). The tool must
    # report that, not take the noted group signal for one that never went out.
    from cairn.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 0.5)  # only to save time
    monkeypatch.setattr(
        local_process.LocalProcess, "_signal_group", staticmethod(lambda popen, sig: popen.send_signal(sig))
    )
    result = harness.local_kill()
    assert "error" not in result, result
    assert result["survivors"] == ["plain", "noterm", "setsid"]
