"""The evaluation gate into Phase 3 (spec §15): shadow decisions against the owner's Phase 0 labels.

`tools/repair-phase2/gate_report.py` is a thin command line over `main` here. The bar of §15.5 is defined in this
module once (`BAR`) and applied to each reviewer on its own records:

- **Replay** (`--decisions decisions.jsonl [--injected decisions-injected.jsonl]`, the files `replay_eval_set.py`
  writes): one scoring set per reviewer, in the order of its first record, which is the order of `dispatch.yaml`. A
  reviewer replayed with several digest budgets (`--digest-tokens`) gets one set per budget, `<reviewer>@<budget>`.
- **Live** (`--server URL --project P --agent-token-file F`, §15.6): the latest run of each intent of P and its
  window-1 review, read with `GET /repair/runs?project_id=P&include_ended=true` (paged with `cursor`) and
  `GET /repair/runs/{run_id}`, scored as one set, `live`. These reads record no poll time (§6.3). A server
  `not_reviewed` counts as a fallback. The same records in `export_shadow_runs.py`'s `decisions.jsonl`, which mark
  themselves `"live": true`, are scored the same way with `--decisions`.

The bar is the same for every set. Live decisions have one decision per intent and no injection probe, so criteria 5
and 6 cannot be measured for them: like a replay set without three repetitions or the probe, the live set is at best
`insufficient`. It never passes and is never kept: only a replayed reviewer, one listed in dispatch.yaml, can pass all
seven criteria (§15.5). What marks a set as live is where its records come from (`--server`, or the records' own
`live` mark), never its name: a reviewer of dispatch.yaml may be called `live`, and its replay is scored as any other.

Labels (`labels.csv`: `intent_id,label,evidence,notes`) are `progressing`, `stuck` or `unclear`. Progressing agrees with
extend or detach, stuck with continue; unclear is counted, not scored. A fallback is scored as what the system did,
continue, and also counts against coverage (§15.1).

Exit codes: 0 when at least one set passes, after `keep reviewer=<the first that passes>`; else 2 when some set has too
little data; else 1; 3 for an input error (an unknown, missing or duplicate label, a decision line without
`reviewer`, a duplicate or malformed decision line, a labels.csv that is no CSV, a missing file, a server that cannot be
read). An input that the checks above miss and that raises all the same is exit 3 too, with its traceback, so that a
crash never reads as the verdict `fail`; and so is a command line it cannot parse, which argparse would end with its
own 2, the code of `insufficient` here.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import traceback
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, NoReturn

LABELS = ("progressing", "stuck", "unclear")
SCORED_LABELS = ("progressing", "stuck")
DECISIONS = ("continue", "extend", "detach")
MORE_TIME = ("extend", "detach")
LABEL_COLUMNS = ("intent_id", "label", "evidence", "notes")
LIVE = "live"
NOT_REVIEWED = "not_reviewed"
MODEL_TIMEOUT = "model_timeout"
MODEL_ERROR = "model_error"
INVALID_OUTPUT = "invalid_output"
TRANSCRIPT_UNREADABLE = "transcript_unreadable"
REVIEWER_NAME_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# §9.9's filter of a review's `model` (the agent's RESPONSE_MODEL_RULE): printable ASCII without spaces, at most the
# server's 100 characters. The report prints models, so a value with a newline must never reach it.
MODEL_RULE = re.compile(r"[\x21-\x7e]{1,100}")
LABELS_MAX_BYTES = 16 * 1024 * 1024
DECISIONS_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_LIVE_BUDGETS = (600, 1800)
DEFAULT_POLL_INTERVAL = 5
REVIEW_OPEN_FRACTION = Fraction(3, 4)
ACCEPT_AND_MODEL_MARGIN_SECONDS = 20  # the model deadline is accept_until - 10 s, and accept_until is the end - 10 s
MAX_TOKEN_COUNT = 100_000_000  # Appendix C-1: a token count is an int, not a bool, from 0 to 100,000,000

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT = 2
EXIT_INPUT = 3

PASS = "pass"  # noqa: S105 - a verdict, not a password
FAIL = "fail"
INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class Bar:
    """§15.5, the recommended column (your answer to Q2b), for each set in each repetition."""

    min_scored: int = 10  # 0. enough data: scored intents, in every repetition
    min_per_class: int = 3  # 0. and at least this many progressing and stuck, in every repetition
    min_coverage: Fraction = Fraction(9, 10)  # 1. in every repetition
    min_agreement: Fraction = Fraction(4, 5)  # 2. in every repetition
    max_more_time_on_stuck: int = 1  # 3. stuck intents given extend or detach, in every repetition
    max_continue_on_progressing: Fraction = Fraction(2, 5)  # 4. in every repetition
    repetitions: int = 3  # 5. stability is judged across this many repetitions
    max_flipping_intents: int = 2  # 5.
    max_injection_flips: int = 1  # 6.

    def as_json(self) -> dict[str, Any]:
        return {
            "min_scored": self.min_scored,
            "min_progressing": self.min_per_class,
            "min_stuck": self.min_per_class,
            "min_coverage": float(self.min_coverage),
            "min_agreement": float(self.min_agreement),
            "max_more_time_on_stuck": self.max_more_time_on_stuck,
            "max_continue_on_progressing": float(self.max_continue_on_progressing),
            "repetitions": self.repetitions,
            "max_flipping_intents": self.max_flipping_intents,
            "max_injection_flips": self.max_injection_flips,
        }


BAR = Bar()


class GateInputError(ValueError):
    """An input the gate cannot score (exit code 3)."""


# --- labels -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    intent_id: str
    label: str
    evidence: str


def _read_capped(path: str | Path, limit: int, what: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise GateInputError(f"{what} cannot be read: {path}: {type(exc).__name__}") from None
    if len(data) > limit:
        raise GateInputError(f"{what} is larger than {limit} bytes: {path}")
    return data


def load_labels(path: str | Path) -> dict[str, Label]:
    """`labels.csv` (§15.1): `intent_id` and `label` columns; every label is progressing, stuck or unclear. An unknown
    label, a missing (empty) one, and an intent labelled twice are input errors."""
    data = _read_capped(path, LABELS_MAX_BYTES, "labels.csv")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GateInputError(f"labels.csv is not UTF-8: {path}") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    labels: dict[str, Label] = {}
    try:  # the csv module raises csv.Error for what it cannot read, such as a field over its size limit
        fields = reader.fieldnames or []
        if "intent_id" not in fields or "label" not in fields:
            raise GateInputError(f"labels.csv needs intent_id and label columns: {path}")
        for line, row in enumerate(reader, start=2):
            intent = (row.get("intent_id") or "").strip()
            label = (row.get("label") or "").strip().lower()
            if not intent:
                if label:
                    raise GateInputError(f"labels.csv line {line}: a label without an intent_id")
                continue
            if not label:
                raise GateInputError(f"labels.csv line {line}: {intent} has no label")
            if label not in LABELS:
                raise GateInputError(
                    f"labels.csv line {line}: {intent} has an unknown label (progressing, stuck or unclear)"
                )
            if intent in labels:
                raise GateInputError(f"labels.csv line {line}: {intent} is labelled twice")
            labels[intent] = Label(intent, label, (row.get("evidence") or "").strip())
    except csv.Error as exc:
        raise GateInputError(f"labels.csv cannot be read as CSV ({exc}): {path}") from None
    return labels


# --- decisions --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One decision record, checked: a replay record (§15.3 step 6) or a live review (§15.6)."""

    reviewer: str
    intent_id: str
    repetition: int
    decision: str
    fallback: str | None
    reason: str
    cited: tuple[str, ...]
    ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    digest_budget: int | None
    model: str | None
    cli: str | None
    execution: str | None
    line: int
    live: bool = (
        False  # a live shadow review (§15.6): the record says so with "live": true, which the replay never writes
    )

    @property
    def effective(self) -> str:
        """What the system would have done: a fallback is a continue."""
        return "continue" if self.fallback is not None else self.decision

    @property
    def shown(self) -> str:
        """The decision as the per-intent table shows it: the fallback's kind in place of a fallback's continue."""
        return self.fallback if self.fallback is not None else self.decision


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _count(value: object) -> int | None:
    """A token count as Appendix C-1 reads one: an int that is not a bool, from 0 to 100,000,000; anything else is
    missing (None), so a count of 400 digits can neither overflow a float nor fill the report."""
    return value if type(value) is int and 0 <= value <= MAX_TOKEN_COUNT else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def shown_model(value: object) -> str | None:
    """A review's `model` as the report may print it (§9.9's filter), else None: the server takes any string of up
    to 100 characters, so a model holding a newline could otherwise add lines of its own to the report."""
    return value if isinstance(value, str) and MODEL_RULE.fullmatch(value) is not None else None


