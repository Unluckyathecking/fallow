"""The coordinator systemd install: its plan, its refusals, and its unit file.

The install itself needs root and a systemd host, neither of which CI has, so
what is tested here is everything up to that line: the `--dry-run` plan names
every action in order, the pinned-ref refusals hold, the preview changes
nothing, and the unit file is one systemd will accept and points at the paths
the script actually creates.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy" / "coordinator" / "install.sh"
UNIT = ROOT / "deploy" / "coordinator" / "fallow-coordinator.service"
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


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


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


def test_the_dry_run_plans_every_install_step_in_order() -> None:
    result = _run("--ref", "v0.3.0", "--dry-run")

    assert result.returncode == 0, result.stderr
    _assert_in_order(
        result,
        [
            "check: running as root",
            "check: git is installed",
            "check: uv is installed",
            "install -d -m 0755 /opt/fallow",
            "fetch --tags --prune origin v0.3.0",
            "checkout --force --detach FETCH_HEAD",
            "uv sync --frozen --no-dev --project /opt/fallow/src",
            "install -d -o fallow -g fallow -m 0750 /var/lib/fallow",
            "install -d -o root -g fallow -m 0750 /etc/fallow",
            "/etc/systemd/system/fallow-coordinator.service",
            "systemctl daemon-reload",
            "systemctl enable fallow-coordinator.service",
            "systemctl restart fallow-coordinator.service",
        ],
    )
    # Three steps read the host and take one of two branches. Either branch is a
    # correct plan; what must never happen is the step going unreported.
    combined = result.stdout + result.stderr
    assert "useradd --system" in combined or "system user fallow already exists" in combined
    assert "git clone" in combined or "updating the existing checkout" in combined
    assert "/etc/fallow/coordinator.toml" in combined


def test_a_first_install_names_the_config_keys_the_operator_must_edit() -> None:
    if Path("/etc/fallow/coordinator.toml").exists():
        pytest.skip("this host already has a coordinator config, so the plan keeps it untouched")

    result = _run("--ref", "v0.3.0", "--dry-run")

    assert "copied the example config" in result.stderr
    for key in ("admin_key", "host", "tls_certfile", "tls_keyfile"):
        assert key in result.stderr


def test_the_dry_run_changes_nothing_on_the_host() -> None:
    before = {path: path.exists() for path in SYSTEM_PATHS}

    assert _run("--ref", "v0.3.0", "--dry-run").returncode == 0
    assert _run("uninstall", "--purge", "--dry-run").returncode == 0

    assert {path: path.exists() for path in SYSTEM_PATHS} == before


def test_install_refuses_without_a_ref() -> None:
    result = _run("--dry-run")

    assert result.returncode != 0
    assert "--ref" in result.stderr


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
    for path in ('SRC_DIR="/opt/fallow/src"', 'CONFIG_DST="${CONFIG_DIR}/coordinator.toml"'):
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
