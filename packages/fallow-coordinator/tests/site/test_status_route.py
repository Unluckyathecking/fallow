"""The read-only Site Mode fleet status route.

Driven against a real :class:`SqliteRegistry` and a real :class:`RelayBroker`
rather than fakes: the two accessors this route depends on are the point of the
change, so stubbing them would prove nothing. Everything is offline and clock-
injected.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fallow_coordinator.app.config import CoordinatorConfig
from fallow_coordinator.registry import RegistryConfig, SqliteRegistry
from fallow_coordinator.site.router import build_site_admin_router
from fallow_coordinator.site_relay import RelayBroker, RelayRequest
from fallow_protocol.capabilities import DeviceCaps, OsFamily
from fallow_protocol.messages import AgentState, Heartbeat, RegisterRequest
from fallow_protocol.models import ReplicaState, ReplicaStatus
from fallow_protocol.version import PROTOCOL_VERSION

ADMIN_KEY = "admin-secret-key"
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
STATUS = "/v1/admin/site/status"

EXPECTED_FIELDS = {
    "agent_id",
    "enrollment_mode",
    "transport",
    "heartbeat_age_s",
    "presence_state",
    "presence_generation",
    "available",
    "ready_replicas",
    "last_claim",
    "last_claim_code",
}


class Clock:
    """A hand-cranked wall clock injected as the registry's ``now``."""

    def __init__(self):
        self._t = START

    def __call__(self):
        return self._t

    def advance(self, seconds):
        self._t = self._t + timedelta(seconds=seconds)


def caps(hostname):
    return DeviceCaps(
        hostname=hostname,
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="test-cpu",
        cpu_cores=8,
        ram_mb=16384,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )


def heartbeat(agent_id, *, replicas=(), state=AgentState.IDLE, serving_paused=False):
    return Heartbeat(
        agent_id=agent_id,
        seq=1,
        sent_at=START,
        protocol_version=PROTOCOL_VERSION,
        state=state,
        user_idle_s=300.0,
        cpu_percent=5.0,
        mem_available_mb=8192,
        replicas=replicas,
        serving_paused=serving_paused,
    )


