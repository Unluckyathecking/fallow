"""The manifest: the root of trust for a distributed model.

A manifest lists a model's chunk hashes in order, the total size, the chunk
size, the whole-file sha256, and the Merkle root over the chunk list. It is a
frozen value: building one reads the source file, and nothing mutates it after.
The manifest is what gets signed (see ``signing``); a peer is trusted for bytes
only, never for correctness, because every byte it sends is checked against a
hash the signed manifest commits to.

The manifest is deliberately not a wire type. It serialises to canonical JSON
for signing and transport, but it is not part of the coordinator/agent protocol
schema, so it carries no schema-drift surface.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from fallow_modelmesh.chunk import DEFAULT_CHUNK_SIZE, chunk_hash, iter_file_chunks
from fallow_modelmesh.merkle import merkle_root


@dataclass(frozen=True, slots=True)
class Manifest:
    """An immutable description of a chunked model file.

    ``chunks`` is the ordered tuple of per-chunk sha256 hex digests. The same
    hash may appear more than once when a file repeats a chunk; storage dedups
    it, but the manifest keeps every position so the file reconstructs exactly.
    """

    model_id: str
    total_size: int
    chunk_size: int
    whole_file_sha256: str
    chunks: tuple[str, ...]
    merkle_root: str

    def canonical_bytes(self) -> bytes:
        """Return the deterministic byte encoding signed and verified.

        Keys are sorted and separators are fixed, so the same manifest always
        produces the same bytes on any machine and any run.
        """
        payload = {
            "model_id": self.model_id,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "whole_file_sha256": self.whole_file_sha256,
            "chunks": list(self.chunks),
            "merkle_root": self.merkle_root,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_manifest(path: Path, model_id: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Manifest:
    """Chunk ``path`` and return its signed-ready manifest.

    The file is read once. The whole-file digest is accumulated across the same
    chunks used for content addressing, so a single pass yields the chunk list,
    the total size, and the whole-file hash together.
    """
    whole = hashlib.sha256()
    chunks: list[str] = []
    total = 0
    for data in iter_file_chunks(path, chunk_size):
        whole.update(data)
        chunks.append(chunk_hash(data))
        total += len(data)
    ordered = tuple(chunks)
    return Manifest(
        model_id=model_id,
        total_size=total,
        chunk_size=chunk_size,
        whole_file_sha256=whole.hexdigest(),
        chunks=ordered,
        merkle_root=merkle_root(ordered),
    )
