"""Tests for switch_off_differential.py, run on this tree and on changed copies of its server code.

The tool must find this tree identical with every switch off. A tree that answers an existing endpoint with an
error is a difference, reported call by call with every later call still compared, never a run that could not be
made (exit status 2), which is kept for runs that fail outside the calls they compare.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import switch_off_differential as differential

# The tool's four runs use the interpreter that runs these tests: the cairn environment.
pytest.importorskip("fastapi")

TREE_SRC = differential.REPO / "cairn" / "src"
CALLS = sum(1 for step in differential.SCRIPT if isinstance(step, differential.Call))

# Appended to a copy of server/app.py: the project list fails with an exception, every other call answers as before.
LISTING_FAULT = """

@app.middleware("http")
async def _injected_fault(request, call_next):
    if request.method == "GET" and request.url.path == "/projects":
        raise RuntimeError("injected fault")
    return await call_next(request)
"""


def _has_base_commit() -> bool:
    found = subprocess.run(
        [
            "git",
            "-C",
            str(differential.REPO),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{differential.DEFAULT_BASE_REV}^{{commit}}",
        ],
        capture_output=True,
        check=False,
    )
    return found.returncode == 0


pytestmark = pytest.mark.skipif(not _has_base_commit(), reason="the base commit is not in this clone")


def _run(tmp_path: Path, tree_src: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, dict[str, Any]]:
    """The tool's exit status, what it printed, and its report by section name (empty if it wrote none)."""
    report = tmp_path / "report.json"
    status = differential.main(["--tree-src", str(tree_src), "--keep", str(tmp_path / "work"), "--json", str(report)])
    out = capsys.readouterr().out
    if not report.exists():
        return status, out, {}
    return (
        status,
        out,
        {section["name"]: section for section in json.loads(report.read_text(encoding="utf-8"))["sections"]},
    )


def _tree_copy(tmp_path: Path) -> Path:
    src = tmp_path / "tree" / "src"
    shutil.copytree(TREE_SRC, src, ignore=shutil.ignore_patterns("__pycache__"))
    return src


def _reported(problems: list[str], call: str) -> bool:
    return any(re.fullmatch(rf"call \d+ {re.escape(call)}", problem) for problem in problems)


def test_this_tree_is_identical_with_every_switch_off(tmp_path, capsys) -> None:
    status, out, sections = _run(tmp_path, TREE_SRC, capsys)
    assert status == 0, out
    assert out.splitlines()[-1] == "result: IDENTICAL"
    assert all(not section["problems"] for section in sections.values()), out


def test_an_endpoint_failing_in_the_tree_is_a_difference_not_a_run_that_could_not_be_made(tmp_path, capsys) -> None:
    # Spec 4.2's hazard: a key of repair_runs on columns that are not intents' primary key makes SQLite refuse
    # today's project delete and reopen with "foreign key mismatch", which the server answers with a 500.
    src = _tree_copy(tmp_path)
    schema = src / "cairn" / "server" / "db.py"
    text, count = re.subn(
        r"(CREATE TABLE IF NOT EXISTS repair_runs \(.*?)"
        r"FOREIGN KEY \(intent_id, project_id\) REFERENCES intents\(id, project_id\)",
        r"\1FOREIGN KEY (intent_id) REFERENCES intents(id)",
        schema.read_text(encoding="utf-8"),
        count=1,
        flags=re.DOTALL,
    )
    assert count == 1
    schema.write_text(text, encoding="utf-8")

    status, out, sections = _run(tmp_path, src, capsys)
    assert status == 1, out
    assert "could not run" not in out
    assert out.splitlines()[-1] == "result: DIFFERENT"
    responses = sections["responses"]
    for call in (
        "POST /projects/proj_001/reopen: status base=200 tree=500",
        "DELETE /projects/proj_002: status base=204 tree=500",
        "DELETE /projects/proj_006: status base=204 tree=500",
        # The last call of the script: every call after the first error is still made and compared.
        "GET /projects/proj_002: status base=404 tree=200",
    ):
        assert _reported(responses["problems"], call), (call, responses["problems"])
    assert responses["summary"].startswith(f"{CALLS} calls;")


def test_a_failing_project_list_on_the_upgrade_path_is_compared_and_every_project_still_read(tmp_path, capsys) -> None:
    src = _tree_copy(tmp_path)
    app_file = src / "cairn" / "server" / "app.py"
    app_file.write_text(app_file.read_text(encoding="utf-8") + LISTING_FAULT, encoding="utf-8")

    status, out, sections = _run(tmp_path, src, capsys)
    assert status == 1, out
    assert "could not run" not in out
    upgrade = sections["upgrade path: responses"]
    assert "call 0 GET /projects: status base=200 tree=500" in upgrade["problems"], upgrade["problems"]
    # Only the listing differs: every project is still read, from the file, with the base server's body.
    assert all(problem.startswith("call 0 GET /projects: ") for problem in upgrade["problems"]), upgrade["problems"]
    assert re.match(r"GET /projects and GET /projects/\{id\} for [1-9]\d* projects", upgrade["summary"])
