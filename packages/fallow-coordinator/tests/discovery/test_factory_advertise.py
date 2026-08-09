"""The coordinator lifespan's half of the contract, with the advertiser injected.

Covers the enabled path (register on startup, unregister on shutdown, including
when the body raises), the disabled path (nothing registered, nothing built) and
a bad interface failing ``create_app`` before anything binds.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.discovery import AdvertiseError

ADMIN_KEY = "admin-secret-key"


class RecordingAdvertiser:
    """Counts register/unregister calls and keeps the record it was handed."""

    def __init__(self):
        self.registered = []
        self.unregistered = 0

    async def register(self, advertisement):
        self.registered.append(advertisement)

    async def unregister(self):
        self.unregistered += 1


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
    certfile = tmp_path / "c.pem"
    keyfile = tmp_path / "k.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def _config(tmp_path: Path, *, mdns: bool, host: str = "127.0.0.1") -> CoordinatorConfig:
    certfile, keyfile = _cert(tmp_path)
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        host=host,
        port=8443,
        requeue_interval_s=3600.0,
        poll_sleep_s=0.01,
        admission_timeout_s=0,
        site={
            "enabled": True,
            "site_id": "school-1",
            "public_urls": ["https://coordinator.school:8443/"],
            "tls_certfile": certfile,
            "tls_keyfile": keyfile,
            "mdns_service": "_fallow._tcp.local." if mdns else None,
        },
    )


async def test_lifespan_registers_and_unregisters(tmp_path: Path):
    advertiser = RecordingAdvertiser()
    app = create_app(_config(tmp_path, mdns=True), sleep=asyncio.sleep, advertiser=advertiser)
    async with app.router.lifespan_context(app):
        assert len(advertiser.registered) == 1
        assert advertiser.unregistered == 0
    assert advertiser.unregistered == 1


async def test_registered_record_matches_the_configured_address(tmp_path: Path):
    advertiser = RecordingAdvertiser()
    app = create_app(_config(tmp_path, mdns=True), sleep=asyncio.sleep, advertiser=advertiser)
    async with app.router.lifespan_context(app):
        pass
    record = advertiser.registered[0]
    assert record.site_id == "school-1"
    assert record.addresses == ("127.0.0.1",)
    assert record.port == 8443
    assert record.txt == {"version": "1", "site_id": "school-1"}


async def test_record_is_withdrawn_when_the_app_body_raises(tmp_path: Path):
    advertiser = RecordingAdvertiser()
    app = create_app(_config(tmp_path, mdns=True), sleep=asyncio.sleep, advertiser=advertiser)
    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("boom")
    assert advertiser.unregistered == 1


async def test_disabled_mdns_never_touches_the_advertiser(tmp_path: Path):
    advertiser = RecordingAdvertiser()
    app = create_app(_config(tmp_path, mdns=False), sleep=asyncio.sleep, advertiser=advertiser)
    async with app.router.lifespan_context(app):
        pass
    assert advertiser.registered == []
    assert advertiser.unregistered == 0


async def test_disabled_mdns_builds_no_default_advertiser(tmp_path: Path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("mDNS off must not construct a responder")

    monkeypatch.setattr("fallow_coordinator.app.factory.ZeroconfAdvertiser", forbidden)
    app = create_app(_config(tmp_path, mdns=False), sleep=asyncio.sleep)
    async with app.router.lifespan_context(app):
        pass


async def test_ambiguous_interface_fails_create_app(tmp_path: Path, monkeypatch):
    def resolve(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.6", 0)),
        ]

    config = _config(tmp_path, mdns=True)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(AdvertiseError, match="ambiguous"):
        create_app(config, sleep=asyncio.sleep, advertiser=RecordingAdvertiser())
