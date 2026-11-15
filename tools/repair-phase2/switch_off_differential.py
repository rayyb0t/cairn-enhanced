#!/usr/bin/env python3
"""Switch-off differential: with every Repair switch off, the server's existing endpoints behave as before.

Replays one script of calls to the existing endpoints against the base commit (the last commit before the repair
layer) and against this tree, each in its own process with the same fake clock, and compares:

  responses  (status, body) of every call, after removing "repair_enabled": false from every project object.
             The field must be there, false, on every project object of this tree's bodies; everything else is
             compared byte for byte. A call that fails with an exception is recorded as the 500 the server
             answers, and compared like any other response, so the calls after it are compared too.
  rows       every row of every table the base has (projects on the base's columns), with this tree's extra
             column projects.repair_enabled 0 on every row and its repair tables empty.
  sql        every statement the endpoints run, as SQLite's trace callback reports it, apart from the one
             expected difference, the cascade of repair_runs' foreign key (spec 4.2): SQLite reports the
             deleting statement again at the start of each cascade program, so `DELETE FROM projects WHERE id = ...`
             appears once more for each intent it deletes (the repair_runs program) and once more for each run it
             deletes (the repair_reviews program), and reopen's `DELETE FROM intents ...` once more. Exactly those
             repetitions are removed, counted from the rows before each call; every other line is compared as is.

It also checks the upgrade path once: this tree's server, started on a copy of the database file the base run
wrote, answers GET /projects and GET /projects/{id} for every project with the base server's bodies on the
original file plus "repair_enabled": false, runs the same SQL for them, and leaves the base's rows as they were.
The projects to read come from the file, so both servers get the same calls whatever their listings answer.

The base tree comes from `git archive <base-rev> cairn/src` in a temporary folder, so neither the repository
nor its worktrees change. The script and its clock are in cairn/tests/test_repair_server_script.py, which the server
tests SV43, SV44 and SV46 replay too; it imports only the standard library, and every run reads it from this
checkout. The page (GET /) is not in the script: the UI changes it on purpose, and UI1 compares it. Neither are the
repair endpoints, which the base does not have.

Usage (from the repository root, with the interpreter that runs the cairn tests):
  python tools/repair-phase2/switch_off_differential.py [--base-rev REV] [--keep DIR] [--json FILE]

Exit status: 0 identical, 1 different (an error response such as a 500 included), 2 could not run: the base tree,
a database copy or one of the four runs failed outside the calls it compares.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

TOOL = Path(__file__).resolve()
REPO = TOOL.parents[2]
DEFAULT_BASE_REV = "279d2df"
# The existing-endpoint script lives with the cairn tests, so that the cairn suite runs from cairn/ alone.
SCRIPT_FILE = REPO / "cairn" / "tests" / "test_repair_server_script.py"
SCRIPT_MODULE = "test_repair_server_script"
READ_CLOCK_START = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
CHILD_TIMEOUT_SECONDS = 600

SWITCH_COLUMN = "repair_enabled"
SWITCH_COLUMN_INFO = ["INTEGER", 1, "0", 0]  # (type, notnull, dflt_value, pk)
REPAIR_TABLES = ("repair_runs", "repair_reviews")
PROJECT_KEYS = frozenset({"id", "title", "status", "bootstrap_enabled", "created_at"})
SWITCH_FALSE = b',"repair_enabled":false'
CASCADE_PROJECT_DELETE = re.compile(r"DELETE FROM projects WHERE id = '([^']*)'")
CASCADE_INTENT_DELETE = re.compile(r"DELETE FROM intents WHERE id = '([^']*)' AND project_id = '([^']*)'")
MAX_SHOWN = 12


def _load_script() -> ModuleType:
    """The existing-endpoint script module, read from cairn/tests (or the one already imported from there)."""
    loaded = sys.modules.get(SCRIPT_MODULE)
    if loaded is not None and Path(getattr(loaded, "__file__", None) or "").resolve() == SCRIPT_FILE:
        return loaded
    spec = importlib.util.spec_from_file_location(SCRIPT_MODULE, SCRIPT_FILE)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the existing-endpoint script {SCRIPT_FILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[SCRIPT_MODULE] = module  # its dataclasses look their module up while they are built
    spec.loader.exec_module(module)
    return module


_SCRIPT = _load_script()
Call = _SCRIPT.Call
Advance = _SCRIPT.Advance
SCRIPT: list[Any] = _SCRIPT.SCRIPT
FakeClock = _SCRIPT.FakeClock
SCRIPT_CLOCK_START: datetime = _SCRIPT.SCRIPT_CLOCK_START


# --------------------------------------------------------------------------- child process (one tree)


_CONNECT = sqlite3.connect


class Tracer:
    """Every statement run on a connection that sqlite3.connect opens while installed."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _connect(self, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = _CONNECT(*args, **kwargs)
        conn.set_trace_callback(self.lines.append)
        return conn

    @contextmanager
    def installed(self) -> Iterator[Tracer]:
        sqlite3.connect = self._connect
        try:
            yield self
        finally:
            sqlite3.connect = _CONNECT


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _census(db_path: Path) -> dict[str, Any]:
    """The rows a delete in the next call can cascade from: intents by project, and repair runs by intent."""
    conn = _CONNECT(str(db_path))
    try:
        intents: dict[str, list[str]] = {}
        for project_id, intent_id in conn.execute("SELECT project_id, id FROM intents ORDER BY project_id, id"):
            intents.setdefault(project_id, []).append(intent_id)
        runs: dict[str, dict[str, int]] = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'repair_runs'").fetchone():
            for project_id, intent_id, count in conn.execute(
                "SELECT project_id, intent_id, COUNT(*) FROM repair_runs GROUP BY project_id, intent_id"
            ):
                runs.setdefault(project_id, {})[intent_id] = count
        return {"intents": intents, "runs": runs}
    finally:
        conn.close()