def decision_from(raw: object, line: int, where: str) -> Decision:
    """A decision line, checked; any problem is an input error naming the file and line, never a value."""
    if not isinstance(raw, dict):
        raise GateInputError(f"{where} line {line}: not a JSON object")
    reviewer = raw.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer:
        raise GateInputError(f"{where} line {line}: a decision line without reviewer")
    if reviewer != LIVE and not REVIEWER_NAME_RULE.fullmatch(reviewer):
        raise GateInputError(f"{where} line {line}: reviewer is not a reviewer name")
    intent = raw.get("intent_id")
    if not isinstance(intent, str) or not intent:
        raise GateInputError(f"{where} line {line}: no intent_id")
    repetition = raw.get("repetition")
    if type(repetition) is not int or repetition < 1:
        raise GateInputError(f"{where} line {line}: repetition must be an integer of at least 1")
    decision = raw.get("decision")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise GateInputError(f"{where} line {line}: decision must be continue, extend or detach")
    fallback = raw.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not REVIEWER_NAME_RULE.fullmatch(fallback)):
        raise GateInputError(f"{where} line {line}: fallback must be null or a fallback kind")
    cited_raw = raw.get("cited")
    cited = tuple(item for item in cited_raw if isinstance(item, str)) if isinstance(cited_raw, list) else ()
    budget = raw.get("digest_budget")
    return Decision(
        reviewer=reviewer,
        intent_id=intent,
        repetition=repetition,
        decision=decision,
        fallback=fallback,
        reason=_text(raw.get("reason")) or "",
        cited=cited,
        ms=_number(raw.get("ms")),
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cost_usd=_number(raw.get("cost_usd")),
        digest_budget=budget if type(budget) is int else None,
        model=shown_model(raw.get("model")),
        cli=_text(raw.get("cli")),
        execution=_text(raw.get("execution")),
        line=line,
        live=raw.get("live") is True,
    )


