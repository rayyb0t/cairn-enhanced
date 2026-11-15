"""The agent's image and its Compose service (spec §9.1, §13): AG51; and the runbook's repair: examples (§8.3, §13).

Static checks of `repair-agent/Dockerfile`, `repair-agent/.dockerignore` and `docker-compose.repair.yaml`. The image
is built with `repair-agent/` as its context, never the repository root, which holds `dispatch.yaml` with the worker
keys and the reviewers' keys, `secrets/` and `datas/`; it copies only the package. The override mounts no key into
the agent: the reviewers' keys reach it in the dispatcher's settings file (§8.3, §9.2).

The runbook, `tools/repair-phase2/README.md`, shows the repair: section of dispatch.yaml for a host and for Compose
(§13 step 3). Some load errors print the section's input cut to its first 25 and last 24 characters: a misspelt
repair: key, the section indented into a worker entry, and a dispatcher from before Phase 2 (§8.3, What a load error
prints). So each reviewer's REVIEW_API_KEY is written between REVIEW_MODEL and REVIEW_BASE_URL, and the section ends
with a base URL, never a key. These two tests have no catalogue id.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT.parent
RUNBOOK = REPOSITORY / "tools" / "repair-phase2" / "README.md"
AGENT_ROOT = "/repair-agent"
COPIED_SOURCES = {"pyproject.toml", "uv.lock", "src/"}
# §9.1: the patterns of repair-agent/.dockerignore, in this order (each "!" line re-includes an example that the
# "**/*.yaml" line before it excludes).
DOCKERIGNORE_PATTERNS = [
    ".venv/",
    "tests/",
    "**/*.yaml",
    "!repair.example.yaml",
    "!repair.compose.example.yaml",
    "**/*.key",
    "**/*.token",
]
# §13: everything the override mounts, all read-only for the agent.
AGENT_VOLUMES = [
    "./repair.compose.yaml:/repair-agent/repair.yaml:ro",
    "./secrets/repair-agent.token:/run/secrets/repair-agent.token:ro",
    "./datas/repair-transcripts:/var/lib/cairn-repair/transcripts:ro",
]


@dataclass(frozen=True)
class Instruction:
    keyword: str
    arguments: str
    workdir: str


def parse_dockerfile(text: str) -> list[Instruction]:
    """The Dockerfile's instructions, with line continuations joined and comments dropped, each with the WORKDIR
    in force when it runs."""
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if current and line.startswith("#"):  # a comment inside a continued instruction
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        logical.append(current + line)
        current = ""
    assert not current, "the Dockerfile ends inside a continued line"

    instructions: list[Instruction] = []
    workdir = "/"
    for line in logical:
        keyword, _, arguments = line.partition(" ")
        keyword = keyword.upper()
        arguments = arguments.strip()
        instructions.append(Instruction(keyword, arguments, workdir))
        if keyword == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, arguments.strip("\"'")))
    return instructions


def _words(arguments: str) -> list[str]:
    if arguments.startswith("["):  # the JSON (exec) form
        value = json.loads(arguments)
        assert isinstance(value, list) and all(isinstance(item, str) for item in value), arguments
        return value
    return shlex.split(arguments)


def _copy_sources_and_destination(instruction: Instruction) -> tuple[list[str], str]:
    words = _words(instruction.arguments)
    flags = [word for word in words if word.startswith("--")]
    # --from copies from another stage or image, not from the build context; --chown and --chmod change nothing
    # about what is copied, but none is used here.
    assert flags == [], f"{instruction.keyword} {instruction.arguments}: flags {flags}"
    assert len(words) >= 2, instruction.arguments
    return words[:-1], words[-1]


def _resolve(path: str, workdir: str) -> str:
    return posixpath.normpath(posixpath.join(workdir, path))


def _covers(target: str, path: str) -> bool:
    return path == target or path.startswith(target.rstrip("/") + "/")


def _env_values(arguments: str) -> dict[str, str]:
    words = shlex.split(arguments)
    if words and "=" not in words[0]:  # the legacy form, ENV KEY value
        return {words[0]: arguments.split(None, 1)[1].strip().strip("\"'")}
    values = {}
    for word in words:
        key, _, value = word.partition("=")
        values[key] = value
    return values


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _check_dockerfile() -> None:
    """The Dockerfile copies only pyproject.toml, uv.lock and src/ into /repair-agent, removes group and world write
    permission from them after the COPY lines, runs `uv sync --frozen --no-dev` there, puts /repair-agent/.venv/bin
    first on PATH and has no USER line (§9.1)."""
    instructions = parse_dockerfile((PROJECT / "Dockerfile").read_text(encoding="utf-8"))
    keywords = [instruction.keyword for instruction in instructions]
    assert "USER" not in keywords  # root in its container, like today's images (§13); a non-root user is set in Compose

    copies = [index for index, instruction in enumerate(instructions) if instruction.keyword in ("COPY", "ADD")]
    assert copies
    sources: set[str] = set()
    copied_paths: list[str] = []
    for index in copies:
        names, destination = _copy_sources_and_destination(instructions[index])
        destination_path = _resolve(destination, instructions[index].workdir)
        assert _covers(AGENT_ROOT, destination_path), destination
        for name in names:
            source = name.removeprefix("./")
            source = "src/" if source == "src" else source
            assert source in COPIED_SOURCES, f"COPY of {name}: only pyproject.toml, uv.lock and src/ (§9.1)"
            sources.add(source)
            if destination.endswith("/") and source != "src/":
                copied_paths.append(posixpath.join(destination_path, posixpath.basename(source)))
            else:
                copied_paths.append(destination_path)
    assert sources == COPIED_SOURCES
    assert sorted(copied_paths) == ["/repair-agent/pyproject.toml", "/repair-agent/src", "/repair-agent/uv.lock"]

    runs = [(index, instruction) for index, instruction in enumerate(instructions) if instruction.keyword == "RUN"]
    chmods = []
    for index, instruction in runs:
        words = _words(instruction.arguments)
        if index > max(copies) and words[:3] == ["chmod", "-R", "go-w"]:
            chmods.append([_resolve(word, instruction.workdir) for word in words[3:]])
    assert chmods, "a RUN line after the COPY lines runs chmod -R go-w on the copied files (§9.1)"
    assert all(any(_covers(target, path) for targets in chmods for target in targets) for path in copied_paths)

    syncs = [
        (index, instruction)
        for index, instruction in runs
        if _words(instruction.arguments)[:2] == ["uv", "sync"]
        and {"--frozen", "--no-dev"} <= set(_words(instruction.arguments))
    ]
    assert len(syncs) == 1, "one RUN line runs uv sync --frozen --no-dev (§9.1)"
    sync_index, sync = syncs[0]
    assert sync_index > max(copies) and sync.workdir == AGENT_ROOT  # so the lock it syncs is the copied one

    environments = [_env_values(instruction.arguments) for instruction in instructions if instruction.keyword == "ENV"]
    paths = [values["PATH"] for values in environments if "PATH" in values]
    assert paths, "ENV PATH puts the virtual environment's bin first, so the Compose command finds cairn-repair"
    assert paths[-1].split(":")[0] == "/repair-agent/.venv/bin"


def _check_dockerignore() -> None:
    """repair-agent/.dockerignore holds the patterns of §9.1, in their order."""
    lines = (PROJECT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert patterns == DOCKERIGNORE_PATTERNS


def _check_compose_override() -> None:
    """docker-compose.repair.yaml builds the agent with context ./repair-agent, gives cairn-repair the
    host.docker.internal:host-gateway entry, mounts only read-only files into it and no model key (§13), and adds
    no .dockerignore at the repository root, whose context today's images use."""
    override = _load_yaml(REPOSITORY / "docker-compose.repair.yaml")
    assert set(override) == {"services"}
    services = override["services"]
    assert set(services) == {"cairn-server", "cairn-dispatcher", "cairn-repair"}

    agent = services["cairn-repair"]
    assert agent["build"] == {"context": "./repair-agent", "dockerfile": "Dockerfile"}
    assert agent["image"] == "cairn-repair-agent" and agent["container_name"] == "cairn-repair"
    assert agent["extra_hosts"] == ["host.docker.internal:host-gateway"]
    # No model key mount, no key in the environment: the reviewers' keys arrive in the settings file (§9.2). The
    # mounts are the agent's configuration, its token and the copy root, all read-only, and neither the Docker socket
    # nor dispatch.yaml.
    assert agent["volumes"] == AGENT_VOLUMES
    # Nothing else: no environment, env_file or secrets that could carry a key, and the default user, root (§13).
    assert set(agent) == {
        "build",
        "image",
        "container_name",
        "depends_on",
        "restart",
        "extra_hosts",
        "volumes",
        "command",
    }
    assert agent["depends_on"] == {
        "cairn-server": {"condition": "service_healthy"},
        "cairn-dispatcher": {"condition": "service_started"},
    }
    assert agent["restart"] == "unless-stopped"
    assert agent["command"] == ["cairn-repair", "--config", "/repair-agent/repair.yaml"]

    # Only the agent is built here; the server and the dispatcher keep today's image and gain token files only.
    assert services["cairn-server"] == {
        "environment": {
            "CAIRN_REPAIR_AGENT_TOKEN_FILE": "/run/secrets/repair-agent.token",
            "CAIRN_REPAIR_DISPATCHER_TOKEN_FILE": "/run/secrets/repair-dispatcher.token",
        },
        "volumes": [
            "./secrets/repair-agent.token:/run/secrets/repair-agent.token:ro",
            "./secrets/repair-dispatcher.token:/run/secrets/repair-dispatcher.token:ro",
        ],
    }
    # The dispatcher reads its repair: section from the dispatch.yaml that docker-compose.yaml mounts: no repair file
    # and no environment variable.
    assert services["cairn-dispatcher"] == {
        "volumes": [
            "./secrets/repair-dispatcher.token:/run/secrets/repair-dispatcher.token:ro",
            "./datas/repair-transcripts:/var/lib/cairn-repair/transcripts",
        ]
    }

    base = _load_yaml(REPOSITORY / "docker-compose.yaml")
    assert {name: service["build"]["context"] for name, service in base["services"].items()} == {
        "cairn-server": ".",
        "cairn-dispatcher": ".",
    }
    assert not (REPOSITORY / ".dockerignore").exists()


