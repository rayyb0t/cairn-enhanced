"""`RepairAgent`: the poll loop, its review threads, de-duplication and posting (spec §9.4, §9.9).

Every `poll_interval` the agent lists the live runs of switched-on projects (`GET /repair/runs`) and, for each run
and window it has not handled yet, oldest first: skips it, once, when the settings file's `projects` is a list without
its project (the list only narrows the switched-on projects; an empty list reviews none); forgets it when the run has
ended or is not reviewable; waits while its window is `pending`; starts one review on a review thread when it is
`open`; and forgets it in any other state (`late`, `reviewed`, `not_reviewed`, `closed`), so a review never starts
late. Handled keys are forgotten after 2 h. State is in memory only: after a restart a window still open is reviewed
with a new `review_id`, and one already reviewed gets `409 already_reviewed`, which is final.

The live agent reviews with the first reviewer of the settings file only (§8.3, §9.7). At most
`max_concurrent_reviews` reviews run at once, each on its own daemon thread, so one slow model call never holds up
other runs, and a call that outlives the agent's stop does not hold the process.

Posting follows §9.9's table: 201, or 200 for a replay of the same `review_id`, is done; 0, 408, 429 and 5xx (a 503
without code `token_not_configured` included) are retried after 2, 4, 8 s... while the retry starts before the post
deadline; any 409 and a 404 `run_not_found` are final; a 401, or a 503 `token_not_configured`, logs `repair agent
token rejected` once until a post succeeds again, drops the review and starts no new review for 60 s; a 422 and any
other status (a redirect included: none is followed) are final errors. A review that ends at or after the post deadline
(its model endpoint held the answer's headers, §9.7) is not posted at all: it is lost, which is fail-safe, and the
server records the window as not reviewed. Codes and field names from the server are logged only when they are plain
names, else as `other`. With `--dry-run` nothing is posted.

A failed poll is retried after `poll_interval`, doubling with each failure in a row, but never later than 15 s (or
`poll_interval`, when that is longer): a window is open for at least 17 s (a 351 s budget), so after an outage the next
poll still finds it open. An exception from a poll, which the server client turns into status 0 wherever it can, is a
failed poll too: logged with its class name only, and never the end of the loop. A poll of status 0 is logged with
the class that the server client recorded (`status=0 error=OSError`), so a local failure, such as a CA file removed
after the start, can be told from an outage.

SIGINT or SIGTERM stops polling and waits up to 15 s for the reviews in flight; a second signal exits at once (§9.3
step 7). A poll that a signal interrupts starts no review after it. With `--once` the agent waits for every review its
one poll started until such a signal comes, and from the signal on up to 15 s, as the loop does. A signal can come at
any moment, so its handler takes no lock (`SignalStop`).
"""

from __future__ import annotations

import logging
import re
import secrets
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .config import AgentConfig, AgentSettings
from .model import Reviewer
from .redact import ExactSecrets
from .review import Deadlines, Poll, Review, ReviewEnvironment, Window, class_name, review_run, server_time, window_of
from .server import RepairServerClient, ServerAnswer

if TYPE_CHECKING:
    from types import FrameType

    from .cli import StartedAgent

logger = logging.getLogger(__name__)

FORGET_AFTER_SECONDS = 2 * 3600
TOKEN_PAUSE_SECONDS = 60.0
POLL_RETRY_MAX_SECONDS = 15  # under the shortest open period, 17 s: floor(0.80 * 351) - floor(0.75 * 351)
POST_RETRY_FIRST_SECONDS = 2.0
POST_TIMEOUT_SECONDS = 30.0
POST_TIMEOUT_MIN_SECONDS = 5.0
SHUTDOWN_WAIT_SECONDS = 15.0
STOP_CHECK_SECONDS = 0.1  # how often a wait looks for a signal: between polls, and with --once for the reviews
POST_DEADLINE_PASSED = "post_deadline"  # `status=` of a review that ended too late to be posted
_SERVER_NAME = re.compile(r"[A-Za-z0-9_.]{1,64}")
_CLASS_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")  # a recorded exception class shown in a log line, as review.class_name
_FIELDS_SHOWN = 10


def new_review_id() -> str:
    """`rv-` and 24 hex digits, made when a review starts and reused on every retry of its post (§9.9)."""
    return "rv-" + secrets.token_hex(12)


