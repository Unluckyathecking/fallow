"""Request-body validation and upstream-error hygiene on the chat route.

Two guarantees are pinned here: malformed chat bodies are rejected at the gateway
boundary with a clean 400 (the backend is never dialed), and a backend 5xx never
leaks its raw body to the client — it is sanitized into a gateway error and the
raw text stays in the server log.
"""

import logging

import pytest
from gateway_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    EMBED_MODEL,
    buffered_handler,
    make_endpoint,
)

from fallow_coordinator.gateway import LogStatus

_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
_ENDPOINTS = {CHAT_MODEL: (make_endpoint("h1", 8001),)}
_LEAKY_BODY = b'{"error":"json.exception.out_of_range.403 key not found"}'


async def _post(build_gateway, body: dict, *, upstream: bytes = b"{}", status: int = 200):
    harness = await build_gateway(
        upstream_handler=buffered_handler(upstream, status), endpoints=_ENDPOINTS
    )
    response = await harness.client.post("/v1/chat/completions", json=body, headers=_AUTH)
    return harness, response


@pytest.mark.parametrize(
    "body",
    [
        {"model": CHAT_MODEL},  # missing messages
        {"model": CHAT_MODEL, "messages": []},  # empty messages
        {"model": CHAT_MODEL, "messages": "hi"},  # messages not a list
        {"model": CHAT_MODEL, "messages": [{"role": "user"}]},  # message without content
        {"model": CHAT_MODEL, "messages": ["not-an-object"]},  # message not an object
    ],
)
async def test_rejects_bad_messages(build_gateway, body) -> None:
    _harness, response = await _post(build_gateway, body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize(
    "body",
    [
        {"n": 0},
        {"n": 2},
        {"max_tokens": -1},
        {"max_tokens": 0},
        {"temperature": 2.5},
        {"temperature": -0.1},
        {"top_p": 1.5},
        {"top_p": -0.1},
    ],
)
async def test_rejects_out_of_range_params(build_gateway, body) -> None:
    full = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hi"}], **body}
    _harness, response = await _post(build_gateway, full)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_bad_body_never_reaches_backend_and_leaks_nothing(build_gateway) -> None:
    harness, response = await _post(
        build_gateway, {"model": CHAT_MODEL}, upstream=_LEAKY_BODY, status=500
    )
    assert response.status_code == 400
    assert "json.exception" not in response.text
    assert harness.log.entries == []  # rejected before any routing or pick


async def test_valid_body_is_accepted(build_gateway) -> None:
    body = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "n": 1,
        "max_tokens": 16,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    _harness, response = await _post(build_gateway, body, upstream=b'{"id":"cmpl-1"}')
    assert response.status_code == 200
    assert response.content == b'{"id":"cmpl-1"}'


async def test_embeddings_route_skips_chat_validation(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"ok":true}'),
        endpoints={EMBED_MODEL: (make_endpoint("h1", 8002, model_id=EMBED_MODEL),)},
    )
    response = await harness.client.post(
        "/v1/embeddings", json={"model": EMBED_MODEL, "input": "hi"}, headers=_AUTH
    )
    assert response.status_code == 200


async def test_upstream_5xx_is_sanitized_and_logged(build_gateway, caplog) -> None:
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    with caplog.at_level(logging.WARNING, logger="fallow_coordinator.gateway.errors"):
        harness, response = await _post(build_gateway, body, upstream=_LEAKY_BODY, status=500)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "json.exception" not in response.text  # raw body sanitized out
    assert "json.exception" in caplog.text  # but present in the server log
    assert harness.log.entries[0].status is LogStatus.ERROR
