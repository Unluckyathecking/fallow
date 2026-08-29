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

from fallow_agent.modelcache.config import (
    BLOB_PATH_TEMPLATE,
    DEFAULT_CACHE_DIR,
    MARKER_SUFFIX,
    MMPROJ_PATH_TEMPLATE,
    PART_SUFFIX,
    ModelCacheConfig,
)
from fallow_agent.modelcache.download import (
    DownloadResult,
    RetryableStatusError,
    stream_to_part,
)
from fallow_agent.modelcache.errors import ModelFetchError, ModelVerificationError
from fallow_agent.modelcache.paths import (
    blob_path,
    marker_path,
    read_marker,
    write_marker_atomic,
)
from fallow_protocol.interfaces import ModelStore
from fallow_protocol.models import ModelManifest

SleepFn = Callable[[float], Awaitable[None]]


def _sibling_marker(target: Path) -> Path:
    return target.with_name(f"{target.name}{MARKER_SUFFIX}")


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
        """Return the blob path iff it (and any mmproj companion) exists and its
        marker matches the manifest sha256. Trusts the marker: does NOT rehash
        the file."""
        blob = blob_path(self._cache_dir, manifest)
        if not blob.exists():
            return None
        if read_marker(marker_path(self._cache_dir, manifest)) != manifest.sha256:
            return None
        if manifest.mmproj_file_name is not None:
            mmproj = blob.with_name(manifest.mmproj_file_name)
            if not mmproj.exists():
                return None
            if read_marker(_sibling_marker(mmproj)) != manifest.mmproj_sha256:
                return None
        return blob

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

    def _mmproj_url(self, model_id: str) -> str:
        return f"{self._base_url}{MMPROJ_PATH_TEMPLATE.format(model_id=model_id)}"

    async def _download_and_verify(self, manifest: ModelManifest) -> Path:
        blob = await self._ensure_file(
            url=self._blob_url(manifest.model_id),
            target=blob_path(self._cache_dir, manifest),
            sha256=manifest.sha256,
            size_bytes=manifest.size_bytes,
        )
        if (
            manifest.mmproj_file_name is not None
            and manifest.mmproj_sha256 is not None
            and manifest.mmproj_size_bytes is not None
        ):
            await self._ensure_file(
                url=self._mmproj_url(manifest.model_id),
                target=blob.with_name(manifest.mmproj_file_name),
                sha256=manifest.mmproj_sha256,
                size_bytes=manifest.mmproj_size_bytes,
            )
        return blob

    async def _ensure_file(self, *, url: str, target: Path, sha256: str, size_bytes: int) -> Path:
        """Download-with-resume one file, verify it, and publish it atomically.

        A file whose marker already matches is returned as-is, so a manifest
        whose main blob verified but whose mmproj failed only refetches the
        mmproj on retry.
        """
        marker = _sibling_marker(target)
        if target.exists() and read_marker(marker) == sha256:
            return target
        part = target.with_name(f"{target.name}{PART_SUFFIX}")
        part.parent.mkdir(parents=True, exist_ok=True)
        result = await self._fetch_with_retries(url, part)
        self._verify_or_raise(target.name, sha256, size_bytes, part, result)
        write_marker_atomic(marker, sha256)
        part.replace(target)  # atomic publish of the verified file
        return target

    @staticmethod
    def _verify_or_raise(
        name: str, sha256: str, size_bytes: int, part: Path, result: DownloadResult
    ) -> None:
        if result.sha256 == sha256 and result.size == size_bytes:
            return
        part.unlink(missing_ok=True)
        raise ModelVerificationError(
            f"verification failed for {name}: "
            f"sha256 {result.sha256} vs {sha256}, "
            f"size {result.size} vs {size_bytes}"
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
