"""Model blob-serving module (C3).

Public API: :func:`create_modelserve_router`, the ASGI router that streams model
blobs (with resumable Range requests) and manifests to authenticated agents, and
the :class:`BlobRegistry` protocol it depends on.

The optional modelmesh path (ADR 074) adds :func:`create_mesh_router` and
:class:`MeshManifestBuilder`, mounted only when a shared signing key is configured.
"""

from fallow_coordinator.modelserve.blob import CHUNK_SIZE, OCTET_STREAM
from fallow_coordinator.modelserve.mesh import MeshManifestBuilder, create_mesh_router
from fallow_coordinator.modelserve.protocols import BlobRegistry
from fallow_coordinator.modelserve.router import create_modelserve_router

__all__ = [
    "CHUNK_SIZE",
    "OCTET_STREAM",
    "BlobRegistry",
    "MeshManifestBuilder",
    "create_mesh_router",
    "create_modelserve_router",
]
