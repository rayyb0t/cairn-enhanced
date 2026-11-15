from __future__ import annotations

import ipaddress
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from cairn.server.db import get_conn
from cairn.server.models import Intent, IntentIssue, ProjectMeta, ProjectReason

LOG = logging.getLogger(__name__)
# The repair layer's server lines (the switch line included) all go through this logger.
REPAIR_LOG = logging.getLogger("cairn.server.repair")
_SWITCH_CLIENT_RE = re.compile(r"[0-9A-Fa-f:.]{2,45}")

JSON_REPAIRED = "json_repaired"
# The strategies of the dispatcher's JSON rescue (cairn.dispatcher.output_parser.rescue_text_envelope).
JSON_REPAIR_STRATEGIES = frozenset({"lenient_decode", "envelope_repair"})
_ISSUE_ERROR_MAX_CHARS = 300
# Recording an issue waits at most this long for a busy database (the request's own work is already done).
_ISSUE_WRITE_BUSY_TIMEOUT_S = 1.0
# A stored detail nested deeper than this is shown as {}; very deep values cannot be serialized.
_ISSUE_DETAIL_MAX_DEPTH = 8


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    project_id: str,
    issues: list[IntentIssue] | None = None,
) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    if issues is None:
        issues = load_intent_issues(conn, project_id, row["id"]).get(row["id"], [])
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        issues=issues,
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    issues = load_intent_issues(conn, project_id)
    return [intent_to_model(conn, r, project_id, issues.get(r["id"], [])) for r in rows]


def json_repair_detail(value: Any) -> dict[str, str] | None:
    """The detail of a json_repaired issue for a conclude's parse_repair value, or None to record none.

    The dispatcher sends {"strategy": ..., "error": ...}. Anything this server does not understand is
    ignored rather than refused, so the optional field can never make a conclude fail.
    """
    if not isinstance(value, dict):
        return None
    strategy = value.get("strategy")
    if not isinstance(strategy, str) or strategy not in JSON_REPAIR_STRATEGIES:
        return None
    detail = {"strategy": strategy}
    error = value.get("error")
    if isinstance(error, str) and error.strip():
        detail["error"] = error.strip()[:_ISSUE_ERROR_MAX_CHARS]
    return detail


def record_intent_issue(
    project_id: str,
    intent_id: str,
    kind: str,
    detail: dict[str, Any],
    now: str,
    run_id: str | None = None,
) -> IntentIssue | None:
    """Record an issue in its own short transaction, after the reporting request's work has committed.

    Best effort: any failure (a busy or full database, a missing table, anything else) is logged and
    None is returned, so recording an issue can never change the outcome of the request that reports it.
    """
    try:
        with get_conn(timeout=_ISSUE_WRITE_BUSY_TIMEOUT_S) as conn:
            cursor = conn.execute(
                "INSERT INTO intent_issues (project_id, intent_id, run_id, kind, detail_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, intent_id, run_id, kind, json.dumps(detail, ensure_ascii=False), now, now),
            )
            issue_id = cursor.lastrowid
        if issue_id is None:  # stored, but the response cannot name it
            return None
        return IntentIssue(id=issue_id, kind=kind, run_id=run_id, detail=detail, created_at=now, updated_at=now)
    except Exception:
        LOG.warning(
            "intent issue not recorded project=%s intent=%s kind=%s", project_id, intent_id, kind, exc_info=True
        )
        return None


def load_intent_issues(
    conn: sqlite3.Connection, project_id: str, intent_id: str | None = None
) -> dict[str, list[IntentIssue]]:
    """Issues by intent id, newest first, for one intent or the whole project.

    Reading them never fails the request. On a database error, including text that cannot be decoded, the
    issues are left out and the error is logged; a row whose fields this server cannot read is skipped,
    and a detail that is not a shallow JSON object is shown as {}.
    """
    query = "SELECT * FROM intent_issues WHERE project_id = ?"
    params: tuple = (project_id,)
    if intent_id is not None:
        query += " AND intent_id = ?"
        params = (project_id, intent_id)
    in_transaction = conn.in_transaction
    try:
        rows = conn.execute(query + " ORDER BY id DESC", params).fetchall()
    except sqlite3.Error:
        if in_transaction and not conn.in_transaction:
            raise  # the error ended the request's transaction, so the request cannot succeed either
        LOG.warning("intent issues not loaded project=%s", project_id, exc_info=True)
        return {}
    issues: dict[str, list[IntentIssue]] = {}
    for row in rows:
        issue = _issue_from_row(row)
        if issue is not None:
            issues.setdefault(row["intent_id"], []).append(issue)
    return issues


def _issue_from_row(row: sqlite3.Row) -> IntentIssue | None:
    try:
        detail = json.loads(row["detail_json"])
    except (TypeError, ValueError, RecursionError):
        detail = None
    try:
        return IntentIssue(
            id=row["id"],
            kind=row["kind"],
            run_id=row["run_id"],
            detail=detail if isinstance(detail, dict) and _within_depth(detail) else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    except ValidationError:
        LOG.warning("intent issue skipped id=%s", row["id"])
        return None


def _within_depth(value: Any, depth: int = 0) -> bool:
    if depth > _ISSUE_DETAIL_MAX_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_within_depth(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return all(_within_depth(item, depth + 1) for item in value)
    return True


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
        repair_enabled=project_repair_enabled(row),
    )


def project_repair_enabled(row: sqlite3.Row) -> bool:
    """The project's Repair switch. A row without the column reads off, so it never fails a project read."""
    if "repair_enabled" not in row.keys():  # noqa: SIM118 (on a sqlite3.Row, `in row` tests the values)
        return False
    return bool(row["repair_enabled"])


def repair_switch_client(request: Request) -> str:
    """The caller's address for the switch log line, or "-" when it is not a plain IP address.

    Both checks are needed: ip_address() also accepts an IPv6 scope id (the text after "%"), which can hold any
    printable text. The address only says that a change happened, not who made it: a caller on 127.0.0.1 can
    set it with X-Forwarded-For.
    """
    host = request.client.host if request.client is not None else None
    if not isinstance(host, str) or _SWITCH_CLIENT_RE.fullmatch(host) is None:
        return "-"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "-"
    return host


def log_repair_switch(project_id: str, enabled: bool, request: Request) -> None:
    """The one log line of a switch change, at WARNING so that it shows with no logging configuration."""
    REPAIR_LOG.warning(
        "repair switch project=%s enabled=%s client=%s", project_id, enabled, repair_switch_client(request)
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
