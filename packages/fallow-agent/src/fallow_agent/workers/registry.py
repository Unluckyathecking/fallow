"""Immutable registry mapping ``WorkerKind`` to a worker factory.

The wave-3 assembly registers a closure per kind (capturing the shared HTTP
client, endpoint resolver, and tmp dir) and then materialises instances once.
Construction may raise :class:`WorkerUnavailableError`; the registry never
swallows that — the caller decides whether to drop the kind.
"""

from collections.abc import Mapping

from fallow_agent.workers.errors import WorkerNotRegisteredError
from fallow_agent.workers.types import Worker, WorkerFactory
from fallow_protocol.capabilities import WorkerKind


class WorkerRegistry:
    """Frozen kind→factory map; ``register`` returns a new registry."""

    def __init__(self, factories: Mapping[WorkerKind, WorkerFactory] | None = None) -> None:
        self._factories: dict[WorkerKind, WorkerFactory] = dict(factories or {})

    def register(self, kind: WorkerKind, factory: WorkerFactory) -> "WorkerRegistry":
        """Return a copy with ``factory`` bound to ``kind`` (last write wins)."""
        return WorkerRegistry({**self._factories, kind: factory})

    def create(self, kind: WorkerKind) -> Worker:
        """Build the worker for ``kind``.

        Raises :class:`WorkerNotRegisteredError` if the kind is unknown, and
        propagates :class:`WorkerUnavailableError` if construction fails.
        """
        try:
            factory = self._factories[kind]
        except KeyError as exc:
            raise WorkerNotRegisteredError(kind) from exc
        return factory()

    @property
    def kinds(self) -> frozenset[WorkerKind]:
        """The set of registered kinds."""
        return frozenset(self._factories)
