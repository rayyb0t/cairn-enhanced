"""The agent is its own program (spec §9.1, decision 1): AG1 and AG58.

`cairn-repair` loads neither the `docker` package nor anything of `cairn` (the dispatcher's `cli.py` loads
`DispatcherLoop`, and with it `docker`, for every command). Both are installed in this environment, through the
`dev` group's path dependency on `../cairn`, so a stray import would really load them. The agent depends on
`requests`, `pyyaml` and `pydantic` only, with its own lock. That dev dependency is `../cairn/src` itself, on the
versions `cairn/uv.lock` locks, so the tests that run the server's or the dispatcher's code here test that code.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from conftest import Layout

PROJECT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOP_LEVEL = ("docker", "cairn", "anthropic", "httpx", "httpx2", "httpcore", "httpcore2")


def _forbidden(modules: list[str]) -> list[str]:
    return sorted({name for name in modules if name.split(".")[0] in FORBIDDEN_TOP_LEVEL})


def _imported_modules(importtime_stderr: str) -> list[str]:
    names = []
    for line in importtime_stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            name = line.rsplit("|", 1)[1].strip()
            if name != "package":  # the header line
                names.append(name)
    return names


def test_console_entry_point_imports_no_docker_or_cairn(layout: Layout) -> None:
    """AG1: the real entry point, `cairn-repair --help` in a fresh interpreter, imports no docker, cairn,
    anthropic, httpx or httpx2; nor does a full startup, which loads requests only after its environment guard."""
    entry_points = [ep for ep in metadata.entry_points(group="console_scripts") if ep.name == "cairn-repair"]
    assert [ep.value for ep in entry_points] == ["cairn_repair_agent.cli:main"]
    script = Path(sys.executable).with_name("cairn-repair")
    assert script.is_file(), "uv sync installs the cairn-repair console script next to the interpreter"
    # The dependencies that must not load are installed here, so a stray import would succeed.
    for name in ("docker", "cairn", "httpx"):
        assert metadata.distribution(name) is not None

    result = subprocess.run(
        [sys.executable, "-X", "importtime", str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=PROJECT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--config", "--once", "--dry-run", "--log-level", "--allow-shared-user"):
        assert flag in result.stdout
    modules = _imported_modules(result.stderr)
    assert "cairn_repair_agent.cli" in modules
    assert _forbidden(modules) == []

    # A full startup in a fresh interpreter: everything it imports, and when requests is first loaded.
    driver = (
        "import json, sys\n"
        "from cairn_repair_agent import cli\n"
        "before = 'requests' in sys.modules\n"
        "agent = cli.start_or_report(cli.build_parser().parse_args(sys.argv[1:]))\n"
        "print(json.dumps({'started': agent is not None, 'requests_before_start': before,"
        " 'modules': sorted(sys.modules)}))\n"
    )
    started = subprocess.run(
        [sys.executable, "-c", driver, *layout.argv("--allow-shared-user")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=PROJECT,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    report = json.loads(started.stdout)
    assert report["started"] is True
    assert report["requests_before_start"] is False
    assert "requests" in report["modules"] and "cairn_repair_agent.server" in report["modules"]
    assert _forbidden(report["modules"]) == []


def test_agent_depends_only_on_requests_pyyaml_and_pydantic() -> None:
    """AG58 (static): exactly requests, pyyaml and pydantic at runtime; no anthropic, httpx2 or httpcore2 in the
    lock, which pins the requests and urllib3 versions Appendix C-1's read rules were checked with (§9.1)."""
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    names = sorted(_requirement_name(requirement) for requirement in dependencies)
    assert names == ["pydantic", "pyyaml", "requests"]
    assert "optional-dependencies" not in project["project"]
    assert "readme" not in project["project"] and "license-files" not in project["project"]
    assert project["project"]["scripts"] == {"cairn-repair": "cairn_repair_agent.cli:main"}
    # The tests' path dependency on ../cairn is in the dev group only, so `uv sync --no-dev` never needs it. It is
    # editable, so those tests run ../cairn/src as it is (test_cairn_dev_dependency_is_the_checkout_itself).
    assert "cairn" in [_requirement_name(item) for item in project["dependency-groups"]["dev"]]
    assert project["tool"]["uv"]["sources"]["cairn"] == {"path": "../cairn", "editable": True}
    # The package index setting of cairn/pyproject.toml.
    cairn_project = tomllib.loads((PROJECT.parent / "cairn" / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["uv"]["index"] == cairn_project["tool"]["uv"]["index"]

    lock = tomllib.loads((PROJECT / "uv.lock").read_text(encoding="utf-8"))
    versions = {package["name"]: package["version"] for package in lock["package"]}
    for name in ("anthropic", "httpx2", "httpcore2"):
        assert name not in versions
    assert versions["requests"] == "2.34.2"
    assert versions["urllib3"] == "2.8.0"
    agent = next(package for package in lock["package"] if package["name"] == "cairn-repair-agent")
    assert sorted(item["name"] for item in agent["dependencies"]) == ["pydantic", "pyyaml", "requests"]


def test_cairn_dev_dependency_is_the_checkout_itself() -> None:
    """§9.1: the tests that run the server's or the dispatcher's code here (AG8, AG9, AG37, XC7, XC8) import
    ../cairn/src itself. A copy installed at the first sync would keep old code: uv checks only cairn's
    pyproject.toml and the top of its src/ before reinstalling a path dependency, so a change deeper in the tree,
    such as the dispatcher's settings-file writer, or a new module would never reach this environment."""
    spec = importlib.util.find_spec("cairn")
    assert spec is not None and spec.origin is not None
    assert Path(spec.origin).resolve() == (PROJECT.parent / "cairn" / "src" / "cairn" / "__init__.py").resolve()
    direct_url = json.loads(metadata.distribution("cairn").read_text("direct_url.json") or "{}")
    assert direct_url.get("dir_info") == {"editable": True}


# Versions the agent's environment keeps its own: requests and urllib3 at the versions Appendix C-1's read rules were
# checked with (§9.1), and certifi, whose CA bundle the agent's own TLS connections use.
OWN_VERSIONS = {"requests": "2.34.2", "urllib3": "2.8.0", "certifi": None}


def _locked_versions(lock_path: Path) -> dict[str, str]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


def test_agent_environment_runs_cairn_on_its_locked_versions() -> None:
    """The server's and the dispatcher's code runs here on the versions that cairn/uv.lock installs in production:
    every package both locks hold has cairn's version (`constraint-dependencies` in pyproject.toml), except those in
    OWN_VERSIONS. Otherwise a cross-component test could pass on, say, a newer FastAPI than the server runs on. A
    change to cairn/uv.lock fails this until the constraints follow it and the agent is locked again."""
    agent = _locked_versions(PROJECT / "uv.lock")
    cairn = _locked_versions(PROJECT.parent / "cairn" / "uv.lock")
    shared = sorted((set(agent) & set(cairn)) - {"cairn", *OWN_VERSIONS})
    assert {"docker", "fastapi", "pydantic", "starlette", "uvicorn"} <= set(shared)
    assert {name: agent[name] for name in shared} == {name: cairn[name] for name in shared}
    for name, version in OWN_VERSIONS.items():
        assert version is None or agent[name] == version


def _requirement_name(requirement: str) -> str:
    for index, char in enumerate(requirement):
        if not (char.isalnum() or char in "-_."):
            return requirement[:index].lower()
    return requirement.lower()
