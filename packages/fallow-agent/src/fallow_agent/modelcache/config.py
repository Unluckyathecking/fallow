"""Configuration and constants for the agent model cache.

No hardcoded magic numbers live in the logic modules: everything tunable is a
named constant here, and per-download behaviour is captured in the frozen
``ModelCacheConfig``.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Production default cache root; expanded (``~``) at store construction time.
DEFAULT_CACHE_DIR = Path("~/.fallow/models")

# Coordinator blob endpoint. model_id is path-substituted at request time.
BLOB_PATH_TEMPLATE = "/v1/models/{model_id}/blob"

# On-disk layout suffixes.
PART_SUFFIX = ".part"
MARKER_SUFFIX = ".sha256"
_TMP_SUFFIX = ".tmp"

# HTTP status codes we act on explicitly.
HTTP_OK = 200
HTTP_PARTIAL_CONTENT = 206

# Streaming chunk size: 1 MiB. Bytes are hashed and flushed one chunk at a time
# so a multi-GB blob never sits in memory.
_ONE_MIB = 1024 * 1024

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_S = 0.5


class ModelCacheConfig(BaseModel):
    """Immutable knobs for a single :class:`HttpModelStore`.

    Frozen so it can be shared freely without any caller mutating another's
    retry/backoff behaviour.
    """

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(default=_DEFAULT_MAX_RETRIES, ge=0)
    backoff_base_s: float = Field(default=_DEFAULT_BACKOFF_BASE_S, gt=0)
    chunk_size: int = Field(default=_ONE_MIB, gt=0)
