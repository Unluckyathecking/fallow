"""Model blob-serving module (C3).

Public API: :func:`create_modelserve_router`, the ASGI router that streams model
blobs (with resumable Range requests) and manifests to authenticated agents, and
the :class:`BlobRegistry` protocol it depends on.
"""

from fallow_coordinator.modelserve.blob import CHUNK_SIZE, OCTET_STREAM
from fallow_coordinator.modelserve.protocols import BlobRegistry
from fallow_coordinator.modelserve.router import create_modelserve_router

__all__ = [
    "CHUNK_SIZE",
    "OCTET_STREAM",
    "BlobRegistry",
    "create_modelserve_router",
]
