#!/usr/bin/env python3
"""Phase 0 kill check: what really stops a worker command in Cairn's container and local modes.

Container mode (the default) runs Cairn's own runtime code against your Docker daemon:

* ContainerManager.build_exec_process, which wraps the command as ``timeout -k <K>s <N>s ...``
  exactly as the dispatcher does,
* ManagedProcess.communicate, kill and cancel.

The command it runs stands in for an agent CLI and starts three children: a normal child, a child
that ignores SIGTERM (as some tools do), and a child in its own session (setsid). Each scenario
then ends the run one way and lists which of those processes are still alive in the container.

Scenarios:
  wrapper_expiry          the timeout wrapper expires (the agent exits on SIGTERM)
  wrapper_expiry_stubborn the timeout wrapper expires and the agent ignores SIGTERM, so the
                          wrapper's SIGKILL follows after K seconds
  managed_kill            ManagedProcess.kill() on a running exec: does the run stop, and what
                          survives? It also records the host PID that exec_inspect reports and
                          whether it exists inside the container, and, with dispatcher code from
                          before kill() was fixed, what _kill_pid ran and the exit codes it accepted
  communicate_timeout     the dispatcher's own wait (communicate) ends before the wrapper
  cancel                  ManagedProcess.cancel(), as when a project is stopped: how long until
                          communicate() returns?

Every scenario uses its own throwaway container (label cairn.phase0=kill-check, no network),
removed at the end even on errors. Nothing else on your Docker host is touched.

Local mode (--mode local, for runtime.execution: local) needs no Docker. It runs the same kind of
agent as processes on this machine, through LocalBackend.build_exec_process and LocalProcess:
  timeout_expiry          LocalProcess's own timeout expires (the agent exits on SIGTERM)
  timeout_expiry_stubborn the same, but the agent ignores SIGTERM, so SIGKILL to its process
                          group follows after the grace period (--kill-after)
  local_kill              LocalProcess.kill() while communicate() waits on another thread, as
                          when a heartbeat lease is lost
  cancel                  TaskCancellation.cancel(), which is how the scheduler stops a task whose
                          project is no longer active; it calls LocalProcess.cancel()

Local mode touches only processes it started itself. It knows each one by the PID it learned when it
started it (the agent's from Popen; each test child writes its own into the run's working directory,
inside this tool's private temporary directory), together with the exact command line it built for it
(all but argv[0]). A process that only repeats such a command line is never one of them. It asks its
processes to exit first and sends SIGKILL only to those that do not, each checked again right before
(on Linux through a pidfd). Each scenario stops what it left running, a final cleanup runs even after
Ctrl-C, and the test children also exit by themselves once this tool's process is gone. A scenario
reports an error instead of results whenever something other than the path under test may have
stopped one of its test processes: it notes every signal LocalProcess sends, lists its test processes
about every 0.2 s until it counts the survivors, and accepts the end of each one only where one of
those signals explains it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
CAIRN_SRC = REPO_ROOT / "cairn" / "src"

DEFAULT_IMAGE = "debian:bookworm-slim"
LABEL_KEY = "cairn.phase0"
LABEL_VALUE = "kill-check"
AGENT_TAG = "cairn-killcheck-agent"
LISTER_TAG = "cairn-killcheck-lister"
ROLE_ORDER = ["wrapper", "agent", "plain", "noterm", "setsid"]
LONG_TIMEOUT_S = 120  # wrapper deadline for scenarios that end the run some other way
COMMUNICATE_SHORT_S = 2  # communicate_timeout: the dispatcher's wait gives up first
CANCEL_WRAPPER_S = 10  # cancel: the wrapper would end the run on its own after this long
KILL_WAIT_S = 5.0  # how long to watch for the exec to stop after kill()
SETTLE_S = 0.5

# Lists the processes in the container from /proc, so the image needs no ps. NUL and newline in
# a command line become spaces; zombies have an empty cmdline and are skipped.
LIST_SCRIPT = r"""for d in /proc/[0-9]*; do
  c=$(tr '\000\n' '  ' < "$d/cmdline" 2>/dev/null) || continue
  [ -n "$c" ] && printf '%s\t%s\n' "${d#/proc/}" "$c"
done
exit 0
"""


def agent_script(*, ignore_term: bool) -> str:
    lines = ["trap '' TERM"] if ignore_term else []
    lines += [
        "sleep 3001 &",
        "( trap '' TERM; exec sleep 3002 ) &",
        "if command -v setsid >/dev/null 2>&1; then setsid sleep 3003 & fi",
        "echo started",
        "wait",
    ]
    return "\n".join(lines) + "\n"


def agent_command(*, ignore_term: bool = False) -> list[str]:
    return ["sh", "-c", agent_script(ignore_term=ignore_term), AGENT_TAG]


def classify(cmdline: str) -> str | None:
    """Role of one of our test processes, or None for anything else (PID 1, the lister, ...)."""
    text = cmdline.strip()
    if LISTER_TAG in text:
        return None
    if AGENT_TAG in text:
        return "wrapper" if text.startswith("timeout ") else "agent"
    return {"sleep 3001": "plain", "sleep 3002": "noterm", "sleep 3003": "setsid"}.get(text)


def parse_listing(output: str) -> list[dict[str, Any]]:
    processes = []
    for line in output.splitlines():
        pid, _, cmdline = line.partition("\t")
        if not pid.strip().isdigit():
            continue
        processes.append({"pid": int(pid), "cmdline": cmdline.strip(), "role": classify(cmdline)})
    return processes


def roles(processes: list[dict[str, Any]]) -> list[str]:
    present = {p["role"] for p in processes if p["role"]}
    return [role for role in ROLE_ORDER if role in present]


def parse_top(top: dict[str, Any] | None) -> dict[int, str]:
    """Host PID -> command line, from Docker's `top` (which reports host PIDs)."""
    if not top:
        return {}
    titles = [str(t).upper() for t in top.get("Titles") or []]
    pid_col = titles.index("PID") if "PID" in titles else None
    cmd_col = next((titles.index(name) for name in ("CMD", "COMMAND", "ARGS") if name in titles), None)
    if pid_col is None or cmd_col is None:
        return {}
    result = {}
    for row in top.get("Processes") or []:
        try:
            result[int(row[pid_col])] = str(row[cmd_col])
        except (ValueError, IndexError):
            continue
    return result


