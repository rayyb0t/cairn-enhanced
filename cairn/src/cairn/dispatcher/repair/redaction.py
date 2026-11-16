"""Exact-value redaction of the transcript copies (spec 8.8, Redaction).

Every byte a copy takes from a worker's session file passes through redact_exact before it is written, with the
values that secret_values collects: the secret values of every configured worker's env (common_env included), in
local mode those of the dispatcher's own environment and of the local CLIs' credential files, the dispatcher's
repair token, and each reviewer's REVIEW_API_KEY, plus the user:pass and the password of every URL among those
environments' and those files' values, of dispatch.yaml's server, and of each reviewer's REVIEW_BASE_URL and proxy.
Since build step RA2 also each header value of the header-list variables (ANTHROPIC_CUSTOM_HEADERS and every
OTEL_EXPORTER_OTLP*_HEADERS) in those environments and in the env objects of .claude.json and settings.json, and each
value of the credential files' header maps (HEADER_MAPS), whatever the header's name, and each value that a header map
takes from an environment variable (REFERENCE_MAPS), resolved in each worker's environment as its CLI resolves it.
The CLIs also take environment variables and headers from places this module does not read, such as Claude Code's
managed settings (see "What is not read", below): a value set only there is not among these values.
The values are kept in memory only and are never logged: a copy's meta.json counts the matches per name, and a log
line gives at most a credential file's label.

The repair agent's pattern-based redaction is the second layer, for keys this set does not know (spec 9.6).

This module is also used by the shared-key check at dispatcher start (repair/housekeeping.py), which compares the
reviewers' keys with the worker values collected here, and by tools/repair-phase2/redact_eval_set.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

from cairn.dispatcher.config import DispatchConfig

LOG = logging.getLogger(__name__)

# A variable or key whose name matches this (a search, so accessToken, refresh_token, apiKey and primaryApiKey match)
# holds a secret.
SECRET_NAME_RULE = re.compile(r"(?i)(key|token|secret|pass|auth|credential|cookie)")
SECRET_MIN_CHARS = 8  # shorter values would match too much ordinary text
CREDENTIAL_ONLY_MIN_CHARS = 16  # every string of a credential-only file of at least this length is a secret
CREDENTIAL_FILE_MAX_BYTES = 16 * 1024 * 1024

REPAIR_TOKEN_NAME = "repair_token"  # noqa: S105 - the name redactions are counted under, not a secret
# The name that the URL passwords of dispatch.yaml's server are counted under: that setting's own name.
SERVER_NAME = "server"
REVIEW_API_KEY_NAME = "REVIEW_API_KEY"
# The names that a reviewer's URL passwords are counted under, as its key is under REVIEW_API_KEY: those of its
# REVIEW_BASE_URL under that variable's name, and those of its proxy setting, which has no variable name, under
# REVIEW_PROXY.
REVIEW_BASE_URL_NAME = "REVIEW_BASE_URL"
REVIEW_PROXY_NAME = "REVIEW_PROXY"

# The local CLIs' credential files (spec 8.8), by label. Credential-only files give every string value of at least
# CREDENTIAL_ONLY_MIN_CHARS at any depth (pi keeps OAuth tokens under access and refresh, names no rule matches);
# the others give the string values of at least SECRET_MIN_CHARS whose own key matches the secret-name rule or is
# key or apiKey (of settings.json, only its env object is read). Each also gives the user:pass and the password of
# every URL with a password among the string values read, whatever their keys (url_userinfo_values).
CLAUDE_CREDENTIALS = "claude:.credentials.json"
CLAUDE_JSON = "claude:.claude.json"
CLAUDE_SETTINGS = "claude:settings.json"
CODEX_AUTH = "codex:auth.json"
CODEX_CONFIG = "codex:config.toml"
PI_AUTH = "pi:auth.json"
PI_MODELS = "pi:models.json"
CREDENTIAL_ONLY_LABELS = frozenset({CLAUDE_CREDENTIALS, CODEX_AUTH, PI_AUTH})
# What is not read (fix round 3 of build step RA2). These seven files and the environments are the sources spec 8.8
# names. The CLIs also take environment variables and headers from other places, and a secret set only in one of them
# is not among the values: it gets only the repair agent's pattern layer (spec 9.6), as a login that Claude Code keeps
# in the macOS Keychain does, and the runbook (host install, step 4 e) says so. Claude Code 2.1.98 adds to its
# environment, after settings.json's env, the env of the project settings in the run's work folder
# (.claude/settings.json and .claude/settings.local.json, which only workers write), of --settings (which Cairn does
# not pass) and, last, of its managed settings, taken from the first of these that holds a setting: the server-managed
# settings it caches in remote-settings.json in its config folder, the managed preferences of a macOS configuration
# profile, and managed-settings.json with the managed-settings.d/*.json drop-ins in /etc/claude-code (on macOS in
# /Library/Application Support/ClaudeCode). It also reads MCP servers, with their headers, from managed-mcp.json in
# that folder and from the .mcp.json of the work folder and of each folder above it but the root, and it reads its
# global config from .config.json in its config folder instead of .claude.json when that file exists, or from
# .claude-custom-oauth.json when CLAUDE_CODE_CUSTOM_OAUTH_URL is set. codex 0.118.0 also loads configuration layers
# besides $CODEX_HOME/config.toml (its binary names /etc/codex/config.toml, /etc/codex/managed_config.toml and a
# project's .codex/config.toml).

# Header lists in an environment (build step RA2). Claude Code sends the headers of ANTHROPIC_CUSTOM_HEADERS, one
# "Name: value" per line, with every API request (a gateway's credential), and its telemetry exporter those of
# OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS, comma-separated name=value pairs whose values are
# URL-encoded (a telemetry backend's ingest key). Claude Code counts these variables among its secrets, but their names
# match no secret-name rule, and the header that holds the key may have no secret word in its name either.
CUSTOM_HEADERS_NAME = "ANTHROPIC_CUSTOM_HEADERS"
OTLP_HEADERS_RULE = re.compile(r"OTEL_EXPORTER_OTLP(?:_[A-Z0-9]+)*_HEADERS")
# The header maps of the credential files whose every value, whatever the header's name, is a credential (build step
# RA2), as (the object whose entries hold the map, or None for anywhere; the map's key): in .claude.json each MCP
# server's headers (mcpServers at the top level and under a project), in config.toml every http_headers table (of a
# model provider or an MCP server), and in models.json every headers object (of a provider, a model or a model
# override).
HEADER_MAPS: dict[str, tuple[str | None, str]] = {
    CLAUDE_JSON: ("mcpServers", "headers"),
    CODEX_CONFIG: (None, "http_headers"),
    PI_MODELS: (None, "headers"),
}
# The header maps whose values name the environment variable that a header's value comes from, and how the file's CLI
# resolves them (the review of build step RA2): pi takes a value that is the name of a set, non-empty variable as that
# variable's value, the form its docs show for keys and headers (a value that starts with '!' is a shell command, which
# only running it would resolve, so it is taken as it is written); Claude Code expands ${NAME} and ${NAME:-default} in
# an MCP server's headers, in its own environment, to which it adds the env objects of .claude.json and settings.json;
# and codex sends, for each entry of an env_http_headers table (of a model provider or an MCP server), the value of the
# variable it names, if that is set and not empty. Each value so taken, of at least SECRET_MIN_CHARS and not only white
# space, is counted under the file's label; each worker resolves them in its own merged environment, as its CLI does.
# The layers of Claude Code's environment read here are three, each later one winning: the worker's, then .claude.json's
# env, then settings.json's env (cli.js 2.1.98 assigns the two env objects to process.env in that order, and expands a
# reference from process.env). Claude Code adds further layers after them, which are not read ("What is not read",
# above): a variable that one of those sets is resolved here as the three layers have it. The whole header value is
# expanded in the three layers as Claude Code expands it, but every value that a referenced variable has in any of
# them is taken (the recheck of build step RA2): a value that a later layer hides is still in a file or an environment
# that a local worker can print, so the order never decides whether a value is removed.
REFERENCE_NAME = "name"  # the value is the name of the variable (pi's headers, codex's env_http_headers)
REFERENCE_TEMPLATE = "template"  # ${NAME} and ${NAME:-default} in the value are expanded (Claude Code)
REFERENCE_MAPS: dict[str, tuple[str | None, str, str]] = {
    CLAUDE_JSON: ("mcpServers", "headers", REFERENCE_TEMPLATE),
    CODEX_CONFIG: (None, "env_http_headers", REFERENCE_NAME),
    PI_MODELS: (None, "headers", REFERENCE_NAME),
}
_TEMPLATE_RULE = re.compile(r"\$\{([^}]+)\}")  # as Claude Code 2.1.98 matches a reference in an MCP server's config
# The whole values that the ${NAME} templates of one file expand to, in one worker's environment, are taken in the
# file's order while they come to at most this many characters together, each distinct template counted once, and none
# is built from a longer text (the recheck of build step RA2). A local worker can write both a template and the value
# it names (in .claude.json's own env), so without a bound a file of 100 KB could expand to a value of gigabytes, which
# the run would hold and every copy escape. 1 MiB is far more than all the MCP headers of a real .claude.json
# together; past it, a template gives only what it puts in place of its references: single variables' values, and
# defaults written in the file.
EXPANDED_HEADERS_MAX_CHARS = 1024 * 1024

_oversized_logged: set[str] = set()
_oversized_lock = threading.Lock()


def secret_name(name: str) -> bool:
    """Whether a variable or key named name holds a secret."""
    return SECRET_NAME_RULE.search(name) is not None


def environment_pairs(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """(name, value) of the secret values of one environment, in its order.

    Each value of at least SECRET_MIN_CHARS whose name matches the secret-name rule; the header values and the whole
    value of a header-list variable (header_list_values); and, for every value that is a URL with a password
    (scheme://user:pass@host), its user:pass and its password; each of at least SECRET_MIN_CHARS, under the
    variable's name. A percent-encoded password also gives its decoded form.
    """
    pairs: list[tuple[str, str]] = []
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if len(value) >= SECRET_MIN_CHARS and secret_name(name):
            pairs.append((name, value))
        pairs.extend((name, part) for part in header_list_values(name, value))
        pairs.extend((name, part) for part in url_userinfo_values(value))
    return pairs


def header_list_values(name: str, value: str) -> list[str]:
    """For a header-list variable, each header value, whatever the header's name, and the whole value, each of at
    least SECRET_MIN_CHARS; nothing for any other variable. Never raises: a value that holds no header gives only
    itself.

    ANTHROPIC_CUSTOM_HEADERS is split as Claude Code splits it: into lines, each "Name: value" with the value after the
    first ':', trimmed. An OTEL_EXPORTER_OTLP*_HEADERS variable is split at commas into name=value pairs, the value
    after the first '=', trimmed; a value also gives its part before a ';' (where W3C baggage puts properties) and the
    URL-decoded form of both, as the exporter sends them.
    """
    if name == CUSTOM_HEADERS_NAME:
        parts = [line.partition(":")[2].strip() for line in value.split("\n") if ":" in line]
    elif OTLP_HEADERS_RULE.fullmatch(name):
        parts = []
        for pair in value.split(","):
            if "=" not in pair:
                continue
            raw = pair.partition("=")[2].strip()
            first = raw.partition(";")[0].strip()
            parts.extend((raw, first, _url_decoded(raw), _url_decoded(first)))
    else:
        return []
    found = [part for part in (*parts, value) if len(part) >= SECRET_MIN_CHARS]
    return list(dict.fromkeys(found))


def _url_decoded(text: str) -> str:
    try:
        return unquote(text)
    except (UnicodeError, ValueError):
        return text


def environment_values(environ: Mapping[str, str]) -> set[str]:
    """The secret values of one environment (environment_pairs), as a set."""
    return {value for _name, value in environment_pairs(environ)}


def url_userinfo_values(value: str) -> list[str]:
    """For a URL with a password, its user:pass and its password (and the password decoded), each of at least
    SECRET_MIN_CHARS; nothing for any other value."""
    if "@" not in value or "://" not in value:
        return []
    try:
        netloc = urlsplit(value).netloc
    except ValueError:
        return []
    if "@" not in netloc:
        return []
    userinfo = netloc.rpartition("@")[0]
    if ":" not in userinfo:
        return []
    password = userinfo.partition(":")[2]
    found = [part for part in (userinfo, password, unquote(password)) if len(part) >= SECRET_MIN_CHARS]
    return list(dict.fromkeys(found))


def secret_values(
    config: DispatchConfig,
    execution: str,
    environ: Mapping[str, str],
    repair_token: str | None,
) -> dict[str, str]:
    """Every value redact_exact removes from a copy, mapped to the name it is counted under (spec 8.8).

    In this order, a value first met keeping its first name: every configured worker's env (common_env is already
    merged into it), in dispatch.yaml order; in local mode the dispatcher's own environment (environ), which local
    workers inherit, and the local CLIs' credential files, found through each worker's merged environment
    {**environ, **worker.env}; the repair token; the user:pass and the password of dispatch.yaml's server when it is
    a URL with a password (url_userinfo_values), under server; and for each reviewer, its REVIEW_API_KEY of at least
    SECRET_MIN_CHARS and the user:pass and the password of its REVIEW_BASE_URL and of its proxy when either is a URL
    with a password, under REVIEW_BASE_URL and REVIEW_PROXY. A local worker can read the server and the reviewers'
    values in dispatch.yaml and in the agent's settings file, as it can a reviewer's key (spec 2.2, 8.8). The
    credential files are read on each call, so a caller that calls again sees rotated tokens.
    """
    found: dict[str, str] = {}

    def add(pairs: Iterable[tuple[str, str]]) -> None:
        for name, value in pairs:
            if value and value not in found:
                found[value] = name

    for worker in config.workers:
        add(environment_pairs(worker.env))
    if execution == "local":
        add(environment_pairs(environ))
        add(credential_file_pairs(config, environ))
    if repair_token:
        add([(REPAIR_TOKEN_NAME, repair_token)])
    # A Cairn server behind a proxy with basic auth puts a password in this URL, which requests sends from it.
    add((SERVER_NAME, part) for part in url_userinfo_values(config.server))
    if config.repair is not None:
        for reviewer in config.repair.reviewers:
            key = reviewer.env.REVIEW_API_KEY
            if key is not None and len(key) >= SECRET_MIN_CHARS:
                add([(REVIEW_API_KEY_NAME, key)])
            # A model server behind basic auth, or a corporate proxy, puts a password in these URLs.
            add((REVIEW_BASE_URL_NAME, part) for part in url_userinfo_values(reviewer.env.REVIEW_BASE_URL))
            if reviewer.proxy is not None:
                add((REVIEW_PROXY_NAME, part) for part in url_userinfo_values(reviewer.proxy))
    return found


def redact_exact(data: bytes, values: Mapping[str, str]) -> tuple[bytes, dict[str, int]]:
    """data with every value of values replaced by [REDACTED:<name>], and the number of replacements per name.

    values maps each value to its name. Each value is matched as its UTF-8 bytes and as its JSON-escaped forms
    (json.dumps(value)[1:-1], with and without ensure_ascii), and those forms escaped once more (a JSON file printed
    inside a JSON line), so a value inside a JSON string, or inside NUL-separated /proc/<pid>/environ output, is
    found as long as it is intact. The forms are replaced longest first, so a value that holds a shorter one is
    replaced whole. The counts hold every name of values, 0 for a name with no match; they never hold a value.
    """
    counts: dict[str, int] = {}
    forms: dict[bytes, str] = {}
    for value, name in values.items():
        counts.setdefault(name, 0)
        for form in _forms(value):
            forms.setdefault(form, name)
    for form, name in sorted(forms.items(), key=lambda item: len(item[0]), reverse=True):
        found = data.count(form)
        if found:
            data = data.replace(form, b"[REDACTED:" + name.encode("utf-8", "backslashreplace") + b"]")
            counts[name] += found
    return data, counts


def _forms(value: str) -> list[bytes]:
    """The byte forms of value that redact_exact replaces: raw UTF-8, both JSON-escaped forms, and those escaped once
    more, which is how a JSON file holding the value looks when a tool prints it into a JSON transcript line (a key
    with a quote or a backslash in a credential file that a worker cats, for example)."""
    escaped: list[str] = []
    for ensure_ascii in (True, False):
        try:
            escaped.append(json.dumps(value, ensure_ascii=ensure_ascii)[1:-1])
        except (TypeError, ValueError):
            continue
    twice: list[str] = []
    for text in escaped:
        for ensure_ascii in (True, False):
            try:
                twice.append(json.dumps(text, ensure_ascii=ensure_ascii)[1:-1])
            except (TypeError, ValueError):
                continue
    found: list[bytes] = []
    for text in [value, *escaped, *twice]:
        for errors in ("surrogateescape", "surrogatepass"):
            try:
                form = text.encode("utf-8", errors)
            except UnicodeEncodeError:
                continue
            if form and form not in found:
                found.append(form)
            break
    return found


# The local CLIs' credential files ------------------------------------------------------------------------------------


def credential_file_pairs(config: DispatchConfig, environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """(label, value) of the secrets in the local CLIs' credential files of every configured worker (spec 8.8).

    Each worker's paths are resolved in its merged environment {**environ, **worker.env}, where ~ is that
    environment's HOME; a file shared by several workers is read once, and the references of its header maps
    (REFERENCE_MAPS) are resolved in each such worker's merged environment, the one its CLI starts with (for Claude
    Code with the env objects of the worker's .claude.json and settings.json added, as Claude Code adds them, and with
    every value that a referenced variable has in any of those three layers; the layers Claude Code adds after them
    are not read, "What is not read" above).
    A file that is missing, unreadable, not a regular file, larger than CREDENTIAL_FILE_MAX_BYTES or unparsable is
    skipped, as is one whose reading, parsing or walking raises anything else, so a credential file never raises out
    of here.
    """
    pairs: list[tuple[str, str]] = []
    files: dict[str, tuple[str, _CredentialFile]] = {}
    for worker in config.workers:
        merged = {**environ, **worker.env}
        paths = credential_paths(merged)
        for label, path in paths:
            if path in files:
                continue
            try:
                found = _credential_file(label, path)
            except Exception:  # a credential file never stops the reporter thread
                found = _NOTHING
            files[path] = (label, found)
            pairs.extend((label, value) for value in found.values)
        # The layers of Claude Code's environment read here (REFERENCE_MAPS): the worker's, then the env object of
        # .claude.json, then that of settings.json, each later layer winning, in the order Claude Code adds them.
        by_label = dict(paths)
        layers: list[Mapping[str, str]] = [merged]
        layers += [files[by_label[label]][1].env for label in (CLAUDE_JSON, CLAUDE_SETTINGS) if label in by_label]
        claude_env = {name: value for layer in layers for name, value in layer.items()}
        for path in dict.fromkeys(path for _label, path in paths):
            label, found = files[path]
            if not found.references:
                continue
            try:
                if label == CLAUDE_JSON:
                    resolved = _resolved(found.references, claude_env, layers)
                else:
                    resolved = _resolved(found.references, merged)
            except Exception:  # noqa: S112 - a credential file never stops the reporter thread
                continue
            pairs.extend((label, value) for value in resolved)
    return pairs


def credential_paths(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(label, absolute path) of the seven credential files of one merged environment.

    ${CLAUDE_CONFIG_DIR:-~/.claude}, ${CODEX_HOME:-~/.codex} and ${PI_CODING_AGENT_DIR:-~/.pi/agent}, with ~ the
    environment's HOME; .claude.json is $CLAUDE_CONFIG_DIR/.claude.json when CLAUDE_CONFIG_DIR is set, else
    ~/.claude.json. A path that does not resolve to an absolute one (no HOME, or a relative directory) is left out.
    """
    home = env.get("HOME") or None
    claude_dir = _directory(env.get("CLAUDE_CONFIG_DIR"), home, ".claude")
    codex_dir = _directory(env.get("CODEX_HOME"), home, ".codex")
    pi_dir = _directory(env.get("PI_CODING_AGENT_DIR"), home, os.path.join(".pi", "agent"))
    if env.get("CLAUDE_CONFIG_DIR"):
        claude_json = _join(claude_dir, ".claude.json")
    else:
        claude_json = _directory(None, home, ".claude.json")
    candidates = [
        (CLAUDE_CREDENTIALS, _join(claude_dir, ".credentials.json")),
        (CODEX_AUTH, _join(codex_dir, "auth.json")),
        (PI_AUTH, _join(pi_dir, "auth.json")),
        (CLAUDE_JSON, claude_json),
        (CLAUDE_SETTINGS, _join(claude_dir, "settings.json")),
        (CODEX_CONFIG, _join(codex_dir, "config.toml")),
        (PI_MODELS, _join(pi_dir, "models.json")),
    ]
    return [(label, path) for label, path in candidates if path is not None]


