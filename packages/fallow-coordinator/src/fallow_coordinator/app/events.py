"""Agent-event handling for the coordinator app (module I1).

Two small collaborators back the ``POST /v1/agents/{id}/events`` route:

* :class:`EventsWriter` appends each :class:`AgentEvent` as one JSON line to the
  events JSONL file under an ``asyncio.Lock`` so a single writer never interleaves
  partial lines (the coordinator runs one event loop; the lock serialises the
  append across concurrent requests).
* :class:`EventStateOverrides` records the *routing-visible* state implied by a
  ``user_returned`` / ``user_idle`` event immediately, so batch routing reacts on
  the event rather than waiting for the next ~5s heartbeat. The registry exposes
  no direct state setter (see ADR 014 open questions), so this override is applied
  at the app layer when a work long-poll builds its agent snapshot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fallow_protocol.messages import AgentEvent, AgentState, EventKind


class EventsWriter:
    """Append-only, single-writer JSONL sink for agent events."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def write(self, event: AgentEvent) -> None:
        line = event.model_dump_json()
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class EventStateOverrides:
    """App-layer, event-driven agent-state overlay for immediate routing reaction."""

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}

    def apply(self, event: AgentEvent) -> None:
        """Record the routing state implied by a user-presence transition event."""
        if event.kind == EventKind.USER_RETURNED:
            self._states[event.agent_id] = AgentState.ACTIVE
        elif event.kind == EventKind.USER_IDLE:
            self._states[event.agent_id] = AgentState.IDLE

    def state_for(self, agent_id: str) -> AgentState | None:
        """The overridden state for ``agent_id`` if an event set one, else ``None``."""
        return self._states.get(agent_id)
