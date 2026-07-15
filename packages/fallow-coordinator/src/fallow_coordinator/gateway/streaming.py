"""The streaming response body generator.

Forwards the upstream replica's bytes to the client **raw** — the first chunk
was already read during acquisition, the rest come straight off
``response.aiter_raw()``. SSE frames are never parsed or re-serialised; the
``[DONE]`` sentinel and every byte in between pass through untouched.

Two invariants live here:

* Each subsequent chunk read is bounded by the inter-chunk timeout; a stalled
  replica ends the stream cleanly rather than hanging the client.
* The upstream response is ``aclose()``d and the inflight slot released in a
  ``finally``, so a client disconnect (which throws into this generator) still
  frees the connection and the counter. No retry happens here — the first byte
  is already out, so a mid-stream failure yields a truncated response.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx

from fallow_coordinator.gateway.inflight import InflightHold
from fallow_coordinator.gateway.proxy import StreamHandle

_MID_STREAM_ERRORS = (TimeoutError, httpx.TimeoutException, httpx.HTTPError)


async def stream_body(
    handle: StreamHandle,
    inter_chunk_timeout_s: float,
    hold: InflightHold,
    finalize: Callable[[], None],
) -> AsyncIterator[bytes]:
    """Yield the upstream body verbatim, then finalize exactly once."""
    try:
        if handle.first is not None:
            yield handle.first
        async for chunk in _guarded_chunks(handle.chunks, inter_chunk_timeout_s):
            yield chunk
    finally:
        await handle.response.aclose()
        hold.release()
        finalize()


async def _guarded_chunks(
    chunks: AsyncIterator[bytes], inter_chunk_timeout_s: float
) -> AsyncIterator[bytes]:
    while True:
        try:
            chunk = await asyncio.wait_for(anext(chunks), inter_chunk_timeout_s)
        except StopAsyncIteration:
            return
        except _MID_STREAM_ERRORS:
            return  # first byte already sent: terminate cleanly, never retry
        yield chunk
