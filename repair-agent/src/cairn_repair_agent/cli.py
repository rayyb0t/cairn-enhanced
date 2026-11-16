"""`cairn-repair`: the repair agent's entry point and its startup (spec §9.3).

The startup, in order: load `repair.yaml`, read the token and the optional server API key, read the dispatcher's
settings file; run the isolation check, with the state folder's reasons; strip the proxy, CA, netrc and key-log
variables from the environment; quiet urllib3's logger; log the plain-http and settings-file WARNINGs (§9.3 step 5);
check the reviewer pin (owner decision O9, `pin.py`), before any client is built; build the server client and the
first reviewer's model client; log the `repair agent started` line. Every startup failure exits with code 2. Then
`main` enters the review loop (`loop.run_agent`, §9.3 step 7): until SIGINT or SIGTERM, or for one poll with `--once`.

This module imports neither `requests` nor the clients, nor the loop, at module level: they are imported only after
the environment guard, so no TLS context or session can ever be made from the variables it removes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import envguard, isolation, pin
from .config import (
    AgentConfig,
    SettingsFile,
    StartupError,
    load_config,
    load_settings,
    read_agent_token,
    read_server_api_key,
)
from .digest import digest_budget

if TYPE_CHECKING:
    from .model import ModelClient
    from .server import RepairServerClient

logger = logging.getLogger(__name__)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
WORLD_READABLE = 0o004


@dataclass(frozen=True)
class StartedAgent:
    """What the startup produced; the review loop runs on it. Secrets are kept out of the repr."""

    config: AgentConfig
    config_path: str
    settings_file: SettingsFile
    token: str = field(repr=False)
    server_api_key: str | None = field(repr=False)
    server: RepairServerClient
    model: ModelClient
    digest_tokens: int
    once: bool
    dry_run: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn-repair",
        description=(
            "Cairn repair agent, shadow mode: reviews each long explore run of a switched-on project once in its "
            "75-80% window and records one decision, which is never applied."
        ),
    )
    parser.add_argument("--config", required=True, metavar="PATH", help="the agent's own repair.yaml")
    parser.add_argument("--once", action="store_true", help="poll once, finish the reviews it started, then exit")
    parser.add_argument("--dry-run", action="store_true", help="build digests and call the model, but post nothing")
    parser.add_argument(
        "--log-level", default="INFO", type=str.upper, choices=LOG_LEVELS, help="log level (default: INFO)"
    )
    parser.add_argument(
        "--allow-shared-user",
        action="store_true",
        help="start although the isolation check fails, logging every reason it found at WARNING",
    )
    return parser


def start(args: argparse.Namespace) -> StartedAgent:
    """Steps 1-6 of §9.3. Raises StartupError; never logs a token, key or settings value."""
    config_path: str = args.config
    config = load_config(config_path)
    token = read_agent_token(config)
    server_api_key = read_server_api_key(config)
    # A reviewer's default max_tokens depends on repair.yaml's digest budget (owner decision O10).
    settings_file = load_settings(config.transcripts.root, digest_tokens=config.digest.max_tokens_estimate)
    settings = settings_file.settings

    # The live agent's state folder, which holds the reviewer pin, is checked at its start only (O9).
    reasons = isolation.check(config, config_path, settings_file) + isolation.state_dir_reasons(config.state.dir)
    if args.allow_shared_user:
        logger.warning("repair agent running without isolation reasons=%s", ",".join(reasons) or "none")
    elif reasons:
        logger.error("repair agent isolation check failed reasons=%s", ",".join(reasons))
        raise StartupError("repair agent isolation check failed", logged=True)

    envguard.strip_environment()
    envguard.quiet_library_loggers()
    # Imported only now, after the guard (see the module docstring).
    from .model import build_model_client, endpoint_of, warn_if_plain_http
    from .server import RepairServerClient

    reviewer = settings.reviewers[0]  # the live agent reviews with the first reviewer (§8.3, §9.7)
    warn_if_plain_http(reviewer)
    if settings_file.uid != 0:
        logger.warning(
            "repair reviewer settings are writable by the dispatcher's user, local workers can change them path=%s",
            settings_file.path,
        )
    if settings_file.mode & WORLD_READABLE:
        logger.warning(
            "repair agent settings file readable by others path=%s mode=%s",
            settings_file.path,
            f"{settings_file.mode:04o}",
        )

    # The reviewer pin, before any client is built or any request sent (O9): a changed reviewer stops the agent here,
    # after the WARNINGs above, which say who can change the reviewer.
    pin.check_reviewer_pin(config.state.dir, reviewer, endpoint_of(reviewer.base_url))
    server = RepairServerClient(settings.server, token, api_key=server_api_key, ca_bundle=config.server_ca_bundle)
    model = build_model_client(reviewer)

    # The digest budget; a reviewer's context_window lowers it (Appendix C-1, Digest budget).
    digest_tokens = digest_budget(config.digest.max_tokens_estimate, reviewer.context_window, reviewer.max_tokens)
    logger.info(
        "repair agent started server=%s reviewer=%s model=%s endpoint=%s digest_tokens=%s poll=%ss projects=%s "
        "transcripts=%s dry_run=%s",
        endpoint_of(settings.server),
        reviewer.name,
        reviewer.model,
        model.endpoint,
        digest_tokens,
        config.poll_interval,
        "all" if settings.projects is None else len(settings.projects),
        config.transcripts.root,
        "true" if args.dry_run else "false",
    )
    return StartedAgent(
        config=config,
        config_path=config_path,
        settings_file=settings_file,
        token=token,
        server_api_key=server_api_key,
        server=server,
        model=model,
        digest_tokens=digest_tokens,
        once=args.once,
        dry_run=args.dry_run,
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format=LOG_FORMAT, stream=sys.stderr)


def start_or_report(args: argparse.Namespace) -> StartedAgent | None:
    """`start`, with a startup error printed to standard error (unless it was logged); None means exit code 2."""
    try:
        return start(args)
    except StartupError as exc:
        if not exc.logged:
            sys.stderr.write(f"{exc}\n")
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    agent = start_or_report(args)
    if agent is None:
        return 2
    from .loop import run_agent  # after the environment guard (see the module docstring)

    return run_agent(agent)


if __name__ == "__main__":
    sys.exit(main())