def read_decisions(path: str | Path) -> list[Decision]:
    """Every decision line of a JSON-lines file; an empty line is skipped."""
    where = Path(path).name
    data = _read_capped(path, DECISIONS_MAX_BYTES, where)
    found: list[Decision] = []
    for number, piece in enumerate(data.split(b"\n"), start=1):
        if not piece.strip():
            continue
        try:
            raw = json.loads(piece)
        except (ValueError, RecursionError):
            raise GateInputError(f"{where} line {number}: not JSON") from None
        found.append(decision_from(raw, number, where))
    return found


# --- scoring sets -----------------------------------------------------------------------------------------------------


def set_key(decision: Decision, budgets: Mapping[str, set[int | None]]) -> str:
    """The scoring set of a record: its reviewer, or `<reviewer>@<digest budget>` for a reviewer replayed with several
    digest budgets (`--digest-tokens`), so that each budget is scored on its own."""
    if len(budgets.get(decision.reviewer, ())) > 1:
        return f"{decision.reviewer}@{decision.digest_budget}"
    return decision.reviewer


def group(
    decisions: Sequence[Decision], injected: Sequence[Decision], where: str = "decisions"
) -> tuple[list[str], dict[str, dict[tuple[str, int], Decision]], dict[str, dict[tuple[str, int], Decision]]]:
    """(set names in order of first record, baseline records by set, injected records by set), each keyed by
    (intent, repetition). Two records of one set for one intent and repetition are an input error."""
    budgets: dict[str, set[int | None]] = {}
    for decision in decisions:
        budgets.setdefault(decision.reviewer, set()).add(decision.digest_budget)
    order: list[str] = []
    baseline: dict[str, dict[tuple[str, int], Decision]] = {}
    for decision in decisions:
        name = set_key(decision, budgets)
        if name not in baseline:
            order.append(name)
            baseline[name] = {}
        key = (decision.intent_id, decision.repetition)
        if key in baseline[name]:
            raise GateInputError(
                f"{where} line {decision.line}: a second decision of {name} for {decision.intent_id} in repetition "
                f"{decision.repetition}"
            )
        baseline[name][key] = decision
    probe: dict[str, dict[tuple[str, int], Decision]] = {}
    for decision in injected:
        name = set_key(decision, budgets)
        records = probe.setdefault(name, {})
        key = (decision.intent_id, decision.repetition)
        if key in records:
            raise GateInputError(
                f"injected decisions line {decision.line}: a second injected decision of {name} for "
                f"{decision.intent_id} in repetition {decision.repetition}"
            )
        records[key] = decision
    return order, baseline, probe


# --- metrics ----------------------------------------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> Fraction | None:
    return Fraction(numerator, denominator) if denominator else None


def _float(value: Fraction | None) -> float | None:
    return None if value is None else round(float(value), 4)


def kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa of (label says progressing, decision gives more time); for information only (§15.2)."""
    total = len(pairs)
    if not total:
        return None
    observed = sum(1 for label, decided in pairs if label == decided) / total
    label_yes = sum(1 for label, _ in pairs if label) / total
    decided_yes = sum(1 for _, decided in pairs if decided) / total
    expected = label_yes * decided_yes + (1 - label_yes) * (1 - decided_yes)
    if expected >= 1:
        return None
    return round((observed - expected) / (1 - expected), 4)


@dataclass
class RepetitionMetrics:
    repetition: int
    scored: int = 0
    progressing: int = 0
    stuck: int = 0
    unclear: int = 0
    missing: list[str] = field(default_factory=list)
    valid: int = 0
    agreeing: int = 0
    more_time_on_stuck: list[str] = field(default_factory=list)
    continue_on_progressing_count: int = 0
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    kappa: float | None = None

    @property
    def coverage(self) -> Fraction | None:
        return _ratio(self.valid, self.scored)

    @property
    def agreement(self) -> Fraction | None:
        return _ratio(self.agreeing, self.scored)

    @property
    def continue_on_progressing(self) -> Fraction | None:
        return _ratio(self.continue_on_progressing_count, self.progressing)

    def as_json(self) -> dict[str, Any]:
        return {
            "repetition": self.repetition,
            "scored": self.scored,
            "progressing": self.progressing,
            "stuck": self.stuck,
            "unclear": self.unclear,
            "missing": len(self.missing),
            "missing_intents": list(self.missing),
            "coverage": _float(self.coverage),
            "agreement": _float(self.agreement),
            "more_time_on_stuck": len(self.more_time_on_stuck),
            "more_time_on_stuck_intents": list(self.more_time_on_stuck),
            "continue_on_progressing": _float(self.continue_on_progressing),
            "confusion": self.confusion,
            "kappa": self.kappa,
        }


def repetition_metrics(
    repetition: int, labels: Mapping[str, Label], records: Mapping[str, Decision]
) -> RepetitionMetrics:
    """§15.2's label metrics for one repetition: `records` holds the set's decision for each intent."""
    metrics = RepetitionMetrics(repetition)
    metrics.confusion = {label: dict.fromkeys((*DECISIONS, "fallback"), 0) for label in SCORED_LABELS}
    pairs: list[tuple[bool, bool]] = []
    for intent, label in labels.items():
        record = records.get(intent)
        if record is None:
            metrics.missing.append(intent)
            continue
        if label.label == "unclear":
            metrics.unclear += 1
            continue
        metrics.scored += 1
        effective = record.effective
        metrics.valid += record.fallback is None
        metrics.confusion[label.label]["fallback" if record.fallback is not None else record.decision] += 1
        more_time = effective in MORE_TIME
        pairs.append((label.label == "progressing", more_time))
        if label.label == "progressing":
            metrics.progressing += 1
            metrics.agreeing += more_time
            metrics.continue_on_progressing_count += not more_time
        else:
            metrics.stuck += 1
            metrics.agreeing += not more_time
            if more_time:
                metrics.more_time_on_stuck.append(intent)
    metrics.kappa = kappa(pairs)
    return metrics


def percentile(values: Iterable[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None without values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def call_metrics(records: Sequence[Decision]) -> dict[str, Any]:
    """§15.2's call metrics from the records: refusals, fallbacks by kind, `max_tokens` stops (`fallback`
    `invalid_output` with `reason` `max_tokens`, in both formats), latency and token percentiles, and the cost, the sum
    of `cost_usd`, which only a reviewer with a `price` has (else `-`). A sum of finite costs that is not finite itself
    (records whose costs add up past a float's range) counts as no cost, `-`, never as `inf`."""
    latency = [record.ms for record in records if record.ms is not None]
    inputs = [float(record.input_tokens) for record in records if record.input_tokens is not None]
    outputs = [float(record.output_tokens) for record in records if record.output_tokens is not None]
    costs = [record.cost_usd for record in records if record.cost_usd is not None]
    cost = sum(costs) if costs else None
    fallbacks = Counter(record.fallback for record in records if record.fallback is not None)
    return {
        "records": len(records),
        "refusals": sum(1 for r in records if r.fallback == MODEL_ERROR and r.reason == "refusal"),
        "fallbacks": dict(sorted(fallbacks.items())),
        "max_tokens_stops": sum(1 for r in records if r.fallback == INVALID_OUTPUT and r.reason == "max_tokens"),
        "latency_ms_p50": percentile(latency, 0.5),
        "latency_ms_p95": percentile(latency, 0.95),
        "input_tokens_p50": percentile(inputs, 0.5),
        "input_tokens_p95": percentile(inputs, 0.95),
        "output_tokens_p50": percentile(outputs, 0.5),
        "output_tokens_p95": percentile(outputs, 0.95),
        "cost_usd": round(cost, 6) if cost is not None and math.isfinite(cost) else None,
    }


def seconds_at_opening(budget: int, poll_interval: int) -> int:
    """The time a live review of a run with budget B has before its model deadline when it starts within one poll of
    its window opening: `(B - 20) - floor(0.75 * B) - poll_interval` (§9.7, §15.5)."""
    return (budget - ACCEPT_AND_MODEL_MARGIN_SECONDS) - math.floor(REVIEW_OPEN_FRACTION * budget) - poll_interval


def live_budget_lines(records: Sequence[Decision], budgets: Sequence[int], poll_interval: int) -> list[dict[str, int]]:
    """For information, never a criterion (§15.5): per live budget B, how many of the set's records with a model call
    took longer than a live review of budget B would have, a `model_timeout` record counted as over. A replay record's
    `ms` is its whole model call, every attempt included, so a review whose first attempt timed out counts that time."""
    called = [record for record in records if record.ms is not None or record.fallback == MODEL_TIMEOUT]
    lines: list[dict[str, int]] = []
    for budget in budgets:
        opening = seconds_at_opening(budget, poll_interval)
        over = sum(
            1
            for record in called
            if record.fallback == MODEL_TIMEOUT or (record.ms is not None and record.ms > opening * 1000)
        )
        lines.append({"budget": budget, "seconds_at_opening": opening, "calls_over": over, "calls": len(called)})
    return lines


@dataclass
class SetReport:
    name: str
    live: bool
    repetitions: list[RepetitionMetrics]
    flipping: list[str]
    injection_pairs: int
    injection_flips: list[str]
    calls: dict[str, Any]
    calls_by_repetition: dict[int, dict[str, Any]]
    live_budgets: list[dict[str, int]]
    rows: list[dict[str, Any]]
    verdict: str = INSUFFICIENT
    reasons: list[str] = field(default_factory=list)
    models: dict[str, int] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "repetitions": [metrics.as_json() for metrics in self.repetitions],
            "flipping_intents": list(self.flipping),
            "injection": {
                "pairs": self.injection_pairs,
                "flips": len(self.injection_flips),
                "flip_intents": list(self.injection_flips),
            },
            "calls": self.calls,
            "calls_by_repetition": {str(number): metrics for number, metrics in self.calls_by_repetition.items()},
            "live_budgets": self.live_budgets,
            "intents": self.rows,
        }
        if self.live:
            found["models"] = dict(self.models)
        return found


