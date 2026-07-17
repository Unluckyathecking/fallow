"""Row 9 (docs section 6): uninstall removes the launch item and state.

Runs the real macOS uninstaller against a throwaway HOME seeded with a planted
launch item and ~/.fallow tree. launchctl and lsof are absent or no-ops here,
which is fine: the script guards them, and this test asserts the file-level
removal that a clean uninstall must do. Live port and process reclaim on a
serving machine stays a manual check (see the matrix).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acceptance_helpers import plant_install_artifacts, run_macos_uninstall

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="exercises the shell uninstaller")


def test_uninstall_removes_launch_item_and_preserves_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plist = plant_install_artifacts(home)

    result = run_macos_uninstall(home)

    assert result.returncode == 0, result.stderr
    assert not plist.exists()
    # Without the purge flag, enrolled state is left in place for a reinstall.
    assert (home / ".fallow").exists()


def test_purge_uninstall_removes_all_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plant_install_artifacts(home)

    result = run_macos_uninstall(home, "--purge")

    assert result.returncode == 0, result.stderr
    assert not (home / ".fallow").exists()
