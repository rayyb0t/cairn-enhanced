"""The model client (spec §9.7, Appendix C-1): one OpenAI-compatible client, for a local model server and a
hosted API alike.

Each reviewer in use gets its own `requests.Session` with `trust_env = False`: `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NETRC` and `REQUESTS_CA_BUNDLE` in the environment can never send a digest elsewhere, and no worker
variable (`OPENAI_*`, `ANTHROPIC_*`) is read. The destination, the key, the CA file and the proxy come only from the
reviewer's entry in the settings file. The session never resolves a redirect (`NoRedirectSession`), so a 3xx comes
back as its status with its body unread, for the answer mapping to treat as `model_error` (C-1). The request, its
read loop and the mapping of answers are built on this client by the review pipeline.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from .config import ReviewerSettings
from .server import NoRedirectSession

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
CONNECT_TIMEOUT_SECONDS = 5.0
LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.ip_address("::1")


@dataclass(frozen=True)
class ModelClient:
    """The client of one reviewer: its session, the URL its requests go to, and its endpoint for log lines."""

    reviewer: ReviewerSettings
    session: requests.Session = field(repr=False)
    url: str
    endpoint: str


def endpoint_of(url: str) -> str:
    """`scheme://host:port` of a URL, never a path or credentials (C-1); the port is the default one if not given."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def is_plain_http_on_a_non_loopback_host(url: str) -> bool:
    """`http://` to a host other than `localhost`, `::1` or an address in 127.0.0.0/8 (§9.3)."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    if address.version == 4:
        return address not in LOOPBACK_V4
    return address != LOOPBACK_V6


def warn_if_plain_http(reviewer: ReviewerSettings) -> bool:
    """Log the plain-http WARNING once for a reviewer in use whose base_url needs it (§9.3); true if it did."""
    if not is_plain_http_on_a_non_loopback_host(reviewer.base_url):
        return False
    logger.warning(
        "repair model endpoint is plain http on a non-loopback host endpoint=%s", endpoint_of(reviewer.base_url)
    )
    return True


def build_model_client(reviewer: ReviewerSettings) -> ModelClient:
    """The reviewer's own OpenAI-compatible client (C-1, Client): nothing from the environment, no redirects."""
    session = NoRedirectSession()
    session.trust_env = False
    session.max_redirects = 0
    session.proxies = {"http": reviewer.proxy, "https": reviewer.proxy} if reviewer.proxy is not None else {}
    session.verify = reviewer.ca_bundle if reviewer.ca_bundle is not None else True
    session.headers["Accept-Encoding"] = "identity"
    if reviewer.api_key is not None:
        session.headers["Authorization"] = f"Bearer {reviewer.api_key}"
    return ModelClient(
        reviewer=reviewer,
        session=session,
        url=reviewer.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH,
        endpoint=endpoint_of(reviewer.base_url),
    )
