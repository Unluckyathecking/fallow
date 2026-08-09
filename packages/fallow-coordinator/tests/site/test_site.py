import base64
import hashlib
import socket
import ssl
import subprocess
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fallow_coordinator.app.config import CoordinatorConfig, SiteConfig
from fallow_coordinator.site.models import JoinBundlesRequest, JoinBundleV1
from fallow_coordinator.site.router import _spki_pin


def base(tmp_path, **kw):
    return CoordinatorConfig(
        db_path=tmp_path / "d",
        blob_dir=tmp_path / "b",
        unit_input_dir=tmp_path / "u",
        events_jsonl_path=tmp_path / "e",
        gateway_log_path=tmp_path / "g",
        admin_key="x",
        **kw,
    )


def cert(tmp_path):
    c = tmp_path / "c.pem"
    k = tmp_path / "k.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(k),
            "-out",
            str(c),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return c, k


def dated_cert(tmp_path, not_before, not_after, name="dated"):
    """Self-signed cert/key pair with an explicit validity window (for expiry tests)."""
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
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    c = tmp_path / f"{name}-c.pem"
    k = tmp_path / f"{name}-k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return c, k


def test_site_defaults_equal_legacy(tmp_path):
    assert base(tmp_path).site == SiteConfig()


def test_site_rejects_wildcard_and_http(tmp_path):
    c, k = cert(tmp_path)
    with pytest.raises(ValueError):
        base(
            tmp_path,
            host="0.0.0.0",
            site={
                "enabled": True,
                "site_id": "x",
                "public_urls": ["https://x/"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "x",
                "public_urls": ["http://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )


def test_spki_pin_and_strict_models(tmp_path):
    c, _ = cert(tmp_path)
    assert _spki_pin(c).startswith("sha256/")
    assert JoinBundlesRequest(count=1).count == 1
    with pytest.raises(ValueError):
        JoinBundlesRequest(count=17)
    with pytest.raises(ValueError):
        JoinBundleV1(
            site_id="x",
            coordinator_urls=("https://x",),
            coordinator_spki_sha256=("sha256/x",),
            enrollment_token="t",
            extra="x",
        )


def site_config(tmp_path, c, k):
    return base(
        tmp_path,
        site={
            "enabled": True,
            "site_id": "x",
            "public_urls": ["https://x/"],
            "tls_certfile": c,
            "tls_keyfile": k,
        },
    )


def test_router_http_contract_and_auth(tmp_path):
    c, k = cert(tmp_path)
    settings = site_config(tmp_path, c, k)
    app = FastAPI()

    class Registry:
        async def authenticate_api_key(self, token):
            from fallow_coordinator.registry.records import ApiKeyInfo

            return ApiKeyInfo(
                name="admin", key_id="x", model_allowlist=None, is_admin=token == "admin"
            )

    class State:
        registry = Registry()

    app.state.coordinator = State()
    tokens = []

    async def mint():
        tokens.append("token-" + str(len(tokens)))
        return tokens[-1]

    from fallow_coordinator.site.router import build_site_admin_router

    app.include_router(build_site_admin_router(settings, mint))
    with TestClient(app) as client:
        assert client.post("/v1/admin/site/join-bundles", json={"count": 1}).status_code == 401
        for n in (1, 16):
            r = client.post(
                "/v1/admin/site/join-bundles",
                json={"count": n},
                headers={"Authorization": "Bearer admin"},
            )
            assert r.status_code == 201
            assert len(r.json()["bundles"]) == n
        assert (
            client.post(
                "/v1/admin/site/join-bundles",
                json={"count": 17},
                headers={"Authorization": "Bearer admin"},
            ).status_code
            == 422
        )
        bundles = client.post(
            "/v1/admin/site/join-bundles",
            json={"count": 2},
            headers={"Authorization": "Bearer admin"},
        ).json()["bundles"]
        assert bundles[0]["enrollment_token"] != bundles[1]["enrollment_token"]
        assert set(bundles[0]) == {
            "version",
            "site_id",
            "coordinator_urls",
            "coordinator_spki_sha256",
            "enrollment_token",
            "mdns_service",
        }
        assert "admin" not in str(bundles) and "token-" in str(bundles)


def test_router_callback_failure_and_key_mismatch(tmp_path):
    c, k = cert(tmp_path)
    bad = tmp_path / "bad.pem"
    bad.write_text(c.read_text())
    settings = site_config(tmp_path, c, k)
    app = FastAPI()

    class Registry:
        async def authenticate_api_key(self, token):
            from fallow_coordinator.registry.records import ApiKeyInfo

            return ApiKeyInfo(name="admin", key_id="x", model_allowlist=None, is_admin=True)

    class State:
        registry = Registry()

    app.state.coordinator = State()

    async def fail():
        raise RuntimeError("mint failed")

    from fallow_coordinator.site.router import build_site_admin_router

    app.include_router(build_site_admin_router(settings, fail))
    with TestClient(app, raise_server_exceptions=True) as client, pytest.raises(RuntimeError):
        client.post(
            "/v1/admin/site/join-bundles",
            json={"count": 1},
            headers={"Authorization": "Bearer admin"},
        )
    with pytest.raises(ValueError):
        site_config(tmp_path, bad, tmp_path / "missing.key")


def test_site_rejects_ipv6_wildcard_invalid_urls_and_mdns(tmp_path):
    c, k = cert(tmp_path)
    for host in ("::0", "0:0:0:0:0:0:0:0"):
        with pytest.raises(ValueError):
            base(
                tmp_path,
                host=host,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": ["https://x"],
                    "tls_certfile": c,
                    "tls_keyfile": k,
                },
            )
    for url in ("https://:8330", "https://host:notaport", "https://host:70000"):
        with pytest.raises(ValueError):
            base(
                tmp_path,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": [url],
                    "tls_certfile": c,
                    "tls_keyfile": k,
                },
            )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "",
                "public_urls": ["https://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "x",
                "public_urls": ["https://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
                "mdns_service": "_wrong._tcp.local.",
            },
        )


def test_live_https_presented_spki_matches_bundle_pin(tmp_path):
    c, k = cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(c), str(k))
    ready = threading.Event()
    port_holder = []

    def serve_once():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port_holder.append(listener.getsockname()[1])
            ready.set()
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True):
                pass

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    assert ready.wait(2)
    client_context = ssl.create_default_context()
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", port_holder[0])) as raw,
        client_context.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        peer = tls.getpeercert(binary_form=True)
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    presented = (
        x509.load_der_x509_certificate(peer)
        .public_key()
        .public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    )
    presented_pin = "sha256/" + base64.b64encode(hashlib.sha256(presented).digest()).decode("ascii")
    assert presented_pin == _spki_pin(c)
    thread.join(timeout=2)


