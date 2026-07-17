"""Content-addressed chunking.

A file is split into fixed-size chunks read in order. Each chunk is named by
the sha256 hex digest of its bytes, so two identical chunks anywhere (across
files or across model versions) carry the same name and are stored once. The
last chunk is whatever bytes remain and is usually shorter than the chunk size.

Fixed-size chunking is chosen for simplicity. It dedups identical regions that
land on the same boundaries, which fits static, aligned weight files. If real
cross-version dedup proves poor, content-defined chunking is the later swap;
see ADR 071.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

# 4 MiB. Large enough to keep the manifest small for terabyte-scale weights,
# small enough to stay a useful dedup and peer-transfer unit. See ADR 071.
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


def chunk_hash(data: bytes) -> str:
    """Return the content address of ``data``: its sha256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def iter_file_chunks(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
    """Yield the file's bytes as in-order chunks of at most ``chunk_size``.

    The file is read once, streaming, so an arbitrarily large file never has to
    fit in memory.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    with path.open("rb") as handle:
        while True:
            data = handle.read(chunk_size)
            if not data:
                return
            yield data
