"""Build step RB1, the final review's part B: the reviewer's URL, the state folder and the pinned line.

- R3_SECURITY-1. The agent shows a reviewer's endpoint (`model.endpoint_of`), tests it for plain http to a host that
  is not loopback, and pins it, all with urllib.parse; requests prepares each request with urllib3's `parse_url`, and
  connects to the host that one reads. A URL that the two read differently is a startup error that names its field,
  for every reviewer's `base_url` and `proxy` and the settings file's `server` (`config.url_problem`): one that holds
  a backslash (`must not contain a backslash`), which urllib3 takes for the end of the host and urllib.parse does not,
  and one whose scheme, host or port the two read differently, or that `parse_url` cannot read. For every URL the agent
  accepts, the endpoint it shows is where requests connects. The pinned line says whether a proxy and a CA file are
  pinned (`set` or `none`), never their values.
- R3_SECURITY-2. A `state.dir` that ends in `/` or holds a `.` or `..` component is a startup error that names
  `state.dir`, so `os.lstat` in the isolation check sees the folder's own entry; and the pin opens the state folder
  with O_NOFOLLOW, so a symlinked folder is refused there even where the isolation check's reason was waived or
  missed, with a remedy that replaces the link (the folder's own remedy, `install -d`, would follow it).
- The review of RB1 (fix round 1): a host that requests rewrites when it prepares a request (a character it
  percent-encodes, an IPv6 zone id) is refused too; and before the environment guard, which urllib3's URL parser now
  precedes, no TLS context exists, requests is not loaded and no guarded variable is read.
- The review of RB1 (fix round 2, RECHECK-1): a host that starts with `*`, which the two parsers read alike but to
  which requests prepares no request (`URL has an invalid label.`), is a startup error that names its field, as a host
  with an empty label is, where the agent used to start and then fail every review or poll; and over seeded random
  URLs, requests prepares a request to every one the agent accepts, for the endpoint shown.

Every value is synthetic: `listener.example` and `provider.example` hosts, documentation addresses (192.0.2.0/24,
2001:db8::/32), loopback stubs and made-up keys.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import random
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from agentkit import (
    EXTEND,
    CairnStub,
    ModelStub,
    Reply,
    cairn_stubs,
    chat_answer,
    configure,
    model_stubs,
    run_view,
    write_run_copy,
)
from conftest import REVIEWER_KEY, Layout, reviewer_entry, settings_data, write_bytes
from urllib3.util import parse_url

from cairn_repair_agent import cli, config, envguard, isolation, model, pin

PIN = pin.PIN_FILE_NAME
PROJECT = Path(__file__).resolve().parents[1]
BACKSLASH = "must not contain a backslash"
READ_DIFFERENTLY = "has a scheme, host or port that requests reads differently"
CANNOT_READ = "has a host or port that requests cannot read"
FOLDER_REMEDY = (
    "repair reviewer pin: the agent cannot use its state folder; check state.dir in repair.yaml, and as root make it "
    "the agent's own folder with: install -d -m 0700 -o {user} -g {group} {folder}"
)
FOLDER_SYMLINK_REMEDY = (
    "repair reviewer pin: the agent's state folder is a symlink, which it does not follow; check state.dir in "
    "repair.yaml, and as root replace the link with a folder of the agent's own: rm {folder} && install -d -m 0700 "
    "-o {user} -g {group} {folder}"
)
CA_PEM = "-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n"


@pytest.fixture
def model_stub() -> Iterator[Callable[..., ModelStub]]:
    yield from model_stubs()


@pytest.fixture
def cairn_stub() -> Iterator[CairnStub]:
    yield from cairn_stubs()


def settings_with(field: str, url: str) -> dict[str, Any]:
    """The layout's settings file with `url` as the server, or as the one reviewer's `base_url` or `proxy`."""
    if field == "server":
        return settings_data(server=url)
    return settings_data(reviewers=[reviewer_entry(**{field: url})])


# Where a startup error of the settings file names each field.
WHERE = {"server": "server", "base_url": "reviewers[0].base_url", "proxy": "reviewers[0].proxy"}
URL_FIELDS = list(WHERE)


# Distinctive pieces of the URLs below, none of which may reach a message.
URL_PIECES = ("listener", "provider.example", "192.0.2.7", "%2e", "%31", "eth0", "canary")


def refused_at_start(layout: Layout, field: str, url: str) -> str:
    """Start the agent with `url` in `field`: it stops at its start with a settings-file error that names the field,
    holds none of the URL, logs no endpoint and builds no client. Returns the error's rule."""
    layout.write_settings(settings_with(field, url))
    run = layout.start("--allow-shared-user")
    assert not run.started
    prefix = f"agent settings invalid: {layout.settings_path}: {WHERE[field]}: "
    (line,) = [line for line in run.stderr.splitlines() if line.startswith(prefix)]
    assert url not in run.text()
    assert [piece for piece in URL_PIECES if piece in url and piece in run.text()] == []
    assert run.messages("repair reviewer pinned") == [] and run.messages("repair agent started") == []
    assert run.messages("repair model endpoint is plain http") == []
    return line.removeprefix(prefix)


