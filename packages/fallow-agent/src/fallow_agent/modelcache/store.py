"""``HttpModelStore``: the concrete agent-side model cache.

Implements :class:`fallow_protocol.interfaces.ModelStore`. Pulls GGUF blobs
from the coordinator with resume + retry, verifies sha256/size, and keeps
verified files on local disk keyed by a cheap marker so the heartbeat hot path
never rehashes a multi-GB file.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from fallow_agent.modelcache.config import BLOB_PATH_TEMPLATE, DEFAULT_CACHE_DIR, ModelCacheConfig
from fallow_agent.modelcache.download import (
    DownloadResult,
    RetryableStatusError,
    stream_to_part,
)
from fallow_agent.modelcache.errors import ModelFetchError, ModelVerificationError
from fallow_agent.modelcache.paths import (
    blob_path,
    marker_path,
    part_path,
    read_marker,
    write_marker_atomic,
)
from fallow_protocol.interfaces import ModelStore
from fallow_protocol.models import ModelManifest

SleepFn = Callable[[float], Awaitable[None]]


class HttpModelStore(ModelStore):
    """Download-with-resume model cache backed by an injected HTTP client."""

    def __init__(
        self,
        *,
        base_url: str,
        device_token: str,
        client: httpx.AsyncClient,
        cache_dir: Path | None = None,
        config: ModelCacheConfig | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._cache_dir = (cache_dir or DEFAULT_CACHE_DIR).expanduser()
        self._config = config or ModelCacheConfig()
        self._sleep = sleep
        self._auth_headers = {"Authorization": f"Bearer {device_token}"}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── ModelStore interface ─────────────────────────────────────────────

    def path_if_present(self, manifest: ModelManifest) -> Path | None:
        """Return the blob path iff it exists and its marker matches the
        manifest sha256. Trusts the marker: does NOT rehash the file."""
        blob = blob_path(self._cache_dir, manifest)
        if not blob.exists():
            return None
        if read_marker(marker_path(self._cache_dir, manifest)) == manifest.sha256:
            return blob
        return None

    async def ensure(self, manifest: ModelManifest) -> Path:
        """Return a verified local path, downloading (with resume) if needed.

        A per-model lock serialises concurrent callers so a model is fetched at
        most once; the second caller re-checks presence under the lock.
        """
        present = self.path_if_present(manifest)
        if present is not None:
            return present
        async with self._lock_for(manifest.model_id):
            present = self.path_if_present(manifest)
            if present is not None:
                return present
            return await self._download_and_verify(manifest)

    # ── internals ────────────────────────────────────────────────────────

    def _lock_for(self, model_id: str) -> asyncio.Lock:
        # Safe without a guard: dict get/set has no await between them.
        lock = self._locks.get(model_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[model_id] = lock
        return lock

    def _blob_url(self, model_id: str) -> str:
        return f"{self._base_url}{BLOB_PATH_TEMPLATE.format(model_id=model_id)}"

    async def _download_and_verify(self, manifest: ModelManifest) -> Path:
        part = part_path(self._cache_dir, manifest)
        part.parent.mkdir(parents=True, exist_ok=True)
        result = await self._fetch_with_retries(self._blob_url(manifest.model_id), part)
        self._verify_or_raise(manifest, part, result)
        blob = blob_path(self._cache_dir, manifest)
        write_marker_atomic(marker_path(self._cache_dir, manifest), manifest.sha256)
        part.replace(blob)  # atomic publish of the verified file
        return blob

    @staticmethod
    def _verify_or_raise(manifest: ModelManifest, part: Path, result: DownloadResult) -> None:
        if result.sha256 == manifest.sha256 and result.size == manifest.size_bytes:
            return
        part.unlink(missing_ok=True)
        raise ModelVerificationError(
            f"verification failed for {manifest.model_id}: "
            f"sha256 {result.sha256} vs {manifest.sha256}, "
            f"size {result.size} vs {manifest.size_bytes}"
        )

    async def _fetch_with_retries(self, url: str, part: Path) -> DownloadResult:
        attempt = 0
        while True:
            try:
                return await stream_to_part(
                    client=self._client,
                    url=url,
                    headers=self._auth_headers,
                    part=part,
                    chunk_size=self._config.chunk_size,
                )
            except (httpx.TransportError, RetryableStatusError) as exc:
                attempt += 1
                if attempt > self._config.max_retries:
                    raise ModelFetchError(
                        f"failed to fetch {url} after {attempt} attempt(s)"
                    ) from exc
                await self._sleep(self._config.backoff_base_s * (2 ** (attempt - 1)))
