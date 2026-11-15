from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
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
    reason_last_heartbeat_at TEXT,
    repair_enabled INTEGER NOT NULL DEFAULT 0
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

-- Repair layer (shadow mode). A run is deleted with its intent, and a review with its run, inside the
-- deleting statement: today's project delete and reopen cascade into these tables, and nothing else does.
CREATE TABLE IF NOT EXISTS repair_runs (
    run_id TEXT PRIMARY KEY,                    -- dispatcher-generated (uuid4 hex), one per execute process
    project_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    worker TEXT NOT NULL,                       -- worker name, the server's claim key
    cli_type TEXT NOT NULL,                     -- claudecode | codex | pi | mock
    execution TEXT NOT NULL,                    -- container | local
    session_id TEXT,                            -- NULL until known
    locator_json TEXT NOT NULL DEFAULT '{}',    -- opaque: container or workdir, prompt marker, dispatcher start time
    budget_seconds INTEGER NOT NULL,            -- tasks.explore.timeout at run start
    started_at TEXT NOT NULL,                   -- server clock: receive time minus started_seconds_ago
    deadline_at TEXT NOT NULL,                  -- started_at + budget_seconds; never moves in Phase 2
    version INTEGER NOT NULL DEFAULT 1,         -- always 1 in Phase 2
    slices_used INTEGER NOT NULL DEFAULT 0,     -- always 0 in Phase 2
    state TEXT NOT NULL DEFAULT 'running',      -- running | ended (Phase 3/4 add sealed, detached, stop_requested)
    reviewable INTEGER NOT NULL DEFAULT 1,      -- 0 if the window is under 350 s or the CLI has no transcript
    settled_window INTEGER NOT NULL DEFAULT 0,  -- highest window whose review question is settled
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,                              -- when the server marked the run ended
    end_reason TEXT,                            -- dispatcher | superseded | concluded | released | claim_lost | claim_taken | project_inactive
    last_alive_at TEXT,                         -- last moment the run is known to have been alive; NULL if unknown
    outcome TEXT,                               -- return value of run_explore_task, from the second /end
    outcome_json TEXT,                          -- {"timed_out", "cancelled", "returncode"} from the first /end
    fact_id TEXT,                               -- the fact the intent produced, once known
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS repair_runs_open_key ON repair_runs (project_id, intent_id, worker) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS repair_runs_by_intent ON repair_runs (project_id, intent_id);
CREATE INDEX IF NOT EXISTS repair_runs_attention ON repair_runs (project_id) WHERE ended_at IS NULL OR settled_window = 0;

CREATE TABLE IF NOT EXISTS repair_reviews (
    run_id TEXT NOT NULL REFERENCES repair_runs(run_id) ON DELETE CASCADE,
    window_no INTEGER NOT NULL,                 -- "window" is an SQL keyword
    review_id TEXT NOT NULL,                    -- the agent's idempotency key; 'server' for not_reviewed
    source TEXT NOT NULL,                       -- agent | server
    decision TEXT NOT NULL,                     -- continue | extend | detach | not_reviewed
    issue_id INTEGER,                           -- the intent_issues row shown in the UI
    detail_json TEXT NOT NULL,                  -- the full stored review
    digest TEXT,                                -- what the model saw, at most 65536 characters
    digest_sha256 TEXT,
    raw_output TEXT,                            -- the model's first text block, at most 16384 characters
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, window_no)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )
    if "repair_enabled" not in columns:
        # Every existing project starts with its Repair switch off.
        conn.execute("ALTER TABLE projects ADD COLUMN repair_enabled INTEGER NOT NULL DEFAULT 0")


@contextmanager
def get_conn(timeout: float = 5.0) -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
