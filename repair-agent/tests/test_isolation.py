"""The startup isolation check and the settings file's WARNINGs (spec §9.3 steps 2 and 5): AG3 and AG55.

The check runs as the agent's user in the host install of §13 step 4 (the `host_install` fixture, which needs root
to give the files their owners): root owns the agent's configuration and code, the agent's user its token, and the
dispatcher's user the copy root, the settings file and `dispatch.yaml`. That layout passes with no reason, and each
case below breaks one thing and expects exactly one reason.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from typing import Any

import pytest
from conftest import (
    AGENT_GID,
    AGENT_TOKEN,
    AGENT_UID,
    DISPATCHER_UID,
    OTHER_GID,
    OTHER_UID,
    REVIEWER_KEY,
    SECOND_REVIEWER_KEY,
    HostInstall,
    Layout,
    reviewer_entry,
    set_mode,
    settings_data,
    write_bytes,
)

from cairn_repair_agent import cli, config, isolation


def _socket(rel: str) -> Callable[[HostInstall], None]:
    def make(host: HostInstall) -> None:
        path = host.system / rel.format(euid=AGENT_UID)
        path.parent.mkdir(parents=True, exist_ok=True)
        set_mode(path.parent, 0o755)
        write_bytes(path, b"")
        os.chown(path, 0, AGENT_GID)  # root:docker 0660, with the agent's user in that group
        set_mode(path, 0o660)

    return make


def _credentials(rel: str) -> Callable[[HostInstall], None]:
    def make(host: HostInstall) -> None:
        home = host.base / "home-of-copy-root-owner"
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        for folder in (home, *path.relative_to(home).parents):
            set_mode(home / folder, 0o755)
        write_bytes(path, '{"note": "a synthetic credential file"}', mode=0o644)
        host.homes[DISPATCHER_UID] = str(home)

    return make


def _ca_file(host: HostInstall, mode: int) -> str:
    path = write_bytes(host.base / "etc-cairn-repair-ca" / "ca.pem", "-----BEGIN CERTIFICATE-----\n", mode=mode)
    set_mode(path.parent, 0o755)
    os.chown(path, 0, 0)
    set_mode(path, mode)
    return str(path)


def _unreadable_path(host: HostInstall, mode: int) -> str:
    path = write_bytes(host.home / "repair-dispatcher.token", "fake-dispatcher-token\n", mode=mode)
    os.chown(path, DISPATCHER_UID, DISPATCHER_UID)
    set_mode(path, mode)
    host.write_config(isolation={"unreadable_paths": [str(path)]})
    return str(path)


def _same_user(host: HostInstall) -> None:
    # The agent started as the dispatcher's user, with its own token; everything else would pass.
    os.chown(host.token_path, DISPATCHER_UID, AGENT_GID)
    set_mode(host.root, 0o2550)
    os.chown(host.dispatch_path, OTHER_UID, OTHER_GID)


def _ownership(target: str, how: str) -> Callable[[HostInstall], None]:
    """Give the agent's configuration, its token, its package folder, purelib or platlib another owner, or a group or
    world write bit; the agent can still read each."""

    def make(host: HostInstall) -> None:
        path = {
            "config": host.config_path,
            "secret": host.token_path,
            "package_folder": host.packages[0],
            "purelib": host.packages[1],
            "platlib": host.packages[2],
        }[target]
        if how == "owned_by_another_user":
            os.chown(path, OTHER_UID, os.stat(path).st_gid)
        else:
            set_mode(path, stat.S_IMODE(os.stat(path).st_mode) | (0o020 if how == "group_writable" else 0o002))

    return make


def _container(marker: str) -> Callable[[HostInstall], None]:
    def make(host: HostInstall) -> None:
        write_bytes(host.system / marker.lstrip("/"), b"", mode=0o644)

    return make


CASES: dict[str, tuple[Callable[[HostInstall], Any], list[str]]] = {
    "host_install_passes": (lambda host: None, []),
    "docker_socket_var_run": (_socket("var/run/docker.sock"), ["docker_socket"]),
    "docker_socket_run": (_socket("run/docker.sock"), ["docker_socket"]),
    "docker_socket_run_user": (_socket("run/user/{euid}/docker.sock"), ["docker_socket"]),
    "docker_socket_run_user_podman": (_socket("run/user/{euid}/podman/podman.sock"), ["docker_socket"]),
    "docker_socket_not_reachable_passes": (
        lambda host: (_socket("var/run/docker.sock")(host), set_mode(host.system / "var/run/docker.sock", 0o600)),
        [],
    ),
    "docker_host": (lambda host: os.environ.__setitem__("DOCKER_HOST", "unix:///run/docker.sock"), ["docker_host"]),
    "dispatch_config_readable": (lambda host: set_mode(host.dispatch_path, 0o644), ["dispatch_config_readable"]),
    "dispatch_config_missing_file": (
        lambda host: host.write_config(dispatch_config_to_check=str(host.home / "moved.yaml")),
        ["dispatch_config_missing"],
    ),
    "dispatch_config_missing_null": (
        lambda host: host.write_config(dispatch_config_to_check=None),
        ["dispatch_config_missing"],
    ),
    "dispatch_config_in_an_unsearchable_folder_passes": (lambda host: set_mode(host.home, 0o700), []),
    "container_null_passes": (
        lambda host: (_container("/.dockerenv")(host), host.write_config(dispatch_config_to_check=None)),
        [],
    ),
    "container_missing_file_passes": (
        lambda host: (
            _container("/run/.containerenv")(host),
            host.write_config(dispatch_config_to_check=str(host.home / "moved.yaml")),
        ),
        [],
    ),
    "same_user": (_same_user, ["same_user"]),
    "copy_root_writable": (lambda host: set_mode(host.root, 0o2770), ["copy_root_writable"]),
    "readable_path": (lambda host: _unreadable_path(host, 0o644), ["readable:{unreadable}"]),
    "unreadable_path_passes": (lambda host: _unreadable_path(host, 0o600), []),
    "missing_unreadable_path_passes": (
        lambda host: host.write_config(isolation={"unreadable_paths": [str(host.home / "absent.token")]}),
        [],
    ),
    "server_ca_bundle_group_writable": (
        lambda host: host.write_config(server_ca_bundle=_ca_file(host, 0o664)),
        ["config_writable"],
    ),
    "reviewer_ca_bundle_group_writable": (
        lambda host: host.write_settings(
            settings_data(reviewers=[reviewer_entry(), reviewer_entry("second", ca_bundle=_ca_file(host, 0o664))])
        ),
        ["config_writable"],
    ),
    "reviewer_ca_bundle_protected_passes": (
        lambda host: host.write_settings(settings_data(reviewers=[reviewer_entry(ca_bundle=_ca_file(host, 0o644))])),
        [],
    ),
    "server_api_key_file_world_writable": (
        lambda host: host.write_config(
            server_api_key_file=str(write_bytes(host.base / "etc-cairn-repair" / "server.key", "k\n", mode=0o606))
        ),
        ["secret_writable"],
    ),
    "settings_group_writable": (lambda host: host.write_settings(settings_data(), mode=0o660), ["settings_writable"]),
    "settings_world_writable": (lambda host: host.write_settings(settings_data(), mode=0o646), ["settings_writable"]),
    "settings_owned_by_neither_root_nor_the_copy_root_owner": (
        lambda host: host.write_settings(settings_data(), uid=OTHER_UID),
        ["settings_writable"],
    ),
    "settings_owned_by_root_passes": (lambda host: host.write_settings(settings_data(), uid=0), []),
}
OWNERSHIP_REASONS = {
    "config": "config_writable",
    "secret": "secret_writable",
    "package_folder": "package_writable",
    "purelib": "package_writable",
    "platlib": "package_writable",
}
for _target, _reason in OWNERSHIP_REASONS.items():
    for _how in ("owned_by_another_user", "group_writable", "world_writable"):
        CASES[f"{_target}_{_how}"] = (_ownership(_target, _how), [_reason])
for _rel in isolation.CREDENTIAL_FILES:
    CASES[f"worker_credentials_readable[{_rel}]"] = (_credentials(_rel), ["worker_credentials_readable"])
CASES["worker_credentials_unreadable_passes"] = (
    lambda host: (_credentials(".codex/auth.json")(host), set_mode(host.base / "home-of-copy-root-owner", 0o700)),
    [],
)
CASES["worker_credentials_skipped_without_a_passwd_entry"] = (
    lambda host: (_credentials(".claude.json")(host), host.homes.clear()),
    [],
)


@pytest.mark.parametrize("case", list(CASES))
def test_isolation_check_refuses_docker_socket_docker_host_and_a_readable_dispatch_config(
    host_install: HostInstall, case: str
) -> None:
    """AG3: each reason of §9.3 step 2 alone, as the agent's user; and what passes."""
    change, expected = CASES[case]
    unreadable = change(host_install)
    as_dispatcher = case == "same_user"
    reasons = host_install.check(uid=DISPATCHER_UID if as_dispatcher else AGENT_UID)
    assert reasons == [reason.format(unreadable=unreadable) for reason in expected]


