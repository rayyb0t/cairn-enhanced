"""The repair: section of dispatch.yaml (spec 8.3; owner decision O1): what its errors print, and what it holds.

An error in the section names the key and the rule but prints no value, at any depth: the section holds the
reviewers' keys. A valid section holds plain values. DS27 (the section is optional and strict, and a file
without it parses exactly as today) is in test_repair_shadow.py.
"""

from __future__ import annotations

import ast
import base64
import copy
import datetime
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from repair_dispatcher_support import FAKE_REVIEWER_KEY, dispatch_payload, repair_section, write_dispatch_yaml

from cairn.dispatcher.config import DispatchConfig, RepairConfig


def test_repair_section_errors_name_the_key_but_print_no_value(tmp_path: Path) -> None:
    """pydantic prints the offending input of an error; for the repair: section it prints <hidden> instead.

    The section holds the reviewers' keys, so a malformed key, or a worker key pasted under a name the section
    does not have, must not reach the startup error a dispatcher prints.
    """
    leaked = "test-leak-canary-0004"
    cases = {
        "repair.reviewers.0.env.REVIEW_API_KEY": lambda s: s["reviewers"][0]["env"].__setitem__(
            "REVIEW_API_KEY", leaked + "\x01"
        ),
        "repair.reviewers.0.env.OPENAI_API_KEY": lambda s: s["reviewers"][0]["env"].__setitem__(
            "OPENAI_API_KEY", leaked
        ),
        "repair.reviewers.0.api_key": lambda s: s["reviewers"][0].__setitem__("api_key", leaked),
        "repair.api_key": lambda s: s.__setitem__("api_key", leaked),
        "repair.reviewers.0.env.REVIEW_BASE_URL": lambda s: s["reviewers"][0]["env"].__setitem__(
            "REVIEW_BASE_URL", f"user:{leaked}@127.0.0.1:11434/v1"
        ),
        "repair.reviewers.0.proxy": lambda s: s["reviewers"][0].__setitem__("proxy", f"http://user:{leaked}@<proxy>"),
        "repair.reviewers.1": lambda s: s["reviewers"].__setitem__(1, leaked),
    }
    for key, change in cases.items():
        section = repair_section()
        change(section)
        with pytest.raises(ValidationError) as caught:
            DispatchConfig.load(write_dispatch_yaml(tmp_path / "leak.yaml", dispatch_payload(section)))
        message = str(caught.value)
        assert f"\n{key}\n" in message, message
        assert leaked not in message, message
        assert "input_value=<hidden>" in message, message


NESTED_CANARY = "test-leak-canary-0005"
SHORT_CANARY = "canary-0006"  # short, and last in its list, so that pydantic's cut of a long input keeps it
NUMERIC_KEY = 24197857200151252728969465429440056815  # what YAML reads from a key made only of digits


def _short_reviewer(name: str, **env: str) -> dict[str, Any]:
    return {"name": name, "env": {"REVIEW_MODEL": "m", "REVIEW_BASE_URL": "http://x", **env}}


