#!/usr/bin/env python3
"""Phase 0 baseline: how explore and bootstrap runs end today, read from the dispatcher's logs.

Writes, for the log files given:

* ``baseline_timeline.csv``: one row per dispatched explore or bootstrap attempt,
* ``baseline_summary.json``: counts, configured timeouts and duration percentiles,
* a printed summary: how many explore runs hit the timeout, how the conclude fallback ended,
  JSON rescues by strategy, and how long runs took.

Neither output contains stdout or stderr previews, so both are safe to share. The previews stay in
your logs; export_eval_set.py copies them into a local evaluation set.

Every message pattern below is the exact first argument of a LOG call in cairn/src. Run with
``--check-formats`` to confirm that against your checkout before trusting the numbers.
"""

from __future__ import annotations

import argparse
import ast
import csv
import gzip
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CAIRN_SRC = TOOL_DIR.parents[1] / "cairn" / "src"

# Header written by cairn.dispatcher.logging.configure_logging:
#   fmt="[%(asctime)s] %(levelname)s %(shortname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
# search() rather than match(), so "docker compose logs" prefixes such as "dispatcher-1  | " are fine.
HEADER_FORMAT = "[%(asctime)s] %(levelname)s %(shortname)s %(message)s"
HEADER_DATEFMT = "%Y-%m-%d %H:%M:%S"
LINE_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<logger>\S+) (?P<message>.*)$"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONTAINER_PREFIX = "cairn-dispatch-"  # ContainerManager._PREFIX
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


@dataclass(frozen=True)
class EventSpec:
    name: str
    fmt: str
    regex: re.Pattern[str]
    prefix: str


def _compile(name: str, fmt: str) -> EventSpec:
    """Turn a %-format log message into a regex with one named group per placeholder.

    A placeholder takes its group name from the "key=" right before it ("arg<n>" if there is none).
    Groups are lazy, so a value may contain spaces, as error= and the previews do.
    """
    parts = re.split(r"%[sr]", fmt)
    pattern = ""
    for index, literal in enumerate(parts):
        pattern += re.escape(literal)
        if index == len(parts) - 1:
            break
        key = re.search(r"(\w+)=$", literal)
        group = key.group(1) if key else f"arg{index}"
        last = index == len(parts) - 2 and parts[-1] == ""
        pattern += f"(?P<{group}>.*)" if last else f"(?P<{group}>.*?)"
    return EventSpec(name, fmt, re.compile("^" + pattern + "$"), parts[0])