def _project_ids(db_path: Path) -> list[str]:
    """Every project in the file, in the order GET /projects lists them (ORDER BY created_at)."""
    conn = _CONNECT(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT id FROM projects ORDER BY created_at, rowid")]
    finally:
        conn.close()


def _dump(db_path: Path) -> dict[str, Any]:
    conn = _CONNECT(str(db_path))
    try:
        schema = [
            list(row) for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
        ]
        tables = {}
        for kind, name, _table, _sql in schema:
            if kind != "table":
                continue
            info = [list(row) for row in conn.execute(f"PRAGMA table_info({_quote(name)})")]
            keys = [list(row) for row in conn.execute(f"PRAGMA foreign_key_list({_quote(name)})")]
            # Names come from sqlite_master of the database this run wrote.
            rows = [list(row) for row in conn.execute(f"SELECT rowid, * FROM {_quote(name)} ORDER BY rowid")]  # noqa: S608
            tables[name] = {"columns": [column[1] for column in info], "info": info, "keys": keys, "rows": rows}
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"schema": schema, "tables": tables}
    finally:
        conn.close()


def _send(client: Any, call: Call) -> Any:
    if call.raw is not None:
        return client.request(call.method, call.path, content=call.raw, headers={"Content-Type": "application/json"})
    return client.request(call.method, call.path, json=call.body)


def _record(call: Call, response: Any, sql: list[str], census: dict[str, Any] | None) -> dict[str, Any]:
    record = {
        "method": call.method,
        "path": call.path,
        "status": response.status_code,
        "body": base64.b64encode(response.content).decode("ascii"),
        "sql": sql,
    }
    if census is not None:
        record["census"] = census
    return record


