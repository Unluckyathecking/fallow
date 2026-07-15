"""Resumable, incrementally-hashed blob streaming.

This module owns exactly one HTTP attempt. Retry/backoff and verification live
in :mod:`fallow_agent.modelcache.store`; here we only:

- rehash any pre-existing ``.part`` prefix once (it may predate this process),
- send a ``Range`` request to resume,
- honour 206 (append) vs 200 (server ignored Range -> restart from zero),
- stream to disk in fixed chunks, hashing as bytes arrive.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, Protocol

import httpx

from fallow_agent.modelcache.config import HTTP_OK, HTTP_PARTIAL_CONTENT


class DownloadResult(NamedTuple):
    """Outcome of one completed stream: the running hash and total byte count."""

    sha256: str
    size: int


class RetryableStatusError(Exception):
    """A blob request returned a status we neither append to nor restart from."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"unexpected blob status {status_code}")
        self.status_code = status_code


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


def _seed_from_existing(hasher: _Hasher, part: Path, block_size: int) -> int:
    """Feed an existing ``.part`` prefix through ``hasher``; return its size.

    The partial file may have been written by an earlier process, so we cannot
    assume any in-memory hash state carried over — we rehash the prefix once.
    """
    if not part.exists():
        return 0
    size = 0
    with part.open("rb") as fh:
        while True:
            block = fh.read(block_size)
            if not block:
                break
            hasher.update(block)
            size += len(block)
    return size


def _resolve_disposition(status_code: int, existing: int) -> tuple[bool, int]:
    """Map (status, resume offset) to (append?, bytes already counted).

    206 -> append to the prefix. 200 -> restart from zero even if we asked for a
    range (the coordinator ignored it). Anything else is retryable.
    """
    if status_code == HTTP_PARTIAL_CONTENT:
        return True, existing
    if status_code == HTTP_OK:
        return False, 0
    raise RetryableStatusError(status_code)


async def stream_to_part(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    part: Path,
    chunk_size: int,
) -> DownloadResult:
    """Perform one resume-aware download attempt into ``part``."""
    hasher = hashlib.sha256()
    existing = _seed_from_existing(hasher, part, chunk_size)
    req_headers = dict(headers)
    if existing:
        req_headers["Range"] = f"bytes={existing}-"

    async with client.stream("GET", url, headers=req_headers) as response:
        append, total = _resolve_disposition(response.status_code, existing)
        if not append:
            hasher = hashlib.sha256()  # restart / full: discard seeded prefix
        with part.open("ab") if append else part.open("wb") as fh:
            async for chunk in response.aiter_bytes(chunk_size):
                fh.write(chunk)
                hasher.update(chunk)
                total += len(chunk)

    return DownloadResult(sha256=hasher.hexdigest(), size=total)