def _directory(value: str | None, home: str | None, default: str) -> str | None:
    """${VALUE:-~/<default>} with ~ as home: absolute, or None when it cannot be resolved to an absolute path."""
    if not value:
        value = "~/" + default
    if value == "~" or value.startswith("~/"):
        if home is None or not os.path.isabs(home):
            return None
        value = home + value[1:]
    return value if os.path.isabs(value) else None


def _join(directory: str | None, name: str) -> str | None:
    return None if directory is None else os.path.join(directory, name)


class _CredentialFile(NamedTuple):
    """What one credential file gives (_credential_file): its values; the references of its header maps
    (REFERENCE_MAPS), as (how the CLI resolves it, the value as written), which each worker resolves in its own
    environment (_resolved); and, for .claude.json and settings.json, the string entries of the env object that
    Claude Code adds to its environment."""

    values: tuple[str, ...]
    references: tuple[tuple[str, str], ...]
    env: Mapping[str, str]


_NOTHING = _CredentialFile((), (), {})


def _credential_file(label: str, path: str) -> _CredentialFile:
    data = _read_credential_file(label, path)
    if data is None:
        return _NOTHING
    try:
        if label == CODEX_CONFIG:
            parsed: Any = tomllib.loads(data.decode("utf-8"))
        else:
            parsed = json.loads(data)
    except (ValueError, RecursionError):
        return _NOTHING
    if not isinstance(parsed, dict):
        return _NOTHING
    env: dict[str, str] = {}
    if label == CLAUDE_JSON:
        env = _string_entries(parsed.get("env"))
    if label == CLAUDE_SETTINGS:
        parsed = parsed.get("env")  # of settings.json, only its env object is read
        if not isinstance(parsed, dict):
            return _NOTHING
        env = _string_entries(parsed)
    references: tuple[tuple[str, str], ...] = ()
    if label in REFERENCE_MAPS:
        within, key, how = REFERENCE_MAPS[label]
        references = tuple(
            (how, value)
            for header_map in _header_maps(parsed, within, key)
            for value in header_map.values()
            if isinstance(value, str) and value
        )
    return _CredentialFile(tuple(_credential_values(label, parsed, env)), references, env)