def run_child(mode: str, src: Path, db_path: Path, out: Path) -> None:
    os.environ["TZ"] = "UTC"
    time.tzset()
    import cairn

    loaded = Path(cairn.__file__ or "").resolve()
    if not loaded.is_relative_to(src.resolve()):
        raise SystemExit(f"cairn was imported from {loaded}, not from {src}")

    from fastapi.testclient import TestClient

    from cairn.server import db, services
    from cairn.server.app import app

    clock = FakeClock(SCRIPT_CLOCK_START if mode == "script" else READ_CLOCK_START)
    original = services.utcnow
    for name, module in list(sys.modules.items()):
        if name.startswith("cairn.server") and getattr(module, "utcnow", None) is original:
            module.utcnow = clock

    db.DEFAULT_DB = db_path  # what the app's lifespan configures, as `cairn serve` does
    tracer = Tracer()
    calls: list[dict[str, Any]] = []
    # raise_server_exceptions=False: a call that fails with an exception gets the 500 that `cairn serve` would
    # answer, recorded and compared like any other response. An error in this tree is a difference to report, and
    # the calls after it still run, so it is never mistaken for a run that could not be made.
    if mode == "script":
        db.configure(db_path)
        with TestClient(app, raise_server_exceptions=False) as client, tracer.installed():
            for step in SCRIPT:
                if isinstance(step, Advance):
                    clock.advance(step.seconds)
                    calls.append({"advance": step.seconds})
                    continue
                census = _census(db_path)
                start = len(tracer.lines)
                response = _send(client, step)
                calls.append(_record(step, response, tracer.lines[start:], census))
    else:
        # A server started on an existing file: the lifespan runs db.configure, and with it the migration. The
        # projects to read are taken from the file before it starts, not from the listing it answers, so both runs
        # make the same calls even when one listing fails.
        reads = [Call("GET", "/projects"), *(Call("GET", f"/projects/{pid}") for pid in _project_ids(db_path))]
        with TestClient(app, raise_server_exceptions=False) as client, tracer.installed():
            for call in reads:
                start = len(tracer.lines)
                calls.append(_record(call, _send(client, call), tracer.lines[start:], None))

    result = {"cairn": str(loaded), "sqlite": sqlite3.sqlite_version, "calls": calls, "database": _dump(db_path)}
    out.write_text(json.dumps(result), encoding="utf-8")


# --------------------------------------------------------------------------- comparison


@dataclass
class Section:
    name: str
    summary: str = ""
    problems: list[str] = field(default_factory=list)

    def problem(self, text: str) -> None:
        self.problems.append(text)


def _strip_switch(value: Any, problems: list[str]) -> int:
    """Remove "repair_enabled": false from every project object, in place; count what was removed."""
    removed = 0
    if isinstance(value, dict):
        if value.keys() >= PROJECT_KEYS:
            if SWITCH_COLUMN not in value:
                problems.append(f"project {value.get('id')!r} has no {SWITCH_COLUMN}")
            elif value[SWITCH_COLUMN] is not False:
                problems.append(f"project {value.get('id')!r} has {SWITCH_COLUMN}={value[SWITCH_COLUMN]!r}")
            else:
                del value[SWITCH_COLUMN]
                removed += 1
        for item in value.values():
            removed += _strip_switch(item, problems)
    elif isinstance(value, list):
        for item in value:
            removed += _strip_switch(item, problems)
    return removed


def _body_difference(base: bytes, tree: bytes) -> tuple[int, list[str]]:
    """How many switch fields came off this tree's body, and what still differs from the base's bytes."""
    try:
        parsed = json.loads(tree)
    except ValueError:
        return 0, [] if tree == base else [_shown_difference(base, tree)]
    problems: list[str] = []
    removed = _strip_switch(parsed, problems)
    if tree.replace(SWITCH_FALSE, b"", removed) != base:
        problems.append(_shown_difference(base, tree))
    return removed, problems


def _shown_difference(base: bytes, tree: bytes) -> str:
    return f"body base={base[:300]!r} tree={tree[:300]!r}"


