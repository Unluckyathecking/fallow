"""Secure local writing of Site Mode join artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fallow_cli.errors import CliError
from fallow_cli.models import SiteJoinBundle


def join_bundle_paths(output: Path, count: int) -> list[Path]:
    """Return the deterministic ``desk-NN.fallow-join`` paths for a batch."""
    return [output / f"desk-{i:02d}.fallow-join" for i in range(1, count + 1)]


def preflight_destinations(output: Path, count: int, *, force: bool) -> None:
    """Refuse a non-``--force`` write before any one-use token is minted.

    Minting join bundles burns short-lived coordinator tokens, so a destination
    that already exists must be caught *before* the network call. The atomic
    no-clobber install still re-checks at write time to close the race.
    """
    if force:
        return
    if any(path.exists() for path in join_bundle_paths(output, count)):
        raise CliError("join file already exists; use --force to overwrite")


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

    paths = join_bundle_paths(output, len(bundles))
    if not force and any(path.exists() for path in paths):
        raise CliError("join file already exists; use --force to overwrite")

    if force:
        _install_overwriting(output, paths, bundles)
    else:
        _install_no_clobber(output, paths, bundles)

    return [
        {
            "path": str(path),
            "site_id": bundle.site_id,
            "coordinator_urls": list(bundle.coordinator_urls),
            "pin_prefix": bundle.coordinator_spki_sha256[0][:16],
        }
        for path, bundle in zip(paths, bundles, strict=True)
    ]


def _install_no_clobber(
    output: Path, paths: list[Path], bundles: tuple[SiteJoinBundle, ...]
) -> None:
    """Create every join file, failing if any already exists.

    ``os.link`` is an atomic create-or-fail, so two concurrent invocations can
    never both believe they created the same file: the loser's link raises and
    its partial batch is rolled back, leaving the winner's files untouched.
    """
    temporary: list[Path] = []
    created: list[Path] = []
    try:
        for path, bundle in zip(paths, bundles, strict=True):
            temporary.append(_write_temporary(output, path.name, bundle))
        for path, temp in zip(paths, temporary, strict=True):
            os.link(temp, path)
            created.append(path)
    except OSError as exc:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        if isinstance(exc, FileExistsError):
            raise CliError("join file already exists; use --force to overwrite") from exc
        raise CliError(f"could not write join files: {exc}") from exc
    finally:
        for temp in temporary:
            temp.unlink(missing_ok=True)


def _install_overwriting(
    output: Path, paths: list[Path], bundles: tuple[SiteJoinBundle, ...]
) -> None:
    """Replace existing join files atomically, rolling back on failure.

    Each pre-existing file is copied to a backup first; if a replace fails the
    already-replaced files are restored from their backups. A restore that
    itself fails keeps its backup on disk and its location is reported rather
    than being silently deleted.
    """
    temporary: list[Path] = []
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, bundle in zip(paths, bundles, strict=True):
            temporary.append(_write_temporary(output, path.name, bundle))
        for path in paths:
            if path.exists():
                backups[path] = _copy_temporary(output, path.name, path)
        for path, temp in zip(paths, temporary, strict=True):
            os.replace(temp, path)
            replaced.append(path)
    except OSError as exc:
        preserved = _rollback(replaced, backups)
        message = f"could not write join files: {exc}"
        if preserved:
            locations = ", ".join(str(path) for path in preserved)
            message += f"; preserved un-restored backups at {locations}"
        raise CliError(message) from exc
    finally:
        for path in [*temporary, *backups.values()]:
            path.unlink(missing_ok=True)


def _rollback(replaced: list[Path], backups: dict[Path, Path]) -> list[Path]:
    """Restore replaced files from their backups; return any that could not be.

    Successfully restored (or never-backed-up) entries are removed from
    ``backups`` so the caller's cleanup does not delete a backup that is still
    the only surviving copy of a file it failed to restore.
    """
    preserved: list[Path] = []
    for path in reversed(replaced):
        backup = backups.pop(path, None)
        if backup is None:
            path.unlink(missing_ok=True)
            continue
        try:
            os.replace(backup, path)
        except OSError:
            preserved.append(backup)
    return preserved


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
        _restrict_to_owner(path)
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
        _restrict_to_owner(path)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable and writable only by the current user.

    POSIX uses ``chmod 0o600``. On Windows ``chmod`` only toggles the read-only
    bit, so an explicit owner-only DACL is applied with ``icacls``: inheritance
    is removed and the current account's SID is granted full control, leaving no
    access for ``Users``, ``Authenticated Users`` or ``Everyone``.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    sid = _current_windows_sid()
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(f"could not restrict join file permissions: {detail}")


def _current_windows_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError("could not determine the current Windows account")
    fields = result.stdout.strip().strip('"').split('","')
    sid = fields[-1].strip().strip('"')
    if not sid.startswith("S-"):
        raise OSError("could not parse the current Windows account SID")
    return sid