def default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def connects_to(url: str) -> str:
    """The `scheme://host:port` that requests connects to for `url`: urllib3's `parse_url`, with which requests
    prepares a request, with the scheme's default port when the URL gives none, as urllib3's pool manager takes it."""
    theirs = parse_url(url)
    assert theirs.scheme is not None and theirs.host is not None
    return f"{theirs.scheme}://{theirs.host}:{theirs.port or default_port(theirs.scheme)}"


def prepared_endpoint(url: str) -> str:
    """The same, read back from the URL that requests actually prepares, whose host and port its connection pool
    takes (requests 2.34.2 `HTTPAdapter.build_connection_pool_key_attributes`)."""
    prepared = requests.Request("POST", url.rstrip("/") + "/chat/completions").prepare().url
    assert prepared is not None
    parts = urlsplit(prepared)
    host = parts.hostname or ""
    return f"{parts.scheme}://{f'[{host}]' if ':' in host else host}:{parts.port or default_port(parts.scheme)}"


def loopback_for_requests(url: str) -> bool:
    """Whether requests connects to `localhost`, `::1` or an address in 127.0.0.0/8 for `url` (§9.3's test, applied to
    the host that `parse_url` reads)."""
    host = (parse_url(url).host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in ipaddress.ip_network("127.0.0.0/8") if address.version == 4 else address == model.LOOPBACK_V6


# --- R3_SECURITY-1: the URL rules -------------------------------------------------------------------------------------

# A backslash before the host (the finding's shape: urllib.parse reads the host after the `\@`, urllib3 the one
# before it), and one in the path, where the two read the same host: only the backslash rule refuses that one.
BACKSLASH_PLACES = {
    "before_the_host": "http://listener.example\\@127.0.0.1:{port}/v1",
    "in_the_path": "http://127.0.0.1:{port}/v1\\chat",
}


@pytest.mark.parametrize("place", list(BACKSLASH_PLACES))
@pytest.mark.parametrize("field", URL_FIELDS)
def test_a_url_with_a_backslash_is_a_startup_error_that_names_its_field(layout: Layout, field: str, place: str) -> None:
    """R3_SECURITY-1: a `server`, `base_url` or `proxy` that holds a backslash, before the host or in the path, stops
    the agent at its start with `agent settings invalid: <path>: <field>: …; this one must not contain a backslash`,
    which holds none of the URL; nothing is pinned or started."""
    url = BACKSLASH_PLACES[place].format(port=8000)
    assert config.url_problem(url) == BACKSLASH
    rule = refused_at_start(layout, field, url)
    assert rule.endswith(f"; this one {BACKSLASH}"), rule
    assert sorted(os.listdir(layout.state_dir)) == []


# The finding's two shapes: a hosted reviewer whose shown endpoint would be the owner's while requests connects to the
# listener before the backslash; and a local one shown as loopback, so that the plain-http WARNING would be left out,
# while requests sends the digest and the key to another address in clear text.
FINDING_SHAPES = {
    "hosted": ("https://listener.example\\@api.provider.example", "https://api.provider.example:443"),
    "local": ("http://192.0.2.7:8080\\@127.0.0.1:11434/v1", "http://127.0.0.1:11434"),
}


@pytest.mark.parametrize("shape", list(FINDING_SHAPES))
def test_the_findings_two_shapes_are_refused(layout: Layout, shape: str) -> None:
    """R3_SECURITY-1, its reproduction: `https://listener.example\\@<owner host>` (with `anthropic_messages` and a key,
    as the owner's hosted reviewer) and `http://<other address>:<port>\\@127.0.0.1:11434/v1` read as the owner's
    endpoint by urllib.parse and as another host by requests; both are refused at the start, by the backslash rule,
    before any line shows an endpoint, the plain-http test or a pin."""
    url, shown_by_urllib_parse = FINDING_SHAPES[shape]
    assert model.endpoint_of(url) == shown_by_urllib_parse  # what the agent would have shown
    assert connects_to(url) != shown_by_urllib_parse  # and where requests would have connected
    assert config.url_problem(url) == BACKSLASH
    if shape == "local":
        assert not model.is_plain_http_on_a_non_loopback_host(url) and not loopback_for_requests(url)
    entry = reviewer_entry(base_url=url)
    if shape == "hosted":
        entry.update(api_format="anthropic_messages", api_key=REVIEWER_KEY)
    layout.write_settings(settings_data(reviewers=[entry]))
    run = layout.start("--allow-shared-user")
    assert not run.started
    assert (
        f"agent settings invalid: {layout.settings_path}: reviewers[0].base_url: reviewer reviewer_local: "
        in run.stderr
    )
    assert run.stderr.rstrip().endswith(f"; this one {BACKSLASH}"), run.stderr
    assert "listener.example" not in run.text() and "192.0.2.7" not in run.text()
    assert run.messages("repair reviewer pinned") == [] and run.messages("repair model endpoint") == []
    assert os.listdir(layout.state_dir) == []


def test_a_backslash_url_sends_nothing_anywhere(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R3_SECURITY-1, end to end with `cairn-repair --once` on loopback stubs: the finding's local shape, with the
    owner's model server after the `\\@` and a listener before it, and a run listed for review. Before RB1 the
    listener received the digest and the key and its answer was posted; now the agent exits with code 2 at its start,
    and neither model stub, nor the Cairn stub, gets a request, and nothing is pinned."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    owner = model_stub(Reply(body=chat_answer(EXTEND)))
    listener = model_stub(Reply(body=chat_answer(EXTEND)))
    base_url = f"{listener.url}\\@{owner.url.removeprefix('http://')}/v1"
    assert connects_to(base_url) == listener.url and model.endpoint_of(base_url) == owner.url
    configure(layout, cairn_stub.url, reviewers=[reviewer_entry(base_url=base_url, api_key=REVIEWER_KEY)])
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)

    result: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: result.update(code=cli.main(layout.argv("--allow-shared-user", "--once"))), daemon=True
    )
    thread.start()
    thread.join(60)
    assert not thread.is_alive()
    stderr = capsys.readouterr().err
    assert result["code"] == 2
    assert "reviewers[0].base_url: reviewer reviewer_local: " in stderr and BACKSLASH in stderr
    assert owner.requests == [] and listener.requests == [] and cairn_stub.requests == []
    assert os.listdir(layout.state_dir) == []
    messages = [record.getMessage() for record in caplog.records if record.name.startswith("cairn_repair_agent")]
    assert [m for m in messages if m.startswith(("repair reviewer pinned", "repair agent started"))] == []


