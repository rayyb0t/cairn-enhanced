"""The child process of test_agent_main.py's signal cases (spec §9.3 step 7): `python signal_trials.py <mode>`.

It runs the real `loop.run_agent` and `RepairAgent.run` many times over on a stub agent, whose `poll_once` touches no
server, starts no review and asks for the next poll after a few milliseconds. Each run is one trial, and gets SIGTERM
as the mode says:

- `lines`: from a trace function, at the k-th line the main thread runs from the first poll on, for each k up to the
  fourth poll: once at each line of the loop, of its wait between polls and of the `threading` code they call. Each
  trial must return 0.
- `second`: the first SIGTERM is raised in the second poll, and the second at the k-th line the main thread runs from
  then until the first one's handler has returned, for each such line. Each trial must exit with 128 + SIGTERM.
- `random`: from another thread, at a random moment while the loop polls back to back. Each trial must return 0.

The trace function raises the signal in the main thread itself (`signal.raise_signal`), so its handler runs before the
traced line does. A trial that has not ended 10 s after it began prints the main thread's stack and exits 3 at once;
one that ends otherwise than it must prints why and exits 1. When every trial ends as it must, the process prints
`ok mode=<mode> trials=<n>` and exits 0.
"""

from __future__ import annotations

import os
import random
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from types import FrameType, SimpleNamespace
from typing import Any
from unittest import mock

from cairn_repair_agent import loop

HANG_SECONDS = 10.0
POLL_WAIT_SECONDS = 0.002  # long enough for the wait between polls to sleep
RANDOM_TRIALS = 20
RANDOM_LATEST_SECONDS = 0.02
RETURNED = "returned 0"
EXITED = f"exited {128 + signal.SIGTERM}"


class StubAgent(loop.RepairAgent):
    """Only what `run_agent` and `RepairAgent.run` use: `poll_once`, and the state of `in_flight` and `wait_idle`."""

    def __init__(self, wait: float, on_poll: Callable[[int], None]) -> None:  # no server, no model, no review
        self._lock = threading.Condition()
        self._in_flight = set()
        self.wait = wait
        self.on_poll = on_poll
        self.polls = 0

    def poll_once(self) -> float:
        self.polls += 1
        self.on_poll(self.polls)
        return self.wait


class LineSignal:
    """A trace function that counts the lines the main thread runs while `armed`, and raises SIGTERM at the
    `target`-th of them (never, when `target` is None)."""

    def __init__(self, target: int | None = None) -> None:
        self.target = target
        self.armed = False
        self.lines = 0
        self.sent = 0

    def __call__(self, frame: FrameType, event: str, arg: Any) -> LineSignal:
        if event == "line" and self.armed:
            self.lines += 1
            if self.lines == self.target:
                self.sent += 1
                signal.raise_signal(signal.SIGTERM)  # its handler runs here, before the traced line
        return self


class Trials:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.count = 0
        self.began: float | None = None  # when the trial in progress began (monotonic)
        threading.Thread(target=self._watch, name="watchdog", daemon=True).start()

    def _watch(self) -> None:
        while True:
            time.sleep(0.05)
            began = self.began
            if began is not None and time.monotonic() - began > HANG_SECONDS:
                frame = sys._current_frames().get(threading.main_thread().ident or 0)
                stack = "".join(traceback.format_stack(frame)) if frame is not None else "(no frame)\n"
                self.write(f"HUNG mode={self.mode} trial={self.count}: not ended {HANG_SECONDS:.0f} s after it began\n")
                self.write(stack)
                os._exit(3)

    def run(self, agent: StubAgent, trace: LineSignal | None = None) -> str:
        """One trial: `run_agent` on `agent`, traced by `trace` when given. How it ended, as text."""
        started: Any = SimpleNamespace(
            once=False,
            server=SimpleNamespace(close=lambda: None),
            model=SimpleNamespace(session=SimpleNamespace(close=lambda: None)),
        )
        self.count += 1
        self.began = time.monotonic()
        try:
            with mock.patch.object(loop.RepairAgent, "from_started", lambda started: agent):
                if trace is not None:
                    sys.settrace(trace)
                try:
                    return f"returned {loop.run_agent(started)}"
                finally:
                    sys.settrace(None)
        except SystemExit as exc:
            return f"exited {exc.code}"
        except Exception as exc:
            return f"raised {type(exc).__name__}"
        finally:
            self.began = None

    def expect(self, ended: str, wanted: str, what: str) -> None:
        if ended != wanted:
            self.write(f"FAIL mode={self.mode} trial={self.count} ({what}): {ended}, expected {wanted}\n")
            sys.exit(1)

    @staticmethod
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


