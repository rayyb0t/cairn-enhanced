"""One review (spec §9.4-§9.10): the copy, the digest, the model call and the validation, with every failure mapped.

`review_run` never raises. A copy that cannot be used (§9.5) is `transcript_unreadable`; an exception while the copy
is parsed or the digest built is `digest_error`, with the exception's class name only; a model call that fails is
`model_timeout` or `model_error` (Appendix C-1); an answer that fails §9.8 is `invalid_output`. Each of these is
posted as `continue`, with the cause as the reason, no evidence and an empty next step (§9.9).

Times. The server's times are mapped to the agent's monotonic clock, `mono = mono_at_response + (t - server_time)`,
with `server_time` and the response time of the poll that listed the run, so the agent's wall clock is never compared
with a server time (§9.4). From the window's `review_accept_until`: the model and copy deadline, 10 s before it (no
model attempt and no copy wait after it), and the post deadline, 1 s before it (posting retries stop). The copy wait
ends at `review_closes_at`, or at the model deadline if that is earlier.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import AgentConfig
from .decision import InvalidOutputError, validate_answer
from .digest import Digest, ReviewContext, build_digest, copy_age, facts_for_run, live_cut
from .mirror import MirrorCopy, Unavailable, freshness_floor, read_fresh_copy, valid_project_id, valid_run_id
from .model import INVALID_OUTPUT, MODEL_ERROR, ModelResult, Reviewer
from .redact import ExactSecrets
from .server import RepairServerClient
from .transcripts import CLI_TYPES, parse_transcript

logger = logging.getLogger(__name__)

SERVER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MODEL_MARGIN_SECONDS = 10.0  # the model and copy deadline: accept_until - 10 s
POST_MARGIN_SECONDS = 1.0  # the post deadline: accept_until - 1 s
FACTS_TIMEOUT_SECONDS = 10.0
DIGEST_POST_MAX_CHARS = 65536  # the server's limits (§6.5)
RAW_OUTPUT_POST_MAX_CHARS = 16384
TRANSCRIPT_UNREADABLE = "transcript_unreadable"
DIGEST_ERROR = "digest_error"
_CLASS_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def server_time(value: object) -> datetime | None:
    """A time of the server's answers, `YYYY-MM-DDTHH:MM:SSZ` on the server's clock; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, SERVER_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def loggable_id(value: object) -> str:
    """An id from the server's answer as a log line shows it: the id when it matches §8.7's id rule, else `-`."""
    return value if isinstance(value, str) and valid_project_id(value) else "-"


def loggable_count(value: object) -> object:
    return value if type(value) is int else "-"


