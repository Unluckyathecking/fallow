"""Shared coordinator runtime state (module I1).

:class:`CoordinatorState` is the single container the route factories and the
lifespan close over. It is built synchronously in ``create_app`` (so routers can
be mounted before serving), then the stores are opened and the background tasks
started inside the lifespan. Everything the request handlers touch — the two
SQLite stores, the placement policy, the injected clock/sleeper, the shared
upstream HTTP client, events writer, quota manager, and event-driven state overlay hangs
off this object.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from fallow_coordinator.app.config import CoordinatorConfig
from fallow_coordinator.app.events import EventStateOverrides, EventsWriter
from fallow_coordinator.app.rag_ingestion import IngestionService
from fallow_coordinator.app.result_blobs import ResultBlobStore
from fallow_coordinator.gateway import QuotaManager
from fallow_coordinator.gateway.protocols import SiteRouteResolver
from fallow_coordinator.queue import SqliteQueueStore
from fallow_coordinator.rag.store import RagVectorStore
from fallow_coordinator.registry import SqliteRegistry
from fallow_coordinator.scheduler import ChurnModel, DispatchLoop
from fallow_coordinator.site_relay import RelayBroker
from fallow_protocol.interfaces import SchedulerPolicy

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class RelayFailureFlags:
    """Bounded record of claims an agent failed with ``retryable: false``.

    Set by the relay failure route and consumed once by the gateway transport, so
    a deterministic non-retryable failure surfaces an error instead of a repick.
    Single event loop, so the mark always lands before the gateway coroutine wakes
    on the same broker terminal; a bounded map drops the rare unconsumed entry.
    """

    def __init__(self, maxlen: int = 4096) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def mark(self, claim_id: str) -> None:
        self._ids[claim_id] = None
        self._ids.move_to_end(claim_id)
        while len(self._ids) > self._maxlen:
            self._ids.popitem(last=False)

    def take(self, claim_id: str) -> bool:
        return self._ids.pop(claim_id, _MISSING) is not _MISSING


_MISSING = object()


@dataclass
class CoordinatorState:
    """Live collaborators for one coordinator app instance."""

    config: CoordinatorConfig
    registry: SqliteRegistry
    queue: SqliteQueueStore
    policy: SchedulerPolicy
    now: Clock
    monotonic: Monotonic
    sleep: Sleeper
    client: httpx.AsyncClient
    events: EventsWriter
    results: ResultBlobStore
    overrides: EventStateOverrides
    quotas: QuotaManager
    rag: RagVectorStore
    ingestion: IngestionService | None = None
    # Idle-survival model for the speculative backup decision (ADR 056). Built at
    # startup only when the feature is enabled; ``None`` leaves it fully off.
    churn: ChurnModel | None = None
    # Site Mode relay + transport resolver, present only when site mode is enabled;
    # ``None`` keeps the coordinator on the direct HTTP path with no relay routes.
    relay: RelayBroker | None = None
    site_route: SiteRouteResolver | None = None
    relay_flags: RelayFailureFlags | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    dispatch: DispatchLoop | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    agent_liveness_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
