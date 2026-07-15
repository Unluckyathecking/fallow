"""Flip-latency verification: poll ``GET /state`` until the agent goes active.

After an injected input this measures the end-to-end
injected-input → replica-yield latency — a headline experiment metric. The poll
is bounded (``max_wait_s``) and driven entirely by the injected clock/sleeper so
it is deterministic under a fake transport.
"""

from __future__ import annotations

import httpx

from fallow_bench.churn import constants as k
from fallow_bench.churn.models import AgentTarget, VerifyConfig
from fallow_bench.churn.ports import Clock, Sleeper


def state_url(target: AgentTarget) -> str:
    return f"http://{target.host}:{target.bench_port}{k.STATE_PATH}"


async def measure_flip(
    *,
    client: httpx.AsyncClient,
    target: AgentTarget,
    since: float,
    clock: Clock,
    sleep: Sleeper,
    config: VerifyConfig,
) -> float | None:
    """Poll until ``state == active`` or the deadline; return flip ms, else None.

    ``since`` is the monotonic instant the input was injected; the returned
    latency is measured from there. Any transport error ends the poll (None).
    """
    deadline = since + config.max_wait_s
    while clock() < deadline:
        if await _is_active(client, target):
            return (clock() - since) * k.MS_PER_S
        await sleep(config.poll_interval_s)
    return None


async def _is_active(client: httpx.AsyncClient, target: AgentTarget) -> bool:
    try:
        response = await client.get(state_url(target))
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return bool(payload.get("state") == k.ACTIVE_STATE)
