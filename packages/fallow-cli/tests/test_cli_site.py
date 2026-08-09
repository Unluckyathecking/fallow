from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from cli_helpers import COORD_URL

from fallow_cli import main
from fallow_cli.errors import CliError
from fallow_cli.models import SiteJoinBundle
from fallow_cli.site import write_join_bundles


def bundle(token="secret"):
    return SiteJoinBundle.model_validate(
        {
            "version": 1,
            "site_id": "pilot",
            "coordinator_urls": ["https://coord.example:8330"],
            "coordinator_spki_sha256": ["sha256/" + base64.b64encode(b"p" * 32).decode()],
            "enrollment_token": token,
            "mdns_service": None,
        }
    )


def test_atomic_owner_only_files_and_metadata(tmp_path):
    out = tmp_path / "join"
    meta = write_join_bundles((bundle(), bundle("other")), out, force=False)
    assert len(meta) == 2 and (out / "desk-01.fallow-join").exists()
    if sys.platform != "win32":
        assert os.stat(out / "desk-01.fallow-join").st_mode & 0o777 == 0o600
    assert json.loads((out / "desk-01.fallow-join").read_text())["enrollment_token"] == "secret"
    assert "secret" not in json.dumps(meta)


def test_refusal_and_force(tmp_path):
    out = tmp_path / "join"
    write_join_bundles((bundle(),), out, force=False)
    with pytest.raises(CliError):
        write_join_bundles((bundle("new"),), out, force=False)
    write_join_bundles((bundle("new"),), out, force=True)
    assert json.loads((out / "desk-01.fallow-join").read_text())["enrollment_token"] == "new"


@pytest.mark.parametrize(
    "field",
    ["version", "coordinator_urls", "coordinator_spki_sha256", "enrollment_token", "mdns_service"],
)
def test_invalid_bundle_rejected(field):
    b = {
        "version": 1,
        "site_id": "pilot",
        "coordinator_urls": ["https://coord.example"],
        "coordinator_spki_sha256": ["sha256/" + base64.b64encode(b"p" * 32).decode()],
        "enrollment_token": "x",
        "mdns_service": None,
    }
    b.pop(field)
    with pytest.raises((ValueError, TypeError)):
        SiteJoinBundle.model_validate(b)


def invoke(runner, env, args, *, json_output=False):
    flags = ["--coordinator-url", COORD_URL] + (["--json"] if json_output else [])
    return runner.invoke(main.app, [*flags, *args], env=env)


def wire(b):
    return b.model_dump(mode="json")


def test_command_posts_exact_count_and_redacts(runner, env, monkeypatch, tmp_path):
    store = {}
    b = bundle("TOPSECRET")

    def handler(req):
        store["method"] = req.method
        store["path"] = req.url.path
        store["body"] = json.loads(req.content)
        return __import__("httpx").Response(201, json={"bundles": [wire(b)]})

    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", __import__("httpx").MockTransport(handler))
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", "1", "--output", str(tmp_path)]
    )
    assert result.exit_code == 0 and store == {
        "method": "POST",
        "path": "/v1/admin/site/join-bundles",
        "body": {"count": 1},
    }
    assert "TOPSECRET" not in result.stdout and "TOPSECRET" not in result.stderr


def test_json_output_redacts_token(runner, env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        __import__("httpx").MockTransport(
            lambda req: __import__("httpx").Response(
                201, json={"bundles": [wire(bundle("SECRET"))]}
            )
        ),
    )
    result = invoke(
        runner,
        env,
        ["site", "join-bundles", "--count", "1", "--output", str(tmp_path)],
        json_output=True,
    )
    assert result.exit_code == 0 and "SECRET" not in result.stdout and "SECRET" not in result.stderr


@pytest.mark.parametrize("status", [401, 403])
def test_admin_rejection_redacts_token(runner, env, monkeypatch, tmp_path, status):
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        __import__("httpx").MockTransport(
            lambda req: __import__("httpx").Response(status, json={"detail": "SECRET"})
        ),
    )
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", "1", "--output", str(tmp_path)]
    )
    assert result.exit_code == 2 and "SECRET" not in result.stdout and "SECRET" not in result.stderr


