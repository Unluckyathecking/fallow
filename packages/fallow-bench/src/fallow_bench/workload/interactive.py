"""Open-loop interactive load driver (module B1).

Fires each scheduled request at its offset via streaming ``chat/completions``
and records one :class:`RequestRecord` per request. Crucially the scheduling
loop launches request *k+1* without awaiting request *k* (`asyncio.create_task`)
— the open-loop property that lets a slow arm accumulate queueing delay
(ADR 019 §1). The per-request timeout is a real-time guard via
``asyncio.wait_for``; all recorded timestamps come from the injected clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.records import RequestRecord, RequestStatus
from fallow_bench.workload.schedule import Arrival
from fallow_bench.workload.writer import JsonlWriter

_CHAT_PATH = "/v1/chat/completions"
_SSE_PREFIX = "data:"
_SSE_DONE = "[DONE]"
_HTTP_ERROR_FLOOR = 400


@dataclass(frozen=True)
class _Attempt:
    """Outcome of one HTTP attempt (before final-record assembly)."""

    t_first_token: datetime | None
    tokens_out: int
    http_status: int | None
    status: RequestStatus


def chat_body(model_id: str, prompt: str, max_tokens: int) -> dict[str, object]:
    """Build a streaming OpenAI ``chat/completions`` request body."""
    return {
        "model": model_id,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }


class InteractiveDriver:
    """Fires the fixed arrival schedule open-loop and records each request."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        model_id: str,
        prompts: Sequence[str],
        schedule: Sequence[Arrival],
        timeout_s: float,
        clocks: Clocks,
        writer: JsonlWriter,
    ) -> None:
        self._client = client
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._model_id = model_id
        self._prompts = prompts
        self._schedule = schedule
        self._timeout_s = timeout_s
        self._clocks = clocks
        self._writer = writer

    async def run(self) -> None:
        """Drive the whole schedule, then wait for in-flight requests."""
        start = self._clocks.monotonic()
        run_start = self._clocks.now()
        tasks: list[asyncio.Task[None]] = []
        for arrival in self._schedule:
            await self._pace_to(start, arrival.t_offset_s)
            scheduled = run_start + timedelta(seconds=arrival.t_offset_s)
            tasks.append(asyncio.create_task(self._fire(arrival, scheduled)))
        if tasks:
            await asyncio.gather(*tasks)

    async def _pace_to(self, start: float, offset_s: float) -> None:
        delay = offset_s - (self._clocks.monotonic() - start)
        if delay > 0:
            await self._clocks.sleep(delay)

    async def _fire(self, arrival: Arrival, scheduled: datetime) -> None:
        t_submit = self._clocks.now()
        prompt = self._prompts[arrival.prompt_idx]
        attempt = await self._attempt(prompt, arrival.max_tokens)
        self._writer.write(
            RequestRecord(
                req_id=arrival.idx,
                prompt_idx=arrival.prompt_idx,
                t_scheduled=scheduled,
                t_submit=t_submit,
                t_first_token=attempt.t_first_token,
                t_done=self._clocks.now(),
                status=attempt.status,
                http_status=attempt.http_status,
                tokens_out=attempt.tokens_out,
            )
        )

    async def _attempt(self, prompt: str, max_tokens: int) -> _Attempt:
        body = chat_body(self._model_id, prompt, max_tokens)
        try:
            return await asyncio.wait_for(self._stream(body), self._timeout_s)
        except TimeoutError:
            return _Attempt(None, 0, None, RequestStatus.TIMEOUT)
        except httpx.HTTPError:
            return _Attempt(None, 0, None, RequestStatus.ERROR)

    async def _stream(self, body: dict[str, object]) -> _Attempt:
        t_first: datetime | None = None
        tokens = 0
        async with self._client.stream("POST", _CHAT_PATH, json=body, headers=self._auth) as resp:
            if resp.status_code >= _HTTP_ERROR_FLOOR:
                await resp.aread()
                return _Attempt(None, 0, resp.status_code, RequestStatus.ERROR)
            async for line in resp.aiter_lines():
                if not line.startswith(_SSE_PREFIX):
                    continue
                if line[len(_SSE_PREFIX) :].strip() == _SSE_DONE:
                    break
                if t_first is None:
                    t_first = self._clocks.now()
                tokens += 1
        return _Attempt(t_first, tokens, resp.status_code, RequestStatus.OK)
