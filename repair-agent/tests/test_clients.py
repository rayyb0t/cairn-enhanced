"""The agent's two HTTP clients and the environment guard (spec §9.3, Appendix C-1): AG4, AG41 and AG50, and the
server client's own session (no proxy, no redirect, its CA file) and its capped read, which AG45 and AG49 then
exercise through the review pipeline.

The model client is the reviewer's own `requests.Session` with `trust_env = False`; the server client is another,
which never uses a proxy. A worker's variables (`OPENAI_*`, `ANTHROPIC_*`) stay in the environment and reach no
request; the proxy, CA, netrc and key-log variables are removed before any client is built.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import (
    AGENT_TOKEN,
    REVIEWER_KEY,
    Layout,
    StubServer,
    TcpListener,
    fake_secret,
    reviewer_entry,
    settings_data,
    write_bytes,
)

from cairn_repair_agent import cli, envguard, model
from cairn_repair_agent import server as agent_server
from cairn_repair_agent.config import parse_settings
from cairn_repair_agent.server import RepairServerClient

PROJECT = Path(__file__).resolve().parents[1]
DIGEST_CANARY = "digest-canary-7f3a: tool output that only the reviewer may see"
RUN_ID = "5f2c9a1e7b3d4c0a9e8d7c6b5a493827"


def _worker_env(worker: StubServer) -> dict[str, str]:
    return {
        "OPENAI_API_KEY": fake_secret("worker-openai-key"),
        "OPENAI_BASE_URL": worker.url + "/v1",
        "ANTHROPIC_BASE_URL": worker.url,
        "ANTHROPIC_AUTH_TOKEN": fake_secret("worker-anthropic-token"),
        "ANTHROPIC_CUSTOM_HEADERS": "X-Worker-Canary: " + fake_secret("worker-header"),
    }


def _guarded_env(layout: Layout, proxy: TcpListener) -> dict[str, str]:
    bogus_ca = str(write_bytes(layout.base / "bogus-ca.pem", "not a certificate\n"))
    netrc = write_bytes(layout.base / "netrc", "machine 127.0.0.1 login worker password fake-netrc-password\n")
    return {
        "HTTP_PROXY": proxy.url,
        "https_proxy": proxy.url,
        "ALL_PROXY": proxy.url,
        "no_proxy": "example.invalid",
        "SSL_CERT_FILE": bogus_ca,
        "SSL_CERT_DIR": str(layout.base / "bogus-ca-dir"),
        "REQUESTS_CA_BUNDLE": bogus_ca,
        "CURL_CA_BUNDLE": bogus_ca,
        "NETRC": str(netrc),
        "SSLKEYLOGFILE": str(layout.base / "tls-keys.log"),
    }


def _assert_reviewer_request(
    model_server: StubServer, worker: StubServer, worker_env: dict[str, str], key: str | None
) -> None:
    assert [(r.method, r.path) for r in model_server.requests] == [("POST", "/v1/chat/completions")]
    headers = model_server.requests[0].headers
    if key is None:
        assert "Authorization" not in headers
    else:
        assert headers["Authorization"] == f"Bearer {key}"
    assert headers["Accept-Encoding"] == "identity"
    assert "X-Worker-Canary" not in headers
    for value in worker_env.values():
        secret = value.split(": ", 1)[-1]
        assert all(secret not in header for header in headers.values())
    assert "fake-netrc-password" not in json.dumps(headers)
    assert worker.requests == []


def test_model_client_ignores_worker_env(
    layout: Layout,
    stub_server: Any,
    tcp_listener: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AG4: with a worker's OPENAI_* and ANTHROPIC_* set, which the guard leaves in place, the request goes to the
    reviewer's base_url with its own key, no header carries one of their values, and no digest text appears in
    the logs; the proxy, CA, netrc and key-log variables are all gone from os.environ before the first client is
    built, one `repair agent ignored environment variables` record names them all and holds no value, and no
    key-log file is written."""
    worker, model_server, proxy, tls_peer = stub_server(), stub_server(), tcp_listener(), tcp_listener()
    layout.write_settings(
        settings_data(
            reviewers=[
                reviewer_entry(base_url=model_server.url + "/v1", api_key=REVIEWER_KEY),
                reviewer_entry("tls_reviewer", base_url=f"https://127.0.0.1:{tls_peer.port}/v1"),
            ]
        )
    )
    worker_env = _worker_env(worker)
    guarded = _guarded_env(layout, proxy)
    for name, value in {**worker_env, **guarded}.items():
        monkeypatch.setenv(name, value)

    seen_at_construction: list[list[str]] = []
    real_init = requests.Session.__init__

    def recording_init(self: requests.Session) -> None:
        seen_at_construction.append(sorted(k for k in os.environ if k.upper() in envguard.GUARDED_VARIABLES))
        real_init(self)

    monkeypatch.setattr(requests.Session, "__init__", recording_init)
    caplog.set_level(logging.DEBUG)
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user", "--log-level", "DEBUG")))
    monkeypatch.setattr(requests.Session, "__init__", real_init)

    assert len(seen_at_construction) == 2  # the server client and the first reviewer's model client
    assert seen_at_construction == [[], []]
    assert not [k for k in os.environ if k.upper() in envguard.GUARDED_VARIABLES]
    for name, value in worker_env.items():
        assert os.environ[name] == value
    ignored = [r.getMessage() for r in caplog.records if r.getMessage().startswith("repair agent ignored")]
    assert len(ignored) == 1
    names = ignored[0].removeprefix("repair agent ignored environment variables names=").split(",")
    assert set(guarded) <= set(names)
    assert names == sorted(names)
    for value in guarded.values():
        assert value not in ignored[0]

    response = agent.model.session.post(
        agent.model.url,
        json={"messages": [{"role": "user", "content": DIGEST_CANARY}]},
        timeout=(5, 10),
        allow_redirects=False,
    )
    assert response.status_code == 200
    _assert_reviewer_request(model_server, worker, worker_env, REVIEWER_KEY)
    assert DIGEST_CANARY.encode() in model_server.requests[0].body
    # A TLS connection made now writes no key-log file, and the proxy saw nothing.
    tls_client = model.build_model_client(agent.settings_file.settings.reviewers[1])
    with pytest.raises(requests.exceptions.SSLError):
        tls_client.session.post(tls_client.url, json={}, timeout=(5, 10), allow_redirects=False)
    assert tls_peer.connections == 1
    assert not Path(guarded["SSLKEYLOGFILE"]).exists()
    assert proxy.connections == 0
    # No digest text, key, token or worker value in any record, and no urllib3 debug line.
    for record in caplog.records:
        text = record.getMessage()
        for secret in (DIGEST_CANARY, REVIEWER_KEY, AGENT_TOKEN, *worker_env.values()):
            assert secret not in text
        assert not record.name.startswith("urllib3") or record.levelno >= logging.WARNING


