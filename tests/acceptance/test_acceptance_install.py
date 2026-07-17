"""Rows 1-2: clean install and login persistence.

The installers render their launch item from a template with a dry-run seam.
These tests drive that seam and assert the rendered output, then assert the
template wiring directly so the persistence checks hold on any host even when
the platform installer cannot run here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acceptance_helpers import (
    MACOS_PLIST_TEMPLATE,
    WINDOWS_TASK_TEMPLATE,
    render_macos_plist,
    render_windows_task,
)

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="drives the macOS installer")
# install.ps1 reads Windows-only env (USERPROFILE, USERDOMAIN), so its dry run
# only renders on Windows. The task wiring is asserted from the template on
# every host by test_scheduled_task_template_starts_at_logon_not_boot below.
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="drives the Windows installer")


def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "agentctl"
    binary.write_bytes(b"fake")
    return binary


@darwin_only
def test_macos_dry_run_renders_without_touching_the_system(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    result = render_macos_plist(home, _fake_binary(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "<plist" in result.stdout
    # Login persistence + relaunch after a crash.
    assert "<key>RunAtLoad</key>" in result.stdout
    assert "<key>KeepAlive</key>" in result.stdout
    # A dry run must not create the launch item or the state tree.
    assert not (home / "Library" / "LaunchAgents").exists()
    assert not (home / ".fallow").exists()


@windows_only
def test_windows_dry_run_renders_the_scheduled_task(tmp_path: Path) -> None:
    result = render_windows_task(_fake_binary(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "<Task" in result.stdout
    assert "<LogonTrigger>" in result.stdout
    assert "<RestartOnFailure>" in result.stdout


def test_launchagent_template_starts_at_login_and_relaunches() -> None:
    plist = MACOS_PLIST_TEMPLATE.read_text(encoding="utf-8")

    assert "<key>RunAtLoad</key>" in plist
    assert "<key>KeepAlive</key>" in plist
    # Standard module entry point, so no source edit is needed to install.
    assert "<string>fallow_agent</string>" in plist


def test_scheduled_task_template_starts_at_logon_not_boot() -> None:
    xml = WINDOWS_TASK_TEMPLATE.read_text(encoding="utf-8")

    assert "<LogonTrigger>" in xml
    assert "<RestartOnFailure>" in xml
    # It must start at login (GUI session for idle detection), never at boot.
    assert "<BootTrigger>" not in xml
    assert "InteractiveToken" in xml
