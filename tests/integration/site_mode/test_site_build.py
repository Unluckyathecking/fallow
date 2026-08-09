"""The acceptance lane never silently skips: it builds cmd/agentctl or fails loud.

CI does not prebuild the Go agent, so the harness builds it from this exact head
when ``FALLOW_GO_AGENT_BIN`` is unset. These guard that contract without a full
scenario: the built binary is a runnable agentctl, and a missing Go toolchain
raises rather than skipping.
"""

from __future__ import annotations

import subprocess

import pytest

from site_mode import site_harness
from site_mode.site_harness import build_go_agent_binary, go_agent_binary


def test_harness_builds_a_runnable_agentctl() -> None:
    binary = go_agent_binary()
    assert binary.is_file()
    result = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "agentctl version produced no output"


def test_missing_go_toolchain_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force a rebuild path (ignore any cached binary) with no ``go`` on PATH.
    monkeypatch.setattr(site_harness, "_BUILT_BINARY", None)
    monkeypatch.delenv(site_harness._GO_AGENT_BIN_ENV, raising=False)
    monkeypatch.setattr(site_harness.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="no 'go' toolchain"):
        build_go_agent_binary()
