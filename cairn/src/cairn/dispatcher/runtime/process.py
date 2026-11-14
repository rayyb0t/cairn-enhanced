from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import hashlib
import logging
import threading
import time
from typing import Any, Protocol, runtime_checkable

from docker.errors import DockerException, NotFound
from docker.models.containers import Container

LOG = logging.getLogger(__name__)
EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0
# A kill that arrives while the exec is being started waits this long for Docker to create its
# process: Docker answers exec_start before the process exists.
EXEC_KILL_START_WAIT_SECONDS = 2.0
EXEC_KILL_POLL_SECONDS = 0.05
# kill() runs KILL_GROUP_SCRIPT as one short helper exec in the run's container. The image's timeout
# bounds it there: every image that can run the run's own `timeout -k 5s Ns` wrapper has one.
EXEC_KILL_HELPER = ("timeout", "-k", "1s", "3s", "/bin/sh", "-c")
EXEC_KILL_HELPER_NAME = "cairn-kill"
EXEC_KILL_STATUS_PREFIX = "cairn-kill: "
# The helper lives at most 3 s + 1 s; its output is never waited for longer than this.
EXEC_KILL_HELPER_READ_SECONDS = 5.0
# A helper attempt that took this long and did not kill (for example a /proc scan of a container
# with tens of thousands of processes) makes this process's later kills wait before trying again,
# so cancel(), which the scheduler repeats every tick, cannot hold the scheduler loop each time.
EXEC_KILL_SLOW_HELPER_SECONDS = 2.0
EXEC_KILL_SLOW_HELPER_PAUSE_SECONDS = 30.0

# Runs as `/bin/sh -c KILL_GROUP_SCRIPT cairn-kill <sha256>` (a second argument, a /proc root, is
# only for tests), as the image's default user, which is also the run's user.
# The root process of every docker exec has parent PID 0 inside the container (its parent is
# outside the container's PID namespace) and leads its own process group (runc starts each exec in
# a new session). For a run, that root is the `timeout` wrapper: its /proc/<pid>/cmdline is exactly
# the exec's argv, and its group is the group the wrapper signals at its own deadline.
# The script SIGKILLs that group only when exactly one parent-0 process has this cmdline, that
# process leads its group, and it is unchanged when read again just before the signal. It never
# signals PID 1. It uses shell builtins (dash's kill is one) and sha256sum, and prints exactly one
# line: "cairn-kill: <verdict> [detail]".
KILL_GROUP_SCRIPT = r"""want=$1 proc=${2:-/proc}
[ -r "$proc/self/stat" ] || { echo "cairn-kill: error no-proc"; exit 0; }
command -v sha256sum >/dev/null 2>&1 || { echo "cairn-kill: error no-sha256sum"; exit 0; }
n=0 match= mgrp= mstart=
for d in "$proc"/[0-9]*; do
  p=${d##*/}
  case $p in *[!0-9]*|0|1) continue ;; esac
  read -r s 2>/dev/null <"$d/stat" || continue
  set -- ${s##*) }
  [ "$2" = 0 ] || continue
  h=$(sha256sum 2>/dev/null <"$d/cmdline") || continue
  [ "${h%% *}" = "$want" ] || continue
  n=$((n + 1)) match="$match $p" mgrp=$3 mstart=${20}
done
[ "$n" -gt 0 ] || { echo "cairn-kill: none"; exit 0; }
[ "$n" -eq 1 ] || { echo "cairn-kill: ambiguous$match"; exit 0; }
p=${match# }
[ "$mgrp" = "$p" ] || { echo "cairn-kill: not-leader $p"; exit 0; }
read -r s 2>/dev/null <"$proc/$p/stat" || { echo "cairn-kill: none"; exit 0; }
set -- ${s##*) }
[ "$2" = 0 ] && [ "$3" = "$p" ] && [ "${20}" = "$mstart" ] || { echo "cairn-kill: none"; exit 0; }
if kill -KILL "-$p" 2>/dev/null; then echo "cairn-kill: killed $p"; else echo "cairn-kill: kill-failed $p"; fi
"""


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


