"""The agent's two sources of settings (spec §9.2), neither of which is ever `dispatch.yaml`.

- `repair.yaml` (`AgentConfig`): the agent's own configuration. Where its token and the copies are, how often it
  polls, and what its isolation check tests. It holds no server address, project list, model or model key.
- The settings file, `<transcripts.root>/agent-settings.json` (`AgentSettings`), which the dispatcher writes at its
  start from the `repair:` section of `dispatch.yaml` (spec §8.3): the server URL, the optional `projects` list and
  the reviewers, each reviewer's key included. It is read once, at the agent's start, with the guards the agent uses
  for copies.

Every startup error names a field and a rule, never a value, and stops the agent with exit code 2.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

logger = logging.getLogger(__name__)

SETTINGS_FILE_NAME = "agent-settings.json"
SETTINGS_MAX_BYTES = 65536
SETTINGS_VERSION = 1
CONFIG_MAX_BYTES = 1024 * 1024
SECRET_FILE_MAX_BYTES = 65536
TOKEN_MIN_LENGTH = 32
MODEL_MAX_LENGTH = 100
PROJECT_ID_MAX_LENGTH = 128
COPY_MAX_BYTES = 64 * 1024 * 1024
READABLE_BY_OTHERS = 0o044

DEFAULT_STRUCTURED_OUTPUT = True
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_ATTEMPTS = 2

REVIEWER_NAME_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENV_NAME_RULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# A key that a message may name; any other key (it comes from the file) is printed as "other".
PRINTABLE_FIELD_RULE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# Appendix C-2: the code has no default model, and the server limits `model` to 100 characters (§6.5).
MODEL_NAME_RULE_TEXT = (
    "set REVIEW_MODEL in the repair: section of dispatch.yaml to the model your provider serves, at most 100 "
    "characters; serve a longer name under a shorter one, for example with vLLM's --served-model-name"
)

# Keys that repair.yaml held before owner decision O1, and where each is set now (§9.2).
RETIRED_CONFIG_KEYS = {
    "projects": "projects is set in the repair: section of dispatch.yaml now, not here",
    "model": (
        "a model is configured as a reviewer entry under repair.reviewers, in the repair: section of "
        "dispatch.yaml, now, not here"
    ),
    "server": (
        "the server is the top-level server of dispatch.yaml now, which the dispatcher passes on in the "
        "settings file, not here"
    ),
}
# Settings of the removed Anthropic client (O1): refused, so an old block cannot silently do nothing.
REMOVED_REVIEWER_KEYS = frozenset({"provider", "effort", "server_fallbacks"})

CONFIG_RULES = {
    "token_file": "must be a path without < or >",
    "token_env": "must be the name of an environment variable",
    "server_api_key_file": "must be null or a path without < or >",
    "server_ca_bundle": "must be null or a readable file",
    "dispatch_config_to_check": (
        "required: the absolute path of the dispatcher's dispatch.yaml, or null only inside a container"
    ),
    "isolation": "must be a mapping",
    "unreadable_paths": "must be a list of absolute paths",
    "poll_interval": "must be an integer from 2 to 30 (seconds)",
    "max_concurrent_reviews": "must be an integer from 1 to 16",
    "transcripts": "required: a mapping with root, the dispatcher's transcript_root",
    "root": "required: an existing directory, the dispatcher's transcript_root",
    "max_bytes": "must be an integer of at least 1",
    "max_staleness_seconds": "must be an integer of at least 0",
    "digest": "must be a mapping",
    "max_tokens_estimate": "must be an integer of at least 1",
}

SETTINGS_RULES = {
    "version": "must be 1",
    "server": "must be an http:// or https:// URL with a host",
    "projects": "must be null or a list of strings of 1 to 128 characters",
    "reviewers": "must be a list of at least one reviewer entry",
    "name": "must be 1 to 64 letters, digits, '.', '_' or '-', starting with a letter or digit",
    "model": MODEL_NAME_RULE_TEXT,
    "base_url": (
        "must be an http:// or https:// URL with a host, and a port from 1 to 65535 if it has one, "
        "to which /chat/completions is appended"
    ),
    "api_key": "must be null or a non-empty string of printable ASCII (0x20-0x7E)",
    "structured_output": "must be null, true or false",
    "max_tokens": "must be null or an integer of at least 1",
    "context_window": "must be null or an integer of at least 1",
    "timeout_seconds": "must be null or an integer of at least 1 (seconds per attempt)",
    "max_attempts": "must be null or an integer of at least 1",
    "ca_bundle": "must be null or a readable file",
    "proxy": "must be null or an http:// or https:// URL with a host",
    "price": "must be null or an object of exactly input_per_mtok and output_per_mtok",
    "input_per_mtok": "must be a finite number of at least 0",
    "output_per_mtok": "must be a finite number of at least 0",
}


class StartupError(Exception):
    """A problem that stops the agent before it polls (exit code 2).

    The message names fields and rules, never a value. `logged` is true when the reason was already logged, so the
    caller prints nothing more.
    """

    def __init__(self, message: str, *, logged: bool = False) -> None:
        super().__init__(message)
        self.logged = logged


def is_printable_ascii(value: str) -> bool:
    """Every character in 0x20-0x7E, the server's rule for tokens (§7)."""
    return value.isascii() and value.isprintable()


