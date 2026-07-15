"""Bounded, content-addressed storage for work-unit result payloads."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterable
from pathlib import Path

import anyio

CHUNK_SIZE = 1024 * 1024


class ResultPayloadTooLarge(Exception):
    """A result stream crossed the configured byte limit."""

    def __init__(self, max_payload_bytes: int, received_payload_bytes: int) -> None:
        self.max_payload_bytes = max_payload_bytes
        self.received_payload_bytes = received_payload_bytes
        super().__init__(
            f"result payload exceeded {max_payload_bytes} bytes "
            f"after receiving {received_payload_bytes} bytes"
        )


class ResultBlobStore:
    """Store async payload streams under their lowercase SHA-256 digest."""

    def __init__(self, result_dir: Path, max_payload_bytes: int) -> None:
        self._result_dir = result_dir
        self._max_payload_bytes = max_payload_bytes

    async def put(self, stream: AsyncIterable[bytes]) -> str:
        """Persist ``stream`` and return its lowercase SHA-256 digest."""
        descriptor, raw_temp_path = tempfile.mkstemp(prefix=".result-", dir=self._result_dir)
        os.close(descriptor)
        temp_path = Path(raw_temp_path)
        digest = hashlib.sha256()
        received = 0
        try:
            async with await anyio.open_file(temp_path, "wb") as handle:
                async for incoming in stream:
                    for offset in range(0, len(incoming), CHUNK_SIZE):
                        chunk = incoming[offset : offset + CHUNK_SIZE]
                        received += len(chunk)
                        if received > self._max_payload_bytes:
                            raise ResultPayloadTooLarge(self._max_payload_bytes, received)
                        digest.update(chunk)
                        await handle.write(chunk)
            hex_digest = digest.hexdigest()
            destination = self._result_dir / hex_digest
            if destination.exists():
                return hex_digest
            os.replace(temp_path, destination)
            return hex_digest
        finally:
            temp_path.unlink(missing_ok=True)
