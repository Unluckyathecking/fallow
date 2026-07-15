"""Small shared holders that thread data between the runtime's async loops.

Both loops run on the same asyncio event loop as the heartbeat, so no locking is
needed: these are plain last-writer-wins cells. ``DesiredModels`` is written by
the heartbeat ``on_response`` and read by the reconcile loop; ``LeaseRegistry``
is written by the work loop and read by the heartbeat when it reports held
leases.
"""

from __future__ import annotations


class DesiredModels:
    """The set of models the coordinator wants this agent to serve."""

    def __init__(self, initial: tuple[str, ...] = ()) -> None:
        self._models = initial

    def update(self, models: tuple[str, ...]) -> None:
        self._models = models

    def current(self) -> tuple[str, ...]:
        return self._models


class LeaseRegistry:
    """The work-unit lease currently in flight (0 or 1 in v0.1)."""

    def __init__(self) -> None:
        self._lease_ids: tuple[str, ...] = ()

    def set(self, lease_id: str) -> None:
        self._lease_ids = (lease_id,)

    def clear(self) -> None:
        self._lease_ids = ()

    def current(self) -> tuple[str, ...]:
        return self._lease_ids
