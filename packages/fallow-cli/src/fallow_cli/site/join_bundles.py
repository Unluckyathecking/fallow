"""Secure local writing of Site Mode join artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fallow_cli.errors import CliError
from fallow_cli.models import SiteJoinBundle


def write_join_bundles(
    bundles: tuple[SiteJoinBundle, ...], output: Path, *, force: bool
) -> list[dict[str, Any]]:
    """Write validated bundles without leaving a partially updated batch."""
    if not bundles:
        raise CliError("coordinator returned no join bundles")
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(f"could not create join file directory: {exc}") from exc

    paths = [output / f"desk-{i:02d}.fallow-join" for i in range(1, len(bundles) + 1)]
    if not force and any(path.exists() for path in paths):
        raise CliError("join file already exists; use --force to overwrite")

    temporary: list[Path] = []
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, bundle in zip(paths, bundles, strict=True):
            temporary.append(_write_temporary(output, path.name, bundle))
        if force:
            for path in paths:
                if path.exists():
                    backups[path] = _copy_temporary(output, path.name, path)
        for path, temp in zip(paths, temporary, strict=True):
            os.replace(temp, path)
            replaced.append(path)
    except OSError as exc:
        for path in reversed(replaced):
            backup = backups.get(path)
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
        raise CliError(f"could not write join files: {exc}") from exc
    finally:
        for path in [*temporary, *backups.values()]:
            path.unlink(missing_ok=True)

    return [
        {
            "path": str(path),
            "site_id": bundle.site_id,
            "coordinator_urls": list(bundle.coordinator_urls),
            "pin_prefix": bundle.coordinator_spki_sha256[0][:16],
        }
        for path, bundle in zip(paths, bundles, strict=True)
    ]


def _write_temporary(output: Path, name: str, bundle: SiteJoinBundle) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f".{name}.", dir=output)
    path = Path(raw_path)
    try:
        # newline="" keeps the written "\n" byte-for-byte on every platform so
        # the portable join file is identical on Windows and POSIX.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(bundle.model_dump(mode="json"), handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


def _copy_temporary(output: Path, name: str, source: Path) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f".{name}.backup.", dir=output)
    path = Path(raw_path)
    try:
        with source.open("rb") as input_handle, os.fdopen(fd, "wb") as output_handle:
            while chunk := input_handle.read(1024 * 1024):
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.chmod(path, 0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path