def tls_pair(tmp_path):
    """A throwaway self-signed cert/key the router pins at build time."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    # The config validates the cert against the real wall clock, not the injected
    # registry clock, so the validity window has to bracket "now".
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "site-cert.pem"
    keyfile = tmp_path / "site-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def site_settings(tmp_path):
    certfile, keyfile = tls_pair(tmp_path)
    return CoordinatorConfig(
        db_path=tmp_path / "d",
        blob_dir=tmp_path / "b",
        unit_input_dir=tmp_path / "u",
        events_jsonl_path=tmp_path / "e",
        gateway_log_path=tmp_path / "g",
        admin_key=ADMIN_KEY,
        site={
            "enabled": True,
            "site_id": "school-pilot",
            "public_urls": ["https://site.example"],
            "tls_certfile": certfile,
            "tls_keyfile": keyfile,
        },
    )


class State:
    """The slice of ``CoordinatorState`` the status route touches."""

    def __init__(self, registry, relay, clock):
        self.registry = registry
        self.relay = relay
        self.now = clock


@pytest.fixture
def clock():
    return Clock()


@pytest_asyncio.fixture
async def registry(clock, tmp_path):
    store = SqliteRegistry(tmp_path / "registry.db", RegistryConfig(admin_key=ADMIN_KEY), clock)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def enroll(registry, hostname, *, mode):
    token = await registry.create_enrollment_token(mode=mode)
    request = RegisterRequest(
        enrollment_token=token, protocol_version=PROTOCOL_VERSION, caps=caps(hostname)
    )
    response = await registry.register_agent(request, host="10.0.0.5")
    return response.agent_id


def build_client(tmp_path, registry, relay, clock):
    app = FastAPI()
    app.state.coordinator = State(registry, relay, clock)

    async def mint():
        return "enrollment-token-never-in-status"

    app.include_router(build_site_admin_router(site_settings(tmp_path), mint))
    return TestClient(app)


async def fail_one_claim(relay, agent_id, code, *, generation=0):
    """Drive one real claim through the broker to a typed failure."""
    import asyncio

    waiter = asyncio.create_task(relay.claim(agent_id, generation, 5.0))
    await asyncio.sleep(0)
    await relay.offer(agent_id, 8081, RelayRequest(body=b"{}"), time.monotonic() + 30)
    claim = await waiter
    await relay.fail(agent_id, claim.claim_id, generation, code)


def rows(response):
    return {row["agent_id"]: row for row in response.json()["agents"]}


async def test_reports_every_site_agent_and_only_site_agents(tmp_path, registry, clock):
    served = await enroll(registry, "desk-01", mode="site")
    quiet = await enroll(registry, "desk-02", mode="site")
    legacy = await enroll(registry, "laptop", mode="legacy")
    loading = ReplicaStatus(model_id="m", port=8081, state=ReplicaState.LOADING)
    await registry.record_heartbeat(served, heartbeat(served, replicas=(loading,)))
    await registry.record_heartbeat(quiet, heartbeat(quiet, replicas=(loading,)))
    relay = RelayBroker()
    await fail_one_claim(relay, served, "connect_failed")

    with build_client(tmp_path, registry, relay, clock) as client:
        response = client.get(STATUS, headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    assert response.status_code == 200
    body = rows(response)
    assert set(body) == {served, quiet}, "a direct agent is not part of the Site Mode fleet"
    assert legacy not in body
    for row in body.values():
        assert set(row) == EXPECTED_FIELDS
        assert row["enrollment_mode"] == "site"
        assert row["transport"] == "site_relay"
        assert row["presence_state"] == "idle"
        assert row["presence_generation"] == 0
        assert row["available"] is True
        assert row["ready_replicas"] == 0  # the only replica is still LOADING

    assert body[served]["last_claim"] == "failed"
    assert body[served]["last_claim_code"] == "connect_failed"
    assert body[quiet]["last_claim"] == "none"
    assert body[quiet]["last_claim_code"] is None


async def test_ready_replicas_presence_and_heartbeat_age(tmp_path, registry, clock):
    agent = await enroll(registry, "desk-01", mode="site")
    ready = ReplicaStatus(model_id="m", port=8081, state=ReplicaState.READY)
    loading = ReplicaStatus(model_id="n", port=8082, state=ReplicaState.LOADING)
    await registry.record_heartbeat(agent, heartbeat(agent, replicas=(ready, loading)))
    await registry.apply_presence_event(agent, "reclaim", 3)
    clock.advance(7.5)

    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        row = rows(client.get(STATUS, headers={"Authorization": f"Bearer {ADMIN_KEY}"}))[agent]

    assert row["ready_replicas"] == 1
    assert row["presence_state"] == "reclaimed"
    assert row["presence_generation"] == 1
    assert row["available"] is False
    assert row["heartbeat_age_s"] == 7.5


async def test_agent_that_stopped_heartbeating_is_still_reported(tmp_path, registry, clock):
    agent = await enroll(registry, "desk-01", mode="site")
    await registry.record_heartbeat(agent, heartbeat(agent))
    clock.advance(600.0)

    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        row = rows(client.get(STATUS, headers={"Authorization": f"Bearer {ADMIN_KEY}"}))[agent]

    assert row["presence_state"] == "offline"
    assert row["available"] is False
    assert row["ready_replicas"] == 0
    assert row["heartbeat_age_s"] == 600.0


async def test_empty_fleet_is_an_empty_list(tmp_path, registry, clock):
    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        response = client.get(STATUS, headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert response.status_code == 200
    assert response.json() == {"agents": []}


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, 401), ("Bearer nope", 401)],
)
async def test_route_requires_an_admin_key(tmp_path, registry, clock, header, expected):
    headers = {} if header is None else {"Authorization": header}
    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        assert client.get(STATUS, headers=headers).status_code == expected


async def test_client_key_is_not_admin_enough(tmp_path, registry, clock):
    key = await registry.create_api_key("pilot")
    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        assert client.get(STATUS, headers={"Authorization": f"Bearer {key}"}).status_code == 403


async def test_status_carries_no_join_material(tmp_path, registry, clock):
    agent = await enroll(registry, "desk-01", mode="site")
    await registry.record_heartbeat(agent, heartbeat(agent))

    with build_client(tmp_path, registry, RelayBroker(), clock) as client:
        response = client.get(STATUS, headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    text = response.text
    assert ADMIN_KEY not in text
    assert "enrollment-token-never-in-status" not in text
    assert "sha256/" not in text
    assert "token" not in text and "pin" not in text
