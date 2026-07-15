"""Offline bundle verification and read-only install previews."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHELL_SCRIPT = ROOT / "deploy" / "bundle.sh"
POWERSHELL_SCRIPT = ROOT / "deploy" / "bundle.ps1"


def _fixture_bundle(path: Path) -> Path:
    files = {
        "config/agent.toml": b"coordinator_url = 'http://127.0.0.1:8330'\n",
        "llama/macos-arm64/llama-server": b"binary",
        "llama/windows-x64-cuda/llama-server.exe": b"binary",
        "llama/windows-x64-cuda/cudart64_12.dll": b"runtime",
        "requirements.lock.txt": b"anyio==4.0\n",
        "wheels/workspace/fallow_agent-0.1.0-py3-none-any.whl": b"wheel",
    }
    lines: list[str] = []
    for relative, content in sorted(files.items()):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        lines.append(f"{hashlib.sha256(content).hexdigest()}  {relative}\n")
    (path / "manifest.sha256").write_text("".join(lines), encoding="utf-8")
    return path


def _run(bundle: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    if sys.platform == "win32":
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(POWERSHELL_SCRIPT),
                *arguments,
                "-Bundle",
                str(bundle),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    return subprocess.run(
        ["bash", str(SHELL_SCRIPT), *arguments, "--bundle", str(bundle)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_verifies_every_file_before_install_preview(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    prefix = tmp_path / "target"
    arguments = (
        ("Install", "-DryRun", "-Prefix", str(prefix))
        if sys.platform == "win32"
        else ("install", "--dry-run", "--prefix", str(prefix))
    )

    result = _run(bundle, *arguments)

    assert result.returncode == 0, result.stderr
    assert "Would create" in result.stdout
    assert not prefix.exists()


def test_hash_failure_happens_before_target_changes(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    prefix = tmp_path / "target"
    (bundle / "config" / "agent.toml").write_text("tampered", encoding="utf-8")
    arguments = (
        ("Install", "-Prefix", str(prefix))
        if sys.platform == "win32"
        else ("install", "--prefix", str(prefix))
    )

    result = _run(bundle, *arguments)

    assert result.returncode != 0
    assert "hash mismatch" in result.stderr.lower()
    assert not prefix.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="PowerShell canonicalizes manifest paths")
@pytest.mark.parametrize("alias", ("..", "./config/agent.toml", "config//agent.toml"))
def test_shell_verifier_rejects_noncanonical_manifest_alias(tmp_path: Path, alias: str) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    manifest = bundle / "manifest.sha256"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    digest = lines[0].split("  ", 1)[0]
    lines[-1] = f"{digest}  {alias}"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SHELL_SCRIPT), "verify", str(bundle)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe manifest path" in result.stderr.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the shell verifier")
def test_shell_verifier_counts_nested_manifest_named_files(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    nested = bundle / "models" / "manifest.sha256"
    nested.parent.mkdir()
    nested.write_text("model-side metadata", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SHELL_SCRIPT), "verify", str(bundle)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "does not cover every bundle file" in result.stderr.lower()


def test_bundle_pins_match_platform_fetchers() -> None:
    bundle = SHELL_SCRIPT.read_text(encoding="utf-8")
    mac_fetcher = (ROOT / "deploy" / "fetch-llama.sh").read_text(encoding="utf-8")
    windows_fetcher = (ROOT / "deploy" / "windows" / "fetch-llama.ps1").read_text(encoding="utf-8")

    def assignment(source: str, name: str) -> str:
        match = re.search(
            rf"(?m)^\s*\$?{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]",
            source,
        )
        assert match is not None, f"missing assignment for {name}"
        return match.group(1)

    assert assignment(bundle, "LLAMA_RELEASE") == assignment(mac_fetcher, "LLAMA_RELEASE")
    assert assignment(bundle, "LLAMA_RELEASE") == assignment(windows_fetcher, "LlamaRelease")
    assert assignment(bundle, "CUDA_TAG") == assignment(windows_fetcher, "CudaTag")


@pytest.mark.parametrize("target", ("macos-arm64", "windows-x64"))
def test_requirement_filter_uses_explicit_target_versions(tmp_path: Path, target: str) -> None:
    source = tmp_path / "requirements.txt"
    output = tmp_path / "selected.txt"
    source.write_text(
        "target-python; implementation_version == '3.12.0'\n"
        "target-release; platform_release == ''\n"
        "target-version; platform_version == ''\n"
        "host-python; implementation_version != '3.12.0'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "filter_bundle_requirements.py"),
            str(source),
            str(output),
            "--target",
            target,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "target-python",
        "target-release",
        "target-version",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlinks need extra privileges")
def test_shell_verifier_rejects_unlisted_symbolic_link(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    (bundle / "unexpected").symlink_to(bundle / "config" / "agent.toml")

    result = _run(bundle, "install", "--dry-run")

    assert result.returncode != 0
    assert "symbolic link" in result.stderr.lower()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
def test_powershell_manifest_rejects_parent_traversal(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    digest = hashlib.sha256(b"outside").hexdigest()
    (tmp_path / "outside").write_bytes(b"outside")
    (bundle / "manifest.sha256").write_text(f"{digest}  ../outside\n", encoding="utf-8")

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(POWERSHELL_SCRIPT),
            "Verify",
            "-Bundle",
            str(bundle),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe manifest path" in result.stderr.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows hidden attributes")
def test_powershell_verifier_counts_unlisted_hidden_files(tmp_path: Path) -> None:
    bundle = _fixture_bundle(tmp_path / "bundle")
    hidden = bundle / "wheels" / "hidden.whl"
    hidden.write_bytes(b"unlisted")
    subprocess.run(["attrib", "+h", str(hidden)], check=True)

    result = _run(bundle, "Verify")

    assert result.returncode != 0
    assert "does not cover every bundle file" in result.stderr.lower()
