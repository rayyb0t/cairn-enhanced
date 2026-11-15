"""Exact-value redaction of the transcript copies (spec 8.8, Redaction).

Every byte a copy takes from a worker's session file passes through redact_exact before it is written, with the
values that secret_values collects: the secret values of every configured worker's env (common_env included), in
local mode those of the dispatcher's own environment and of the local CLIs' credential files, the dispatcher's
repair token, and each reviewer's REVIEW_API_KEY, plus the user:pass and the password of every URL among those
environments' and those files' values, of dispatch.yaml's server, and of each reviewer's REVIEW_BASE_URL and proxy.
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
from collections.abc import Iterable, Mapping
from typing import Any
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

_oversized_logged: set[str] = set()
_oversized_lock = threading.Lock()


def secret_name(name: str) -> bool:
    """Whether a variable or key named name holds a secret."""
    return SECRET_NAME_RULE.search(name) is not None


def environment_pairs(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """(name, value) of the secret values of one environment, in its order.

    Each value of at least SECRET_MIN_CHARS whose name matches the secret-name rule, and, for every value that is a
    URL with a password (scheme://user:pass@host), its user:pass and its password, each of at least
    SECRET_MIN_CHARS, under the variable's name. A percent-encoded password also gives its decoded form.
    """
    pairs: list[tuple[str, str]] = []
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if len(value) >= SECRET_MIN_CHARS and secret_name(name):
            pairs.append((name, value))
        pairs.extend((name, part) for part in url_userinfo_values(value))
    return pairs


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
    environment's HOME; a file shared by several workers is read once. A file that is missing, unreadable, not a
    regular file, larger than CREDENTIAL_FILE_MAX_BYTES or unparsable is skipped, as is one whose reading, parsing or
    walking raises anything else, so a credential file never raises out of here.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for worker in config.workers:
        merged = {**environ, **worker.env}
        for label, path in credential_paths(merged):
            if path in seen:
                continue
            seen.add(path)
            try:
                pairs.extend((label, value) for value in _credential_values(label, path))
            except Exception:  # noqa: S112 - a credential file never stops the reporter thread
                continue
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


def _credential_values(label: str, path: str) -> list[str]:
    data = _read_credential_file(label, path)
    if data is None:
        return []
    try:
        if label == CODEX_CONFIG:
            parsed: Any = tomllib.loads(data.decode("utf-8"))
        else:
            parsed = json.loads(data)
    except (ValueError, RecursionError):
        return []
    if not isinstance(parsed, dict):
        return []
    if label == CLAUDE_SETTINGS:
        parsed = parsed.get("env")  # of settings.json, only its env object is read
        if not isinstance(parsed, dict):
            return []
    if label in CREDENTIAL_ONLY_LABELS:
        values = _strings(parsed, CREDENTIAL_ONLY_MIN_CHARS, keyed=False)
    else:
        values = _strings(parsed, SECRET_MIN_CHARS, keyed=True)
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