def kill_pid_accepted(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The attempt after which _kill_pid stopped trying: its first exit code in (None, 0, 1)."""
    return next((a for a in attempts if a.get("exit_code") in (None, 0, 1)), None)


def _fmt_s(value: float | None) -> str:
    return "?" if value is None else f"{value} s"


def conclusions(results: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for name in ("wrapper_expiry", "wrapper_expiry_stubborn"):
        r = results.get(name)
        if r and "error" not in r:
            survivors = ", ".join(r["survivors"]) or "nothing"
            lines.append(
                f"{name}: the wrapper ended the run with exit {r['returncode']} (process gone after "
                f"{_fmt_s(r.get('exec_ended_after_s'))}, communicate() returned after {r['ended_after_s']} s); "
                f"still alive in the container afterwards: {survivors}."
            )
    r = results.get("managed_kill")
    if r and "error" not in r:
        if r["exec_running_after_kill"]:
            text = f"managed_kill: ManagedProcess.kill() did not stop the run (still running {KILL_WAIT_S:g} s later)"
            accepted = r.get("kill_pid_accepted_attempt")
            if accepted or r.get("kill_attempts"):  # dispatcher code from before kill() was fixed
                if accepted:
                    text += f"; _kill_pid stopped after `{' '.join(map(str, accepted['command']))}` exited {accepted['exit_code']}, which it treats as done"
                if r.get("exec_pid_host_command"):
                    text += f"; the PID it used ({r['exec_inspect_pid']}) is the host PID of `{r['exec_pid_host_command'][:80]}`"
                if not r.get("exec_pid_visible_inside_container"):
                    text += ", and no process has that PID inside the container"
                replay = r.get("kill_replay_with_output")
                if replay:
                    text += f"; run again with output attached it exits {replay['exit_code']}: {replay['output'] or '(no output)'}"
            else:
                text += (
                    "; the dispatcher's warning gives the reason (for example an image whose `timeout` replaces itself "
                    "with the command, as BusyBox's does)"
                )
            lines.append(text + ".")
        else:
            lines.append(
                f"managed_kill: ManagedProcess.kill() stopped the run within {r['stopped_after_s']} s; "
                f"still alive afterwards: {', '.join(r['survivors']) or 'nothing'}."
            )
    r = results.get("communicate_timeout")
    if r and "error" not in r:
        lines.append(
            f"communicate_timeout: communicate() returned exit {r['returncode']} (timed_out={r['timed_out']}) after "
            f"{r['communicate_returned_after_s']} s; the run was {'still running' if r['exec_running_after'] else 'stopped'} "
            f"afterwards; alive: {', '.join(r['survivors']) or 'nothing'}."
        )
    r = results.get("cancel")
    if r and "error" not in r:
        waited = r["communicate_returned_after_cancel_s"]
        verdict = (
            "cancel stopped the run promptly"
            if waited < 3
            else f"cancel did not stop the run: communicate() waited for the wrapper ({r['wrapper_timeout_s']} s budget)"
        )
        lines.append(
            f"cancel: {verdict}; returned {waited} s after cancel(); alive afterwards: {', '.join(r['survivors']) or 'nothing'}."
        )
    return lines


class ExitWatcher:
    """Notes when a running exec stops, from exec_inspect, apart from when communicate() returns.

    Docker can keep the exec's output stream open a little after the process ends, while
    surviving children still hold its stdout, so the two moments can differ.
    """

    def __init__(self, inspect: Callable[[], dict[str, Any]], started: float):
        self._inspect = inspect
        self._started = started
        self._stop = threading.Event()
        self.ended_after_s: float | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        seen_running = False
        while not self._stop.wait(0.1):
            try:
                running = bool(self._inspect().get("Running"))
            except Exception:
                return
            if running:
                seen_running = True
            elif seen_running:
                self.ended_after_s = round(time.monotonic() - self._started, 1)
                return

    def stop(self) -> float | None:
        self._stop.set()
        self._thread.join(timeout=2)
        return self.ended_after_s


class Harness:
    """Holds the Docker client and Cairn's runtime classes; one fresh container per scenario."""

    def __init__(self, image: str, timeout_s: int, kill_after_s: int):
        import docker

        from cairn.dispatcher.config import ContainerConfig
        from cairn.dispatcher.runtime.containers import ContainerManager
        from cairn.dispatcher.tasks.common import communicate_timeout, did_timeout

        self.image = image
        self.timeout_s = timeout_s
        self.kill_after_s = kill_after_s
        self.client = docker.from_env()
        self.manager = ContainerManager(ContainerConfig(image=image, network_mode="none", completed_action="remove"))
        self.communicate_timeout: Callable[[int], int] = communicate_timeout
        self.did_timeout: Callable[[Any], bool] = did_timeout
        self._processes: list[Any] = []

    def close(self) -> None:
        for process in self._processes:
            reader = getattr(process, "_reader", None)
            if reader is not None:
                reader.join(timeout=5)
        self.manager.close()
        self.client.close()

    @contextlib.contextmanager
    def container(self):
        container = self.client.containers.run(
            self.image,
            ["sleep", "infinity"],
            detach=True,
            name=f"cairn-phase0-killcheck-{uuid.uuid4().hex[:12]}",
            labels={LABEL_KEY: LABEL_VALUE},
            network_mode="none",
        )
        try:
            yield container
        finally:
            with contextlib.suppress(Exception):
                container.remove(force=True)

    def start(self, container: Any, *, timeout_s: int, ignore_term: bool = False) -> Any:
        process = self.manager.build_exec_process(
            container.name,
            {},
            agent_command(ignore_term=ignore_term),
            timeout_seconds=timeout_s,
            kill_after_seconds=self.kill_after_s,
        )
        process.start()
        self._processes.append(process)
        return process

    def processes(self, container: Any) -> list[dict[str, Any]]:
        result = container.exec_run(["sh", "-c", LIST_SCRIPT, LISTER_TAG], stdout=True, stderr=False)
        output = result.output.decode("utf-8", "replace") if isinstance(result.output, bytes) else str(result.output)
        return parse_listing(output)

    def wait_for_children(self, container: Any, timeout: float = 5.0) -> list[dict[str, Any]]:
        expected = {"wrapper", "agent", "plain", "noterm"}
        deadline = time.monotonic() + timeout
        while True:
            found = self.processes(container)
            if expected <= set(roles(found)) or time.monotonic() >= deadline:
                return found
            time.sleep(0.2)

    def exec_state(self, process: Any) -> dict[str, Any]:
        return self.client.api.exec_inspect(process._exec_id)

    def host_view(self, container: Any) -> dict[int, str]:
        try:
            return parse_top(container.top())
        except Exception:
            return {}

    # -- scenarios ---------------------------------------------------------------------------

    def wrapper_expiry(self, *, ignore_term: bool = False) -> dict[str, Any]:
        with self.container() as container:
            started = time.monotonic()
            process = self.start(container, timeout_s=self.timeout_s, ignore_term=ignore_term)
            watcher = ExitWatcher(lambda: self.exec_state(process), started)
            before = self.wait_for_children(container)
            result = process.communicate(timeout=self.communicate_timeout(self.timeout_s))
            ended_after = time.monotonic() - started
            time.sleep(SETTLE_S)
            state = self.exec_state(process)
            return {
                "wrapper": f"timeout -k {self.kill_after_s}s {self.timeout_s}s",
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "did_timeout": self.did_timeout(result),
                "exec_ended_after_s": watcher.stop(),
                "ended_after_s": round(ended_after, 1),
                "exec_running_after": bool(state.get("Running")),
                "started": roles(before),
                "survivors": roles(self.processes(container)),
            }

    def managed_kill(self) -> dict[str, Any]:
        with self.container() as container:
            process = self.start(container, timeout_s=LONG_TIMEOUT_S)
            before = self.wait_for_children(container)
            state = self.exec_state(process)
            exec_pid = state.get("Pid")
            inside = {p["pid"]: p for p in before}
            host = self.host_view(container)
            attempts: list[dict[str, Any]] = []
            original = process._container.exec_run

            def recording_exec_run(command: Any, *args: Any, **kwargs: Any) -> Any:
                outcome = original(command, *args, **kwargs)
                attempts.append({"command": list(command), "exit_code": getattr(outcome, "exit_code", None)})
                return outcome

            process._container.exec_run = recording_exec_run
            try:
                kill_started = time.monotonic()
                process.kill()
                kill_call_s = time.monotonic() - kill_started
            finally:
                process._container.exec_run = original
            stopped_after = None
            watch_started = time.monotonic()
            while time.monotonic() - watch_started < KILL_WAIT_S:
                if not self.exec_state(process).get("Running"):
                    stopped_after = round(time.monotonic() - watch_started, 1)
                    break
                time.sleep(0.2)
            time.sleep(SETTLE_S)
            wrapper_inside = next((p["pid"] for p in before if p["role"] == "wrapper"), None)
            accepted = kill_pid_accepted(attempts)
            replay = None
            if accepted is not None and exec_pid not in inside:
                # exec_run returns before the kill finishes, so _kill_pid never sees its real exit code.
                # Run the same command again with its output attached; harmless, as no process inside
                # the container has that PID.
                again = container.exec_run(accepted["command"], stdout=True, stderr=True)
                replay = {
                    "command": accepted["command"],
                    "exit_code": again.exit_code,
                    "output": (again.output if isinstance(again.output, bytes) else b"")
                    .decode("utf-8", "replace")
                    .strip()[:200],
                }
            return {
                "exec_inspect_pid": exec_pid,
                "exec_pid_host_command": host.get(exec_pid) if exec_pid else None,
                "exec_pid_visible_inside_container": exec_pid in inside,
                "inside_process_with_that_pid": inside[exec_pid]["cmdline"] if exec_pid in inside else None,
                "wrapper_pid_inside_container": wrapper_inside,
                "kill_attempts": attempts,
                "kill_pid_accepted_attempt": accepted,
                "kill_replay_with_output": replay,
                "kill_call_s": round(kill_call_s, 2),
                "exec_running_after_kill": stopped_after is None,
                "stopped_after_s": stopped_after,
                "started": roles(before),
                "survivors": roles(self.processes(container)),
            }

    def communicate_timeout_scenario(self) -> dict[str, Any]:
        with self.container() as container:
            process = self.start(container, timeout_s=LONG_TIMEOUT_S)
            before = self.wait_for_children(container)
            started = time.monotonic()
            result = process.communicate(timeout=COMMUNICATE_SHORT_S)
            returned_after = time.monotonic() - started
            time.sleep(SETTLE_S)
            return {
                "communicate_timeout_s": COMMUNICATE_SHORT_S,
                "wrapper_timeout_s": LONG_TIMEOUT_S,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "did_timeout": self.did_timeout(result),
                "communicate_returned_after_s": round(returned_after, 1),
                "exec_running_after": bool(self.exec_state(process).get("Running")),
                "started": roles(before),
                "survivors": roles(self.processes(container)),
            }

    def cancel(self) -> dict[str, Any]:
        with self.container() as container:
            process = self.start(container, timeout_s=CANCEL_WRAPPER_S)
            before = self.wait_for_children(container)
            cancelled_at = time.monotonic()
            watcher = ExitWatcher(lambda: self.exec_state(process), cancelled_at)
            process.cancel("kill-check: project stopped")
            result = process.communicate(timeout=self.communicate_timeout(CANCEL_WRAPPER_S))
            returned_after = time.monotonic() - cancelled_at
            time.sleep(SETTLE_S)
            return {
                "wrapper_timeout_s": CANCEL_WRAPPER_S,
                "exec_ended_after_cancel_s": watcher.stop(),
                "cancelled": result.cancelled,
                "cancel_reason": result.cancel_reason,
                "returncode": result.returncode,
                "communicate_returned_after_cancel_s": round(returned_after, 1),
                "exec_running_after": bool(self.exec_state(process).get("Running")),
                "started": roles(before),
                "survivors": roles(self.processes(container)),
            }


SCENARIOS: dict[str, Callable[[Harness], dict[str, Any]]] = {
    "wrapper_expiry": lambda h: h.wrapper_expiry(),
    "wrapper_expiry_stubborn": lambda h: h.wrapper_expiry(ignore_term=True),
    "managed_kill": lambda h: h.managed_kill(),
    "communicate_timeout": lambda h: h.communicate_timeout_scenario(),
    "cancel": lambda h: h.cancel(),
}


# --- local mode ----------------------------------------------------------------------------------
#
# With runtime.execution: local the dispatcher runs each worker command as a process on its own host,
# through LocalBackend and LocalProcess. This mode drives that code on your machine, against processes
# it starts itself, and is built never to touch anything else:
#
# * it knows each process it starts by the PID it learned from the process itself: the agent's from Popen,
#   right after starting it, and each test child's from the pid file that the child writes into the run's
#   working directory, inside the tool's own temporary directory, as its first step. A running process is
#   its own only if it has one of those PIDs and its command line still ends with exactly the one the tool
#   built for that process (everything after argv[0]). A process that only repeats such a command line,
#   even word for word, is never its own;
# * to stop its processes it first asks them to exit (a stop file in their working directory), and
#   sends SIGKILL only to those still running about a second later, each one checked again right before
#   the signal (on Linux through a pidfd, so a PID reused in between cannot be hit);
# * each scenario stops what it left running, and a final cleanup runs in a finally block; the first
#   Ctrl-C, SIGTERM or SIGHUP stops the run and any that follow are ignored, so that the cleanup and its
#   summary finish. The handlers are in place before anything is created, and one that comes while a
#   scenario's own cleanup runs is held back until that cleanup has recorded what it found;
# * a cleanup confirms that nothing is left only when it knows every test child's PID, or the agent has
#   exited by itself (its `wait` returns only once all its children have ended);
# * each test child exits by itself once this tool's process or the scratch workspace is gone, or after
#   a few minutes;
# * so that no such exit, and no kill from outside, passes for the work of the path under test, a scenario
#   reports an error instead of results when its working directory was removed, a test child exited on its
#   own, or a test process was already gone when the path started or went missing after the path had run,
#   as it does when its test processes did not all start;
# * and, around the path itself, it notes every signal that LocalProcess sends (record_signals), lists the
#   test processes about every POLL_S from the moment they have all started until the survivors are counted
#   (Monitor), and reports an error for any test process that is gone at the end without one of those signals
#   to explain it (unexplained), or that survived one that ends it and may not have been delivered (outlived);
#   and when communicate() raised or had not returned within the scenario's budget (not_returned).

LOCAL_MARKER_PREFIX = "cairn-killcheck-local-"
LOCAL_CHILD_ROLES = ("plain", "noterm", "setsid")
LOCAL_ROLES = ("agent", *LOCAL_CHILD_ROLES)
CONTAINER_JSON_OUT = Path("kill_check_results.json")
LOCAL_JSON_OUT = Path("kill_check_local_results.json")
READY_WAIT_S = 15.0  # how long to wait for the agent's children to start and set themselves up
STOP_WAIT_S = 1.0  # how long the test processes get to exit by themselves once asked to stop
SWEEP_DEADLINE_S = 10.0  # how long a sweep keeps killing before it reports what is left
POLL_S = 0.2  # how often a scenario lists its test processes, from when they have all started until it counts survivors
EXIT_LAG_S = 0.5  # how long a test process may still be listed after a signal that ends it (see unexplained)
CANCEL_REASON = "kill-check: project stopped"
STOP_FILE = "stop"  # created in a run's working directory to ask its test children to exit
PYTHON_ENV = "CAIRN_KILLCHECK_PYTHON"  # the agent's environment variable naming the Python for the children
PROC = Path("/proc")
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)  # SIGINT last: once restored, it can interrupt
WORKDIR_REMOVED = "the run's working directory was removed during the scenario"
STOPPED_BEFORE = "stopped before the kill path ran"
STOPPED_AFTER = "stopped after the kill path had run"
STOPPED_OTHERWISE = "stopped by something other than the kill path"
NOT_TIMED_OUT = "the timeout did not end the run (communicate() returned timed_out=False)"
NOTHING_STARTED = {"running": [], "exited": [], "killed": [], "left": [], "unknown": [], "verified": True}

