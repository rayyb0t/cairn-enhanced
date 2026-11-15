"""Repair layer (shadow mode): the server tests of spec section 16 (the SV ids).

This file holds the tests of the switch column, the project objects and the run lifecycle: SV1-SV5, SV7, SV9,
SV23, SV24 and SV37-SV42. The tests of the repair endpoints, which drive them over HTTP, are in four files beside
it: test_repair_server_auth.py (SV6, SV10-SV15, SV47), test_repair_server_runs.py (SV16-SV22, SV25, SV28-SV36),
test_repair_server_existing.py (SV8, SV26, SV27, SV43, SV44, SV46) and test_repair_server_logs.py (SV45). They share
test_repair_server_support.py, and SV43, SV44 and SV46 replay the existing-endpoint script of
test_repair_server_script.py. Every test keeps its section 16 name.

The switch column and the repair tables (SV1, SV2). Every existing database gets `projects.repair_enabled` only
through the ALTER in `db._ensure_project_columns`, so these tests start from databases built without the column: a
literal copy of the schema at ee0f227, the last server schema before the repair layer, and the two legacy
`projects` tables of test_db_migrations.py. A database built from `db.SCHEMA` or by `db.configure` already has the
column, so its ALTER would never run and a wrong default (DEFAULT 1, which would switch on every existing project at
the upgrade) would go unnoticed.

The switch on project objects and in the create path (SV3, SV4, SV5, SV7, SV9). With the switch off, which is every
project's default, the only change an existing endpoint shows is one additive key, "repair_enabled": false, on every
project object. Creating a project runs exactly today's SQL and logs nothing unless the body asks for the switch.
The dispatcher parses the same models, so only a JSON true reads as on, and no value of the field can fail a project
read.

The run lifecycle (SV23, SV24, SV37-SV42; spec sections 5.1-5.6): how a run ends without the dispatcher's /end, why
a short heartbeat gap does not end it, when a run that reached its limit unreviewed gets the server's "not reviewed"
record, and how an ended run is stamped on its issues. Supporting tests below them check an agent review's stored
copy and display copy, the window maths and the run view, the liveness rules one by one, the text cleaning, the
request models, the sweep's reads (no orphan pass), the not-reviewed rule applied once more after a late /end, the
edges of /end, and that repair lines are logged only once their transaction commits. These tests drive the run's
side through cairn.server.repair itself (insert_run, sweep, end_run, record_agent_review, run_view), each call in its
own BEGIN IMMEDIATE transaction as an endpoint makes it, so the rules are tested apart from the endpoints. The intent's
side is driven through today's endpoints (heartbeat, release, conclude, status, complete, the project read), which
set the claims and statuses the sweep reads; the many keep-alive heartbeats call the heartbeat endpoint's own
function, the other calls go over HTTP.

The server fixture is test_server_api.py's, with the server's clock frozen: utcnow is patched in services,
routers.intents, routers.projects, routers.hints and repair, and moves only when a test moves it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter, ValidationError

from cairn.server import db, repair, services
from cairn.server.app import app
from cairn.server.models import (
    HeartbeatRequest,
    IntentIssue,
    ProjectDetail,
    ProjectMeta,
    ProjectReason,
    ProjectSummary,
)
from cairn.server.repair_models import (
    ReviewRequest,
    ReviewResponse,
    RunDetailView,
    RunEndRequest,
    RunRegisterRequest,
    RunView,
    valid_run_id,
)
from cairn.server.routers import hints as hints_router
from cairn.server.routers import intents as intents_router
from cairn.server.routers import projects as projects_router
from cairn.server.services import project_repair_enabled, repair_switch_client

# --------------------------------------------------------------------------- the switch column and the repair tables

# The SCHEMA text of cairn/src/cairn/server/db.py at ee0f227, copied literally. Never derive it from db.SCHEMA.
EE0F227_SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS intent_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS intent_issues_by_intent ON intent_issues (project_id, intent_id);
"""

# Rows in every table of that schema: two projects, one of them with intents, sources, a hint and an issue.
EE0F227_ROWS = """\
UPDATE settings SET intent_timeout = 30, reason_timeout = 45 WHERE rowid = 1;
UPDATE counters SET value = 2 WHERE name = 'project';
INSERT INTO projects (id, title, status, bootstrap_enabled, created_at, reason_worker, reason_trigger,
                      reason_started_at, reason_last_heartbeat_at)
    VALUES ('proj_001', 'old project', 'active', 0, '2026-01-01T00:00:00Z', 'reasoner', 'facts:2->3',
            '2026-01-01T00:00:05Z', '2026-01-01T00:00:06Z');
INSERT INTO projects (id, title, status, created_at) VALUES ('proj_002', 'stopped project', 'stopped', '2026-01-02T00:00:00Z');
INSERT INTO facts (id, project_id, description) VALUES
    ('origin', 'proj_001', 'start'), ('goal', 'proj_001', 'finish'), ('f001', 'proj_001', 'a fact'),
    ('origin', 'proj_002', 'start'), ('goal', 'proj_002', 'finish');
INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at)
    VALUES ('i001', 'proj_001', 'f001', 'investigate', 'reasoner', 'explorer', '2026-01-01T00:00:03Z',
            '2026-01-01T00:00:01Z', '2026-01-01T00:00:03Z'),
           ('i002', 'proj_001', NULL, 'dig further', 'reasoner', 'explorer', '2026-01-01T00:00:04Z',
            '2026-01-01T00:00:02Z', NULL);
INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'origin'), ('i002', 'proj_001', 'f001');
INSERT INTO hints (id, project_id, content, creator, created_at) VALUES ('h001', 'proj_001', 'a clue', 'human', '2026-01-01T00:00:00Z');
INSERT INTO scoped_counters (project_id, kind, value) VALUES ('proj_001', 'fact', 1), ('proj_001', 'intent', 2), ('proj_001', 'hint', 1);
INSERT INTO intent_issues (project_id, intent_id, run_id, kind, detail_json, created_at, updated_at)
    VALUES ('proj_001', 'i001', NULL, 'json_repaired', '{"strategy": "lenient_decode"}', '2026-01-01T00:00:03Z',
            '2026-01-01T00:00:03Z');
"""

EE0F227_TABLES = (
    "settings",
    "projects",
    "facts",
    "intents",
    "intent_sources",
    "hints",
    "counters",
    "scoped_counters",
    "intent_issues",
    "sqlite_sequence",
)
EE0F227_PROJECT_COLUMNS = (
    "id",
    "title",
    "status",
    "bootstrap_enabled",
    "created_at",
    "reason_worker",
    "reason_trigger",
    "reason_started_at",
    "reason_last_heartbeat_at",
)

# The two legacy projects tables of test_db_migrations.py, with their rows, copied literally.
LEGACY_PROJECTS_WITHOUT_BOOTSTRAP = """\
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);
INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-01-01T00:00:00Z');
"""
LEGACY_PROJECTS_WITH_BOOTSTRAP_MODE = """\
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_mode TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);
INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_001', 'disabled', 'disabled', '2026-01-01T00:00:00Z');
INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_002', 'enabled', 'enabled', '2026-01-01T00:00:00Z');
"""

# (type, notnull, dflt_value, pk) of projects.repair_enabled, on every path to it.
SWITCH_COLUMN = ("INTEGER", 1, "0", 0)
# PRAGMA foreign_key_list rows: (id, seq, table, from, to, on_update, on_delete, match).
RUNS_FOREIGN_KEYS = [
    (0, 0, "intents", "intent_id", "id", "NO ACTION", "CASCADE", "NONE"),
    (0, 1, "intents", "project_id", "project_id", "NO ACTION", "CASCADE", "NONE"),
]
REVIEWS_FOREIGN_KEYS = [(0, 0, "repair_runs", "run_id", "run_id", "NO ACTION", "CASCADE", "NONE")]
# PRAGMA table_info rows without the cid: (name, type, notnull, dflt_value, pk).
RUNS_COLUMNS = [
    ("run_id", "TEXT", 0, None, 1),
    ("project_id", "TEXT", 1, None, 0),
    ("intent_id", "TEXT", 1, None, 0),
    ("worker", "TEXT", 1, None, 0),
    ("cli_type", "TEXT", 1, None, 0),
    ("execution", "TEXT", 1, None, 0),
    ("session_id", "TEXT", 0, None, 0),
    ("locator_json", "TEXT", 1, "'{}'", 0),
    ("budget_seconds", "INTEGER", 1, None, 0),
    ("started_at", "TEXT", 1, None, 0),
    ("deadline_at", "TEXT", 1, None, 0),
    ("version", "INTEGER", 1, "1", 0),
    ("slices_used", "INTEGER", 1, "0", 0),
    ("state", "TEXT", 1, "'running'", 0),
    ("reviewable", "INTEGER", 1, "1", 0),
    ("settled_window", "INTEGER", 1, "0", 0),
    ("registered_at", "TEXT", 1, None, 0),
    ("updated_at", "TEXT", 1, None, 0),
    ("ended_at", "TEXT", 0, None, 0),
    ("end_reason", "TEXT", 0, None, 0),
    ("last_alive_at", "TEXT", 0, None, 0),
    ("outcome", "TEXT", 0, None, 0),
    ("outcome_json", "TEXT", 0, None, 0),
    ("fact_id", "TEXT", 0, None, 0),
]
REVIEWS_COLUMNS = [
    ("run_id", "TEXT", 1, None, 1),
    ("window_no", "INTEGER", 1, None, 2),
    ("review_id", "TEXT", 1, None, 0),
    ("source", "TEXT", 1, None, 0),
    ("decision", "TEXT", 1, None, 0),
    ("issue_id", "INTEGER", 0, None, 0),
    ("detail_json", "TEXT", 1, None, 0),
    ("digest", "TEXT", 0, None, 0),
    ("digest_sha256", "TEXT", 0, None, 0),
    ("raw_output", "TEXT", 0, None, 0),
    ("created_at", "TEXT", 1, None, 0),
]
# The three indexes of repair_runs as SQLite stores them (it drops IF NOT EXISTS), by name.
RUNS_INDEXES = {
    "repair_runs_open_key": "CREATE UNIQUE INDEX repair_runs_open_key ON repair_runs (project_id, intent_id, worker) "
    "WHERE ended_at IS NULL",
    "repair_runs_by_intent": "CREATE INDEX repair_runs_by_intent ON repair_runs (project_id, intent_id)",
    "repair_runs_attention": "CREATE INDEX repair_runs_attention ON repair_runs (project_id) "
    "WHERE ended_at IS NULL OR settled_window = 0",
}
# PRAGMA index_list rows by name: (unique, origin, partial), and the indexed columns.
RUNS_INDEX_LIST = {
    "repair_runs_open_key": ((1, "c", 1), ["project_id", "intent_id", "worker"]),
    "repair_runs_by_intent": ((0, "c", 0), ["project_id", "intent_id"]),
    "repair_runs_attention": ((0, "c", 1), ["project_id"]),
    "sqlite_autoindex_repair_runs_1": ((1, "pk", 0), ["run_id"]),
}
REVIEWS_INDEX_LIST = {"sqlite_autoindex_repair_reviews_1": ((1, "pk", 0), ["run_id", "window_no"])}


