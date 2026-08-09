import asyncio
import time

import pytest

from fallow_coordinator.site_relay import (
    RelayBroker,
    RelayRequest,
    RelayRequestTooLarge,
    RelayStateError,
)

DEADLINE = 1.0


async def _pair(b, agent="a", gen=1, port=8100, body=b"{}"):
    waiter = asyncio.create_task(b.claim(agent, gen, DEADLINE))
    await asyncio.sleep(0)
    ex = await b.offer(agent, port, body, time.monotonic() + DEADLINE)
    claim = await waiter
    assert claim is not None
    return ex, claim


@pytest.mark.asyncio
async def test_claim_offer_roundtrip():
    b = RelayBroker()
    ex, c = await _pair(b, gen=4)
    assert c.claim_id == ex.claim_id and c.presence_generation == 4
    await ex.start_response("a", c.claim_id, 4)
    await ex.write("a", c.claim_id, 4, b"ok")
    await ex.finish("a", c.claim_id, 4)
    assert ex.first_byte is True
    assert await ex.__anext__() == b"ok"
    with pytest.raises(StopAsyncIteration):
        await ex.__anext__()


@pytest.mark.asyncio
async def test_offer_requires_waiter_and_limits_body():
    b = RelayBroker()
    with pytest.raises(RelayStateError):
        await b.offer("a", 1, b"", time.monotonic() + DEADLINE)
    c = asyncio.create_task(b.claim("a", 0, DEADLINE))
    await asyncio.sleep(0)
    with pytest.raises(RelayRequestTooLarge):
        await b.offer("a", 1, b"x" * (2 * 1024 * 1024 + 1), time.monotonic() + DEADLINE)
    c.cancel()
    assert await c is None


@pytest.mark.asyncio
async def test_offer_rejects_non_post_and_bad_path():
    b = RelayBroker()
    c = asyncio.create_task(b.claim("a", 1, DEADLINE))
    await asyncio.sleep(0)
    with pytest.raises(RelayStateError):
        await b.offer("a", 1, RelayRequest(method="GET"), time.monotonic() + DEADLINE)
    c.cancel()
    assert await c is None