# The test child, one per role. Its first step writes its PID to pid-<role> in its working directory (by
# renaming, so the file is never seen half written). It then sets itself up for its role, says so with a
# ready-<role> file, and waits. It exits by itself once a stop file appears in that directory, once that
# directory (inside the scratch workspace) is gone, once this tool's process is gone, or after `lifetime`
# seconds, so even a tool killed with SIGKILL (no cleanup at all) leaves nothing behind for long. None of
# those exits may pass for the work of the kill path under test: the child leaves an exited-<role> file on
# its way out, and where the directory is gone and it cannot, the tool sees that the directory is gone.
LOCAL_CHILD_SOURCE = """\
import os, signal, sys, time
token, tool_pid, lifetime = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
role = token.rsplit('.', 1)[1]
with open('pid-' + role + '.new', 'w') as f:
    f.write(str(os.getpid()))
os.rename('pid-' + role + '.new', 'pid-' + role)
if role == 'noterm':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif role == 'setsid':
    os.setsid()
here = os.getcwd()
open('ready-' + role, 'w').close()
deadline = time.monotonic() + lifetime
while time.monotonic() < deadline:
    time.sleep(0.1)
    if os.path.exists(os.path.join(here, 'stop')) or not os.path.isdir(here):
        break
    try:
        os.kill(tool_pid, 0)
    except ProcessLookupError:
        break
    except OSError:
        pass
try:
    open(os.path.join(here, 'exited-' + role), 'w').close()
except OSError:
    pass
"""


def child_code(source: str) -> str:
    """`source` as one line of printable ASCII for `python -c`, with no backslash: newlines travel as `~`.

    The tool reads its processes' command lines back to recognise them, and `ps` on macOS shows a newline,
    a tab or a non-ASCII character in an escaped form, so the command lines hold none of those.
    """
    bad = sorted({c for c in source if c in '\\"~' or not (c == "\n" or " " <= c <= "~")})
    if bad:
        raise ValueError(f"the child code cannot contain {bad!r}")
    return 'exec("' + source.replace("\n", "~") + '".replace("~", chr(10)))'


LOCAL_CHILD_CODE = child_code(LOCAL_CHILD_SOURCE)


def new_marker() -> str:
    """A marker that no other process on this machine has on its command line."""
    return LOCAL_MARKER_PREFIX + secrets.token_hex(8)


def child_lifetime_s(timeout_s: int, kill_after_s: int) -> int:
    """The longest a test child may live: well past the longest local scenario."""
    return 2 * (max(LONG_TIMEOUT_S, timeout_s) + max(1, kill_after_s)) + 60


def local_agent_script(*, ignore_term: bool) -> str:
    """The stand-in agent for local mode: one line of sh that starts the three children and waits.

    $1 is the child code, $2 the scenario tag, $3 this tool's PID and $4 the children's lifetime. The
    Python for the children comes in the environment (PYTHON_ENV), so its path, which may hold any
    character, is in no argument that the tool reads back. macOS has no setsid binary, so the setsid
    child calls os.setsid() itself.
    """
    parts = ["trap '' TERM;"] if ignore_term else []
    parts += [f'"${PYTHON_ENV}" -I -c "$1" "$2.{role}" "$3" "$4" &' for role in LOCAL_CHILD_ROLES]
    return " ".join([*parts, "echo started;", "wait"])


def local_agent_command(tag: str, *, ignore_term: bool, tool_pid: int, lifetime_s: int) -> list[str]:
    script = local_agent_script(ignore_term=ignore_term)
    return ["/bin/sh", "-c", script, f"{tag}.agent", LOCAL_CHILD_CODE, tag, str(tool_pid), str(lifetime_s)]


def local_child_args(tag: str, role: str, *, tool_pid: int, lifetime_s: int) -> list[str]:
    """The arguments (argv[1:]) of one test child, exactly as local_agent_script passes them."""
    return ["-I", "-c", LOCAL_CHILD_CODE, f"{tag}.{role}", str(tool_pid), str(lifetime_s)]


def owned_tails(marker: str, name: str, *, ignore_term: bool, tool_pid: int, lifetime_s: int) -> dict[str, str]:
    """The command lines of one run's processes after argv[0], exactly as the tool builds them, each mapped to
    `<scenario>.<role>`.

    argv[0] is left out because it may not read back as started: the macOS framework Python replaces it
    with the path of its real interpreter.
    """
    tag = f"{marker}.{name}"
    agent = local_agent_command(tag, ignore_term=ignore_term, tool_pid=tool_pid, lifetime_s=lifetime_s)
    tails = {" ".join(agent[1:]): f"{name}.agent"}
    for role in LOCAL_CHILD_ROLES:
        tails[" ".join(local_child_args(tag, role, tool_pid=tool_pid, lifetime_s=lifetime_s))] = f"{name}.{role}"
    return tails


def owned_label(cmdline: str, tails: Mapping[str, str]) -> str | None:
    """`<scenario>.<role>` if `cmdline` ends with a space and then exactly one of the command lines in `tails`.

    This is only half of the check: any program can end its arguments with the same text, as a `watch pgrep -f`
    given a whole command line does. A process is the tool's only if its PID is one the tool learned too
    (see Started).
    """
    return next((label for tail, label in tails.items() if cmdline.endswith(" " + tail)), None)


def read_pid(path: Path) -> int | None:
    """The PID in a pid file that a test child wrote; None if there is none (yet)."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    return int(text) if text.isdigit() and int(text) > 1 else None


@dataclass
class Started:
    """How the tool knows one run's processes. It learns them from the run itself, never from a process listing.

    A running process is one of them only if both of these hold:

    * its PID is one the tool learned: the agent's from Popen, right after starting it, and each test child's from
      the pid-<role> file that the child writes into the run's working directory as its first step. That directory
      is inside the tool's own temporary directory, which only this user can enter;
    * its command line, read right now, ends with exactly the one the tool built for that same process (see
      owned_label).

    The PIDs are kept once learned, so they survive the working directory going away.
    """

    name: str
    workdir: Path
    tails: dict[str, str]  # each process's command line after argv[0] -> `<scenario>.<role>`
    pids: dict[int, str] = field(default_factory=dict)  # PID -> `<scenario>.<role>`
    agent: subprocess.Popen[str] | None = None  # the agent, once started
    agent_started_at: float = 0.0  # time.monotonic() when it started

    def learn(self) -> None:
        """Notes the PIDs that the test children have written since the last call."""
        for role in LOCAL_CHILD_ROLES:
            pid = read_pid(self.workdir / f"pid-{role}")
            if pid is not None:
                self.pids.setdefault(pid, f"{self.name}.{role}")

    def label(self, pid: int, cmdline: str) -> str | None:
        """`<scenario>.<role>` if `pid` is one of this run's processes and `cmdline` is still its command line."""
        expected = self.pids.get(pid)
        if expected is None or pid == os.getpid():
            return None
        return expected if owned_label(cmdline, self.tails) == expected else None

    def unaccounted(self) -> list[str]:
        """`<scenario>.<role>` of each test child that may be running without the tool knowing it: one whose PID it
        has not learned (the child has not written its pid file yet), unless the agent has exited by itself.

        The agent's last command is `wait`, which returns only once all its children have ended, so an agent that
        exited with status 0 has none left. One that was killed, or still runs, may have children that are still
        starting, which no process listing can tell from anyone else's."""
        if self.agent is None:  # never started: it has no children
            return []
        learned = {label.rpartition(".")[2] for label in self.pids.values()}
        missing = [role for role in LOCAL_CHILD_ROLES if role not in learned]
        if not missing or self.agent.poll() == 0:
            return []
        return [f"{self.name}.{role}" for role in missing]

    def may_still_start(self, now: float) -> bool:
        """Whether a cleanup should wait for this run's children to make themselves known: some are unaccounted for
        (see unaccounted), and it is less than READY_WAIT_S since the agent started them."""
        return now < self.agent_started_at + READY_WAIT_S and bool(self.unaccounted())


def owned_label_of(pid: int, cmdline: str, runs: Iterable[Started]) -> str | None:
    """`<scenario>.<role>` if the process `pid`, running `cmdline`, is one of the tool's (see Started), else None."""
    for run in runs:
        label = run.label(pid, cmdline)
        if label is not None:
            return label
    return None


def listing_method() -> str:
    """How processes are listed: /proc where it exists (Linux), `ps` elsewhere (macOS)."""
    return "proc" if (PROC / "self" / "cmdline").is_file() else "ps"


def _proc_cmdline(pid: int) -> str | None:
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None


def proc_processes() -> dict[int, str] | None:
    try:
        names = os.listdir(PROC)
    except OSError:
        return None
    found = {}
    for name in names:
        if name.isdigit() and (cmdline := _proc_cmdline(int(name))):
            found[int(name)] = cmdline
    return found