def _build(path: Path, script: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(script)
        conn.commit()


def _configure(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)


def _connect(path: Path) -> sqlite3.Connection:
    # As db.get_conn does, and as ee0f227's get_conn did.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...] | None = None) -> list[tuple]:
    selected = ", ".join(columns) if columns else "*"
    # Table and column names come from the constants of this file.
    return [tuple(row) for row in conn.execute(f"SELECT {selected} FROM {table} ORDER BY rowid")]  # noqa: S608


def _ee0f227_rows(path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            table: _rows(conn, table, EE0F227_PROJECT_COLUMNS if table == "projects" else None)
            for table in EE0F227_TABLES
        }


def _snapshot(path: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """The whole database: every schema object and every row of every table."""
    with closing(sqlite3.connect(path)) as conn:
        schema = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables = [row[1] for row in schema if row[0] == "table"]
        return schema, {table: _rows(conn, table) for table in tables}


def _column(conn: sqlite3.Connection, table: str, name: str) -> tuple:
    rows = [row for row in conn.execute(f"PRAGMA table_info({table})") if row[1] == name]
    assert len(rows) == 1, rows
    _cid, _name, column_type, notnull, default, pk = rows[0]
    return (column_type, notnull, default, pk)


def _index_list(conn: sqlite3.Connection, table: str) -> dict[str, tuple[tuple, list[str]]]:
    indexes = {}
    for _seq, name, unique, origin, partial in conn.execute(f"PRAGMA index_list({table})"):
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
        indexes[name] = ((unique, origin, partial), columns)
    return indexes


def _check_repair_tables(conn: sqlite3.Connection) -> None:
    """The two tables, their keys and the three indexes, exactly as spec section 4.2 and 4.3 give them."""
    assert [row[1:] for row in conn.execute("PRAGMA table_info(repair_runs)")] == RUNS_COLUMNS
    assert [row[1:] for row in conn.execute("PRAGMA table_info(repair_reviews)")] == REVIEWS_COLUMNS
    assert conn.execute("PRAGMA foreign_key_list(repair_runs)").fetchall() == RUNS_FOREIGN_KEYS
    assert conn.execute("PRAGMA foreign_key_list(repair_reviews)").fetchall() == REVIEWS_FOREIGN_KEYS
    assert _index_list(conn, "repair_runs") == RUNS_INDEX_LIST
    assert _index_list(conn, "repair_reviews") == REVIEWS_INDEX_LIST
    stored = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'repair_runs'"))
    assert {name: stored.get(name) for name in RUNS_INDEXES} == RUNS_INDEXES
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _migrate_ee0f227_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple]]:
    """Build the pre-Phase-2 database from the literal schema, configure it, and return its rows from before."""
    _build(path, EE0F227_SCHEMA + EE0F227_ROWS)
    with closing(sqlite3.connect(path)) as conn:
        assert "repair_enabled" not in {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    before = _ee0f227_rows(path)
    _configure(path, monkeypatch)
    return before


# SV1 (spec 4.1-4.3, 4.6)
def test_configure_adds_repair_tables_and_switch_column_to_an_existing_database(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ee0f227.db"
    before = _migrate_ee0f227_database(path, monkeypatch)

    with closing(_connect(path)) as conn:
        _check_repair_tables(conn)
        assert _column(conn, "projects", "repair_enabled") == SWITCH_COLUMN
        assert conn.execute("SELECT id, repair_enabled FROM projects ORDER BY rowid").fetchall() == [
            ("proj_001", 0),
            ("proj_002", 0),
        ]
        assert conn.execute("SELECT COUNT(*) FROM repair_runs").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM repair_reviews").fetchone() == (0,)
    # Every row is intact, the projects rows on their old columns.
    assert _ee0f227_rows(path) == before

    # A second configure, as at the next server start, changes nothing, schema or rows, and a switched-on project
    # stays on. Every switch is 0 until here, so without this project a reset at every start would go unnoticed.
    with closing(_connect(path)) as conn, conn:
        conn.execute("UPDATE projects SET repair_enabled = 1 WHERE id = 'proj_001'")
    snapshot = _snapshot(path)
    _configure(path, monkeypatch)
    assert _snapshot(path) == snapshot
    with closing(_connect(path)) as conn:
        assert conn.execute("SELECT id, repair_enabled FROM projects ORDER BY rowid").fetchall() == [
            ("proj_001", 1),
            ("proj_002", 0),
        ]

    # Today's project delete, on a connection like get_conn's, deletes the project with its intents, and the
    # intents' runs and the runs' reviews with them. A key of the wrong form would refuse this delete with
    # "foreign key mismatch", one without ON DELETE CASCADE with "FOREIGN KEY constraint failed", and with no
    # key the run would stay.
    with closing(_connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO repair_runs (run_id, project_id, intent_id, worker, cli_type, execution, budget_seconds,"
            " started_at, deadline_at, registered_at, updated_at)"
            " VALUES ('run-0001', 'proj_001', 'i002', 'explorer', 'codex', 'container', 1800, '2026-01-01T00:00:02Z',"
            " '2026-01-01T00:30:02Z', '2026-01-01T00:00:02Z', '2026-01-01T00:00:02Z')"
        )
        conn.execute(
            "INSERT INTO repair_reviews (run_id, window_no, review_id, source, decision, detail_json, created_at)"
            " VALUES ('run-0001', 1, 'rv-0001', 'agent', 'continue', '{}', '2026-01-01T00:22:32Z')"
        )
    with closing(_connect(path)) as conn:
        with conn:
            conn.execute("DELETE FROM projects WHERE id = ?", ("proj_001",))
        for table in ("intents", "facts", "hints", "intent_sources", "intent_issues", "scoped_counters", "repair_runs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = 'proj_001'").fetchone() == (0,), table  # noqa: S608
        assert conn.execute("SELECT COUNT(*) FROM repair_reviews").fetchone() == (0,)
        assert conn.execute("SELECT id FROM projects").fetchall() == [("proj_002",)]
        assert conn.execute("SELECT COUNT(*) FROM facts WHERE project_id = 'proj_002'").fetchone() == (2,)

    # The two legacy projects tables of test_db_migrations.py: every row is off, with the same column.
    for name, script, rows in (
        ("without_bootstrap", LEGACY_PROJECTS_WITHOUT_BOOTSTRAP, [("proj_001", 0)]),
        ("with_bootstrap_mode", LEGACY_PROJECTS_WITH_BOOTSTRAP_MODE, [("proj_001", 0), ("proj_002", 0)]),
    ):
        legacy = tmp_path / f"legacy_{name}.db"
        _build(legacy, script)
        _configure(legacy, monkeypatch)
        with closing(_connect(legacy)) as conn:
            assert conn.execute("SELECT id, repair_enabled FROM projects ORDER BY rowid").fetchall() == rows, name
            assert _column(conn, "projects", "repair_enabled") == SWITCH_COLUMN, name
            _check_repair_tables(conn)


# SV2 (spec 4.1, 4.6)
def test_fresh_and_migrated_databases_have_the_same_projects_columns(tmp_path, monkeypatch) -> None:
    migrated = tmp_path / "ee0f227.db"
    _migrate_ee0f227_database(migrated, monkeypatch)
    fresh = tmp_path / "fresh.db"
    _configure(fresh, monkeypatch)

    with closing(sqlite3.connect(fresh)) as fresh_conn, closing(sqlite3.connect(migrated)) as migrated_conn:
        # Row for row, as (cid, name, type, notnull, dflt_value, pk): this also proves the column order.
        fresh_columns = fresh_conn.execute("PRAGMA table_info(projects)").fetchall()
        assert migrated_conn.execute("PRAGMA table_info(projects)").fetchall() == fresh_columns
        assert fresh_columns[-1] == (9, "repair_enabled", *SWITCH_COLUMN)
        assert [row[1] for row in fresh_columns[:-1]] == list(EE0F227_PROJECT_COLUMNS)
        # The repair tables come out the same on both paths too.
        for pragma in ("table_info", "foreign_key_list", "index_list"):
            for table in ("repair_runs", "repair_reviews"):
                query = f"PRAGMA {pragma}({table})"
                assert migrated_conn.execute(query).fetchall() == fresh_conn.execute(query).fetchall(), query


# --------------------------------------------------------------------------- the server fixture (spec 16)

START = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    """The server's time, `seconds` after START."""
    return (START + timedelta(seconds=seconds)).strftime(repair.TIME_FORMAT)


class Clock:
    """services.utcnow, frozen: it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> str:
        return self.now.strftime(repair.TIME_FORMAT)

    @property
    def elapsed(self) -> float:
        return (self.now - START).total_seconds()

    def to(self, seconds: float) -> None:
        assert seconds >= self.elapsed, "the clock never goes back"
        self.now = START + timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    frozen = Clock()
    for module in (services, intents_router, projects_router, hints_router, repair):
        monkeypatch.setattr(module, "utcnow", frozen)
    return frozen


@pytest.fixture
def server(tmp_path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    """The app on a fresh database, as in test_server_api.py, with the server's clock frozen at START. The server's
    in-memory state (the agent's last poll, the intent_timeouts the sweeps read) starts empty, as at a start.
    """
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    repair.reset_memory()
    with TestClient(app) as client:
        yield client
    repair.reset_memory()


# --------------------------------------------------------------------------- the switch on project objects

REPAIR_LOGGER = "cairn.server.repair"
PROJECT_KEYS = {"id", "title", "status", "bootstrap_enabled", "created_at", "reason", "repair_enabled"}
SUMMARY_KEYS = PROJECT_KEYS | {
    "fact_count",
    "intent_count",
    "working_intent_count",
    "unclaimed_intent_count",
    "hint_count",
}
BODY = {"title": "t", "origin": "start", "goal": "finish", "hints": [{"content": "a clue", "creator": "human"}]}
SWITCH_UPDATE = "UPDATE projects SET repair_enabled = 1 WHERE id = '<pid>'"
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


@contextmanager
def traced_sql(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every statement run on a connection opened inside the block, as SQLite's trace callback reports it."""
    statements: list[str] = []
    connect = sqlite3.connect

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", traced_connect)
        yield statements


def _repair_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records if record.name == REPAIR_LOGGER]


def _normalized(statements: list[str], project_id: str) -> list[str]:
    return [_TIMESTAMP.sub("<now>", statement.replace(f"'{project_id}'", "'<pid>'")) for statement in statements]


def _create(client: TestClient, **extra: Any) -> dict[str, Any]:
    response = client.post("/projects", json={**BODY, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _stored_switch(project_id: str) -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT repair_enabled FROM projects WHERE id = ?", (project_id,)).fetchone()[0]


def _listed(client: TestClient, project_id: str) -> dict[str, Any]:
    return next(item for item in client.get("/projects").json() if item["id"] == project_id)


# SV3 (spec 6.8, and the switch line of 6.6 and 12)
def test_create_project_defaults_repair_off_and_persists_on(server: TestClient, tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=REPAIR_LOGGER)
    traces = {}
    project_ids = {}
    for name, extra in (("absent", {}), ("false", {"repair_enabled": False}), ("true", {"repair_enabled": True})):
        caplog.clear()
        with traced_sql(monkeypatch) as statements:
            created = _create(server, **extra)
        project_id = project_ids[name] = created["project"]["id"]
        traces[name] = _normalized(statements, project_id)
        if name == "true":
            assert _repair_records(caplog) == [
                (logging.WARNING, f"repair switch project={project_id} enabled=True client=-")
            ]
        else:
            assert [record for record in caplog.records if record.name.startswith("cairn.server")] == [], name

        expected = name == "true"
        assert created["project"]["repair_enabled"] is expected
        assert server.get(f"/projects/{project_id}").json()["project"]["repair_enabled"] is expected
        assert _listed(server, project_id)["repair_enabled"] is expected
        assert _stored_switch(project_id) == int(expected)
        # Everything else about the project is what the same body without the switch gives.
        project = {key: value for key, value in created["project"].items() if key not in ("id", "created_at")}
        assert project == {
            "title": "t",
            "status": "active",
            "bootstrap_enabled": True,
            "reason": None,
            "repair_enabled": expected,
        }
        assert [fact["id"] for fact in created["facts"]] == ["origin", "goal"]
        assert [hint["content"] for hint in created["hints"]] == ["a clue"]

    # Off, with or without the field: exactly the same SQL, and none of it touches the switch.
    assert traces["false"] == traces["absent"]
    assert not any("repair" in statement for statement in traces["absent"])
    # On: the same SQL plus one UPDATE, right after today's INSERT.
    insert = next(i for i, statement in enumerate(traces["absent"]) if statement.startswith("INSERT INTO projects "))
    assert traces["true"] == [*traces["absent"][: insert + 1], SWITCH_UPDATE, *traces["absent"][insert + 1 :]]

    # The switch persists across a restart, which runs configure again on the same file, as `cairn serve` does.
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    for name, project_id in project_ids.items():
        assert server.get(f"/projects/{project_id}").json()["project"]["repair_enabled"] is (name == "true"), name
        assert _stored_switch(project_id) == int(name == "true"), name

    # The switch and the bootstrap flag are stored independently.
    both = _create(server, bootstrap_enabled=False, repair_enabled=True)["project"]
    assert (both["bootstrap_enabled"], both["repair_enabled"]) == (False, True)
    assert server.get(f"/projects/{both['id']}").json()["project"]["bootstrap_enabled"] is False

    # The line names the connection's address only when it is a plain IP address. "deadbeef" and "1.2.3.4.5" hold only
    # the allowed characters, so it is the ip_address() check that keeps them out of the line.
    for host, shown in (
        ("203.0.113.9", "203.0.113.9"),
        ("::1", "::1"),
        ("::1%x enabled=False", "-"),
        ("deadbeef", "-"),
        ("1.2.3.4.5", "-"),
    ):
        caplog.clear()
        with TestClient(app, client=(host, 50000)) as other:
            project_id = _create(other, repair_enabled=True)["project"]["id"]
        assert _repair_records(caplog) == [
            (logging.WARNING, f"repair switch project={project_id} enabled=True client={shown}")
        ]


# The client= rule of spec 6.6 and 12, used by SV3 here and by the switch endpoint (SV6).
def test_switch_log_line_shows_only_plain_ip_addresses() -> None:
    def request(host: Any) -> Any:
        return SimpleNamespace(client=None if host is None else SimpleNamespace(host=host, port=50000))

    for host in ("127.0.0.1", "203.0.113.77", "::1", "2001:db8::1", "::ffff:192.0.2.1"):
        assert repair_switch_client(request(host)) == host
    # Refused by the first check: no client, not a string, or not 2 to 45 hex digits, colons and dots.
    for host in (None, "testclient", "not-an-address", "::1%x enabled=False", "fe80::1%eth0", "127.0.0.1\n", "", 7):
        assert repair_switch_client(request(host)) == "-"
    # Only the allowed characters, but not an address: these get past the first check, so only ip_address() refuses
    # them. Without it the line would show client=deadbeef or client=1.2.3.4.5.
    for host in ("deadbeef", "1.2.3.4.5", ":::", "1.1", "1.2.3.256", "1::2::3"):
        assert re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", host), host
        assert repair_switch_client(request(host)) == "-", host


# SV4 (spec 6.8)
@pytest.mark.parametrize(
    "value",
    ["sometimes", None, 2, "shadow", [], {"enabled": True}],
    ids=["sometimes", "null", "2", "shadow", "list", "object"],
)
def test_create_project_rejects_invalid_repair_enabled(server: TestClient, value: Any) -> None:
    response = server.post("/projects", json={**BODY, "repair_enabled": value})
    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "repair_enabled"]]
    # Refused before anything is written.
    assert server.get("/projects").json() == []
    with db.get_conn() as conn:
        assert conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()[0] == 0
    # The same value is refused for bootstrap_enabled, the plain bool it copies.
    assert server.post("/projects", json={**BODY, "bootstrap_enabled": value}).status_code == 422


# SV5 (spec 2.1, 6.8)
def test_every_project_response_reports_repair_enabled(server: TestClient) -> None:
    for extra, expected in (({}, False), ({"repair_enabled": True}, True)):
        created = _create(server, **extra)
        project_id = created["project"]["id"]
        assert (
            server.post(
                f"/projects/{project_id}/intents",
                json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
            ).status_code
            == 201
        )
        responses = {"create": created["project"], "get": server.get(f"/projects/{project_id}").json()["project"]}
        responses["title"] = server.put(f"/projects/{project_id}/title", json={"title": "renamed"}).json()
        responses["status stopped"] = server.put(f"/projects/{project_id}/status", json={"status": "stopped"}).json()
        responses["status unchanged"] = server.put(f"/projects/{project_id}/status", json={"status": "stopped"}).json()
        responses["status active"] = server.put(f"/projects/{project_id}/status", json={"status": "active"}).json()
        reason = {"worker": "reasoner", "trigger": "facts:2->3"}
        responses["reason claim"] = server.post(f"/projects/{project_id}/reason/claim", json=reason).json()
        responses["reason claim again"] = server.post(f"/projects/{project_id}/reason/claim", json=reason).json()
        worker = {"worker": "reasoner"}
        responses["reason heartbeat"] = server.post(f"/projects/{project_id}/reason/heartbeat", json=worker).json()
        responses["reason release"] = server.post(f"/projects/{project_id}/reason/release", json=worker).json()
        responses["reason release again"] = server.post(f"/projects/{project_id}/reason/release", json=worker).json()
        assert (
            server.post(
                f"/projects/{project_id}/intents/i001/conclude", json={"worker": "explorer", "description": "a fact"}
            ).status_code
            == 200
        )
        assert (
            server.post(
                f"/projects/{project_id}/complete",
                json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
            ).status_code
            == 200
        )
        reopened = server.post(f"/projects/{project_id}/reopen", json={"description": "not yet", "creator": "human"})
        assert reopened.status_code == 200
        responses["reopen"] = reopened.json()["project"]
        responses["get after reopen"] = server.get(f"/projects/{project_id}").json()["project"]

        for name, project in responses.items():
            assert set(project) == PROJECT_KEYS, name
            assert project["id"] == project_id, name
            assert project["repair_enabled"] is expected, name
        listed = _listed(server, project_id)
        assert set(listed) == SUMMARY_KEYS
        assert listed["repair_enabled"] is expected


# SV7 (spec 6.8)
def test_a_missing_switch_column_never_fails_project_reads(server: TestClient) -> None:
    project_id = _create(server)["project"]["id"]
    with db.get_conn() as conn:
        conn.execute("ALTER TABLE projects DROP COLUMN repair_enabled")
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        assert "repair_enabled" not in row.keys()  # noqa: SIM118 (on a sqlite3.Row, `in row` tests the values)
        assert project_repair_enabled(row) is False

    listed = server.get("/projects")
    assert listed.status_code == 200
    assert [item["repair_enabled"] for item in listed.json()] == [False]
    detail = server.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert detail.json()["project"]["repair_enabled"] is False
    reason = {"worker": "reasoner", "trigger": "facts:2->3"}
    for method, path, body in (
        ("PUT", f"/projects/{project_id}/title", {"title": "renamed"}),
        ("PUT", f"/projects/{project_id}/status", {"status": "stopped"}),
        ("PUT", f"/projects/{project_id}/status", {"status": "active"}),
        ("POST", f"/projects/{project_id}/reason/claim", reason),
        ("POST", f"/projects/{project_id}/reason/heartbeat", {"worker": "reasoner"}),
        ("POST", f"/projects/{project_id}/reason/release", {"worker": "reasoner"}),
    ):
        response = server.request(method, path, json=body)
        assert response.status_code == 200, (path, response.text)
        assert response.json()["repair_enabled"] is False, path
    assert (
        server.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
        ).status_code
        == 201
    )
    assert (
        server.post(
            f"/projects/{project_id}/intents/i001/conclude", json={"worker": "explorer", "description": "a fact"}
        ).status_code
        == 200
    )
    assert (
        server.post(
            f"/projects/{project_id}/complete", json={"from": ["f001"], "description": "solved", "worker": "reasoner"}
        ).status_code
        == 200
    )
    reopened = server.post(f"/projects/{project_id}/reopen", json={"description": "not yet", "creator": "human"})
    assert reopened.status_code == 200
    assert reopened.json()["project"]["repair_enabled"] is False
    assert server.get(f"/projects/{project_id}/export", params={"format": "yaml"}).status_code == 200
    # A statement that named the dropped column would fail: a create with the switch off runs none.
    off = _create(server, repair_enabled=False)["project"]
    assert off["repair_enabled"] is False
    assert server.get(f"/projects/{off['id']}").json()["project"]["repair_enabled"] is False


# The project fields an older server sends: no repair_enabled.
OLDER_PROJECT = {
    "id": "proj_001",
    "title": "t",
    "status": "active",
    "bootstrap_enabled": True,
    "created_at": "2026-01-01T00:00:00Z",
    "reason": None,
}
COUNTS = {"fact_count": 2, "intent_count": 1, "working_intent_count": 0, "unclaimed_intent_count": 1, "hint_count": 0}
_MISSING = object()
# Each value with whether it reads as on. Only a JSON true does.
SWITCH_VALUES = [(_MISSING, False), (None, False), (1, False), ("yes", False), (2, False), ("shadow", False)]
SWITCH_VALUES += [(True, True), (False, False), ("true", False), (1.0, False), ([], False), ({}, False)]


def _project(index: int, value: Any) -> dict[str, Any]:
    project = {**OLDER_PROJECT, "id": f"proj_{index:03d}", "title": f"project {index}"}
    if value is not _MISSING:
        project["repair_enabled"] = value
    return project


class _ProjectMetaAtEe0f227(BaseModel):
    """ProjectMeta as an older dispatcher (ee0f227) has it, without the field."""

    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


# SV9 (spec 2.1, 6.8)
def test_project_meta_parses_older_server_payloads() -> None:
    # As the dispatcher reads them: response.json(), then TypeAdapter(list[ProjectSummary]).validate_python.
    text = json.dumps([{**_project(index, value), **COUNTS} for index, (value, _on) in enumerate(SWITCH_VALUES)])
    summaries = TypeAdapter(list[ProjectSummary]).validate_python(json.loads(text))
    assert [summary.repair_enabled for summary in summaries] == [on for _value, on in SWITCH_VALUES]
    # Every project in the list parses, with every other field as sent.
    for index, summary in enumerate(summaries):
        assert summary.model_dump(exclude={"repair_enabled"}) == {**_project(index, _MISSING), **COUNTS}
    assert [summary.repair_enabled for summary in TypeAdapter(list[ProjectSummary]).validate_json(text)] == [
        on for _value, on in SWITCH_VALUES
    ]

    for index, (value, on) in enumerate(SWITCH_VALUES):
        payload = {"project": _project(index, value), "facts": [], "intents": [], "hints": []}
        detail = ProjectDetail.model_validate(json.loads(json.dumps(payload)))
        assert detail.project.repair_enabled is on, value
        assert detail.project.model_dump(exclude={"repair_enabled"}) == _project(index, _MISSING)
        assert ProjectMeta.model_validate(_project(index, value)).repair_enabled is on, value

    # A missing field is off, and the default is the plain False the server writes.
    assert ProjectMeta.model_validate(OLDER_PROJECT).repair_enabled is False
    assert ProjectMeta.model_validate(OLDER_PROJECT).model_dump()["repair_enabled"] is False
    # And an older dispatcher reading this server ignores the new field.
    newer = {**OLDER_PROJECT, "repair_enabled": True}
    assert _ProjectMetaAtEe0f227.model_validate(newer).model_dump() == OLDER_PROJECT


# --------------------------------------------------------------------------- the run lifecycle (spec 5)

BUDGET = 1800
# The review window of an 1800 s run started at 10:00:00 (spec 5.4): it opens at 10:22:30 (75%), closes at
# 10:24:00 (80%) and accepts a review started in time until 10:29:50 (10 s before the deadline).
OPENS, CLOSES, ACCEPT_UNTIL = 1350, 1440, 1790
ONE_CLAIM = (("i001", "w1"),)


# --------------------------------------------------------------------------- the intent's side: today's endpoints


def new_project(server: TestClient, *, intents: int = 1) -> str:
    """A project with Repair on, created through today's POST /projects, and its open intents i001, i002, ..."""
    response = server.post(
        "/projects",
        json={"title": "t", "origin": "start", "goal": "finish", "bootstrap_enabled": False, "repair_enabled": True},
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["project"]["id"]
    for number in range(1, intents + 1):
        response = server.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": f"investigate {number}", "creator": "reasoner", "worker": None},
        )
        assert response.status_code == 201, response.text
    return project_id


def heartbeat(server: TestClient, project_id: str, intent_id: str = "i001", worker: str = "w1") -> None:
    """POST /projects/{id}/intents/{intent_id}/heartbeat. The keep-alive sends hundreds per test, so it calls the
    endpoint's own function, which runs the same code and SQL without the HTTP round trip; a refusal raises.
    """
    del server  # the same server: the function uses the configured database
    intent = intents_router.heartbeat(project_id, intent_id, HeartbeatRequest(worker=worker))
    assert (intent.worker, intent.last_heartbeat_at) == (worker, services.utcnow())


def advance(
    server: TestClient,
    clock: Clock,
    project_id: str,
    until: float,
    claims: tuple[tuple[str, str], ...] = ONE_CLAIM,
    *,
    step: float = 10,
    polls: bool = False,
) -> None:
    """Move the clock to `until` seconds after START in steps; at each step every claim heartbeats, as today's
    heartbeat lease does while a run's task holds its intent, and, with polls, the repair agent polls.
    """
    while clock.elapsed < until:
        clock.to(min(clock.elapsed + step, until))
        for intent_id, worker in claims:
            heartbeat(server, project_id, intent_id, worker)
        if polls:
            poll()


def project_intents(server: TestClient, project_id: str) -> list[dict[str, Any]]:
    """The intents as GET /projects/{id} shows them. Like the UI's read, it runs today's expire_workers."""
    response = server.get(f"/projects/{project_id}")
    assert response.status_code == 200, response.text
    return response.json()["intents"]


def intent_of(server: TestClient, project_id: str, intent_id: str) -> dict[str, Any]:
    return next(intent for intent in project_intents(server, project_id) if intent["id"] == intent_id)


def set_switch(project_id: str, enabled: bool) -> None:
    """The project's Repair switch, written directly (its endpoint is tested in test_repair_server_auth.py, SV6)."""
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET repair_enabled = ? WHERE id = ?", (int(enabled), project_id))


# --------------------------------------------------------------------------- the run's side: the repair functions


def register(
    run_id: str,
    project_id: str,
    intent_id: str = "i001",
    worker: str = "w1",
    *,
    cli_type: str = "codex",
    budget: int = BUDGET,
    started_seconds_ago: float = 0.0,
) -> None:
    """A register's sweep and insert, in one transaction (the register's checks come with its endpoint)."""
    body = RunRegisterRequest(
        project_id=project_id,
        intent_id=intent_id,
        worker=worker,
        cli_type=cli_type,
        execution="container",
        budget_seconds=budget,
        started_seconds_ago=started_seconds_ago,
        locator={"container": f"cairn-dispatch-{project_id}", "prompt_marker": None},
    )
    with repair.immediate_transaction() as conn:
        now = repair.server_now()
        repair.sweep(conn, now, repair.agent_last_seen())
        repair.insert_run(conn, run_id, body, now)


def sweep() -> None:
    """The sweep any repair call makes first, such as a register of another run."""
    with repair.immediate_transaction() as conn:
        repair.sweep(conn, repair.server_now(), repair.agent_last_seen())


def poll() -> None:
    """The agent's default poll of GET /repair/runs: it sweeps with the previous poll's time, then records its own."""
    with repair.immediate_transaction() as conn:
        now = repair.server_now()
        repair.sweep(conn, now, repair.agent_last_seen())
    repair.record_agent_poll(now)


def end(run_id: str, **fields: Any) -> sqlite3.Row:
    """One of the dispatcher's two /end calls."""
    with repair.immediate_transaction() as conn:
        row = repair.end_run(
            conn,
            run_id,
            RunEndRequest.model_validate(fields),
            now=repair.server_now(),
            agent_seen_before=repair.agent_last_seen(),
        )
    assert row is not None
    return row


def exit_end(run_id: str, *, exited_seconds_ago: float = 0.0, timed_out: bool = False, returncode: int = 0) -> None:
    """The /end the dispatcher sends when the execute process exits."""
    end(
        run_id,
        timed_out=timed_out,
        cancelled=False,
        returncode=returncode,
        exited_seconds_ago=exited_seconds_ago,
    )


def outcome_end(run_id: str, outcome: str = "success") -> None:
    """The /end the dispatcher sends after the task returns."""
    end(run_id, outcome=outcome)


def record_review(conn: sqlite3.Connection, run_id: str, **fields: Any) -> IntentIssue:
    """An agent review posted inside the run's window, in the caller's transaction: the post's sweep, then the
    record."""
    now = repair.server_now()
    repair.sweep(conn, now, repair.agent_last_seen())
    run = repair.find_run(conn, run_id)
    assert run is not None and run["ended_at"] is None
    view = repair.run_view(conn, run, now)
    assert view["window"]["review_status"] in ("open", "late")
    body = {
        "review_id": f"rv-{run_id}-0001",
        "window": 1,
        "decision": "extend",
        "reason": "The extraction is advancing and the next step depends on its result.",
        "evidence": ["E14 t+21:10 second partition found", "E16 t+22:10 extraction at 70%"],
        "next_step": "List /etc once the extraction finishes",
        "expected_version": 1,
        "digest_elapsed_seconds": view["elapsed_seconds"],
        "model": "test-model",
        "digest": "a digest of the run",
        "raw_output": '{"decision": "extend"}',
        **fields,
    }
    _review, issue = repair.record_agent_review(conn, run, ReviewRequest.model_validate(body), now)
    return issue


def review(run_id: str, **fields: Any) -> IntentIssue:
    """An agent review posted inside the run's window, in its own transaction."""
    with repair.immediate_transaction() as conn:
        return record_review(conn, run_id, **fields)


def run_row(run_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM repair_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row is not None
    return dict(row)


def view(run_id: str, *, full_reviews: bool = False) -> dict[str, Any]:
    with db.get_conn() as conn:
        run = repair.find_run(conn, run_id)
        assert run is not None
        return repair.run_view(conn, run, repair.server_now(), full_reviews=full_reviews)


def review_rows(run_id: str | None = None) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        if run_id is None:
            rows = conn.execute("SELECT * FROM repair_reviews ORDER BY run_id, window_no").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM repair_reviews WHERE run_id = ? ORDER BY window_no", (run_id,)
            ).fetchall()
    return [dict(row) for row in rows]


def issue_rows(run_id: str | None = None) -> list[dict[str, Any]]:
    """The intent_issues rows (of one run), oldest first, with their detail parsed."""
    with db.get_conn() as conn:
        if run_id is None:
            rows = conn.execute("SELECT * FROM intent_issues ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM intent_issues WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]


def not_reviewed_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == REPAIR_LOGGER and record.getMessage().startswith("repair window not reviewed")
    ]


def not_reviewed_detail(**changes: Any) -> dict[str, Any]:
    """The detail of the server's not-reviewed record of an 1800 s run started at START (spec 4.5, 5.5)."""
    return {
        "decision": "not_reviewed",
        "mode": "shadow",
        "applied": False,
        "window": 1,
        "run_started_at": at(0),
        "elapsed_seconds": ACCEPT_UNTIL,
        "budget_seconds": BUDGET,
        "window_seconds": BUDGET,
        "deadline_at": at(BUDGET),
        "slices_used": 0,
        "display_truncated": False,
        "why": "agent_not_seen",
        "agent_last_seen_at": None,
        "run_ended": False,
        **changes,
    }


# --------------------------------------------------------------------------- SV23, SV24: liveness (spec 5.1, 5.2)


# SV23
@pytest.mark.parametrize("how", ["conclude", "release", "lapse", "stop", "complete", "other_worker"])
def test_runs_end_when_their_claim_ends(server: TestClient, clock: Clock, how: str) -> None:
    project_id = new_project(server)
    heartbeat(server, project_id)
    register("run-0001", project_id)
    advance(server, clock, project_id, 60)  # the run's task heartbeats, the last time at 10:01:00
    sweep()
    assert run_row("run-0001")["ended_at"] is None

    if how == "conclude":
        clock.to(65)
        response = server.post(
            f"/projects/{project_id}/intents/i001/conclude", json={"worker": "w1", "description": "the answer"}
        )
        assert response.status_code == 200, response.text
        expected = ("concluded", at(65), response.json()["fact"]["id"])  # the intent's concluded_at and fact
    elif how == "release":
        clock.to(65)
        response = server.post(f"/projects/{project_id}/intents/i001/release", json={"worker": "w1"})
        assert response.status_code == 200, response.text
        expected = ("released", at(60), None)  # the claim cleared with a heartbeat younger than intent_timeout
    elif how == "lapse":
        # No heartbeat after 10:01:00. The project read clears the claim once it is older than intent_timeout,
        # as today; the run outlives the gap up to repair_lapse_seconds, 60 s at the default intent_timeout of 15.
        clock.to(80)
        assert intent_of(server, project_id, "i001")["worker"] is None
        sweep()
        assert run_row("run-0001")["ended_at"] is None
        clock.to(120)  # exactly repair_lapse_seconds: still open
        sweep()
        assert run_row("run-0001")["ended_at"] is None
        expected = ("claim_lost", at(60), None)  # one second later
    elif how == "stop":
        clock.to(65)
        assert server.put(f"/projects/{project_id}/status", json={"status": "stopped"}).status_code == 200
        expected = ("project_inactive", at(60), None)
    elif how == "complete":
        clock.to(65)
        response = server.post(
            f"/projects/{project_id}/complete", json={"from": ["origin"], "description": "done", "worker": "reasoner"}
        )
        assert response.status_code == 200, response.text
        expected = ("project_inactive", at(60), None)
    else:
        # The claim lapses at intent_timeout and another worker takes the intent: when this run stopped is unknown.
        clock.to(76)
        heartbeat(server, project_id, worker="w2")
        expected = ("claim_taken", None, None)

    clock.to(clock.elapsed + 1)
    ended_at = clock()
    sweep()
    row = run_row("run-0001")
    assert (row["end_reason"], row["last_alive_at"], row["fact_id"]) == expected
    assert (row["state"], row["ended_at"], row["updated_at"], row["outcome"]) == ("ended", ended_at, ended_at, None)
    run = view("run-0001")
    assert (run["state"], run["elapsed_seconds"], run["remaining_seconds"]) == ("ended", None, None)
    assert (run["end_reason"], run["last_alive_at"], run["fact_id"]) == expected
    assert run["window"]["review_status"] == "closed"
    assert review_rows() == [] and issue_rows() == []

    # Idempotent: the row has ended and its window is settled, so later sweeps leave it as it is.
    clock.to(clock.elapsed + 30)
    sweep()
    poll()
    assert run_row("run-0001") == row


# SV24
def test_a_short_heartbeat_gap_leaves_the_run_open_and_reviewable(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server)
    heartbeat(server, project_id)
    register("run-0001", project_id)
    advance(server, clock, project_id, 600)  # the last heartbeat before the gap, at 10:10:00
    with db.get_conn() as conn:
        intent_timeout = services.get_intent_timeout(conn)
    assert intent_timeout == 15

    # A gap of intent_timeout + 1 s. Today's expire_workers clears the claim on the project read.
    clock.to(600 + intent_timeout + 1)
    intent = intent_of(server, project_id, "i001")
    assert (intent["worker"], intent["last_heartbeat_at"]) == (None, at(600))
    poll()
    assert (run_row("run-0001")["ended_at"], run_row("run-0001")["end_reason"]) == (None, None)

    # The heartbeats resume: the same worker re-claims its intent, as today's protocol allows.
    heartbeat(server, project_id)
    poll()
    assert run_row("run-0001")["ended_at"] is None

    # A second gap of the same length, this time with the claim still held (nothing read the project).
    advance(server, clock, project_id, 900, polls=True)
    clock.to(900 + intent_timeout + 1)
    poll()
    heartbeat(server, project_id)
    assert run_row("run-0001")["ended_at"] is None

    # The run reaches its review window open and reviewable, and its review is recorded.
    advance(server, clock, project_id, OPENS + 10, polls=True)
    run = view("run-0001")
    assert (run["state"], run["ended_at"], run["end_reason"], run["last_alive_at"]) == ("running", None, None, None)
    assert (run["window"]["reviewable"], run["window"]["review_status"]) == (True, "open")
    issue = review("run-0001")
    assert issue.detail["decision"] == "extend"
    assert view("run-0001")["window"]["review_status"] == "reviewed"
    row = run_row("run-0001")
    assert (row["ended_at"], row["settled_window"], row["updated_at"]) == (None, 1, at(OPENS + 10))
    assert [row["source"] for row in review_rows("run-0001")] == ["agent"]


def test_a_gap_of_exactly_intent_timeout_is_not_a_release(server: TestClient, clock: Clock) -> None:
    """Today's expire_workers compares julianday floats, and for some heartbeat times their rounding reads an age of
    exactly intent_timeout as older, so it clears the claim of a live run at that age. A sweep in the same second
    must read that as a heartbeat gap, not as a release, or the run would end and drop out of the shadow data.
    """
    project_id = new_project(server, intents=2)
    heartbeat(server, project_id)
    heartbeat(server, project_id, "i002", "w2")
    register("run-0001", project_id)
    with db.get_conn() as conn:
        intent_timeout = services.get_intent_timeout(conn)
        # The heartbeat seconds from 10:01:00 on whose age of exactly intent_timeout SQLite reads as older (SQLite
        # 3.53.1: 38 of them before 10:30:00, the first at 10:01:45).
        rounding = [
            seconds
            for seconds in range(60, BUDGET)
            if conn.execute(
                "SELECT (julianday(?) - julianday(?)) * 86400 > ?",
                (at(seconds + intent_timeout), at(seconds), intent_timeout),
            ).fetchone()[0]
        ]
    assert rounding, "no heartbeat second whose age of exactly intent_timeout SQLite reads as older"
    last = rounding[0]
    both = (("i001", "w1"), ("i002", "w2"))
    advance(server, clock, project_id, last, both)  # the run's last heartbeat before the gap
    advance(server, clock, project_id, last + intent_timeout, (("i002", "w2"),))

    # The other intent's heartbeat runs today's expire_workers, which clears this claim at an age of exactly
    # intent_timeout; then the agent polls in that same second.
    intent = intent_of(server, project_id, "i001")
    assert (intent["worker"], intent["last_heartbeat_at"]) == (None, at(last))
    poll()
    row = run_row("run-0001")
    assert (row["state"], row["ended_at"], row["end_reason"], row["last_alive_at"]) == ("running", None, None, None)

    # The heartbeats resume, the same worker re-claims its intent, and the run reaches its review window.
    advance(server, clock, project_id, OPENS + 10, both, polls=True)
    run = view("run-0001")
    assert (run["state"], run["ended_at"], run["window"]["review_status"]) == ("running", None, "open")
    assert review("run-0001").detail["decision"] == "extend"
    assert run_row("run-0001")["ended_at"] is None


@pytest.mark.parametrize("polls", [False, True], ids=["agent_down_before_the_gap", "agent_polling"])
def test_a_gap_is_not_a_release_after_intent_timeout_is_raised(server: TestClient, clock: Clock, polls: bool) -> None:
    """Today's expire_workers clears a claim by the intent_timeout in force when it runs, and PUT /settings (the UI's
    Server Settings dialog) can raise that value at any time. A claim cleared in a gap longer than the old value is
    a heartbeat gap, also once the value has been raised: the sweep compares the heartbeat's age with every value in
    force since the heartbeat, as far as the sweeps have read them, not only with today's. Without the agent's polls,
    the register's sweep is the only one that read the old value.
    """
    project_id = new_project(server)
    heartbeat(server, project_id)
    register("run-0001", project_id)  # its sweep reads today's intent_timeout, 15
    advance(server, clock, project_id, 600, polls=polls)  # the last heartbeat before the gap, at 10:10:00
    settings = server.get("/settings").json()
    assert settings["intent_timeout"] == 15

    # A gap of 17 s: the project read clears the claim (older than 15). Then the owner raises intent_timeout to 30,
    # and the agent polls in that same second.
    clock.to(617)
    assert intent_of(server, project_id, "i001")["worker"] is None
    response = server.put("/settings", json={**settings, "intent_timeout": 30})
    assert response.status_code == 200, response.text
    poll()
    row = run_row("run-0001")
    assert (row["state"], row["ended_at"], row["end_reason"], row["last_alive_at"]) == ("running", None, None, None)

    # The heartbeats resume, the same worker re-claims its intent, and the run reaches its review window.
    clock.to(619)
    heartbeat(server, project_id)
    advance(server, clock, project_id, OPENS + 10, polls=True)
    run = view("run-0001")
    assert (run["state"], run["ended_at"], run["window"]["review_status"]) == ("running", None, "open")
    assert review("run-0001").detail["decision"] == "extend"

    # A release follows the raised value once the sweeps had read it before the heartbeat: the run's task releases
    # its intent, and a sweep 20 s after the last heartbeat (older than 15, younger than 30) ends the run as released.
    last = OPENS + 10
    clock.to(last + 1)
    response = server.post(f"/projects/{project_id}/intents/i001/release", json={"worker": "w1"})
    assert response.status_code == 200, response.text
    clock.to(last + 20)
    sweep()
    row = run_row("run-0001")
    assert (row["state"], row["end_reason"], row["last_alive_at"], row["ended_at"]) == (
        "ended",
        "released",
        at(last),
        at(last + 20),
    )


# --------------------------------------------------------------------------- SV37-SV41: the not-reviewed rule (5.5)


# SV37
@pytest.mark.parametrize(
    ("last_poll", "recorded_by", "records_at", "why"),
    [
        # The agent polls until 10:29:49; its next poll records it.
        pytest.param(ACCEPT_UNTIL - 1, "poll", ACCEPT_UNTIL, "agent_seen", id="polling"),
        # Its last poll was exactly when the window opened, 10:22:30 ("at or after": seen), or 1 s before (not seen);
        # then it is down, and another repair call records it.
        pytest.param(OPENS, "sweep", ACCEPT_UNTIL, "agent_seen", id="last_poll_as_the_window_opened"),
        pytest.param(OPENS - 1, "sweep", ACCEPT_UNTIL, "agent_not_seen", id="last_poll_before_the_window_opened"),
        # It polls until 10:05:00 and is down until its first poll after the downtime, which records it.
        pytest.param(300, "poll", ACCEPT_UNTIL, "agent_not_seen", id="back_after_downtime"),
        pytest.param(None, "sweep", ACCEPT_UNTIL, "agent_not_seen", id="never_polled"),
        # No repair call at the accept limit: the first one, 5 s later, records it with the time used by then.
        pytest.param(None, "sweep", ACCEPT_UNTIL + 5, "agent_not_seen", id="first_call_after_the_limit"),
    ],
)
def test_a_live_run_without_review_is_flagged_once_at_the_accept_limit(
    server: TestClient,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    last_poll: int | None,
    recorded_by: str,
    records_at: int,
    why: str,
) -> None:
    caplog.set_level(logging.INFO, logger=REPAIR_LOGGER)
    project_id = new_project(server)
    heartbeat(server, project_id)
    register("run-0001", project_id)
    if last_poll is not None:
        advance(server, clock, project_id, last_poll, polls=True)  # the agent's polls, the last one at last_poll
    advance(server, clock, project_id, ACCEPT_UNTIL - 1)
    sweep()  # a repair call 1 s before the accept limit records nothing
    assert review_rows() == [] and issue_rows() == []
    assert run_row("run-0001")["settled_window"] == 0
    assert view("run-0001")["window"]["review_status"] == "late"

    # From the accept limit, 10:29:50, the next poll or other repair call records it. Its sweep runs with the poll
    # before it, so a returning agent's downtime is not agent_seen.
    advance(server, clock, project_id, records_at)
    if records_at > ACCEPT_UNTIL:
        assert view("run-0001")["window"]["review_status"] == "closed"  # until a sweep records it
    if recorded_by == "poll":
        poll()
    else:
        sweep()
    last_seen = None if last_poll is None else at(last_poll)
    detail = not_reviewed_detail(elapsed_seconds=records_at, why=why, agent_last_seen_at=last_seen)
    issues = issue_rows()
    assert [(issue["kind"], issue["run_id"], issue["intent_id"]) for issue in issues] == [
        ("repair_review", "run-0001", "i001")
    ]
    assert issues[0]["detail"] == detail
    assert (issues[0]["created_at"], issues[0]["updated_at"]) == (at(records_at), at(records_at))
    reviews = review_rows()
    assert [
        (row["run_id"], row["window_no"], row["review_id"], row["source"], row["decision"], row["issue_id"])
        for row in reviews
    ] == [("run-0001", 1, "server", "server", "not_reviewed", issues[0]["id"])]
    assert json.loads(reviews[0]["detail_json"]) == detail
    assert (reviews[0]["digest"], reviews[0]["digest_sha256"], reviews[0]["raw_output"]) == (None, None, None)
    row = run_row("run-0001")
    assert (row["settled_window"], row["ended_at"], row["state"]) == (1, None, "running")
    assert view("run-0001")["window"]["review_status"] == "not_reviewed"
    assert not_reviewed_lines(caplog) == [
        (logging.INFO, f"repair window not reviewed run=run-0001 project={project_id} intent=i001 window=1 why={why}")
    ]
    # The record is the intent's issue, read the way the UI reads it.
    assert [issue["kind"] for issue in intent_of(server, project_id, "i001")["issues"]] == ["repair_review"]

    # Once: later sweeps, both of the dispatcher's /end calls and the rule applied again record nothing more.
    advance(server, clock, project_id, BUDGET, polls=True)
    clock.to(BUDGET + 1)
    exit_end("run-0001", timed_out=True, returncode=124)
    advance(server, clock, project_id, BUDGET + 20, polls=True)
    outcome_end("run-0001")
    with repair.immediate_transaction() as conn:
        run = repair.find_run(conn, "run-0001")
        assert run is not None
        assert repair.settle_window(conn, run, repair.server_now(), None, again=True) is False
    assert len(review_rows()) == 1 and len(issue_rows()) == 1
    assert len(not_reviewed_lines(caplog)) == 1


# SV38
@pytest.mark.parametrize("ending", ["end", "release"])
def test_a_run_ending_after_the_window_closed_is_flagged_at_its_end(
    server: TestClient, clock: Clock, ending: str
) -> None:
    project_id = new_project(server, intents=2)
    claims = (("i001", "w1"), ("i002", "w2"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)
    register("run-0001", project_id, "i001", "w1")
    register("run-0002", project_id, "i002", "w2")
    advance(server, clock, project_id, CLOSES, claims)

    def finish(run_id: str, intent_id: str, worker: str, alive_until: float) -> None:
        # The run was last alive at alive_until; the server hears of its end 2 s later.
        if ending == "end":
            clock.to(alive_until + 2)
            exit_end(run_id, exited_seconds_ago=2.0)
        else:
            clock.to(alive_until + 1)
            response = server.post(f"/projects/{project_id}/intents/{intent_id}/release", json={"worker": worker})
            assert response.status_code == 200, response.text
            clock.to(alive_until + 2)
            sweep()

    end_reason = "dispatcher" if ending == "end" else "released"
    # run-0002 was last alive exactly when its window closed (80%): that is reaching the limit.
    finish("run-0002", "i002", "w2", CLOSES)
    assert [issue["detail"] for issue in issue_rows("run-0002")] == [
        not_reviewed_detail(elapsed_seconds=CLOSES, run_ended=True, run_ended_at=at(CLOSES), end_reason=end_reason)
    ]
    # run-0001 was last alive at 85%, after its window closed, and before its accept limit.
    advance(server, clock, project_id, 1530, (("i001", "w1"),))
    sweep()
    assert issue_rows("run-0001") == []
    finish("run-0001", "i001", "w1", 1530)
    issues = issue_rows("run-0001")
    assert [issue["detail"] for issue in issues] == [
        not_reviewed_detail(elapsed_seconds=1530, run_ended=True, run_ended_at=at(1530), end_reason=end_reason)
    ]
    assert issues[0]["created_at"] == at(1532)
    assert [(row["source"], row["decision"], row["issue_id"]) for row in review_rows("run-0001")] == [
        ("server", "not_reviewed", issues[0]["id"])
    ]
    assert (run_row("run-0001")["last_alive_at"], run_row("run-0001")["settled_window"]) == (at(1530), 1)
    assert view("run-0001")["window"]["review_status"] == "not_reviewed"

    # Flagged once: the late or second /end calls and later sweeps add nothing.
    clock.to(1540)
    if ending == "release":
        exit_end("run-0001", exited_seconds_ago=10.0)
    outcome_end("run-0001")
    outcome_end("run-0002")
    advance(server, clock, project_id, BUDGET + 30, (), polls=True)
    assert [len(review_rows(run_id)) for run_id in ("run-0001", "run-0002")] == [1, 1]
    assert [len(issue_rows(run_id)) for run_id in ("run-0001", "run-0002")] == [1, 1]


# SV39
@pytest.mark.parametrize("ending", ["end", "conclude"])
def test_a_run_ending_before_the_window_closes_is_not_flagged(server: TestClient, clock: Clock, ending: str) -> None:
    project_id = new_project(server)
    heartbeat(server, project_id)
    register("run-0001", project_id)
    if ending == "end":
        # The process exits at 10:23:59, inside the window and 1 s before it closes. Its /end reaches the server
        # after the window closed: the reported exit time decides, not the time the call arrives.
        advance(server, clock, project_id, CLOSES - 1, polls=True)
        clock.to(CLOSES + 3)
        exit_end("run-0001", exited_seconds_ago=4.0)
        last_alive = at(CLOSES - 1)
    else:
        # The run's intent concludes at 77.8% (10:23:20), inside the window.
        advance(server, clock, project_id, 1400, polls=True)
        response = server.post(
            f"/projects/{project_id}/intents/i001/conclude", json={"worker": "w1", "description": "the answer"}
        )
        assert response.status_code == 200, response.text
        clock.to(1401)
        poll()
        last_alive = at(1400)
    row = run_row("run-0001")
    assert (row["state"], row["last_alive_at"], row["settled_window"]) == ("ended", last_alive, 1)
    assert review_rows() == [] and issue_rows() == []

    # Nothing later flags it: the agent's polls through the accept limit and the deadline, and the outcome /end.
    advance(server, clock, project_id, BUDGET + 60, (), polls=True)
    outcome_end("run-0001")
    poll()
    assert review_rows() == [] and issue_rows() == []
    assert view("run-0001")["window"]["review_status"] == "closed"
    assert intent_of(server, project_id, "i001")["issues"] == []


# SV40
def test_short_windows_and_unreviewable_clis_are_never_flagged(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server, intents=4)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"), ("i004", "w4"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)

    def registered(run_id: str, intent_id: str, worker: str, **fields: Any) -> tuple[int, int]:
        # The row as its register's own transaction left it. Each register sweeps before its insert, so this read
        # comes before any sweep could settle the run.
        register(run_id, project_id, intent_id, worker, **fields)
        row = run_row(run_id)
        return row["reviewable"], row["settled_window"]

    # Settled at register: never reviewed, never flagged.
    assert registered("run-short", "i001", "w1", budget=349) == (0, 1)  # a window shorter than 350 s
    assert registered("run-mock", "i002", "w2", cli_type="mock") == (0, 1)  # mock has no transcript
    assert registered("run-other", "i003", "w3", cli_type="other") == (0, 1)  # outside the reviewable CLI types
    assert registered("run-edge", "i004", "w4", budget=350) == (1, 0)  # the shortest reviewable window, for contrast
    never = ("run-short", "run-mock", "run-other")
    assert [(run_row(run_id)["reviewable"], run_row(run_id)["settled_window"]) for run_id in never] == [(0, 1)] * 3
    assert (run_row("run-edge")["reviewable"], run_row("run-edge")["settled_window"]) == (1, 0)
    for run_id in never:
        window = view(run_id)["window"]
        assert (window["reviewable"], window["review_status"]) == (False, "not_reviewable")

    # Every run lives past its accept limit and ends after its window closed, with the agent polling throughout.
    advance(server, clock, project_id, 340, claims, polls=True)  # run-edge's accept limit: 350 - 10 s
    assert [issue["run_id"] for issue in issue_rows()] == ["run-edge"]
    advance(server, clock, project_id, 352, claims, polls=True)
    exit_end("run-short", exited_seconds_ago=2.0, timed_out=True, returncode=124)  # its window closed at 279 s
    exit_end("run-edge", exited_seconds_ago=2.0, timed_out=True, returncode=124)
    # run-mock and run-other live past their accept limit (1790 s), then end after their windows closed.
    advance(server, clock, project_id, ACCEPT_UNTIL + 5, claims[1:3], polls=True)
    exit_end("run-mock")
    exit_end("run-other")
    advance(server, clock, project_id, BUDGET + 60, (), polls=True)
    for run_id in (*never, "run-edge"):
        outcome_end(run_id)
    poll()

    assert [issue["run_id"] for issue in issue_rows()] == ["run-edge"]
    assert [(row["run_id"], row["decision"]) for row in review_rows()] == [("run-edge", "not_reviewed")]
    for run_id in never:
        run = view(run_id)
        assert (run["state"], run["window"]["reviewable"], run["window"]["review_status"]) == (
            "ended",
            False,
            "not_reviewable",
        )
        assert run["reviews"] == []


# SV41
def test_nothing_is_flagged_while_the_switch_is_off(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server, intents=3)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)
    register("run-0001", project_id, "i001", "w1")
    register("run-0002", project_id, "i002", "w2")
    register("run-0003", project_id, "i003", "w3")
    advance(server, clock, project_id, 1000, claims, polls=True)

    # Switched off mid-run: the next sweep settles every window of the project with nothing recorded.
    set_switch(project_id, False)
    advance(server, clock, project_id, 1010, claims, polls=True)
    assert [run_row(run_id)["settled_window"] for run_id in ("run-0001", "run-0002", "run-0003")] == [1, 1, 1]

    # run-0003: its claim is taken after its window closed, and its late /end reports a later exit (rule 4).
    advance(server, clock, project_id, 1450, claims, polls=True)
    clock.to(1466)
    heartbeat(server, project_id, "i003", "w9")
    poll()
    assert run_row("run-0003")["end_reason"] == "claim_taken"
    # run-0002 exits after its window closed (rule 4), and run-0003's late /end reports an exit at 1600 s.
    advance(server, clock, project_id, 1600, claims[:2], polls=True)
    exit_end("run-0002")
    clock.to(1610)
    exit_end("run-0003", exited_seconds_ago=10.0)
    assert run_row("run-0003")["last_alive_at"] == at(1600)
    # run-0001 lives past its accept limit (rule 3). The switch is not part of liveness: it stays live and ends
    # through the dispatcher's /end, as any run does.
    advance(server, clock, project_id, ACCEPT_UNTIL + 5, claims[:1], polls=True)
    assert run_row("run-0001")["ended_at"] is None
    assert view("run-0001")["state"] == "running"
    clock.to(BUDGET + 1)
    exit_end("run-0001", timed_out=True, returncode=124)
    assert run_row("run-0001")["end_reason"] == "dispatcher"

    # Switched on again: the settled windows stay settled.
    set_switch(project_id, True)
    advance(server, clock, project_id, BUDGET + 30, (), polls=True)
    for run_id in ("run-0001", "run-0002", "run-0003"):
        outcome_end(run_id)
    poll()

    assert review_rows() == [] and issue_rows() == []
    assert [intent["issues"] for intent in project_intents(server, project_id)] == [[], [], []]
    assert [view(run_id)["window"]["review_status"] for run_id in ("run-0001", "run-0002", "run-0003")] == [
        "closed"
    ] * 3


# --------------------------------------------------------------------------- SV42: stamping (spec 5.6)


# SV42
def test_ended_run_issues_are_stamped_with_their_end(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server, intents=3)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)
    register("run-0001", project_id, "i001", "w1")
    register("run-0002", project_id, "i002", "w2")
    register("run-0003", project_id, "i003", "w3")

    def issue_of(run_id: str) -> dict[str, Any]:
        issues = issue_rows(run_id)
        assert len(issues) == 1
        return issues[0]

    # Agent reviews of run-0001 (at 1352 s) and run-0003 (at 1360 s): no end keys while the runs are live.
    advance(server, clock, project_id, 1352, claims, polls=True)
    review("run-0001")
    reviewed = issue_of("run-0001")
    assert {"run_ended_at", "end_reason", "outcome"}.isdisjoint(reviewed["detail"])
    advance(server, clock, project_id, 1360, claims, polls=True)
    review("run-0003")
    reviewed_late_end = issue_of("run-0003")

    # run-0003: its worker's heartbeats stop at 1400 s and another worker takes the intent. The sweep ends it with
    # an unknown time, so its issue gets the time the server marked it ended.
    advance(server, clock, project_id, 1400, claims, polls=True)
    advance(server, clock, project_id, 1416, claims[:2])
    heartbeat(server, project_id, "i003", "w9")
    clock.to(1417)
    poll()
    stamped = issue_of("run-0003")
    assert stamped["detail"] == {
        **reviewed_late_end["detail"],
        "run_ended_at": at(1417),
        "end_reason": "claim_taken",
    }
    assert stamped["updated_at"] == at(1417)
    # A late /end re-stamps run_ended_at with the reported exit time, keeping the end reason; then the outcome.
    advance(server, clock, project_id, 1450, claims[:2], polls=True)
    exit_end("run-0003", exited_seconds_ago=2.0)
    restamped = issue_of("run-0003")
    assert restamped["detail"] == {
        **reviewed_late_end["detail"],
        "run_ended_at": at(1448),
        "end_reason": "claim_taken",
    }
    assert (restamped["created_at"], restamped["updated_at"]) == (at(1360), at(1450))
    clock.to(1460)
    outcome_end("run-0003", "success")
    assert issue_of("run-0003")["detail"] == {**restamped["detail"], "outcome": "success"}
    assert [row["source"] for row in review_rows("run-0003")] == ["agent"]  # the rule applied again: a review exists

    # run-0002 has no review and exits after its window closed (rule 4): its record carries its end from the
    # insert, and the outcome once the outcome /end arrives.
    advance(server, clock, project_id, 1500, claims[:2], polls=True)
    exit_end("run-0002")
    flagged = issue_of("run-0002")
    assert flagged["detail"] == not_reviewed_detail(
        elapsed_seconds=1500,
        why="agent_seen",
        agent_last_seen_at=at(1500),
        run_ended=True,
        run_ended_at=at(1500),
        end_reason="dispatcher",
    )
    assert (flagged["created_at"], flagged["updated_at"]) == (at(1500), at(1500))
    clock.to(1510)
    outcome_end("run-0002", "failed")
    assert issue_of("run-0002")["detail"] == {**flagged["detail"], "outcome": "failed"}
    assert issue_of("run-0002")["updated_at"] == at(1510)

    # run-0001 times out: its review gets run_ended_at and end_reason at the first /end, the outcome at the second.
    advance(server, clock, project_id, BUDGET, claims[:1], polls=True)
    clock.to(BUDGET + 1)
    exit_end("run-0001", exited_seconds_ago=1.0, timed_out=True, returncode=124)
    exited = issue_of("run-0001")
    assert exited["detail"] == {**reviewed["detail"], "run_ended_at": at(BUDGET), "end_reason": "dispatcher"}
    assert (exited["created_at"], exited["updated_at"]) == (at(1352), at(BUDGET + 1))
    clock.to(BUDGET + 20)  # today's conclude fallback runs here
    outcome_end("run-0001", "success")
    finished = issue_of("run-0001")
    assert finished["detail"] == {**exited["detail"], "outcome": "success"}
    assert finished["updated_at"] == at(BUDGET + 20)
    run = view("run-0001")
    assert (run["state"], run["end_reason"], run["last_alive_at"], run["outcome"]) == (
        "ended",
        "dispatcher",
        at(BUDGET),
        "success",
    )

    # Stamps go to the issues only; the stored reviews keep what was recorded.
    assert [json.loads(row["detail_json"]).get("run_ended_at") for row in review_rows()] == [None, None, None]
    # The UI reads the stamped issues with the project.
    shown = {intent["id"]: intent["issues"] for intent in project_intents(server, project_id)}
    assert [shown[intent_id][0]["detail"]["outcome"] for intent_id in ("i001", "i002", "i003")] == [
        "success",
        "failed",
        "success",
    ]


# --------------------------------------------------------------------------- supporting tests (not in section 16)


def test_an_agent_review_is_stored_in_full_and_shown_as_a_display_copy(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server, intents=2)
    claims = (("i001", "w1"), ("i002", "w2"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)
    register("run-0001", project_id, "i001", "w1")
    register("run-0002", project_id, "i002", "w2")
    advance(server, clock, project_id, 1352, claims, polls=True)
    before = run_row("run-0001")

    reason = "Steady\nprogress  \u202e" + "r" * 500
    evidence = [f"E{number} " + "e" * 300 for number in range(1, 6)] + ["\u200b"]
    model = "test-model-" + "m" * 80
    issue = review(
        "run-0001",
        reason=reason,
        evidence=evidence,
        next_step="n" * 300,
        model=model,
        transcript_cut_seconds=1340,
        applied=True,  # unknown to the request model: the server sets applied
        mode="live",
    )
    timing = {
        "window": 1,
        "run_started_at": at(0),
        "elapsed_seconds": 1352,
        "budget_seconds": BUDGET,
        "window_seconds": BUDGET,
        "deadline_at": at(BUDGET),
        "slices_used": 0,
    }
    cleaned_reason = "Steady progress " + "r" * 500  # whitespace folded, the bidi override dropped
    cleaned_evidence = [f"E{number} " + "e" * 300 for number in range(1, 6)]  # the blank item dropped
    stored = review_rows("run-0001")
    assert [(row["source"], row["review_id"], row["decision"], row["issue_id"]) for row in stored] == [
        ("agent", "rv-run-0001-0001", "extend", issue.id)
    ]
    assert json.loads(stored[0]["detail_json"]) == {
        "decision": "extend",
        "mode": "shadow",
        "applied": False,
        **timing,
        "reason": cleaned_reason,
        "evidence": cleaned_evidence,
        "next_step": "n" * 300,
        "fallback": None,
        "model": model,
        "expected_version": 1,
        "digest_elapsed_seconds": 1352,
        "transcript_cut_seconds": 1340,
        "slice_seconds": repair.SLICE_SECONDS,
    }
    assert (stored[0]["digest"], stored[0]["raw_output"], stored[0]["created_at"]) == (
        "a digest of the run",
        '{"decision": "extend"}',
        at(1352),
    )
    assert stored[0]["digest_sha256"] == hashlib.sha256(b"a digest of the run").hexdigest()
    display = {
        "decision": "extend",
        "mode": "shadow",
        "applied": False,
        **timing,
        "reason": cleaned_reason[:400],
        "evidence": [item[:200] for item in cleaned_evidence[:4]],
        "next_step": "n" * 200,
        "display_truncated": True,
        "fallback": None,
        "model": model[:64],
        "slice_seconds": repair.SLICE_SECONDS,
    }
    assert issue.detail == display
    assert [(row["kind"], row["run_id"], row["detail"]) for row in issue_rows("run-0001")] == [
        ("repair_review", "run-0001", display)
    ]
    # Shadow mode: the decision is recorded, never applied. The run's window is settled.
    after = run_row("run-0001")
    for key in ("deadline_at", "version", "slices_used", "state", "ended_at"):
        assert after[key] == before[key], key
    assert (after["settled_window"], after["updated_at"]) == (1, at(1352))
    with db.get_conn() as conn:
        review_row = conn.execute("SELECT * FROM repair_reviews WHERE run_id = 'run-0001'").fetchone()
        body = repair.review_body(conn, review_row, created=True)
    assert ReviewResponse.model_validate(body).model_dump() == body
    assert body == {
        "created": True,
        "mode": "shadow",
        "applied": False,
        "review": {
            "run_id": "run-0001",
            "window": 1,
            "review_id": "rv-run-0001-0001",
            "source": "agent",
            "decision": "extend",
            "issue_id": issue.id,
            "created_at": at(1352),
        },
        "issue": issue.model_dump(),
    }

    # A fallback continue that fits its display copy: nothing cut, no slice, no next step.
    issue = review(
        "run-0002",
        decision="continue",
        fallback="transcript_unreadable",
        reason="the transcript could not be read",
        evidence=[],
        next_step="",
        model=None,
        digest=None,
        raw_output=None,
    )
    assert issue.detail == {
        "decision": "continue",
        "mode": "shadow",
        "applied": False,
        **timing,
        "reason": "the transcript could not be read",
        "evidence": [],
        "next_step": None,
        "display_truncated": False,
        "fallback": "transcript_unreadable",
        "model": None,
    }
    stored = review_rows("run-0002")
    assert (stored[0]["digest"], stored[0]["digest_sha256"], stored[0]["raw_output"]) == (None, None, None)


def test_window_times_and_the_run_view_follow_the_server_clock(server: TestClient, clock: Clock) -> None:
    project_id = new_project(server, intents=2)
    heartbeat(server, project_id)
    heartbeat(server, project_id, "i002", "w2")
    register("run-0001", project_id)
    register("run-0002", project_id, "i002", "w2", budget=1001, started_seconds_ago=2.4)

    def status(run_id: str) -> str:
        return view(run_id)["window"]["review_status"]

    advance(server, clock, project_id, OPENS - 1)
    assert status("run-0001") == "pending"
    advance(server, clock, project_id, OPENS)
    assert status("run-0001") == "open"
    advance(server, clock, project_id, 1361)
    run = view("run-0001")
    assert run == {
        "run_id": "run-0001",
        "project_id": project_id,
        "intent_id": "i001",
        "intent": {"description": "investigate 1", "from": ["origin"]},
        "worker": "w1",
        "cli_type": "codex",
        "execution": "container",
        "session_id": None,
        "locator": {"container": f"cairn-dispatch-{project_id}", "prompt_marker": None},
        "budget_seconds": BUDGET,
        "started_at": at(0),
        "deadline_at": at(BUDGET),
        "version": 1,
        "slices_used": 0,
        "state": "running",
        "elapsed_seconds": 1361,
        "remaining_seconds": 439,
        "window": {
            "number": 1,
            "start_at": at(0),
            "end_at": at(BUDGET),
            "seconds": BUDGET,
            "review_opens_at": at(OPENS),
            "review_closes_at": at(CLOSES),
            "review_accept_until": at(ACCEPT_UNTIL),
            "reviewable": True,
            "review_status": "open",
        },
        "reviews": [],
        "registered_at": at(0),
        "updated_at": at(0),
        "ended_at": None,
        "end_reason": None,
        "last_alive_at": None,
        "outcome": None,
        "fact_id": None,
    }
    assert RunView.model_validate(run).model_dump(by_alias=True) == run
    # started_seconds_ago moves the start; the window is floored: 75% of 1001 s is 750.75 s, 80% is 800.8 s.
    window = view("run-0002")["window"]
    assert (window["start_at"], window["review_opens_at"], window["review_closes_at"]) == (at(-2), at(748), at(798))
    assert (window["end_at"], window["review_accept_until"]) == (at(999), at(989))

    advance(server, clock, project_id, CLOSES)
    assert status("run-0001") == "late"
    advance(server, clock, project_id, ACCEPT_UNTIL)
    assert status("run-0001") == "closed"  # until the next sweep records not_reviewed
    poll()
    assert status("run-0001") == "not_reviewed"
    advance(server, clock, project_id, BUDGET + 1)
    run = view("run-0001")
    assert (run["state"], run["elapsed_seconds"], run["remaining_seconds"]) == ("over_budget", BUDGET + 1, 0)
    detail = view("run-0001", full_reviews=True)
    assert RunDetailView.model_validate(detail).model_dump(by_alias=True) == detail
    assert detail["reviews"] == [
        {
            **not_reviewed_detail(),
            "review_id": "server",
            "source": "server",
            "issue_id": issue_rows("run-0001")[0]["id"],
            "created_at": at(ACCEPT_UNTIL),
            "digest": None,
            "digest_sha256": None,
            "raw_output": None,
        }
    ]


def test_run_end_reason_applies_its_rules_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    now = START + timedelta(seconds=1000)
    run = {"worker": "w1", "state": "running"}
    active = {"status": "active"}

    def intent(age: float | None = 10, **changes: Any) -> dict[str, Any]:
        heartbeat_at = None if age is None else at(1000 - age)
        return {"to_fact_id": None, "concluded_at": None, "worker": "w1", "last_heartbeat_at": heartbeat_at, **changes}

    def reason(
        intent_row: dict[str, Any],
        project: dict[str, Any] | None = None,
        timeout: int = 15,
        shortest: int | None = None,
    ) -> Any:
        return repair.run_end_reason(run, intent_row, project or active, now, timeout, shortest_intent_timeout=shortest)

    assert (repair.repair_lapse_seconds(5), repair.repair_lapse_seconds(15), repair.repair_lapse_seconds(30)) == (
        60,
        60,
        120,
    )
    assert reason(intent()) is None
    # A concluded intent ends the run first, whatever else is true.
    concluded = intent(to_fact_id="f001", concluded_at=at(995), worker="w2")
    assert reason(concluded, {"status": "stopped"}) == ("concluded", at(995))
    assert reason(intent(worker="w2"), {"status": "completed"}) == ("project_inactive", at(990))
    assert reason(intent(worker=None), {"status": "stopped"}) == ("project_inactive", at(990))
    assert reason(intent(worker="w2")) == ("claim_taken", None)
    # A cleared claim: released while the heartbeat is younger than intent_timeout, else a gap, until the lapse.
    # Exactly intent_timeout old is a gap: today's expire_workers can clear a claim at that age (julianday rounding).
    assert reason(intent(14, worker=None)) == ("released", at(986))
    assert reason(intent(15, worker=None)) is None
    assert reason(intent(16, worker=None)) is None
    assert reason(intent(60, worker=None)) is None
    assert reason(intent(61, worker=None)) == ("claim_lost", at(939))
    assert reason(intent(60)) is None
    assert reason(intent(61)) == ("claim_lost", at(939))
    assert reason(intent(None, worker=None)) == ("claim_lost", None)
    assert reason(intent(None)) == ("claim_lost", None)
    # The release and the lapse follow intent_timeout.
    assert reason(intent(29, worker=None), timeout=30) == ("released", at(971))
    assert reason(intent(30, worker=None), timeout=30) is None
    assert reason(intent(120), timeout=30) is None
    assert reason(intent(121), timeout=30) == ("claim_lost", at(879))
    # A cleared claim is released only while its heartbeat is younger than every intent_timeout in force since then:
    # with 15 still in force after the heartbeat and 30 today, a claim cleared 16 s after it was cleared by
    # expire_workers. Today's value still bounds the release, and the lapse follows today's value only.
    assert reason(intent(14, worker=None), timeout=30, shortest=15) == ("released", at(986))
    assert reason(intent(15, worker=None), timeout=30, shortest=15) is None
    assert reason(intent(16, worker=None), timeout=30, shortest=15) is None
    assert reason(intent(29, worker=None), timeout=30, shortest=30) == ("released", at(971))
    assert reason(intent(16, worker=None), timeout=15, shortest=30) is None
    assert reason(intent(120, worker=None), timeout=30, shortest=15) is None
    assert reason(intent(121, worker=None), timeout=30, shortest=15) == ("claim_lost", at(879))
    # The rule is written over the run's state: a state that Phase 4 exempts never lapses.
    monkeypatch.setattr(repair, "LAPSE_EXEMPT_STATES", frozenset({"detached"}))
    run["state"] = "detached"
    assert reason(intent(10_000)) is None
    assert reason(intent(10_000, worker=None)) is None
    assert reason(intent(worker="w2")) == ("claim_taken", None)
    assert reason(concluded) == ("concluded", at(995))


def test_the_sweeps_keep_the_intent_timeouts_they_read() -> None:
    def since(seconds: float) -> datetime:
        return START + timedelta(seconds=seconds)

    timeouts = repair._IntentTimeouts()
    assert timeouts.shortest_since(since(0)) is None
    # Only changes are kept: the same value read again adds nothing.
    for seconds in (0, 5, 10, 600):
        timeouts.record(since(seconds), 15)
    assert timeouts._changes == [(since(0), 15)]
    assert (timeouts.shortest_since(since(-1)), timeouts.shortest_since(since(600))) == (15, 15)
    # Raised to 30, first read at 10:10:17. A heartbeat before that read, or in its second, may have met 15 after it;
    # from the next second on, only 30 is in force.
    timeouts.record(since(617), 30)
    assert [timeouts.shortest_since(since(seconds)) for seconds in (-1, 600, 616, 617, 618)] == [15, 15, 15, 15, 30]
    # Lowered to 10, first read at 10:15:00: every heartbeat before that read meets 10 too.
    timeouts.record(since(900), 10)
    assert [timeouts.shortest_since(since(seconds)) for seconds in (0, 617, 899, 900, 1000)] == [10, 10, 10, 10, 10]
    timeouts.reset()
    assert timeouts.shortest_since(since(0)) is None

    # Two values read in one second: a heartbeat in that second meets both, a later one only the value read last,
    # which later reads of the same value do not add again.
    timeouts.record(since(617), 15)
    timeouts.record(since(617), 30)
    timeouts.record(since(622), 30)
    assert timeouts._changes == [(since(617), 15), (since(617), 30)]
    assert (timeouts.shortest_since(since(617)), timeouts.shortest_since(since(618))) == (15, 30)

    # At most INTENT_TIMEOUT_CHANGES_KEPT changes: past that, the two oldest merge into one with the smaller value,
    # which can only make the release rule stricter.
    bounded = repair._IntentTimeouts()
    bounded.record(since(0), 5)
    for seconds in range(1, 100):
        bounded.record(since(seconds), 20 + seconds % 2)
    assert len(bounded._changes) == repair.INTENT_TIMEOUT_CHANGES_KEPT == 32
    assert bounded._changes[:2] == [(since(0), 5), (since(69), 21)]
    assert [bounded.shortest_since(since(seconds)) for seconds in (0, 30, 98, 99, 100)] == [5, 5, 20, 20, 21]

    # A NULL or unreadable heartbeat has an infinite age, so no value applies to it.
    assert repair.shortest_intent_timeout_since(None) is None
    assert repair.shortest_intent_timeout_since("2026-11-14 10:00:00") is None


def test_clean_text_folds_whitespace_and_drops_control_and_format_characters() -> None:
    assert repair.clean_text("Steady\nprogress  \u202eevil\u200b!") == "Steady progress evil!"
    assert repair.clean_text("a \u200b b") == "a b"
    assert repair.clean_text("line one\r\n\tline two\u2028three\u00a0four") == "line one line two three four"
    assert repair.clean_text("\x1b[31mred\x00\x7f \ufeffend\u2066") == "[31mred end"
    assert repair.clean_text(" \t\u200b\u200d\n ") == ""
    assert repair.clean_text(None) == "" and repair.clean_text("") == ""
    assert repair.clean_text("kept: é ü 中文 \u2010 ✓") == "kept: é ü 中文 \u2010 ✓"


def test_a_worker_name_is_logged_only_if_it_matches_the_id_rule() -> None:
    # The register model takes any worker name of 1 to 128 characters: a worker name has no rule of its own.
    forged = "w1\nrepair run ended run=forged project=proj_001 intent=i001 reason=dispatcher outcome=success\x1b[2K"
    body = RunRegisterRequest.model_validate(
        {
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": forged,
            "cli_type": "codex",
            "execution": "local",
            "budget_seconds": 1800,
        }
    )
    assert body.worker == forged
    # So a log line shows a name only if it fully matches the id rule, else "-".
    for name in ("w1", "codex-1", "claude_code.2", "a" * 128, "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"):
        assert repair.loggable_name(name) == name
    hidden = [forged, "w1\n", "w1\r", "w1\x1b[31m", "w1\u2028x", "codex 1", "w1 project=proj_999", "-w1", ".w1"]
    hidden += ["", "a" * 129, "\u5de5\u4eba", "w1/..", None, 7]
    for name in hidden:
        assert repair.loggable_name(name) == "-", name


def _errors(model: type[Any], body: dict[str, Any]) -> list[tuple[Any, ...]]:
    try:
        model.model_validate(body)
    except ValidationError as error:
        return [item["loc"] for item in error.errors()]
    return []


def test_request_models_follow_the_field_rules() -> None:
    register_body = {
        "project_id": "proj_001",
        "intent_id": "i001",
        "worker": "w1",
        "cli_type": "codex",
        "execution": "local",
        "budget_seconds": 1800,
        "newer_field": "ignored",
    }
    parsed = RunRegisterRequest.model_validate(register_body)
    assert (parsed.session_id, parsed.started_seconds_ago, parsed.locator) == (None, 0.0, {})
    refused = [
        {"session_id": "0199a1b2-6a3f\n"},
        {"session_id": "a..b"},
        {"session_id": "-abc"},
        {"session_id": "a" * 129},
        {"started_seconds_ago": float("inf")},
        {"started_seconds_ago": float("nan")},
        {"started_seconds_ago": -1},
        {"budget_seconds": 0},
        {"budget_seconds": 604801},
        {"budget_seconds": float("inf")},
        {"cli_type": "Codex"},
        {"execution": "remote"},
        {"worker": ""},
        {"project_id": "p" * 129},
        {"locator": {str(key): None for key in range(9)}},
        {"locator": {"k" * 65: None}},
        {"locator": {"container": "c" * 1025}},
        {"locator": None},
    ]
    for change in refused:
        assert _errors(RunRegisterRequest, {**register_body, **change}), change
    assert not _errors(RunRegisterRequest, {**register_body, "session_id": "a" * 128, "started_seconds_ago": 2000})

    assert _errors(RunEndRequest, {"outcome": "Success"}) and _errors(RunEndRequest, {"outcome": "a" * 33})
    assert _errors(RunEndRequest, {"exited_seconds_ago": 604801}) and _errors(RunEndRequest, {"returncode": 1.5})
    assert _errors(RunEndRequest, {"exited_seconds_ago": float("inf")})
    assert not _errors(RunEndRequest, {"outcome": "not_a_known_outcome", "timed_out": None, "applied": True})

    review_body = {
        "review_id": "rv-8b1e2f30a9c4d5e6f7a8b9c0",
        "window": 1,
        "decision": "continue",
        "reason": "steady progress",
        "digest_elapsed_seconds": 1352,
    }
    assert not _errors(ReviewRequest, review_body)
    assert not _errors(ReviewRequest, {**review_body, "fallback": "model_timeout", "applied": True})
    for review_id in ("a" * 8, "a" * 64, "rv_1.2:3-4"):
        assert not _errors(ReviewRequest, {**review_body, "review_id": review_id}), review_id
    refused = [
        {"digest_elapsed_seconds": None},
        {"digest_elapsed_seconds": float("inf")},
        {"digest_elapsed_seconds": 604801},
        {"digest_elapsed_seconds": -1},
        {"review_id": "abcdefgh\n"},
        {"review_id": "short"},
        {"review_id": "a" * 7},
        {"review_id": "a" * 65},
        {"review_id": "rv 12345"},
        {"window": 0},
        {"window": 1001},
        {"decision": "stop"},
        {"reason": ""},
        {"reason": " \u200b\n\u202e "},
        {"reason": "r" * 2001},
        {"evidence": ["e"] * 11},
        {"evidence": ["e" * 501]},
        {"evidence": [""]},
        {"next_step": "n" * 501},
        {"fallback": "model_timeout", "decision": "extend"},
        {"fallback": "something_else"},
        {"transcript_cut_seconds": -1},
        {"transcript_cut_seconds": float("nan")},
        {"model": "m" * 101},
        {"digest": "d" * 65537},
        {"raw_output": "o" * 16385},
    ]
    for change in refused:
        assert _errors(ReviewRequest, {**review_body, **change}), change
    missing = {key: value for key, value in review_body.items() if key != "digest_elapsed_seconds"}
    assert _errors(ReviewRequest, missing) == [("digest_elapsed_seconds",)]

    assert [valid_run_id(value) for value in ("abcdefgh", "a" * 64, "5f2c9a1e7b3d4c0a9e8d7c6b5a493827")] == [True] * 3
    assert [valid_run_id(value) for value in ("abcdefgh\n", "abcdefg", "a" * 65, "-abcdefgh", "abc/defgh")] == [
        False
    ] * 5


def test_the_sweep_reads_only_open_or_unsettled_runs_and_has_no_orphan_pass(
    server: TestClient, clock: Clock, tmp_path
) -> None:
    with db.get_conn() as conn:
        plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + repair.ATTENTION_SQL)]
    assert "SCAN r USING INDEX repair_runs_attention" in plan, plan

    project_id = new_project(server, intents=2)
    heartbeat(server, project_id)
    register("run-0001", project_id)
    advance(server, clock, project_id, 60)
    # A delete run outside Cairn with foreign keys off leaves the run without its intent.
    with closing(sqlite3.connect(str(tmp_path / "cairn.db"))) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM intents WHERE id = 'i001' AND project_id = ?", (project_id,))
        conn.commit()
    orphan = run_row("run-0001")
    # Such a row is never read: no sweep ends or removes it, and it is not found, as a deleted run is not.
    clock.to(1000)
    sweep()
    poll()
    assert run_row("run-0001") == orphan
    with repair.immediate_transaction() as conn:
        assert repair.find_run(conn, "run-0001") is None
        body = RunEndRequest(outcome="success")
        assert repair.end_run(conn, "run-0001", body, now=repair.server_now(), agent_seen_before=None) is None
    assert run_row("run-0001") == orphan