# URLs that urllib.parse and urllib3 read with different hosts although no backslash is in them, or that parse_url
# cannot read: only the second rule refuses them. (A percent-encoded host that requests decodes, so that the shown
# endpoint would name `192%2e0%2e2%2e7` while requests connects to 192.0.2.7; a loopback address written so that the
# plain-http test cannot recognise it; an IPv6 zone id, which urllib3 unquotes; and an invalid percent sign.)
READ_OTHERWISE = {
    "percent_encoded_address": ("http://192%2e0%2e2%2e7:8080/v1", READ_DIFFERENTLY),
    "percent_encoded_loopback": ("http://%31%32%37.0.0.1:11434/v1", READ_DIFFERENTLY),
    "ipv6_zone_id": ("http://[fe80::1%25eth0]:8080/v1", READ_DIFFERENTLY),
    "unreadable_percent": ("http://listener%zz-canary.example:8080/v1", CANNOT_READ),
}


@pytest.mark.parametrize("case", list(READ_OTHERWISE))
@pytest.mark.parametrize("field", URL_FIELDS)
def test_a_url_that_requests_reads_differently_is_a_startup_error(layout: Layout, field: str, case: str) -> None:
    """R3_SECURITY-1: a `server`, `base_url` or `proxy` whose scheme, host or port urllib3's `parse_url` (requests'
    parser) reads differently from urllib.parse (the agent's), or that `parse_url` cannot read, stops the agent at its
    start with a rule that names the field and holds none of the URL."""
    url, problem = READ_OTHERWISE[case]
    if problem == READ_DIFFERENTLY:
        assert model.endpoint_of(url) != connects_to(url)
    else:
        with pytest.raises(ValueError):
            parse_url(url)
    assert config.url_problem(url) == problem
    rule = refused_at_start(layout, field, url)
    assert rule.endswith(f"; this one {problem}"), rule


# Hosts that urllib.parse and `parse_url` read alike, but that requests rewrites when it prepares the request (the
# review of RB1, SAFETY-4): a character it percent-encodes (`utils.requote_uri`), so that its connection pool takes
# `listener%7Bcanary.example` for the shown `listener{canary.example`; and an IPv6 zone id, whose escape it decodes
# (`[2001:db8::1%31]` is 2001:db8::11 to it) or whose `%` it encodes. Only the host-character rule refuses them.
REWRITTEN_BY_REQUESTS = {
    "double_quote": 'http://listener"canary.example:8080/v1',
    "caret": "http://listener^canary.example:8080/v1",
    "backquote": "http://listener`canary.example:8080/v1",
    "open_brace": "http://listener{canary.example:8080/v1",
    "pipe": "http://listener|canary.example:8080/v1",
    "close_brace": "http://listener}canary.example:8080/v1",
    "zone_id_escape": "http://[2001:db8::1%31]:8080/v1",
    "zone_id": "http://[fe80::1%eth0]:8080/v1",
}