def _run_ps(args: list[str]) -> str | None:
    try:
        done = subprocess.run(
            ["ps", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            start_new_session=True,  # so a Ctrl-C in the terminal cannot cut a listing short
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_ps(output: str) -> dict[int, str]:
    """PID -> command line, from the output of `ps -o pid= -o command=`."""
    found = {}
    for line in output.splitlines():
        pid, _, cmdline = line.strip().partition(" ")
        if pid.isdigit() and cmdline.strip():
            found[int(pid)] = cmdline.strip()
    return found


def ps_processes() -> dict[int, str] | None:
    output = _run_ps(["-A", "-ww", "-o", "pid=", "-o", "command="])
    return None if output is None else parse_ps(output)


def all_processes() -> dict[int, str] | None:
    """PID -> command line of every process this user can see; None if they could not be listed.

    Zombies and kernel threads have no command line and are left out.
    """
    return proc_processes() if listing_method() == "proc" else ps_processes()


def command_line(pid: int) -> str | None:
    """The command line of one process as it is right now; None if it is gone or a zombie."""
    if listing_method() == "proc":
        return _proc_cmdline(pid)
    return (_run_ps(["-ww", "-o", "command=", "-p", str(pid)]) or "").strip() or None


def owned_processes(
    runs: Sequence[Started], listing: Callable[[], dict[int, str] | None] = all_processes
) -> dict[int, str] | None:
    """PID -> `<scenario>.<role>` of the running processes that are the tool's (see Started), never the tool
    itself; None if the processes could not be listed."""
    for run in runs:
        run.learn()
    found = listing()
    if found is None:
        return None
    owned = {}
    for pid, cmdline in found.items():
        label = owned_label_of(pid, cmdline, runs)
        if label is not None:
            owned[pid] = label
    return owned


def _open_pidfd(pid: int) -> int | None:
    """A pidfd for `pid` where Linux offers them, else None. Raises ProcessLookupError if `pid` is gone."""
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
        return None
    try:
        return os.pidfd_open(pid)
    except ProcessLookupError:
        raise
    except OSError:  # a kernel older than 5.3, or a sandbox that blocks pidfds
        return None


def kill_if_owned(pid: int, runs: Sequence[Started]) -> bool:
    """SIGKILL `pid`, but only if it is one of the tool's processes (see Started), with its command line read
    right now.

    Where Linux offers a pidfd, it is opened before that read and the signal goes through it, so the signal
    can only reach the process that was checked, even if that one ended and its PID was reused. macOS has
    no pidfd: there a PID reused between the read and the signal could be hit, which is why the tool asks
    its processes to exit first (stop_owned) and kills only those that did not.
    """
    if pid <= 1 or pid == os.getpid():  # never 0 or -1, which would signal a whole group or everything
        return False
    try:
        pidfd = _open_pidfd(pid)
    except ProcessLookupError:
        return False
    try:
        cmdline = command_line(pid)
        if not cmdline or owned_label_of(pid, cmdline, runs) is None:
            return False
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except OSError:  # gone already, or not ours to signal
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _labelled(processes: Mapping[int, str]) -> list[dict[str, Any]]:
    return [{"pid": pid, "process": label} for pid, label in sorted(processes.items())]


def sweep(
    runs: Sequence[Started],
    *,
    killed: dict[int, str] | None = None,
    deadline_s: float = SWEEP_DEADLINE_S,
    listing: Callable[[], dict[int, str] | None] = all_processes,
) -> dict[str, Any]:
    """SIGKILL every running process that is the tool's (see Started), checking each one again first.

    Repeats until a listing finds none left (`verified`) or `deadline_s` has passed. Each kill goes into
    `killed` as it happens, so a caller that is interrupted keeps the record. `left` is None when the
    processes could not be listed at all.
    """
    killed = {} if killed is None else killed
    end = time.monotonic() + deadline_s
    while True:
        found = owned_processes(runs, listing)
        if not found or time.monotonic() >= end:
            break
        for pid, label in sorted(found.items()):
            if kill_if_owned(pid, runs):
                killed.setdefault(pid, label)
        time.sleep(0.1)
    return {"killed": _labelled(killed), "left": None if found is None else _labelled(found), "verified": found == {}}


def stop_owned(
    runs: Sequence[Started],
    *,
    killed: dict[int, str] | None = None,
    wait_s: float = STOP_WAIT_S,
    listing: Callable[[], dict[int, str] | None] = all_processes,
) -> dict[str, Any]:
    """Stops the tool's processes: asks them to exit first, and kills only those that do not.

    A stop file in each run's working directory asks the test children to exit, and the agent ends once its
    children have. Whatever still runs `wait_s` after the cleanup first saw it gets SIGKILL from sweep(), each
    one checked again right before. A run with children that the tool does not know yet (see
    Started.unaccounted) gets until READY_WAIT_S after its agent started: they may still be starting, and once
    started they see the stop file.

    `running` lists what was found running, `exited` those that then ended without a kill, and `unknown` the
    children that may still run unknown to the tool. `verified` holds only when a listing found none of the
    tool's processes left and no child is unknown.
    """
    first = owned_processes(runs, listing)
    for run in runs:
        with contextlib.suppress(OSError):  # a working directory that is gone stays gone
            (run.workdir / STOP_FILE).touch()
    seen = dict(first or {})
    remaining = first
    end = time.monotonic() + wait_s
    while True:
        now = time.monotonic()
        if not any(run.may_still_start(now) for run in runs) and (not remaining or now >= end):
            break
        time.sleep(0.05)
        remaining = owned_processes(runs, listing)
        if set(remaining or {}) - set(seen):  # one that has only now made itself known gets its wait_s too
            end = max(end, time.monotonic() + wait_s)
        seen.update(remaining or {})
    report = sweep(runs, killed=killed, listing=listing)
    not_exited = {p["pid"] for p in report["killed"]} | {p["pid"] for p in report["left"] or []}
    exited = {pid: label for pid, label in seen.items() if pid not in not_exited}
    unknown = [label for run in runs for label in run.unaccounted()]
    return {
        "running": None if first is None else _labelled(seen),
        "exited": _labelled(exited),
        **report,
        "unknown": unknown,
        "verified": report["verified"] and not unknown,
    }


class StopRequest:
    """The handler for Ctrl-C, SIGTERM and SIGHUP while the scenarios run.

    The first of them raises KeyboardInterrupt, which stops the run and starts the cleanup. Any that follow are
    only noted: raising again could cut that cleanup short, or escape the loop that retries it.
    """

    def __init__(self) -> None:
        self.received: list[int] = []

    def __call__(self, signum: int, _frame: Any) -> None:
        self.received.append(signum)
        if len(self.received) == 1:
            raise KeyboardInterrupt(f"signal {signum}")


@contextlib.contextmanager
def interrupts_held() -> Iterator[None]:
    """Holds Ctrl-C, SIGTERM and SIGHUP back during the block, then delivers them to the handlers from before it.

    The tool starts each agent inside one, so that no interrupt can come between starting it and noting its PID; it
    creates its workspace inside one, so that the final cleanup always knows it; and each scenario's own cleanup runs
    inside one, so that it is never cut short with what it found and killed unrecorded.
    Only the handlers change, never the signal mask, which the agent would inherit. As the handlers are Python
    functions, the agent starts with these signals at their default actions (exec resets caught signals), even
    if this process was started with one of them ignored: an agent that ignored SIGTERM would turn timeout_expiry
    into timeout_expiry_stubborn.
    """
    if threading.current_thread() is not threading.main_thread():  # only the main thread gets signals
        yield
        return
    held: list[int] = []
    previous: dict[int, Any] = {}
    try:
        for signum in STOP_SIGNALS:
            previous[signum] = signal.signal(signum, lambda received, _frame: held.append(received))
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)
        for signum in dict.fromkeys(held):
            signal.raise_signal(signum)


def _set_handlers(handler: Any, signums: Iterable[int]) -> None:
    if threading.current_thread() is threading.main_thread():  # only the main thread may set them
        for signum in signums:
            signal.signal(signum, handler)


class LocalExitWatcher:
    """Notes the moment a worker process exits. Popen.poll() is safe next to a wait() on another thread."""

    def __init__(self, popen: subprocess.Popen[str]):
        self._popen = popen
        self.exited_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            if self._popen.poll() is not None:
                self.exited_at = time.monotonic()
                return

    def after(self, reference: float) -> float | None:
        return None if self.exited_at is None else round(self.exited_at - reference, 1)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class Communicator:
    """process.communicate() on its own thread, as the dispatcher's task thread waits on a run."""

    def __init__(self, process: Any, timeout: float):
        self.started_at = time.monotonic()
        self.returned_at: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, args=(process, timeout), daemon=True)
        self._thread.start()

    def _run(self, process: Any, timeout: float) -> None:
        try:
            self.result = process.communicate(timeout=timeout)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.returned_at = time.monotonic()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def after(self, reference: float) -> float | None:
        return None if self.returned_at is None else round(self.returned_at - reference, 1)


@dataclass(frozen=True)
class Listing:
    """One listing of a run's test processes: when it started and ended (time.monotonic()), and the roles it showed."""

    start: float
    end: float
    roles: frozenset[str]


@dataclass(frozen=True)
class Sent:
    """A signal that LocalProcess sent to a run: when (time.monotonic(), taken right before it went out), which one,
    the process group it went to (None if the agent was gone by then: LocalProcess then sends nothing), and whether
    the agent had been reaped by the time LocalProcess's send returned. Only then can LocalProcess have found no
    group and sent nothing (see outlived)."""

    at: float
    sig: int
    group: int | None
    reaped: bool = False


def record_signals(process: Any, sent: list[Sent]) -> None:
    """Makes `process`, a LocalProcess, note in `sent` each signal that it sends, then send it as it would.

    LocalProcess sends every signal through self._signal_group(process, sig), so an attribute of that name on the
    instance takes the place of the class's own, which still does the sending. If LocalProcess ever sends one some
    other way, nothing is noted, and its scenarios report errors rather than results (see unexplained)."""
    send = process._signal_group

    def noting(popen: subprocess.Popen[str], sig: int) -> None:
        try:
            group: int | None = os.getpgid(popen.pid)
        except OSError:  # gone and reaped: LocalProcess falls back to Popen.send_signal, which then sends nothing
            group = None
        at = time.monotonic()
        try:
            send(popen, sig)
        finally:
            sent.append(Sent(at, sig, group, reaped=popen.returncode is not None))

    process._signal_group = noting


def process_groups(run: Started) -> dict[str, int | None]:
    """Role -> process group of each of the run's processes that the tool knows (None for one that is gone)."""
    groups: dict[str, int | None] = {}
    for pid, label in run.pids.items():
        try:
            groups[label.rpartition(".")[2]] = os.getpgid(pid)
        except OSError:
            groups[label.rpartition(".")[2]] = None
    return groups


def ends_by_construction(role: str, sig: int, *, ignore_term: bool) -> bool:
    """Whether `sig` ends the test process `role`, by the way the tool builds them.

    SIGKILL ends any of them. SIGTERM never ends the noterm child, and ends none of them in the stubborn run, whose
    agent ignores it (`trap '' TERM`) and whose children inherit that. Where this says that a signal does not end a
    process and it does, the result is an error, never a wrong answer."""
    if sig == signal.SIGKILL:
        return True
    return sig == signal.SIGTERM and not ignore_term and role != "noterm"


def unexplained(
    listings: Sequence[Listing],
    sent: Sequence[Sent],
    groups: Mapping[str, int | None],
    survivors: Sequence[str],
    *,
    ignore_term: bool,
    agent_returncode: int | None,
) -> list[str]:
    """The test processes that are gone at the end of a scenario without a signal from the kill path to explain it.

    A test process that is not among the `survivors` was ended by the path only if the path sent it a signal (`sent`)
    that went to that process's group, that ends that process (ends_by_construction), and that it sent no earlier
    than EXIT_LAG_S before the start of the last listing that still showed the process, and before the end of the
    first listing after that which no longer did. The agent's exit status must also be that signal. Anything else
    ended it, and the survivors would be undercounted: a kill from outside, or one of the children's own exits.

    A child in its own session (setsid) is in no group that the path signals, so its end is never explained. The
    survivors get the converse check in outlived."""
    lost = []
    for role in LOCAL_ROLES:
        if role in survivors:
            continue
        last_seen = max((listing.start for listing in listings if role in listing.roles), default=None)
        if last_seen is None:
            lost.append(role)
            continue
        gone_by = min(
            (listing.end for listing in listings if role not in listing.roles and listing.end > last_seen),
            default=-math.inf,  # no listing missed it: nothing can explain that it is gone
        )
        group = groups.get(role)
        if not any(
            group is not None
            and s.group == group
            and ends_by_construction(role, s.sig, ignore_term=ignore_term)
            and last_seen - EXIT_LAG_S <= s.at < gone_by
            and (role != "agent" or agent_returncode == -s.sig)
            for s in sent
        ):
            lost.append(role)
    return lost