@pytest.mark.parametrize(
    ("change", "key"),
    [
        pytest.param(
            lambda s: s.__setitem__("common_env", {"OPENAI_API_KEY": NESTED_CANARY}),
            "repair.common_env",
            id="common_env-pasted-into-repair",
        ),
        pytest.param(
            lambda s: s["reviewers"][0].__setitem__("headers", {"Authorization": f"Bearer {NESTED_CANARY}"}),
            "repair.reviewers.0.headers",
            id="mapping-under-an-unknown-reviewer-key",
        ),
        pytest.param(
            lambda s: s["reviewers"][0]["env"].__setitem__("EXTRA", {"OPENAI_API_KEY": NESTED_CANARY}),
            "repair.reviewers.0.env.EXTRA",
            id="mapping-under-an-unknown-env-key",
        ),
        pytest.param(
            lambda s: s.__setitem__("keys", [{"k": [NESTED_CANARY]}]),
            "repair.keys",
            id="list-of-mappings-under-an-unknown-key",
        ),
        pytest.param(
            lambda s: s["reviewers"][0]["env"].__setitem__("REVIEW_API_KEY", {"value": NESTED_CANARY}),
            "repair.reviewers.0.env.REVIEW_API_KEY",
            id="mapping-as-REVIEW_API_KEY",
        ),
        pytest.param(
            lambda s: s["reviewers"].append(
                {
                    "name": "worker-pasted",
                    "type": "codex",
                    "env": {"REVIEW_MODEL": "m", "REVIEW_BASE_URL": "http://x", "OPENAI_API_KEY": NESTED_CANARY},
                }
            ),
            "repair.reviewers.2.env.OPENAI_API_KEY",
            id="worker-entry-pasted-as-a-reviewer",
        ),
        pytest.param(
            lambda s: s.__setitem__(
                "reviewers", [_short_reviewer("dup"), _short_reviewer("dup", REVIEW_API_KEY=SHORT_CANARY)]
            ),
            "repair.reviewers",
            id="duplicate-name-error-on-the-whole-list",
        ),
        pytest.param(
            lambda s: s.__setitem__("reviewers", _short_reviewer("one", REVIEW_API_KEY=SHORT_CANARY)),
            "repair.reviewers",
            id="reviewers-given-as-a-mapping",
        ),
        pytest.param(
            lambda s: s["reviewers"][0]["env"].__setitem__("REVIEW_API_KEY", NUMERIC_KEY),
            "repair.reviewers.0.env.REVIEW_API_KEY",
            id="REVIEW_API_KEY-that-YAML-reads-as-a-number",
        ),
        pytest.param(
            lambda s: s["reviewers"][1]["env"].__setitem__("OPENAI_API_KEY", NUMERIC_KEY / 7),
            "repair.reviewers.1.env.OPENAI_API_KEY",
            id="unknown-key-holding-a-float",
        ),
    ],
)
def test_repair_section_errors_print_no_value_at_any_depth(tmp_path: Path, change: Any, key: str) -> None:
    """Values in mappings and lists of the section, and numbers, are hidden too; the message still names the key."""
    section = repair_section()
    change(section)
    path = write_dispatch_yaml(tmp_path / "leak.yaml", dispatch_payload(section))
    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(path)
    message = str(caught.value)
    assert f"\n{key}\n" in message, message
    for leaked in ("canary", str(NUMERIC_KEY), str(NUMERIC_KEY / 7), repr(NUMERIC_KEY / 7), FAKE_REVIEWER_KEY):
        assert leaked not in message, message
    assert "<hidden>" in message, message


def test_a_valid_repair_section_holds_plain_values(tmp_path: Path) -> None:
    """Validation turns every hidden value back into a plain str, int or float, so nothing downstream sees one."""
    section = repair_section(projects=["proj_001"])
    section["reviewers"][1].update(
        structured_output=True,
        max_tokens=4096,
        context_window=32768,
        max_attempts=2,
        ca_bundle="/etc/ssl/certs/test-ca.pem",
        proxy="http://127.0.0.1:3128",
        price={"input_per_mtok": 1, "output_per_mtok": 2.5},
    )
    text = yaml.safe_dump(dispatch_payload(section), sort_keys=False)
    repair = DispatchConfig.load(write_dispatch_yaml(tmp_path / "plain.yaml", yaml.safe_load(text))).repair
    assert repair is not None

    def plain(value: Any) -> bool:
        if isinstance(value, dict):
            return all(type(key) is str and plain(item) for key, item in value.items())
        if isinstance(value, list):
            return all(plain(item) for item in value)
        return value is None or type(value) in (str, int, float, bool)

    values = [repair.token_file, repair.transcript_root, repair.projects]
    for reviewer in repair.reviewers:
        values += [reviewer.name, *reviewer.env.model_dump().values()]
        values += [getattr(reviewer, name) for name in type(reviewer).model_fields if name not in ("name", "env")]
    assert all(plain(value) for value in values), [type(value) for value in values]
    provider = repair.reviewers[1]
    assert type(provider.env.REVIEW_API_KEY) is str and provider.env.REVIEW_API_KEY == FAKE_REVIEWER_KEY
    assert type(provider.max_tokens) is int and provider.max_tokens == 4096
    assert provider.price == {"input_per_mtok": 1.0, "output_per_mtok": 2.5}
    assert all(type(value) is float for value in (provider.price or {}).values())
    assert "<hidden>" not in repr(repair) and "<hidden>" not in str(repair.model_dump())


