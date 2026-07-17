"""Agent mesh path: fetch a model as verified chunks, fall back to the blob.

Off by default (ADR 074). Public API:

- :class:`MeshModelStore` — the :class:`fallow_protocol.interfaces.ModelStore`
  wrapper that tries the mesh and falls back to the blob download.
- :class:`MeshSettings` — the opt-in config block.
- :class:`HttpMeshTransport` — the coordinator-backed chunk transport.
- :class:`MeshError` — a mesh failure that triggers the fallback.

The verification logic composes the ``fallow-modelmesh`` public API; this package
never modifies it.
"""

from fallow_agent.mesh.config import MeshSettings
from fallow_agent.mesh.errors import MeshError
from fallow_agent.mesh.store import MeshModelStore
from fallow_agent.mesh.transport import HttpMeshTransport

__all__ = [
    "HttpMeshTransport",
    "MeshError",
    "MeshModelStore",
    "MeshSettings",
]
