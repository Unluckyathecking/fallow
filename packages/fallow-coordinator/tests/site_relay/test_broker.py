import asyncio
import time

import pytest

from fallow_coordinator.site_relay import (
    RelayBroker,
    RelayRequest,
    RelayRequestTooLarge,
    RelayStateError,
)


@pytest.mark.asyncio
async def test_claim_offer_roundtrip():
    b = RelayBroker()
    ctask = asyncio.create_task(b.claim("a", 4, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, RelayRequest(body=b"{}"), time.monotonic() + 1)
    c = await ctask
    assert c and c.claim_id == ex.claim_id and c.presence_generation == 4
    await ex.start_response("a", c.claim_id, 4)
    await ex.write("a", c.claim_id, 4, b"ok")
    await ex.finish("a", c.claim_id, 4)
    assert await ex.__anext__() == b"ok"
    with pytest.raises(StopAsyncIteration):
        await ex.__anext__()


@pytest.mark.asyncio
async def test_offer_requires_waiter_and_limits_body():
    b = RelayBroker()
    with pytest.raises(RelayStateError):
        await b.offer("a", 1, b"", time.monotonic() + 1)
    c = asyncio.create_task(b.claim("a", 0, 1))
    await asyncio.sleep(0)
    with pytest.raises(RelayRequestTooLarge):
        await b.offer("a", 1, b"x" * (2 * 1024 * 1024 + 1), time.monotonic() + 1)
    c.cancel()


@pytest.mark.asyncio
async def test_wrong_owner_stale_and_duplicate_completion():
    b = RelayBroker()
    waiter = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + 1)
    claim = await waiter
    assert claim is not None
    with pytest.raises(RelayStateError):
        await ex.start_response("b", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.start_response("a", claim.claim_id, 2)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.finish("a", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.finish("a", claim.claim_id, 1)


@pytest.mark.asyncio
async def test_invalidation_closes_claim_and_disconnect():
    b = RelayBroker()
    waiter = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + 1)
    claim = await waiter
    assert claim is not None
    await b.invalidate_agent("a", 2, "reclaimed")
    with pytest.raises(RelayStateError):
        await ex.start_response("a", claim.claim_id, 1)


@pytest.mark.asyncio
async def test_chunk_bound_and_failure_codes():
    b = RelayBroker()
    waiter = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + 1)
    claim = await waiter
    assert claim is not None
    await ex.start_response("a", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.write("a", claim.claim_id, 1, b"x" * (32 * 1024 + 1))
    with pytest.raises(RelayStateError):
        await ex.fail("a", claim.claim_id, 1, "bad")
    await ex.fail("a", claim.claim_id, 1, "cancelled")


@pytest.mark.asyncio
async def test_claim_timeout_and_cancellation_leave_no_waiter():
    b = RelayBroker()
    assert await b.claim("a", 1, 0.001) is None
    task = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    task.cancel()
    assert await task is None
    assert not b._waiters["a"]


@pytest.mark.asyncio
async def test_status_and_cleanup():
    b = RelayBroker()
    waiter = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + 1)
    claim = await waiter
    assert claim is not None
    await ex.start_response("a", claim.claim_id, 1, 429)
    assert ex.status == 429
    await ex.finish("a", claim.claim_id, 1)
    assert claim.claim_id not in b._works


@pytest.mark.asyncio
async def test_invalidation_terminates_full_buffer():
    b = RelayBroker()
    waiter = asyncio.create_task(b.claim("a", 1, 1))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + 1)
    claim = await waiter
    assert claim is not None
    await ex.start_response("a", claim.claim_id, 1)
    for _ in range(8):
        await ex.write("a", claim.claim_id, 1, b"x")
    await b.invalidate_agent("a", 2, "reclaimed")
    assert await ex.__anext__() == b"x"
    while True:
        try:
            await ex.__anext__()
        except StopAsyncIteration:
            break
