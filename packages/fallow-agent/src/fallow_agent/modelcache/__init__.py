"""Agent model cache: pull GGUF blobs with resume, verify, keep on disk.

Public API:

- :class:`HttpModelStore` — the :class:`fallow_protocol.interfaces.ModelStore`
  implementation.
- :class:`ModelCacheConfig` — retry/backoff/chunk tuning.
- :class:`ModelFetchError` / :class:`ModelVerificationError` — typed failures.

Everything else in this package is private implementation detail.
"""

from fallow_agent.modelcache.config import ModelCacheConfig
from fallow_agent.modelcache.errors import (
    ModelCacheError,
    ModelFetchError,
    ModelVerificationError,
)
from fallow_agent.modelcache.store import HttpModelStore

__all__ = [
    "HttpModelStore",
    "ModelCacheConfig",
    "ModelCacheError",
    "ModelFetchError",
    "ModelVerificationError",
]
