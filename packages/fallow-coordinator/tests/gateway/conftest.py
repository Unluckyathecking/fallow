"""Fixtures: a factory that assembles a gateway app over fake collaborators."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from gateway_helpers import (
    DEFAULT_KEYS,
    DEFAULT_MODELS,
    Clock,
    FakeGatewayRegistry,
    Handler,
    RecordingRequestLog,
    first_pick,
)
from httpx import ASGITransport

from fallow_coordinator.gateway import GatewayConfig, create_gateway_router
from fallow_coordinator.gateway.protocols import PickReplica
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.messages import ReplicaEndpoint
from fallow_protocol.models import ModelManifest


@dataclass
class GatewayHarness:
    client: httpx.AsyncClient  # test client -> gateway ASGI app
    log: RecordingRequestLog
    router: APIRouter  # carries the get_inflight seam


BuildGateway = Callable[..., "object"]


@pytest_asyncio.fixture
async def build_gateway() -> AsyncIterator[BuildGateway]:
    created: list[httpx.AsyncClient] = []

    async def _build(
        *,
        upstream_handler: Handler,
        endpoints: dict[str, tuple[ReplicaEndpoint, ...]],
        models: tuple[ModelManifest, ...] = DEFAULT_MODELS,
        api_keys: dict[str, ApiKeyInfo] | None = None,
        pick: PickReplica = first_pick,
        config: GatewayConfig | None = None,
    ) -> GatewayHarness:
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        created.append(upstream)
        log = RecordingRequestLog()
        registry = FakeGatewayRegistry(
            api_keys if api_keys is not None else DEFAULT_KEYS, endpoints, models
        )
        router = create_gateway_router(
            registry, pick, upstream, config or GatewayConfig(), log, Clock()
        )
        app = FastAPI()
        app.include_router(router)
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://gw")
        created.append(client)
        return GatewayHarness(client=client, log=log, router=router)

    yield _build
    for opened in created:
        await opened.aclose()
