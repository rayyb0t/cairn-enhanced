"""start_repair_side: the repair side's housekeeping thread, started with the dispatcher (spec 8.3, At start).

DispatcherLoop.run calls start_repair_side once, through scheduler/loop.py's _start_repair_side, and only when
dispatch.yaml has a repair: section; without the section this module is never imported. start_repair_side starts
one daemon thread, repair-housekeeping, and returns at once. The thread, in this order:

1. reads the dispatcher token (repair/config.py; a reporter thread that needs it first reads it itself);
2. checks the reviewers' keys against the workers' keys (spec 8.3, Shared keys);
3. writes the repair agent's settings file (spec 8.3, The agent's settings file);
4. runs the copy housekeeping (spec 8.8, Removal) at once, and then every HOUSEKEEPING_INTERVAL_SECONDS for the
   dispatcher's lifetime.

Each step, and each housekeeping pass, runs inside its own guard: an Exception logs "repair shadow housekeeping
failed step=%s error=%s" once per process and step, with the exception's class name, and the thread goes on with
the next step or pass. A thread that cannot start logs the same line with step=start, and the dispatcher runs
without it. Nothing here raises into the scheduler.

Without a repair: section none of this exists, and with the section the scheduler and every run are as before: the
thread only reads dispatch.yaml's values, writes in the copy root, and reads two existing endpoints of the server
(GET /projects, and GET /projects/{id} for a folder that may be removed), without the repair token.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.repair import redaction, transcripts
from cairn.dispatcher.repair.client import RepairRunsClient
from cairn.dispatcher.repair.config import ASCII_WHITESPACE, RepairState, load_state
from cairn.dispatcher.repair.session_ids import valid_project_id

LOG = logging.getLogger(__name__)

THREAD_NAME = "repair-housekeeping"
HOUSEKEEPING_INTERVAL_SECONDS = 60

# The whitespace removed around a key: what bytes.strip() removes, as from the dispatcher token (repair/config.py)
# and from REVIEW_API_KEY when dispatch.yaml is loaded (dispatcher/config.py), spec 8.3.
KEY_WHITESPACE = ASCII_WHITESPACE.decode("ascii")

# A reviewer's call when its api_format is left out or null (spec 8.3; owner decision O8). The settings file always
# names the format, so that the agent never guesses it.
DEFAULT_API_FORMAT = "openai_chat"

# A reviewer's optional client settings (Appendix C-1), written to the agent's settings file when set.
OPTIONAL_REVIEWER_SETTINGS = (
    "structured_output",
    "max_tokens",
    "context_window",
    "timeout_seconds",
    "max_attempts",
    "ca_bundle",
    "proxy",
    "price",
)

# Waited on between passes. The dispatcher never sets it, so the thread lives as long as the process, as a daemon;
# tests set it to end the thread.
_STOP = threading.Event()

# The steps whose failure has been logged in this process: each is logged once.
_failed_steps: set[str] = set()
_failed_lock = threading.Lock()


def start_repair_side(config: DispatchConfig) -> None:
    """Start the housekeeping thread and return at once. Never raises."""
    try:
        threading.Thread(target=_housekeeping, args=(config,), name=THREAD_NAME, daemon=True).start()
    except Exception as exc:
        _log_failure("start", exc)


def _housekeeping(config: DispatchConfig) -> None:
    """The housekeeping thread: steps 1 to 3 once, then the copy housekeeping's passes."""
    state = _guarded("token", load_state, config)
    _guarded("shared_keys", check_shared_keys, config)
    _guarded("settings", write_agent_settings, config, state)
    client = RepairRunsClient(config.server)  # GET /projects and GET /projects/{id} only, without the token (8.6)
    try:
        while True:
            _guarded("removal", remove_copies, config, client)
            if _STOP.wait(HOUSEKEEPING_INTERVAL_SECONDS):
                return
    finally:
        client.close()


