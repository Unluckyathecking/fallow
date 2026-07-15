"""On-disk layout helpers for the model cache.

Layout::

    cache_dir/<model_id>/<file_name>          # verified blob
    cache_dir/<model_id>/<file_name>.part     # in-flight / interrupted download
    cache_dir/<model_id>/<file_name>.sha256   # verification marker

The marker records the sha256 that was verified for the sibling blob; its
presence-and-match is the cheap "is this model trusted?" signal used on the
heartbeat hot path (no rehashing of multi-GB files).
"""

from pathlib import Path

from fallow_agent.modelcache.config import _TMP_SUFFIX, MARKER_SUFFIX, PART_SUFFIX
from fallow_protocol.models import ModelManifest


def model_dir(cache_dir: Path, manifest: ModelManifest) -> Path:
    """Directory holding every artefact for one model."""
    return cache_dir / manifest.model_id


def blob_path(cache_dir: Path, manifest: ModelManifest) -> Path:
    """Final, verified blob path."""
    return model_dir(cache_dir, manifest) / manifest.file_name


def part_path(cache_dir: Path, manifest: ModelManifest) -> Path:
    """Partial-download path (append target while fetching)."""
    return model_dir(cache_dir, manifest) / f"{manifest.file_name}{PART_SUFFIX}"


def marker_path(cache_dir: Path, manifest: ModelManifest) -> Path:
    """Verification-marker path."""
    return model_dir(cache_dir, manifest) / f"{manifest.file_name}{MARKER_SUFFIX}"


def read_marker(marker: Path) -> str | None:
    """Return the stored sha256 (stripped), or None if absent/unreadable."""
    if not marker.exists():
        return None
    try:
        return marker.read_text(encoding="ascii").strip()
    except OSError:
        return None


def write_marker_atomic(marker: Path, sha256: str) -> None:
    """Write the verification marker atomically (temp file + rename)."""
    tmp = marker.with_name(marker.name + _TMP_SUFFIX)
    tmp.write_text(sha256, encoding="ascii")
    tmp.replace(marker)