ALIAS_CANARY = "test-leak-canary-0007"
REVIEWERS_YAML = "  reviewers:\n    - name: r\n      env: {REVIEW_MODEL: m, REVIEW_BASE_URL: 'http://x'}\n"


def _file_with_section(tmp_path: Path, section_yaml: str, prefix: str = "") -> Path:
    """dispatch.yaml with a worker key in common_env, then a repair: section written as YAML text."""
    payload = dispatch_payload()
    payload["common_env"] = {"OPENAI_API_KEY": ALIAS_CANARY}
    text = prefix + yaml.safe_dump(payload, sort_keys=False) + "repair:\n  token_file: /srv/cairn-test/repair.token\n"
    path = tmp_path / "dispatch.yaml"
    path.write_text(text + section_yaml, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("prefix", "section_yaml", "key"),
    [
        pytest.param("", "  reviewers: &r [*r]\n", "repair.reviewers.0", id="reviewers-holding-itself"),
        pytest.param(
            "",
            "  reviewers:\n    - name: r\n"
            f"      env: &e {{REVIEW_MODEL: *e, REVIEW_BASE_URL: 'http://{ALIAS_CANARY}.invalid/v1'}}\n",
            "repair.reviewers.0.env.REVIEW_MODEL",
            id="env-holding-itself",
        ),
        pytest.param(
            "",
            f"  extra: &x {{k: [*x, {ALIAS_CANARY}]}}\n" + REVIEWERS_YAML,
            "repair.extra",
            id="unknown-key-holding-itself",
        ),
        pytest.param("--- &file\n", "  reviewers: [*file]\n", "repair.reviewers.0.common_env", id="the-whole-file"),
    ],
)
def test_an_alias_that_holds_itself_is_an_error_that_names_the_key(
    tmp_path: Path, prefix: str, section_yaml: str, key: str
) -> None:
    """A YAML alias can refer to a node that holds it. The same alias in today's sections is a ValidationError
    that names the key, and so it is in repair:, with no value printed, rather than a RecursionError."""
    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(_file_with_section(tmp_path, section_yaml, prefix))
    message = str(caught.value)
    assert f"\n{key}\n" in message, message
    assert ALIAS_CANARY not in message, message


def test_hidden_keeps_a_shared_value_shared_and_a_cycle_a_cycle() -> None:
    """hidden() copies each mapping and list once per call. Ten aliases of a value at each of seven levels, a few
    hundred bytes of YAML, are ten million values once expanded; kept shared, they cost what the file holds."""
    leaf: dict[str, Any] = {"k": [ALIAS_CANARY]}
    fan: Any = leaf
    for _ in range(5):
        fan = [fan] * 10
    looped: dict[str, Any] = {"name": ALIAS_CANARY}
    looped["self"] = looped
    hidden = RepairConfig.hidden({"looped": looped, "fan": fan, "again": leaf})

    assert hidden["looped"]["self"] is hidden["looped"]
    assert repr(hidden["looped"]) == "{'name': <hidden>, 'self': {...}}"
    level = hidden["fan"]
    for _ in range(5):
        assert len(level) == 10 and all(item is level[0] for item in level)
        level = level[0]
    assert level is hidden["again"] and repr(level) == "{'k': [<hidden>]}"
    assert type(leaf["k"][0]) is str and looped["self"] is looped  # the input is left as it was