def test_model_client_ignores_worker_env_in_a_fresh_interpreter(
    layout: Layout, stub_server: Any, tcp_listener: Any
) -> None:
    """AG4, as the agent is really started: the variables are set before the interpreter starts, the startup
    strips them before `requests` is first imported, and the key-log file is never written."""
    worker, model_server, proxy, tls_peer = stub_server(), stub_server(), tcp_listener(), tcp_listener()
    layout.write_settings(
        settings_data(
            server="http://127.0.0.1:9",  # nothing is sent to the server in this test
            reviewers=[
                reviewer_entry(base_url=model_server.url + "/v1"),
                reviewer_entry("tls_reviewer", base_url=f"https://127.0.0.1:{tls_peer.port}/v1"),
            ],
        )
    )
    worker_env = _worker_env(worker)
    guarded = _guarded_env(layout, proxy)
    env = {k: v for k, v in os.environ.items() if k.upper() not in envguard.GUARDED_VARIABLES}
    env.update(worker_env)
    env.update(guarded)
    driver = (
        "import json, logging, os, sys\n"
        "records = []\n"
        "class Keep(logging.Handler):\n"
        "    def emit(self, record): records.append([record.name, record.levelname, record.getMessage()])\n"
        "logging.getLogger().addHandler(Keep()); logging.getLogger().setLevel(logging.DEBUG)\n"
        "from cairn_repair_agent import cli, envguard\n"
        "loaded_before = 'requests' in sys.modules or 'urllib3' in sys.modules\n"
        "agent = cli.start(cli.build_parser().parse_args(sys.argv[2:]))\n"
        "from cairn_repair_agent import model\n"
        "left = sorted(k for k in os.environ if k.upper() in envguard.GUARDED_VARIABLES)\n"
        "status = agent.model.session.post(agent.model.url, json={'messages': [{'role': 'user',"
        " 'content': sys.argv[1]}]}, timeout=(5, 10), allow_redirects=False).status_code\n"
        "tls = model.build_model_client(agent.settings_file.settings.reviewers[1])\n"
        "try:\n"
        "    tls.session.post(tls.url, json={}, timeout=(5, 10), allow_redirects=False)\n"
        "    tls_error = None\n"
        "except Exception as exc:\n"
        "    tls_error = type(exc).__name__\n"
        "print(json.dumps({'loaded_before': loaded_before, 'left': left, 'status': status,"
        " 'tls_error': tls_error, 'records': records}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver, DIGEST_CANARY, *layout.argv("--allow-shared-user", "--log-level", "DEBUG")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=PROJECT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["loaded_before"] is False
    assert report["left"] == []
    assert report["status"] == 200
    assert report["tls_error"] == "SSLError" and tls_peer.connections == 1
    assert not Path(guarded["SSLKEYLOGFILE"]).exists()
    assert proxy.connections == 0
    _assert_reviewer_request(model_server, worker, worker_env, None)
    ignored = [message for _, _, message in report["records"] if message.startswith("repair agent ignored")]
    assert ignored == [f"repair agent ignored environment variables names={','.join(sorted(guarded))}"]
    for _, _, message in report["records"]:
        assert DIGEST_CANARY not in message
        for value in (*worker_env.values(), *guarded.values()):
            assert value not in message
    assert not [r for r in report["records"] if r[0].startswith("urllib3") and r[1] == "DEBUG"]


def test_api_key_header_is_sent_when_configured(layout: Layout, stub_server: Any) -> None:
    """AG41 (dev-0.3.0 port): with `server_api_key_file`, every request of the server client carries its
    `X-API-Key`; the agent token goes to the repair endpoints only; without the file no `X-API-Key` is sent."""
    server = stub_server(body={"runs": []})
    api_key = fake_secret("server-api-key")
    key_file = write_bytes(layout.base / "server-api.key", api_key + "\n", mode=0o600)
    layout.write_settings(settings_data(server=server.url))

    for configured in (True, False):
        server.requests.clear()
        if configured:
            layout.write_config(server_api_key_file=str(key_file))
        else:
            layout.write_config({k: v for k, v in layout.config.items() if k != "server_api_key_file"})
        agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
        assert agent.server_api_key == (api_key if configured else None)
        client = agent.server
        answers = [
            client.list_runs(),
            client.get_run(RUN_ID),
            client.get_project("proj_001"),
            client.post_review(RUN_ID, {"review_id": "rv-000000000000000000000000", "window": 1}),
        ]
        assert [answer.status for answer in answers] == [200, 200, 200, 200]
        assert answers[0].body == {"runs": []}
        paths = [(r.method, r.path) for r in server.requests]
        assert paths == [
            ("GET", "/repair/runs"),
            ("GET", f"/repair/runs/{RUN_ID}"),
            ("GET", "/projects/proj_001"),
            ("POST", f"/repair/runs/{RUN_ID}/reviews"),
        ]
        for recorded in server.requests:
            if configured:
                assert recorded.headers["X-API-Key"] == api_key
            else:
                assert "X-API-Key" not in recorded.headers
            if recorded.path.startswith("/repair/"):
                assert recorded.headers["X-Cairn-Repair-Token"] == AGENT_TOKEN
            else:
                assert "X-Cairn-Repair-Token" not in recorded.headers
            assert "Authorization" not in recorded.headers
        assert json.loads(server.requests[3].body)["review_id"] == "rv-000000000000000000000000"


def test_api_key_header_is_sent_when_configured_and_ids_stay_in_their_segment(layout: Layout) -> None:
    """AG41: the server client never builds a path from an id that breaks the server's rule, and quotes a
    project id; an empty or non-ASCII key file stops the agent at startup without its value."""
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    for bad in ("../projects", "abc", f"{RUN_ID}\n", "a/" + RUN_ID):
        with pytest.raises(ValueError, match="server's rule"):
            agent.server.get_run(bad)
    key_file = write_bytes(layout.base / "server-api.key", "\n", mode=0o600)
    layout.write_config(server_api_key_file=str(key_file))
    run = layout.start("--allow-shared-user")
    assert not run.started
    assert run.stderr == f"server API key refused: server_api_key_file {key_file}: empty\n"
    write_bytes(key_file, "fake-key-é\n", mode=0o600)
    run = layout.start("--allow-shared-user")
    assert run.stderr == f"server API key refused: server_api_key_file {key_file}: not_ascii\n"


PLAIN_HTTP_CASES = {
    "private_address": ("http://10.0.0.5:11434/v1", "http://10.0.0.5:11434"),
    "compose_host_gateway": ("http://host.docker.internal:11434/v1", "http://host.docker.internal:11434"),
    "loopback": ("http://127.0.0.1:11434/v1", None),
    "loopback_range": ("http://127.8.9.1:11434/v1", None),
    "ipv6_loopback": ("http://[::1]:11434/v1", None),
    "localhost": ("http://localhost:11434/v1", None),
    "https_private_address": ("https://10.0.0.5/v1", None),
    "https_hosted": ("https://provider.example/v1", None),
}


@pytest.mark.parametrize("case", list(PLAIN_HTTP_CASES))
def test_plain_http_model_endpoint_warns_on_a_non_loopback_host(layout: Layout, case: str) -> None:
    """AG50: the first reviewer's plain-http base_url on a non-loopback host logs one WARNING with its
    `scheme://host:port`, and the agent starts; loopback hosts and https log none."""
    base_url, endpoint = PLAIN_HTTP_CASES[case]
    layout.write_settings(settings_data(reviewers=[reviewer_entry(base_url=base_url)]))
    run = layout.start("--allow-shared-user")
    assert run.started
    warnings = run.messages("repair model endpoint is plain http")
    expected = (
        [] if endpoint is None else [f"repair model endpoint is plain http on a non-loopback host endpoint={endpoint}"]
    )
    assert warnings == expected
    started = run.messages("repair agent started")
    assert len(started) == 1
    assert f" endpoint={model.endpoint_of(base_url)} " in started[0]


def test_plain_http_model_endpoint_warns_on_a_non_loopback_host_for_each_reviewer_in_use(
    layout: Layout, caplog: pytest.LogCaptureFixture
) -> None:
    """AG50: the live agent uses only the first reviewer, so a second one on plain http logs nothing at its start;
    a tool that runs each reviewer (the replay) logs the WARNING once for each one whose base_url needs it."""
    reviewers = [
        reviewer_entry("local", base_url="http://127.0.0.1:11434/v1"),
        reviewer_entry("lan", base_url="http://10.0.0.5:11434/v1"),
        reviewer_entry("hosted", base_url="https://provider.example/v1"),
        reviewer_entry("bridge", base_url="http://host.docker.internal:8080/v1"),
    ]
    layout.write_settings(settings_data(reviewers=reviewers))
    run = layout.start("--allow-shared-user")
    assert run.started and run.messages("repair model endpoint is plain http") == []
    caplog.set_level(logging.WARNING, logger="cairn_repair_agent")
    caplog.clear()
    settings = parse_settings(settings_data(reviewers=reviewers), "agent-settings.json")
    assert [model.warn_if_plain_http(reviewer) for reviewer in settings.reviewers] == [False, True, False, True]
    assert [r.getMessage() for r in caplog.records] == [
        "repair model endpoint is plain http on a non-loopback host endpoint=http://10.0.0.5:11434",
        "repair model endpoint is plain http on a non-loopback host endpoint=http://host.docker.internal:8080",
    ]


def test_model_client_construction_follows_the_reviewer_entry(layout: Layout, tcp_listener: Any) -> None:
    """C-1, Client: the destination, key, CA file and proxy come from the reviewer's entry only; no redirect is
    followed; `endpoint` is `scheme://host:port`, never a path or credentials."""
    proxy = tcp_listener()
    ca = str(write_bytes(layout.base / "reviewer-ca.pem", "-----BEGIN CERTIFICATE-----\n"))
    settings = parse_settings(
        settings_data(
            reviewers=[
                reviewer_entry(base_url="http://user:pw@10.0.0.5:11434/v1/", api_key=REVIEWER_KEY),
                reviewer_entry("proxied", base_url="http://10.0.0.6/v1", proxy=proxy.url, ca_bundle=ca),
            ]
        ),
        "agent-settings.json",
    )
    first = model.build_model_client(settings.reviewers[0])
    assert first.session.trust_env is False and first.session.proxies == {} and first.session.verify is True
    assert first.session.max_redirects == 0
    assert first.endpoint == "http://10.0.0.5:11434"  # never the path or the credentials
    assert first.session.headers["Authorization"] == f"Bearer {REVIEWER_KEY}"
    assert REVIEWER_KEY not in repr(first)
    second = model.build_model_client(settings.reviewers[1])
    assert second.session.proxies == {"http": proxy.url, "https": proxy.url}
    assert second.session.verify == ca
    assert "Authorization" not in second.session.headers
    assert second.url == "http://10.0.0.6/v1/chat/completions"
    assert second.endpoint == "http://10.0.0.6:80"
    with pytest.raises(requests.exceptions.RequestException):
        second.session.post(second.url, json={}, timeout=(5, 5), allow_redirects=False)
    assert proxy.connections == 1  # the reviewer's own proxy, and only it


# --- the server client's session and reads (§9.3, §9.9) ---------------------------------------------------------

REVIEW_ID = "rv-000000000000000000000000"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
REDIRECT_BODY_BYTES = 8 * 1024 * 1024


def _review_body() -> dict[str, Any]:
    return {"review_id": REVIEW_ID, "window": 1}


def _json_of_size(size: int, **fields: Any) -> bytes:
    """A JSON object with these fields and a `pad` string that makes it exactly `size` bytes long."""
    shell = json.dumps({**fields, "pad": ""}).encode()
    return json.dumps({**fields, "pad": "x" * (size - len(shell))}).encode()


def _gzip_of_blank_json(mebibytes: int) -> bytes:
    """A small gzip body that expands to `mebibytes` MiB of JSON: an empty list padded with blanks."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    blanks = b" " * (1024 * 1024)
    parts = [compressor.compress(b"["), *(compressor.compress(blanks) for _ in range(mebibytes))]
    return b"".join([*parts, compressor.compress(b"]"), compressor.flush()])


def test_server_client_construction_follows_section_9_3(stub_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """§9.3: the server client's own session takes nothing from the environment, has no proxy and never resolves a
    redirect, and every request it sends has `allow_redirects=False` and a streamed body; `verify` is
    `server_ca_bundle` only for an https `server`. Each redirect guard is pinned here, because any one of them keeps
    a redirect from being followed, so removing another changes no answer."""
    server = stub_server(body={"runs": []})
    client = RepairServerClient(server.url, AGENT_TOKEN, ca_bundle="/etc/cairn-repair/server-ca.pem")
    session = client.session
    assert isinstance(session, agent_server.NoRedirectSession)
    assert session.trust_env is False and session.proxies == {} and session.max_redirects == 0
    assert session.headers["Accept-Encoding"] == "identity"
    assert session.verify is True  # an http server: the CA file is not used
    https = RepairServerClient("https://127.0.0.1:8443", AGENT_TOKEN, ca_bundle="/etc/cairn-repair/server-ca.pem")
    assert https.session.verify == "/etc/cairn-repair/server-ca.pem"
    assert RepairServerClient("https://127.0.0.1:8443", AGENT_TOKEN).session.verify is True

    sent: list[dict[str, Any]] = []
    real_request = session.request

    def recording_request(method: str, url: str, **kwargs: Any) -> requests.Response:
        sent.append(kwargs)
        return real_request(method, url, **kwargs)

    monkeypatch.setattr(session, "request", recording_request)
    client.list_runs()
    client.get_run(RUN_ID)
    client.get_project("proj_001")
    client.post_review(RUN_ID, _review_body())
    assert [(kwargs["allow_redirects"], kwargs["stream"]) for kwargs in sent] == [(False, True)] * 4


def test_server_client_ignores_proxy_environment(
    stub_server: Any, tcp_listener: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.3: built with HTTP_PROXY, HTTPS_PROXY and ALL_PROXY, upper and lower case, still set (the guard bypassed),
    the server client still sends its `GET /repair/runs` and its review POST straight to `server`: its own session
    has `trust_env = False` and no proxy. A default session sends the same request to the proxy."""
    server, proxy = stub_server(body={"runs": []}), tcp_listener()
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, proxy.url)
        monkeypatch.setenv(name.lower(), proxy.url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = RepairServerClient(server.url, AGENT_TOKEN)
    answers = [client.list_runs(), client.post_review(RUN_ID, _review_body())]
    assert [(answer.status, answer.error) for answer in answers] == [(200, None), (200, None)]
    assert [(r.method, r.path) for r in server.requests] == [
        ("GET", "/repair/runs"),
        ("POST", f"/repair/runs/{RUN_ID}/reviews"),
    ]
    assert proxy.connections == 0
    with requests.Session() as default, pytest.raises(requests.RequestException):
        default.get(server.url + "/repair/runs", timeout=(5, 5))
    assert proxy.connections == 1  # the environment really names a proxy


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
def test_server_client_reports_a_redirect_as_its_status(stub_server: Any, tcp_listener: Any, status: int) -> None:
    """§9.3, §9.9: a redirect is never followed. It comes back as its status, which is final for a review POST
    (`repair review failed … status=%s`), with no body read, and nothing reaches its Location on another host, so
    neither the agent token nor the X-API-Key goes there. requests' own redirect handling read the whole 3xx body
    and raised TooManyRedirects, which the agent took for status 0, no answer, and retried. The body is valid JSON
    with a code, so an answer that read it would have one."""
    target = tcp_listener("127.0.0.2")
    body = _json_of_size(REDIRECT_BODY_BYTES, detail={"code": "run_conflict"})
    server = stub_server(status, raw=body, headers={"Location": target.url + "/repair/runs"})
    client = RepairServerClient(server.url, AGENT_TOKEN, api_key=fake_secret("server-api-key"))
    answers = [client.list_runs(), client.get_project("proj_001"), client.post_review(RUN_ID, _review_body())]
    assert [(answer.status, answer.error, answer.body) for answer in answers] == [(status, None, None)] * 3
    assert len(server.requests) == 3
    assert target.connections == 0


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
def test_model_client_returns_a_redirect_unread(stub_server: Any, tcp_listener: Any, status: int) -> None:
    """C-1: a redirect is `model_error` with its status, which the review pipeline can give only if the session
    returns the 3xx. requests' own redirect handling raised TooManyRedirects instead, after reading the whole 3xx
    body into memory, outside the capped read loop and its deadline. The 3xx comes back with not one byte of its
    body read, and nothing reaches its Location on another host, so the reviewer's key does not go there."""
    target = tcp_listener("127.0.0.2")
    model_server = stub_server(
        status, raw=b" " * REDIRECT_BODY_BYTES, headers={"Location": target.url + "/v1/chat/completions"}
    )
    settings = parse_settings(
        settings_data(reviewers=[reviewer_entry(base_url=model_server.url + "/v1", api_key=REVIEWER_KEY)]),
        "agent-settings.json",
    )
    client = model.build_model_client(settings.reviewers[0])
    response = client.session.post(client.url, json={}, stream=True, allow_redirects=False, timeout=(5, 30))
    try:
        assert response.status_code == status
        assert response.raw.tell() == 0
    finally:
        response.close()
    assert [(r.method, r.path) for r in model_server.requests] == [("POST", "/v1/chat/completions")]
    assert target.connections == 0


def test_server_client_reads_at_most_its_cap(stub_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """§9.3: `server` comes from the settings file, which a local worker can point at a listener of its own, so the
    server client asks for no compression and reads at most SERVER_ANSWER_MAX_BYTES of decoded body. A larger answer,
    whether large on the wire or a small gzip body that expands in memory, is closed past the cap: it has no body,
    and so no code, like one that does not parse, and keeps its status, so a 503 is retried like any 5xx (§9.9)."""
    cap = 1024 * 1024
    monkeypatch.setattr(agent_server, "SERVER_ANSWER_MAX_BYTES", cap)
    at_cap = _json_of_size(cap, runs=[])
    compressed = _gzip_of_blank_json(64)
    assert len(at_cap) == cap and len(compressed) < cap
    stubs = {
        "at_cap": stub_server(raw=at_cap),
        "one_byte_over": stub_server(raw=_json_of_size(cap + 1, runs=[])),
        "large": stub_server(raw=b"[" + b" " * (64 * 1024 * 1024) + b"]"),
        "gzip": stub_server(raw=compressed, headers={"Content-Encoding": "gzip"}),
        "coded_503": stub_server(503, raw=_json_of_size(cap + 1, detail={"code": "token_not_configured"})),
    }
    answers = {name: RepairServerClient(stub.url, AGENT_TOKEN).list_runs() for name, stub in stubs.items()}
    assert (answers["at_cap"].status, answers["at_cap"].body) == (200, json.loads(at_cap))
    for name in ("one_byte_over", "large", "gzip"):
        assert (answers[name].status, answers[name].body, answers[name].error) == (200, None, None), name
    assert (answers["coded_503"].status, answers["coded_503"].code) == (503, None)
    for stub in stubs.values():
        assert [recorded.headers["Accept-Encoding"] for recorded in stub.requests] == ["identity"]


CERTIFICATE_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = cairn-repair-test
[ca_ext]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[leaf_ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""


def _test_certificates(folder: Path) -> tuple[str, str, str]:
    """A throwaway CA and a certificate for 127.0.0.1 that it signed: the CA file, the certificate and its key."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command to make a test certificate")
    config = str(write_bytes(folder / "openssl.cnf", CERTIFICATE_CONFIG))
    ca, ca_key = str(folder / "ca.pem"), str(folder / "ca-private.pem")
    leaf, leaf_key, request = str(folder / "leaf.pem"), str(folder / "leaf-private.pem"), str(folder / "leaf.csr")
    new_key = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-config", config]
    sign = ["-CA", ca, "-CAkey", ca_key, "-set_serial", "2", "-extfile", config, "-extensions", "leaf_ext"]
    for command in (
        ["req", "-x509", *new_key, "-extensions", "ca_ext", "-days", "2", "-keyout", ca_key, "-out", ca],
        ["req", "-new", *new_key, "-subj", "/CN=127.0.0.1", "-keyout", leaf_key, "-out", request],
        ["x509", "-req", "-in", request, *sign, "-days", "2", "-out", leaf],
    ):
        subprocess.run([openssl, *command], check=True, capture_output=True, timeout=60)
    return ca, leaf, leaf_key


def test_server_client_verifies_an_https_server_with_server_ca_bundle(
    layout: Layout, stub_server: Any, tmp_path: Path
) -> None:
    """§9.2, §9.3: for an https `server`, the server client verifies the certificate with `server_ca_bundle`: a server
    whose certificate that CA signed is reached, and without the file the default CA bundle refuses the same server
    (status 0, SSLError). The file is not used for an http `server`."""
    ca, leaf, leaf_key = _test_certificates(tmp_path / "tls")
    https_server = stub_server(body={"runs": []}, tls=(leaf, leaf_key))
    layout.write_settings(settings_data(server=https_server.url))
    layout.write_config(server_ca_bundle=ca)
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert agent.server.session.verify == ca
    answer = agent.server.list_runs()
    assert (answer.status, answer.body, answer.error) == (200, {"runs": []}, None)

    layout.write_config({name: value for name, value in layout.config.items() if name != "server_ca_bundle"})
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    answer = agent.server.list_runs()
    assert (answer.status, answer.error) == (0, "SSLError")
    assert len(https_server.requests) == 1

    layout.write_settings(settings_data(server="http://127.0.0.1:8000"))
    layout.write_config(server_ca_bundle=ca)
    agent = cli.start(cli.build_parser().parse_args(layout.argv("--allow-shared-user")))
    assert agent.server.session.verify is True
