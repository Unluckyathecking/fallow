"""Delta set: which chunks a store still needs for a target manifest.

Content addressing makes this cheap and dedup-aware. Chunks shared between an
old and a new manifest carry the same hash, so a store already holding the old
version is only ever asked to fetch the genuinely new chunks. The result is the
distinct missing hashes in first-seen order, which is a stable fetch plan.
"""

from fallow_modelmesh.manifest import Manifest
from fallow_modelmesh.store import ChunkStore


def missing_chunks(manifest: Manifest, store: ChunkStore) -> tuple[str, ...]:
    """Return the distinct chunk hashes ``manifest`` needs that ``store`` lacks.

    Order follows first appearance in the manifest; each missing hash appears
    once even if the file repeats it.
    """
    have = store.availability()
    seen: set[str] = set()
    missing: list[str] = []
    for h in manifest.chunks:
        if h in have or h in seen:
            continue
        seen.add(h)
        missing.append(h)
    return tuple(missing)
