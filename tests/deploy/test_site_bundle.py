"""Site Mode desk bundle: manifest discipline and script closure."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHELL_SCRIPT = ROOT / "deploy" / "site-bundle.sh"
BUNDLE_NAME = "fallow-site-agent_0.1.0-test_windows_amd64"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the site bundler is bash-only")


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SHELL_SCRIPT), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    agent = tmp_path / "agentctl.exe"
    agent.write_bytes(b"MZ stub")
    result = _run(
        "build",
        "--agent",
        str(agent),
        "--version",
        "0.1.0-test",
        "--output",
        str(tmp_path / "dist"),
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / "dist" / BUNDLE_NAME


def test_build_ships_one_zip_holding_the_installer_and_the_agent(bundle: Path) -> None:
    archive = bundle.parent / f"{BUNDLE_NAME}.zip"

    assert archive.is_file()
    names = set(zipfile.ZipFile(archive).namelist())
    assert {
        f"{BUNDLE_NAME}/agentctl.exe",
        f"{BUNDLE_NAME}/agent.example.toml",
        f"{BUNDLE_NAME}/README.md",
        f"{BUNDLE_NAME}/manifest.sha256",
        f"{BUNDLE_NAME}/windows/install.ps1",
        f"{BUNDLE_NAME}/windows/doctor.ps1",
        f"{BUNDLE_NAME}/windows/lib/backend.ps1",
    } <= names
    assert _run("verify", str(bundle)).returncode == 0


def test_verify_rejects_a_tampered_file(bundle: Path) -> None:
    (bundle / "windows" / "install.ps1").write_text("whoami", encoding="utf-8")

    result = _run("verify", str(bundle))

    assert result.returncode != 0
    assert "hash mismatch" in result.stderr.lower()


def test_verify_rejects_an_unlisted_file(bundle: Path) -> None:
    (bundle / "windows" / "extra.ps1").write_text("whoami", encoding="utf-8")

    result = _run("verify", str(bundle))

    assert result.returncode != 0
    assert "does not cover every bundle file" in result.stderr.lower()


def test_bundled_scripts_resolve_every_relative_reference(bundle: Path) -> None:
    """The layout mirrors deploy/, so $ScriptDir and $DeployDir must both land."""
    reference = re.compile(r"Join-Path \$(ScriptDir|DeployDir) '([^']+)'")
    missing = []
    for script in sorted(bundle.rglob("*.ps1")):
        for base, relative in reference.findall(script.read_text(encoding="utf-8")):
            # bin\windows is staged by fetch-llama.ps1 on the desk, not shipped.
            if relative == "bin\\windows":
                continue
            root = script.parent if base == "ScriptDir" else script.parent.parent
            target = root.joinpath(*relative.split("\\"))
            if not target.exists():
                missing.append(f"{script.relative_to(bundle)} -> ${base}\\{relative}")

    assert not missing, f"bundle does not carry: {missing}"
