"""Token-bucket, UTC-day, gateway-envelope, and restart quota behavior."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from gateway_helpers import CHAT_MODEL, GatewayHarness, buffered_handler, make_endpoint

from fallow_coordinator.gateway import QuotaManager
from fallow_coordinator.registry import ApiKeyInfo, ApiKeyQuotaSnapshot

LIMITED_KEY = "limited-key"


class Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class MemoryStore:
    def __init__(self) -> None:
        self.snapshots: tuple[ApiKeyQuotaSnapshot, ...] = ()

    async def load_quota_snapshots(self) -> tuple[ApiKeyQuotaSnapshot, ...]:
        return self.snapshots

    async def save_quota_snapshots(self, snapshots: Sequence[ApiKeyQuotaSnapshot]) -> None:
        self.snapshots = tuple(snapshots)


def _key(*, rpm: int | None = None, daily: int | None = None) -> ApiKeyInfo:
    return ApiKeyInfo(name="limited", key_id="key-hash", rpm_limit=rpm, daily_limit=daily)


def test_token_bucket_refills_at_exact_boundary() -> None:
    clock = Clock(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    manager = QuotaManager(MemoryStore(), clock)
    key = _key(rpm=2)

    assert manager.consume(key) is None
    assert manager.consume(key) is None
    exceeded = manager.consume(key)
    assert exceeded is not None and exceeded.retry_after_s == 30

    clock.advance(30)
    assert manager.consume(key) is None


def test_daily_counter_resets_at_utc_midnight() -> None:
    clock = Clock(datetime(2026, 7, 15, 23, 59, 59, tzinfo=UTC))
    manager = QuotaManager(MemoryStore(), clock)
    key = _key(daily=1)

    assert manager.consume(key) is None
    exceeded = manager.consume(key)
    assert exceeded is not None and exceeded.retry_after_s == 1

    clock.advance(1)
    assert manager.consume(key) is None


async def test_snapshot_restores_counters_after_restart() -> None:
    clock = Clock(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    store = MemoryStore()
    key = _key(rpm=2, daily=2)
    first = QuotaManager(store, clock)
    assert first.consume(key) is None
    await first.snapshot()

    restarted = QuotaManager(store, clock)
    await restarted.restore()
    assert restarted.consume(key) is None
    assert restarted.consume(key) is not None


async def test_gateway_returns_openai_429_with_retry_after(build_gateway) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    manager = QuotaManager(MemoryStore(), clock)
    key = _key(rpm=1)
    harness: GatewayHarness = await build_gateway(
        upstream_handler=buffered_handler(b'{"ok":true}'),
        endpoints={CHAT_MODEL: (make_endpoint("h1", 8001),)},
        api_keys={LIMITED_KEY: key},
        quotas=manager,
    )
    headers = {"Authorization": f"Bearer {LIMITED_KEY}"}
    assert (
        await harness.client.post(
            "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=headers
        )
    ).status_code == 200

    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=headers
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json() == {
        "error": {"message": "api key request quota exceeded", "type": "rate_limit_error"}
    }