def outlived(
    sent: Sequence[Sent], groups: Mapping[str, int | None], survivors: Sequence[str], *, ignore_term: bool
) -> list[str]:
    """The survivors that a noted signal to their own process group ends by construction (ends_by_construction),
    among the signals sent when the agent had already been reaped (Sent.reaped).

    Counted as survivors, they would be a wrong answer: that signal never reached them. record_signals notes a
    signal with the agent's group right before LocalProcess sends it. If the agent is ended from outside and reaped
    in that instant, LocalProcess finds no group and falls back to Popen.send_signal, which sends nothing to a
    reaped process, so the rest of the group never gets the signal that was noted. A signal sent while the agent
    was not yet reaped was delivered as LocalProcess sends it, so what survives it is the answer, even if a changed
    LocalProcess signalled the agent alone. A delivered signal that reaches the group ends what it ends, so marking
    one that the agent's quick reap made look undelivered never flags a true survivor."""
    return [
        role
        for role in LOCAL_ROLES
        if role in survivors
        and groups.get(role) is not None
        and any(
            s.reaped and s.group == groups.get(role) and ends_by_construction(role, s.sig, ignore_term=ignore_term)
            for s in sent
        )
    ]


def outlived_problem(roles: Sequence[str]) -> str:
    one = len(roles) == 1
    return (
        f"{', '.join(roles)} outlived a signal that was noted for {'its' if one else 'their'} process group "
        f"and that ends {'it' if one else 'them'}"
    )


def shown_before(listings: Sequence[Listing], moment: float) -> list[str]:
    """The roles that the last listing to end before `moment` showed (none, if no listing ended before it)."""
    last = max((listing for listing in listings if listing.end < moment), key=lambda listing: listing.end, default=None)
    return [role for role in LOCAL_ROLES if last is not None and role in last.roles]


class Monitor:
    """Lists one run's test processes about every POLL_S on a thread of its own, until stopped, so that the run's
    listings leave no longer gap than that (see unexplained)."""

    def __init__(self, list_roles: Callable[[], Any]):
        self._list = list_roles
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(POLL_S):
            with contextlib.suppress(Exception):  # a listing that failed leaves a gap, and the next one may work
                self._list()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