@pytest.mark.parametrize("case", list(REWRITTEN_BY_REQUESTS))
@pytest.mark.parametrize("field", URL_FIELDS)
def test_a_url_whose_host_requests_rewrites_is_a_startup_error(layout: Layout, field: str, case: str) -> None:
    """The review of RB1 (SAFETY-4): a `server`, `base_url` or `proxy` whose host the two parsers read alike, but
    which requests rewrites when it prepares the request, so that it connects to another name or address than the
    endpoint shown, stops the agent at its start with the rule of a URL that requests reads differently, which names
    the field and holds none of the URL."""
    url = REWRITTEN_BY_REQUESTS[case]
    assert model.endpoint_of(url).lower() == connects_to(url).lower()  # urllib.parse and parse_url agree
    assert model.endpoint_of(url).lower() != prepared_endpoint(url).lower()  # but requests connects elsewhere
    assert config.url_problem(url) == READ_DIFFERENTLY
    rule = refused_at_start(layout, field, url)
    assert rule.endswith(f"; this one {READ_DIFFERENTLY}"), rule


# Hosts that start with `*` (the review of RB1, RECHECK-1): urllib.parse and `parse_url` read them alike, and requests
# changes none of their characters, but it prepares no request to such a host (requests 2.34.2
# `PreparedRequest.prepare_url`: `URL has an invalid label.`), so every review, or every poll, would fail. A proxy's
# host requests does not check: it asks the resolver for the literal name. The agent refuses such a host in all three
# fields, as it refuses a host with an empty label, and only the star rule refuses these; a `*` later in the host is
# accepted (`ACCEPTED`).
STAR_HOST = "has a host that starts with *"
STAR_HOSTS = {
    "wildcard_label": "http://*.listener.example:8080/v1",
    "star_before_a_name": "https://*canary.provider.example/v1",
    "star_alone": "http://*:8000",
}


@pytest.mark.parametrize("case", list(STAR_HOSTS))
@pytest.mark.parametrize("field", URL_FIELDS)
def test_a_url_whose_host_starts_with_a_star_is_a_startup_error(layout: Layout, field: str, case: str) -> None:
    """The review of RB1 (RECHECK-1): a `server`, `base_url` or `proxy` whose host starts with `*` stops the agent at
    its start with `…; this one has a host that starts with *`, which names the field and holds none of the URL, and
    nothing is pinned. The agent used to start, and then requests refused every request to such a `server` or
    `base_url` as it prepared it."""
    url = STAR_HOSTS[case]
    assert model.endpoint_of(url).lower() == connects_to(url).lower()  # urllib.parse and parse_url agree
    with pytest.raises(requests.exceptions.InvalidURL, match="URL has an invalid label"):
        requests.Request("POST", url.rstrip("/") + "/chat/completions").prepare()
    assert config.url_problem(url) == STAR_HOST
    rule = refused_at_start(layout, field, url)
    assert rule.endswith(f"; this one {STAR_HOST}"), rule
    assert os.listdir(layout.state_dir) == []


