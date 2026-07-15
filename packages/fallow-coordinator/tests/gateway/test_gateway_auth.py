"""Auth and per-key allowlist enforcement, all in the OpenAI error envelope."""

from conftest import GatewayHarness
from gateway_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    EMBED_MODEL,
    RESTRICTED_KEY,
    buffered_handler,
    make_endpoint,
)

_ENDPOINTS = {CHAT_MODEL: (make_endpoint("h1", 8001),)}


async def _make(build_gateway) -> GatewayHarness:
    return await build_gateway(
        upstream_handler=buffered_handler(b'{"ok":true}'), endpoints=_ENDPOINTS
    )


async def test_missing_key_returns_401(build_gateway) -> None:
    harness = await _make(build_gateway)
    response = await harness.client.post("/v1/chat/completions", json={"model": CHAT_MODEL})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_bad_bearer_returns_401(build_gateway) -> None:
    harness = await _make(build_gateway)
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


async def test_out_of_allowlist_model_returns_403(build_gateway) -> None:
    harness = await _make(build_gateway)
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": EMBED_MODEL},  # restricted key may only use CHAT_MODEL
        headers={"Authorization": f"Bearer {RESTRICTED_KEY}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_in_allowlist_model_is_permitted(build_gateway) -> None:
    harness = await _make(build_gateway)
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL},
        headers={"Authorization": f"Bearer {RESTRICTED_KEY}"},
    )
    assert response.status_code == 200


async def test_admin_key_bypasses_allowlist(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b"{}"),
        endpoints={EMBED_MODEL: (make_endpoint("h1", 8002, model_id=EMBED_MODEL),)},
    )
    response = await harness.client.post(
        "/v1/embeddings",
        json={"model": EMBED_MODEL},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert response.status_code == 200