def _credential_values(label: str, parsed: dict[Any, Any], env: Mapping[str, str]) -> list[str]:
    """The values of a parsed credential file (of settings.json, its env object); env holds the string entries of the
    env object that Claude Code adds to its environment, that of .claude.json or settings.json, and is empty for the
    other files."""
    if label in CREDENTIAL_ONLY_LABELS:
        values = _strings(parsed, CREDENTIAL_ONLY_MIN_CHARS, keyed=False)
    else:
        values = _strings(parsed, SECRET_MIN_CHARS, keyed=True)
    # The header lists of the env objects of .claude.json and settings.json, which Claude Code applies to its own
    # environment, as an environment's (environment_pairs); counted under the file's label, as every value of the file.
    for name, value in env.items():
        values.extend(header_list_values(name, value))
    if label in HEADER_MAPS:
        values.extend(
            value
            for header_map in _header_maps(parsed, *HEADER_MAPS[label])
            for value in header_map.values()
            if isinstance(value, str) and len(value) >= SECRET_MIN_CHARS
        )
    # A URL with a password among the values read, whatever its key (an HTTPS_PROXY in settings.json env or in
    # config.toml's shell_environment_policy.set, which the CLI and every command the worker runs then see, or a
    # provider's or an MCP server's URL), also gives its user:pass and its password, as such a URL in an environment
    # does (environment_pairs).
    values.extend(part for value in _strings(parsed, 0, keyed=False) for part in url_userinfo_values(value))
    return values