def _label(index: int, call: dict[str, Any]) -> str:
    return f"call {index} {call['method']} {call['path']}"


def compare_responses(name: str, base: list[dict], tree: list[dict]) -> Section:
    section = Section(name)
    if [("advance" in c, c.get("method"), c.get("path")) for c in base] != [
        ("advance" in c, c.get("method"), c.get("path")) for c in tree
    ]:
        section.problem("the two runs made different calls")
        return section
    removed_total = 0
    calls = 0
    for index, (b, t) in enumerate(zip(base, tree, strict=True)):
        if "advance" in b:
            continue
        calls += 1
        if b["status"] != t["status"]:
            section.problem(f"{_label(index, b)}: status base={b['status']} tree={t['status']}")
        removed, problems = _body_difference(base64.b64decode(b["body"]), base64.b64decode(t["body"]))
        removed_total += removed
        section.problems.extend(f"{_label(index, b)}: {problem}" for problem in problems)
    section.summary = f'{calls} calls; "repair_enabled": false removed from {removed_total} project objects'
    return section


def _expected_repetitions(statement: str, base_census: dict, tree_census: dict) -> int:
    """How many more times this tree's trace repeats a deleting statement: one per intent and run it deletes."""
    match = CASCADE_PROJECT_DELETE.fullmatch(statement)
    if match:
        project_id = match.group(1)
        intents = len(base_census["intents"].get(project_id, []))
        runs = sum(tree_census["runs"].get(project_id, {}).values())
        return intents + runs
    match = CASCADE_INTENT_DELETE.fullmatch(statement)
    if match:
        intent_id, project_id = match.groups()
        intents = 1 if intent_id in base_census["intents"].get(project_id, []) else 0
        runs = tree_census["runs"].get(project_id, {}).get(intent_id, 0)
        return intents + runs
    return 0


def _without_repetitions(lines: list[str], allowance: dict[str, int]) -> list[str]:
    """The lines with up to allowance[line] repetitions of each line removed, each next to another copy of it."""
    left = dict(allowance)
    kept: list[str] = []
    for line in lines:
        if left.get(line, 0) > 0 and kept and kept[-1] == line:
            left[line] -= 1
            continue
        kept.append(line)
    return kept


def compare_sql(name: str, base: list[dict], tree: list[dict], *, cascade: bool) -> Section:
    section = Section(name)
    statements = 0
    removed: dict[str, int] = {}
    for index, (b, t) in enumerate(zip(base, tree, strict=True)):
        if "advance" in b:
            continue
        statements += len(b["sql"])
        allowance: dict[str, int] = {}
        for line in dict.fromkeys(b["sql"]) if cascade else ():
            expected = _expected_repetitions(line, b["census"], t["census"])
            if not expected:
                continue
            at_base, in_tree = b["sql"].count(line), t["sql"].count(line)
            if in_tree != at_base + expected:
                section.problem(
                    f"{_label(index, b)}: {line!r} is reported {in_tree} times, expected {at_base + expected}:"
                    f" {at_base} as at the base and {expected} cascade repetitions (one per intent and run it deletes)"
                )
            # Remove only repetitions the tree has beyond the base's count, so a shortfall is not hidden.
            allowance[line] = max(0, min(expected, in_tree - at_base))
            removed[line] = removed.get(line, 0) + allowance[line]
        kept = _without_repetitions(t["sql"], allowance)
        if kept != b["sql"]:
            diff = list(difflib.unified_diff(b["sql"], kept, "base", "tree", lineterm="", n=1))
            section.problem(f"{_label(index, b)}: SQL differs\n    " + "\n    ".join(diff[:40]))
    shown = ", ".join(f"{line!r} x{count}" for line, count in removed.items() if count) or "none"
    section.summary = f"{statements} statements; cascade repetitions removed: {shown}"
    return section


