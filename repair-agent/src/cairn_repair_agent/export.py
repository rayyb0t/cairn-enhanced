"""Live shadow decisions (spec §15.6): the reads that `gate.py` scores as the set `live`, and
`tools/repair-phase2/export_shadow_runs.py`, a thin command line over `main` here, which writes live shadow runs as
Phase 0-style folders for blind labelling.

Reads. `GET /repair/runs?project_id=P&include_ended=true`, paged with `cursor` (newest registration first), then
`GET /repair/runs/{run_id}` for the stored reviews; with the agent token, from the agent's own server client
(`RepairServerClient`: no proxy, no redirect, nothing from the environment). With `include_ended`, the server records
no poll time, so these reads never change a not-reviewed record's `why`; their sweep may still end rows and record
not-reviewed issues, like any sweep (§6.3). Per intent, the latest run (by registration) and its window-1 review are
used; a run without one is not scored, and a server `not_reviewed` is a fallback.

Export. `--out shadow-P` gets one folder per intent whose latest run has a window-1 review: `intent.json` (the intent,
and the run under Phase 0's `timed_out_attempts` with its budget and review window, so that `replay_eval_set.py` can
read the folder), `digest.txt` (the stored digest, what the model saw), and, while the project's copy folder is kept
(§8.8, Removal), the copy as `transcript.redacted.jsonl` with `redaction.json`: the dispatcher already removed the
worker secrets from it (§8.8), and `redaction.json` says so (`written_by: export_shadow_runs.py`), so that
`redact_eval_set.py` leaves the folder as it is. The copy root is `transcripts.root` of the agent's `repair.yaml`
(`--config`, as the replay reads it) or `--copy-root`; with neither, the tool cannot know where the copies are, exports
none and says so (a departure from §15.6's command line, which names no copy root). `shadow-P/labels.csv` is a
template to fill in. Decisions go only to `shadow-P/decisions.jsonl`, outside the intent folders: label before you open
it, and before you open the project's graph, which shows each decision on its node.

What the server returns is read as untrusted: a review's `model` is shown only when it is printable ASCII without
spaces, at most 100 characters (the agent's own filter, §9.9), and an `evidence` that is not a list counts as none.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import StartupError, load_config, token_problem
from .decision import CITATION
from .gate import (
    LIVE,
    NOT_REVIEWED,
    TRANSCRIPT_UNREADABLE,
    Decision,
    GateInputError,
    decision_from,
    percentile,
    shown_model,
)
from .mirror import valid_project_id, valid_run_id

if TYPE_CHECKING:
    from .server import RepairServerClient

PAGE_SIZE = 200
SECRET_FILE_MAX_BYTES = 65536
COPY_MAX_BYTES = 64 * 1024 * 1024
REVIEW_OPEN_FRACTION = 0.75
REVIEW_CLOSE_FRACTION = 0.80
LATE_SOURCES = ("output", "final_output")
WHY_VALUES = ("agent_seen", "agent_not_seen")
REDACTED_NAME = "transcript.redacted.jsonl"
REDACTION_REPORT = "redaction.json"
WRITTEN_BY = "export_shadow_runs.py"  # in redaction.json, so that redact_eval_set.py leaves the folder as it is
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INPUT = 3
_SESSION_LINE = re.compile(
    r"repair shadow session registered project=(?P<project>\S+) intent=\S+ worker=.* run=(?P<run>\S+) "
    r"source=(?P<source>\S+) after_s=(?P<after>\S+)\s*$"
)


class LiveReadError(GateInputError):
    """The server's answers cannot be read (the gate's exit code 3)."""


# --- the server -------------------------------------------------------------------------------------------------------


def read_secret_file(path: str, what: str, *, token: bool) -> str:
    """A secret file's content with surrounding ASCII whitespace removed: a regular file of at most 64 KiB, printable
    ASCII, and for a token at least 32 characters (the agent's rules, §9.2). Never printed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise LiveReadError(f"{what} cannot be read: {path}: {type(exc).__name__}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LiveReadError(f"{what} is not a regular file: {path}")
        data = os.read(fd, SECRET_FILE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > SECRET_FILE_MAX_BYTES:
        raise LiveReadError(f"{what} is larger than {SECRET_FILE_MAX_BYTES} bytes: {path}")
    try:
        value = data.strip().decode("ascii")
    except UnicodeDecodeError:
        raise LiveReadError(f"{what} refused: {path}: not_ascii") from None
    if token:
        problem = token_problem(value)
    else:
        problem = "empty" if not value else (None if value.isprintable() else "not_ascii")
    if problem is not None:
        raise LiveReadError(f"{what} refused: {path}: {problem}")
    return value


def server_client(server: str, token_file: str, api_key_file: str | None, ca_bundle: str | None) -> RepairServerClient:
    """The agent's own server client, built after the environment guard (§9.3 steps 3 and 4)."""
    from . import envguard

    token = read_secret_file(token_file, "agent token", token=True)
    api_key = read_secret_file(api_key_file, "server API key", token=False) if api_key_file is not None else None
    if not server.startswith(("http://", "https://")):
        raise LiveReadError("--server must be an http:// or https:// URL")
    envguard.strip_environment()
    envguard.quiet_library_loggers()
    from .server import RepairServerClient

    return RepairServerClient(server, token, api_key=api_key, ca_bundle=ca_bundle)


def live_runs(client: RepairServerClient, project_id: str, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Every run of the project, ended ones included, newest registration first, page after page by `next_cursor`."""
    runs: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        answer = client.list_runs(project_id=project_id, include_ended=True, limit=page_size, cursor=cursor)
        body = answer.body
        if answer.status != 200 or not isinstance(body, dict) or not isinstance(body.get("runs"), list):
            raise LiveReadError(f"GET /repair/runs answered {answer.status} without a run list")
        runs.extend(run for run in body["runs"] if isinstance(run, dict) and run.get("project_id") == project_id)
        cursor = body.get("next_cursor")
        if cursor is None:
            return runs
        if not isinstance(cursor, str) or cursor in seen:
            raise LiveReadError("GET /repair/runs gave a next_cursor it gave before, or one that is not a string")
        seen.add(cursor)


def latest_runs(runs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The latest run of each intent: the first in the listing, which is newest registration first."""
    latest: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        intent = run.get("intent_id")
        if isinstance(intent, str) and intent not in latest:
            latest[intent] = run
    return [latest[intent] for intent in sorted(latest)]


def full_run(client: RepairServerClient, run_id: object) -> dict[str, Any]:
    if not valid_run_id(run_id):
        raise LiveReadError("a listed run has no valid run_id")
    assert isinstance(run_id, str)
    answer = client.get_run(run_id)
    if answer.status != 200 or not isinstance(answer.body, dict):
        raise LiveReadError(f"GET /repair/runs/<run> answered {answer.status}")
    return answer.body


def window_one(run: Mapping[str, Any]) -> dict[str, Any] | None:
    reviews = run.get("reviews")
    if not isinstance(reviews, list):
        return None
    return next((review for review in reviews if isinstance(review, dict) and review.get("window") == 1), None)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def live_record(run: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any] | None:
    """A window-1 review as a decision record of the set `live`, marked `"live": true` (the gate scores it by that mark,
    never by the name): a server `not_reviewed` is a `continue` with the fallback `not_reviewed`. None for a review
    that holds no decision of §6.5."""
    decision = review.get("decision")
    raw_evidence = review.get("evidence")
    evidence = [item for item in raw_evidence if isinstance(item, str)] if isinstance(raw_evidence, list) else []
    if decision == NOT_REVIEWED:
        why = review.get("why")
        values: dict[str, Any] = {
            "decision": "continue",
            "fallback": NOT_REVIEWED,
            "reason": f"not reviewed: {why if why in WHY_VALUES else 'other'}",
            "evidence": [],
            "next_step": "",
        }
    elif decision in ("continue", "extend", "detach"):
        fallback = review.get("fallback")
        values = {
            "decision": decision,
            "fallback": fallback if isinstance(fallback, str) else None,
            "reason": _text(review.get("reason")),
            "evidence": evidence,
            "next_step": _text(review.get("next_step")),
        }
    else:
        return None
    cited = [match.group(1) for item in values["evidence"] if (match := CITATION.match(item))]
    return {
        "reviewer": LIVE,
        "live": True,
        "intent_id": run.get("intent_id"),
        "repetition": 1,
        "run_id": run.get("run_id"),
        "cli": run.get("cli_type"),
        "execution": run.get("execution"),
        "budget_seconds": run.get("budget_seconds"),
        **values,
        "cited": cited,
        "model": shown_model(review.get("model")),
        "source": review.get("source"),
        "digest_sha256": review.get("digest_sha256"),
        "transcript_cut_seconds": review.get("transcript_cut_seconds"),
        "created_at": review.get("created_at"),
    }


def session_after_s(logs: Sequence[str], project_id: str) -> dict[str, tuple[str, float | None]]:
    """From dispatcher logs: run id -> (source, after_s) of its `repair shadow session registered` line (§8.11)."""
    found: dict[str, tuple[str, float | None]] = {}
    for log in logs:
        try:
            handle = open(log, encoding="utf-8", errors="replace")  # noqa: SIM115 - closed below, line by line
        except OSError as exc:
            raise LiveReadError(f"dispatcher log cannot be read: {log}: {type(exc).__name__}") from None
        with handle:
            for line in handle:
                match = _SESSION_LINE.search(line)
                if match is None or match.group("project") != project_id:
                    continue
                try:
                    after: float | None = float(match.group("after"))
                except ValueError:
                    after = None
                if after is not None and not math.isfinite(after):
                    after = None
                found.setdefault(match.group("run"), (match.group("source"), after))
    return found


def _mode_key(run: Mapping[str, Any]) -> str:
    return f"{_text(run.get('cli_type')) or '-'}/{_text(run.get('execution')) or '-'}"


def read_live_decisions(
    *,
    server: str,
    project_id: str,
    token_file: str,
    api_key_file: str | None,
    ca_bundle: str | None,
    logs: Sequence[str] = (),
    page_size: int | None = None,
) -> tuple[list[Decision], dict[str, Any]]:
    """The live set's decisions (the latest run of each intent, its window-1 review), and the report's extra lines:
    per CLI and execution mode, the share of `transcript_unreadable` reviews, and the `after_s` distribution of late
    session ids from the dispatcher logs (§15.6, §8.7)."""
    client = server_client(server, token_file, api_key_file, ca_bundle)
    try:
        runs = live_runs(client, project_id, PAGE_SIZE if page_size is None else page_size)
        decisions: list[Decision] = []
        shares: dict[str, Counter[str]] = {}
        latest_ids = {id(run) for run in latest_runs(runs)}
        skipped = 0
        for run in runs:
            summaries = run.get("reviews")
            if not isinstance(summaries, list) or not summaries:
                continue
            full = full_run(client, run.get("run_id"))
            review = window_one(full)
            if review is not None:
                counts = shares.setdefault(_mode_key(full), Counter())
                if review.get("source") == "agent":
                    counts["agent_reviews"] += 1
                    counts[TRANSCRIPT_UNREADABLE] += review.get("fallback") == TRANSCRIPT_UNREADABLE
                elif review.get("decision") == NOT_REVIEWED:
                    counts[NOT_REVIEWED] += 1
            if id(run) not in latest_ids or review is None:
                continue
            record = live_record(full, review)
            if record is None:
                skipped += 1
                continue
            decisions.append(decision_from(record, len(decisions) + 1, "live reviews"))
    finally:
        client.close()
    lines: list[str] = []
    share_json: dict[str, Any] = {}
    for mode, counts in sorted(shares.items()):
        agent = counts["agent_reviews"]
        unreadable = counts[TRANSCRIPT_UNREADABLE]
        share = f"{unreadable / agent:.0%}" if agent else "-"
        lines.append(
            f"transcript_unreadable cli/execution={mode} share={share} ({unreadable} of {agent} agent reviews; "
            f"{counts[NOT_REVIEWED]} not reviewed)"
        )
        share_json[mode] = {
            "agent_reviews": agent,
            TRANSCRIPT_UNREADABLE: unreadable,
            NOT_REVIEWED: counts[NOT_REVIEWED],
        }
    after_json: dict[str, Any] = {}
    if logs:
        sessions = session_after_s(logs, project_id)
        by_mode: dict[str, list[float]] = {}
        early: Counter[str] = Counter()
        for run in runs:
            seen = sessions.get(_text(run.get("run_id")))
            if seen is None:
                continue
            source, after = seen
            mode = _mode_key(run)
            if source in LATE_SOURCES:
                by_mode.setdefault(mode, [])
                if after is not None:
                    by_mode[mode].append(after)
            else:
                early[mode] += 1
        for mode in sorted(set(by_mode) | set(early)):
            values = by_mode.get(mode, [])
            p50, p95 = percentile(values, 0.5), percentile(values, 0.95)
            lines.append(
                f"late session ids cli/execution={mode} count={len(values)} after_s_p50={'-' if p50 is None else p50} "
                f"after_s_p95={'-' if p95 is None else p95} after_s_max={max(values) if values else '-'} "
                f"from_argv={early[mode]}"
            )
            after_json[mode] = {"late": len(values), "after_s": sorted(values), "from_argv": early[mode]}
    else:
        lines.append("late session ids: no dispatcher log given (--log)")
    if skipped:
        lines.append(f"window-1 reviews without a decision of spec 6.5, not scored: {skipped}")
    extra = {"lines": lines, "json": {"transcript_unreadable": share_json, "late_session_ids": after_json}}
    return sorted(decisions, key=lambda decision: decision.intent_id), extra


# --- the export -------------------------------------------------------------------------------------------------------


def _write_new(path: Path, data: bytes, mode: int = 0o640) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _json_bytes(content: object) -> bytes:
    return (json.dumps(content, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def intent_document(project_id: str, run: Mapping[str, Any]) -> dict[str, Any]:
    """intent.json in Phase 0's shape (`tools/repair-phase0/export_eval_set.py`), with the run under
    `timed_out_attempts` (its budget and review window), so that the replay reads its budget as from a Phase 0 set.
    No review and no decision is in it."""
    intent = run.get("intent") if isinstance(run.get("intent"), Mapping) else {}
    window = run.get("window") if isinstance(run.get("window"), Mapping) else {}
    assert isinstance(intent, Mapping) and isinstance(window, Mapping)
    seconds = window.get("seconds")
    review_window: dict[str, Any] | None = None
    if type(seconds) is int and seconds > 0:
        review_window = {
            "from_s": math.floor(seconds * REVIEW_OPEN_FRACTION),
            "to_s": math.floor(seconds * REVIEW_CLOSE_FRACTION),
            "server_clock": [window.get("review_opens_at"), window.get("review_closes_at")],
        }
    return {
        "project": project_id,
        "intent_id": run.get("intent_id"),
        "found_on_server": True,
        "description": intent.get("description"),
        "from": intent.get("from"),
        "source": "live shadow run (export_shadow_runs.py)",
        "timed_out_attempts": [
            {
                "attempt": 1,
                "run_id": run.get("run_id"),
                "worker": run.get("worker"),
                "worker_type": run.get("cli_type"),
                "execution": run.get("execution"),
                "execute_started_at": run.get("started_at"),
                "execute_timeout_s": run.get("budget_seconds"),
                "review_window": review_window,
                "session_id": run.get("session_id"),
                "ended_at": run.get("ended_at"),
                "end_reason": run.get("end_reason"),
                "outcome": run.get("outcome"),
                "fact_id": run.get("fact_id"),
            }
        ],
    }


def export(
    client: RepairServerClient, project_id: str, out: Path, copy_root: str | None, page_size: int = PAGE_SIZE
) -> dict[str, int]:
    """Write the folders, the labels template and decisions.jsonl; returns the counts printed at the end."""
    from .mirror import Unavailable, read_copy

    runs = live_runs(client, project_id, page_size)
    exported: list[tuple[str, dict[str, Any]]] = []
    counts: Counter[str] = Counter()
    for run in latest_runs(runs):
        intent_id = run.get("intent_id")
        if not isinstance(intent_id, str) or not valid_project_id(intent_id):
            counts["skipped_bad_intent_id"] += 1
            continue
        summaries = run.get("reviews")
        if not isinstance(summaries, list) or not any(
            isinstance(item, dict) and item.get("window") == 1 for item in summaries
        ):
            counts["without_review"] += 1
            continue
        full = full_run(client, run.get("run_id"))
        review = window_one(full)
        record = live_record(full, review) if review is not None else None
        if review is None or record is None:
            counts["without_review"] += 1
            continue
        folder = out / intent_id
        folder.mkdir(mode=0o750)
        _write_new(folder / "intent.json", _json_bytes(intent_document(project_id, full)))
        digest = review.get("digest")
        if isinstance(digest, str):
            _write_new(folder / "digest.txt", digest.encode("utf-8", "replace"))
            counts["digests"] += 1
        run_id = full.get("run_id")
        if copy_root is not None and isinstance(run_id, str):
            copy = read_copy(copy_root, project_id, run_id, COPY_MAX_BYTES)
            if isinstance(copy, Unavailable):
                counts["copies_unavailable"] += 1
            else:
                _write_new(folder / REDACTED_NAME, copy.data)
                report = {
                    "version": 1,
                    "written_by": WRITTEN_BY,
                    "files": [
                        {
                            "source": f"dispatcher copy {project_id}/{run_id}/transcript.jsonl",
                            "output": REDACTED_NAME,
                            "redacted_by": "dispatcher (spec 8.8); counts in that copy's meta.json",
                            "output_sha256": copy.sha256,
                        }
                    ],
                }
                _write_new(folder / REDACTION_REPORT, _json_bytes(report))
                counts["copies"] += 1
        exported.append((intent_id, record))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["intent_id", "label", "evidence", "notes"], lineterminator="\n")
    writer.writeheader()
    for intent_id, _ in exported:
        writer.writerow({"intent_id": intent_id, "label": "", "evidence": "", "notes": ""})
    _write_new(out / "labels.csv", buffer.getvalue().encode("utf-8"))
    lines = b"".join(json.dumps(record, ensure_ascii=True).encode("ascii") + b"\n" for _, record in exported)
    _write_new(out / "decisions.jsonl", lines)
    counts["intents"] = len(exported)
    return dict(counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_shadow_runs.py",
        description=(
            "Write the live shadow runs of one project as Phase 0-style folders for blind labelling (spec 15.6). "
            "Decisions go only to <out>/decisions.jsonl, outside the intent folders."
        ),
        epilog="Exit codes: 0 written; 2 a token or key file, or --config's repair.yaml, cannot be used; 3 --out is not "
        "new, or the server's answers cannot be read.",
    )
    parser.add_argument("--server", required=True, metavar="URL", help="the Cairn server, as the agent reaches it")
    parser.add_argument("--project", required=True, metavar="P", help="the project to export")
    parser.add_argument("--agent-token-file", required=True, metavar="F", help="the agent role's token file")
    parser.add_argument("--out", required=True, metavar="DIR", help="a new or empty folder, such as shadow-P")
    copies = parser.add_mutually_exclusive_group()
    copies.add_argument(
        "--config",
        metavar="PATH",
        help="the agent's repair.yaml: its transcripts.root is the copy root, to export each run's copy while it is kept",
    )
    copies.add_argument("--copy-root", metavar="DIR", help="the copy root itself, in place of --config")
    parser.add_argument("--server-api-key-file", metavar="F", help="dev-0.3.0 port only: the X-API-Key file")
    parser.add_argument("--server-ca-bundle", metavar="F", help="a CA file for an https server")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out)
    if not valid_project_id(args.project):
        sys.stderr.write("export_shadow_runs: --project is not a project id\n")
        return EXIT_INPUT
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        sys.stderr.write(f"export_shadow_runs: --out exists and is not an empty folder: {out}\n")
        return EXIT_INPUT
    copy_root: str | None = args.copy_root
    if args.config is not None:
        try:
            copy_root = load_config(args.config).transcripts.root
        except StartupError as exc:
            sys.stderr.write(f"export_shadow_runs: {exc}\n")
            return EXIT_CONFIG
    try:
        client = server_client(args.server, args.agent_token_file, args.server_api_key_file, args.server_ca_bundle)
    except LiveReadError as exc:
        sys.stderr.write(f"export_shadow_runs: {exc}\n")
        return EXIT_CONFIG
    try:
        out.mkdir(mode=0o750, parents=True, exist_ok=True)
        counts = export(client, args.project, out, copy_root)
    except LiveReadError as exc:
        sys.stderr.write(f"export_shadow_runs: {exc}\n")
        return EXIT_INPUT
    finally:
        client.close()
    if copy_root is None:
        sys.stdout.write(
            "no copy root given (--config repair.yaml, or --copy-root): no transcript copy was exported, only the "
            "stored digests; the folders replay only with a copy\n"
        )
    sys.stdout.write(
        f"exported project={args.project} intents={counts.get('intents', 0)} digests={counts.get('digests', 0)} "
        f"copies={counts.get('copies', 0)} copies_unavailable={counts.get('copies_unavailable', 0)} "
        f"without_review={counts.get('without_review', 0)}\n"
        f"label {out / 'labels.csv'} from the intent folders before you open {out / 'decisions.jsonl'} or the "
        "project's graph\n"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
