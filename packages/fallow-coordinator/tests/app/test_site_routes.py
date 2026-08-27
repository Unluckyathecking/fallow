"""Vertical coverage for the mounted Site Mode routes over the ASGI app.

Exercises the join-bundle admin route, the authenticated relay claim/response/
failure routes and site presence fencing against a real coordinator app running
in Site Mode. A role-playing agent coroutine holds a claim while a gateway client
request is relayed end to end; no network, no llama-server, no GPU.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from app_helpers import (
    ADMIN_KEY,
    MODEL_ID,
    FakeClock,
    Harness,
    admin_headers,
    bearer,
    make_heartbeat,
    make_manifest,
    make_register_request,
    make_replica,
)
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.app.background import _invalidate_offline_relay
from fallow_coordinator.httpauth import authenticate_agent
from fallow_coordinator.rag import Chunk
from fallow_coordinator.site_relay import RelayStateError
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import AgentEvent, AgentState, EventKind

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _cert(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    c = tmp_path / "c.pem"
    k = tmp_path / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return c, k


def _site_config(tmp_path: Path) -> CoordinatorConfig:
    certfile, keyfile = _cert(tmp_path)
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        host="127.0.0.1",
        port=8443,
        requeue_interval_s=3600.0,
        poll_sleep_s=0.01,
        chunks_per_unit=32,
        admission_timeout_s=0,
        site={
            "enabled": True,
            "site_id": "school-1",
            "public_urls": ["https://coordinator.school:8443/"],
            "tls_certfile": certfile,
            "tls_keyfile": keyfile,
        },
    )


@pytest_asyncio.fixture
async def site_harness(tmp_path: Path) -> AsyncIterator[Harness]:
    clock = FakeClock()
    config = _site_config(tmp_path)
    app = create_app(config, now=clock, sleep=asyncio.sleep)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord")
        try:
            yield Harness(client=client, clock=clock, config=config, state=app.state.coordinator)
        finally:
            await client.aclose()


async def _register(client: httpx.AsyncClient, token: str, hostname: str) -> tuple[str, str]:
    body = make_register_request(token, hostname).model_dump(mode="json")
    resp = await client.post("/v1/agents/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return str(data["agent_id"]), str(data["device_token"])


async def _ready_site_agent(harness: Harness, hostname: str = "pc1") -> tuple[str, str]:
    """Enroll a site-mode agent and heartbeat it IDLE with a READY replica."""
    token = await harness.state.registry.create_enrollment_token(mode="site")
    agent_id, device_token = await _register(harness.client, token, hostname)
    await harness.state.registry.put_model(make_manifest(MODEL_ID), "weights")
    hb = make_heartbeat(agent_id, replicas=(make_replica(MODEL_ID),)).model_dump(mode="json")
    resp = await harness.client.post(
        f"/v1/agents/{agent_id}/heartbeat", json=hb, headers=bearer(device_token)
    )
    assert resp.status_code == 200, resp.text
    return agent_id, device_token


async def _await_relay_waiter(harness: Harness, agent_id: str) -> None:
    assert harness.state.relay is not None
    for _ in range(2000):
        if harness.state.relay._waiters.get(agent_id):
            return
        await asyncio.sleep(0)
    raise AssertionError("relay claim waiter never registered")


async def test_join_bundles_route_is_mounted(site_harness: Harness) -> None:
    resp = await site_harness.client.post(
        "/v1/admin/site/join-bundles", json={"count": 2}, headers=admin_headers()
    )
    assert resp.status_code == 201, resp.text
    bundles = resp.json()["bundles"]
    assert len(bundles) == 2
    assert all(b["site_id"] == "school-1" for b in bundles)
    # Distinct one-use enrollment tokens per bundle.
    assert len({b["enrollment_token"] for b in bundles}) == 2


async def test_join_bundles_requires_admin(site_harness: Harness) -> None:
    resp = await site_harness.client.post(
        "/v1/admin/site/join-bundles", json={"count": 1}, headers=bearer("not-admin")
    )
    assert resp.status_code == 401


async def test_claim_requires_matching_device_token(site_harness: Harness) -> None:
    agent_id, _ = await _ready_site_agent(site_harness)
    other_token = await site_harness.state.registry.create_enrollment_token(mode="site")
    _, other_device = await _register(site_harness.client, other_token, "pc2")
    resp = await site_harness.client.get(
        f"/v1/agents/{agent_id}/inference/claims?timeout_s=0", headers=bearer(other_device)
    )
    assert resp.status_code == 403


async def test_claim_unauthenticated(site_harness: Harness) -> None:
    agent_id, _ = await _ready_site_agent(site_harness)
    resp = await site_harness.client.get(f"/v1/agents/{agent_id}/inference/claims?timeout_s=0")
    assert resp.status_code == 401


async def test_direct_agent_has_no_relay_claim(site_harness: Harness) -> None:
    token = await site_harness.state.registry.create_enrollment_token(mode="legacy")
    agent_id, device_token = await _register(site_harness.client, token, "pc-direct")
    resp = await site_harness.client.get(
        f"/v1/agents/{agent_id}/inference/claims?timeout_s=0", headers=bearer(device_token)
    )
    assert resp.status_code == 404


async def test_claim_times_out_with_no_work(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    resp = await site_harness.client.get(
        f"/v1/agents/{agent_id}/inference/claims?timeout_s=0", headers=bearer(device_token)
    )
    assert resp.status_code == 204


async def test_relay_generation_end_to_end(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)

    async def agent_serves() -> None:
        claim_resp = await site_harness.client.get(
            f"/v1/agents/{agent_id}/inference/claims?timeout_s=5", headers=bearer(device_token)
        )
        assert claim_resp.status_code == 200, claim_resp.text
        claim = claim_resp.json()
        assert claim["path"] == "/v1/chat/completions"
        assert claim["replica_port"] == 8080
        gen = claim["presence_generation"]
        upload = await site_harness.client.post(
            f"/v1/agents/{agent_id}/inference/claims/{claim['claim_id']}/response",
            headers={
                **bearer(device_token),
                "X-Fallow-Presence-Generation": str(gen),
                "X-Fallow-Upstream-Status": "200",
                "Content-Type": "application/json",
            },
            content=b'{"choices":[{"text":"hi"}]}',
        )
        assert upload.status_code == 202, upload.text

    async def client_calls() -> httpx.Response:
        await _await_relay_waiter(site_harness, agent_id)
        return await site_harness.client.post(
            "/v1/chat/completions",
            headers=admin_headers(),
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}]},
        )

    agent_task = asyncio.create_task(agent_serves())
    resp = await client_calls()
    await agent_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"choices": [{"text": "hi"}]}


async def test_response_upload_unknown_claim_is_404(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    resp = await site_harness.client.post(
        f"/v1/agents/{agent_id}/inference/claims/deadbeefdeadbeef/response",
        headers={
            **bearer(device_token),
            "X-Fallow-Presence-Generation": "0",
            "X-Fallow-Upstream-Status": "200",
            "Content-Type": "application/json",
        },
        content=b"{}",
    )
    assert resp.status_code == 404


async def test_failure_report_unknown_claim_is_404(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    resp = await site_harness.client.post(
        f"/v1/agents/{agent_id}/inference/claims/deadbeefdeadbeef/failure",
        headers=bearer(device_token),
        json={"presence_generation": 0, "code": "became_active", "retryable": True},
    )
    assert resp.status_code == 404


async def test_user_returned_event_fences_and_invalidates(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    assert site_harness.state.relay is not None

    async def agent_claims() -> int:
        resp = await site_harness.client.get(
            f"/v1/agents/{agent_id}/inference/claims?timeout_s=5", headers=bearer(device_token)
        )
        assert resp.status_code == 200, resp.text
        return int(resp.json()["presence_generation"])

    async def offer_work() -> None:
        await _await_relay_waiter(site_harness, agent_id)
        await site_harness.state.relay.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)

    claim_task = asyncio.create_task(agent_claims())
    await offer_work()
    claim_gen = await claim_task

    event = AgentEvent(
        agent_id=agent_id, kind=EventKind.USER_RETURNED, at=START, detail={"sequence": "7"}
    )
    resp = await site_harness.client.post(
        f"/v1/agents/{agent_id}/events",
        json=event.model_dump(mode="json"),
        headers=bearer(device_token),
    )
    assert resp.status_code == 202
    # The presence event advanced the generation past the claim, so a later
    # response upload at the claim generation is rejected as gone.
    snapshots = await site_harness.state.registry.snapshots(site_harness.state.now())
    assert snapshots[0].state.value == "active"
    assert claim_gen == 0


async def test_rag_query_relays_a_site_embed_agent(tmp_path: Path) -> None:
    def bomb(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a site embedding agent must never be dialed directly")

    bomb_client = httpx.AsyncClient(transport=httpx.MockTransport(bomb))
    clock = FakeClock()
    app = create_app(
        _site_config(tmp_path), now=clock, sleep=asyncio.sleep, http_client=bomb_client
    )
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord")
        state = app.state.coordinator
        token = await state.registry.create_enrollment_token(mode="site")
        agent_id, device_token = await _register(client, token, "pc-embed")
        await state.registry.put_model(make_manifest("bge-small", WorkerKind.EMBED), "w")
        await state.rag.create_collection("policies", "bge-small", 2)
        await state.rag.upsert(
            "policies",
            (
                Chunk(chunk_id="planted", text="rail ok", metadata={}, embedding=(1.0, 0.0)),
                Chunk(chunk_id="decoy", text="kitchen", metadata={}, embedding=(0.0, 1.0)),
            ),
        )
        key = await state.registry.create_api_key("rag", ["bge-small"])
        hb = make_heartbeat(agent_id, replicas=(make_replica("bge-small"),)).model_dump(mode="json")
        hbr = await client.post(
            f"/v1/agents/{agent_id}/heartbeat", json=hb, headers=bearer(device_token)
        )
        assert hbr.status_code == 200

        async def agent_serves() -> None:
            resp = await client.get(
                f"/v1/agents/{agent_id}/inference/claims?timeout_s=5", headers=bearer(device_token)
            )
            assert resp.status_code == 200, resp.text
            claim = resp.json()
            assert claim["path"] == "/v1/embeddings"
            gen = claim["presence_generation"]
            payload = json.dumps(
                {"model": "bge-small", "data": [{"embedding": [1.0, 0.0], "index": 0}]}
            )
            up = await client.post(
                f"/v1/agents/{agent_id}/inference/claims/{claim['claim_id']}/response",
                headers={
                    **bearer(device_token),
                    "X-Fallow-Presence-Generation": str(gen),
                    "X-Fallow-Upstream-Status": "200",
                    "Content-Type": "application/json",
                },
                content=payload.encode(),
            )
            assert up.status_code == 202, up.text

        async def run_query() -> httpx.Response:
            for _ in range(2000):
                if state.relay is not None and state.relay._waiters.get(agent_id):
                    break
                await asyncio.sleep(0)
            return await client.post(
                "/v1/rag/collections/policies/query",
                headers=bearer(key),
                json={"q": "how do I travel", "k": 2},
            )

        task = asyncio.create_task(agent_serves())
        resp = await run_query()
        await task
        await client.aclose()
    assert resp.status_code == 200, resp.text
    assert [chunk["chunk_id"] for chunk in resp.json()["chunks"]] == ["planted", "decoy"]


async def test_nonretryable_failure_is_recorded(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    broker = site_harness.state.relay
    assert broker is not None
    claim_task = asyncio.create_task(broker.claim(agent_id, 0, timeout=5.0))
    await _await_relay_waiter(site_harness, agent_id)
    await broker.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)
    claim = await claim_task
    resp = await site_harness.client.post(
        f"/v1/agents/{agent_id}/inference/claims/{claim.claim_id}/failure",
        headers=bearer(device_token),
        json={"presence_generation": 0, "code": "upstream_error", "retryable": False},
    )
    assert resp.status_code == 202
    assert site_harness.state.relay_flags is not None
    assert site_harness.state.relay_flags.take(claim.claim_id) is True


async def test_response_upload_disconnect_terminates_claim(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    broker = site_harness.state.relay
    assert broker is not None
    claim_task = asyncio.create_task(broker.claim(agent_id, 0, timeout=5.0))
    await _await_relay_waiter(site_harness, agent_id)
    await broker.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)
    claim = await claim_task

    async def broken() -> AsyncIterator[bytes]:
        yield b"data: partial\n\n"
        raise RuntimeError("client vanished mid-upload")

    with pytest.raises((RuntimeError, httpx.HTTPError)):
        await site_harness.client.post(
            f"/v1/agents/{agent_id}/inference/claims/{claim.claim_id}/response",
            headers={
                **bearer(device_token),
                "X-Fallow-Presence-Generation": "0",
                "X-Fallow-Upstream-Status": "200",
                "Content-Type": "text/event-stream",
            },
            content=broken(),
        )
    # The handler terminated the claim, so it no longer holds relay capacity.
    with pytest.raises(RelayStateError):
        await broker.write(agent_id, claim.claim_id, 0, b"more")


async def test_heartbeat_active_invalidates_relay_work(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    broker = site_harness.state.relay
    assert broker is not None
    claim_task = asyncio.create_task(broker.claim(agent_id, 0, timeout=5.0))
    await _await_relay_waiter(site_harness, agent_id)
    await broker.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)
    claim = await claim_task
    hb = make_heartbeat(agent_id, state=AgentState.ACTIVE, replicas=(make_replica(MODEL_ID),))
    hb_json = hb.model_copy(update={"seq": 9}).model_dump(mode="json")
    resp = await site_harness.client.post(
        f"/v1/agents/{agent_id}/heartbeat", json=hb_json, headers=bearer(device_token)
    )
    assert resp.status_code == 200
    # The active heartbeat fenced the agent, so its in-flight claim is gone.
    with pytest.raises(RelayStateError):
        await broker.start_response(agent_id, claim.claim_id, 0, 200, "application/json")


async def test_offline_sweep_invalidates_relay_work(site_harness: Harness) -> None:
    agent_id, _device_token = await _ready_site_agent(site_harness)
    broker = site_harness.state.relay
    assert broker is not None
    claim_task = asyncio.create_task(broker.claim(agent_id, 0, timeout=5.0))
    await _await_relay_waiter(site_harness, agent_id)
    await broker.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)
    claim = await claim_task
    # The agent ages out; the offline sweep drops its relay work.
    site_harness.clock.advance(site_harness.config.offline_after_s + 1)
    offline = await site_harness.state.registry.list_offline(site_harness.state.now())
    assert agent_id in offline
    await _invalidate_offline_relay(site_harness.state, agent_id)
    with pytest.raises(RelayStateError):
        await broker.start_response(agent_id, claim.claim_id, 0, 200, "application/json")


async def test_revoking_an_agent_drops_its_in_flight_relay_work(site_harness: Harness) -> None:
    agent_id, device_token = await _ready_site_agent(site_harness)
    broker = site_harness.state.relay
    assert broker is not None
    claim_task = asyncio.create_task(broker.claim(agent_id, 0, timeout=5.0))
    await _await_relay_waiter(site_harness, agent_id)
    await broker.offer(agent_id, 8080, b'{"model":"x"}', deadline=1e9)
    claim = await claim_task

    resp = await site_harness.client.post(
        f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers()
    )
    assert resp.status_code == 204

    # The claim it was holding is invalid, and it cannot claim again.
    with pytest.raises(RelayStateError):
        await broker.start_response(agent_id, claim.claim_id, 0, 200, "application/json")
    again = await site_harness.client.get(
        f"/v1/agents/{agent_id}/inference/claims?timeout_s=0", headers=bearer(device_token)
    )
    assert again.status_code == 401


async def test_a_claim_authorised_before_revocation_can_never_form_a_waiter(
    site_harness: Harness,
) -> None:
    """The window a one-time generation bump does not close.

    A claim whose device-token check passed a moment before the revocation
    committed never sees a 401. It goes on to resolve its route and register
    with the broker *after* the bump — and a bump only fences the waiters that
    already exist, so that late waiter registered at the NEW generation and was
    eligible for one more gateway request against a machine the operator had
    just been told, with a 204, was cut off.

    The fence has to be the route itself. ``site_route`` was the one agent read
    without the ``revoked_at IS NULL`` that ``snapshots`` and ``list_offline``
    already carry; with it, no waiter can form however the two interleave.
    """
    agent_id, device_token = await _ready_site_agent(site_harness)
    state = site_harness.state
    assert state.relay is not None and state.site_route is not None
    assert await state.registry.replica_endpoints(MODEL_ID, site_harness.clock())

    # 1. The claim authorises while the token is still live.
    assert await authenticate_agent(state.registry, f"Bearer {device_token}") == agent_id

    # 2. The whole revocation runs inside the window that opens, bump and
    #    broker invalidation included.
    resp = await site_harness.client.post(
        f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers()
    )
    assert resp.status_code == 204

    # 3. Only now does the claim do what the handler does next: resolve its
    #    route. There is none, so it answers 404 and never reaches the broker.
    assert await state.site_route(agent_id) is None
    assert not state.relay._waiters.get(agent_id)

    # The live route is shut for the same token, and still forms no waiter.
    late = await site_harness.client.get(
        f"/v1/agents/{agent_id}/inference/claims?timeout_s=0", headers=bearer(device_token)
    )
    assert late.status_code == 401
    assert not state.relay._waiters.get(agent_id)

    # And the gateway has nowhere to put a request for that model on this desk:
    # it re-routes to another one or sheds, but it cannot pick this one.
    assert await state.registry.replica_endpoints(MODEL_ID, site_harness.clock()) == ()
