"""Secure local writing of Site Mode join artifacts."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from fallow_cli.errors import CliError
from fallow_cli.models import SiteJoinBundle

def write_join_bundles(bundles: tuple[SiteJoinBundle, ...], output: Path, *, force: bool) -> list[dict[str, str]]:
    if not bundles: raise CliError("coordinator returned no join bundles")
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / f"desk-{i:02d}.fallow-join" for i in range(1, len(bundles)+1)]
    if not force and any(p.exists() for p in paths): raise CliError("join file already exists; use --force to overwrite")
    written: list[Path] = []
    try:
        for path, bundle in zip(paths, bundles, strict=True):
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=output)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(bundle.model_dump(mode="json"), fh, separators=(",", ":"))
                    fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
                os.replace(tmp, path); written.append(path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
    except OSError as exc:
        for path in written: path.unlink(missing_ok=True)
        raise CliError(f"could not write join files: {exc}") from exc
    return [{"path": str(path), "site_id": bundle.site_id, "pin_prefix": bundle.coordinator_spki_sha256[0][:16]} for path, bundle in zip(paths, bundles, strict=True)]
