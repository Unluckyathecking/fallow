"""Shared coordinator runtime state (module I1).

:class:`CoordinatorState` is the single container the route factories and the
lifespan close over. It is built synchronously in ``create_app`` (so routers can
be mounted before serving), then the stores are opened and the background tasks
started inside the lifespan. Everything the request handlers touch — the two
SQLite stores, the placement policy, the injected clock/sleeper, the shared
upstream HTTP client, the events writer, and the event-driven state overlay —
hangs off this object.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from fallow_coordinator.app.config import CoordinatorConfig
from fallow_coordinator.app.events import EventStateOverrides, EventsWriter
from fallow_coordinator.app.rag_ingestion import IngestionService
from fallow_coordinator.app.result_blobs import ResultBlobStore
from fallow_coordinator.queue import SqliteQueueStore
from fallow_coordinator.registry import SqliteRegistry
from fallow_coordinator.scheduler import DispatchLoop
from fallow_protocol.interfaces import SchedulerPolicy

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass
class CoordinatorState:
    """Live collaborators for one coordinator app instance."""

    config: CoordinatorConfig
    registry: SqliteRegistry
    queue: SqliteQueueStore
    policy: SchedulerPolicy
    now: Clock
    sleep: Sleeper
    client: httpx.AsyncClient
    events: EventsWriter
    results: ResultBlobStore
    overrides: EventStateOverrides
    ingestion: IngestionService | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    dispatch: DispatchLoop | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    agent_liveness_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