@pytest.mark.asyncio
async def test_wrong_owner_stale_and_duplicate_completion():
    b = RelayBroker()
    ex, claim = await _pair(b)
    with pytest.raises(RelayStateError):
        await ex.start_response("b", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.start_response("a", claim.claim_id, 2)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.finish("a", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.finish("a", claim.claim_id, 1)


@pytest.mark.asyncio
async def test_write_after_finish_rejected():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.finish("a", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.write("a", claim.claim_id, 1, b"late")


@pytest.mark.asyncio
async def test_reject_second_response_start():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    with pytest.raises(RelayStateError):
        await ex.start_response("a", claim.claim_id, 1)


@pytest.mark.asyncio
async def test_write_requires_started_response():
    b = RelayBroker()
    ex, claim = await _pair(b)
    with pytest.raises(RelayStateError):
        await ex.write("a", claim.claim_id, 1, b"x")


@pytest.mark.asyncio
async def test_chunk_bound_and_failure_codes():
    b = RelayBroker()
    ex, claim = await _pair(b)
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
    task = asyncio.create_task(b.claim("a", 1, DEADLINE))
    await asyncio.sleep(0)
    task.cancel()
    assert await task is None
    assert not b._waiters["a"]


@pytest.mark.asyncio
async def test_status_propagation_and_registry_cleanup():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1, 429)
    assert ex.status == 429
    await ex.finish("a", claim.claim_id, 1)
    assert claim.claim_id not in b._works


@pytest.mark.asyncio
async def test_registry_released_on_fail_and_invalidate():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.fail("a", claim.claim_id, 1, "upstream_error")
    assert claim.claim_id not in b._works
    _ex2, claim2 = await _pair(b)
    await b.invalidate_agent("a", 2, "reclaimed")
    assert claim2.claim_id not in b._works


@pytest.mark.asyncio
async def test_wait_response_returns_status():
    b = RelayBroker()
    ex, claim = await _pair(b)
    waiter = asyncio.create_task(ex.wait_response())
    await asyncio.sleep(0)
    assert not waiter.done()
    await ex.start_response("a", claim.claim_id, 1, 503)
    assert await waiter == 503


@pytest.mark.asyncio
async def test_wait_response_raises_on_pre_byte_failure():
    b = RelayBroker()
    ex, claim = await _pair(b)
    waiter = asyncio.create_task(ex.wait_response())
    await asyncio.sleep(0)
    await ex.fail("a", claim.claim_id, 1, "connect_failed")
    with pytest.raises(RelayStateError, match="connect_failed"):
        await waiter


@pytest.mark.asyncio
async def test_pre_byte_failure_is_not_clean_eof():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.fail("a", claim.claim_id, 1, "connect_failed")
    with pytest.raises(RelayStateError, match="connect_failed"):
        await ex.__anext__()
    assert ex.first_byte is False


@pytest.mark.asyncio
async def test_pre_byte_invalidation_is_not_clean_eof():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await b.invalidate_agent("a", 2, "reclaimed")
    with pytest.raises(RelayStateError, match="reclaimed"):
        await ex.__anext__()
    with pytest.raises(RelayStateError):
        await ex.start_response("a", claim.claim_id, 1)


@pytest.mark.asyncio
async def test_disconnect_surfaces_as_error():
    b = RelayBroker()
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.write("a", claim.claim_id, 1, b"partial")
    await ex.aclose()
    assert await ex.__anext__() == b"partial"
    with pytest.raises(RelayStateError, match="client_disconnect"):
        await ex.__anext__()


@pytest.mark.asyncio
async def test_reject_claims_below_generation_fence():
    b = RelayBroker()
    await b.invalidate_agent("a", 5, "reclaimed")
    with pytest.raises(RelayStateError, match="stale generation"):
        await b.claim("a", 3, DEADLINE)


@pytest.mark.asyncio
async def test_offer_skips_cancelled_waiter_future():
    b = RelayBroker()
    loop = asyncio.get_running_loop()
    dead: asyncio.Future = loop.create_future()
    dead.cancel()
    b._waiters["a"] = [(dead, 1)]
    b._generation["a"] = 0
    live = asyncio.create_task(b.claim("a", 1, DEADLINE))
    await asyncio.sleep(0)
    ex = await b.offer("a", 8100, b"{}", time.monotonic() + DEADLINE)
    claim = await live
    assert claim is not None and claim.claim_id == ex.claim_id


@pytest.mark.asyncio
async def test_current_generation_work_survives_invalidation():
    b = RelayBroker()
    ex, claim = await _pair(b, gen=2)
    await b.invalidate_agent("a", 2, "reclaimed")
    await ex.start_response("a", claim.claim_id, 2)
    await ex.finish("a", claim.claim_id, 2)


@pytest.mark.asyncio
async def test_configured_buffer_backpressure_and_bound():
    b = RelayBroker(max_response_buffer=32 * 1024)
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.write("a", claim.claim_id, 1, b"x" * (16 * 1024))
    await ex.write("a", claim.claim_id, 1, b"x" * (16 * 1024))
    blocked = asyncio.create_task(ex.write("a", claim.claim_id, 1, b"y" * (16 * 1024)))
    await asyncio.sleep(0)
    assert not blocked.done()
    assert await ex.__anext__() == b"x" * (16 * 1024)
    await asyncio.sleep(0)
    assert await blocked is None
    assert await ex.__anext__() == b"x" * (16 * 1024)
    assert await ex.__anext__() == b"y" * (16 * 1024)


@pytest.mark.asyncio
async def test_invalidation_releases_blocked_producer_no_leak():
    b = RelayBroker(max_response_buffer=32 * 1024)
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.write("a", claim.claim_id, 1, b"a" * (16 * 1024))
    await ex.write("a", claim.claim_id, 1, b"b" * (16 * 1024))
    blocked = asyncio.create_task(ex.write("a", claim.claim_id, 1, b"c" * (16 * 1024)))
    await asyncio.sleep(0)
    assert not blocked.done()
    await b.invalidate_agent("a", 2, "reclaimed")
    with pytest.raises(RelayStateError):
        await blocked
    assert blocked.done()
    # contiguous prefix preserved, no dropped-oldest hole, then explicit error
    assert await ex.__anext__() == b"a" * (16 * 1024)
    assert await ex.__anext__() == b"b" * (16 * 1024)
    with pytest.raises(RelayStateError, match="reclaimed"):
        await ex.__anext__()


@pytest.mark.asyncio
async def test_disconnect_releases_blocked_producer():
    b = RelayBroker(max_response_buffer=32 * 1024)
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.write("a", claim.claim_id, 1, b"a" * (16 * 1024))
    await ex.write("a", claim.claim_id, 1, b"b" * (16 * 1024))
    blocked = asyncio.create_task(ex.write("a", claim.claim_id, 1, b"c" * (16 * 1024)))
    await asyncio.sleep(0)
    assert not blocked.done()
    await ex.aclose()
    with pytest.raises(RelayStateError):
        await blocked
    assert blocked.done()


@pytest.mark.asyncio
async def test_full_buffer_invalidation_preserves_contiguous_prefix():
    b = RelayBroker(max_response_buffer=4 * 32 * 1024)
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    labels = [b"a", b"b", b"c", b"d"]
    for label in labels:
        await ex.write("a", claim.claim_id, 1, label * (32 * 1024))
    await b.invalidate_agent("a", 2, "reclaimed")
    for label in labels:
        assert await ex.__anext__() == label * (32 * 1024)
    with pytest.raises(RelayStateError, match="reclaimed"):
        await ex.__anext__()


@pytest.mark.asyncio
async def test_concurrent_agents_and_slots_are_isolated():
    b = RelayBroker()
    wa = asyncio.create_task(b.claim("a", 1, DEADLINE))
    wb = asyncio.create_task(b.claim("b", 1, DEADLINE))
    await asyncio.sleep(0)
    exa = await b.offer("a", 8100, b"{}", time.monotonic() + DEADLINE)
    exb = await b.offer("b", 8200, b"{}", time.monotonic() + DEADLINE)
    ca = await wa
    cb = await wb
    assert ca.claim_id != cb.claim_id
    await exa.start_response("a", ca.claim_id, 1)
    await exa.write("a", ca.claim_id, 1, b"A")
    await exa.finish("a", ca.claim_id, 1)
    await exb.start_response("b", cb.claim_id, 1)
    await exb.write("b", cb.claim_id, 1, b"B")
    await exb.finish("b", cb.claim_id, 1)
    assert await exa.__anext__() == b"A"
    assert await exb.__anext__() == b"B"


@pytest.mark.asyncio
async def test_teardown_leaves_no_pending_tasks():
    before = set(asyncio.all_tasks())
    b = RelayBroker(max_response_buffer=32 * 1024)
    ex, claim = await _pair(b)
    await ex.start_response("a", claim.claim_id, 1)
    await ex.write("a", claim.claim_id, 1, b"a" * (16 * 1024))
    await ex.write("a", claim.claim_id, 1, b"b" * (16 * 1024))
    blocked = asyncio.create_task(ex.write("a", claim.claim_id, 1, b"c" * (16 * 1024)))
    await asyncio.sleep(0)
    await b.invalidate_agent("a", 2, "reclaimed")
    with pytest.raises(RelayStateError):
        await blocked
    while True:
        try:
            await ex.__anext__()
        except RelayStateError:
            break
    leaked = {t for t in asyncio.all_tasks() if t not in before and not t.done()}
    leaked.discard(asyncio.current_task())
    assert not leaked