def test_hidden_reaches_into_every_container_and_keeps_what_is_no_secret() -> None:
    """Strings, bytes and numbers print as <hidden> in mappings, lists, tuples, sets and frozensets; booleans,
    nulls, dates and mapping keys print as they are; each hidden value still equals the value it hides."""
    when = datetime.date(2026, 1, 1)
    data = {
        "tuple": ("text", b"bytes", 7, 2.5, (["nested"],)),
        "set": {"text"},
        "frozenset": frozenset({b"bytes"}),
        "kept": [True, None, when],
    }
    hidden = RepairConfig.hidden(data)
    assert repr(hidden) == (
        "{'tuple': (<hidden>, <hidden>, <hidden>, <hidden>, ([<hidden>],)), 'set': {<hidden>}, "
        "'frozenset': frozenset({<hidden>}), 'kept': [True, None, datetime.date(2026, 1, 1)]}"
    )
    assert hidden == data
    for item, kind in zip(hidden["tuple"][:4], (str, bytes, int, float), strict=True):
        assert isinstance(item, kind) and type(item) is not kind, item  # validated as the type it subclasses
    assert type(hidden["set"]) is set and type(hidden["frozenset"]) is frozenset
    assert hidden["kept"][0] is True and hidden["kept"][2] is when


def test_hidden_never_takes_one_container_for_another() -> None:
    """hidden() knows containers by id(), which Python gives again once an object is gone. A mapping whose items()
    makes a fresh list per key, and drops each list once it has made the next, lets a later list take the id of an
    earlier one; each key still gets its own value, because hidden() holds every container it has copied."""

    class FreshLists(dict):
        def items(self) -> Any:
            previous = None
            for key in self:
                current = [f"value-{key}"]
                yield key, current
                previous = current  # released only once the next list exists
            del previous

    hidden = RepairConfig.hidden(FreshLists.fromkeys(f"k{index}" for index in range(8)))
    assert hidden == {f"k{index}": [f"value-k{index}"] for index in range(8)}


CHAIN_CANARY = "test-leak-canary-0012"
CHAIN_LENGTH = 2000  # twice Python's default recursion limit


def _alias_chain(indent: str) -> str:
    """YAML list items &a0 to &a1999, each anchor a list that holds the one before it: 2,000 short lines that YAML
    reads as one object 2,000 levels deep, without recursing. The leaf and the last 40 anchors hold the canary, so
    that it is both in the levels that hidden() copies and in the ones it leaves out."""
    lines = [f"{indent}- &a0 [{CHAIN_CANARY}]"]
    for index in range(1, CHAIN_LENGTH):
        canary = f", {CHAIN_CANARY}" if index >= CHAIN_LENGTH - 40 else ""
        lines.append(f"{indent}- &a{index} [*a{index - 1}{canary}]")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("holder", "outside"),
    [
        pytest.param("top-level", {("chains",)}, id="anchors-under-an-unknown-top-level-key"),
        pytest.param("runtime", set(), id="anchors-under-a-key-that-runtime-ignores"),
    ],
)
def test_a_deep_chain_of_aliases_is_an_error_that_names_the_key(
    tmp_path: Path, holder: str, outside: set[tuple[str, ...]]
) -> None:
    """The chain's anchors are outside repair:, and its last anchor is used in it twice. The same chain in today's
    sections is a ValidationError that names the key, and so it is in repair:, with no value printed, rather than
    a RecursionError. An unknown top-level key is an error of its own, as today, which prints its value cut to its
    two ends, here two keys that hold no canary; runtime: ignores a key it does not have, as today."""
    text = yaml.safe_dump(dispatch_payload(), sort_keys=False)
    if holder == "runtime":
        assert text.count("\nruntime:\n") == 1
        text = text.replace("\nruntime:\n", "\nruntime:\n  chains:\n" + _alias_chain("  "), 1)
    else:
        text = "chains:\n  first_of_the_chains: 0\n  chain:\n" + _alias_chain("  ") + "  last_of_the_chains: 0\n" + text
    last = f"*a{CHAIN_LENGTH - 1}"
    text += (
        "repair:\n  token_file: /srv/cairn-test/repair.token\n  reviewers:\n    - name: r\n"
        f"      env: {{REVIEW_MODEL: {last}, REVIEW_BASE_URL: 'http://x'}}\n  extra: {last}\n"
    )
    path = tmp_path / "dispatch.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(path)
    message = str(caught.value)
    inside = {("repair", "reviewers", 0, "env", "REVIEW_MODEL"), ("repair", "extra")}
    assert {error["loc"] for error in caught.value.errors()} == inside | outside, message
    for key in ("repair.reviewers.0.env.REVIEW_MODEL", "repair.extra"):
        assert f"\n{key}\n" in message, message
    assert CHAIN_CANARY not in message, message
    for error in caught.value.errors():
        if error["loc"] in inside:  # the whole input, not only what the message shows of it
            assert CHAIN_CANARY not in str(error) and "<hidden>" in str(error), error["loc"]