def test_a_late_end_on_a_run_the_sweep_ended_applies_the_not_reviewed_rule_once_more(
    server: TestClient, clock: Clock
) -> None:
    project_id = new_project(server, intents=2)
    claims = (("i001", "w1"), ("i002", "w2"))
    for intent_id, worker in claims:
        heartbeat(server, project_id, intent_id, worker)
    register("run-0001", project_id, "i001", "w1")
    register("run-0002", project_id, "i002", "w2")
    advance(server, clock, project_id, 1450, claims)
    # Both claims are taken after the windows closed: the sweep ends both runs at an unknown time, records nothing.
    clock.to(1466)
    heartbeat(server, project_id, "i001", "w8")
    heartbeat(server, project_id, "i002", "w9")
    sweep()
    for run_id in ("run-0001", "run-0002"):
        row = run_row(run_id)
        assert (row["end_reason"], row["last_alive_at"], row["settled_window"]) == ("claim_taken", None, 1)
    assert review_rows() == []

    # run-0001's late /end reports an exit after its window closed: it is flagged now, once.
    clock.to(1480)
    exit_end("run-0001", exited_seconds_ago=2.0)
    issues = issue_rows("run-0001")
    assert [issue["detail"] for issue in issues] == [
        not_reviewed_detail(elapsed_seconds=1478, run_ended=True, run_ended_at=at(1478), end_reason="claim_taken")
    ]
    row = run_row("run-0001")
    assert (row["ended_at"], row["end_reason"], row["last_alive_at"]) == (at(1466), "claim_taken", at(1478))
    # A retried exit /end fills only empty fields: no second correction and no second record.
    clock.to(1485)
    exit_end("run-0001", exited_seconds_ago=0.0)
    assert run_row("run-0001")["last_alive_at"] == at(1478)
    outcome_end("run-0001")
    assert len(review_rows("run-0001")) == 1
    assert issue_rows("run-0001")[0]["detail"] == {**issues[0]["detail"], "outcome": "success"}

    # run-0002's late /end reports an exit before its window closed: nothing to flag.
    clock.to(1490)
    exit_end("run-0002", exited_seconds_ago=60.0)
    assert run_row("run-0002")["last_alive_at"] == at(1430)
    assert review_rows("run-0002") == [] and issue_rows("run-0002") == []