def compare_rows(name: str, base: dict, tree: dict) -> Section:
    section = Section(name)
    base_tables, tree_tables = base["tables"], tree["tables"]
    rows = 0
    for table, b in base_tables.items():
        t = tree_tables.get(table)
        if t is None:
            section.problem(f"table {table} is missing")
            continue
        columns = b["columns"]
        extra = t["columns"][len(columns) :]
        if t["columns"][: len(columns)] != columns or t["info"][: len(columns)] != b["info"]:
            section.problem(f"table {table}: columns base={b['info']} tree={t['info']}")
            continue
        allowed_extra = [SWITCH_COLUMN] if table == "projects" else []
        if extra and extra != allowed_extra:
            section.problem(f"table {table}: unexpected columns {extra}")
            continue
        if b["keys"] != t["keys"]:
            section.problem(f"table {table}: foreign keys base={b['keys']} tree={t['keys']}")
        projected = [row[: 1 + len(columns)] for row in t["rows"]]
        rows += len(b["rows"])
        if projected != b["rows"]:
            differing = [(x, y) for x, y in zip(b["rows"], projected, strict=False) if x != y][:3]
            section.problem(
                f"table {table}: rows differ (base {len(b['rows'])}, tree {len(projected)}); first: {differing}"
            )
        if extra:
            if t["info"][-1][2:] != SWITCH_COLUMN_INFO:
                section.problem(f"projects.{SWITCH_COLUMN} is {t['info'][-1][2:]}, not {SWITCH_COLUMN_INFO}")
            values = sorted({row[-1] for row in t["rows"]}, key=repr)
            if values not in ([], [0]):
                section.problem(f"projects.{SWITCH_COLUMN} holds {values} with every switch off")
    for table in sorted(set(tree_tables) - set(base_tables)):
        if table not in REPAIR_TABLES:
            section.problem(f"unexpected table {table}")
        elif tree_tables[table]["rows"]:
            section.problem(f"table {table} holds {len(tree_tables[table]['rows'])} rows with every switch off")
    base_objects = {(kind, name): sql for kind, name, _table, sql in base["schema"]}
    tree_objects = {(kind, name): (table, sql) for kind, name, table, sql in tree["schema"]}
    for key, sql in base_objects.items():
        if key == ("table", "projects"):
            continue  # one column more, checked above
        if key not in tree_objects or tree_objects[key][1] != sql:
            section.problem(f"schema object {key} differs: base={sql!r} tree={tree_objects.get(key)!r}")
    for key in sorted(set(tree_objects) - set(base_objects)):
        if tree_objects[key][0] not in REPAIR_TABLES:
            section.problem(f"unexpected schema object {key}")
    extra_tables = sorted(set(tree_tables) - set(base_tables))
    section.summary = (
        f"{len(base_tables)} tables, {rows} rows; tree adds projects.{SWITCH_COLUMN} and {', '.join(extra_tables)}"
        if extra_tables
        else f"{len(base_tables)} tables, {rows} rows"
    )
    return section


# --------------------------------------------------------------------------- driver


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _run_child(python: str, mode: str, src: Path, db_path: Path, work: Path, label: str) -> dict[str, Any]:
    out = work / f"{label}.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_REPAIR_")}
    env.update(
        {
            "PYTHONPATH": str(src),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
            "HOME": str(work / f"home-{label}"),
        }
    )
    (work / f"home-{label}").mkdir(exist_ok=True)
    command = [python, str(TOOL), "--child", mode, "--src", str(src), "--db", str(db_path), "--out", str(out)]
    result = subprocess.run(
        command, env=env, cwd=work, capture_output=True, text=True, timeout=CHILD_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0 or not out.exists():
        raise RuntimeError(f"{label} run failed (exit {result.returncode}):\n{result.stderr[-4000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _base_tree(repo: Path, rev: str, work: Path) -> tuple[Path, str]:
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit, "cairn/src"], capture_output=True, check=True
    ).stdout
    dest = work / "base-tree"
    dest.mkdir()
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest / "cairn" / "src", commit