@runtime_checkable
class ExecProcess(Protocol):
    """A worker process, regardless of whether it runs inside a container or on the host.

    Container mode uses ManagedProcess; local mode uses LocalProcess. Both expose this
    surface so the task runners, heartbeat lease and cancellation stay backend-agnostic.
    """

    def start(self) -> None: ...

    def communicate(self, timeout: float | None) -> ProcessResult: ...

    def kill(self) -> None: ...

    def cancel(self, reason: str) -> None: ...


class ManagedProcess:
    def __init__(self, container: Container, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._container = container
        self._api = container.client.api
        self._exec_id: str | None = None
        self._reader: threading.Thread | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._read_error: str | None = None
        self._done = threading.Event()
        # kill() state. _state_lock orders a kill against the reader thread's exec_start and is
        # never held across a Docker call. _kill_lock lets one thread at a time make a kill
        # attempt and guards the three fields after it.
        self._state_lock = threading.Lock()
        self._kill_requested = False
        self._exec_started = False
        self._kill_lock = threading.Lock()
        self._killed = False
        self._start_wait_spent = False
        self._helper_paused_until = 0.0
        self._kill_warned: set[str] = set()

    def start(self) -> None:
        exec_info = self._api.exec_create(
            self._container.id,
            self.command,
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            environment=self.env,
        )
        self._exec_id = exec_info["Id"]
        self._reader = threading.Thread(target=self._read_stream, daemon=True)
        self._reader.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._reader is not None
        self._reader.join(timeout=timeout)
        if self._reader.is_alive():
            self._timed_out = True
            self.kill()
            self._reader.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        if self._reader.is_alive():
            if self._returncode is None:
                self._returncode = 137
            self._done.set()
        self._done.wait(timeout=0)
        if self._read_error and not self._stderr:
            self._stderr.append(self._read_error)
        return ProcessResult(
            returncode=self._returncode if self._returncode is not None else 1,
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        """SIGKILL the run's process group in the container: the group the wrapper's -k signals.

        Docker has no call that stops one exec, and exec_inspect's Pid is a host PID, so this runs
        KILL_GROUP_SCRIPT in one short helper exec. Safe to call repeatedly and from several
        threads: a call made while another thread is killing returns at once, and calls after a
        successful kill make no Docker call.
        """
        if self._exec_id is None:
            return
        with self._state_lock:
            self._kill_requested = True
            exec_started = self._exec_started
        if not exec_started or self._killed:
            return  # not started yet: the reader thread sees the request and never starts it
        if not self._kill_lock.acquire(blocking=False):
            return
        try:
            if not self._killed:
                self._kill_started_exec()
        finally:
            self._kill_lock.release()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _read_stream(self) -> None:
        assert self._exec_id is not None
        with self._state_lock:
            killed_before_start = self._kill_requested
            self._exec_started = not killed_before_start
        if killed_before_start:
            LOG.info("container exec killed before it started exec_id=%s container=%s", self._exec_id, self._container.name)
            self._returncode = 137
            self._done.set()
            return
        stream: Any | None = None
        try:
            stream = self._api.exec_start(
                self._exec_id,
                detach=False,
                tty=False,
                stream=True,
                demux=True,
            )
            for chunk in stream:
                stdout, stderr = self._split_chunk(chunk)
                if stdout:
                    self._stdout.append(stdout)
                if stderr:
                    self._stderr.append(stderr)
        except DockerException as exc:
            self._read_error = str(exc)
        finally:
            self._close_stream(stream)
            self._returncode = self._resolve_exit_code()
            self._done.set()

    @staticmethod
    def _close_stream(stream: Any | None) -> None:
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        response = getattr(stream, "_response", None)
        response_close = getattr(response, "close", None)
        if callable(response_close):
            with suppress(Exception):
                response_close()

    def _resolve_exit_code(self) -> int:
        assert self._exec_id is not None
        deadline = time.monotonic() + EXEC_KILL_JOIN_TIMEOUT_SECONDS
        while True:
            try:
                details = self._api.exec_inspect(self._exec_id)
            except DockerException as exc:
                if self._read_error is None:
                    self._read_error = str(exc)
                return 137 if self._timed_out else 1
            exit_code = details.get("ExitCode")
            if exit_code is not None:
                return int(exit_code)
            if time.monotonic() >= deadline:
                return 137 if self._timed_out else 1
            time.sleep(0.1)

    def _kill_started_exec(self) -> None:
        if time.monotonic() < self._helper_paused_until:
            LOG.debug("kill helper paused after a slow attempt exec_id=%s container=%s", self._exec_id, self._container.name)
            return
        details = self._wait_for_exec_process()
        if details is None:
            return
        name = self._container.name
        argv = _exec_argv(details) or list(self.command)
        try:
            digest = _cmdline_sha256(argv)
        except UnicodeError as exc:
            self._warn_once("argv", "cannot identify container exec to kill exec_id=%s container=%s error=%s", self._exec_id, name, exc)
            return
        try:
            twins = self._running_twins(argv)
            if twins:
                self._warn_once(
                    "twins",
                    "not killing container exec: other running execs have the same command line exec_id=%s container=%s others=%s",
                    self._exec_id,
                    name,
                    ",".join(twins),
                )
                return
            began = time.monotonic()
            try:
                stdout, stderr = self._run_kill_helper(digest)
            finally:
                spent = time.monotonic() - began
                if spent >= EXEC_KILL_SLOW_HELPER_SECONDS:
                    self._helper_paused_until = time.monotonic() + EXEC_KILL_SLOW_HELPER_PAUSE_SECONDS
                    self._warn_once(
                        "slow",
                        "kill helper took %.1fs, next kill attempts wait %.0fs exec_id=%s container=%s",
                        spent,
                        EXEC_KILL_SLOW_HELPER_PAUSE_SECONDS,
                        self._exec_id,
                        name,
                    )
        except DockerException as exc:
            self._warn_once("docker", "failed to kill container exec exec_id=%s container=%s error=%s", self._exec_id, name, exc)
            return
        status = next(
            (line[len(EXEC_KILL_STATUS_PREFIX) :] for line in stdout.splitlines() if line.startswith(EXEC_KILL_STATUS_PREFIX)), None
        )
        if status is None:
            output = " ".join(f"{stdout} {stderr}".split())[:300]
            self._warn_once("output", "kill helper gave no result exec_id=%s container=%s output=%s", self._exec_id, name, output)
            return
        verdict, _, detail = status.strip().partition(" ")
        if verdict == "killed":
            self._killed = True
            LOG.info("killed container exec process group exec_id=%s container=%s pgid=%s", self._exec_id, name, detail)
        elif verdict == "none":
            self._warn_once(
                "none",
                "no process of the container exec found to kill (it may have just ended, or the image's timeout replaces itself with the command, as BusyBox's does) exec_id=%s container=%s",
                self._exec_id,
                name,
            )
        else:
            self._warn_once(
                verdict, "not killing container exec: helper reported %s exec_id=%s container=%s", status.strip(), self._exec_id, name
            )

    def _wait_for_exec_process(self) -> dict[str, Any] | None:
        """exec_inspect once the exec's process exists; None when it has ended or has no process.

        Docker answers exec_start before it creates the process, so a kill right after start can
        see the exec without a Pid. That is waited for at most once per process.
        """
        assert self._exec_id is not None
        deadline = time.monotonic() + EXEC_KILL_START_WAIT_SECONDS
        while True:
            try:
                details = self._api.exec_inspect(self._exec_id)
            except DockerException as exc:
                LOG.warning("failed to inspect exec before kill exec_id=%s error=%s", self._exec_id, exc)
                return None
            if details.get("ExitCode") is not None:
                return None
            if details.get("Running") and details.get("Pid"):
                return details
            if self._start_wait_spent or time.monotonic() >= deadline:
                self._start_wait_spent = True
                self._warn_once(
                    "not-started",
                    "container exec has no process yet, kill not applied exec_id=%s container=%s",
                    self._exec_id,
                    self._container.name,
                )
                return None
            if self._done.wait(EXEC_KILL_POLL_SECONDS):
                return None

    def _running_twins(self, argv: list[str]) -> list[str]:
        """Other execs of the container that Docker reports running with this argv (or an unreadable one).

        The helper identifies the run by its command line, so it must not run while another exec
        with the same command line is alive: had this run's root just ended, that exec's root
        would be the only match.
        """
        info = self._api.inspect_container(str(self._container.id))
        twins: list[str] = []
        for exec_id in info.get("ExecIDs") or []:
            if exec_id == self._exec_id:
                continue
            try:
                details = self._api.exec_inspect(exec_id)
            except NotFound:
                continue
            if details.get("Running") and _exec_argv(details) in (None, argv):
                twins.append(str(exec_id))
        return twins

    def _run_kill_helper(self, digest: str) -> tuple[str, str]:
        """Run KILL_GROUP_SCRIPT and return its stdout and stderr, read up to its status line.

        The stream is then closed rather than read to its end: when the run just killed leaves a
        process that still holds its output (a setsid child), Docker ends this helper's stream
        only about 2 s later, which would hold kill() for that long.
        """
        command = [*EXEC_KILL_HELPER, KILL_GROUP_SCRIPT, EXEC_KILL_HELPER_NAME, digest]
        helper_id = self._api.exec_create(self._container.id, command, stdout=True, stderr=True, stdin=False, tty=False)["Id"]
        stream = self._api.exec_start(helper_id, detach=False, tty=False, stream=True, demux=True)
        stdout: list[str] = []
        stderr: list[str] = []
        # Docker ends this stream only after it has handled every earlier exit in the container, each
        # of which may wait 2 s for output; so the read must not depend on the stream ending.
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            self._close_stream(stream)

        deadline = threading.Timer(EXEC_KILL_HELPER_READ_SECONDS, expire)
        deadline.daemon = True
        deadline.start()
        try:
            for chunk in stream:
                out, err = self._split_chunk(chunk)
                stdout.append(out)
                stderr.append(err)
                if "\n" in out or "\n" in err:
                    break
        except (OSError, ValueError):  # the stream closed under the read when the deadline passed
            if not expired.is_set():
                raise
        finally:
            deadline.cancel()
            self._close_stream(stream)
        return "".join(stdout), "".join(stderr)

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        """Warn once per process and kind of failed kill; cancel() is repeated every scheduler tick."""
        level = logging.DEBUG if key in self._kill_warned else logging.WARNING
        self._kill_warned.add(key)
        LOG.log(level, message, *args)

    @staticmethod
    def _split_chunk(chunk: Any) -> tuple[str, str]:
        if isinstance(chunk, tuple):
            stdout, stderr = chunk
        else:
            stdout, stderr = chunk, None
        return ManagedProcess._decode(stdout), ManagedProcess._decode(stderr)

    @staticmethod
    def _decode(chunk: bytes | str | None) -> str:
        if chunk is None:
            return ""
        if isinstance(chunk, bytes):
            return chunk.decode("utf-8", errors="replace")
        return chunk


def _exec_argv(details: dict[str, Any]) -> list[str] | None:
    """The exec's argv as Docker started it (exec_inspect's ProcessConfig), or None."""
    config = details.get("ProcessConfig")
    if not isinstance(config, dict):
        return None
    entrypoint = config.get("entrypoint")
    arguments = config.get("arguments") or []
    if not isinstance(entrypoint, str) or not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        return None
    return [entrypoint, *arguments]


def _cmdline_sha256(argv: list[str]) -> str:
    """sha256 of /proc/<pid>/cmdline of a process started with argv: each argument UTF-8, NUL-terminated."""
    return hashlib.sha256(b"".join(arg.encode("utf-8") + b"\0" for arg in argv)).hexdigest()
