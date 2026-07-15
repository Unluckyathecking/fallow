"""Bounded, expiring session affinity for interactive gateway requests."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from fallow_coordinator.gateway.logentry import AffinityState
from fallow_protocol.messages import ReplicaEndpoint

EndpointKey = tuple[str, str, int, str]


@dataclass(frozen=True)
class AffinityDecision:
    """The selected endpoint and the cache state observed by the request."""

    endpoint: ReplicaEndpoint | None
    state: AffinityState


@dataclass(frozen=True)
class _Entry:
    endpoint: EndpointKey
    touched_at: datetime


class AffinityMap:
    """An injected-clock, sliding-TTL LRU map from sessions to endpoints."""

    def __init__(self, ttl_s: float, max_entries: int, now: Callable[[], datetime]) -> None:
        if ttl_s <= 0:
            raise ValueError("affinity ttl must be positive")
        if max_entries <= 0:
            raise ValueError("affinity maximum must be positive")
        self._ttl = timedelta(seconds=ttl_s)
        self._max = max_entries
        self._now = now
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def resolve(
        self,
        session_key: str | None,
        candidates: Sequence[ReplicaEndpoint],
        pick: Callable[[Sequence[ReplicaEndpoint]], ReplicaEndpoint | None],
    ) -> AffinityDecision:
        """Reuse a healthy mapping or ask ``pick`` for a replacement."""
        if session_key is None:
            return AffinityDecision(pick(candidates), AffinityState.NONE)

        now = self._now()
        self._expire(now)
        entry = self._entries.get(session_key)
        if entry is not None:
            endpoint = next(
                (item for item in candidates if _endpoint_key(item) == entry.endpoint), None
            )
            if endpoint is not None:
                self._entries[session_key] = _Entry(entry.endpoint, now)
                self._entries.move_to_end(session_key)
                return AffinityDecision(endpoint, AffinityState.HIT)
            del self._entries[session_key]

        endpoint = pick(candidates)
        if endpoint is not None:
            self.remember(session_key, endpoint, now=now)
        return AffinityDecision(endpoint, AffinityState.MISS)

    def remember(
        self, session_key: str, endpoint: ReplicaEndpoint, *, now: datetime | None = None
    ) -> None:
        """Bind a session to the endpoint that actually served its request."""
        moment = self._now() if now is None else now
        self._expire(moment)
        self._entries[session_key] = _Entry(_endpoint_key(endpoint), moment)
        self._entries.move_to_end(session_key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def forget(self, session_key: str) -> None:
        """Remove a mapping after all attempts to reach an upstream fail."""
        self._entries.pop(session_key, None)

    def _expire(self, now: datetime) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if now - entry.touched_at < self._ttl:
                return
            del self._entries[key]


def _endpoint_key(endpoint: ReplicaEndpoint) -> EndpointKey:
    return endpoint.agent_id, endpoint.host, endpoint.port, endpoint.model_id
