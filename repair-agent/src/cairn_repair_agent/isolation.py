"""The startup isolation check (spec §9.3 step 2, decision 1).

The agent reads untrusted run output to decide, so it must hold no Docker access and no worker keys, and nothing
that another user controls may steer it. `check` returns every reason that holds, in the spec's order; the caller
refuses to start unless `--allow-shared-user` turns them into a WARNING.

"Can be opened for reading" means that `os.open(path, os.O_RDONLY | os.O_NONBLOCK)` succeeds; the descriptor is
closed at once and nothing is read.
"""

from __future__ import annotations

import errno
import os
import pwd
import stat
import sysconfig

from .config import AgentConfig, SettingsFile

# The fixed system paths below (sockets and container markers) are taken relative to this root; tests point it at a
# folder of their own. It is "/" in the agent.
SYSTEM_ROOT = "/"

# The CLI credential files that the dispatcher redacts in local mode (§8.8), at their default locations below the
# home directory of the copy root's owner. In local mode these, not dispatch.yaml, hold the worker keys.
CREDENTIAL_FILES = (
    ".claude/.credentials.json",
    ".claude/settings.json",
    ".claude.json",
    ".codex/auth.json",
    ".codex/config.toml",
    ".pi/agent/auth.json",
    ".pi/agent/models.json",
)
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
GROUP_OR_WORLD_WRITABLE = 0o022


def docker_socket_paths(euid: int) -> tuple[str, ...]:
    return (
        "/var/run/docker.sock",
        "/run/docker.sock",
        f"/run/user/{euid}/docker.sock",
        f"/run/user/{euid}/podman/podman.sock",
    )


def _system(path: str) -> str:
    return os.path.join(SYSTEM_ROOT, path.lstrip("/"))


def in_container() -> bool:
    return any(os.path.exists(_system(marker)) for marker in CONTAINER_MARKERS)


def _open_error(path: str) -> int | None:
    """None when `path` can be opened for reading, else the errno of the failed open."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        return exc.errno if exc.errno is not None else errno.EIO
    except ValueError:  # a path with a NUL byte
        return errno.EINVAL
    os.close(fd)
    return None


def can_open_for_reading(path: str) -> bool:
    return _open_error(path) is None


def _reachable_socket(path: str) -> bool:
    if not os.path.exists(path):
        return False
    return os.access(path, os.R_OK, effective_ids=True) or os.access(path, os.W_OK, effective_ids=True)


def package_paths() -> list[str]:
    """Where the agent's code and its dependencies are imported from: the package folder, purelib and platlib."""
    import cairn_repair_agent

    paths = sysconfig.get_paths()
    result: list[str] = []
    for path in (os.path.dirname(os.path.abspath(cairn_repair_agent.__file__)), paths["purelib"], paths["platlib"]):
        if path not in result:
            result.append(path)
    return result


def _home_of(uid: int) -> str | None:
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        return None
    return home or None


def _on_read_only_mount(path: str) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, ValueError):
        return False


def changeable_by_others(path: str, allowed_uids: frozenset[int]) -> bool:
    """The ownership check: true unless `path` is owned by an allowed uid and neither group- nor world-writable.

    A path on a read-only mount is not checked. A path that cannot be examined fails the check.
    """
    if _on_read_only_mount(path):
        return False
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return True
    return info.st_uid not in allowed_uids or bool(info.st_mode & GROUP_OR_WORLD_WRITABLE)


def check(config: AgentConfig, config_path: str, settings_file: SettingsFile) -> list[str]:
    """Every isolation reason that holds, comma-joinable, in the order of §9.3 step 2."""
    euid = os.geteuid()
    root = config.transcripts.root
    root_uid = os.stat(root).st_uid
    reasons: list[str] = []

    if any(_reachable_socket(_system(path)) for path in docker_socket_paths(euid)):
        reasons.append("docker_socket")
    if "DOCKER_HOST" in os.environ:
        reasons.append("docker_host")

    on_host = not in_container()
    target = config.dispatch_config_to_check
    if target is None:
        if on_host:
            reasons.append("dispatch_config_missing")
    else:
        failure = _open_error(target)
        if failure is None:
            reasons.append("dispatch_config_readable")
        elif on_host and failure not in (errno.EACCES, errno.EPERM):
            # An agent started as the dispatcher's user from another directory cannot pass by not finding the file.
            reasons.append("dispatch_config_missing")

    if euid != 0 and euid == root_uid:
        reasons.append("same_user")
    if os.access(root, os.W_OK, effective_ids=True):
        reasons.append("copy_root_writable")

    home = _home_of(root_uid)
    if home is not None and any(can_open_for_reading(os.path.join(home, name)) for name in CREDENTIAL_FILES):
        reasons.append("worker_credentials_readable")

    for path in config.isolation.unreadable_paths:
        reason = f"readable:{path}"
        if reason not in reasons and can_open_for_reading(path):
            reasons.append(reason)

    agent_uids = frozenset({0, euid})
    config_files = [config_path, config.server_ca_bundle]
    config_files += [reviewer.ca_bundle for reviewer in settings_file.settings.reviewers]
    if any(changeable_by_others(path, agent_uids) for path in config_files if path is not None):
        reasons.append("config_writable")
    secret_files = [config.token_file, config.server_api_key_file]
    if any(changeable_by_others(path, agent_uids) for path in secret_files if path is not None):
        reasons.append("secret_writable")
    if any(changeable_by_others(path, agent_uids) for path in package_paths()):
        reasons.append("package_writable")
    # The settings file belongs to the dispatcher's user by design (O1): root or the copy root's owner.
    if changeable_by_others(settings_file.path, frozenset({0, root_uid})):
        reasons.append("settings_writable")
    return reasons


def state_dir_reasons(state_dir: str) -> list[str]:
    """The reasons of the live agent's state folder, which holds the reviewer pin (owner decision O9), in their order
    after `settings_writable`: a symlink (`os.lstat`), a folder the agent's effective uid does not own, or one that is
    group- or world-writable (the folder a symlink names, for these two). Another user, or a local worker, who can
    replace the folder or write in it could replace the pin. Only the live agent's start checks it (`cli.start`); a
    missing folder, or the agent's own folder whose mode lacks u+rwx, is not a reason: the pin step refuses it
    (`pin.check_reviewer_pin`)."""
    reasons: list[str] = []
    try:
        if stat.S_ISLNK(os.lstat(state_dir).st_mode):
            reasons.append("state_dir_symlink")
        info = os.stat(state_dir)
    except (OSError, ValueError):
        return reasons
    if info.st_uid != os.geteuid():
        reasons.append("state_dir_owner")
    if info.st_mode & GROUP_OR_WORLD_WRITABLE:
        reasons.append("state_dir_writable")
    return reasons
