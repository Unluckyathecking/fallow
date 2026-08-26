"""The coordinator systemd install: its plan, its refusals, and its unit file.

The install itself needs root and a systemd host, neither of which CI has, so
what is tested here is everything up to that line: the `--dry-run` plan names
every action in order, the pinned-ref refusals hold, the preview changes
nothing, and the unit file is one systemd will accept and points at the paths
the script actually creates.

The branches that read the host — an already-installed unit, a config that is
there or not, a standby_path the unit could not write — are driven through the
script's one test seam, ``FALLOW_INSTALL_ROOT``: it prefixes every system path,
so a temporary directory stands in for /etc and /var on a machine that has no
systemd and must not grow a real /etc/fallow.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy" / "coordinator" / "install.sh"
UNIT = ROOT / "deploy" / "coordinator" / "fallow-coordinator.service"
EXAMPLE_CONFIG = ROOT / "deploy" / "coordinator.example.toml"
SYSTEM_PATHS = (
    Path("/opt/fallow"),
    Path("/etc/fallow"),
    Path("/var/lib/fallow"),
    Path("/etc/systemd/system/fallow-coordinator.service"),
)
VENV_PYTHON = "/opt/fallow/src/.venv/bin/python"
EXEC_START = f"{VENV_PYTHON} -m fallow_coordinator serve --config /etc/fallow/coordinator.toml"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the coordinator installer is bash-only"
)


def _run(*arguments: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if root is not None:
        environment["FALLOW_INSTALL_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def _host(tmp_path: Path, *, config: str | None = None, unit_installed: bool = False) -> Path:
    """A stand-in root: the /etc the installer reads before it plans anything."""
    root = tmp_path / "host"
    (root / "etc" / "fallow").mkdir(parents=True)
    if config is not None:
        (root / "etc" / "fallow" / "coordinator.toml").write_text(config, encoding="utf-8")
    if unit_installed:
        units = root / "etc" / "systemd" / "system"
        units.mkdir(parents=True)
        (units / "fallow-coordinator.service").write_text("", encoding="utf-8")
    return root


def _assert_in_order(result: subprocess.CompletedProcess[str], expected: list[str]) -> None:
    plan = [line for line in result.stdout.splitlines() if line.startswith("plan: ")]
    remaining = plan
    for fragment in expected:
        for index, line in enumerate(remaining):
            if fragment in line:
                remaining = remaining[index + 1 :]
                break
        else:
            pytest.fail(f"plan never reaches {fragment!r} in order:\n" + "\n".join(plan))


def test_the_dry_run_plans_every_install_step_in_order(tmp_path: Path) -> None:
    # The config is in place, so this is the ordinary shape: install or upgrade
    # a host whose config an operator has already edited. The example config is
    # used verbatim, which also proves its commented-out standby_path is read as
    # the comment it is.
    root = _host(tmp_path, config=EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    assert result.returncode == 0, result.stderr
    _assert_in_order(
        result,
        [
            "check: running as root",
            "check: git is installed",
            "check: uv is installed",
            "git ls-remote --exit-code",
            f"install -d -m 0755 {root}/opt/fallow",
            "fetch --tags --prune origin v0.3.0",
            "checkout --force --detach FETCH_HEAD",
            f"uv sync --frozen --no-dev --project {root}/opt/fallow/src",
            f"install -d -o fallow -g fallow -m 0750 {root}/var/lib/fallow",
            f"install -d -o root -g fallow -m 0750 {root}/etc/fallow",
            f"{root}/etc/systemd/system/fallow-coordinator.service",
            "systemctl daemon-reload",
            "systemctl enable fallow-coordinator.service",
            "systemctl restart fallow-coordinator.service",
        ],
    )
    # Two steps read the host and take one of two branches. Either branch is a
    # correct plan; what must never happen is the step going unreported.
    combined = result.stdout + result.stderr
    assert "useradd --system" in combined or "system user fallow already exists" in combined
    assert "git clone" in combined or "updating the existing checkout" in combined
    assert f"keeping the existing config {root}/etc/fallow/coordinator.toml" in result.stderr


def test_the_ref_is_probed_before_the_host_is_touched(tmp_path: Path) -> None:
    """A typo in --ref must not leave a system user and a clone behind."""
    root = _host(tmp_path, config=EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    _assert_in_order(result, ["git ls-remote --exit-code", "git clone"])


def test_a_first_install_seeds_the_config_and_refuses_to_start(tmp_path: Path) -> None:
    root = _host(tmp_path)

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    assert result.returncode == 0, result.stderr
    assert "copied the example config" in result.stderr
    for key in ("admin_key", "host", "tls_certfile", "tls_keyfile"):
        assert key in result.stderr
    # The seeded config still carries the example's published placeholder key,
    # so this run installs the unit and stops there.
    assert "did NOT start it" in result.stderr
    assert "systemctl enable fallow-coordinator.service" not in result.stdout
    assert "systemctl restart fallow-coordinator.service" not in result.stdout


def test_the_next_run_starts_the_service_once_the_config_is_there(tmp_path: Path) -> None:
    root = _host(tmp_path, config='admin_key = "a-real-one"\n')

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    _assert_in_order(result, ["systemctl restart fallow-coordinator.service"])


def test_an_upgrade_stops_the_service_before_it_rewrites_the_checkout(tmp_path: Path) -> None:
    """The venv runs the code out of the checkout, so a live service is stopped first."""
    root = _host(tmp_path, config='admin_key = "a-real-one"\n', unit_installed=True)

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    _assert_in_order(
        result,
        [
            "systemctl stop fallow-coordinator.service",
            "checkout --force --detach FETCH_HEAD",
            "uv sync --frozen",
            "systemctl restart fallow-coordinator.service",
        ],
    )


def test_a_fresh_host_has_no_service_to_stop(tmp_path: Path) -> None:
    root = _host(tmp_path, config='admin_key = "a-real-one"\n')

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    assert "systemctl stop" not in result.stdout


def test_install_refuses_a_standby_path_the_unit_cannot_write(tmp_path: Path) -> None:
    """ProtectSystem=strict makes every export outside /var/lib/fallow fail silently."""
    root = _host(tmp_path, config='standby_path = "/mnt/standby/coordinator.db"\n')

    result = _run("--ref", "v0.3.0", "--dry-run", root=root)

    assert result.returncode != 0
    assert "standby_path" in result.stderr
    assert "ReadWritePaths=/mnt/standby" in result.stderr
    assert "--allow-external-standby" in result.stderr


def test_the_standby_refusal_lifts_once_the_drop_in_is_declared(tmp_path: Path) -> None:
    root = _host(tmp_path, config='standby_path = "/mnt/standby/coordinator.db"\n')

    result = _run("--ref", "v0.3.0", "--dry-run", "--allow-external-standby", root=root)

    assert result.returncode == 0, result.stderr
    assert "WARNING: standby_path" in result.stderr


def test_a_standby_path_under_the_state_dir_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "host"
    root_config = f'standby_path = "{root}/var/lib/fallow/standby/coordinator.db"\n'

    result = _run("--ref", "v0.3.0", "--dry-run", root=_host(tmp_path, config=root_config))

    assert result.returncode == 0, result.stderr
    assert "WARNING: standby_path" not in result.stderr


def test_the_dry_run_changes_nothing_on_the_host() -> None:
    before = {path: path.exists() for path in SYSTEM_PATHS}

    assert _run("--ref", "v0.3.0", "--dry-run").returncode == 0
    assert _run("uninstall", "--purge", "--dry-run").returncode == 0

    assert {path: path.exists() for path in SYSTEM_PATHS} == before


def test_install_refuses_without_a_ref() -> None:
    result = _run("--dry-run")

    assert result.returncode != 0
    assert "--ref" in result.stderr


def test_install_refuses_a_ref_that_swallowed_the_next_flag() -> None:
    """`--ref --dry-run` must not consume the flag and quietly do a real run."""
    result = _run("--ref", "--dry-run")

    assert result.returncode != 0
    assert "--ref requires a value" in result.stderr


def test_install_refuses_a_branch_unless_it_is_allowed() -> None:
    refused = _run("--ref", "main", "--dry-run")
    allowed = _run("--ref", "main", "--allow-branch", "--dry-run")

    assert refused.returncode != 0
    assert "unpinned ref" in refused.stderr
    assert allowed.returncode == 0
    _assert_in_order(allowed, ["fetch --tags --prune origin main"])


def test_uninstall_keeps_the_state_and_config_unless_purged() -> None:
    kept = _run("uninstall", "--dry-run")
    purged = _run("uninstall", "--purge", "--dry-run")

    _assert_in_order(
        kept,
        [
            "systemctl disable --now fallow-coordinator.service",
            "rm -f /etc/systemd/system/fallow-coordinator.service",
            "systemctl daemon-reload",
            "rm -rf /opt/fallow/src",
        ],
    )
    assert "preserved /var/lib/fallow and /etc/fallow" in kept.stderr
    assert "rm -rf /var/lib/fallow /etc/fallow" not in kept.stdout
    _assert_in_order(purged, ["rm -rf /var/lib/fallow /etc/fallow"])


def test_the_unit_runs_the_venv_python_at_the_paths_the_script_installs() -> None:
    """The unit is copied verbatim, not rendered, so the two must not drift."""
    unit = configparser.ConfigParser()
    unit.read_string(UNIT.read_text(encoding="utf-8"))
    service = unit["Service"]
    script = SCRIPT.read_text(encoding="utf-8")

    assert service["User"] == "fallow"
    assert service["ExecStart"] == EXEC_START
    assert service["Restart"] == "on-failure"
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/fallow"
    assert service["PrivateTmp"] == "yes"
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    for path in (
        'SRC_DIR="${PREFIX}/opt/fallow/src"',
        'CONFIG_DST="${CONFIG_DIR}/coordinator.toml"',
    ):
        assert path in script


def test_systemd_accepts_the_unit() -> None:
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is not installed on this host")

    result = subprocess.run(
        ["systemd-analyze", "verify", str(UNIT)],
        capture_output=True,
        check=False,
        text=True,
    )

    # The venv is built by the install, so on a host that has not run it the one
    # expected complaint is the missing ExecStart binary. Anything else — an
    # unknown key, an unparsable value — is a real defect in the unit.
    missing_venv = (
        f"{UNIT.name}: Command {VENV_PYTHON} is not executable: No such file or directory"
    )
    problems = [line for line in result.stderr.splitlines() if line.strip()]
    assert [line for line in problems if line != missing_venv] == []
