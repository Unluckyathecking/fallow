"""Durable job / work-unit queue (coordinator module C1).

Public API: :class:`SqliteQueueStore`, the SQLite implementation of
:class:`fallow_protocol.interfaces.QueueStore`.
"""

from fallow_coordinator.queue.store import (
    JobDetails,
    JobUnitOutcome,
    QueueNotInitializedError,
    SqliteQueueStore,
)

__all__ = ["JobDetails", "JobUnitOutcome", "QueueNotInitializedError", "SqliteQueueStore"]
