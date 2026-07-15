"""Empirical per-agent idle-churn model (scheduler module C6, arm c v2).

The churn-aware scheduler needs one number per candidate agent: *given the user
has already been idle ``u`` seconds, how likely is the machine to stay idle for
another ``t`` seconds?* This module derives that survival estimate from recorded
idle sessions, bucketed by ``(agent_id, hour-of-day)`` because office churn is
strongly diurnal (lunch runs are long; a 09:00 idle is a coffee, not a nap).

Kaplan-Meier is overkill here: every session in ``events.jsonl`` is *completed*
(we see both its ``user_idle`` start and its ``user_returned`` end), so the raw
empirical distribution of completed idle-session lengths is exact. For a bucket's
sorted session lengths ``S``::

    survival(u, t) = |{s in S : s >= u + t}| / |{s in S : s >= u}|

i.e. of the sessions that reached age ``u`` at all, the fraction that reached
``u + t``. Sparse buckets fall back to the agent's all-hours pool, then to a
configurable optimistic prior when the agent has no history at all.

Purity: this module performs no I/O. :func:`build_churn_model` consumes already
parsed event mappings (the app layer reads ``events.jsonl``) and an injected
``hour_of`` function, so model construction is fully deterministic and replayable.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime

from fallow_protocol.messages import EventKind

# A bucket with fewer than this many completed sessions is too thin to trust; the
# survival query falls back to the agent's all-hours pool instead.
MIN_BUCKET_SAMPLES = 5

# Survival returned when an agent (or the query's conditioning age) has no data.
# "Optimistic" = assume the machine will stay idle, so an unseen agent is not
# starved of batch work purely for lack of history.
DEFAULT_OPTIMISTIC_PRIOR = 0.9

# Maps an event's timestamp to its hour-of-day bucket (injected for determinism
# and to allow local-vs-UTC bucketing to be decided by the caller).
HourOf = Callable[[datetime], int]

# One completed idle session: which agent, which hour bucket, how long it lasted.
_Session = tuple[str, int, float]


def _count_ge(sorted_samples: tuple[float, ...], threshold: float) -> int:
    """Number of samples ``>= threshold`` in an ascending-sorted tuple."""
    return len(sorted_samples) - bisect_left(sorted_samples, threshold)


@dataclass(frozen=True)
class ChurnModel:
    """Frozen empirical idle-survival estimator, bucketed by ``(agent, hour)``.

    ``by_bucket`` maps ``(agent_id, hour)`` to that bucket's session lengths;
    ``by_agent`` maps ``agent_id`` to the agent's all-hours pool. Both are sorted
    ascending so survival counts are O(log n) via binary search. Build instances
    with :func:`build_churn_model`, never by hand.
    """

    by_bucket: Mapping[tuple[str, int], tuple[float, ...]]
    by_agent: Mapping[str, tuple[float, ...]]
    optimistic_prior: float = DEFAULT_OPTIMISTIC_PRIOR

    def _samples_for(self, agent_id: str, hour: int) -> tuple[float, ...]:
        """The bucket if it is dense enough, else the agent's all-hours pool."""
        bucket = self.by_bucket.get((agent_id, hour), ())
        if len(bucket) >= MIN_BUCKET_SAMPLES:
            return bucket
        return self.by_agent.get(agent_id, ())

    def survival(self, agent_id: str, hour: int, idle_age_s: float, horizon_s: float) -> float:
        """P(session lasts ``>= idle_age_s + horizon_s`` | already idle ``idle_age_s``).

        Falls back bucket → all-hours pool → optimistic prior. A non-positive
        horizon is already satisfied (survival 1.0); a conditioning age no
        recorded session reached leaves no evidence, so the prior applies.
        """
        if horizon_s <= 0:
            return 1.0
        samples = self._samples_for(agent_id, hour)
        if not samples:
            return self.optimistic_prior
        reached_age = _count_ge(samples, idle_age_s)
        if reached_age == 0:
            return self.optimistic_prior
        reached_horizon = _count_ge(samples, idle_age_s + horizon_s)
        return reached_horizon / reached_age


def build_churn_model(
    events: Iterable[Mapping[str, object]],
    hour_of: HourOf,
    *,
    optimistic_prior: float = DEFAULT_OPTIMISTIC_PRIOR,
) -> ChurnModel:
    """Build a :class:`ChurnModel` from parsed ``events.jsonl`` mappings.

    Each ``user_idle`` starts a session for its agent and each ``user_returned``
    ends the agent's open session; the length is ``returned.at - idle.at``. A
    ``user_returned`` with no open session, a re-opened ``user_idle`` (the earlier
    open session is dropped), a negative-length pair, and any malformed line are
    all skipped. Buckets are keyed by ``hour_of(session_start)``.
    """
    by_bucket: dict[tuple[str, int], list[float]] = {}
    by_agent: dict[str, list[float]] = {}
    for agent_id, hour, length_s in _extract_sessions(events, hour_of):
        by_bucket.setdefault((agent_id, hour), []).append(length_s)
        by_agent.setdefault(agent_id, []).append(length_s)
    return ChurnModel(
        by_bucket={key: tuple(sorted(vals)) for key, vals in by_bucket.items()},
        by_agent={key: tuple(sorted(vals)) for key, vals in by_agent.items()},
        optimistic_prior=optimistic_prior,
    )


def _extract_sessions(
    events: Iterable[Mapping[str, object]], hour_of: HourOf
) -> Iterator[_Session]:
    """Pair ``user_idle`` → ``user_returned`` per agent, in file (chronological) order."""
    open_since: dict[str, datetime] = {}
    for event in events:
        parsed = _parse_event(event)
        if parsed is None:
            continue
        agent_id, kind, at = parsed
        if kind == EventKind.USER_IDLE:
            open_since[agent_id] = at
        elif kind == EventKind.USER_RETURNED:
            start = open_since.pop(agent_id, None)
            if start is None:
                continue
            length_s = (at - start).total_seconds()
            if length_s >= 0:
                yield agent_id, hour_of(start), length_s


def _parse_event(event: Mapping[str, object]) -> tuple[str, str, datetime] | None:
    """Extract ``(agent_id, kind, at)`` from one event mapping, or ``None`` if malformed."""
    agent_id = event.get("agent_id")
    kind = event.get("kind")
    at = _parse_at(event.get("at"))
    if not isinstance(agent_id, str) or not isinstance(kind, str) or at is None:
        return None
    return agent_id, kind, at


def _parse_at(at_raw: object) -> datetime | None:
    """Coerce an event ``at`` (ISO-8601 string or already a ``datetime``) to a datetime."""
    if isinstance(at_raw, datetime):
        return at_raw
    if isinstance(at_raw, str):
        try:
            return datetime.fromisoformat(at_raw)
        except ValueError:
            return None
    return None
