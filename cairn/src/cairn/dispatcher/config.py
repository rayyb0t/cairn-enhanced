from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskType = Literal["reason", "explore", "bootstrap"]
WorkerType = Literal["claudecode", "codex", "pi", "mock"]
CompletedAction = Literal["remove", "stop"]
WorkerHealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]
ExecutionMode = Literal["container", "local"]
LocalCompletedAction = Literal["keep", "remove"]

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "claudecode": (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "codex": (
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
    ),
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "mock": (),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "explore_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
}

PROMPT_REQUIRED_TOKENS_BY_GROUP: dict[str, dict[str, tuple[str, ...]]] = {
    "mock": {
        "reason.md": ("{fact_ids}", "{open_intents}", "{max_intents}"),
        "explore.md": ("{intent_id}",),
        "explore_conclude.md": ("{intent_id}",),
        "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
        "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
    }
}

MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "healthcheck": frozenset({"ok", "fail"}),
    "reason": frozenset({"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_execute": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap": frozenset({"complete", "fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
}

MOCK_DEFAULT_BEHAVIOR: dict[str, dict[str, Any]] = {
    "healthcheck": {
        "delay": [0.05, 0.15],
        "outcomes": {"ok": "1.0", "fail": "0.0"},
    },
    "reason": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "0.0",
            "intent": "1.0",
            "noop": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_execute": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "1.0",
            "fact": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
}

MOCK_ALLOWED_ENV_KEYS = frozenset(
    {f"MOCK_{phase.upper()}" for phase in MOCK_ALLOWED_OUTCOMES}
)


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig


class ContainerConfig(BaseModel):
    image: str
    network_mode: str
    completed_action: CompletedAction
    cap_add: list[str] = Field(default_factory=list)


class LocalConfig(BaseModel):
    workspace_root: str | None = None
    completed_action: LocalCompletedAction = "keep"


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    worker_healthcheck: WorkerHealthcheckMode = "startup_only"
    execution: ExecutionMode = "container"
    prompt_group: str = Field(min_length=1)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: WorkerType
    task_types: list[TaskType]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        # Required LLM env keys (base_url / key / model) are enforced per execution mode by
        # DispatchConfig: container mode needs them, local mode reuses the host CLI config.
        # The checks below are mode-independent and always apply.
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        if self.type == "mock":
            resolve_mock_behavior(self.name, self.env)
        return self


class RepairReviewerEnv(BaseModel):
    """A reviewer's env in the repair: section (spec 8.3): only these three names, never common_env."""

    model_config = ConfigDict(extra="forbid")

    REVIEW_MODEL: str = Field(min_length=1)
    REVIEW_BASE_URL: str
    REVIEW_API_KEY: str | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def hide_inputs(cls, data: Any) -> Any:
        return RepairConfig.hidden(data)

    @field_validator("REVIEW_MODEL", "REVIEW_BASE_URL")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return RepairConfig.refuse_placeholder(value)

    @field_validator("REVIEW_BASE_URL")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("REVIEW_BASE_URL must start with http:// or https://")
        return value

    @field_validator("REVIEW_API_KEY", mode="before")
    @classmethod
    def empty_api_key_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("REVIEW_API_KEY")
    @classmethod
    def validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        RepairConfig.refuse_placeholder(value)
        if not (value.isascii() and value.isprintable()):
            raise ValueError("REVIEW_API_KEY must be printable ASCII (0x20-0x7E)")
        return value


class RepairReviewerConfig(BaseModel):
    """One entry of repair.reviewers (spec 8.3): written like a worker entry, but never a worker.

    The optional settings are Appendix C-1's. Only their types are checked here; left out or null, the
    repair agent applies C-1's default, and it checks the values at its start.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    env: RepairReviewerEnv
    structured_output: bool | None = Field(default=None, strict=True)
    max_tokens: int | None = Field(default=None, ge=1, strict=True)
    context_window: int | None = Field(default=None, ge=1, strict=True)
    timeout_seconds: int | None = Field(default=None, ge=1, strict=True)
    max_attempts: int | None = Field(default=None, ge=1, strict=True)
    ca_bundle: str | None = Field(default=None, min_length=1)
    proxy: str | None = Field(default=None, min_length=1)
    price: dict[str, Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0)]] | None = None

    @model_validator(mode="before")
    @classmethod
    def hide_inputs(cls, data: Any) -> Any:
        return RepairConfig.hidden(data)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        # Printed in log lines and in the replay's report, so it has a rule; worker names have none.
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is None:
            raise ValueError("reviewer name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        return value

    @field_validator("ca_bundle", "proxy")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return None if value is None else RepairConfig.refuse_placeholder(value)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is not None and set(value) != {"input_per_mtok", "output_per_mtok"}:
            raise ValueError("price must have exactly the keys input_per_mtok and output_per_mtok")
        return value


class RepairConfig(BaseModel):
    """The optional repair: section of dispatch.yaml (spec 8.3; owner decision O1).

    Parsing reads no file and makes no request: the token file is read later, once per process, by the
    repair module, which loading dispatch.yaml never imports.

    An error raised inside the section names the key and the rule, never a value. pydantic prints the offending
    input next to the key it names, and the section holds the reviewers' keys (URLs can hold passwords too), so
    each of the three repair models first passes its input through hidden(): every string, byte string and number
    in it, in mappings, lists, tuples and sets at any depth, prints as <hidden> (no log line holds a key, spec 2.2;
    the agent's settings errors hold no value either, 9.2). Other values (booleans, nulls, dates and timestamps)
    and mapping keys, which are names and which the error's location shows anyway, print as they are. Validation
    turns each hidden value back into a plain str, int or float.

    That covers only errors raised inside the section. An error about the whole file (a missing top-level key, or
    one of DispatchConfig's own checks: unique worker names, max_project_workers, the execution mode) prints the
    file's input cut to its first and last two dozen characters or so. With repair: last in dispatch.yaml, the end
    of its last value, which can be a reviewer's REVIEW_API_KEY, is among them, as a worker's key is today when
    workers: comes last. So place repair: before workers:, which keeps every reviewer value away from both ends.

    These helpers sit in the model because dispatcher/config.py changes only by the repair field and its three
    models (spec 8.3).
    """

    model_config = ConfigDict(extra="forbid")

    token_file: str
    transcript_root: str = Field(default="~/.local/share/cairn/repair/transcripts", validate_default=True)
    projects: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = None
    reviewers: list[RepairReviewerConfig] = Field(min_length=1)

    class _HiddenStr(str):
        __slots__ = ()

        def __repr__(self) -> str:
            return "<hidden>"

    class _HiddenBytes(bytes):
        __slots__ = ()

        def __repr__(self) -> str:
            return "<hidden>"

    class _HiddenInt(int):
        __slots__ = ()

        def __repr__(self) -> str:
            return "<hidden>"

    class _HiddenFloat(float):
        __slots__ = ()

        def __repr__(self) -> str:
            return "<hidden>"

    class _HiddenDeep:
        # What hidden() puts in place of a container nested deeper than any valid section; it holds nothing.
        __slots__ = ()

        def __repr__(self) -> str:
            return "<hidden>"

    @staticmethod
    def hidden(data: Any) -> Any:
        """data with each str, bytes, int and float in it printed as <hidden>, in its containers at any depth.

        Containers are copied by id(), each mapping and list once per call: a YAML alias stays one shared object,
        so a value used through many aliases costs no more than its anchor, and an alias that holds itself stays a
        cycle, which pydantic then reports under its key, as it does elsewhere in dispatch.yaml.

        A container 32 levels down or deeper, which no valid section has, is not copied: it prints as one <hidden>.
        A chain of aliases, each anchor holding the one before it, builds an object as deep as the chain is long
        from as many short lines, and the YAML loader builds it without recursing. hidden() recurses once per
        level, so without that limit such a chain would end in a RecursionError, where the same chain in today's
        sections is an error that names the key.
        """
        return RepairConfig._hidden(data, {}, 0)

    @staticmethod
    def _hidden(data: Any, copies: dict[int, tuple[Any, Any]], depth: int) -> Any:
        # copies maps id() of each container met so far to (that container, its copy). Holding the container keeps
        # it alive for the whole call, so that no other object can be given its id() meanwhile. depth is how many
        # containers hold data in this call: 0 for the input itself.
        if isinstance(data, bool):  # a bool is an int; it is no secret, and a strict bool field needs a real one
            return data
        if isinstance(data, str):
            return RepairConfig._HiddenStr(data)
        if isinstance(data, bytes):  # YAML's !!binary
            return RepairConfig._HiddenBytes(data)
        if isinstance(data, int):
            return RepairConfig._HiddenInt(data)
        if isinstance(data, float):
            return RepairConfig._HiddenFloat(data)
        if not isinstance(data, (dict, list, tuple, set, frozenset)):
            return data
        known = copies.get(id(data))
        if known is not None:  # looked up before the depth limit, so that a value copied once stays one shared copy
            return known[1]
        if depth >= 32:
            # A valid section is 4 levels deep at most (repair.reviewers.N.env.KEY), and pydantic reports an error at
            # the first level that fails, so it never looks this far down.
            return RepairConfig._HiddenDeep()
        if isinstance(data, dict):
            mapping: dict[Any, Any] = {}
            copies[id(data)] = (data, mapping)  # before its values, so that a value holding this mapping gets the copy
            for key, value in data.items():
                mapping[key] = RepairConfig._hidden(value, copies, depth + 1)
            return mapping
        if isinstance(data, list):
            items: list[Any] = []
            copies[id(data)] = (data, items)
            for item in data:
                items.append(RepairConfig._hidden(item, copies, depth + 1))
            return items
        # YAML's !!pairs and !!omap give tuples, and !!set a set. Neither can hold itself, so a cycle through one
        # also passes a mapping or a list, whose copy is in copies by then: the recursion ends there. A set's copy
        # can iterate in another order, as a set of strings does from one process to the next anyway.
        hidden_items = [RepairConfig._hidden(item, copies, depth + 1) for item in data]
        if isinstance(data, tuple):
            copied: Any = tuple(hidden_items)
        elif isinstance(data, set):
            copied = set(hidden_items)
        else:
            copied = frozenset(hidden_items)
        copies[id(data)] = (data, copied)
        return copied

    @staticmethod
    def refuse_placeholder(value: str) -> str:
        if "<" in value or ">" in value:
            raise ValueError("still contains < or >, an unedited placeholder")
        return value

    @model_validator(mode="before")
    @classmethod
    def hide_inputs(cls, data: Any) -> Any:
        return cls.hidden(data)

    @field_validator("token_file", "transcript_root")
    @classmethod
    def validate_path(cls, value: str) -> str:
        expanded = os.path.expanduser(cls.refuse_placeholder(value))
        if not os.path.isabs(expanded):
            raise ValueError("must be an absolute path once ~ is expanded")
        return expanded

    @field_validator("projects")
    @classmethod
    def validate_projects(cls, value: list[str] | None) -> list[str] | None:
        for project_id in value or ():
            cls.refuse_placeholder(project_id)
        return value

    @field_validator("reviewers")
    @classmethod
    def validate_reviewer_names(cls, value: list[RepairReviewerConfig]) -> list[RepairReviewerConfig]:
        names = [reviewer.name for reviewer in value]
        if len(set(names)) != len(names):
            raise ValueError("reviewer names must be unique")
        return value


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig | None = None
    local: LocalConfig | None = None
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig]
    repair: RepairConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        return self

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "DispatchConfig":
        if self.runtime.execution == "container":
            if self.container is None:
                raise ValueError("container config is required when runtime.execution is container")
            for worker in self.workers:
                required = WORKER_ENV_KEYS[worker.type]
                missing = [key for key in required if not worker.env.get(key)]
                if missing:
                    raise ValueError(f"worker {worker.name} missing env keys: {', '.join(missing)}")
        else:  # local: workers reuse the host CLI config, so no LLM env keys are required
            if self.local is None:
                self.local = LocalConfig()
        return self

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        validate_prompt_resources(config.runtime.prompt_group)
        return config


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("cairn.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = PROMPT_REQUIRED_TOKENS_BY_GROUP.get(prompt_group, DEFAULT_PROMPT_REQUIRED_TOKENS)
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")


def resolve_mock_behavior(worker_name: str, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    unknown = sorted(key for key in env if key.startswith("MOCK_") and key not in MOCK_ALLOWED_ENV_KEYS)
    if unknown:
        raise ValueError(f"worker {worker_name} has unsupported mock env keys: {', '.join(unknown)}")

    behavior: dict[str, dict[str, Any]] = {}
    for phase, allowed_outcomes in MOCK_ALLOWED_OUTCOMES.items():
        prefix = _mock_env_prefix(phase)
        payload = _parse_mock_phase_payload(worker_name, env, prefix, MOCK_DEFAULT_BEHAVIOR[phase])
        min_delay, max_delay = _parse_mock_delay_range(worker_name, prefix, payload.get("delay"))
        if max_delay < min_delay:
            raise ValueError(f"worker {worker_name} {prefix}.delay[1] must be greater than or equal to delay[0]")
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, dict):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes must be an object")
        unknown_outcomes = sorted(set(raw_outcomes) - allowed_outcomes)
        if unknown_outcomes:
            raise ValueError(f"worker {worker_name} {prefix}.outcomes has unsupported keys: {', '.join(unknown_outcomes)}")
        outcomes: dict[str, float] = {}
        total = Decimal("0")
        for outcome in sorted(allowed_outcomes):
            weight = _parse_mock_probability(
                worker_name,
                prefix,
                raw_outcomes,
                outcome,
            )
            outcomes[outcome] = float(weight)
            total += weight
        if total != Decimal("1"):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes probabilities must sum to 1.0, got {total}")
        behavior[phase] = {
            "delay": {"min": min_delay, "max": max_delay},
            "outcomes": outcomes,
        }
        rules = payload.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ValueError(f"worker {worker_name} {prefix}.rules must be an array")
            normalized_rules: list[dict[str, Any]] = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"worker {worker_name} {prefix}.rules[{index}] must be an object")
                force = rule.get("force")
                if not isinstance(force, str) or force not in allowed_outcomes:
                    raise ValueError(
                        f"worker {worker_name} {prefix}.rules[{index}].force must be one of: {', '.join(sorted(allowed_outcomes))}"
                    )
                entry: dict[str, Any] = {"force": force}
                if "fact_ids_gte" in rule:
                    value = rule["fact_ids_gte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_gte must be a non-negative integer")
                    entry["fact_ids_gte"] = value
                if "fact_ids_lte" in rule:
                    value = rule["fact_ids_lte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_lte must be a non-negative integer")
                    entry["fact_ids_lte"] = value
                if "open_intents_empty" in rule:
                    value = rule["open_intents_empty"]
                    if not isinstance(value, bool):
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].open_intents_empty must be boolean")
                    entry["open_intents_empty"] = value
                normalized_rules.append(entry)
            behavior[phase]["rules"] = normalized_rules
    return behavior


def _mock_env_prefix(phase: str) -> str:
    return f"MOCK_{phase.upper()}"


def _parse_mock_phase_payload(worker_name: str, env: dict[str, str], key: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = env.get(key)
    if raw is None:
        return json.loads(json.dumps(default))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker {worker_name} {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"worker {worker_name} {key} must be a JSON object")
    return value


def _parse_mock_delay_range(worker_name: str, key: str, value: Any) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"worker {worker_name} {key}.delay must be a two-element number array")
    min_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[0]", value[0])
    max_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[1]", value[1])
    return min_delay, max_delay


def _coerce_mock_seconds(worker_name: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"worker {worker_name} {key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"worker {worker_name} {key} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"worker {worker_name} {key} must be non-negative")
    return parsed


def _parse_mock_probability(worker_name: str, phase_key: str, outcomes: dict[str, Any], outcome: str) -> Decimal:
    raw = outcomes.get(outcome, MOCK_DEFAULT_BEHAVIOR[phase_key.removeprefix("MOCK_").lower()]["outcomes"][outcome])
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be a decimal probability") from exc
    if value < 0 or value > 1:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be between 0 and 1")
    return value