@dataclass
class LocalRun:
    started: Started  # how the tool knows this run's processes
    process: Any  # LocalProcess
    popen: subprocess.Popen[str]
    watcher: LocalExitWatcher
    ignore_term: bool = False  # the stubborn run: its agent ignores SIGTERM, and its children inherit that
    waiter: Communicator | None = None
    killed: dict[int, str] = field(default_factory=dict)  # what its cleanup killed, recorded as it goes
    cleanup: dict[str, Any] | None = None
    listings: list[Listing] = field(default_factory=list)  # every listing of its test processes, as it ended
    sent: list[Sent] = field(default_factory=list)  # every signal that LocalProcess sent it
    groups: dict[str, int | None] = field(default_factory=dict)  # role -> process group, once all have started
    monitor: Monitor | None = None  # lists its test processes from then on, until its survivors are counted

    @property
    def workdir(self) -> Path:
        return self.started.workdir

    def stop_monitor(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()


def stopped_before(met: Sequence[str]) -> str | None:
    """The problem, if a test process was not running when the path under test started: `met` lists the roles that
    were. Whatever stopped that one, it was not the path, so the path cannot be credited with it."""
    missing = [role for role in LOCAL_ROLES if role not in met]
    return f"{', '.join(missing)} {STOPPED_BEFORE}" if missing else None


def stopped_after(left: Sequence[str] | None, survivors: Sequence[str]) -> str | None:
    """The problem, if a test process that the path under test left running (`left`, listed once it had run) was gone
    by the time the survivors were counted.

    LocalProcess signals only until the agent has exited (a stubborn agent is ended by the SIGKILL to its group), and
    kill() and cancel() only until they return, so something else stopped it: the survivors would be undercounted."""
    lost = [role for role in left or () if role not in survivors]
    return f"{', '.join(lost)} {STOPPED_AFTER}" if lost else None


class LocalHarness:
    """Cairn's LocalBackend, LocalProcess and TaskCancellation, run against the tool's own processes."""

    def __init__(self, timeout_s: int, kill_after_s: int, marker: str):
        from cairn.dispatcher.config import LocalConfig
        from cairn.dispatcher.runtime import local_process
        from cairn.dispatcher.runtime.cancellation import TaskCancellation
        from cairn.dispatcher.runtime.local_backend import LocalBackend
        from cairn.dispatcher.tasks.common import communicate_timeout, did_timeout

        self.timeout_s = timeout_s
        self.kill_after_s = kill_after_s
        self.term_grace_s = max(1, kill_after_s)  # LocalProcess waits at least 1 s before SIGKILL
        self.marker = marker
        self.lifetime_s = child_lifetime_s(timeout_s, kill_after_s)
        self.task_cancellation: Callable[[], Any] = TaskCancellation
        self.communicate_timeout: Callable[[int], int] = communicate_timeout
        self.did_timeout: Callable[[Any], bool] = did_timeout
        # Once the agent has exited, LocalProcess waits up to this long to reap it and read its output.
        self.after_exit_s = float(getattr(local_process, "FORCE_KILL_REAP_TIMEOUT_SECONDS", 2)) + 2 * float(
            getattr(local_process, "STREAM_JOIN_TIMEOUT_SECONDS", 5)
        )
        self._backend_for: Callable[[str], Any] = lambda root: LocalBackend(LocalConfig(workspace_root=root))
        # The scratch workspace and the LocalBackend in it, created by the first run: nothing exists before then.
        self.workspace: Path | None = None
        self.backend: Any = None
        self._runs: list[LocalRun] = []
        # How the tool knows every run's processes (see Started), registered before the agent starts, so that the
        # final cleanup also covers a run that failed before the scenario got hold of it.
        self.started: list[Started] = []
        self.final_killed: dict[int, str] = {}

    def budget_s(self, timeout_s: float) -> float:
        """How long to wait for communicate() after its clock starts before giving up on it."""
        return timeout_s + self.term_grace_s + self.after_exit_s + 10

    def ensure_workspace(self) -> Path:
        """The scratch workspace, created on first use, inside a new temporary directory that only this user may
        enter. A Ctrl-C cannot come between creating it and noting it, so the final cleanup always removes it."""
        if self.workspace is None:
            with interrupts_held():
                self.workspace = Path(tempfile.mkdtemp(prefix="cairn-phase0-local-"))
                self.backend = self._backend_for(str(self.workspace))
        return self.workspace

    @contextlib.contextmanager
    def run(self, name: str, *, timeout_s: int, ignore_term: bool = False) -> Iterator[LocalRun]:
        workspace = self.ensure_workspace()
        if (workspace / name).exists():  # its files would be taken for this run's
            raise RuntimeError(f"{name} has already run in {workspace}")
        tag = f"{self.marker}.{name}"
        workdir = Path(self.backend.ensure_running(name))
        tool_pid = os.getpid()
        tails = owned_tails(self.marker, name, ignore_term=ignore_term, tool_pid=tool_pid, lifetime_s=self.lifetime_s)
        started = Started(name, workdir, tails)
        self.started.append(started)  # before start(), so the final cleanup covers this run whatever happens next
        command = local_agent_command(tag, ignore_term=ignore_term, tool_pid=tool_pid, lifetime_s=self.lifetime_s)
        process = self.backend.build_exec_process(
            str(workdir),
            {PYTHON_ENV: sys.executable},
            command,
            timeout_seconds=timeout_s,
            kill_after_seconds=self.kill_after_s,
        )
        sent: list[Sent] = []
        record_signals(process, sent)
        with interrupts_held():  # no Ctrl-C between starting the agent and noting its PID
            try:
                process.start()
            finally:  # also when start() fails after Popen, so that the final cleanup knows the agent
                popen: subprocess.Popen[str] | None = process._process
                if popen is not None:
                    started.pids[popen.pid] = f"{name}.agent"
                    started.agent, started.agent_started_at = popen, time.monotonic()
        if popen is None:
            raise RuntimeError("LocalProcess.start() left no process to watch")
        run = LocalRun(started, process, popen, LocalExitWatcher(popen), ignore_term=ignore_term, sent=sent)
        self._runs.append(run)
        try:
            yield run
        finally:
            self.finish(run)

    def finish(self, run: LocalRun) -> dict[str, Any]:
        """Stops what the scenario left running (see stop_owned), then reaps the agent.

        A Ctrl-C, SIGTERM or SIGHUP that comes meanwhile is held back until the end (see interrupts_held), so that
        it never cuts this cleanup short: what it found and killed stays recorded for the summary."""
        with interrupts_held():
            run.stop_monitor()
            if run.cleanup is None:
                run.cleanup = stop_owned([run.started], killed=run.killed)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    run.popen.wait(timeout=5)
                if run.waiter is not None:
                    run.waiter.join(5)
                run.watcher.stop()
            return run.cleanup

    def scenario_cleanup(self) -> dict[str, int]:
        """How many processes the scenarios' own cleanups found still running, and how those ended."""
        totals = {"running": 0, "exited": 0, "killed": 0}
        for run in self._runs:
            if run.cleanup is not None:
                totals["running"] += len(run.cleanup["running"] or [])
                totals["exited"] += len(run.cleanup["exited"])
            totals["killed"] += len(run.killed)  # also what a cleanup that was interrupted had killed
        return totals

    def close(self) -> dict[str, Any]:
        """The final cleanup over every run registered (see stop_owned); then the scratch workspace goes."""
        for run in self._runs:
            run.stop_monitor()
        final = stop_owned(self.started, killed=self.final_killed)
        for run in self._runs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                run.popen.wait(timeout=2)
            run.watcher.stop()
        if self.workspace is not None:
            shutil.rmtree(self.workspace, ignore_errors=True)
        return final

    def running_roles(self, run: LocalRun) -> list[str]:
        """The roles of the run's test processes that run now. Each listing is noted in run.listings."""
        start = time.monotonic()
        found = owned_processes([run.started])
        if found is None:
            raise RuntimeError("could not list the processes on this machine")
        shown = roles([{"role": label.rpartition(".")[2]} for label in found.values()])
        run.listings.append(Listing(start, time.monotonic(), frozenset(shown)))
        return shown

    def wait_for_children(self, run: LocalRun) -> tuple[list[str], str | None]:
        """Waits until the agent and its three children all run and each child has set itself up.

        Returns the roles running at the end, and why the scenario cannot go on, if it cannot: some of them did
        not start or set themselves up in time, one stopped after it had, or the run's working directory was removed.
        Once they all run, it notes their process groups, and a Monitor lists them from then on (see unexplained).
        """
        deadline = time.monotonic() + READY_WAIT_S
        while True:
            # Read before the listing: a child writes its ready file after its pid file, so the listing has learned
            # the PID of every child in `ready`, and one of them that it does not show has stopped.
            ready = [role for role in LOCAL_CHILD_ROLES if (run.workdir / f"ready-{role}").exists()]
            running = self.running_roles(run)
            if not run.workdir.is_dir():
                return running, WORKDIR_REMOVED
            gone = [role for role in ready if role not in running]
            if gone:
                return running, self.disturbance(run) or f"{', '.join(gone)} {STOPPED_BEFORE}"
            missing = [role for role in LOCAL_ROLES if role not in running or (role != "agent" and role not in ready)]
            if not missing:
                run.groups = process_groups(run.started)
                run.monitor = Monitor(lambda: self.running_roles(run))
                return running, None
            returncode = run.popen.poll()
            if returncode is not None:  # its children cannot all start and set themselves up any more
                waiting = [role for role in missing if role != "agent"]
                before = (
                    f"{', '.join(waiting)} {'was' if len(waiting) == 1 else 'were'} ready"
                    if waiting
                    else "the scenario"
                )
                return running, f"the agent exited (status {returncode}) before {before}"
            if time.monotonic() >= deadline:
                return running, f"{', '.join(missing)} did not start within {READY_WAIT_S:g} s"
            time.sleep(0.1)

    def disturbance(self, run: LocalRun) -> str | None:
        """Why a scenario's results cannot be trusted, if something besides the path under test may have ended its
        test processes: the run's working directory was removed, which makes the test children exit (the tool
        itself removes it only after the final cleanup), or a test child exited on its own for another reason,
        such as its lifetime running out while the tool was suspended (it leaves an exited-<role> file then)."""
        if not run.workdir.is_dir():
            return WORKDIR_REMOVED
        exited = [role for role in LOCAL_CHILD_ROLES if (run.workdir / f"exited-{role}").exists()]
        if exited:
            return f"{', '.join(exited)} exited on {'its' if len(exited) == 1 else 'their'} own during the scenario"
        return None

    def not_ready(self, run: LocalRun) -> str | None:
        """Why the path under test cannot be measured, if a test process no longer runs right before it starts (see
        stopped_before): the cause, where disturbance can name it."""
        problem = stopped_before(self.running_roles(run))
        return problem and (self.disturbance(run) or problem)

    def invalid(self, run: LocalRun, started: list[str], problem: str) -> dict[str, Any]:
        """A scenario that cannot give results: an error, never results, and its cleanup."""
        return {"error": f"{problem}; results not valid", "started": started, "cleanup": self.finish(run)}

    def checked(self, run: LocalRun, report: dict[str, Any], *problems: str | None) -> dict[str, Any]:
        """The scenario's report with its cleanup, once its survivors are counted; an error instead if it has no
        result or a test process may have ended some other way than through the path under test: see disturbance,
        the problems that the scenario found once the path had run (not_returned, stopped_after, NOT_TIMED_OUT), and
        stopped_otherwise. Those found before it ran (stopped_before) end the scenario right there."""
        run.stop_monitor()
        problem = (
            self.disturbance(run)
            or next((p for p in problems if p), None)
            or self.stopped_otherwise(run, report["survivors"])
        )
        if problem:
            return self.invalid(run, report["started"], problem)
        report["cleanup"] = self.finish(run)
        return report

    def stopped_otherwise(self, run: LocalRun, survivors: Sequence[str]) -> str | None:
        """The problem, if a test process is gone without a signal from the kill path to explain it (see unexplained),
        or survived one that ends it (see outlived). Called once the survivors are counted: the listing that counted
        them is the run's last."""
        lost = unexplained(
            list(run.listings),
            list(run.sent),
            run.groups,
            survivors,
            ignore_term=run.ignore_term,
            agent_returncode=run.popen.poll(),
        )
        if lost:
            return f"{', '.join(lost)} {STOPPED_OTHERWISE}"
        missed = outlived(list(run.sent), run.groups, survivors, ignore_term=run.ignore_term)
        return outlived_problem(missed) if missed else None

    def wait_for_exit(self, run: LocalRun, since: float, limit_s: float) -> bool:
        """Waits until the agent has exited, but no longer than `limit_s` after `since`; whether it has."""
        while run.watcher.exited_at is None and time.monotonic() - since < limit_s:
            time.sleep(0.05)
        return run.watcher.exited_at is not None

    def left_running(self, run: LocalRun) -> list[str]:
        """What runs SETTLE_S from now. Called once the path under test has run, it is what the path left running,
        which must all still run when the survivors are counted (see stopped_after)."""
        time.sleep(SETTLE_S)
        return self.running_roles(run)

    def not_returned(self, result: Any, waiter: Communicator, limit_s: float, after: str) -> str | None:
        """Why the scenario has no results, if communicate() gave none (`result`, read once by the scenario): it
        raised, or it had not returned `limit_s` after `after`, the scenario's budget, in which case the dispatcher's
        task thread would hang."""
        if result is not None:
            return None
        if waiter.error is not None:
            return f"communicate() raised {waiter.error}"
        return f"communicate() had not returned {limit_s:g} s after {after}, so the task thread would hang"

    def result_fields(self, result: Any, waiter: Communicator) -> dict[str, Any]:
        if result is None:
            return {"communicate_returned": False, "communicate_error": waiter.error}
        return {
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "cancel_reason": result.cancel_reason,
            "did_timeout": self.did_timeout(result),
        }

    # -- scenarios ---------------------------------------------------------------------------

    def timeout_expiry(self, *, ignore_term: bool = False) -> dict[str, Any]:
        name = "timeout_expiry_stubborn" if ignore_term else "timeout_expiry"
        with self.run(name, timeout_s=self.timeout_s, ignore_term=ignore_term) as run:
            started, problem = self.wait_for_children(run)
            if problem:
                return self.invalid(run, started, problem)
            # LocalProcess's clock starts in communicate(); here it starts once the children are up.
            waiter = run.waiter = Communicator(run.process, self.communicate_timeout(self.timeout_s))
            fires_at = waiter.started_at + self.timeout_s  # the timeout cannot act any earlier
            deadline = waiter.started_at + self.budget_s(self.timeout_s)
            # Meanwhile the monitor lists the test processes about every POLL_S s: the last listing that ended before
            # then is what the timeout met. One missing from it was stopped by something else, and so was an agent
            # that exited before then.
            while (remaining := fires_at - time.monotonic()) > 0:
                time.sleep(remaining)
            met = shown_before(list(run.listings), fires_at)
            exited_at = run.watcher.exited_at  # an exit noticed only later shows as timed_out=False (NOT_TIMED_OUT)
            early = exited_at is not None and exited_at < fires_at
            problem = stopped_before(met) or (f"agent {STOPPED_BEFORE}" if early else None)
            if problem:
                return self.invalid(run, started, self.disturbance(run) or problem)
            while run.watcher.exited_at is None and waiter.returned_at is None and time.monotonic() < deadline:
                time.sleep(0.05)
            left = None if run.watcher.exited_at is None else self.left_running(run)
            waiter.join(max(0.0, deadline - time.monotonic()))
            result = waiter.result  # read once: what the report and the checks below see
            time.sleep(SETTLE_S)
            survivors = self.running_roles(run)
            return self.checked(
                run,
                {
                    "timeout": f"LocalProcess timeout {self.timeout_s} s, SIGKILL after a {self.term_grace_s} s grace",
                    "timeout_s": self.timeout_s,
                    **self.result_fields(result, waiter),
                    "exec_ended_after_s": run.watcher.after(waiter.started_at),
                    "ended_after_s": waiter.after(waiter.started_at),
                    "exec_running_after": run.popen.poll() is None,
                    "started": started,
                    "survivors": survivors,
                },
                self.not_returned(result, waiter, self.budget_s(self.timeout_s), "it was called"),
                NOT_TIMED_OUT if result is not None and not result.timed_out else None,
                stopped_after(left, survivors),
            )

    def local_kill(self) -> dict[str, Any]:
        with self.run("local_kill", timeout_s=LONG_TIMEOUT_S) as run:
            started, problem = self.wait_for_children(run)
            if problem:
                return self.invalid(run, started, problem)
            waiter = run.waiter = Communicator(run.process, self.communicate_timeout(LONG_TIMEOUT_S))
            time.sleep(SETTLE_S)  # communicate() is waiting now, as the task thread would be
            problem = self.not_ready(run)  # right before the call: what is gone now, the path did not stop
            if problem:
                return self.invalid(run, started, problem)
            kill_at = time.monotonic()
            run.process.kill()  # HeartbeatLease._fail makes this call from the heartbeat thread
            kill_call_s = time.monotonic() - kill_at
            running_after_kill = not self.wait_for_exit(run, kill_at, KILL_WAIT_S)
            left = self.left_running(run)
            waiter.join(self.budget_s(0))
            result = waiter.result  # read once: what the report and the checks below see
            time.sleep(SETTLE_S)
            survivors = self.running_roles(run)
            return self.checked(
                run,
                {
                    "timeout_s": LONG_TIMEOUT_S,
                    **self.result_fields(result, waiter),
                    "kill_call_s": round(kill_call_s, 2),
                    "exec_running_after_kill": running_after_kill,
                    "stopped_after_s": run.watcher.after(kill_at),
                    "communicate_returned_after_kill_s": waiter.after(kill_at),
                    "started": started,
                    "survivors": survivors,
                },
                self.not_returned(result, waiter, self.budget_s(0), "kill()"),
                stopped_after(left, survivors),
            )

    def cancel(self) -> dict[str, Any]:
        cancellation = self.task_cancellation()
        with self.run("cancel", timeout_s=LONG_TIMEOUT_S) as run:
            cancellation.attach_process(run.process)  # run_worker_process attaches it right after start()
            try:
                started, problem = self.wait_for_children(run)
                if problem:
                    return self.invalid(run, started, problem)
                waiter = run.waiter = Communicator(run.process, self.communicate_timeout(LONG_TIMEOUT_S))
                time.sleep(SETTLE_S)
                problem = self.not_ready(run)  # right before the call: what is gone now, the path did not stop
                if problem:
                    return self.invalid(run, started, problem)
                cancelled_at = time.monotonic()
                # What the scheduler does for a project that is no longer active (_cancel_inactive_tasks).
                cancellation.cancel(CANCEL_REASON)
                cancel_call_s = time.monotonic() - cancelled_at
                self.wait_for_exit(run, cancelled_at, KILL_WAIT_S)
                left = self.left_running(run)
                waiter.join(self.budget_s(0))
                result = waiter.result  # read once: what the report and the checks below see
            finally:
                cancellation.attach_process(None)  # as run_worker_process does once communicate() returns
            time.sleep(SETTLE_S)
            survivors = self.running_roles(run)
            return self.checked(
                run,
                {
                    "timeout_s": LONG_TIMEOUT_S,
                    **self.result_fields(result, waiter),
                    "cancel_call_s": round(cancel_call_s, 2),
                    "exec_ended_after_cancel_s": run.watcher.after(cancelled_at),
                    "communicate_returned_after_cancel_s": waiter.after(cancelled_at),
                    "exec_running_after": run.popen.poll() is None,
                    "started": started,
                    "survivors": survivors,
                },
                self.not_returned(result, waiter, self.budget_s(0), "the cancel"),
                stopped_after(left, survivors),
            )


LOCAL_SCENARIOS: dict[str, Callable[[LocalHarness], dict[str, Any]]] = {
    "timeout_expiry": lambda h: h.timeout_expiry(),
    "timeout_expiry_stubborn": lambda h: h.timeout_expiry(ignore_term=True),
    "local_kill": lambda h: h.local_kill(),
    "cancel": lambda h: h.cancel(),
}


def _alive(r: dict[str, Any]) -> str:
    return ", ".join(r["survivors"]) or "nothing"


def _pipe_wait(r: dict[str, Any], exited: float | None, returned: float | None) -> str:
    """How long communicate() went on after the agent had exited, if that was a second or more."""
    if exited is None or returned is None or returned - exited < 1:
        return ""
    waited = round(returned - exited, 1)
    if r.get("survivors"):
        return f", having waited {waited} s for output pipes that surviving children still held open"
    return f", {waited} s after the agent exited"


def local_conclusions(results: dict[str, dict[str, Any]]) -> list[str]:
    ok = {name: r for name, r in results.items() if "error" not in r and "returncode" in r}
    lines: list[str] = []
    for name in ("timeout_expiry", "timeout_expiry_stubborn"):
        r = ok.get(name)
        if r:
            how = " (SIGKILL to its process group after the grace period)" if r["returncode"] == -signal.SIGKILL else ""
            lines.append(
                f"{name}: LocalProcess's own timeout ({r['timeout_s']} s) ended the run with exit {r['returncode']} "
                f"(timed_out={r['timed_out']}, did_timeout={r['did_timeout']}); the agent exited after "
                f"{_fmt_s(r.get('exec_ended_after_s'))}{how}, and communicate() returned after "
                f"{_fmt_s(r.get('ended_after_s'))}{_pipe_wait(r, r.get('exec_ended_after_s'), r.get('ended_after_s'))}; "
                f"still alive afterwards: {_alive(r)}."
            )
    r = ok.get("local_kill")
    if r:
        if r["exec_running_after_kill"]:
            stopped = f"LocalProcess.kill() did not stop the agent within {KILL_WAIT_S:g} s"
        else:
            stopped = (
                f"LocalProcess.kill(), which a lost heartbeat lease calls, stopped the agent "
                f"{_fmt_s(r.get('stopped_after_s'))} after the call (which took {r['kill_call_s']} s)"
            )
        lines.append(
            f"local_kill: {stopped}; communicate() returned {_fmt_s(r.get('communicate_returned_after_kill_s'))} after "
            f"kill() with exit {r['returncode']}, timed_out={r['timed_out']}, cancelled={r['cancelled']}, "
            f"did_timeout={r['did_timeout']}"
            f"{_pipe_wait(r, r.get('stopped_after_s'), r.get('communicate_returned_after_kill_s'))}; "
            f"still alive afterwards: {_alive(r)}."
        )
    r = ok.get("cancel")
    if r:
        lines.append(
            f"cancel: TaskCancellation.cancel() reached LocalProcess.cancel() and the agent exited "
            f"{_fmt_s(r.get('exec_ended_after_cancel_s'))} later; communicate() returned "
            f"{_fmt_s(r.get('communicate_returned_after_cancel_s'))} after the cancel with exit {r['returncode']}, "
            f"cancelled={r['cancelled']} ({r['cancel_reason']!r})"
            f"{_pipe_wait(r, r.get('exec_ended_after_cancel_s'), r.get('communicate_returned_after_cancel_s'))}; "
            f"still alive afterwards: {_alive(r)}."
        )
    term_paths = [name for name in ("timeout_expiry", "local_kill", "cancel") if name in ok]
    noterm = [name for name in term_paths if "noterm" in ok[name]["survivors"]]
    stubborn = ok.get("timeout_expiry_stubborn")
    if noterm and stubborn and "noterm" not in stubborn["survivors"]:
        lines.append(
            f"A child that ignores SIGTERM survived {', '.join(noterm)}. It died only in timeout_expiry_stubborn: "
            "LocalProcess sends SIGKILL to the process group only when the agent itself is still alive after "
            "the grace period, which happened only when the agent ignored SIGTERM too."
        )
    elif noterm:
        lines.append(f"A child that ignores SIGTERM survived {', '.join(noterm)}.")
    setsid = [name for name in ok if "setsid" in ok[name]["survivors"]]
    if setsid and len(setsid) == len(ok) == len(results):
        lines.append(
            "A child in its own session (os.setsid) survived every scenario: "
            "LocalProcess signals only the agent's process group."
        )
    elif setsid:
        lines.append(f"A child in its own session (os.setsid) survived {', '.join(setsid)}.")
    if any(r["survivors"] for r in ok.values()):
        lines.append(
            "Survivors are processes on the host, and nothing in LocalBackend stops them later (there is no "
            "container to remove): in a real run they keep running after the task has ended, until they exit "
            "by themselves. This tool stopped them after each scenario."
        )
    if any(isinstance(r["returncode"], int) and r["returncode"] < 0 for r in ok.values()):
        lines.append(
            "A run ended by a signal reports a negative exit code (-15 for SIGTERM, -9 for SIGKILL), never "
            "124 or 137, so did_timeout() rests on the timed_out flag alone."
        )
    return lines


def kill_pattern(marker: str) -> str:
    """An extended regex for `pgrep -f`/`pkill -f` that matches only command lines ending as this run's do:
    `<marker>.<scenario>[.<role>] <tool pid> <lifetime>`."""
    return f"{marker}\\.[a-z_.]+ [0-9]+ [0-9]+$"


def pgrep_command(marker: str, platform: str = sys.platform) -> str:
    """The command that lists the processes whose command line mentions `marker`, each with its whole command line.

    procps pgrep (Linux) prints that with -a, where its -l prints only the process name; BSD pgrep (macOS) prints it
    with -l when -f is given, and its -a means something else (include ancestors)."""
    return f"pgrep -{'fa' if platform.startswith('linux') else 'fl'} {marker}"


def _how_they_ended(exited: int, killed: int) -> str:
    return f"{exited} exited when asked to stop, {killed} had to be killed"


def describe_cleanup(final: dict[str, Any], marker: str, scenarios: dict[str, int]) -> list[str]:
    """The summary of the cleanup: `final` is the final cleanup's report, `scenarios` the scenarios' totals."""
    parts = []
    if scenarios["running"] or scenarios["killed"]:
        parts.append(
            f"{scenarios['running']} test process(es) were still running when their scenario ended "
            f"({_how_they_ended(scenarios['exited'], scenarios['killed'])})"
        )
    found = final.get("running") or final.get("killed") or []
    if found:
        parts.append(
            f"the final cleanup found {len(found)} ({', '.join(p['process'] for p in found)}): "
            f"{_how_they_ended(len(final.get('exited') or []), len(final.get('killed') or []))}"
        )
    text = "Cleanup: " + ("; ".join(parts) or "no test process was still running at the end")
    if final.get("verified"):
        return [f"{text}; nothing with marker {marker} is running now."]
    problems = []
    if final.get("left") is None:
        problems.append("WARNING: could not list processes to confirm the cleanup.")
    elif final["left"]:
        problems.append(
            f"WARNING: still running: {', '.join(p['process'] for p in final['left'])}. "
            "They exit by themselves within a second once this tool has exited."
        )
    if final.get("unknown"):
        problems.append(
            f"WARNING: cannot confirm that {', '.join(final['unknown'])} stopped: they had not told this tool their "
            "PIDs yet. They exit by themselves as soon as they have started up."
        )
    # No PIDs to kill by hand: by the time someone reads this, a PID may belong to another process.
    return [
        f"{text}.",
        *(problems or ["WARNING: could not confirm the cleanup."]),
        f"To see whether any are left: {pgrep_command(marker)}",
        f"To stop them, once that list shows only this tool's processes: pkill -9 -f '{kill_pattern(marker)}'",
        "(pkill matches command lines that end the way this tool's do, and reads them when it runs, so no PID "
        "noted earlier is involved)",
    ]


def print_local_plan(timeout_s: int, kill_after_s: int, selected: list[str]) -> None:
    tag = f"{LOCAL_MARKER_PREFIX}<random>.<scenario>"
    print("mode: local (LocalBackend and LocalProcess on this machine, no Docker)")
    print(f"agent: /bin/sh -c <agent script> {tag}.agent <child code> {tag} <tool pid> <lifetime>")
    print(f"  with {PYTHON_ENV}=<this python> in its environment")
    print("agent script:\n  " + local_agent_script(ignore_term=False))
    print(
        f"children plain, noterm and setsid, each: <this python> -I -c <child code> {tag}.<role> <tool pid> <lifetime>"
    )
    print("child code (on one line, with newlines as ~):\n" + textwrap.indent(LOCAL_CHILD_SOURCE, "  "), end="")
    print(
        f"timeout scenarios: LocalProcess timeout {timeout_s} s, SIGKILL after a {max(1, kill_after_s)} s grace; "
        f"local_kill and cancel: timeout {LONG_TIMEOUT_S} s"
    )
    print(f"scenarios: {', '.join(selected)}")
    print(
        "safety: a process is this run's only if its PID is one this tool learned when starting it (the agent's "
        "from Popen; each child writes its own to pid-<role> in its working directory) and its command line still "
        "ends with exactly the one this tool built for it (all but argv[0]); its processes are first asked to exit "
        f"(a {STOP_FILE} file), and only those still running {STOP_WAIT_S:g} s later get SIGKILL, each checked "
        f"again right before; children also exit once this tool's process or workspace is gone, or after "
        f"{child_lifetime_s(timeout_s, kill_after_s)} s; a scenario in which that happened, or in which a test "
        "process ended without a signal from the kill path to explain it, reports an error"
    )


def run_local(parser: argparse.ArgumentParser, args: argparse.Namespace, *, restore_signals: bool = True) -> int:
    """Local mode. The first Ctrl-C, SIGTERM or SIGHUP stops the run; any that follow, and any from the cleanup
    on, are ignored, so that the cleanup and the summary finish. restore_signals puts the previous handlers back
    before returning, for a caller in this same process; the command line keeps them ignored until the process
    exits, so a late Ctrl-C cannot cut the exit short (it would end the process by SIGINT instead of with code
    130)."""
    if args.image or args.config:
        parser.error("--image and --config are for container mode; local mode runs on this machine")
    selected = list(dict.fromkeys(args.scenario or LOCAL_SCENARIOS))  # each scenario once, in the order given
    wrong = [name for name in selected if name not in LOCAL_SCENARIOS]
    if wrong:
        parser.error(
            f"--scenario {wrong[0]} is a container-mode scenario; local mode has: {', '.join(LOCAL_SCENARIOS)}"
        )
    if args.timeout < 1 or args.kill_after < 0:
        parser.error("--timeout must be at least 1 and --kill-after at least 0")
    json_out = args.json_out or LOCAL_JSON_OUT
    # The results are written last, after a run of a minute or more: a place they cannot go would lose them.
    if not json_out.parent.is_dir():
        parser.error(f"--json-out: directory {json_out.parent} does not exist")
    if json_out.is_dir():
        parser.error(f"--json-out: {json_out} is a directory")
    if args.dry_run:
        print_local_plan(args.timeout, args.kill_after, selected)
        return 0
    if os.name != "posix" or not Path("/bin/sh").exists() or not sys.executable:
        print("local mode needs Linux or macOS: a POSIX /bin/sh and a Python executable", file=sys.stderr)
        return 2

    if (CAIRN_SRC / "cairn").is_dir():
        sys.path.insert(0, str(CAIRN_SRC))  # use the dispatcher code from this checkout
    marker = new_marker()
    saved = {signum: signal.getsignal(signum) for signum in STOP_SIGNALS}
    harness: LocalHarness | None = None
    results: dict[str, dict[str, Any]] = {}
    cleanup: dict[str, Any] = {}
    interrupted = False
    try:
        try:
            try:
                # Before anything exists: from here on Ctrl-C, SIGTERM and SIGHUP stop the run, and the cleanup runs.
                _set_handlers(StopRequest(), STOP_SIGNALS)
                try:
                    harness = LocalHarness(args.timeout, args.kill_after, marker)  # creates nothing yet
                except ImportError as exc:
                    print(
                        f"missing dependency: {exc}. Run this with the Python environment that runs the dispatcher.",
                        file=sys.stderr,
                    )
                    return 2
                print(f"marker: {marker} (in the command line of every process this run starts)", flush=True)
                for name in selected:
                    print(f"running {name} ...", flush=True)
                    try:
                        results[name] = LOCAL_SCENARIOS[name](harness)
                    except Exception as exc:
                        results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                # The cleanup runs whatever happened. From here on Ctrl-C, SIGTERM and SIGHUP are ignored, so the
                # summary prints and the exit code stands. Only the first one raises (StopRequest): if that comes
                # during the cleanup, the cleanup starts again, and nothing else can interrupt it.
                while True:
                    try:
                        _set_handlers(signal.SIG_IGN, STOP_SIGNALS)
                        cleanup = harness.close() if harness is not None else dict(NOTHING_STARTED)
                        break
                    except KeyboardInterrupt:
                        interrupted = True
        except KeyboardInterrupt:
            interrupted = True
        return _finish_local(args, json_out, marker, harness, results, cleanup, interrupted=interrupted)
    finally:
        if restore_signals:
            for signum, handler in saved.items():
                _set_handlers(signal.SIG_DFL if handler is None else handler, (signum,))


def _finish_local(
    args: argparse.Namespace,
    json_out: Path,
    marker: str,
    harness: LocalHarness | None,
    results: dict[str, dict[str, Any]],
    cleanup: dict[str, Any],
    *,
    interrupted: bool,
) -> int:
    """Prints the summary and, unless interrupted, the results and their file. Returns the exit code."""
    totals = harness.scenario_cleanup() if harness is not None else {"running": 0, "exited": 0, "killed": 0}
    cleanup_lines = describe_cleanup(cleanup, marker, totals)
    if interrupted or harness is None:  # no harness: a Ctrl-C, SIGTERM or SIGHUP came before it existed
        with contextlib.suppress(OSError):  # after SIGHUP the terminal may be gone
            print("\ninterrupted; nothing written")
            for line in cleanup_lines:
                print(line)
        return 130

    findings = local_conclusions(results)
    report = {
        "tool": "tools/repair-phase0/kill_check.py",
        "mode": "local",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cairn_commit": _git_commit(),
        "marker": marker,
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "process_listing": listing_method(),
        },
        "settings": {
            "timeout_s": args.timeout,
            "kill_after_s": args.kill_after,
            "term_grace_s": harness.term_grace_s,
            "long_timeout_s": LONG_TIMEOUT_S,
            "child_lifetime_s": harness.lifetime_s,
        },
        "scenarios": results,
        "conclusions": findings,
        "cleanup": cleanup,
    }
    print_table(results, show_flags=True)
    print()
    for line in findings:
        print(f"- {line}")
    print()
    for line in cleanup_lines:
        print(line)
    try:
        json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # its directory was checked before the run, but it can still fail
        print(f"\ncould not write {json_out}: {exc}", file=sys.stderr)
        return 1
    print(f"\nWrote {json_out}")
    failed = any("error" in r for r in results.values())
    return 0 if cleanup.get("verified") and not failed else 1


