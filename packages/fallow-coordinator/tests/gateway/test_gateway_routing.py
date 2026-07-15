"""Model resolution, the shed metric, and the /v1/models catalogue shape."""

from collections.abc import Sequence

from gateway_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    EMBED_MODEL,
    RESTRICTED_KEY,
    buffered_handler,
    make_endpoint,
)

from fallow_coordinator.gateway import LogStatus
from fallow_protocol.messages import ReplicaEndpoint

_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
_ENDPOINTS = {CHAT_MODEL: (make_endpoint("h1", 8001),)}


async def test_unknown_model_returns_404(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=buffered_handler(b"{}"), endpoints=_ENDPOINTS)
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": "ghost-model"}, headers=_AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"


async def test_no_replica_sheds_with_503_and_logs(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b"{}"),
        endpoints={CHAT_MODEL: ()},  # registered model, zero ready replicas
    )
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=_AUTH
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "no_replica_available"
    assert len(harness.log.entries) == 1
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SHED
    assert entry.model_id == CHAT_MODEL
    assert entry.t_first_byte is None
    assert entry.retried is False


async def test_malformed_body_returns_400(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=buffered_handler(b"{}"), endpoints=_ENDPOINTS)
    response = await harness.client.post("/v1/chat/completions", content=b"not json", headers=_AUTH)
    assert response.status_code == 400


async def test_models_list_shape(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=buffered_handler(b"{}"), endpoints=_ENDPOINTS)
    response = await harness.client.get("/v1/models", headers=_AUTH)
    body = response.json()
    assert response.status_code == 200
    assert body["object"] == "list"
    ids = {item["id"] for item in body["data"]}
    assert ids == {CHAT_MODEL, EMBED_MODEL}
    first = body["data"][0]
    assert first["object"] == "model"
    assert first["owned_by"] == "fallow"
    assert isinstance(first["created"], int)


async def test_models_list_filtered_by_allowlist(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=buffered_handler(b"{}"), endpoints=_ENDPOINTS)
    response = await harness.client.get(
        "/v1/models", headers={"Authorization": f"Bearer {RESTRICTED_KEY}"}
    )
    ids = {item["id"] for item in response.json()["data"]}
    assert ids == {CHAT_MODEL}
    assert EMBED_MODEL not in ids


async def test_models_list_requires_auth(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=buffered_handler(b"{}"), endpoints=_ENDPOINTS)
    assert (await harness.client.get("/v1/models")).status_code == 401


async def test_reported_inflight_reaches_picker_without_local_history(build_gateway) -> None:
    seen: list[tuple[int, ...]] = []
    busy = make_endpoint("h1", 8001).model_copy(update={"inflight": 2})
    idle = make_endpoint("h2", 8002, agent_id="agent-2")

    def pick(_model: str, replicas: Sequence[ReplicaEndpoint]) -> ReplicaEndpoint | None:
        seen.append(tuple(replica.inflight for replica in replicas))
        return min(replicas, key=lambda replica: replica.inflight) if replicas else None

    harness = await build_gateway(
        upstream_handler=buffered_handler(b"{}"),
        endpoints={CHAT_MODEL: (busy, idle)},
        pick=pick,
    )
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=_AUTH
    )

    assert response.status_code == 200
    assert seen == [(2, 0)]
    assert harness.log.entries[0].agent_id == "agent-2"