def check_shared_keys(config: DispatchConfig) -> None:
    """Step 2, spec 8.3 (Shared keys): one WARNING per reviewer whose REVIEW_API_KEY a worker also holds.

    Each key that is set is compared, by exact value, with the worker values the copy redaction collects
    (repair/redaction.py): for each worker entry the secret values of its env, common_env included, and the user:pass
    and password of every URL in it; in local mode also those of the dispatcher's own environment. Each of those
    values is compared as written and with the whitespace around it removed, as it is removed around the key when
    dispatch.yaml is loaded, so that a key written with the same spaces in both places still matches. The local CLIs'
    credential files are not read for this (owner decision O4), and the repair token is not a worker's. The line
    names the reviewer and the matching workers in dispatch.yaml order, then environment; never a value. Sharing is
    allowed: nothing else happens.
    """
    repair = config.repair
    if repair is None:
        return
    workers = [
        (worker.name, _as_written_and_stripped(redaction.environment_values(worker.env))) for worker in config.workers
    ]
    environment = (
        _as_written_and_stripped(redaction.environment_values(os.environ))
        if config.runtime.execution == "local"
        else set()
    )
    for reviewer in repair.reviewers:
        key = reviewer.env.REVIEW_API_KEY
        if not key:
            continue
        holders = [name for name, values in workers if key in values]
        if key in environment:
            holders.append("environment")
        if holders:
            LOG.warning("repair reviewer shares a worker key reviewer=%s worker=%s", reviewer.name, ",".join(holders))


def _as_written_and_stripped(values: set[str]) -> set[str]:
    """values, and each of them with the whitespace around it removed (KEY_WHITESPACE)."""
    return values | {value.strip(KEY_WHITESPACE) for value in values}


def agent_settings(config: DispatchConfig) -> dict[str, Any]:
    """The content of the agent's settings file (spec 8.3): version (first), server, projects and the reviewers.

    Each reviewer has name, model, base_url, api_key (null when left out) and api_format (openai_chat when left out
    or null, owner decision O8), plus each optional setting that is set and not null, under its own name. Nothing
    else from dispatch.yaml: no worker entry or env, no common_env, no token or token_file, no transcript_root.
    """
    repair = config.repair
    if repair is None:
        raise ValueError("dispatch.yaml has no repair: section")
    reviewers: list[dict[str, Any]] = []
    for reviewer in repair.reviewers:
        entry: dict[str, Any] = {
            "name": reviewer.name,
            "model": reviewer.env.REVIEW_MODEL,
            "base_url": reviewer.env.REVIEW_BASE_URL,
            "api_key": reviewer.env.REVIEW_API_KEY,
            "api_format": reviewer.api_format or DEFAULT_API_FORMAT,
        }
        for name in OPTIONAL_REVIEWER_SETTINGS:
            value = getattr(reviewer, name)
            if value is not None:
                entry[name] = dict(value) if isinstance(value, dict) else value
        reviewers.append(entry)
    projects = None if repair.projects is None else list(repair.projects)
    return {"version": 1, "server": config.server, "projects": projects, "reviewers": reviewers}


def write_agent_settings(config: DispatchConfig, state: RepairState | None) -> None:
    """Step 3, spec 8.3 (The agent's settings file): <transcript_root>/agent-settings.json, written atomically.

    state is the token step's result (None if that step failed, and the token is then read here). A REVIEW_API_KEY
    equal to the dispatcher token is never written: that start writes no file and removes an earlier one. The root is
    created if missing and opened with O_NOFOLLOW; the file is written to agent-settings.json.tmp (mode 0640, the
    root's group), fsynced and moved over agent-settings.json, so a reader sees the old file or the new one and a
    symlink there is replaced, never followed. A root that is a symlink or not a directory is refused and nothing is
    touched. Any other failure after the root is opened removes an earlier agent-settings.json, so that the agent
    refuses to start rather than use settings dispatch.yaml no longer has.
    """
    repair = config.repair
    if repair is None:
        return
    path = os.path.join(repair.transcript_root, transcripts.SETTINGS_NAME)
    try:
        root_fd = transcripts.open_root(repair.transcript_root)
    except transcripts.UnsafePathError:
        _settings_not_written(path, "unsafe_path")
        return
    except Exception as exc:
        _settings_not_written(path, type(exc).__name__)
        return
    try:
        if state is None:
            state = load_state(config)
        token = state.token if state.configured else None
        content = agent_settings(config)
        if token is not None and any(reviewer["api_key"] == token for reviewer in content["reviewers"]):
            _remove_settings(root_fd)
            _settings_not_written(path, "reviewer_key_is_token")
            return
        payload = json.dumps(content, ensure_ascii=True, allow_nan=False).encode("ascii")
        transcripts.write_file(root_fd, transcripts.SETTINGS_NAME, payload, os.fstat(root_fd).st_gid)
    except Exception as exc:
        with suppress(Exception):
            _remove_settings(root_fd)
        _settings_not_written(path, type(exc).__name__)
        return
    finally:
        os.close(root_fd)
    LOG.info("repair shadow agent settings written path=%s reviewers=%s", path, len(content["reviewers"]))