def image_from_config(path: Path) -> str | None:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    container = data.get("container") or {}
    image = container.get("image") if isinstance(container, dict) else None
    return image if isinstance(image, str) and image else None


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def print_table(results: dict[str, dict[str, Any]], *, show_flags: bool = False) -> None:
    """The results table. show_flags (local mode) adds the timed_out and cancelled flags as a column."""

    def flags_cell(text: str) -> str:
        return f"  {text:<20}" if show_flags else ""

    print(f"\n{'scenario':<26}{'exit':>6}{flags_cell('flags')}{'ended/returned':>16}  {'run still going':<16}survivors")
    for name, r in results.items():
        if "error" in r:
            print(f"{name:<26}  error: {r['error']}")
            continue
        seconds = r.get(
            "ended_after_s",
            r.get(
                "communicate_returned_after_s",
                r.get(
                    "communicate_returned_after_cancel_s",
                    r.get("communicate_returned_after_kill_s", r.get("stopped_after_s")),
                ),
            ),
        )
        running = r.get("exec_running_after", r.get("exec_running_after_kill"))
        flags = ", ".join(flag for flag in ("timed_out", "cancelled") if r.get(flag)) or "-"
        print(
            f"{name:<26}{r.get('returncode', '-')!s:>6}{flags_cell(flags)}{('-' if seconds is None else f'{seconds} s'):>16}  "
            f"{('yes' if running else 'no'):<16}{', '.join(r.get('survivors', [])) or 'nothing'}"
        )


