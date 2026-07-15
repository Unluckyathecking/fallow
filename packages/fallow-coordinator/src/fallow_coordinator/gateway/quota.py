"""Per-key request quotas with an injected UTC clock."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from fallow_coordinator.registry import ApiKeyInfo, ApiKeyQuotaSnapshot


class QuotaSnapshotStore(Protocol):
    async def load_quota_snapshots(self) -> tuple[ApiKeyQuotaSnapshot, ...]: ...

    async def save_quota_snapshots(self, snapshots: Sequence[ApiKeyQuotaSnapshot]) -> None: ...


@dataclass(frozen=True)
class QuotaExceeded:
    """A rejected request and the earliest useful retry delay."""

    retry_after_s: int


@dataclass
class _KeyState:
    bucket_tokens: float
    bucket_updated_at: datetime
    day: date
    daily_count: int


class QuotaManager:
    """Consume request quotas and periodically persist the in-memory counters."""

    def __init__(self, store: QuotaSnapshotStore, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now
        self._states: dict[str, _KeyState] = {}

    async def restore(self) -> None:
        """Load the last registry snapshot before the gateway starts serving."""
        snapshots = await self._store.load_quota_snapshots()
        self._states = {
            item.key_id: _KeyState(
                bucket_tokens=item.bucket_tokens,
                bucket_updated_at=item.bucket_updated_at,
                day=date.fromisoformat(item.day),
                daily_count=item.daily_count,
            )
            for item in snapshots
        }

    def consume(self, key: ApiKeyInfo) -> QuotaExceeded | None:
        """Consume one request, or return the delay until both limits permit it."""
        if key.is_admin or (key.rpm_limit is None and key.daily_limit is None):
            return None
        now = self._utc_now()
        key_id = key.key_id or key.name
        state = self._states.get(key_id)
        if state is None:
            state = _KeyState(
                bucket_tokens=float(key.rpm_limit or 0),
                bucket_updated_at=now,
                day=now.date(),
                daily_count=0,
            )
            self._states[key_id] = state
        self._refresh(state, key, now)

        retry_delays: list[int] = []
        if key.rpm_limit is not None and state.bucket_tokens < 1.0:
            refill_per_s = key.rpm_limit / 60.0
            retry_delays.append(max(1, math.ceil((1.0 - state.bucket_tokens) / refill_per_s)))
        if key.daily_limit is not None and state.daily_count >= key.daily_limit:
            next_day = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
            retry_delays.append(max(1, math.ceil((next_day - now).total_seconds())))
        if retry_delays:
            return QuotaExceeded(retry_after_s=max(retry_delays))

        if key.rpm_limit is not None:
            state.bucket_tokens -= 1.0
        if key.daily_limit is not None:
            state.daily_count += 1
        return None

    async def snapshot(self) -> None:
        """Write one consistent copy of every active key counter to the registry."""
        snapshotted_at = self._utc_now()
        snapshots = tuple(
            ApiKeyQuotaSnapshot(
                key_id=key_id,
                bucket_tokens=state.bucket_tokens,
                bucket_updated_at=state.bucket_updated_at,
                day=state.day.isoformat(),
                daily_count=state.daily_count,
                snapshotted_at=snapshotted_at,
            )
            for key_id, state in sorted(self._states.items())
        )
        await self._store.save_quota_snapshots(snapshots)

    def _refresh(self, state: _KeyState, key: ApiKeyInfo, now: datetime) -> None:
        if key.rpm_limit is not None:
            elapsed_s = max(0.0, (now - state.bucket_updated_at).total_seconds())
            state.bucket_tokens = min(
                float(key.rpm_limit), state.bucket_tokens + elapsed_s * key.rpm_limit / 60.0
            )
        state.bucket_updated_at = max(state.bucket_updated_at, now)
        if now.date() > state.day:
            state.day = now.date()
            state.daily_count = 0

    def _utc_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("quota clock must return an aware datetime")
        return now.astimezone(UTC)