def _rule(text: str) -> PydanticCustomError:
    return PydanticCustomError("agent_rule", "{rule}", {"rule": text})


def url_problem(value: str) -> str | None:
    """Why `value` is not an http:// or https:// URL with a host and a valid port; None when it is one."""
    if "<" in value or ">" in value:
        return "still holds a <...> placeholder"
    if not value.startswith(("http://", "https://")):
        return "must start with http:// or https://"
    if not is_printable_ascii(value) or " " in value:
        return "must be printable ASCII without spaces"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return "has an invalid host or port"
    if not parts.hostname:
        return "has no host"
    if port is not None and not 1 <= port <= 65535:
        return "has a port outside 1-65535"
    return None


def _path(value: str) -> str:
    if "<" in value or ">" in value:
        raise _rule("still holds a <...> placeholder; set a path")
    if not value or "\x00" in value:
        raise _rule("must be a non-empty path")
    return value


def _absolute_path(value: str) -> str:
    _path(value)
    if not os.path.isabs(value):
        raise _rule("must be an absolute path")
    return value


def _env_name(value: str) -> str:
    if not ENV_NAME_RULE.fullmatch(value):
        raise _rule("must be the name of an environment variable")
    return value


def _reviewer_name(value: str) -> str:
    if not REVIEWER_NAME_RULE.fullmatch(value):
        raise _rule(SETTINGS_RULES["name"])
    return value


def model_name_problem(value: str) -> bool:
    """Appendix C-2: 1-100 characters, no whitespace, '<' or '>'; control characters are refused too."""
    if not 1 <= len(value) <= MODEL_MAX_LENGTH:
        return True
    return any(char.isspace() or char in "<>" or not char.isprintable() for char in value)


def _model_name(value: str) -> str:
    if model_name_problem(value):
        raise _rule(MODEL_NAME_RULE_TEXT)
    return value


def _http_url(field: str) -> Any:
    def check(value: str) -> str:
        problem = url_problem(value)
        if problem is not None:
            raise _rule(f"{SETTINGS_RULES[field]}; this one {problem}")
        return value

    return check


def _api_key(value: str) -> str:
    if not value or not is_printable_ascii(value):
        raise _rule(SETTINGS_RULES["api_key"])
    return value