def test_hidden_copies_containers_32_levels_down_and_no_further() -> None:
    """hidden() recurses once per level, so a container 32 levels down or deeper, which no valid section has, is not
    copied but printed as one <hidden>. A value copied higher up stays that one copy where it is met again that
    deep; a value met that deep first is still copied in full where it is met higher up later."""
    deep: list[Any] = [CHAIN_CANARY]
    for _ in range(CHAIN_LENGTH):
        deep = [deep, CHAIN_CANARY]
    hidden = RepairConfig.hidden({"deep": deep})
    level, depth = hidden["deep"], 1
    while isinstance(level, list):
        assert len(level) == 2 and repr(level[1]) == "<hidden>" and level[1] == CHAIN_CANARY, depth
        level, depth = level[0], depth + 1
    assert depth == 32 and repr(level) == "<hidden>"
    assert not isinstance(level, (str, bytes, int, float, dict, list, tuple, set, frozenset))
    assert CHAIN_CANARY not in repr(hidden)
    assert type(deep[1]) is str and len(deep) == 2  # the input is left as it was

    shared: dict[str, Any] = {"k": CHAIN_CANARY}
    below: Any = shared
    for _ in range(31):
        below = [below]  # in {"below": below}, shared is 32 levels down
    high_first = RepairConfig.hidden({"high": shared, "below": below})
    level = high_first["below"]
    for _ in range(31):
        level = level[0]
    assert level is high_first["high"] and repr(level) == "{'k': <hidden>}"
    below_first = RepairConfig.hidden({"below": below, "high": shared})
    assert below_first["high"] == shared and repr(below_first["high"]) == "{'k': <hidden>}"


TAG_CANARY = "test-leak-canary-0010"
TAGGED_BINARY = base64.b64encode(f"{TAG_CANARY}\x01".encode()).decode()


@pytest.mark.parametrize(
    ("env_yaml", "key"),
    [
        pytest.param(
            f"{{REVIEW_MODEL: m, REVIEW_BASE_URL: 'http://x', REVIEW_API_KEY: !!binary {TAGGED_BINARY}}}",
            "repair.reviewers.0.env.REVIEW_API_KEY",
            id="binary",
        ),
        pytest.param(
            f"{{REVIEW_MODEL: m, REVIEW_BASE_URL: 'http://x', REVIEW_API_KEY: !!set {{{TAG_CANARY}}}}}",
            "repair.reviewers.0.env.REVIEW_API_KEY",
            id="set",
        ),
        pytest.param(f"!!pairs [REVIEW_MODEL: m, REVIEW_API_KEY: {TAG_CANARY}]", "repair.reviewers.0.env", id="pairs"),
        pytest.param(f"!!omap [REVIEW_MODEL: m, REVIEW_API_KEY: {TAG_CANARY}]", "repair.reviewers.0.env", id="omap"),
    ],
)
def test_tagged_yaml_values_print_no_value_either(tmp_path: Path, env_yaml: str, key: str) -> None:
    """YAML's !!binary gives bytes, !!set a set, and !!pairs and !!omap lists of tuples: hidden as strings are."""
    section_yaml = f"  reviewers:\n    - name: r\n      env: {env_yaml}\n"
    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(_file_with_section(tmp_path, section_yaml))
    message = str(caught.value)
    assert f"\n{key}\n" in message, message
    assert TAG_CANARY not in message and ALIAS_CANARY not in message, message
    assert "<hidden>" in message, message