def test_end_clamps_the_exit_time_and_fills_the_fact_once_the_intent_has_concluded(
    server: TestClient, clock: Clock
) -> None:
    project_id = new_project(server, intents=4)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"), ("i004", "w4"))
    for number, (intent_id, worker) in enumerate(claims, start=1):
        heartbeat(server, project_id, intent_id, worker)
        register(f"run-000{number}", project_id, intent_id, worker)

    def conclude(intent_id: str, worker: str) -> str:
        response = server.post(
            f"/projects/{project_id}/intents/{intent_id}/conclude", json={"worker": worker, "description": "an answer"}
        )
        assert response.status_code == 200, response.text
        return response.json()["fact"]["id"]

    def ending(run_id: str) -> tuple[Any, ...]:
        row = run_row(run_id)
        return row["end_reason"], row["ended_at"], row["last_alive_at"], row["fact_id"], row["outcome"]

    # run-0001's process exits at once. Both times are whole seconds, rounded at each end, so its reported exit can
    # fall before its start: it was last alive no earlier than it started.
    clock.to(1)
    exit_end("run-0001", exited_seconds_ago=2.0)
    assert ending("run-0001") == ("dispatcher", at(1), at(0), None, None)

    # run-0002's process exits at 598 s, and its first /end is lost. Its intent concludes at 600 s, and the retried
    # /end arrives with no repair call in between: the /end ends the run itself and takes the intent's fact.
    advance(server, clock, project_id, 600, claims[1:])
    fact_2 = conclude("i002", "w2")
    clock.to(604)
    exit_end("run-0002", exited_seconds_ago=6.0)
    assert ending("run-0002") == ("dispatcher", at(604), at(598), fact_2, None)

    # run-0003 and run-0004 time out: their exit /end comes first, then today's conclude fallback makes a fact.
    advance(server, clock, project_id, BUDGET, claims[2:])
    clock.to(BUDGET + 1)
    exit_end("run-0003", exited_seconds_ago=1.0, timed_out=True, returncode=124)
    exit_end("run-0004", exited_seconds_ago=1.0, timed_out=True, returncode=124)
    clock.to(BUDGET + 10)
    fact_3 = conclude("i003", "w3")
    fact_4 = conclude("i004", "w4")
    # run-0004's exit /end was applied but its answer was lost: the retry brings nothing new but the fact, and fills
    # it. The sweeps leave run-0003's fact to its own /end.
    clock.to(BUDGET + 12)
    exit_end("run-0004", exited_seconds_ago=12.0, timed_out=True, returncode=124)
    assert ending("run-0004") == ("dispatcher", at(BUDGET + 1), at(BUDGET), fact_4, None)
    assert ending("run-0003") == ("dispatcher", at(BUDGET + 1), at(BUDGET), None, None)
    # The outcome /end: it records run-0003's fact with its outcome, and keeps run-0004's.
    clock.to(BUDGET + 20)
    outcome_end("run-0003")
    outcome_end("run-0004", "failed")
    assert ending("run-0003") == ("dispatcher", at(BUDGET + 1), at(BUDGET), fact_3, "success")
    assert ending("run-0004") == ("dispatcher", at(BUDGET + 1), at(BUDGET), fact_4, "failed")