def test_isolation_check_lists_every_reason_in_order_and_refuses_start(host_install: HostInstall) -> None:
    """AG3: all reasons at once, comma-separated in §9.3's order; the agent refuses to start with exit code 2 and
    `repair agent isolation check failed reasons=%s`; with `--allow-shared-user` the WARNING lists every reason
    found and the agent starts."""
    _socket("run/docker.sock")(host_install)
    os.environ["DOCKER_HOST"] = "tcp://127.0.0.1:2375"
    set_mode(host_install.dispatch_path, 0o644)
    os.chown(host_install.token_path, DISPATCHER_UID, AGENT_GID)
    set_mode(host_install.root, 0o2770)
    _credentials(".pi/agent/models.json")(host_install)
    unreadable = _unreadable_path(host_install, 0o644)
    set_mode(host_install.config_path, 0o660)
    set_mode(host_install.token_path, 0o460)
    set_mode(host_install.packages[1], 0o775)
    host_install.write_settings(settings_data(), mode=0o660)
    order = [
        "docker_socket",
        "docker_host",
        "dispatch_config_readable",
        "same_user",
        "copy_root_writable",
        "worker_credentials_readable",
        f"readable:{unreadable}",
        "config_writable",
        "secret_writable",
        "package_writable",
        "settings_writable",
    ]
    assert host_install.check(uid=DISPATCHER_UID) == order

    refused = host_install.start(uid=DISPATCHER_UID)
    assert not refused.started
    assert refused.records[-1] == (
        "cairn_repair_agent.cli",
        "ERROR",
        f"repair agent isolation check failed reasons={','.join(order)}",
    )
    assert refused.messages("repair agent started") == []

    allowed = host_install.start("--allow-shared-user", uid=DISPATCHER_UID)
    assert allowed.started
    assert allowed.messages("repair agent running without isolation") == [
        f"repair agent running without isolation reasons={','.join(order)}"
    ]
    assert len(allowed.messages("repair agent started")) == 1