def class_name(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _CLASS_NAME.fullmatch(name) else "other"


@dataclass(frozen=True)
class Poll:
    """The poll that listed the run: the server's clock at its answer, and the agent's monotonic clock then."""

    server_time: datetime
    mono_at_response: float
    slice_seconds: Any

    def mono(self, at: datetime) -> float:
        return self.mono_at_response + (at - self.server_time).total_seconds()


@dataclass(frozen=True)
class Window:
    """What the agent reads of a listed run, each value checked (§6.3): a run whose view lacks one is not reviewed."""

    run_id: str
    number: int
    started_at: datetime
    opens_at: datetime
    closes_at: datetime
    accept_until: datetime
    elapsed_seconds: int | float

    @property
    def opens_elapsed(self) -> float:
        return (self.opens_at - self.started_at).total_seconds()


def window_of(run: Mapping[str, Any]) -> Window | None:
    """The run's id, window and times, or None when the view does not have them in §6.3's shape."""
    window = run.get("window")
    if not isinstance(window, Mapping):
        return None
    run_id, number, elapsed = run.get("run_id"), window.get("number"), run.get("elapsed_seconds")
    times = [
        server_time(value)
        for value in (
            run.get("started_at"),
            window.get("review_opens_at"),
            window.get("review_closes_at"),
            window.get("review_accept_until"),
        )
    ]
    if not isinstance(run_id, str) or not valid_run_id(run_id) or type(number) is not int or number < 1:
        return None
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not 0 <= elapsed <= 604800:
        return None
    started, opens, closes, accept = times
    if started is None or opens is None or closes is None or accept is None:
        return None
    return Window(run_id, number, started, opens, closes, accept, elapsed)


@dataclass(frozen=True)
class Deadlines:
    """On the agent's monotonic clock (§9.4)."""

    model: float  # accept_until - 10 s: no new model attempt and no copy wait after it
    post: float  # accept_until - 1 s: posting retries stop
    copy_until: float  # review_closes_at, or the model deadline if earlier: the copy wait ends

    @classmethod
    def of(cls, window: Window, poll: Poll) -> Deadlines:
        accept = poll.mono(window.accept_until)
        model = accept - MODEL_MARGIN_SECONDS
        return cls(model=model, post=accept - POST_MARGIN_SECONDS, copy_until=min(poll.mono(window.closes_at), model))


@dataclass(frozen=True)
class Review:
    """What is posted for one review (§9.9), and whether a text was cut (§9.8)."""

    decision: str
    reason: str = field(repr=False)
    evidence: tuple[str, ...] = field(default=(), repr=False)
    next_step: str = field(default="", repr=False)
    fallback: str | None = None
    model: str | None = None
    digest: str | None = field(default=None, repr=False)
    raw_output: str | None = field(default=None, repr=False)
    transcript_cut_seconds: float | None = None
    truncated: bool = False

    def body(self, run: Mapping[str, Any], window: Window, review_id: str) -> dict[str, Any]:
        """The body of `POST /repair/runs/{run_id}/reviews` (§6.5, §9.9)."""
        version = run.get("version")
        return {
            "review_id": review_id,
            "window": window.number,
            "decision": self.decision,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "next_step": self.next_step,
            "fallback": self.fallback,
            "expected_version": version if type(version) is int else None,
            "digest_elapsed_seconds": window.elapsed_seconds,
            "transcript_cut_seconds": self.transcript_cut_seconds,
            "model": self.model,
            "digest": None if self.digest is None else self.digest[:DIGEST_POST_MAX_CHARS],
            "raw_output": None if self.raw_output is None else self.raw_output[:RAW_OUTPUT_POST_MAX_CHARS],
        }


def fallback(kind: str, cause: str, **values: Any) -> Review:
    """An automatic continue: the cause as the reason, no evidence, an empty next step (§9.9)."""
    return Review(decision="continue", reason=cause, fallback=kind, **values)


@dataclass
class ReviewEnvironment:
    """What every review uses: the agent's configuration, its first reviewer, the agent's own secrets for the
    digest's exact-value layer, the reviewer's digest budget, the server client, and the clock."""

    config: AgentConfig
    reviewer: Reviewer
    exact: ExactSecrets
    budget: int
    server: RepairServerClient
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def unreadable_reason(result: Unavailable, window: Window) -> str:
    """The reason of a `transcript_unreadable` review: the copy's fixed reason, and for a stale copy how long before
    the review opened it was last checked (§9.10)."""
    if result.reason == "stale" and result.checked_elapsed_seconds is not None:
        before = max(0, round(window.opens_elapsed - result.checked_elapsed_seconds))
        return f"transcript copy stale: checked {before} s before the review opened"
    return f"transcript copy {result.reason}"


def review_run(
    run: Mapping[str, Any], window: Window, poll: Poll, deadlines: Deadlines, env: ReviewEnvironment
) -> Review:
    """One review of the run's open window; never raises (see the module docstring)."""
    logger.info(
        "repair review started run=%s project=%s intent=%s window=%s elapsed=%ss remaining=%ss cli=%s",
        window.run_id,
        loggable_id(run.get("project_id")),
        loggable_id(run.get("intent_id")),
        window.number,
        loggable_count(run.get("elapsed_seconds")),
        loggable_count(run.get("remaining_seconds")),
        run.get("cli_type") if run.get("cli_type") in CLI_TYPES else "-",
    )
    copy = _read_copy(run, window, deadlines, env)
    if isinstance(copy, Unavailable):
        logger.warning("repair transcript unavailable run=%s reason=%s", window.run_id, copy.reason)
        return fallback(TRANSCRIPT_UNREADABLE, unreadable_reason(copy, window))
    cut_seconds = copy.checked_elapsed_seconds
    try:
        digest = _digest(run, window, poll, copy, deadlines, env)
    except Exception as exc:  # a parser or digest exception: its type only (§9.10)
        return fallback(DIGEST_ERROR, class_name(exc), transcript_cut_seconds=cut_seconds)
    try:
        result = env.reviewer.call(digest.text, run=window.run_id, deadline=deadlines.model)
    except Exception as exc:  # §9.7: any exception the rules do not name is model_error, never the thread's end
        result = ModelResult(MODEL_ERROR, f"error_{class_name(exc)}")
    values: dict[str, Any] = {
        "model": result.model,
        "digest": digest.text,
        "raw_output": result.raw_output,
        "transcript_cut_seconds": cut_seconds,
    }
    if result.fallback is not None:
        return fallback(result.fallback, result.cause, **values)
    try:
        decision = validate_answer(result.text or "", digest.event_ids, digest.running_ids)
    except InvalidOutputError as invalid:
        return fallback(INVALID_OUTPUT, invalid.cause, **values)
    except Exception as exc:  # validation must never end the thread either
        return fallback(INVALID_OUTPUT, f"not valid: {class_name(exc)}", **values)
    return Review(
        decision=decision.decision,
        reason=decision.reason,
        evidence=decision.evidence,
        next_step=decision.next_step,
        truncated=decision.truncated,
        **values,
    )


def _read_copy(
    run: Mapping[str, Any], window: Window, deadlines: Deadlines, env: ReviewEnvironment
) -> MirrorCopy | Unavailable:
    """The run's copy (§9.5), fresh as of the window's opening; a stale one is read again until the copy wait ends.
    Both ids come from the run's view, and nothing else of it is used to find the copy."""
    project_id = run.get("project_id")
    if not isinstance(project_id, str):
        return Unavailable("bad_project_id")
    transcripts = env.config.transcripts
    try:
        return read_fresh_copy(
            transcripts.root,
            project_id,
            window.run_id,
            transcripts.max_bytes,
            min_checked_elapsed=freshness_floor(window.opens_elapsed, transcripts.max_staleness_seconds),
            wait_until=deadlines.copy_until,
            monotonic=env.monotonic,
            sleep=env.sleep,
        )
    except Exception as exc:  # a failed read, whatever its cause, is a copy that cannot be read (§9.10)
        return Unavailable(f"error:{class_name(exc)}")


def _digest(
    run: Mapping[str, Any], window: Window, poll: Poll, copy: MirrorCopy, deadlines: Deadlines, env: ReviewEnvironment
) -> Digest:
    """The digest of the copy at its last check (§9.6), with the facts the project answer gives; logs its line."""
    began = env.monotonic()
    age = copy_age(window.elapsed_seconds, began - poll.mono_at_response, copy.checked_elapsed_seconds)
    context = ReviewContext.from_run(run, slice_seconds=poll.slice_seconds, copy_age_seconds=age)
    transcript = parse_transcript(context.cli_type, copy.data)
    intent = run.get("intent")
    description = intent.get("description") if isinstance(intent, Mapping) else None
    intent_from = intent.get("from") if isinstance(intent, Mapping) else None
    source_facts, new_facts = None, None
    project_id = run.get("project_id")
    if isinstance(project_id, str):
        timeout = max(1.0, min(FACTS_TIMEOUT_SECONDS, deadlines.model - env.monotonic()))
        answer = env.server.get_project(project_id, timeout=timeout)
        if answer.status == 200:
            source_facts, new_facts = facts_for_run(
                answer.body, intent_from=intent_from, own_intent_id=run.get("intent_id"), since=window.started_at
            )
    digest = build_digest(
        transcript,
        context,
        live_cut(window.started_at, copy.checked_elapsed_seconds),
        intent=description if isinstance(description, str) else "",
        source_facts=source_facts,
        new_facts=new_facts,
        exact=env.exact,
        budget=env.budget,
    )
    logger.info(
        "repair digest built run=%s tokens_est=%s tool_calls=%s running=%s redactions=%s shrunk=%s build_ms=%s",
        window.run_id,
        digest.tokens_estimate,
        digest.tool_calls,
        digest.running,
        digest.redaction_count,
        digest.shrunk,
        max(0, round((env.monotonic() - began) * 1000)),
    )
    return digest
