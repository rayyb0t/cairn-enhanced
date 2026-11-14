#!/usr/bin/env python3
"""Phase 0 kill check: what really stops a worker command in Cairn's container mode.

Runs Cairn's own runtime code against your Docker daemon:

* ContainerManager.build_exec_process, which wraps the command as ``timeout -k <K>s <N>s ...``
  exactly as the dispatcher does,
* ManagedProcess.communicate, kill and cancel, and the _kill_pid that kill() relies on.

The command it runs stands in for an agent CLI and starts three children: a normal child, a child
that ignores SIGTERM (as some tools do), and a child in its own session (setsid). Each scenario
then ends the run one way and lists which of those processes are still alive in the container.

Scenarios:
  wrapper_expiry          the timeout wrapper expires (the agent exits on SIGTERM)
  wrapper_expiry_stubborn the timeout wrapper expires and the agent ignores SIGTERM, so the
                          wrapper's SIGKILL follows after K seconds
  managed_kill            ManagedProcess.kill() on a running exec: does anything stop? It records
                          what _kill_pid ran, the exit codes it accepted, and whether the PID that
                          exec_inspect reports even exists inside the container
  communicate_timeout     the dispatcher's own wait (communicate) ends before the wrapper
  cancel                  ManagedProcess.cancel(), as when a project is stopped: how long until
                          communicate() returns?

Every scenario uses its own throwaway container (label cairn.phase0=kill-check, no network),
removed at the end even on errors. Nothing else on your Docker host is touched.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
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
            if accepted:
                text += f"; _kill_pid stopped after `{' '.join(map(str, accepted['command']))}` exited {accepted['exit_code']}, which it treats as done"
            if r.get("exec_pid_host_command"):
                text += f"; the PID it used ({r['exec_inspect_pid']}) is the host PID of `{r['exec_pid_host_command'][:80]}`"
            if not r.get("exec_pid_visible_inside_container"):
                text += ", and no process has that PID inside the container"
            replay = r.get("kill_replay_with_output")
            if replay:
                text += f"; run again with output attached it exits {replay['exit_code']}: {replay['output'] or '(no output)'}"
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


def print_table(results: dict[str, dict[str, Any]]) -> None:
    print(f"\n{'scenario':<26}{'exit':>6}{'ended/returned':>16}  {'run still going':<16}survivors")
    for name, r in results.items():
        if "error" in r:
            print(f"{name:<26}  error: {r['error']}")
            continue
        seconds = r.get(
            "ended_after_s",
            r.get(
                "communicate_returned_after_s", r.get("communicate_returned_after_cancel_s", r.get("stopped_after_s"))
            ),
        )
        running = r.get("exec_running_after", r.get("exec_running_after_kill"))
        print(
            f"{name:<26}{r.get('returncode', '-')!s:>6}{('-' if seconds is None else f'{seconds} s'):>16}  "
            f"{('yes' if running else 'no'):<16}{', '.join(r.get('survivors', [])) or 'nothing'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", help=f"image to test (default: container.image from --config, else {DEFAULT_IMAGE})")
    parser.add_argument("--config", type=Path, help="your dispatch.yaml, to test the same image your workers use")
    parser.add_argument(
        "--scenario", action="append", choices=list(SCENARIOS), help="run only this scenario (repeatable)"
    )
    parser.add_argument(
        "--timeout", type=int, default=4, help="wrapper timeout N for the expiry scenarios (default: %(default)s)"
    )
    parser.add_argument(
        "--kill-after", type=int, default=2, help="wrapper kill-after K (default: %(default)s; the dispatcher uses 5)"
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("kill_check_results.json"),
        help="where to write the results (default: %(default)s)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print what would run and exit, without Docker")
    args = parser.parse_args(argv)

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
    args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