@pytest.mark.parametrize("count", [0, 17])
def test_count_bounds(runner, env, tmp_path, count):
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", str(count), "--output", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_site_command_uses_no_proxy_client(runner, env, monkeypatch, tmp_path):
    """The site endpoint must be reached with a direct, no-proxy client."""
    import httpx as _httpx

    captured: dict[str, object] = {}
    real_client = _httpx.Client

    def spy(*args, **kwargs):
        captured["trust_env"] = kwargs.get("trust_env")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "Client", spy)
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        _httpx.MockTransport(lambda req: _httpx.Response(201, json={"bundles": [wire(bundle())]})),
    )
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", "1", "--output", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert captured["trust_env"] is False


def test_legacy_command_keeps_proxy_client(runner, env, monkeypatch):
    """Existing admin commands keep their trust_env proxy behaviour."""
    import httpx as _httpx

    captured: dict[str, object] = {}
    real_client = _httpx.Client

    def spy(*args, **kwargs):
        captured["trust_env"] = kwargs.get("trust_env")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "Client", spy)
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        _httpx.MockTransport(lambda req: _httpx.Response(200, json={"token": "t"})),
    )
    result = runner.invoke(
        main.app, ["--coordinator-url", COORD_URL, "enroll", "new-token"], env=env
    )
    assert result.exit_code == 0
    assert captured["trust_env"] is True


def test_short_bundle_response_fails_loudly(runner, env, monkeypatch, tmp_path):
    """A coordinator returning fewer bundles than requested is a hard error."""
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        __import__("httpx").MockTransport(
            lambda req: __import__("httpx").Response(201, json={"bundles": [wire(bundle())]})
        ),
    )
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", "3", "--output", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert not (tmp_path / "desk-01.fallow-join").exists()


def test_metadata_carries_origins_without_secrets(tmp_path):
    out = tmp_path / "join"
    meta = write_join_bundles((bundle("hidden"),), out, force=False)
    assert meta[0]["coordinator_urls"] == ["https://coord.example:8330"]
    assert meta[0]["pin_prefix"] == ("sha256/" + base64.b64encode(b"p" * 32).decode())[:16]
    assert "hidden" not in json.dumps(meta)


def test_exact_bundle_bytes_written(tmp_path):
    out = tmp_path / "join"
    b = bundle()
    write_join_bundles((b,), out, force=False)
    written = (out / "desk-01.fallow-join").read_bytes()
    expected = json.dumps(b.model_dump(mode="json"), separators=(",", ":")).encode() + b"\n"
    assert written == expected


def test_human_output_lists_origins_and_redacts(runner, env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "_ADMIN_TRANSPORT",
        __import__("httpx").MockTransport(
            lambda req: __import__("httpx").Response(201, json={"bundles": [wire(bundle("LEAK"))]})
        ),
    )
    result = invoke(
        runner, env, ["site", "join-bundles", "--count", "1", "--output", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "origins=https://coord.example:8330" in result.stdout
    assert "LEAK" not in result.stdout and "LEAK" not in result.stderr


def test_initial_write_failure_leaves_no_partial_files(tmp_path, monkeypatch):
    out = tmp_path / "join"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(CliError):
        write_join_bundles((bundle(), bundle("two")), out, force=False)
    assert not (out / "desk-01.fallow-join").exists()
    assert not (out / "desk-02.fallow-join").exists()
    # No stray temporary or backup files linger.
    assert list(out.iterdir()) == []


def test_force_rollback_restores_prior_files(tmp_path, monkeypatch):
    out = tmp_path / "join"
    write_join_bundles((bundle("old-a"), bundle("old-b")), out, force=False)
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        # Let the first overwrite through, fail the second; the rollback
        # os.replace that restores desk-01 must still succeed.
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(CliError):
        write_join_bundles((bundle("new-a"), bundle("new-b")), out, force=True)
    # Both original files are intact after rollback.
    a = json.loads((out / "desk-01.fallow-join").read_text())
    b = json.loads((out / "desk-02.fallow-join").read_text())
    assert a["enrollment_token"] == "old-a"
    assert b["enrollment_token"] == "old-b"
    # Only the two join files remain; no temp/backup residue.
    assert sorted(p.name for p in out.iterdir()) == [
        "desk-01.fallow-join",
        "desk-02.fallow-join",
    ]


def test_directory_creation_failure_is_friendly(tmp_path, monkeypatch):
    out = tmp_path / "join"

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    with pytest.raises(CliError) as excinfo:
        write_join_bundles((bundle(),), out, force=False)
    assert "could not create join file directory" in str(excinfo.value)
