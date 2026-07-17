"""Shared helpers for the Phase-A acceptance suite.

Per ADR 023 the non-fixture code lives here, not in conftest. These drive the
real installer scripts under deploy/ in their dry-run render modes and the
uninstall scripts against a throwaway HOME. Nothing here reaches a real
coordinator, model, or network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"

MACOS_INSTALL = DEPLOY_DIR / "macos" / "install.sh"
MACOS_UNINSTALL = DEPLOY_DIR / "macos" / "uninstall.sh"
MACOS_PLIST_TEMPLATE = DEPLOY_DIR / "macos" / "com.fallow.agent.plist"
WINDOWS_INSTALL = DEPLOY_DIR / "windows" / "install.ps1"
WINDOWS_TASK_TEMPLATE = DEPLOY_DIR / "windows" / "fallow-agent-task.xml"


def render_macos_plist(home: Path, fake_binary: Path) -> subprocess.CompletedProcess[str]:
    """Run the macOS installer's dry run and return the rendered plist on stdout.

    Uses the prebuilt-binary flavour so the render needs no uv or venv. Dry run
    prints the plist and exits before it touches launchctl or the filesystem.
    """
    return subprocess.run(
        ["bash", str(MACOS_INSTALL), "--go-binary", str(fake_binary)],
        capture_output=True,
        check=False,
        text=True,
        env={"HOME": str(home), "PATH": _path(), "FALLOW_INSTALL_DRY_RUN": "1"},
    )


def render_windows_task(fake_binary: Path) -> subprocess.CompletedProcess[str]:
    """Run the Windows installer's -DryRun and return the rendered task XML.

    Uses -GoBinary so the render needs no uv, and PowerShell can run it on any
    host. Caller must skip when pwsh is unavailable.
    """
    return subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(WINDOWS_INSTALL),
            "-GoBinary",
            str(fake_binary),
            "-DryRun",
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def run_macos_uninstall(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the macOS uninstaller against a throwaway HOME."""
    return subprocess.run(
        ["bash", str(MACOS_UNINSTALL), *args],
        capture_output=True,
        check=False,
        text=True,
        env={"HOME": str(home), "PATH": _path()},
    )


def plant_install_artifacts(home: Path) -> Path:
    """Seed a fake installed agent under HOME: the launch item plist plus the
    ~/.fallow state tree. Returns the planted plist path."""
    plist = home / "Library" / "LaunchAgents" / "com.fallow.agent.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>\n", encoding="utf-8")

    fallow_home = home / ".fallow"
    (fallow_home / "logs").mkdir(parents=True, exist_ok=True)
    (fallow_home / "models").mkdir(parents=True, exist_ok=True)
    (fallow_home / "agent.toml").write_text("coordinator_url = ''\n", encoding="utf-8")
    (fallow_home / "logs" / "agent.err.log").write_text("", encoding="utf-8")
    return plist


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