def test_site_rejects_legacy_wildcards_duplicates_and_long_ids(tmp_path):
    c, k = cert(tmp_path)
    for host in ("0", "0x0", "0.0.0.00"):
        with pytest.raises(ValueError):
            base(
                tmp_path,
                host=host,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": ["https://x"],
                    "tls_certfile": c,
                    "tls_keyfile": k,
                },
            )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "x",
                "public_urls": ["https://x", "https://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "x" * 129,
                "public_urls": ["https://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )
    with pytest.raises(ValueError):
        base(
            tmp_path,
            site={
                "enabled": True,
                "site_id": "x",
                "public_urls": ["HTTPS://x"],
                "tls_certfile": c,
                "tls_keyfile": k,
            },
        )


def _write_factory_config(tmp_path, lines):
    """Serialize a coordinator TOML with POSIX paths so it parses on Windows too."""
    config = tmp_path / "coordinator.toml"
    posix = {name: (tmp_path / name).as_posix() for name in ("d", "b", "u", "e", "g")}
    base_lines = [
        f'db_path = "{posix["d"]}"',
        f'blob_dir = "{posix["b"]}"',
        f'unit_input_dir = "{posix["u"]}"',
        f'events_jsonl_path = "{posix["e"]}"',
        f'gateway_log_path = "{posix["g"]}"',
        'admin_key = "x"',
    ]
    config.write_text("\n".join(base_lines + lines), encoding="utf-8")
    return config


def test_build_app_factory_fails_closed_under_site_mode(tmp_path, monkeypatch):
    from fallow_coordinator.app import build_app
    from fallow_coordinator.app.factory import CONFIG_ENV

    c, k = cert(tmp_path)
    config = _write_factory_config(
        tmp_path,
        [
            'host = "127.0.0.1"',
            "[site]",
            "enabled = true",
            'site_id = "x"',
            'public_urls = ["https://x/"]',
            f'tls_certfile = "{c.as_posix()}"',
            f'tls_keyfile = "{k.as_posix()}"',
        ],
    )
    monkeypatch.setenv(CONFIG_ENV, str(config))
    with pytest.raises(RuntimeError, match="serve"):
        build_app()


def test_build_app_factory_serves_legacy_http(tmp_path, monkeypatch):
    from fallow_coordinator.app import build_app
    from fallow_coordinator.app.factory import CONFIG_ENV

    config = _write_factory_config(tmp_path, [])
    monkeypatch.setenv(CONFIG_ENV, str(config))
    app = build_app()
    assert app.title == "fallow-coordinator"


def test_site_rejects_ipv4_mapped_wildcard_binds(tmp_path):
    c, k = cert(tmp_path)
    for host in ("::ffff:0.0.0.0", "::ffff:0:0"):
        with pytest.raises(ValueError):
            base(
                tmp_path,
                host=host,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": ["https://x"],
                    "tls_certfile": c,
                    "tls_keyfile": k,
                },
            )


def test_site_rejects_public_url_authority_characters(tmp_path):
    c, k = cert(tmp_path)
    for url in ("https://ho st", "https://host\\evil", "https://%20", "https://a_b"):
        with pytest.raises(ValueError):
            base(
                tmp_path,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": [url],
                    "tls_certfile": c,
                    "tls_keyfile": k,
                },
            )