def test_repair_lines_are_logged_only_once_their_transaction_commits(
    server: TestClient, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """A transaction that rolls back, such as a request that fails or is refused inside it, writes nothing and logs
    nothing; the call that then makes the same record logs it once."""
    caplog.set_level(logging.INFO, logger=REPAIR_LOGGER)
    project_id = new_project(server, intents=3)
    claims = (("i001", "w1"), ("i002", "w2"), ("i003", "w3"))
    for number, (intent_id, worker) in enumerate(claims, start=1):
        heartbeat(server, project_id, intent_id, worker)
        register(f"run-000{number}", project_id, intent_id, worker)
    caplog.clear()  # the switch line of the project's create

    def lines() -> list[str]:
        return [record.getMessage() for record in caplog.records if record.name == REPAIR_LOGGER]

    # run-0001: a review post that fails after the review was stored.
    advance(server, clock, project_id, 1352, claims)
    with pytest.raises(RuntimeError), repair.immediate_transaction() as conn:
        record_review(conn, "run-0001")
        raise RuntimeError("the post fails after the review was stored")
    assert (review_rows(), issue_rows(), lines()) == ([], [], [])
    review("run-0001")
    reviewed = f"repair review recorded run=run-0001 project={project_id} intent=i001 window=1 decision=extend"
    assert lines() == [f"{reviewed} fallback=None mode=shadow applied=false"]

    # run-0002: an exit /end that fails after it ended the run, in its sweep say.
    advance(server, clock, project_id, 1400, claims[1:])
    exit_report = RunEndRequest(timed_out=False, cancelled=False, returncode=0, exited_seconds_ago=0.0)
    with pytest.raises(RuntimeError), repair.immediate_transaction() as conn:
        ended = repair.end_run(conn, "run-0002", exit_report, now=repair.server_now(), agent_seen_before=None)
        assert ended is not None and ended["end_reason"] == "dispatcher"
        raise RuntimeError("the /end fails after the ending")
    assert run_row("run-0002")["ended_at"] is None and len(lines()) == 1
    exit_end("run-0002")
    assert lines()[1:] == [
        f"repair run ended run=run-0002 project={project_id} intent=i002 reason=dispatcher outcome=None"
    ]

    # run-0003: at its accept limit, a review post's sweep records "not reviewed", and the post is then refused
    # inside the same transaction.
    advance(server, clock, project_id, ACCEPT_UNTIL, claims[2:])
    with pytest.raises(HTTPException), repair.immediate_transaction() as conn:
        repair.sweep(conn, repair.server_now(), repair.agent_last_seen())
        assert conn.execute("SELECT count(*) FROM repair_reviews WHERE run_id = 'run-0003'").fetchone()[0] == 1
        raise HTTPException(409, {"code": "window_closed", "message": "refused inside the transaction"})
    assert (review_rows("run-0003"), issue_rows("run-0003"), len(lines())) == ([], [], 2)
    # The agent's next poll records it again, and the record is logged once.
    poll()
    assert [(row["source"], row["decision"]) for row in review_rows("run-0003")] == [("server", "not_reviewed")]
    assert len(issue_rows("run-0003")) == 1
    assert lines()[2:] == [
        f"repair window not reviewed run=run-0003 project={project_id} intent=i003 window=1 why=agent_not_seen"
    ]

    # Outside a repair transaction a line is logged at once.
    repair.log_after_commit("repair test line value=%s", 7)
    assert lines()[3:] == ["repair test line value=7"]