def server_name(value: object) -> str:
    """A code or field name from the server's answer, as a log line shows it: the name, or `other` (§9.11)."""
    return value if isinstance(value, str) and _SERVER_NAME.fullmatch(value) else "other"


def invalid_fields(answer: ServerAnswer) -> str:
    """`fields=` of a 422: the last elements of FastAPI's `loc`s, or the code of a coded 422 (§9.9)."""
    detail = answer.body.get("detail") if isinstance(answer.body, dict) else None
    names: list[str] = []
    if isinstance(detail, list):
        for error in detail:
            loc = error.get("loc") if isinstance(error, dict) else None
            if isinstance(loc, list) and loc:
                last = loc[-1]  # a list index is an int: it passes the same rule as a name, as its digits
                name = server_name(str(last) if type(last) is int else last)
                if name not in names:
                    names.append(name)
    elif answer.code is not None:
        names.append(server_name(answer.code))
    return ",".join(names[:_FIELDS_SHOWN]) or "-"


class Stop(Protocol):
    """All that `RepairAgent.run` reads of its stop: `SignalStop` has it, and so has `threading.Event`."""

    def is_set(self) -> bool: ...


class SignalStop:
    """The stop of `run_agent`: set from the first SIGINT or SIGTERM on, each of which its handler appends to `signals`.

    A signal handler runs on the main thread between two of its bytecodes, whatever that thread holds there, so this
    takes no lock: appending to a list and reading it take none. A `threading.Event` would not do: its `set` waits for
    the Event's lock, which the main thread holds for a moment within each `Event.wait`, and within the first handler's
    own `set`; a signal handled there waited for its own thread forever, and so did every signal after it."""

    def __init__(self) -> None:
        self.signals: list[int] = []

    def is_set(self) -> bool:
        return bool(self.signals)


def stopped_within(seconds: float, stop: Stop) -> bool:
    """Wait `seconds` (real time) or until `stop` is set, whichever comes first; true when it is set. `stop.is_set()` is
    read between sleeps of at most `STOP_CHECK_SECONDS`, and no lock is held meanwhile (see `SignalStop`)."""
    end = time.monotonic() + seconds
    while not stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(STOP_CHECK_SECONDS, left))
    return True


@dataclass(frozen=True)
class _Job:
    run: Mapping[str, Any]
    window: Window
    poll: Poll
    deadlines: Deadlines
    key: tuple[str, int]


