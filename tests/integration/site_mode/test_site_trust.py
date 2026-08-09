"""Site Mode trust-boundary rejections: prove the pilot fails closed.

These cover the acceptance contract's "Network and trust" clauses that keep the
static path honest — HTTPS only, an exact non-wildcard bind, a mandatory correct
pin that leaks no token, ignored proxy settings, and a loopback-only replica bind.
Config-shaped rules are asserted at the real validators (coordinator config and
the Go agent's own ``doctor``); the pin and proxy rules run the real daemon.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from site_mode.site_harness import (
    SiteCoordinator,
    corrupt_join_pin,
    create_api_key,
    doctor,
    list_agents,
    llama_command,
    make_site_config,
    mint_join_bundle_via_flw,
    register_chat_model,
    run_site_daemon,
    wait_process_exit,
    write_agent_toml,
    write_tls_cert,
)

# ── coordinator config: HTTPS only, exact non-wildcard bind ──────────────────


def test_coordinator_rejects_cleartext_public_url(tmp_path: Path) -> None:
    certfile, keyfile = write_tls_cert(tmp_path)
    with pytest.raises(ValidationError, match="HTTPS root origins"):
        make_site_config(
            tmp_path,
            8443,
            certfile,
            keyfile,
            site={
                "enabled": True,
                "site_id": "school-pilot",
                "public_urls": ("http://desk.example:8443",),  # cleartext
                "tls_certfile": certfile,
                "tls_keyfile": keyfile,
            },
        )


def test_coordinator_rejects_wildcard_bind(tmp_path: Path) -> None:
    certfile, keyfile = write_tls_cert(tmp_path)
    with pytest.raises(ValidationError, match="non-wildcard bind"):
        make_site_config(tmp_path, 8443, certfile, keyfile, host="0.0.0.0")


def test_coordinator_rejects_missing_tls(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="TLS"):
        make_site_config(
            tmp_path,
            8443,
            tmp_path / "none",
            tmp_path / "none",
            site={
                "enabled": True,
                "site_id": "school-pilot",
                "public_urls": ("https://desk.example:8443",),
                "tls_certfile": None,
                "tls_keyfile": None,
            },
        )


# ── agent config: a Site replica binds loopback only ─────────────────────────


async def test_agent_doctor_rejects_non_loopback_site_bind(
    site_binary: Path, tmp_path: Path
) -> None:
    """A Site Mode agent asked to bind a LAN address fails its own config check."""
    join = tmp_path / "desk.fallow-join"
    join.write_text(
        json.dumps(
            {
                "version": 1,
                "site_id": "school-pilot",
                "coordinator_urls": ["https://desk.example:8443"],
                "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
                "enrollment_token": "tok",
                "mdns_service": None,
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=tmp_path / "state.json",
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
        bind_host="0.0.0.0",  # would expose llama on the LAN
    )
    report = await doctor(site_binary, config)
    assert report.get("config", {}).get("ok") is False, report
    assert "loopback" in json.dumps(report).lower()


# ── a wrong pin fails closed and leaks no token ──────────────────────────────


async def test_wrong_pin_enrollment_fails_and_leaks_no_token(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    coord = coordinator
    await create_api_key(coord)  # ensure the coordinator is fully live
    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    token_before = json.loads(join.read_text())["enrollment_token"]
    corrupt_join_pin(join)  # replace the real pin with a valid-but-wrong one

    state = tmp_path / "state.json"
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )
    async with run_site_daemon(site_binary, config, state) as daemon:
        rc = await wait_process_exit(daemon, timeout=15.0)

    # Enrollment fails closed: non-zero exit, a pin error, no persisted identity.
    assert rc != 0
    assert "pin" in daemon.stderr.lower(), daemon.stderr
    assert not state.exists(), "a failed enrollment must persist no identity"
    # The pinned handshake is rejected before the request is sent, so no token,
    # bearer or body leaves: the coordinator registered no agent.
    assert await list_agents(coord) == []
    # The join file (still carrying its one-time token) was not consumed.
    assert join.exists() and json.loads(join.read_text())["enrollment_token"] == token_before


# ── proxy settings are ignored ───────────────────────────────────────────────


async def test_proxy_env_is_ignored_on_enrollment(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    """A poisoned proxy env must not divert the pinned client: enrollment still works."""
    coord = coordinator
    await register_chat_model(coord, _blob(tmp_path))
    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    state = tmp_path / "state.json"
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )
    black_hole = "http://127.0.0.1:9"  # a port nothing listens on
    poisoned = {
        "HTTP_PROXY": black_hole,
        "HTTPS_PROXY": black_hole,
        "ALL_PROXY": black_hole,
        "http_proxy": black_hole,
        "https_proxy": black_hole,
        "all_proxy": black_hole,
    }
    async with run_site_daemon(site_binary, config, state, env=poisoned) as daemon:
        agent_id = await _wait_one_agent(coord)
        assert agent_id, daemon.stderr
        rc = await daemon.stop()
    assert rc == 0, daemon.stderr


def _blob(tmp_path: Path) -> Path:
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    return blob


async def _wait_one_agent(coord: SiteCoordinator, *, timeout: float = 20.0) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        agents = await list_agents(coord)
        if agents:
            return str(agents[0]["agent_id"])
        await asyncio.sleep(0.1)
    return None