def lines(trials: Trials) -> None:
    """SIGTERM at each line from the first poll to the fourth, one trial each."""
    calibration = LineSignal()
    at_fourth: list[int] = []
    ended = trials.run(StubAgent(POLL_WAIT_SECONDS, arming(calibration, at_fourth)), calibration)
    trials.expect(ended, RETURNED, "SIGTERM at the fourth poll")
    for target in range(1, at_fourth[0] + 1):
        trace = LineSignal(target)
        ended = trials.run(StubAgent(POLL_WAIT_SECONDS, arming(trace)), trace)
        trials.expect(ended if trace.sent == 1 else f"{ended} with no signal", RETURNED, f"SIGTERM at line {target}")


def arming(trace: LineSignal, at_fourth: list[int] | None = None) -> Callable[[int], None]:
    """An `on_poll` that arms `trace` from the first poll on; with `at_fourth`, it appends to it the lines `trace` has
    counted when the fourth poll begins, and raises SIGTERM there."""

    def on_poll(polls: int) -> None:
        trace.armed = True
        if at_fourth is not None and polls == 4:
            at_fourth.append(trace.lines)
            signal.raise_signal(signal.SIGTERM)

    return on_poll


def second(trials: Trials) -> None:
    """The first SIGTERM in the second poll; the second at each line until the first one's handler has returned."""
    calibration = LineSignal()
    ended = trials.run(StubAgent(POLL_WAIT_SECONDS, first_signal(calibration)), calibration)
    trials.expect(ended, RETURNED, "one SIGTERM")
    for target in range(1, calibration.lines + 1):
        trace = LineSignal(target)
        ended = trials.run(StubAgent(POLL_WAIT_SECONDS, first_signal(trace)), trace)
        trials.expect(ended if trace.sent == 1 else f"{ended} with no second", EXITED, f"second at line {target}")


def first_signal(trace: LineSignal) -> Callable[[int], None]:
    """An `on_poll` that raises SIGTERM in the second poll, with `trace` armed while it is handled."""

    def on_poll(polls: int) -> None:
        if polls == 2:
            trace.armed = True
            signal.raise_signal(signal.SIGTERM)  # its handler runs here, traced
            trace.armed = False

    return on_poll


def random_moment(trials: Trials) -> None:
    """SIGTERM from another thread, a random 0 to 20 ms after the first poll, while the loop polls back to back."""
    for _ in range(RANDOM_TRIALS):
        polled = threading.Event()
        threading.Thread(target=send_after, args=(polled,), name="sender", daemon=True).start()
        trials.expect(trials.run(StubAgent(0.0, first_poll(polled))), RETURNED, "SIGTERM at a random moment")


def first_poll(polled: threading.Event) -> Callable[[int], None]:
    def on_poll(polls: int) -> None:
        if polls == 1:
            polled.set()

    return on_poll


def send_after(polled: threading.Event) -> None:
    polled.wait()
    time.sleep(random.uniform(0, RANDOM_LATEST_SECONDS))
    os.kill(os.getpid(), signal.SIGTERM)


def main() -> int:
    mode = sys.argv[1]
    trials = Trials(mode)
    {"lines": lines, "second": second, "random": random_moment}[mode](trials)
    trials.write(f"ok mode={mode} trials={trials.count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