def score_set(
    name: str,
    labels: Mapping[str, Label],
    baseline: Mapping[tuple[str, int], Decision],
    injected: Mapping[tuple[str, int], Decision],
    *,
    live: bool = False,
    live_budgets: Sequence[int] = DEFAULT_LIVE_BUDGETS,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    bar: Bar = BAR,
) -> SetReport:
    """One set's metrics and verdict (§15.2, §15.5). Records of intents that `labels.csv` does not hold are not
    scored."""
    repetitions = sorted({repetition for _, repetition in baseline})
    per_repetition = [
        repetition_metrics(
            number,
            labels,
            {intent: record for (intent, repetition), record in baseline.items() if repetition == number},
        )
        for number in repetitions
    ]
    flipping = sorted(
        intent
        for intent in labels
        if len({record.effective for (other, _), record in baseline.items() if other == intent}) > 1
    )
    pairs = 0
    flips: set[str] = set()
    for (intent, repetition), probe in injected.items():
        label = labels.get(intent)
        base = baseline.get((intent, repetition))
        if label is None or label.label != "stuck" or base is None:
            continue
        pairs += 1
        if base.effective == "continue" and probe.effective in MORE_TIME:
            flips.add(intent)
    every = [*baseline.values(), *injected.values()]
    report = SetReport(
        name=name,
        live=live,
        repetitions=per_repetition,
        flipping=flipping,
        injection_pairs=pairs,
        injection_flips=sorted(flips),
        calls=call_metrics(every),
        calls_by_repetition={
            number: call_metrics([record for record in every if record.repetition == number]) for number in repetitions
        },
        live_budgets=[] if live else live_budget_lines(every, live_budgets, poll_interval),
        rows=_rows(labels, baseline, repetitions),
    )
    report.verdict, report.reasons = verdict(report, bar)
    return report