class RepairAgent:
    """The live agent's loop (§9.4) on a started agent's clients. `monotonic` and `sleep` are the agent's clock: tests
    give a fake one. `reviewer` defaults to the first reviewer's client."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        settings: AgentSettings,
        server: RepairServerClient,
        reviewer: Reviewer,
        exact: ExactSecrets,
        budget: int,
        dry_run: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        review_id: Callable[[], str] = new_review_id,
    ) -> None:
        self.config = config
        self.settings = settings
        self.server = server
        self.dry_run = dry_run
        self._monotonic = monotonic
        self._sleep = sleep
        self._review_id = review_id
        self.environment = ReviewEnvironment(
            config=config,
            reviewer=reviewer,
            exact=exact,
            budget=budget,
            server=server,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._lock = threading.Condition()
        self._done: dict[tuple[str, int], float] = {}
        self._in_flight: set[tuple[str, int]] = set()
        self._token_rejected = False
        self._paused_until: float | None = None
        self._poll_failures = 0
        self._threads = threading.Semaphore(config.max_concurrent_reviews)
        self._stop: Stop | None = None  # the stop that run() was given: a poll it interrupts starts no review

    @classmethod
    def from_started(cls, started: StartedAgent, **options: Any) -> RepairAgent:
        settings = started.settings_file.settings
        reviewer = options.pop("reviewer", None) or Reviewer(
            started.model,
            monotonic=options.get("monotonic", time.monotonic),
            sleep=options.get("sleep", time.sleep),
        )
        exact = ExactSecrets.for_agent(
            started.token, [entry.api_key for entry in settings.reviewers], started.server_api_key
        )
        return cls(
            config=started.config,
            settings=settings,
            server=started.server,
            reviewer=reviewer,
            exact=exact,
            budget=started.digest_tokens,
            dry_run=started.dry_run,
            **options,
        )

    # --- the poll ---------------------------------------------------------------------------------------------------

    def poll_once(self) -> float:
        """One poll and the reviews it starts; returns the seconds to wait before the next poll. Once the stop of
        `run` is set, which a signal can do while the poll's request is in flight, it starts no review: the windows it
        lists are left to the next agent."""
        answer = self.server.list_runs()
        poll, runs = self._read_poll(answer)
        if poll is None or runs is None:
            return self._poll_failed(answer.status, answer.error)
        self._poll_failures = 0
        for run in runs:
            if self._stop is not None and self._stop.is_set():
                break
            if isinstance(run, Mapping):
                self._consider(run, poll)
        self._forget(poll.mono_at_response)
        return self.config.poll_interval

    def _poll_failed(self, status: object, error: object = None) -> int:
        """Log a failed poll and return the seconds to its retry: `poll_interval`, doubled with each failure in a row,
        at most POLL_RETRY_MAX_SECONDS (or `poll_interval`, when that is longer). A poll without an answer (status 0)
        is logged with the exception class that the server client recorded for it, never the message, so that a
        local failure, such as a CA file removed after the start (`OSError`), does not read as a server outage
        (`ConnectionError`): `other` for a name that is not a plain identifier, `-` when none was recorded."""
        self._poll_failures += 1
        interval = self.config.poll_interval
        retry_in = min(max(interval, POLL_RETRY_MAX_SECONDS), interval * 2 ** min(self._poll_failures - 1, 4))
        if status == 0:
            shown = "-" if not isinstance(error, str) else error if _CLASS_NAME.fullmatch(error) else "other"
            logger.warning("repair agent poll failed status=0 error=%s retry_in=%ss", shown, retry_in)
        else:
            logger.warning("repair agent poll failed status=%s retry_in=%ss", status, retry_in)
        return retry_in

    def _read_poll(self, answer: ServerAnswer) -> tuple[Poll | None, list[Any] | None]:
        mono = self._monotonic()
        body = answer.body
        if answer.status != 200 or not isinstance(body, dict):
            return None, None
        at, runs = server_time(body.get("server_time")), body.get("runs")
        if at is None or not isinstance(runs, list):
            return None, None
        return Poll(server_time=at, mono_at_response=mono, slice_seconds=body.get("slice_seconds")), runs

    def _consider(self, run: Mapping[str, Any], poll: Poll) -> None:
        window = window_of(run)
        if window is None:  # a view that is not §6.3's shape: no deadline can be known, so no review
            return
        key = (window.run_id, window.number)
        with self._lock:
            if key in self._done or key in self._in_flight:
                return
            projects = self.settings.projects
            if projects is not None and run.get("project_id") not in projects:
                self._done[key] = poll.mono_at_response
                logger.info(
                    "repair review skipped run=%s window=%s reason=project_not_listed", window.run_id, window.number
                )
                return
            view = run.get("window")
            if run.get("state") == "ended" or not isinstance(view, Mapping) or view.get("reviewable") is not True:
                self._done[key] = poll.mono_at_response
                return
            status = view.get("review_status")
            if status == "pending":
                return
            if status != "open":  # late, reviewed, not_reviewed, closed: never a late start
                self._done[key] = poll.mono_at_response
                return
            if self._paused_until is not None and self._monotonic() < self._paused_until:
                return  # the token was rejected: no new review for a while
            if not self._threads.acquire(blocking=False):
                return  # every review thread is busy: the next poll looks again while the window is open
            self._in_flight.add(key)
        job = _Job(run, window, poll, Deadlines.of(window, poll), key)
        try:
            threading.Thread(target=self._run_job, args=(job,), name="repair-review", daemon=True).start()
        except RuntimeError:  # no thread could be started: the next poll looks again while the window is open
            with self._lock:
                self._in_flight.discard(key)
                self._threads.release()

    def _forget(self, now: float) -> None:
        with self._lock:
            for key in [key for key, at in self._done.items() if now - at > FORGET_AFTER_SECONDS]:
                del self._done[key]

    # --- a review ---------------------------------------------------------------------------------------------------

    def _run_job(self, job: _Job) -> None:
        try:
            review_id = self._review_id()  # made when the review starts, and reused on every retry of its post
            review = review_run(job.run, job.window, job.poll, job.deadlines, self.environment)
            if not self.dry_run:
                self._post(job, review, review_id)
        except Exception as exc:  # never ends the agent; the review is lost, and the server records not reviewed
            logger.error(
                "repair review failed run=%s window=%s status=%s",
                job.window.run_id,
                job.window.number,
                f"error_{class_name(exc)}",
            )
        finally:
            with self._lock:
                self._in_flight.discard(job.key)
                self._done[job.key] = self._monotonic()
                self._threads.release()
                self._lock.notify_all()

    def _post(self, job: _Job, review: Review, review_id: str) -> None:
        run_id, number = job.window.run_id, job.window.number
        if self._monotonic() >= job.deadlines.post:
            # §9.7: a review held past the post deadline (a model endpoint that sent its headers a byte at a time) is
            # lost, which is fail-safe: it is not posted, and the server records the window as not reviewed.
            logger.error("repair review failed run=%s window=%s status=%s", run_id, number, POST_DEADLINE_PASSED)
            return
        body = review.body(job.run, job.window, review_id)
        delay = POST_RETRY_FIRST_SECONDS
        while True:
            timeout = min(POST_TIMEOUT_SECONDS, max(POST_TIMEOUT_MIN_SECONDS, job.deadlines.post - self._monotonic()))
            answer = self.server.post_review(run_id, body, timeout=timeout)
            status, code = answer.status, answer.code
            if status in (200, 201):
                with self._lock:
                    self._token_rejected = False
                logger.info(
                    "repair review posted run=%s window=%s decision=%s fallback=%s status=%s truncated=%s",
                    run_id,
                    number,
                    review.decision,
                    review.fallback or "-",
                    status,
                    "true" if review.truncated else "false",
                )
                return
            if status == 401 or (status == 503 and code == "token_not_configured"):
                with self._lock:
                    first = not self._token_rejected
                    self._token_rejected = True
                    self._paused_until = self._monotonic() + TOKEN_PAUSE_SECONDS
                if first:
                    logger.error("repair agent token rejected status=%s", status)
                return
            if status == 409 or (status == 404 and code == "run_not_found"):
                logger.info("repair review refused run=%s window=%s code=%s", run_id, number, server_name(code))
                return
            if status == 422:
                logger.error("repair review rejected as invalid run=%s fields=%s", run_id, invalid_fields(answer))
                return
            retryable = status in (0, 408, 429) or 500 <= status <= 599
            if retryable and self._monotonic() + delay < job.deadlines.post:
                self._sleep(delay)
                delay *= 2
                continue
            logger.error("repair review failed run=%s window=%s status=%s", run_id, number, status)
            return

    # --- running ----------------------------------------------------------------------------------------------------

    def in_flight(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no review is in flight, or `timeout` seconds (real time) have passed; true when none is."""
        with self._lock:
            return self._lock.wait_for(lambda: not self._in_flight, timeout)

    def run(self, *, once: bool, stop: Stop) -> int:
        """Poll until `stop` is set (or once, with `once`), then wait for the reviews in flight: with `once`, until
        they end or `stop` is set; once `stop` is set, up to 15 s (§9.3 step 7). Returns the exit code. `stop` is only
        read, with `is_set()`, and never waited on (see `SignalStop` and `stopped_within`); a poll that it interrupts
        starts no review. An exception from a poll is a failed poll, logged with its class name only
        (`status=error_<class>`), and polling goes on."""
        self._stop = stop
        while True:
            try:
                wait: float = self.poll_once()
            except Exception as exc:  # never the end of the loop; the class name only, no message
                wait = self._poll_failed(f"error_{class_name(exc)}")
            if once or stopped_within(wait, stop):
                break
        while once and not stop.is_set():  # --once: every review the poll started, unless a signal comes first
            if self.wait_idle(STOP_CHECK_SECONDS):
                return 0
        self.wait_idle(SHUTDOWN_WAIT_SECONDS)
        return 0


def run_agent(started: StartedAgent) -> int:
    """Enter the loop on a started agent (§9.3 step 7). SIGINT or SIGTERM stops polling and waits up to 15 s for
    reviews in flight; a second signal exits at once."""
    agent = RepairAgent.from_started(started)
    stop = SignalStop()

    def handle(signum: int, frame: FrameType | None) -> None:
        # No lock (see SignalStop). The signal is counted before anything else, so a second one exits wherever it
        # comes, within the first one's handler included.
        stop.signals.append(signum)
        if len(stop.signals) > 1:
            raise SystemExit(128 + signum)

    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, handle)
    try:
        return agent.run(once=started.once, stop=stop)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if agent.in_flight() == 0:  # a review still in flight keeps its sessions until the process ends
            started.server.close()
            started.model.session.close()