def _read_credential_file(label: str, path: str) -> bytes | None:
    """The file's bytes, or None. Opened with O_NONBLOCK, so a FIFO never blocks, and checked with fstat first."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > CREDENTIAL_FILE_MAX_BYTES:
            _log_oversized(label, path, info.st_size)
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= CREDENTIAL_FILE_MAX_BYTES and (chunk := os.read(fd, CREDENTIAL_FILE_MAX_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
        if size > CREDENTIAL_FILE_MAX_BYTES:  # it grew after fstat
            _log_oversized(label, path, size)
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _log_oversized(label: str, path: str, size: int) -> None:
    with _oversized_lock:
        first = path not in _oversized_logged
        _oversized_logged.add(path)
    if first:
        LOG.warning("repair shadow credential file too large, skipped file=%s bytes=%s", label, size)


def _header_maps(parsed: Any, within: str | None, key: str) -> list[dict[Any, Any]]:
    """Every object found under `key` at any depth; with `within`, only the `key` objects of the entries of an object
    found under `within` (HEADER_MAPS, REFERENCE_MAPS). Walked with an explicit stack, never by recursion, as
    _strings is. A map that is not an object is left out."""
    maps: list[Any] = []
    stack: list[Any] = [parsed]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for name, child in item.items():
                if within is None and name == key:
                    maps.append(child)
                elif within is not None and name == within and isinstance(child, dict):
                    maps.extend(entry.get(key) for entry in child.values() if isinstance(entry, dict))
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return [header_map for header_map in maps if isinstance(header_map, dict)]


def _resolved(
    references: Iterable[tuple[str, str]], env: Mapping[str, str], layers: Sequence[Mapping[str, str]] = ()
) -> list[str]:
    """The values that a header map's references take in env, as the file's CLI resolves them (REFERENCE_MAPS), each
    of at least SECRET_MIN_CHARS and not only white space (no credential, and as a value to remove it would take the
    indentation out of every copied line). A name gives its variable's value when that is set and not empty (pi,
    codex); a value that starts with '!' is a shell command to pi, which is never run here. A template gives the
    value of each ${NAME} whose NAME is set, the default of each ${NAME:-default} whose NAME is not, the value that a
    set NAME has in each of layers (the environments that env merges, of which a later one hides an earlier one's
    value), and the whole value so expanded (Claude Code). The whole values of the distinct templates are taken in
    order while they come to at most EXPANDED_HEADERS_MAX_CHARS together (one that does not fit is skipped, a later
    one that fits is taken), and none is built from a text longer than that."""
    found: list[str] = []
    room = EXPANDED_HEADERS_MAX_CHARS
    for how, text in dict.fromkeys(references):
        if how == REFERENCE_NAME:
            if not text.startswith("!"):
                found.append(env.get(text) or "")
            continue
        parts, whole = _expanded_template(text, env, room, layers)
        found.extend(parts)
        if whole is not None:
            found.append(whole)
            room -= len(whole)
    # Each value is checked once, however many references give it.
    return [value for value in dict.fromkeys(found) if len(value) >= SECRET_MIN_CHARS and value.strip()]


def _expanded_template(
    text: str, env: Mapping[str, str], room: int, layers: Sequence[Mapping[str, str]] = ()
) -> tuple[list[str], str | None]:
    """What each ${NAME} or ${NAME:-default} of text is replaced with in env, as Claude Code 2.1.98 expands an MCP
    server's config (NAME's value when NAME is set, even empty; else the default, if there is one; else the reference
    stays as it is written), each once, with the value that each such set NAME has in each of layers; and the whole
    text so expanded, or None when the whole or the text it is built from would be longer than room characters.
    Nothing when no reference is replaced.

    Only the text up to its last '}' is searched: no reference ends after it, and a '${' with no '}' anywhere after it
    would make the search scan to the end of the text from each such '${', a time quadratic in the text's length (a
    local worker can write the file). Up to the last '}', every attempt stops at the next '}'. Each distinct reference
    is resolved once, and the whole's length is counted before the whole is built, so a short text whose references
    name a long value many times (a local worker can write both, the value in .claude.json's own env) is never built:
    neither time nor memory grows with the number of references times the length of the value they name."""
    end = text.rfind("}") + 1
    counts = Counter(match.group(1) for match in _TEMPLATE_RULE.finditer(text, 0, end))
    replacements: dict[str, str] = {}
    parts: dict[str, None] = {}
    length = len(text)
    for reference, count in counts.items():
        name, has_default, default = reference.partition(":-")  # the first ':-' ends the name, as in Claude Code
        value = env.get(name)
        if value is None and not has_default:
            continue
        replacements[reference] = default if value is None else value
        parts[replacements[reference]] = None
        parts.update(dict.fromkeys(layer[name] for layer in layers if name in layer))
        length += count * (len(replacements[reference]) - len(reference) - len("${}"))
    if not parts:
        return [], None
    if max(length, len(text)) > room:
        return list(parts), None

    def expand(match: re.Match[str]) -> str:
        replacement = replacements.get(match.group(1))
        return match.group() if replacement is None else replacement

    return list(parts), _TEMPLATE_RULE.sub(expand, text[:end]) + text[end:]


def _string_entries(value: Any) -> dict[str, str]:
    """The entries of an object whose name and value are strings; nothing for anything but an object."""
    if not isinstance(value, dict):
        return {}
    return {name: item for name, item in value.items() if isinstance(name, str) and isinstance(item, str)}


def _strings(value: Any, min_chars: int, *, keyed: bool) -> list[str]:
    """The string values in value at any depth, walked with an explicit stack, never by recursion.

    json.loads accepts nesting far deeper than Python's recursion limit. With keyed, only the strings whose own key
    (for an item of a list, the list's key) is a secret name, key or apiKey count.
    """
    found: list[str] = []
    stack: list[tuple[Any, str | None]] = [(value, None)]
    while stack:
        item, key = stack.pop()
        if isinstance(item, dict):
            stack.extend((child, name if isinstance(name, str) else None) for name, child in item.items())
        elif isinstance(item, list):
            stack.extend((child, key) for child in item)
        elif (
            isinstance(item, str)
            and len(item) >= min_chars
            and (not keyed or (key is not None and (secret_name(key) or key in ("key", "apiKey"))))
        ):
            found.append(item)
    return found