PathText = Annotated[StrictStr, AfterValidator(_path)]
AbsolutePathText = Annotated[StrictStr, AfterValidator(_absolute_path)]
EnvName = Annotated[StrictStr, AfterValidator(_env_name)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
Money = Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
ProjectId = Annotated[StrictStr, Field(min_length=1, max_length=PROJECT_ID_MAX_LENGTH)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IsolationConfig(_Model):
    unreadable_paths: tuple[AbsolutePathText, ...] = ()

    @field_validator("unreadable_paths", mode="before")
    @classmethod
    def _null_is_empty(cls, value: Any) -> Any:
        return () if value is None else value


class TranscriptsConfig(_Model):
    root: PathText
    max_bytes: PositiveInt = COPY_MAX_BYTES
    max_staleness_seconds: Annotated[StrictInt, Field(ge=0)] = 45


class DigestConfig(_Model):
    max_tokens_estimate: PositiveInt = 8000


class AgentConfig(_Model):
    """repair.yaml (§9.2). `extra="forbid"`: an unknown key, such as a pasted `workers:` block, is refused."""

    token_file: PathText | None = None
    token_env: EnvName | None = None
    server_api_key_file: PathText | None = None
    server_ca_bundle: PathText | None = None
    dispatch_config_to_check: AbsolutePathText | None  # required key: no default
    isolation: IsolationConfig = IsolationConfig()
    poll_interval: Annotated[StrictInt, Field(ge=2, le=30)] = 5
    max_concurrent_reviews: Annotated[StrictInt, Field(ge=1, le=16)] = 4
    transcripts: TranscriptsConfig
    digest: DigestConfig = DigestConfig()

    @field_validator("isolation", "digest", mode="before")
    @classmethod
    def _empty_section_is_default(cls, value: Any) -> Any:
        return {} if value is None else value


class Price(_Model):
    input_per_mtok: Money
    output_per_mtok: Money


class ReviewerSettings(_Model):
    """One reviewer of the settings file. Optional settings left out or null take Appendix C-1's defaults."""

    name: Annotated[StrictStr, AfterValidator(_reviewer_name)]
    model: Annotated[StrictStr, AfterValidator(_model_name)]
    base_url: Annotated[StrictStr, AfterValidator(_http_url("base_url"))]
    api_key: Annotated[StrictStr, AfterValidator(_api_key)] | None = Field(default=None, repr=False)
    structured_output: StrictBool = DEFAULT_STRUCTURED_OUTPUT
    max_tokens: PositiveInt = DEFAULT_MAX_TOKENS
    context_window: PositiveInt | None = None
    timeout_seconds: PositiveInt = DEFAULT_TIMEOUT_SECONDS
    max_attempts: PositiveInt = DEFAULT_MAX_ATTEMPTS
    ca_bundle: PathText | None = None
    proxy: Annotated[StrictStr, AfterValidator(_http_url("proxy"))] | None = None
    price: Price | None = None

    @field_validator("structured_output", "max_tokens", "timeout_seconds", "max_attempts", mode="before")
    @classmethod
    def _null_takes_the_default(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None and info.field_name is not None:
            return cls.model_fields[info.field_name].default
        return value


class AgentSettings(_Model):
    """The settings file the dispatcher writes (§8.3, §9.2). `projects` null: every switched-on project."""

    version: StrictInt
    server: Annotated[StrictStr, AfterValidator(_http_url("server"))]
    projects: tuple[ProjectId, ...] | None
    reviewers: Annotated[tuple[ReviewerSettings, ...], Field(min_length=1)]


@dataclass(frozen=True)
class SettingsFile:
    """The settings file as read at start: its path, content, and the owner and mode seen on the open file."""

    path: str
    settings: AgentSettings
    uid: int
    mode: int


def _field_path(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
            continue
        name = str(item) if PRINTABLE_FIELD_RULE.fullmatch(str(item)) else "other"
        parts.append(f".{name}" if parts else name)
    return "".join(parts) or "file"


def _rule_for(error: Mapping[str, Any], rules: Mapping[str, str]) -> str:
    kind = error["type"]
    if kind == "agent_rule":
        return str(error["msg"])
    names = [item for item in error["loc"] if isinstance(item, str)]
    if kind == "extra_forbidden":
        if names and names[-1] in REMOVED_REVIEWER_KEYS:
            return (
                "unknown key: there is one model client, the OpenAI-compatible one, and the settings of the "
                "removed Anthropic client are gone"
            )
        return "unknown key"
    for name in reversed(names):
        if name in rules:
            return rules[name]
    return "has an invalid value"


def _first_error(exc: ValidationError) -> Mapping[str, Any]:
    return exc.errors(include_url=False, include_context=False, include_input=False)[0]


# --- repair.yaml --------------------------------------------------------------------------------------------------


def _read_regular_file(path: str, limit: int) -> tuple[bytes, os.stat_result]:
    """Read a small regular file without blocking on a FIFO. Raises OSError, or ValueError naming the problem."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise ValueError(f"larger than {limit} bytes")
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _readable_file(path: str) -> bool:
    """A regular file that this process can open for reading (a CA file, §9.2 and Appendix C-1)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError):
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def load_config(path: str) -> AgentConfig:
    """Load and validate repair.yaml (§9.2). Raises StartupError naming the field, never a value."""

    def invalid(field: str, rule: str) -> StartupError:
        return StartupError(f"agent config invalid: {path}: {field}: {rule}")

    try:
        data, _ = _read_regular_file(path, CONFIG_MAX_BYTES)
    except OSError as exc:
        raise StartupError(f"agent config unreadable: {path}: {_errno_name(exc)}") from None
    except ValueError as exc:
        raise invalid("file", str(exc)) from None
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError):
        raise invalid("file", "not valid YAML") from None
    if not isinstance(raw, dict):
        raise invalid("file", "the top level must be a mapping")
    for key, message in RETIRED_CONFIG_KEYS.items():
        if key in raw:
            raise invalid(key, message)
    try:
        config = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        error = _first_error(exc)
        rule = _rule_for(error, CONFIG_RULES)
        if error["type"] == "missing" and not rule.startswith("required"):
            rule = f"required: {rule}"
        raise invalid(_field_path(tuple(error["loc"])), rule) from None
    if config.token_file is None and config.token_env is None:
        raise invalid("token_file", "required: set token_file, or token_env, to the agent token's source")
    if config.token_file is not None and config.token_env is not None:
        raise invalid("token_env", "set token_file or token_env, not both")
    if _not_a_directory(config.transcripts.root):
        raise invalid("transcripts.root", CONFIG_RULES["root"])
    if config.server_ca_bundle is not None and not _readable_file(config.server_ca_bundle):
        raise invalid("server_ca_bundle", CONFIG_RULES["server_ca_bundle"])
    return config


def _not_a_directory(path: str) -> bool:
    """True when `path` is missing, is not a directory, or is not a path the file system can encode.

    A path this user may not reach (EACCES or EPERM, such as a copy root under a home it cannot search) is not
    reported here: reading the settings file refuses it as `agent settings unreadable: …: permission denied`, which
    §9.2 requires for `transcripts.root`.
    """
    try:
        return not stat.S_ISDIR(os.stat(path).st_mode)
    except ValueError:  # UnicodeEncodeError for a lone surrogate (a YAML "\ud800" escape), or a NUL byte: no such path
        return True
    except OSError as exc:
        return exc.errno not in (errno.EACCES, errno.EPERM)


def _errno_name(exc: OSError) -> str:
    if exc.errno is not None and exc.errno in errno.errorcode:
        return errno.errorcode[exc.errno]
    return type(exc).__name__


def _secret_text(data: bytes) -> str | None:
    """The secret with surrounding ASCII whitespace removed, or None if a byte is not ASCII."""
    try:
        return data.strip().decode("ascii")
    except UnicodeDecodeError:
        return None


def token_problem(value: str | None) -> str | None:
    """Why a token is refused, checked in the server's order (§7): not_ascii, then shorter_than_32."""
    if value is None or not is_printable_ascii(value):
        return "not_ascii"
    if len(value) < TOKEN_MIN_LENGTH:
        return "shorter_than_32"
    return None


def read_agent_token(config: AgentConfig) -> str:
    """The agent role's token, from token_file or token_env, stripped (§9.2). Never logged."""
    if config.token_file is not None:
        source = f"token_file {config.token_file}"
        try:
            data, info = _read_regular_file(config.token_file, SECRET_FILE_MAX_BYTES)
        except OSError as exc:
            raise StartupError(f"agent token unreadable: {source}: {_errno_name(exc)}") from None
        except ValueError as exc:
            raise StartupError(f"agent token unreadable: {source}: {exc}") from None
        mode = stat.S_IMODE(info.st_mode)
        if mode & READABLE_BY_OTHERS:
            logger.warning(
                "repair agent token file readable by others file=%s mode=%s", config.token_file, f"{mode:04o}"
            )
    else:
        assert config.token_env is not None
        source = f"token_env {config.token_env}"
        raw = os.environ.get(config.token_env)
        if raw is None:
            raise StartupError(f"agent token missing: {source} is not set")
        data = raw.encode("utf-8", "surrogateescape")
    value = _secret_text(data)
    reason = token_problem(value)
    if reason is not None or value is None:
        raise StartupError(f"agent token refused: {source}: {reason}")
    return value


def read_server_api_key(config: AgentConfig) -> str | None:
    """The optional X-API-Key of a dev-0.3.0 server port (§7, §9.2), stripped. Never logged."""
    if config.server_api_key_file is None:
        return None
    source = f"server_api_key_file {config.server_api_key_file}"
    try:
        data, _ = _read_regular_file(config.server_api_key_file, SECRET_FILE_MAX_BYTES)
    except OSError as exc:
        raise StartupError(f"server API key unreadable: {source}: {_errno_name(exc)}") from None
    except ValueError as exc:
        raise StartupError(f"server API key unreadable: {source}: {exc}") from None
    value = _secret_text(data)
    if value is None or not is_printable_ascii(value):
        raise StartupError(f"server API key refused: {source}: not_ascii")
    if not value:
        raise StartupError(f"server API key refused: {source}: empty")
    return value


# --- the settings file --------------------------------------------------------------------------------------------


def settings_path(root: str) -> str:
    return os.path.join(root, SETTINGS_FILE_NAME)


def load_settings(root: str) -> SettingsFile:
    """Read `<root>/agent-settings.json` once, safely and strictly (§9.2). Raises StartupError."""
    path = settings_path(root)

    def invalid(field: str, rule: str) -> StartupError:
        return StartupError(f"agent settings invalid: {path}: {field}: {rule}")

    def denied() -> StartupError:
        return StartupError(
            f"agent settings unreadable: {path}: permission denied; the agent's user must hold the group of the "
            "copy root and of this file"
        )

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise denied() from None
        raise invalid("file", f"the copy root cannot be opened ({_errno_name(exc)})") from None
    try:
        try:
            fd = os.open(SETTINGS_FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise StartupError(
                    f"agent settings missing: {path}; start the dispatcher with a repair: section in dispatch.yaml "
                    "first"
                ) from None
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise denied() from None
            if exc.errno == errno.ELOOP:
                raise invalid("file", "is a symlink, not a regular file") from None
            raise invalid("file", f"cannot be opened ({_errno_name(exc)})") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise invalid("file", "is not a regular file")
            if info.st_size > SETTINGS_MAX_BYTES:
                raise invalid("file", f"is larger than {SETTINGS_MAX_BYTES} bytes")
            chunks: list[bytes] = []
            size = 0
            while size <= SETTINGS_MAX_BYTES:
                chunk = os.read(fd, SETTINGS_MAX_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > SETTINGS_MAX_BYTES:
                raise invalid("file", f"is larger than {SETTINGS_MAX_BYTES} bytes")
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    try:
        raw = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise invalid("file", "is not valid JSON") from None
    settings = parse_settings(raw, path)
    return SettingsFile(path=path, settings=settings, uid=info.st_uid, mode=stat.S_IMODE(info.st_mode))


def parse_settings(raw: Any, path: str) -> AgentSettings:
    """Validate decoded settings-file content. Every reviewer is checked, used or not (§9.2)."""

    def invalid(field: str, rule: str) -> StartupError:
        return StartupError(f"agent settings invalid: {path}: {field}: {rule}")

    if not isinstance(raw, dict):
        raise invalid("file", "the top level must be a JSON object")
    version = raw.get("version")
    if type(version) is not int or version != SETTINGS_VERSION:
        raise invalid("version", SETTINGS_RULES["version"])
    try:
        settings = AgentSettings.model_validate(raw)
    except ValidationError as exc:
        error = _first_error(exc)
        loc = tuple(error["loc"])
        rule = _rule_for(error, SETTINGS_RULES)
        if error["type"] == "missing":
            rule = f"required: {rule}" if rule != "has an invalid value" else "required"
        raise invalid(_field_path(loc), _for_reviewer(raw, loc, rule)) from None
    seen: set[str] = set()
    for index, reviewer in enumerate(settings.reviewers):
        if reviewer.name in seen:
            raise invalid(f"reviewers[{index}].name", "must be unique among reviewers")
        seen.add(reviewer.name)
        if reviewer.ca_bundle is not None and not _readable_file(reviewer.ca_bundle):
            raise invalid(f"reviewers[{index}].ca_bundle", f"reviewer {reviewer.name}: {SETTINGS_RULES['ca_bundle']}")
    return settings


def _for_reviewer(raw: Mapping[str, Any], loc: tuple[Any, ...], rule: str) -> str:
    """Prefix a reviewer field's rule with `reviewer <name>:`, so the message names the reviewer and the setting."""
    if len(loc) < 3 or loc[0] != "reviewers" or not isinstance(loc[1], int) or loc[2] == "name":
        return rule
    reviewers = raw.get("reviewers")
    entry = reviewers[loc[1]] if isinstance(reviewers, list) and loc[1] < len(reviewers) else None
    name = entry.get("name") if isinstance(entry, dict) else None
    if isinstance(name, str) and REVIEWER_NAME_RULE.fullmatch(name):
        return f"reviewer {name}: {rule}"
    return f"reviewer at reviewers[{loc[1]}]: {rule}"