def _remove_settings(root_fd: int) -> None:
    with suppress(FileNotFoundError):
        os.unlink(transcripts.SETTINGS_NAME, dir_fd=root_fd)


def _settings_not_written(path: str, error: str) -> None:
    LOG.warning("repair shadow agent settings not written path=%s error=%s", path, error)


# Copy removal (spec 8.8, Removal). --------------------------------------------------------------------------------

DELETED = "deleted"
COMPLETED = "completed"
NOT_COPIES = "not_copies"

# The folders whose "copies not removed" line has been logged in this process: the not_copies line once per folder,
# a failure once per project.
_not_removed_logged: set[tuple[str, str]] = set()
_not_removed_lock = threading.Lock()


class _NotCopiesError(Exception):
    """The folder holds something other than copies: it is kept."""


def remove_copies(config: DispatchConfig, client: RepairRunsClient) -> None:
    """Step 4, one pass of spec 8.8's copy housekeeping (Removal), with the housekeeping thread's client.

    A project's copy folder, <transcript_root>/<project_id>, is removed when the project was deleted (it is not in
    GET /projects, and GET /projects/{id} answers the server's exact "Project not found" 404) or completed under the
    execution mode's completed_action: remove; never while a run of the project is being reported in this process,
    and only if the folder holds at least one run folder and nothing but copies. The pass first lists the root: with
    no folder whose name passes the id rule and that holds an entry, it sends no request. A folder with no entry holds
    no copies, and nothing in it shows that this dispatcher made it (a home folder such as Templates, or a deleted
    project's empty work folder when transcript_root is local.workspace_root): it is never asked about or removed.
    """
    repair = config.repair
    if repair is None:
        return
    try:
        root_fd = os.open(repair.transcript_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            return  # no root yet, or one this pass does not follow
        raise
    try:
        folders = _project_folders(root_fd)
        if not folders:
            return
        projects = client.list_projects()
        if projects is None:
            return  # no list: every folder is kept until a later pass
        statuses = {project.id: project.status for project in projects}
        completed_action = _completed_action(config)
        for project_id in folders:
            status = statuses.get(project_id)
            if status is None:
                reason = DELETED
            elif status == "completed" and completed_action == "remove":
                reason = COMPLETED
            else:
                continue  # the project is still there: its copies stay, as its container or folder does
            if transcripts.reporting_count(project_id) > 0:
                continue  # a run of it is being reported: tried again at the next pass
            _remove_project_folder(root_fd, project_id, reason, client)
    finally:
        os.close(root_fd)


def _project_folders(root_fd: int) -> list[str]:
    """The entries of the root that are folders, not symlinks, whose names pass the id rule and that are not empty,
    in name order."""
    folders: list[str] = []
    for name in sorted(os.listdir(root_fd)):
        if name in (transcripts.SETTINGS_NAME, transcripts.SETTINGS_NAME + transcripts.TEMPORARY_SUFFIX):
            continue
        if not valid_project_id(name):
            continue
        try:
            mode = os.lstat(name, dir_fd=root_fd).st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode) and not _is_empty_folder(root_fd, name):
            folders.append(name)
    return folders