# Longer variants come first: "intent concluded ... total_ms=%s" must win over the shorter one.
_EVENT_FORMATS: list[tuple[str, str]] = [
    # scheduler/loop.py
    ("dispatched_explore", "dispatched explore project=%s intent=%s worker=%s"),
    ("dispatched_bootstrap", "dispatched bootstrap project=%s intent=%s worker=%s"),
    ("task_finished", "task finished project=%s task=%s worker=%s outcome=%s"),
    ("task_cancelled", "task cancelled project=%s task=%s worker=%s"),
    # tasks/common.py
    ("exec_start", "starting container exec container=%s worker=%s phase=%s timeout=%ss"),
    (
        "conclude_skipped_inactive",
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
    ),
    ("intent_concluded", "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s"),
    ("intent_concluded", "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s"),
    ("project_inactive_during_conclude", "project became inactive during conclude project=%s intent=%s worker=%s"),
    ("conclude_write_failed", "conclude write failed project=%s intent=%s worker=%s status=%s body=%s"),
    ("released_intent", "released intent project=%s intent=%s worker=%s"),
    ("release_failed", "release failed project=%s intent=%s worker=%s status=%s"),
    ("release_skipped", "release skipped project=%s intent=%s worker=%s status=%s"),
    # contracts.py (the second %s is the context: "phase=... project=... intent=... worker=...")
    ("json_rescued", "json output rescued strategy=%s %s strict_error=%s description_chars=%s"),
    # tasks/explore.py
    ("explore_cancelled_healthcheck", "explore cancelled during healthcheck project=%s intent=%s worker=%s reason=%s"),
    (
        "explore_heartbeat_lost_healthcheck",
        "heartbeat lost during explore healthcheck project=%s intent=%s worker=%s status=%s",
    ),
    ("explore_cancelled", "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s"),
    ("explore_heartbeat_lost", "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s"),
    (
        "explore_parse_failed",
        "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    ("explore_rejected", "explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s"),
    (
        "explore_timed_out",
        "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "explore_command_failed",
        "explore command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    ("explore_crashed", "explore task crashed project=%s intent=%s worker=%s"),
    (
        "conclude_unavailable",
        "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
    ),
    (
        "conclude_skipped_heartbeat",
        "conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
    ),
    (
        "conclude_skipped_cancelled",
        "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
    ),
    ("conclude_started", "starting conclude fallback project=%s intent=%s worker=%s"),
    ("conclude_cancelled", "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s"),
    (
        "conclude_failed",
        "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "conclude_parse_failed",
        "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    ("conclude_rejected", "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s"),
    # tasks/bootstrap.py
    ("bootstrap_cancelled", "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s"),
    (
        "bootstrap_heartbeat_lost",
        "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
    ),
    (
        "bootstrap_parse_failed",
        "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "bootstrap_rejected",
        "bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
    ),
    (
        "bootstrap_timed_out",
        "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "bootstrap_command_failed",
        "bootstrap command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "bootstrap_conclude_unavailable",
        "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
    ),
    (
        "bootstrap_conclude_skipped_heartbeat",
        "bootstrap conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
    ),
    (
        "bootstrap_conclude_skipped_cancelled",
        "bootstrap conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
    ),
    ("bootstrap_conclude_started", "starting bootstrap conclude fallback project=%s intent=%s worker=%s"),
    (
        "bootstrap_conclude_cancelled",
        "bootstrap conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
    ),
    (
        "bootstrap_conclude_failed",
        "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "bootstrap_conclude_parse_failed",
        "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
    (
        "bootstrap_conclude_rejected",
        "bootstrap conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
    ),
    (
        "bootstrap_completed",
        "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
    ),
    ("bootstrap_completed", "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s"),
    # tasks/reason.py
    (
        "reason_timed_out",
        "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
    ),
]
EVENT_SPECS: list[EventSpec] = [_compile(name, fmt) for name, fmt in _EVENT_FORMATS]

# Event name -> (stage, outcome). "execute" ends the main run; "conclude" ends the conclude fallback.
_EXECUTE_END = {
    "explore_timed_out": "timed_out",
    "explore_parse_failed": "parse_failed",
    "explore_rejected": "rejected",
    "explore_command_failed": "command_failed",
    "explore_cancelled": "cancelled",
    "explore_cancelled_healthcheck": "cancelled_in_healthcheck",
    "explore_heartbeat_lost": "heartbeat_lost",
    "explore_heartbeat_lost_healthcheck": "heartbeat_lost_in_healthcheck",
    "explore_crashed": "crashed",
    "bootstrap_timed_out": "timed_out",
    "bootstrap_parse_failed": "parse_failed",
    "bootstrap_rejected": "rejected",
    "bootstrap_command_failed": "command_failed",
    "bootstrap_cancelled": "cancelled",
    "bootstrap_heartbeat_lost": "heartbeat_lost",
}
_CONCLUDE_NOT_STARTED = {
    "conclude_unavailable": "unavailable",
    "conclude_skipped_heartbeat": "skipped_heartbeat_lost",
    "conclude_skipped_cancelled": "skipped_cancelled",
    "conclude_skipped_inactive": "skipped_project_inactive",
    "bootstrap_conclude_unavailable": "unavailable",
    "bootstrap_conclude_skipped_heartbeat": "skipped_heartbeat_lost",
    "bootstrap_conclude_skipped_cancelled": "skipped_cancelled",
}
_CONCLUDE_END = {
    "conclude_cancelled": "cancelled",
    "conclude_failed": "failed",
    "conclude_parse_failed": "parse_failed",
    "conclude_rejected": "rejected",
    "bootstrap_conclude_cancelled": "cancelled",
    "bootstrap_conclude_failed": "failed",
    "bootstrap_conclude_parse_failed": "parse_failed",
    "bootstrap_conclude_rejected": "rejected",
}
_PHASE_TASK = {
    "explore_execute": ("explore", "execute"),
    "explore_conclude": ("explore", "conclude"),
    "bootstrap": ("bootstrap", "execute"),
    "bootstrap_conclude": ("bootstrap", "conclude"),
}
_EXECUTE_SOURCES = {"explore_execute", "bootstrap"}
_CONCLUDE_SOURCES = {"explore_conclude", "bootstrap_conclude"}


@dataclass
class Event:
    name: str
    ts: str
    level: str
    fields: dict[str, str]


@dataclass
class Attempt:
    """One dispatched explore or bootstrap run of an intent, as the logs tell it."""

    project: str
    intent: str
    task: str
    attempt: int
    worker: str
    dispatched_at: str | None = None
    execute_started_at: str | None = None
    execute_timeout_s: int | None = None
    execute_outcome: str | None = None
    execute_ms: int | None = None
    execute_error: str | None = None
    conclude_started_at: str | None = None
    conclude_timeout_s: int | None = None
    conclude_outcome: str | None = None
    conclude_ms: int | None = None
    conclude_error: str | None = None
    rescue_strategy: str | None = None
    concluded_source: str | None = None
    concluded_phase_ms: int | None = None
    concluded_total_ms: int | None = None
    released: bool = False
    final: str = "open"
    final_reason: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    events: list[str] = field(default_factory=list)
    # Previews stay in memory for export_eval_set.py; they are never written to the timeline CSV.
    execute_stdout_preview: str | None = field(default=None, repr=False)
    execute_stderr_preview: str | None = field(default=None, repr=False)
    conclude_stdout_preview: str | None = field(default=None, repr=False)
    conclude_stderr_preview: str | None = field(default=None, repr=False)


TIMELINE_COLUMNS = [f.name for f in fields(Attempt) if not f.name.endswith("_preview")]


@dataclass
class ParseResult:
    files: list[str] = field(default_factory=list)
    lines: int = 0
    records: int = 0
    unparsed_lines: int = 0
    other_records: int = 0
    events_by_name: Counter[str] = field(default_factory=Counter)
    attempts: list[Attempt] = field(default_factory=list)
    exec_starts: Counter[str] = field(default_factory=Counter)
    exec_timeouts: dict[str, Counter[int]] = field(default_factory=lambda: defaultdict(Counter))
    exec_linked: int = 0
    exec_unlinked: int = 0
    exec_ambiguous: int = 0
    tasks_finished: Counter[str] = field(default_factory=Counter)
    tasks_cancelled: Counter[str] = field(default_factory=Counter)
    reason_timed_out: int = 0
    rescues: list[dict[str, str]] = field(default_factory=list)
    write_failures: int = 0


def iter_lines(paths: Iterable[str]) -> Iterator[tuple[str, str]]:
    for path in paths:
        if path == "-":
            for line in sys.stdin:
                yield path, line
            continue
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            yield from ((path, line) for line in handle)


def match_event(message: str) -> tuple[str, dict[str, str]] | None:
    for spec in EVENT_SPECS:
        if not message.startswith(spec.prefix):
            continue
        found = spec.regex.match(message)
        if found:
            return spec.name, {k: v for k, v in found.groupdict().items() if v is not None}
    return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return (datetime.strptime(end, HEADER_DATEFMT) - datetime.strptime(start, HEADER_DATEFMT)).total_seconds()
    except ValueError:
        return None


def _context_fields(context: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", context))


def project_of_container(container: str) -> str:
    """cairn-dispatch-<project> in container mode; the project folder's name in local mode."""
    if container.startswith(CONTAINER_PREFIX):
        return container[len(CONTAINER_PREFIX) :]
    return Path(container.rstrip("/")).name


class _Builder:
    def __init__(self, result: ParseResult, projects: set[str] | None):
        self.result = result
        self.projects = projects
        self.by_intent: dict[tuple[str, str], list[Attempt]] = defaultdict(list)

    def wanted(self, project: str | None) -> bool:
        return self.projects is None or project in self.projects

    def new_attempt(self, project: str, intent: str, task: str, worker: str) -> Attempt:
        history = self.by_intent[(project, intent)]
        attempt = Attempt(project=project, intent=intent, task=task, attempt=len(history) + 1, worker=worker)
        history.append(attempt)
        self.result.attempts.append(attempt)
        return attempt

    def attempt_for(self, event: Event, task_hint: str) -> Attempt:
        project, intent, worker = event.fields["project"], event.fields["intent"], event.fields.get("worker", "")
        history = self.by_intent.get((project, intent))
        if history:
            # A run whose claim expired can still log after the intent was dispatched again, so
            # prefer the latest attempt by the same worker.
            same_worker = [a for a in history if a.worker == worker]
            return same_worker[-1] if same_worker else history[-1]
        # The log starts in the middle of this attempt: keep it, with no dispatch time.
        return self.new_attempt(project, intent, task_hint, worker)

    def link_exec_start(self, event: Event) -> None:
        phase = event.fields.get("phase", "")
        timeout = _int(event.fields.get("timeout"))
        project = project_of_container(event.fields.get("container", ""))
        if not self.wanted(project):
            return
        self.result.exec_starts[phase] += 1
        if timeout is not None:
            self.result.exec_timeouts[phase][timeout] += 1
        if phase not in _PHASE_TASK:
            return  # reason runs are not intent attempts
        task, stage = _PHASE_TASK[phase]
        worker = event.fields.get("worker", "")
        candidates = [
            a
            for a in self.result.attempts
            if a.project == project
            and a.worker == worker
            and a.task == task
            and a.final == "open"
            and (
                (stage == "execute" and a.execute_started_at is None and a.execute_outcome is None)
                or (stage == "conclude" and a.conclude_started_at is not None and a.conclude_timeout_s is None)
            )
        ]
        if not candidates:
            self.result.exec_unlinked += 1
            return
        if len(candidates) > 1:
            self.result.exec_ambiguous += 1  # same worker, same project, two runs: first in, first linked
        chosen = candidates[0]
        self.result.exec_linked += 1
        if stage == "execute":
            chosen.execute_started_at = event.ts
            chosen.execute_timeout_s = timeout
        else:
            chosen.conclude_timeout_s = timeout

    def apply(self, event: Event) -> None:
        name, f = event.name, event.fields
        if name == "exec_start":
            self.link_exec_start(event)
            return
        if name in ("task_finished", "task_cancelled"):
            if self.wanted(f.get("project")):
                key = f"{f.get('task')}:{f.get('outcome', 'cancelled')}"
                (self.result.tasks_finished if name == "task_finished" else self.result.tasks_cancelled)[key] += 1
            return
        if name == "reason_timed_out":
            if self.wanted(f.get("project")):
                self.result.reason_timed_out += 1
            return
        if name == "json_rescued":
            context = _context_fields(f.get("arg1", ""))
            if not self.wanted(context.get("project")):
                return
            rescue = {
                "strategy": f.get("strategy", ""),
                "phase": context.get("phase", ""),
                "project": context.get("project", ""),
                "intent": context.get("intent", ""),
                "worker": context.get("worker", ""),
                "strict_error": f.get("strict_error", ""),
            }
            self.result.rescues.append(rescue)
            if rescue["project"] and rescue["intent"]:
                task = "bootstrap" if rescue["phase"].startswith("bootstrap") else "explore"
                event.fields.update(project=rescue["project"], intent=rescue["intent"], worker=rescue["worker"])
                attempt = self.attempt_for(event, task)
                attempt.rescue_strategy = rescue["strategy"]
                self._touch(attempt, event)
            return
        if "project" not in f or "intent" not in f or not self.wanted(f["project"]):
            return
        if name in ("dispatched_explore", "dispatched_bootstrap"):
            attempt = self.new_attempt(f["project"], f["intent"], name.removeprefix("dispatched_"), f.get("worker", ""))
            attempt.dispatched_at = event.ts
            self._touch(attempt, event)
            return
        task_hint = "bootstrap" if name.startswith("bootstrap") else "explore"
        attempt = self.attempt_for(event, task_hint)
        self._touch(attempt, event)
        if name in _EXECUTE_END:
            attempt.execute_outcome = _EXECUTE_END[name]
            attempt.execute_ms = _int(f.get("execute_ms"))
            attempt.execute_error = f.get("error")
            attempt.execute_stdout_preview = f.get("stdout_preview")
            attempt.execute_stderr_preview = f.get("stderr_preview")
            if attempt.execute_outcome in ("rejected", "cancelled", "cancelled_in_healthcheck"):
                self._finish(attempt, "rejected" if attempt.execute_outcome == "rejected" else "cancelled", name, event)
            elif attempt.execute_outcome in (
                "command_failed",
                "heartbeat_lost",
                "heartbeat_lost_in_healthcheck",
                "crashed",
            ):
                self._finish(attempt, "failed", name, event)
        elif name in ("conclude_started", "bootstrap_conclude_started"):
            attempt.conclude_started_at = event.ts
        elif name in _CONCLUDE_NOT_STARTED:
            attempt.conclude_outcome = _CONCLUDE_NOT_STARTED[name]
            self._finish(attempt, "cancelled" if "cancelled" in name else "failed", name, event)
        elif name in _CONCLUDE_END:
            outcome = _CONCLUDE_END[name]
            if outcome == "failed" and f.get("timed_out") == "True":
                outcome = "timed_out"
            attempt.conclude_outcome = outcome
            attempt.conclude_ms = _int(f.get("conclude_ms"))
            attempt.conclude_error = f.get("error")
            attempt.conclude_stdout_preview = f.get("stdout_preview")
            attempt.conclude_stderr_preview = f.get("stderr_preview")
            final = {"rejected": "rejected", "cancelled": "cancelled"}.get(outcome, "failed")
            self._finish(attempt, final, name, event)
        elif name in ("intent_concluded", "bootstrap_completed"):
            source = f.get("source", "")
            if name == "intent_concluded" or attempt.concluded_source is None:
                attempt.concluded_source = source
                attempt.concluded_phase_ms = _int(f.get("phase_ms"))
                attempt.concluded_total_ms = _int(f.get("total_ms"))
            if source in _EXECUTE_SOURCES:
                if attempt.execute_outcome is None:
                    attempt.execute_outcome = "concluded"
                    attempt.execute_ms = attempt.concluded_phase_ms
                self._finish(attempt, "concluded_at_execute", name, event)
            elif source in _CONCLUDE_SOURCES:
                attempt.conclude_outcome = "concluded"
                attempt.conclude_ms = attempt.concluded_phase_ms
                self._finish(attempt, "concluded_by_fallback", name, event)
        elif name == "conclude_write_failed":
            self.result.write_failures += 1
            self._finish(attempt, "failed", name, event)
        elif name == "project_inactive_during_conclude":
            self._finish(attempt, "cancelled", name, event)
        elif name == "released_intent":
            attempt.released = True
            if attempt.final == "open":
                self._finish(attempt, "released", name, event)
            elif attempt.final not in ("concluded_at_execute", "concluded_by_fallback"):
                attempt.ended_at = event.ts  # the claim is given back after the failure was logged
                attempt.duration_s = _seconds_between(attempt.dispatched_at, attempt.ended_at)

    @staticmethod
    def _touch(attempt: Attempt, event: Event) -> None:
        attempt.events.append(event.name)

    @staticmethod
    def _finish(attempt: Attempt, final: str, reason: str, event: Event) -> None:
        if attempt.final in ("concluded_at_execute", "concluded_by_fallback"):
            return  # a later release or warning does not undo a conclude
        attempt.final = final
        attempt.final_reason = reason
        attempt.ended_at = event.ts
        attempt.duration_s = _seconds_between(attempt.dispatched_at, attempt.ended_at)


def parse_logs(paths: Iterable[str], projects: Iterable[str] | None = None) -> ParseResult:
    result = ParseResult()
    builder = _Builder(result, set(projects) if projects else None)
    seen_files: list[str] = []
    for path, raw in iter_lines(paths):
        if not seen_files or seen_files[-1] != path:
            seen_files.append(path)
        result.lines += 1
        line = ANSI_RE.sub("", raw.rstrip("\r\n"))
        header = LINE_RE.search(line)
        if header is None:
            result.unparsed_lines += 1
            continue
        result.records += 1
        matched = match_event(header.group("message"))
        if matched is None:
            result.other_records += 1
            continue
        name, values = matched
        result.events_by_name[name] += 1
        builder.apply(Event(name, header.group("ts"), header.group("level"), values))
    result.files = seen_files
    return result


def percentiles(values: list[float]) -> dict[str, float | int | None]:
    def at(q: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * q
        low, high = math.floor(position), math.ceil(position)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 1)

    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": at(0.5),
        "p75": at(0.75),
        "p90": at(0.9),
        "p95": at(0.95),
        "max": round(ordered[-1], 1) if ordered else None,
    }


def _seconds(values: Iterable[int | None]) -> list[float]:
    return [v / 1000 for v in values if v is not None]


def _task_summary(attempts: list[Attempt], result: ParseResult, task: str) -> dict[str, Any]:
    runs = [a for a in attempts if a.task == task]
    execute_phase, conclude_phase = (
        ("explore_execute", "explore_conclude") if task == "explore" else ("bootstrap", "bootstrap_conclude")
    )
    timed_out = [a for a in runs if a.execute_outcome == "timed_out"]
    fallback = [a for a in runs if a.conclude_started_at is not None]
    return {
        "attempts": len(runs),
        "intents": len({(a.project, a.intent) for a in runs}),
        "attempts_without_dispatch_line": sum(1 for a in runs if a.dispatched_at is None),
        "configured_timeout_s": {
            "execute": dict(sorted(result.exec_timeouts.get(execute_phase, Counter()).items())),
            "conclude": dict(sorted(result.exec_timeouts.get(conclude_phase, Counter()).items())),
        },
        "execute_outcomes": dict(Counter(a.execute_outcome or "unknown" for a in runs).most_common()),
        "hit_timeout": len(timed_out),
        "hit_timeout_pct": round(100 * len(timed_out) / len(runs), 1) if runs else None,
        "conclude_fallback": {
            "started": len(fallback),
            "after_timeout": sum(1 for a in fallback if a.execute_outcome == "timed_out"),
            "after_parse_failure": sum(1 for a in fallback if a.execute_outcome == "parse_failed"),
            "outcomes": dict(Counter(a.conclude_outcome or "unknown" for a in fallback).most_common()),
            "not_started": dict(
                Counter(
                    a.conclude_outcome for a in runs if a.conclude_started_at is None and a.conclude_outcome
                ).most_common()
            ),
            "parse_errors": dict(Counter(a.conclude_error for a in fallback if a.conclude_error).most_common()),
        },
        "after_timeout_final": dict(Counter(a.final for a in timed_out).most_common()),
        "final": dict(Counter(a.final for a in runs).most_common()),
        "duration_s": {
            "execute_concluded": percentiles(_seconds(a.execute_ms for a in runs if a.execute_outcome == "concluded")),
            "execute_timed_out": percentiles(_seconds(a.execute_ms for a in timed_out)),
            "conclude_fallback": percentiles(_seconds(a.conclude_ms for a in fallback)),
            "dispatch_to_end": percentiles([a.duration_s for a in runs if a.duration_s is not None]),
        },
    }


def summarize(result: ParseResult) -> dict[str, Any]:
    return {
        "inputs": {
            "files": result.files,
            "lines": result.lines,
            "log_records": result.records,
            "events_matched": sum(result.events_by_name.values()),
            "other_records": result.other_records,
            "unparsed_lines": result.unparsed_lines,
            "events_by_name": dict(sorted(result.events_by_name.items())),
        },
        "explore": _task_summary(result.attempts, result, "explore"),
        "bootstrap": _task_summary(result.attempts, result, "bootstrap"),
        "json_rescues": {
            "total": len(result.rescues),
            "by_strategy": dict(Counter(r["strategy"] for r in result.rescues).most_common()),
            "by_phase": dict(Counter(r["phase"] for r in result.rescues).most_common()),
            "strict_errors": dict(Counter(r["strict_error"] for r in result.rescues).most_common()),
        },
        "reason": {
            "timed_out": result.reason_timed_out,
            "runs_started": result.exec_starts.get("reason_execute", 0),
        },
        "tasks": {
            "finished_not_success": dict(result.tasks_finished.most_common()),
            "cancelled": dict(result.tasks_cancelled.most_common()),
            "conclude_write_failures": result.write_failures,
        },
        "exec_start_linking": {
            "starts_by_phase": dict(sorted(result.exec_starts.items())),
            "linked": result.exec_linked,
            "unlinked": result.exec_unlinked,
            "ambiguous": result.exec_ambiguous,
        },
    }


def write_timeline(attempts: list[Attempt], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TIMELINE_COLUMNS)
        writer.writeheader()
        for attempt in attempts:
            row = {k: v for k, v in asdict(attempt).items() if k in TIMELINE_COLUMNS}
            row["events"] = " ".join(attempt.events)
            writer.writerow(row)


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def print_summary(summary: dict[str, Any]) -> None:
    inputs = summary["inputs"]
    print(
        f"Dispatcher log baseline: {len(inputs['files'])} file(s), {inputs['lines']} lines, "
        f"{inputs['log_records']} log records, {inputs['events_matched']} events used, "
        f"{inputs['unparsed_lines']} lines skipped (no log header)"
    )
    for task in ("explore", "bootstrap"):
        s = summary[task]
        if not s["attempts"]:
            print(f"\n{task.capitalize()}: no attempts in these logs")
            continue
        fb = s["conclude_fallback"]
        print(f"\n{task.capitalize()}: {s['attempts']} attempts on {s['intents']} intents")
        print(
            f"  configured timeout (s): execute {s['configured_timeout_s']['execute'] or '-'}, conclude {s['configured_timeout_s']['conclude'] or '-'}"
        )
        print(f"  hit the timeout: {s['hit_timeout']} ({_fmt(s['hit_timeout_pct'])}%)")
        print(f"  execute outcomes: {s['execute_outcomes']}")
        print(
            f"  conclude fallback: {fb['started']} started ({fb['after_timeout']} after a timeout, "
            f"{fb['after_parse_failure']} after unusable output); outcomes {fb['outcomes']}"
        )
        if fb["not_started"]:
            print(f"  conclude fallback not started: {fb['not_started']}")
        if s["hit_timeout"]:
            print(f"  runs that hit the timeout ended as: {s['after_timeout_final']}")
        print(f"  final: {s['final']}")
    rescues = summary["json_rescues"]
    print(f"\nJSON rescues: {rescues['total']} {rescues['by_strategy'] or ''}")
    print("\nDurations (s)               n      p50      p75      p90      p95      max")
    for task in ("explore", "bootstrap"):
        for label, stats in summary[task]["duration_s"].items():
            if stats["n"]:
                cells = "".join(f"{_fmt(stats[k]):>9}" for k in ("p50", "p75", "p90", "p95", "max"))
                print(f"  {task} {label:<20}{stats['n']:>4}{cells}")
    linking = summary["exec_start_linking"]
    if linking["unlinked"] or linking["ambiguous"]:
        print(
            f"\nNote: {linking['unlinked']} exec start line(s) could not be tied to an attempt and "
            f"{linking['ambiguous']} were ambiguous (same worker, same project); their timeouts still count above."
        )


def logged_messages(cairn_src: Path) -> set[str]:
    """Every string literal passed as the first argument to a .debug/.info/... call in the dispatcher."""
    messages: set[str] = set()
    for path in sorted((cairn_src / "cairn" / "dispatcher").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                messages.add(node.args[0].value)
    return messages


def check_formats(cairn_src: Path) -> list[str]:
    """Problems found when comparing these patterns with the dispatcher source; empty if all match."""
    problems = []
    logging_py = cairn_src / "cairn" / "dispatcher" / "logging.py"
    text = logging_py.read_text(encoding="utf-8") if logging_py.exists() else ""
    if HEADER_FORMAT not in text or HEADER_DATEFMT not in text:
        problems.append(f"log header format not found in {logging_py}")
    messages = logged_messages(cairn_src)
    problems.extend(
        f"{spec.name}: not a LOG message in this checkout: {spec.fmt!r}"
        for spec in EVENT_SPECS
        if spec.fmt not in messages
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="*", help="dispatcher log files in time order (.gz is fine; - reads stdin)")
    parser.add_argument(
        "--out-dir", default="phase0-baseline", help="where to write the CSV and JSON (default: %(default)s)"
    )
    parser.add_argument("--project", action="append", help="only this project id (repeatable)")
    parser.add_argument(
        "--check-formats",
        action="store_true",
        help="only compare the patterns with the dispatcher source and exit (non-zero on a mismatch)",
    )
    parser.add_argument(
        "--cairn-src", type=Path, default=DEFAULT_CAIRN_SRC, help="cairn/src of your checkout (default: %(default)s)"
    )
    args = parser.parse_args(argv)

    if args.check_formats:
        problems = check_formats(args.cairn_src)
        for problem in problems:
            print(f"MISMATCH {problem}")
        print(f"{len(EVENT_SPECS) - len(problems)} of {len(EVENT_SPECS)} patterns match {args.cairn_src}")
        return 1 if problems else 0
    if not args.logs:
        parser.error("give at least one log file, or --check-formats")
    if args.cairn_src.exists():
        for problem in check_formats(args.cairn_src):
            print(f"warning: {problem}", file=sys.stderr)

    result = parse_logs(args.logs, args.project)
    summary = summarize(result)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_timeline(result.attempts, out_dir / "baseline_timeline.csv")
    (out_dir / "baseline_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_summary(summary)
    print(f"\nWrote {out_dir / 'baseline_timeline.csv'} and {out_dir / 'baseline_summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
