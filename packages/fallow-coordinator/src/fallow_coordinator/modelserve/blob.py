"""HTTP Range parsing and chunked, anyio-friendly file streaming."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio

# Blobs are multi-GB; stream them a mebibyte at a time to bound memory.
CHUNK_SIZE = 1024 * 1024
OCTET_STREAM = "application/octet-stream"
_RANGE_PREFIX = "bytes="


class RangeNotSatisfiable(Exception):
    """The Range header is malformed or falls outside the file."""


@dataclass(frozen=True)
class ByteRange:
    """An inclusive byte interval ``[start, end]`` within a file."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """Parse a single-range HTTP ``Range`` header against a file of ``size``.

    Returns ``None`` when no header is present (caller serves the full body) and
    raises :class:`RangeNotSatisfiable` for malformed or out-of-bounds ranges.
    Supports ``bytes=N-``, ``bytes=N-M`` and suffix ``bytes=-N``.
    """
    if header is None:
        return None
    if not header.startswith(_RANGE_PREFIX):
        raise RangeNotSatisfiable(header)
    spec = header[len(_RANGE_PREFIX) :].strip()
    if "," in spec or "-" not in spec:
        raise RangeNotSatisfiable(header)
    start_text, _, end_text = spec.partition("-")
    try:
        start, end = _resolve_bounds(start_text.strip(), end_text.strip(), size)
    except ValueError as exc:
        raise RangeNotSatisfiable(header) from exc
    if start < 0 or start >= size or end < start:
        raise RangeNotSatisfiable(header)
    return ByteRange(start=start, end=min(end, size - 1))


def _resolve_bounds(start_text: str, end_text: str, size: int) -> tuple[int, int]:
    if start_text == "":  # suffix form: last N bytes
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("suffix length must be positive")
        return max(0, size - suffix), size - 1
    start = int(start_text)
    end = size - 1 if end_text == "" else int(end_text)
    return start, end


async def stream_file(
    path: str, start: int, length: int, chunk_size: int = CHUNK_SIZE
) -> AsyncIterator[bytes]:
    """Yield ``length`` bytes of ``path`` from ``start`` in ``chunk_size`` pieces."""
    remaining = length
    async with await anyio.open_file(path, "rb") as handle:
        await handle.seek(start)
        while remaining > 0:
            chunk = await handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
