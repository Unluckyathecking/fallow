"""A local content-addressed chunk store with a size cap.

Chunks are keyed by their sha256 hex digest, so ``put`` dedups automatically:
storing the same bytes twice keeps one copy and returns the same key. The store
holds bytes in memory up to ``max_bytes`` and evicts least-recently-used chunks
when a new chunk would push it over the cap. Every access, read or write, marks
a chunk most-recently-used.

This is the on-machine cache the roadmap's peer layer will fill from the LAN.
Keeping it in memory and free of I/O makes it a leaf with nothing to import.
"""

from collections import OrderedDict

from fallow_modelmesh.chunk import chunk_hash
from fallow_modelmesh.errors import ChunkNotFound


class ChunkStore:
    """In-memory content-addressed cache with LRU, size-capped eviction."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._used = 0
        # Ordered oldest-first; the right end is most-recently-used.
        self._chunks: OrderedDict[str, bytes] = OrderedDict()

    def put(self, data: bytes) -> str:
        """Store ``data`` and return its content address.

        A chunk larger than the whole cap cannot be held and is rejected. An
        already-present chunk is a no-op beyond marking it recently used.
        """
        if len(data) > self._max_bytes:
            raise ValueError("chunk larger than store capacity")
        key = chunk_hash(data)
        if key in self._chunks:
            self._chunks.move_to_end(key)
            return key
        self._evict_until_fits(len(data))
        self._chunks[key] = data
        self._used += len(data)
        return key

    def get(self, key: str) -> bytes:
        """Return the chunk stored under ``key`` and mark it recently used."""
        try:
            data = self._chunks[key]
        except KeyError as exc:
            raise ChunkNotFound(key) from exc
        self._chunks.move_to_end(key)
        return data

    def has(self, key: str) -> bool:
        """Report whether ``key`` is currently held, without touching its age."""
        return key in self._chunks

    def availability(self) -> frozenset[str]:
        """Return the set of chunk hashes the store currently holds."""
        return frozenset(self._chunks)

    @property
    def used_bytes(self) -> int:
        """Bytes currently held across all chunks."""
        return self._used

    def _evict_until_fits(self, incoming: int) -> None:
        while self._used + incoming > self._max_bytes and self._chunks:
            _, evicted = self._chunks.popitem(last=False)
            self._used -= len(evicted)
