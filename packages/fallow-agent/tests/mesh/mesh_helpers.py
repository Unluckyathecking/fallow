"""In-memory fakes for the agent mesh tests: no network, no real model.

A signed payload is built with the modelmesh public API exactly as the
coordinator would, so these tests exercise the real verification path. Fake
peers and a fake transport stand in for the LAN and the coordinator's chunk
endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fallow_modelmesh import (
    Manifest,
    Peer,
    build_manifest,
    chunk_hash,
    sign_manifest,
)

KEY = b"shared-mesh-signing-key"
CHUNK_SIZE = 1024


def signed_payload(manifest: Manifest, key: bytes = KEY) -> dict[str, Any]:
    """Encode a manifest and its signature in the coordinator's wire shape."""
    return {
        "manifest": {
            "model_id": manifest.model_id,
            "total_size": manifest.total_size,
            "chunk_size": manifest.chunk_size,
            "whole_file_sha256": manifest.whole_file_sha256,
            "chunks": list(manifest.chunks),
            "merkle_root": manifest.merkle_root,
        },
        "signature": sign_manifest(manifest, key),
    }


def write_blob(path: Path, data: bytes) -> Manifest:
    """Write ``data`` to ``path`` and return its manifest at the test chunk size."""
    path.write_bytes(data)
    return build_manifest(path, path.stem, CHUNK_SIZE)


def chunks_of(data: bytes, chunk_size: int = CHUNK_SIZE) -> dict[str, bytes]:
    """Map each chunk hash of ``data`` to its bytes."""
    out: dict[str, bytes] = {}
    for i in range(0, len(data), chunk_size):
        piece = data[i : i + chunk_size]
        out[chunk_hash(piece)] = piece
    return out


class FakePeer:
    """A :class:`~fallow_modelmesh.Peer` over an in-memory chunk map.

    Records every chunk fetched so a test can assert a delta upgrade pulled only
    the changed chunks. ``corrupt`` makes it serve wrong bytes to model a lying
    peer.
    """

    def __init__(self, chunks: dict[str, bytes], *, corrupt: bool = False) -> None:
        self._chunks = chunks
        self._corrupt = corrupt
        self.fetched: list[str] = []

    def available(self) -> frozenset[str]:
        return frozenset(self._chunks)

    def fetch(self, chunk_hash: str) -> bytes:
        self.fetched.append(chunk_hash)
        if self._corrupt:
            return b"tampered bytes"
        return self._chunks[chunk_hash]


class FakeSession:
    """A mesh session returning a canned payload and canned peers."""

    def __init__(self, payload: Any, peers: Sequence[Peer]) -> None:
        self._payload = payload
        self._peers = peers

    def signed_manifest(self, model_id: str) -> Any:
        return self._payload

    def peers(self, model_id: str, manifest: Manifest) -> Sequence[Peer]:
        return self._peers


class FakeTransport:
    """A transport whose ``session`` yields one :class:`FakeSession`."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    @contextmanager
    def session(self) -> Iterator[FakeSession]:
        yield self._session


class RaisingTransport:
    """A transport whose session raises on first use, to model a dead coordinator."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @contextmanager
    def session(self) -> Iterator[FakeSession]:
        raise self._error
        yield  # pragma: no cover - unreachable, satisfies the generator contract
