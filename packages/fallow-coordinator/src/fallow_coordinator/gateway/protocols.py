"""The narrow seams the gateway depends on.

Declared as ``Protocol`` / type-alias so the router is decoupled from the
concrete :class:`~fallow_coordinator.registry.SqliteRegistry` and the scheduler
policy, and can be unit-tested against in-memory fakes with no network.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from fallow_coordinator.gateway.logentry import GatewayLogEntry
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.messages import ReplicaEndpoint
from fallow_protocol.models import ModelManifest

# The scheduler policy's replica chooser, injected by the app layer so the
# gateway never imports the scheduler (they are DAG siblings). ``None`` means
# "no replica should serve this request right now".
PickReplica = Callable[[str, Sequence[ReplicaEndpoint]], ReplicaEndpoint | None]


class GatewayRegistry(Protocol):
    """Auth + routing surface the gateway needs from the registry (module C2)."""

    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None: ...

    async def replica_endpoints(
        self, model_id: str, now: datetime
    ) -> tuple[ReplicaEndpoint, ...]: ...

    async def list_models(self) -> tuple[ModelManifest, ...]: ...


class RequestLog(Protocol):
    """Sink for one audit record per interactive request; must not block."""

    def log(self, entry: GatewayLogEntry) -> None: ...
