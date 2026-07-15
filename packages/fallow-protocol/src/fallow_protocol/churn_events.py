"""Shared churn-event parsing: pair idle/returned events into idle sessions.

One policy for both the bench validator and the scheduler's model builder, so a
churn history validated by one is parsed identically by the other.

Timestamp policy: accept a trailing "Z" (UTC) and REJECT naive datetimes — a
timestamp with no timezone is ambiguous, so such an event is skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from typing import NamedTuple

from fallow_protocol.messages import EventKind


class ChurnSession(NamedTuple):
    """One completed idle session with tz-aware start and end (``end >= start``)."""

    agent_id: str
    start: datetime
    end: datetime

    @property
    def length_s(self) -> float:
        return (self.end - self.start).total_seconds()


def parse_event_at(at_raw: object) -> datetime | None:
    """Parse an event ``at`` to a tz-aware datetime, or ``None`` if invalid or naive.

    Accepts an ISO-8601 string (trailing "Z" allowed) or an existing datetime;
    a naive datetime is rejected because its instant is ambiguous.
    """
    if isinstance(at_raw, datetime):
        at = at_raw
    elif isinstance(at_raw, str):
        try:
            at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if at.tzinfo is None or at.utcoffset() is None:
        return None
    return at


def iter_churn_sessions(events: Iterable[Mapping[str, object]]) -> Iterator[ChurnSession]:
    """Pair ``user_idle`` → ``user_returned`` per agent into completed sessions.

    In file (chronological) order: each ``user_idle`` opens (or re-opens, dropping
    the prior open) an agent's session; each ``user_returned`` closes it. Unpaired
    ``user_returned`` events, negative-length pairs, and malformed events (missing
    or non-string ``agent_id``/``kind``, or an unparsable/naive ``at``) are skipped.
    """
    open_since: dict[str, datetime] = {}
    for event in events:
        agent_id = event.get("agent_id")
        kind = event.get("kind")
        at = parse_event_at(event.get("at"))
        if not isinstance(agent_id, str) or not isinstance(kind, str) or at is None:
            continue
        if kind == EventKind.USER_IDLE.value:
            open_since[agent_id] = at
        elif kind == EventKind.USER_RETURNED.value:
            start = open_since.pop(agent_id, None)
            if start is not None and at >= start:
                yield ChurnSession(agent_id, start, at)