def _is_empty_folder(root_fd: int, name: str) -> bool:
    """Whether the folder name in the root opens, without following a symlink, and holds no entry. A folder that
    cannot be opened or listed is not taken for empty: the removal's own checks then keep it and log why."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return False
    try:
        with os.scandir(fd) as entries:
            return next(entries, None) is None
    except OSError:
        return False
    finally:
        os.close(fd)


def _completed_action(config: DispatchConfig) -> str:
    """The completed_action of the execution mode in use: container.completed_action or local.completed_action."""
    if config.runtime.execution == "local":
        return (config.local or LocalConfig()).completed_action
    return config.container.completed_action if config.container is not None else "stop"


def _remove_project_folder(root_fd: int, project_id: str, reason: str, client: RepairRunsClient) -> None:
    """Remove one project's copy folder if it holds only copies and, for a deleted project, the server confirms it.

    The check runs before GET /projects/{id} is asked. The removal unlinks exactly the files checked and removes each
    run folder, then the project folder, with rmdir, through descriptors opened with O_NOFOLLOW: an entry added
    meanwhile makes rmdir fail and keeps the folder. The count check and the removal hold the lock reporter threads
    take to join the count, so no reporter thread writes into a folder that is being removed.
    """
    try:
        plan = _copies_plan(root_fd, project_id)
    except _NotCopiesError:
        _log_not_removed(project_id, reason, NOT_COPIES, NOT_COPIES)
        return
    except Exception as exc:
        _log_not_removed(project_id, reason, type(exc).__name__, "failed")
        return
    if not plan:
        return  # emptied since the root was listed: it holds no copies, so it is kept and not asked about
    if reason == DELETED and not client.project_gone(project_id):
        return  # any other answer, or none, keeps the folder until a later pass
    with transcripts.REPORTING_LOCK:
        if transcripts.reporting_count(project_id) > 0:
            return
        try:
            _remove_planned(root_fd, project_id, plan)
        except Exception as exc:
            _log_not_removed(project_id, reason, type(exc).__name__, "failed")
            return
    LOG.info("repair shadow copies removed project=%s reason=%s count=%s", project_id, reason, len(plan))


def _copies_plan(root_fd: int, project_id: str) -> list[tuple[str, list[str]]]:
    """The run folders of a project folder and their files, if the folder holds nothing but copies.

    Every entry, by lstat, must be a directory, not a symlink, named like a run id this dispatcher makes, and every
    entry of each of those a regular file named transcript.jsonl, meta.json or one of their .tmp files; otherwise
    _NotCopiesError. An empty folder gives an empty plan, which removes nothing.
    """
    project_fd = os.open(project_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        plan: list[tuple[str, list[str]]] = []
        for run_name in sorted(os.listdir(project_fd)):
            info = os.lstat(run_name, dir_fd=project_fd)
            if not stat.S_ISDIR(info.st_mode) or transcripts.RUN_FOLDER_RULE.fullmatch(run_name) is None:
                raise _NotCopiesError
            run_fd = os.open(run_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=project_fd)
            try:
                files = sorted(os.listdir(run_fd))
                for file_name in files:
                    if file_name not in transcripts.COPY_FILE_NAMES:
                        raise _NotCopiesError
                    if not stat.S_ISREG(os.lstat(file_name, dir_fd=run_fd).st_mode):
                        raise _NotCopiesError
            finally:
                os.close(run_fd)
            plan.append((run_name, files))
        return plan
    finally:
        os.close(project_fd)


def _remove_planned(root_fd: int, project_id: str, plan: list[tuple[str, list[str]]]) -> None:
    project_fd = os.open(project_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        for run_name, files in plan:
            run_fd = os.open(run_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=project_fd)
            try:
                for file_name in files:
                    os.unlink(file_name, dir_fd=run_fd)
            finally:
                os.close(run_fd)
            os.rmdir(run_name, dir_fd=project_fd)
    finally:
        os.close(project_fd)
    os.rmdir(project_id, dir_fd=root_fd)


def _log_not_removed(project_id: str, reason: str, error: str, kind: str) -> None:
    with _not_removed_lock:
        first = (kind, project_id) not in _not_removed_logged
        _not_removed_logged.add((kind, project_id))
    if first:
        LOG.warning("repair shadow copies not removed project=%s reason=%s error=%s", project_id, reason, error)


def _guarded(step: str, function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except Exception as exc:
        _log_failure(step, exc)
        return None


def _log_failure(step: str, exc: Exception) -> None:
    with _failed_lock:
        first = step not in _failed_steps
        _failed_steps.add(step)
    if first:
        LOG.warning("repair shadow housekeeping failed step=%s error=%s", step, type(exc).__name__)
