"""The narrow registry surface the model-serving router depends on.

Declared as a ``Protocol`` so the router is decoupled from the concrete
``SqliteRegistry`` and can be unit-tested against an in-memory fake.
:class:`~fallow_coordinator.registry.SqliteRegistry` satisfies it structurally.
"""

from typing import Protocol

from fallow_coordinator.registry.records import ModelRecord
from fallow_protocol.models import ModelManifest


class BlobRegistry(Protocol):
    """Auth + model lookup needed to serve blobs and manifests."""

    async def authenticate_agent(self, bearer: str) -> str | None: ...

    async def get_manifest(self, model_id: str) -> ModelManifest | None: ...

    async def get_model(self, model_id: str) -> ModelRecord | None: ...