def test_site_rejects_certificate_outside_validity_window(tmp_path):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    expired_c, expired_k = dated_cert(
        tmp_path, now - timedelta(days=2), now - timedelta(days=1), name="expired"
    )
    future_c, future_k = dated_cert(
        tmp_path, now + timedelta(days=1), now + timedelta(days=2), name="future"
    )
    for cf, kf in ((expired_c, expired_k), (future_c, future_k)):
        with pytest.raises(ValueError, match="expired or not yet valid"):
            base(
                tmp_path,
                site={
                    "enabled": True,
                    "site_id": "x",
                    "public_urls": ["https://x"],
                    "tls_certfile": cf,
                    "tls_keyfile": kf,
                },
            )
    valid_c, valid_k = dated_cert(
        tmp_path, now - timedelta(days=1), now + timedelta(days=1), name="valid"
    )
    assert base(
        tmp_path,
        site={
            "enabled": True,
            "site_id": "x",
            "public_urls": ["https://x"],
            "tls_certfile": valid_c,
            "tls_keyfile": valid_k,
        },
    ).site.enabled


def test_router_pin_retained_across_on_disk_rotation(tmp_path):
    c, k = cert(tmp_path)
    settings = site_config(tmp_path, c, k)
    app = FastAPI()

    class Registry:
        async def authenticate_api_key(self, token):
            from fallow_coordinator.registry.records import ApiKeyInfo

            return ApiKeyInfo(name="admin", key_id="x", model_allowlist=None, is_admin=True)

    class State:
        registry = Registry()

    app.state.coordinator = State()

    async def mint():
        return "token"

    from fallow_coordinator.site.router import _spki_pin, build_site_admin_router

    startup_pin = _spki_pin(c)
    router = build_site_admin_router(settings, mint)
    app.include_router(router)

    # Rotate the certificate on disk after startup; the emitted pin must stay
    # aligned with the certificate the listener loaded, not the new file.
    rotated_c, _ = cert(tmp_path)
    c.write_bytes(rotated_c.read_bytes())
    assert _spki_pin(c) != startup_pin

    with TestClient(app) as client:
        bundle = client.post(
            "/v1/admin/site/join-bundles",
            json={"count": 1},
            headers={"Authorization": "Bearer admin"},
        ).json()["bundles"][0]
    assert bundle["coordinator_spki_sha256"] == [startup_pin]
