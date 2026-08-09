import asyncio
import time
import pytest
from fallow_coordinator.site_relay import RelayBroker, RelayRequest, RelayRequestTooLarge, RelayStateError

@pytest.mark.asyncio
async def test_claim_offer_roundtrip():
    b=RelayBroker(); ctask=asyncio.create_task(b.claim("a", 4, 1)); await asyncio.sleep(0)
    ex=await b.offer("a",8100,RelayRequest(body=b"{}"),time.monotonic()+1); c=await ctask
    assert c and c.claim_id==ex.claim_id and c.presence_generation==4
    await ex.start_response("a",c.claim_id,4); await ex.write("a",c.claim_id,4,b"ok"); await ex.finish("a",c.claim_id,4)
    assert await ex.__anext__()==b"ok"
    with pytest.raises(StopAsyncIteration): await ex.__anext__()

@pytest.mark.asyncio
async def test_offer_requires_waiter_and_limits_body():
    b=RelayBroker()
    with pytest.raises(RelayStateError): await b.offer("a",1,b"",time.monotonic()+1)
    c=asyncio.create_task(b.claim("a",0,1)); await asyncio.sleep(0)
    with pytest.raises(RelayRequestTooLarge): await b.offer("a",1,b"x"*(2*1024*1024+1),time.monotonic()+1)
    c.cancel()