# URLs the agent accepts: user information, IPv4 and IPv6 literals, explicit, default, empty and zero-padded ports,
# upper-case hosts, a trailing dot, Compose's host name, and every other character RFC 3986 allows in a host
# (`HOST_CHARACTERS`), which requests leaves as it is: `*` included where it does not start the host (requests
# refuses a host that starts with one, and so does the agent: `STAR_HOSTS`).
ACCEPTED = [
    "http://reviewer_1!$&'()*+,;=~.example:8080/v1",
    "http://[::ffff:192.0.2.7]:8080/v1",
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434/v1",
    "https://api.provider.example",
    "https://API.Provider.Example:8443/v1",
    "https://user-canary:password-canary@api.provider.example/v1",
    "http://user-canary@REVIEWER.EXAMPLE:8080/v1/deep-path",
    "http://[::1]:11434/v1",
    "http://[2001:DB8::7]/v1",
    "https://user-canary:password-canary@[2001:db8::7]:8443/v1",
    "http://127.0.0.1:0080/v1",
    "http://reviewer.example:/v1",
    "http://reviewer.example.:8080/v1",
    "http://192.0.2.7:8080/v1",
    "http://host.docker.internal:11434/v1",
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_the_shown_endpoint_is_where_requests_connects(url: str) -> None:
    """R3_SECURITY-1: for every URL the agent accepts, the endpoint it shows (on the pinned line, the started line and
    the plain-http WARNING: `model.endpoint_of`) equals `parse_url`'s `scheme://host:port`, and the host and port of
    the URL that requests prepares; and the plain-http test is the one for the host requests connects to."""
    assert config.url_problem(url) is None
    shown = model.endpoint_of(url)
    assert shown == connects_to(url) == prepared_endpoint(url)
    plain_http_elsewhere = parse_url(url).scheme == "http" and not loopback_for_requests(url)
    assert model.is_plain_http_on_a_non_loopback_host(url) is plain_http_elsewhere
    for hidden in ("user-canary", "password-canary", "deep-path", "/v1"):
        assert hidden not in shown


# Accepted URLs whose host urllib3 writes in other letters than urllib.parse: urllib3 upper-cases a percent escape, and
# keeps the case of a numeric host such as 0X7F.0.0.1, which urllib.parse lower-cases. The rule compares hosts in any
# letter case, which neither DNS nor the system's address parser tells apart, so these name the host requests
# connects to, in other letters.
ACCEPTED_IN_OTHER_LETTERS = ["http://a%2fb.example:8080/v1", "http://0X7F.0.0.1:11434/v1"]


@pytest.mark.parametrize("url", ACCEPTED_IN_OTHER_LETTERS)
def test_the_shown_endpoint_names_requests_host_in_any_letter_case(url: str) -> None:
    """R3_SECURITY-1: the host rule ignores letter case. Such a URL is accepted; its shown endpoint differs from
    `parse_url`'s only in letter case, and the plain-http test is the one for the host requests connects to."""
    assert config.url_problem(url) is None
    shown, theirs = model.endpoint_of(url), connects_to(url)
    assert shown != theirs and shown.lower() == theirs.lower() == prepared_endpoint(url).lower()
    plain_http_elsewhere = parse_url(url).scheme == "http" and not loopback_for_requests(url)
    assert model.is_plain_http_on_a_non_loopback_host(url) is plain_http_elsewhere


# The pieces of a URL's host, port and path that matter to the two parsers and to requests (the reviews' fuzz of RB1).
# No `@`: this is about the address, and requests reads user information only for a Basic auth header.
URL_FUZZ_PIECES = (
    "a", "b", "z", "Z", "0", "1", "9", ".", "-", "_", "~", "%", "%25", "%2e", "%2E", "%41", "%7b", "%5c", ":", "[",
    "]", "::1", "127.0.0.1", "0x7f", "localhost", "/", "?", "#", "!", "$", "&", "'", "(", ")", "*", "+", ",", ";", "=",
    "{", "}", "|", "^", "`", '"', "80", "443", "65535", "eth0", "example", "xn--", "fe80::1",
)  # fmt: skip


def test_requests_prepares_every_accepted_url_for_the_endpoint_shown() -> None:
    """R3_SECURITY-1 and the review of RB1 (SAFETY-4, RECHECK-1), over 20,000 seeded random URLs built from those
    pieces: `url_problem` never raises, and for every URL it accepts requests prepares a request (none is refused, as
    one to a host that starts with `*` was), whose scheme, host (in any letter case) and port are the endpoint shown."""
    rng = random.Random(20261116)
    accepted = 0
    for _ in range(20000):
        body = "".join(rng.choice(URL_FUZZ_PIECES) for _ in range(rng.randint(1, 8)))
        url = rng.choice(("http://", "https://")) + body + rng.choice(("", "/v1", "/"))
        if config.url_problem(url) is not None:
            continue
        accepted += 1
        try:
            prepared = prepared_endpoint(url)
        except requests.exceptions.RequestException as exc:
            pytest.fail(f"requests prepares no request to the accepted {url!r}: {exc}")
        assert model.endpoint_of(url).lower() == prepared.lower(), url
    assert accepted > 4000, accepted


@pytest.mark.reviewer_pin
def test_the_lines_show_the_endpoint_that_receives_the_review(
    layout: Layout,
    cairn_stub: CairnStub,
    model_stub: Callable[..., ModelStub],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R3_SECURITY-1, end to end with `cairn-repair --once`: with a `base_url` that holds user information and a path,
    the pinned line and the started line show `scheme://host:port` of the model stub that then receives the review's
    request, the address requests connected to, and `extend` is posted."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    stub = model_stub(Reply(body=chat_answer(EXTEND)))
    base_url = f"http://user-canary:password-canary@{stub.url.removeprefix('http://')}/v1"
    configure(layout, cairn_stub.url, reviewers=[reviewer_entry(base_url=base_url)])
    cairn_stub.runs = [run_view()]
    write_run_copy(layout.root)

    result: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: result.update(code=cli.main(layout.argv("--allow-shared-user", "--once"))), daemon=True
    )
    thread.start()
    thread.join(60)
    assert not thread.is_alive()
    assert result["code"] == 0, capsys.readouterr().err
    messages = [record.getMessage() for record in caplog.records if record.name.startswith("cairn_repair_agent")]
    assert stub.url == connects_to(base_url)
    assert [message for message in messages if message.startswith("repair reviewer pinned")] == [
        f"repair reviewer pinned reviewer=reviewer_local endpoint={stub.url} proxy=none ca_bundle=none"
    ]
    (started,) = [message for message in messages if message.startswith("repair agent started")]
    assert f" endpoint={stub.url} " in started
    assert len(stub.requests) == 1 and [body["decision"] for body in cairn_stub.posted()] == ["extend"]
    assert not any("user-canary" in message or "password-canary" in message for message in messages)


# --- The review of RB1: what the start does before the environment guard -------------------------------------------

# A fresh interpreter, as AG4's `test_model_client_ignores_worker_env_in_a_fresh_interpreter` runs the agent: from the
# moment it starts, it counts every TLS context made and every read of a guarded variable (argv[1], a JSON list), by a
# key, `get`, `in` or the items of `os.environ`; and it looks at both, and at whether requests is loaded, when
# `cli.start` reaches the environment guard (§9.3 step 3).
GUARD_DRIVER = """
import json, os, ssl, sys
GUARDED = frozenset(json.loads(sys.argv[1]))
contexts, reads, watching = [], [], [True]
new = ssl.SSLContext.__new__
def counted(cls, *args, **kwargs):
    contexts.append(cls.__name__)
    return new(cls, *args, **kwargs)
ssl.SSLContext.__new__ = staticmethod(counted)
class Watched(type(os.environ)):
    def __getitem__(self, key):
        if watching[0] and key.upper() in GUARDED:
            reads.append(key)
        return super().__getitem__(key)
plain = os.environ
os.environ = Watched(plain._data, plain.encodekey, plain.decodekey, plain.encodevalue, plain.decodevalue)
from cairn_repair_agent import cli, envguard
seen = {}
strip = envguard.strip_environment
def at_the_guard(*args, **kwargs):
    seen.update(contexts=len(contexts), reads=sorted(set(reads)), requests="requests" in sys.modules)
    watching[0] = False
    return strip(*args, **kwargs)
envguard.strip_environment = at_the_guard
agent = cli.start(cli.build_parser().parse_args(sys.argv[2:]))
seen.update(started=agent.model.endpoint)
print(json.dumps(seen))
"""


def test_before_the_environment_guard_no_tls_context_exists_and_no_guarded_variable_is_read(layout: Layout) -> None:
    """The review of RB1 (SPEC-3, SAFETY-5): the settings file's URL check imports urllib3's URL parser, so urllib3
    is now loaded before the environment guard, not only after it. That leaves the guard's purpose intact, which this
    holds: when the start reaches the guard, with every guarded variable set (`SSLKEYLOGFILE` among them, and the
    reviewer and the server at https URLs), no TLS context has been made, requests is not loaded, and none of those
    variables has been read; the key-log file is never written."""
    keylog = layout.base / "tls-keys-canary.log"
    layout.write_settings(
        settings_data(server="https://127.0.0.1:9", reviewers=[reviewer_entry(base_url="https://127.0.0.1:9/v1")])
    )
    bogus_ca = str(layout.base / "bogus-ca.pem")
    env = {name: value for name, value in os.environ.items() if name.upper() not in envguard.GUARDED_VARIABLES}
    env.update({name: "http://127.0.0.1:9" for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY")})
    env.update(
        no_proxy="example.invalid", SSL_CERT_FILE=bogus_ca, SSL_CERT_DIR=str(layout.base), SSLKEYLOGFILE=str(keylog)
    )
    env.update(REQUESTS_CA_BUNDLE=bogus_ca, CURL_CA_BUNDLE=bogus_ca, NETRC=str(layout.base / "netrc"))
    assert {name.upper() for name in env} >= envguard.GUARDED_VARIABLES
    guarded = json.dumps(sorted(envguard.GUARDED_VARIABLES))
    result = subprocess.run(
        [sys.executable, "-c", GUARD_DRIVER, guarded, *layout.argv("--allow-shared-user")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=PROJECT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert seen == {"contexts": 0, "reads": [], "requests": False, "started": "https://127.0.0.1:9"}, seen
    assert not keylog.exists()


# --- R3_SECURITY-1: the pinned line -----------------------------------------------------------------------------------

PROXY_URL = "http://proxy-user-canary:proxy-password-canary@127.0.0.1:3128"


@pytest.mark.reviewer_pin
@pytest.mark.parametrize(
    ("proxy", "ca_bundle"), [(False, False), (True, False), (False, True), (True, True)], ids=lambda flag: str(flag)
)
def test_the_pinned_line_says_whether_a_proxy_and_a_ca_file_are_pinned(
    layout: Layout, proxy: bool, ca_bundle: bool
) -> None:
    """R3_SECURITY-1: at the first start the pinned line ends ` proxy=<set|none> ca_bundle=<set|none>`, so the owner
    sees at the moment of trust on first use, besides the reviewer's name and endpoint, whether a proxy or a CA file
    also decides where digests go; it never shows the proxy URL, which holds a user name and password here, or the CA
    file's path, which only the pin file holds (the owner reads it as root). A matching second start logs no pinned
    line."""
    ca_path = str(write_bytes(layout.base / "ca-path-canary.pem", CA_PEM, mode=0o644))
    entry = reviewer_entry(base_url="http://127.0.0.1:11434/v1")
    if proxy:
        entry["proxy"] = PROXY_URL
    if ca_bundle:
        entry["ca_bundle"] = ca_path
    layout.write_settings(settings_data(reviewers=[entry]))

    first = layout.start("--allow-shared-user")
    assert first.started, first.text()
    expected = (
        "repair reviewer pinned reviewer=reviewer_local endpoint=http://127.0.0.1:11434 "
        f"proxy={'set' if proxy else 'none'} ca_bundle={'set' if ca_bundle else 'none'}"
    )
    assert first.messages("repair reviewer pinned") == [expected]
    for hidden in (
        PROXY_URL,
        "proxy-user-canary",
        "proxy-password-canary",
        "127.0.0.1:3128",
        ca_path,
        "ca-path-canary",
    ):
        assert hidden not in first.text(), hidden
    pinned = (layout.state_dir / PIN).read_text(encoding="utf-8")
    assert ("proxy-password-canary" in pinned) is proxy  # the pin itself holds the values, as §11.3 says
    second = layout.start("--allow-shared-user")
    assert second.started and second.messages("repair reviewer pinned") == []


# --- R3_SECURITY-2: the state folder ----------------------------------------------------------------------------------

# Spellings of a state folder that do not end in its own name: os.lstat, and an open with O_NOFOLLOW, follow a
# symlinked folder named so.
NOT_ITS_OWN_NAME = {
    "trailing_slash": "{state}/",
    "two_trailing_slashes": "{state}//",
    "trailing_dot": "{state}/.",
    "trailing_dot_dot": "{state}/..",
    "dot_component": "{base}/./state",
    "dot_dot_component": "{base}/../{base_name}/state",
    "root": "/",
}
STATE_DIR_RULE = "must not end in / or hold a . or .. component"


@pytest.mark.parametrize("spelling", list(NOT_ITS_OWN_NAME))
@pytest.mark.parametrize("flags", [(), ("--allow-shared-user",)], ids=["checked", "allow_shared_user"])
def test_a_state_dir_that_does_not_end_in_its_own_name_is_a_startup_error(
    layout: Layout, spelling: str, flags: tuple[str, ...]
) -> None:
    """R3_SECURITY-2: a `state.dir` that ends in `/` or holds a `.` or `..` component (`/./`, `/../`) stops the agent
    at its start, with or without `--allow-shared-user`, with `agent config invalid: <path>: state.dir: …` before any
    isolation line, and nothing is pinned; the replay and the gate tools, which load the same file, refuse it too."""
    state = str(layout.state_dir)
    value = NOT_ITS_OWN_NAME[spelling].format(state=state, base=layout.base, base_name=layout.base.name)
    layout.write_config(state={"dir": value})
    run = layout.start(*flags)
    assert not run.started
    assert run.stderr == f"agent config invalid: {layout.config_path}: state.dir: {STATE_DIR_RULE}\n"
    assert run.records == []  # stopped before the isolation check and the pin
    assert os.listdir(layout.state_dir) == []
    with pytest.raises(config.StartupError, match=re.escape(f"state.dir: {STATE_DIR_RULE}")):
        config.load_config(str(layout.config_path))


def make_symlinked_state(layout: Layout) -> tuple[Path, Path]:
    """A real 0700 folder of the test's user and a symlink to it, as `state.dir` might name it."""
    target = layout.base / "state-target"
    target.mkdir()
    os.chmod(target, 0o700)
    link = layout.base / "state-link"
    link.symlink_to(target)
    return link, target


@pytest.mark.parametrize("spelling", ["{link}/", "{link}/.", "{link}//"])
def test_a_symlinked_state_folder_spelt_with_a_trailing_slash_is_refused(layout: Layout, spelling: str) -> None:
    """R3_SECURITY-2, the finding's reproduction: a symlinked state folder named with a trailing `/`, `/.` or `//`,
    which `os.lstat` would follow, giving no `state_dir_symlink`, and the pin's open would follow too, pinning in the
    link's target. It is now a startup error that names `state.dir`; spelt as the link itself, the isolation check
    reports `state_dir_symlink` as before."""
    link, target = make_symlinked_state(layout)
    assert isolation.state_dir_reasons(spelling.format(link=link)) == []  # what the spelling hid from the check
    assert isolation.state_dir_reasons(str(link)) == ["state_dir_symlink"]
    layout.write_config(state={"dir": spelling.format(link=link)})
    run = layout.start("--allow-shared-user")
    assert not run.started
    assert run.stderr == f"agent config invalid: {layout.config_path}: state.dir: {STATE_DIR_RULE}\n"
    assert os.listdir(target) == []


@pytest.mark.reviewer_pin
@pytest.mark.parametrize("how", ["missed", "waived"])
def test_a_symlinked_state_folder_is_refused_by_the_pins_open(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """R3_SECURITY-2: the pin opens the state folder with O_NOFOLLOW, so a symlinked folder is refused there even when
    the isolation check does not refuse it: its reasons missed (`isolation.check` and `state_dir_reasons` report
    nothing, and the agent runs without `--allow-shared-user`) or waived (`--allow-shared-user`, whose WARNING names
    `state_dir_symlink`). The start stops with `reason=unreadable:ENOTDIR` and the folder's remedy, before any client,
    and nothing is pinned in the link's target."""
    link, target = make_symlinked_state(layout)
    layout.write_config(state={"dir": str(link)})
    if how == "missed":
        monkeypatch.setattr(isolation, "check", lambda *args: [])
        monkeypatch.setattr(isolation, "state_dir_reasons", lambda state_dir: [])
        run = layout.start()
        assert run.messages("repair agent isolation check failed") == []
    else:
        run = layout.start("--allow-shared-user")
        (warning,) = run.messages("repair agent running without isolation reasons=")
        assert "state_dir_symlink" in warning.split("reasons=", 1)[1].split(",")
    assert not run.started
    user, group = pin.agent_user_and_group()
    assert run.messages("repair reviewer pin") == [
        f"repair reviewer pin unusable path={link / PIN} reason=unreadable:ENOTDIR",
        FOLDER_SYMLINK_REMEDY.format(user=user, group=group, folder=link),
    ]
    assert run.messages("repair agent started") == []
    assert os.listdir(target) == []


@pytest.mark.reviewer_pin
def test_the_symlinks_remedy_run_as_printed_gives_the_agent_a_folder_of_its_own(layout: Layout) -> None:
    """The review of RB1 (SAFETY-3): for a symlinked state folder, the folder's remedy (`install -d`, which RB1's
    first version printed for it too) follows the link and leaves it in place, so an owner who ran it got the same
    refusal again. The link's remedy, run as the line prints it (here as the test's user, who may give a folder to
    itself), replaces the link with a folder of the agent's own, 0700; the agent then starts and pins there, and the
    folder the link named is left as it was."""
    link, target = make_symlinked_state(layout)
    layout.write_config(state={"dir": str(link)})
    user, group = pin.agent_user_and_group()
    folder_command = FOLDER_REMEDY.format(user=user, group=group, folder=link).split("own folder with: ", 1)[1]
    assert subprocess.run(["sh", "-c", folder_command], check=False).returncode == 0
    assert link.is_symlink()  # what the folder's remedy leaves

    refused = layout.start("--allow-shared-user")
    assert not refused.started
    unusable, remedy = refused.messages("repair reviewer pin")
    assert unusable == f"repair reviewer pin unusable path={link / PIN} reason=unreadable:ENOTDIR"
    assert remedy == FOLDER_SYMLINK_REMEDY.format(user=user, group=group, folder=link)
    command = remedy.split("a folder of the agent's own: ", 1)[1]
    assert subprocess.run(["sh", "-c", command], check=False).returncode == 0
    assert not link.is_symlink() and link.is_dir()
    assert stat.S_IMODE(os.stat(link).st_mode) == 0o700 and os.stat(link).st_uid == os.geteuid()

    started = layout.start("--allow-shared-user")
    assert started.started, started.text()
    assert started.messages("repair reviewer pinned") == [
        "repair reviewer pinned reviewer=reviewer_local endpoint=http://127.0.0.1:11434 proxy=none ca_bundle=none"
    ]
    assert os.listdir(link) == [PIN] and os.listdir(target) == []


@pytest.mark.reviewer_pin
def test_check_reviewer_pin_itself_refuses_a_symlinked_folder(layout: Layout, caplog: pytest.LogCaptureFixture) -> None:
    """R3_SECURITY-2, the pin step alone, with no isolation check at all: `pin.check_reviewer_pin` on a symlinked state
    folder raises a logged StartupError with `unreadable:ENOTDIR` and the link's remedy, and writes nothing into the
    link's target; on the folder itself it pins, and the pin is the agent user's own."""
    caplog.set_level(logging.DEBUG, logger="cairn_repair_agent")
    link, target = make_symlinked_state(layout)
    reviewer = config.ReviewerSettings.model_validate(reviewer_entry())
    with pytest.raises(config.StartupError) as refused:
        pin.check_reviewer_pin(str(link), reviewer, "http://127.0.0.1:11434")
    assert refused.value.logged
    user, group = pin.agent_user_and_group()
    assert [record.getMessage() for record in caplog.records if record.levelname == "ERROR"] == [
        f"repair reviewer pin unusable path={link / PIN} reason=unreadable:ENOTDIR",
        FOLDER_SYMLINK_REMEDY.format(user=user, group=group, folder=link),
    ]
    assert os.listdir(target) == []
    pin.check_reviewer_pin(str(target), reviewer, "http://127.0.0.1:11434")
    assert os.listdir(target) == [PIN] and os.stat(target / PIN).st_uid == os.geteuid()