def main(argv: list[str] | None = None, *, restore_signals: bool = True) -> int:
    """The command line. restore_signals: see run_local (container mode does not touch signal handlers)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--mode",
        choices=["container", "local"],
        default="container",
        help="container: workers in Docker, as runtime.execution: container (default); "
        "local: workers as processes on this machine, as runtime.execution: local (no Docker needed)",
    )
    parser.add_argument(
        "--image", help=f"container mode: image to test (default: container.image from --config, else {DEFAULT_IMAGE})"
    )
    parser.add_argument(
        "--config", type=Path, help="container mode: your dispatch.yaml, to test the same image your workers use"
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[*SCENARIOS, *(name for name in LOCAL_SCENARIOS if name not in SCENARIOS)],
        help=f"run only this scenario (repeatable). Container mode: {', '.join(SCENARIOS)}. "
        f"Local mode: {', '.join(LOCAL_SCENARIOS)}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=4,
        help="timeout N for the expiry scenarios: the wrapper's, or LocalProcess's in local mode (default: %(default)s)",
    )
    parser.add_argument(
        "--kill-after",
        type=int,
        default=2,
        help="wrapper kill-after K, or LocalProcess's grace before SIGKILL in local mode "
        "(default: %(default)s; the dispatcher uses 5)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help=f"where to write the results (default: {CONTAINER_JSON_OUT}, or {LOCAL_JSON_OUT} in local mode)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would run and exit, without Docker or starting anything"
    )
    args = parser.parse_args(argv)
    if args.mode == "local":
        return run_local(parser, args, restore_signals=restore_signals)
    wrong = [name for name in args.scenario or [] if name not in SCENARIOS]
    if wrong:
        parser.error(f"--scenario {wrong[0]} is a local-mode scenario; container mode has: {', '.join(SCENARIOS)}")
    json_out = args.json_out or CONTAINER_JSON_OUT

    image = args.image or (image_from_config(args.config) if args.config else None) or DEFAULT_IMAGE
    selected = args.scenario or list(SCENARIOS)
    if args.dry_run:
        print(f"image: {image}")
        print(
            f"wrapper (expiry scenarios): timeout -k {args.kill_after}s {args.timeout}s sh -c <agent script> {AGENT_TAG}"
        )
        print("agent script:\n" + agent_script(ignore_term=False))
        print(f"scenarios: {', '.join(selected)}")
        print(f"containers: one per scenario, label {LABEL_KEY}={LABEL_VALUE}, network none, removed afterwards")
        return 0

    if (CAIRN_SRC / "cairn").is_dir():
        sys.path.insert(0, str(CAIRN_SRC))  # use the dispatcher code from this checkout
    try:
        harness = Harness(image, args.timeout, args.kill_after)
    except ImportError as exc:
        print(
            f"missing dependency: {exc}. Run this with the Python environment that runs the dispatcher.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(f"Docker is not reachable: {exc}", file=sys.stderr)
        return 2

    results: dict[str, dict[str, Any]] = {}
    try:
        try:
            harness.client.ping()
            version = harness.client.version()
        except Exception as exc:
            print(f"Docker is not reachable: {exc}", file=sys.stderr)
            return 2
        try:
            image_id = harness.client.images.get(image).id
        except Exception:
            print(f"pulling {image} ...")
            try:
                image_id = harness.client.images.pull(image).id
            except Exception as exc:
                print(f"could not pull {image}: {exc}", file=sys.stderr)
                return 2
        for name in selected:
            print(f"running {name} ...", flush=True)
            try:
                results[name] = SCENARIOS[name](harness)
            except Exception as exc:
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        harness.close()

    findings = conclusions(results)
    report = {
        "tool": "tools/repair-phase0/kill_check.py",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cairn_commit": _git_commit(),
        "image": image,
        "image_id": image_id,
        "docker": {k: version.get(k) for k in ("Version", "ApiVersion", "Os", "Arch", "KernelVersion")},
        "host": {"python": platform.python_version(), "platform": platform.platform()},
        "settings": {"timeout_s": args.timeout, "kill_after_s": args.kill_after, "long_timeout_s": LONG_TIMEOUT_S},
        "scenarios": results,
        "conclusions": findings,
    }
    print_table(results)
    print()
    for line in findings:
        print(f"- {line}")
    json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(restore_signals=False))
