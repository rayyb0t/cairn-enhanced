"""Repair layer (shadow mode): the server tests of spec section 16 (the SV ids).

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
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectDetail, ProjectMeta, ProjectReason, ProjectSummary
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


@pytest.fixture
def server(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


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
