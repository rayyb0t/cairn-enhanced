"""Environment stripping and library logger levels (spec §9.3 steps 3 and 4).

With a proxy variable set, a default `requests.Session` sends the digest, the reviewer's key and the agent token to
the proxy it names; `SSLKEYLOGFILE` makes `urllib3` write TLS session keys whatever `trust_env` says. So before any
client is built the agent removes these variables, in any letter case, and logs their names only. The clients also
ignore the environment themselves (`trust_env = False`), so this guard is not their only protection. A worker's own
variables (`OPENAI_*`, `ANTHROPIC_*`) are left in place: nothing in the agent reads them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

GUARDED_VARIABLES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NETRC",
        "SSLKEYLOGFILE",
    }
)


def strip_environment(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove the proxy, CA, netrc and key-log variables; log their names, sorted, in one line, never a value."""
    target = os.environ if environ is None else environ
    removed = sorted(name for name in list(target) if name.upper() in GUARDED_VARIABLES)
    for name in removed:
        del target[name]
    if removed:
        logger.warning("repair agent ignored environment variables names=%s", ",".join(removed))
    return removed


def quiet_library_loggers() -> None:
    """`--log-level DEBUG` shows the agent's own lines, not urllib3's connection and request lines (§9.3 step 4)."""
    logging.getLogger("urllib3").setLevel(logging.WARNING)
