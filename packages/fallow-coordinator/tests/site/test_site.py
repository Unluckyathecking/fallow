import subprocess

import pytest

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