def test_tagged_yaml_values_load_as_plain_values(tmp_path: Path) -> None:
    """What pydantic accepts from a tagged value loads as it would unhidden: a plain str, never a hidden one."""
    model = base64.b64encode(b"test-model").decode()
    section_yaml = (
        "  projects: !!set {proj_001}\n  reviewers:\n    - name: r\n"
        f"      env: {{REVIEW_MODEL: !!binary {model}, REVIEW_BASE_URL: 'http://x'}}\n"
    )
    repair = DispatchConfig.load(_file_with_section(tmp_path, section_yaml)).repair
    assert repair is not None
    assert repair.projects == ["proj_001"] and type(repair.projects[0]) is str
    assert repair.reviewers[0].env.REVIEW_MODEL == "test-model" and type(repair.reviewers[0].env.REVIEW_MODEL) is str


def test_whole_file_errors_show_no_reviewer_value_with_repair_before_workers(tmp_path: Path) -> None:
    """An error about the whole file prints the file's input cut to its two ends, which hidden() does not reach
    (RepairConfig's docstring). With repair: placed before workers:, as that docstring advises, even a reviewer
    key that is the section's last value reaches neither end."""
    distinct = "0123456789abcdefghijklmnopqrstuvwxyz"
    reviewer_key = "test-reviewer-key-" + distinct
    changes = {
        "duplicate worker names": lambda p: p["workers"].append(copy.deepcopy(p["workers"][0])),
        "max_project_workers above max_workers": lambda p: p["runtime"].__setitem__("max_project_workers", 99),
        "no server": lambda p: p.pop("server"),
    }
    for what, change in changes.items():
        section = repair_section()
        section["reviewers"][1] = {
            "name": "reviewer_provider",
            "env": {
                "REVIEW_MODEL": "m",
                "REVIEW_BASE_URL": "https://provider.invalid/v1",
                "REVIEW_API_KEY": reviewer_key,
            },
        }
        payload = dispatch_payload(section)
        workers = payload.pop("workers")
        payload["workers"] = workers  # after repair: now
        change(payload)
        with pytest.raises(ValidationError) as caught:
            DispatchConfig.load(write_dispatch_yaml(tmp_path / "order.yaml", payload))
        message = str(caught.value)
        assert [error["loc"] for error in caught.value.errors()] in ([()], [("server",)]), (what, message)
        shown = [distinct[i : i + 6] for i in range(len(distinct) - 5) if distinct[i : i + 6] in message]
        assert shown == [], (what, message)


CONFIG_PY = Path(__file__).resolve().parents[1] / "src" / "cairn" / "dispatcher" / "config.py"
REPAIR_MODELS = ["RepairReviewerEnv", "RepairReviewerConfig", "RepairConfig"]
REPAIR_FIELD_LINE = "    repair: RepairConfig | None = None\n"
REPAIR_IMPORTS = {"os", "re", "typing.Annotated"}