def test_agent_image_copies_only_the_package() -> None:
    """AG51 (static): the Dockerfile and .dockerignore put only the package into the agent's image, and
    docker-compose.repair.yaml builds that image from ./repair-agent, gives cairn-repair the
    host.docker.internal:host-gateway entry and mounts no model key into it."""
    _check_dockerfile()
    _check_dockerignore()
    _check_compose_override()


# The example dispatch.yaml that each runbook example goes into, in the README's order: Host (local mode), Compose.
RUNBOOK_EXAMPLE_FILES = ["dispatch.local.example.yaml", "dispatch.example.yaml"]
# The ends of made-up reviewer keys, one per reviewer; none of their 6-character pieces occurs elsewhere in a message.
KEY_ENDS = ["q7w8e9r0t1y2u3i4o5p6", "a1s2d3f4g5h6j7k8l9z0", "m3n4b5v6c7x8z9l0k1j2"]


def _runbook_repair_sections() -> list[dict[str, Any]]:
    """The runbook's examples of the repair: section (§13 step 3: for a host, then for Compose), as loaded."""
    blocks = re.findall(r"^```yaml\n(.*?)^```", RUNBOOK.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    loaded = [yaml.safe_load(block) for block in blocks if block.startswith("repair:")]
    assert len(loaded) == 2, "step 3 shows the repair: section for a host and for Compose"
    assert all(list(document) == ["repair"] for document in loaded)
    return [document["repair"] for document in loaded]


def test_runbook_writes_each_reviewer_key_between_model_and_base_url() -> None:
    """§8.3 rule (1), §13 step 3: in both examples, each reviewer's env names REVIEW_MODEL, then REVIEW_API_KEY, then
    REVIEW_BASE_URL, so the section ends with a base URL, never a key. The model and the key are placeholders
    (Appendix C-2); one reviewer calls the Claude Messages API at https://api.anthropic.com, the other an
    OpenAI-compatible endpoint (O8)."""
    for section in _runbook_repair_sections():
        base_urls = {}
        for reviewer in section["reviewers"]:
            env = reviewer["env"]
            assert list(env) == ["REVIEW_MODEL", "REVIEW_API_KEY", "REVIEW_BASE_URL"], reviewer["name"]
            assert env["REVIEW_MODEL"] == "<model name>"
            assert re.fullmatch(r"<[^<>]+>", env["REVIEW_API_KEY"]), reviewer["name"]
            base_urls[reviewer.get("api_format", "openai_chat")] = env["REVIEW_BASE_URL"]
        assert base_urls.keys() == {"anthropic_messages", "openai_chat"}
        assert base_urls["anthropic_messages"] == "https://api.anthropic.com"


@pytest.mark.parametrize("mistake", ["misspelt_repair_key", "repair_inside_a_worker"])
def test_runbook_examples_print_no_key_when_dispatch_yaml_fails_to_load(tmp_path: Path, mistake: str) -> None:
    """§8.3, What a load error prints; §13 steps 3 and 9: each runbook example, with made-up keys, goes right after
    server: in the example dispatch.yaml of its mode, as step 3 advises, and is then misspelt `repairs:` or indented
    into the first worker entry. The dispatcher's load error prints the section's input cut to its first 25 and last
    24 characters, and neither end holds a piece of a key. A dispatcher from before Phase 2 refuses `repair:` with
    the misspelt key's error (step 9). With REVIEW_API_KEY last in its env, the cut ends with the last key's end."""
    from cairn.dispatcher.config import DispatchConfig  # the dev group's cairn (spec §9.1)

    for section, example in zip(_runbook_repair_sections(), RUNBOOK_EXAMPLE_FILES, strict=True):
        assert len(section["reviewers"]) <= len(KEY_ENDS)
        for index, reviewer in enumerate(section["reviewers"]):
            reviewer["env"]["REVIEW_API_KEY"] = f"made-up-key-{index}-{KEY_ENDS[index]}"
        payload = yaml.safe_load((REPOSITORY / example).read_text(encoding="utf-8"))
        assert next(iter(payload)) == "server"
        if mistake == "misspelt_repair_key":
            payload = {"server": payload.pop("server"), "repairs": section, **payload}
        else:
            first = payload["workers"][0]
            payload["workers"][0] = {"name": first.pop("name"), "repair": section, **first}
        path = tmp_path / f"{mistake}-{example}"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

        with pytest.raises(ValidationError) as caught:
            DispatchConfig.load(path)
        message = str(caught.value)
        assert "input_value={'token_file': " in message, message  # the section's input, cut to its two ends
        pieces = [end[start : start + 6] for end in KEY_ENDS for start in range(len(end) - 5)]
        assert [piece for piece in pieces if piece in message] == [], (example, message)