def test_isolation_check_host_install_starts_and_allow_shared_user_says_none(host_install: HostInstall) -> None:
    """AG3: the host install of §13 step 4 starts as the agent's user without the flag; with it, the WARNING
    says `reasons=none`. Root, on a host, is refused: it can read dispatch.yaml and write the copy root, and
    the agent's token belongs to another uid than its own."""
    run = host_install.start()
    assert run.started, run.text()
    assert [level for _, level, _ in run.records if level == "ERROR"] == []
    assert run.messages("repair agent running without isolation") == []
    allowed = host_install.start("--allow-shared-user")
    assert allowed.messages("repair agent running without isolation") == [
        "repair agent running without isolation reasons=none"
    ]
    as_root = host_install.start(uid=0, groups=(0,))
    assert not as_root.started
    assert as_root.messages("repair agent isolation check failed") == [
        "repair agent isolation check failed reasons=dispatch_config_readable,copy_root_writable,secret_writable"
    ]


def test_isolation_check_skips_read_only_mounts(host_install: HostInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """AG3: none of the ownership checks applies to a path on a read-only mount (`os.statvfs` reporting
    `ST_RDONLY`), as Compose binds the configuration, the secrets and the copy root."""
    os.chown(host_install.config_path, OTHER_UID, AGENT_GID)
    set_mode(host_install.token_path, 0o620)
    os.chown(host_install.packages[0], OTHER_UID, OTHER_GID)
    host_install.write_settings(settings_data(), mode=0o660)
    expected = ["config_writable", "secret_writable", "package_writable", "settings_writable"]
    assert host_install.check() == expected

    read_only = {
        str(host_install.config_path),
        str(host_install.token_path),
        str(host_install.packages[0]),
        str(host_install.settings_path),
    }
    real_statvfs = os.statvfs

    class ReadOnly:
        f_flag = os.ST_RDONLY

    def fake_statvfs(path: Any) -> Any:
        return ReadOnly() if os.fspath(path) in read_only else real_statvfs(path)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    assert host_install.check() == []


def test_isolation_check_passes_the_compose_agent_running_as_root(
    host_install: HostInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AG3, the Compose deployment of §13: the agent is root in its own container, the dispatcher (root in its
    container) owns the copy root and the settings file, `dispatch_config_to_check` is null, and the copy root,
    the configuration and the token are read-only mounts. `same_user` never applies to uid 0."""
    write_bytes(host_install.system / ".dockerenv", b"", mode=0o644)
    host_install.write_config(dispatch_config_to_check=None)
    os.chown(host_install.root, 0, 0)
    host_install.write_settings(settings_data(), uid=0, gid=0)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    read_only = {str(host_install.root), str(host_install.config_path), str(host_install.token_path)}
    real_statvfs, real_access = os.statvfs, os.access

    class ReadOnly:
        f_flag = os.ST_RDONLY

    def fake_statvfs(path: Any) -> Any:
        return ReadOnly() if os.fspath(path) in read_only else real_statvfs(path)

    def fake_access(path: Any, mode: int, **kwargs: Any) -> bool:
        if os.fspath(path) in read_only and mode & os.W_OK:
            return False  # EROFS: a read-only mount refuses writes, also to root
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    monkeypatch.setattr(os, "access", fake_access)
    assert os.geteuid() == 0 and os.stat(host_install.root).st_uid == 0
    loaded = config.load_config(str(host_install.config_path))
    reasons = isolation.check(loaded, str(host_install.config_path), config.load_settings(str(host_install.root)))
    assert reasons == []


def test_isolation_check_default_package_paths() -> None:
    """AG3: the code checked is the folder `cairn_repair_agent` is imported from, and the interpreter's purelib
    and platlib, which hold its dependencies."""
    import sysconfig

    import cairn_repair_agent

    paths = sysconfig.get_paths()
    expected = {os.path.dirname(os.path.abspath(cairn_repair_agent.__file__)), paths["purelib"], paths["platlib"]}
    assert set(isolation.package_paths()) == expected
    assert isolation.docker_socket_paths(1000) == (
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/run/user/1000/docker.sock",
        "/run/user/1000/podman/podman.sock",
    )


def test_isolation_check_startup_errors_are_not_reasons(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    """AG3: a relative or placeholder path in `dispatch_config_to_check` or `isolation.unreadable_paths` is a
    startup error that `--allow-shared-user` does not override."""
    changes: list[dict[str, Any]] = [
        {"dispatch_config_to_check": "relative/dispatch.yaml"},
        {"dispatch_config_to_check": "/home/<you>/cairn/dispatch.yaml"},
        {"isolation": {"unreadable_paths": ["relative.token"]}},
        {"isolation": {"unreadable_paths": ["/home/<you>/cairn/secrets/repair-dispatcher.token"]}},
    ]
    for change in changes:
        layout.write_config(**change)
        assert cli.main(layout.argv("--allow-shared-user")) == 2
        assert capsys.readouterr().err.startswith(f"agent config invalid: {layout.config_path}: ")
        layout.write_config(dispatch_config_to_check=str(layout.dispatch_path), isolation={"unreadable_paths": []})


# --- AG55 -----------------------------------------------------------------------------------------------------------


def _no_secret(run_text: str) -> None:
    for secret in (AGENT_TOKEN, REVIEWER_KEY, SECOND_REVIEWER_KEY):
        assert secret not in run_text


def test_settings_file_warnings(host_install: HostInstall) -> None:
    """AG55: a settings file owned by a uid other than 0 logs exactly one `repair reviewer settings are writable
    by the dispatcher's user, local workers can change them path=<path>`, and the agent starts; one owned by
    uid 0 logs none. No record holds a key."""
    keyed = settings_data(
        reviewers=[reviewer_entry(api_key=REVIEWER_KEY), reviewer_entry("second", api_key=SECOND_REVIEWER_KEY)]
    )
    host_install.write_settings(keyed)
    run = host_install.start()
    assert run.started, run.text()
    assert run.messages("repair reviewer settings are writable") == [
        "repair reviewer settings are writable by the dispatcher's user, local workers can change them "
        f"path={host_install.settings_path}"
    ]
    assert run.messages("repair agent settings file readable by others") == []
    _no_secret(run.text())

    host_install.write_settings(keyed, uid=0)
    run = host_install.start()
    assert run.started, run.text()
    assert run.messages("repair reviewer settings are writable") == []
    _no_secret(run.text())


@pytest.mark.parametrize(("mode", "warned"), [(0o644, True), (0o640, False), (0o600, False)])
def test_settings_file_warnings_mode(layout: Layout, mode: int, warned: bool) -> None:
    """AG55: mode 0644 logs one `repair agent settings file readable by others path=<path> mode=0644`; 0640 (the
    copy root's group, the agent's) logs none."""
    layout.write_settings(settings_data(reviewers=[reviewer_entry(api_key=REVIEWER_KEY)]), mode=mode)
    run = layout.start("--allow-shared-user")
    assert run.started
    expected = [f"repair agent settings file readable by others path={layout.settings_path} mode={mode:04o}"]
    assert run.messages("repair agent settings file readable by others") == (expected if warned else [])
    _no_secret(run.text())