# dispatcher/config.py at 279d2df (the same file as at ee0f227): each top-level definition, in file order, with
# the first 16 hex digits of the SHA-256 of its source, and the names it imports.
TODAYS_DEFINITIONS = {
    "TaskType": "0dbc140016506107",
    "WorkerType": "19533b81729d92ad",
    "CompletedAction": "35263441cfd46cfe",
    "WorkerHealthcheckMode": "7f8c9d124864ca1b",
    "ExecutionMode": "4a9d84126ac295f3",
    "LocalCompletedAction": "4f339e57093a3725",
    "WORKER_ENV_KEYS": "6fd1e74a38386e0e",
    "DEFAULT_PROMPT_REQUIRED_TOKENS": "5cfa5fe3534a8c64",
    "PROMPT_REQUIRED_TOKENS_BY_GROUP": "ec59bc5d8ae761a5",
    "MOCK_ALLOWED_OUTCOMES": "1681525a2b4e3d4d",
    "MOCK_DEFAULT_BEHAVIOR": "595d730b2be56394",
    "MOCK_ALLOWED_ENV_KEYS": "03f89cb567917fe0",
    "ReasonTaskConfig": "0a733a60ca55d7ca",
    "ExploreTaskConfig": "ca5e4459c1e7b765",
    "BootstrapTaskConfig": "cccb3014b4a411fd",
    "TasksConfig": "6f6719189b352c78",
    "ContainerConfig": "1275f1540c160df8",
    "LocalConfig": "b78ca7017914bc68",
    "RuntimeConfig": "73db4f3da94f6300",
    "WorkerConfig": "4ecdf3f8fa7ca9b4",
    "DispatchConfig": "5c91c92a24b31bd7",
    "_validate_optional_positive_int_env": "39053be44957ebb6",
    "validate_prompt_resources": "2d8e14e9dd2695fb",
    "resolve_mock_behavior": "d63375ae9df8b605",
    "_mock_env_prefix": "1e843dae7fddb6ab",
    "_parse_mock_phase_payload": "a0ce27aa09082dbd",
    "_parse_mock_delay_range": "2d63e88369be1c6a",
    "_coerce_mock_seconds": "9b5c0ba820c16b6e",
    "_parse_mock_probability": "c18d0852a95ada74",
}
TODAYS_IMPORTS = {
    "__future__.annotations",
    "decimal.Decimal",
    "decimal.InvalidOperation",
    "importlib.resources",
    "json",
    "pathlib.Path",
    "pydantic.BaseModel",
    "pydantic.ConfigDict",
    "pydantic.Field",
    "pydantic.field_validator",
    "pydantic.model_validator",
    "typing.Any",
    "typing.Literal",
    "yaml",
}


def _top_level(source: str) -> tuple[dict[str, str], set[str]]:
    """Each top-level definition's source by name, in file order, and the names the module imports."""
    lines = source.splitlines(keepends=True)
    definitions: dict[str, str] = {}
    imports: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom):
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
            continue
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, start = node.targets[0].id, node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, start = node.target.id, node.lineno
        else:
            raise AssertionError(f"config.py: a top-level statement that is no definition, line {node.lineno}")
        definitions[name] = "".join(lines[start - 1 : node.end_lineno])
    return definitions, imports


def test_dispatcher_config_changes_only_by_the_repair_field_and_its_three_models() -> None:
    """Spec 8.3 and the build plan's allowlist: config.py gains the repair field of DispatchConfig and its three
    models, with their imports, and nothing else changes; no other definition is added, removed or edited."""
    definitions, imports = _top_level(CONFIG_PY.read_text(encoding="utf-8"))

    assert [name for name in definitions if name not in REPAIR_MODELS] == list(TODAYS_DEFINITIONS)
    position = list(definitions).index("DispatchConfig")
    assert list(definitions)[position - 3 : position] == REPAIR_MODELS
    assert definitions["DispatchConfig"].count(REPAIR_FIELD_LINE) == 1
    definitions["DispatchConfig"] = definitions["DispatchConfig"].replace(REPAIR_FIELD_LINE, "")
    changed = [
        name
        for name, digest in TODAYS_DEFINITIONS.items()
        if hashlib.sha256(definitions[name].encode()).hexdigest()[:16] != digest
    ]
    assert changed == []
    assert imports == TODAYS_IMPORTS | REPAIR_IMPORTS