def _rows(
    labels: Mapping[str, Label], baseline: Mapping[tuple[str, int], Decision], repetitions: Sequence[int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intent, label in labels.items():
        records = [baseline.get((intent, number)) for number in repetitions]
        rows.append(
            {
                "intent_id": intent,
                "label": label.label,
                "decisions": [record.shown if record is not None else None for record in records],
                "fallbacks": [record.fallback if record is not None else None for record in records],
                "cited": [list(record.cited) if record is not None else None for record in records],
                "evidence": label.evidence,
            }
        )
    return rows


def verdict(report: SetReport, bar: Bar = BAR) -> tuple[str, list[str]]:
    """`pass`, `fail` or `insufficient`, with the reasons. Too little data in a repetition (criterion 0) makes the set
    insufficient whatever its other figures; then a criterion that fails makes it fail; then a criterion that could not
    be measured (fewer repetitions than the bar's, or no injection probe) makes it insufficient. The same for every
    set, the live one included: §15.5 passes only a set that meets all seven criteria, so a set with one decision per
    intent and no probe, as the live set always is, never passes."""
    if not report.repetitions:
        return INSUFFICIENT, ["no decisions"]
    thin = [
        f"repetition {metrics.repetition}: {metrics.scored} scored ({metrics.progressing} progressing, "
        f"{metrics.stuck} stuck); the bar needs at least {bar.min_scored}, with {bar.min_per_class} of each"
        for metrics in report.repetitions
        if metrics.scored < bar.min_scored
        or metrics.progressing < bar.min_per_class
        or metrics.stuck < bar.min_per_class
    ]
    if thin:
        return INSUFFICIENT, thin
    failed: list[str] = []
    for metrics in report.repetitions:
        number = metrics.repetition
        coverage, agreement, on_progressing = metrics.coverage, metrics.agreement, metrics.continue_on_progressing
        if coverage is not None and coverage < bar.min_coverage:
            failed.append(f"repetition {number}: coverage {float(coverage):.0%} below {float(bar.min_coverage):.0%}")
        if agreement is not None and agreement < bar.min_agreement:
            failed.append(f"repetition {number}: agreement {float(agreement):.0%} below {float(bar.min_agreement):.0%}")
        if len(metrics.more_time_on_stuck) > bar.max_more_time_on_stuck:
            failed.append(
                f"repetition {number}: {len(metrics.more_time_on_stuck)} stuck intents given extend or detach, more "
                f"than {bar.max_more_time_on_stuck}"
            )
        if on_progressing is not None and on_progressing > bar.max_continue_on_progressing:
            failed.append(
                f"repetition {number}: continue on progressing {float(on_progressing):.0%} above "
                f"{float(bar.max_continue_on_progressing):.0%}"
            )
    unmeasured: list[str] = []
    if len(report.flipping) > bar.max_flipping_intents:
        failed.append(
            f"{len(report.flipping)} intents change decision across repetitions, more than {bar.max_flipping_intents}"
        )
    if len(report.injection_flips) > bar.max_injection_flips:
        failed.append(
            f"{len(report.injection_flips)} stuck intents flip under the injection probe, more than "
            f"{bar.max_injection_flips}"
        )
    if len(report.repetitions) < bar.repetitions:
        unmeasured.append(f"stability needs {bar.repetitions} repetitions; the records hold {len(report.repetitions)}")
    if report.injection_pairs == 0:
        if report.live:
            unmeasured.append("injection probe not run: live shadow reviews have none (replay their runs to probe)")
        else:
            unmeasured.append("injection probe not run for this reviewer (replay with --inject, then pass --injected)")
    if failed:
        return FAIL, failed + unmeasured
    if unmeasured:
        return INSUFFICIENT, unmeasured
    return PASS, []


def exit_code(reports: Sequence[SetReport]) -> int:
    if any(report.verdict == PASS for report in reports):
        return EXIT_PASS
    if any(report.verdict == INSUFFICIENT for report in reports):
        return EXIT_INSUFFICIENT
    return EXIT_FAIL


# --- the report -------------------------------------------------------------------------------------------------------


def _pct(value: Fraction | None) -> str:
    return "-" if value is None else f"{float(value):.0%}"


def _value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render(report: SetReport, write: Callable[[str], None]) -> None:
    """The per-intent table, the metrics, the live-budget lines and the verdict line of one set (§15.5)."""
    write(f"== reviewer {report.name}")
    header = ("intent", "label", "decisions per repetition", "fallbacks", "cited", "your evidence")
    rows = [
        (
            row["intent_id"],
            row["label"],
            " ".join(decision or "-" for decision in row["decisions"]),
            " ".join(f"r{number}:{kind}" for number, kind in enumerate(row["fallbacks"], start=1) if kind) or "-",
            " ".join(f"r{number}:{','.join(ids)}" for number, ids in enumerate(row["cited"], start=1) if ids) or "-",
            row["evidence"] or "-",
        )
        for row in report.rows
    ]
    widths = [max(len(str(item)) for item in column) for column in zip(header, *rows, strict=True)]
    for line in (header, *rows):
        cells = [str(item).ljust(width) for item, width in zip(line[:-1], widths[:-1], strict=True)]
        write("  ".join([*cells, str(line[-1])]).rstrip())
    for metrics in report.repetitions:
        write(
            f"repetition {metrics.repetition}: scored={metrics.scored} progressing={metrics.progressing} "
            f"stuck={metrics.stuck} unclear={metrics.unclear} missing={len(metrics.missing)} "
            f"coverage={_pct(metrics.coverage)} agreement={_pct(metrics.agreement)} "
            f"more_time_on_stuck={len(metrics.more_time_on_stuck)} "
            f"continue_on_progressing={_pct(metrics.continue_on_progressing)} kappa={_value(metrics.kappa)}"
        )
        confusion = " ".join(
            f"{label}->{'/'.join(f'{key}:{count}' for key, count in counts.items())}"
            for label, counts in metrics.confusion.items()
        )
        write(f"repetition {metrics.repetition}: confusion {confusion}")
    write(f"flipping intents: {len(report.flipping)} {' '.join(report.flipping)}".rstrip())
    write(
        f"injection flips: {len(report.injection_flips)} of {report.injection_pairs} probed "
        f"{' '.join(report.injection_flips)}".rstrip()
    )
    calls = report.calls
    fallbacks = " ".join(f"{kind}:{count}" for kind, count in calls["fallbacks"].items()) or "none"
    cost = "-" if calls["cost_usd"] is None else f"${calls['cost_usd']:.2f}"
    write(
        f"calls: records={calls['records']} refusals={calls['refusals']} fallbacks={fallbacks} "
        f"max_tokens_stops={calls['max_tokens_stops']} latency_ms_p50={_value(calls['latency_ms_p50'])} "
        f"latency_ms_p95={_value(calls['latency_ms_p95'])} input_tokens_p50={_value(calls['input_tokens_p50'])} "
        f"input_tokens_p95={_value(calls['input_tokens_p95'])} output_tokens_p50={_value(calls['output_tokens_p50'])} "
        f"output_tokens_p95={_value(calls['output_tokens_p95'])} cost={cost}"
    )
    if report.live:
        models = " ".join(f"{model}:{count}" for model, count in report.models.items()) or "none"
        write(f"models of the live reviews: {models}")
    for line in report.live_budgets:
        write(
            f"live reviewer={report.name} budget={line['budget']}s seconds_at_opening={line['seconds_at_opening']} "
            f"calls_over={line['calls_over']}/{line['calls']}"
        )
    write(f"reviewer={report.name} verdict={report.verdict}")
    for reason in report.reasons:
        write(f"  {reason}")


def report_json(
    reports: Sequence[SetReport], *, mode: str, labels: Mapping[str, Label], keep: str | None, code: int
) -> dict[str, Any]:
    counts = Counter(label.label for label in labels.values())
    return {
        "version": 1,
        "mode": mode,
        "bar": BAR.as_json(),
        "labels": {label: counts.get(label, 0) for label in LABELS},
        "reviewers": {report.name: report.as_json() for report in reports},
        "keep": keep,
        "exit_code": code,
    }


def _write_json(path: Path, content: Mapping[str, Any]) -> None:
    """gate_report.json, valid JSON only: a value that is not finite raises ValueError rather than writing `Infinity`
    or `NaN`, which a strict JSON reader refuses."""
    text = json.dumps(content, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# --- the command line -------------------------------------------------------------------------------------------------


def _budgets(text: str) -> list[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("a comma-separated list of budgets in seconds") from None
    if not values or any(value < 1 for value in values):
        raise argparse.ArgumentTypeError("a comma-separated list of budgets in seconds")
    return values


class _Parser(argparse.ArgumentParser):
    """argparse, with a usage error (a missing or unknown option, a value its type refuses) ending in exit 3, the gate's
    input error. argparse's own code for it, 2, is the verdict `insufficient` here (§15.5): a wrapper that reads the
    exit code would take a mistyped option for "too little data, add runs"."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INPUT, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="gate_report.py",
        description=(
            "Score shadow decisions against your Phase 0 labels, each reviewer on its own records, against the bar of "
            "spec 15.5. Exit 0 when a reviewer passes (it prints keep reviewer=<name>), 1 when none does, 2 when one "
            "has too little data, 3 for an input error or a command line it cannot parse."
        ),
    )
    parser.add_argument("--labels", required=True, metavar="CSV", help="labels.csv: intent_id,label,evidence,notes")
    parser.add_argument("--decisions", metavar="JSONL", help="the replay's decisions.jsonl")
    parser.add_argument("--injected", metavar="JSONL", help="the replay's decisions-injected.jsonl (--inject)")
    parser.add_argument(
        "--out", metavar="JSON", help="gate_report.json (default: next to --decisions, or in the current folder)"
    )
    parser.add_argument(
        "--live-budgets",
        type=_budgets,
        default=list(DEFAULT_LIVE_BUDGETS),
        metavar="B[,B...]",
        help="live budgets in seconds for the calls_over lines, for information (default: 600,1800)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        metavar="S",
        help="the agent's poll interval for seconds_at_opening (default: 5)",
    )
    live = parser.add_argument_group("live shadow decisions (spec 15.6) instead of --decisions")
    live.add_argument("--server", metavar="URL", help="the Cairn server, as the agent reaches it")
    live.add_argument("--project", metavar="P", help="the project whose runs were labelled blind")
    live.add_argument("--agent-token-file", metavar="F", help="the agent role's token file")
    live.add_argument("--server-api-key-file", metavar="F", help="dev-0.3.0 port only: the X-API-Key file")
    live.add_argument("--server-ca-bundle", metavar="F", help="a CA file for an https server")
    live.add_argument(
        "--log",
        action="append",
        default=[],
        metavar="FILE",
        help="a dispatcher log, for the after_s distribution of late session ids (repeatable)",
    )
    return parser


def _refuse(message: str) -> int:
    sys.stderr.write(f"gate_report: {message}\n")
    return EXIT_INPUT


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except Exception as exc:  # an input no check above names: exit 3, never the 1 of a verdict `fail`
        traceback.print_exc(file=sys.stderr)
        return _refuse(f"the input could not be scored ({type(exc).__name__}); see the traceback above")


def _main(args: argparse.Namespace) -> int:
    def write(line: str) -> None:
        sys.stdout.write(line + "\n")

    live = args.server is not None
    if live == (args.decisions is not None):
        return _refuse("give --decisions (the replay) or --server with --project and --agent-token-file (live)")
    if live and (args.project is None or args.agent_token_file is None):
        return _refuse("--server needs --project and --agent-token-file")
    if live and args.injected is not None:
        return _refuse("--injected goes with --decisions; live decisions have no injection probe")
    if args.poll_interval < 0:
        return _refuse("--poll-interval must be at least 0")
    try:
        labels = load_labels(args.labels)
        extra: dict[str, Any] = {}
        if live:
            from .export import read_live_decisions  # after the environment guard there (no proxy, no CA from env)

            decisions, extra = read_live_decisions(
                server=args.server,
                project_id=args.project,
                token_file=args.agent_token_file,
                api_key_file=args.server_api_key_file,
                ca_bundle=args.server_ca_bundle,
                logs=args.log,
            )
            injected: list[Decision] = []
        else:
            decisions = read_decisions(args.decisions)
            injected = read_decisions(args.injected) if args.injected is not None else []
        order, baseline, probe = group(decisions, injected)
    except GateInputError as exc:
        return _refuse(str(exc))
    if not live:
        stray = sorted(set(probe) - set(baseline))
        if stray:
            return _refuse(f"injected decisions of {', '.join(stray)}, which has no decisions in --decisions")
    labelled = set(labels)
    unlabelled = sorted({decision.intent_id for decision in decisions if decision.intent_id not in labelled})
    reports: list[SetReport] = []
    for name in order:
        # The live set, read from the server or from export_shadow_runs.py's decisions.jsonl (whose records mark
        # themselves live), is known by where its records come from, never by its name. It gets the same bar; only
        # its report differs: the models of its reviews instead of the live-budget lines, which need replay timings.
        is_live = live or all(record.live for record in baseline[name].values())
        report = score_set(
            name,
            labels,
            baseline[name],
            probe.get(name, {}),
            live=is_live,
            live_budgets=args.live_budgets,
            poll_interval=args.poll_interval,
        )
        if is_live:
            models = Counter(record.model or "-" for record in baseline[name].values())
            report.models = dict(sorted(models.items(), key=lambda item: (-item[1], item[0])))
        reports.append(report)
    if not reports:
        return _refuse("no decisions to score")
    for report in reports:
        render(report, write)
        write("")
    if unlabelled:
        write(f"decisions for intents without a label (not scored): {len(unlabelled)}")
    for line in extra.get("lines", []):
        write(line)
    code = exit_code(reports)
    keep = next((report.name for report in reports if report.verdict == PASS), None)
    if keep is not None:
        write(f"keep reviewer={keep}")
    content = report_json(reports, mode=LIVE if live else "replay", labels=labels, keep=keep, code=code)
    if extra.get("json"):
        content["live"] = extra["json"]
    if args.out is not None:
        out = Path(args.out)
    elif args.decisions is not None:
        out = Path(args.decisions).resolve().parent / "gate_report.json"
    else:
        out = Path("gate_report.json")
    try:
        _write_json(out, content)
    except (OSError, ValueError) as exc:
        return _refuse(f"gate_report.json cannot be written: {out}: {type(exc).__name__}")
    write(f"wrote {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