def _copy_database(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        part = source.with_name(source.name + suffix)
        if part.exists():
            shutil.copy2(part, dest.with_name(dest.name + suffix))


@contextmanager
def _work_dir(keep: Path | None) -> Iterator[Path]:
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        if any(keep.iterdir()):
            raise RuntimeError(f"--keep folder {keep} is not empty")
        yield keep
        return
    with tempfile.TemporaryDirectory(prefix="switch-off-differential-") as name:
        yield Path(name)


def compare(base: dict, tree: dict, base_read: dict, tree_read: dict) -> list[Section]:
    upgrade_responses = compare_responses("upgrade path: responses", base_read["calls"], tree_read["calls"])
    upgrade_responses.summary = (
        f"GET /projects and GET /projects/{{id}} for {len(base_read['calls']) - 1} projects, "
        + (upgrade_responses.summary)
    )
    return [
        compare_responses("responses", base["calls"], tree["calls"]),
        compare_rows("rows", base["database"], tree["database"]),
        compare_sql("sql", base["calls"], tree["calls"], cascade=True),
        upgrade_responses,
        compare_sql("upgrade path: sql", base_read["calls"], tree_read["calls"], cascade=False),
        compare_rows("upgrade path: rows", base_read["database"], tree_read["database"]),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-rev", default=DEFAULT_BASE_REV, help="the commit before the repair layer")
    parser.add_argument("--repo", type=Path, default=REPO, help="the git repository to take the base from")
    parser.add_argument("--tree-src", type=Path, default=REPO / "cairn" / "src", help="this tree's cairn/src")
    parser.add_argument("--keep", type=Path, help="an empty folder to keep the databases and traces in")
    parser.add_argument("--json", type=Path, help="write the comparison to this file")
    parser.add_argument("--python", default=sys.executable, help="the interpreter for the two runs")
    parser.add_argument("--child", choices=("script", "read"), help=argparse.SUPPRESS)
    parser.add_argument("--src", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.child:
        run_child(args.child, args.src, args.db, args.out)
        return 0

    try:
        with _work_dir(args.keep) as work:
            base_src, commit = _base_tree(args.repo, args.base_rev, work)
            tree_src = args.tree_src.resolve()
            base = _run_child(args.python, "script", base_src, work / "base" / "cairn.db", work, "base")
            tree = _run_child(args.python, "script", tree_src, work / "tree" / "cairn.db", work, "tree")
            _copy_database(work / "base" / "cairn.db", work / "upgrade" / "cairn.db")
            base_read = _run_child(args.python, "read", base_src, work / "base" / "cairn.db", work, "base-read")
            tree_read = _run_child(args.python, "read", tree_src, work / "upgrade" / "cairn.db", work, "tree-read")
            sections = compare(base, tree, base_read, tree_read)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        _out(f"switch-off differential could not run: {error}")
        return 2

    identical = not any(section.problems for section in sections)
    _out(f"switch-off differential: base {commit[:12]} (git archive) against {tree_src}")
    _out(f"sqlite {tree['sqlite']}; script of {sum(1 for step in SCRIPT if isinstance(step, Call))} calls")
    for section in sections:
        verdict = "identical" if not section.problems else f"DIFFERENT ({len(section.problems)})"
        _out(f"{section.name}: {verdict}; {section.summary}")
        for problem in section.problems[:MAX_SHOWN]:
            _out(f"  - {problem}")
        if len(section.problems) > MAX_SHOWN:
            _out(f"  ... {len(section.problems) - MAX_SHOWN} more")
    _out(f"result: {'IDENTICAL' if identical else 'DIFFERENT'}")
    if args.json:
        report = {
            "base": commit,
            "tree": str(tree_src),
            "identical": identical,
            "sections": [{"name": s.name, "summary": s.summary, "problems": s.problems} for s in sections],
        }
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
