"""Fleet-wide fit assignment: POST /v1/admin/assignments/fit places one model
on every live, unassigned agent that can hold it, and says why the rest were
skipped."""

from __future__ import annotations

from pathlib import Path

import httpx
from app_helpers import (
    Harness,
    admin_headers,
    enrolled_idle_agent,
    make_manifest,
    mint_enrollment_token,
    register_agent,
    send_heartbeat,
)

from fallow_protocol.models import ModelManifest

_FIT_PATH = "/v1/admin/assignments/fit"


async def _register_model(harness: Harness, tmp_path: Path, manifest: ModelManifest) -> None:
    blob = tmp_path / f"{manifest.model_id}.gguf"
    blob.write_bytes(b"fake-gguf-bytes")
    resp = await harness.client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": str(blob)},
        headers=admin_headers(),
    )
    assert resp.status_code == 201, resp.text


async def _fit(harness: Harness, model_id: str) -> httpx.Response:
    return await harness.client.post(
        _FIT_PATH, json={"model_id": model_id}, headers=admin_headers()
    )


async def _desired(harness: Harness, agent_id: str, token: str) -> list[str]:
    hb = await send_heartbeat(harness.client, agent_id, token)
    return list(hb.json()["desired_models"])


async def test_fit_assigns_every_unassigned_agent_that_fits(
    harness: Harness, tmp_path: Path
) -> None:
    manifest = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, manifest)
    agent_a, token_a = await enrolled_idle_agent(harness.client)
    agent_b, token_b = await enrolled_idle_agent(harness.client)

    resp = await _fit(harness, "vlm-small")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["assigned"]) == sorted([agent_a, agent_b])
    assert body["kept"] == []
    assert body["skipped"] == []
    assert await _desired(harness, agent_a, token_a) == ["vlm-small"]
    assert await _desired(harness, agent_b, token_b) == ["vlm-small"]


async def test_fit_skips_unfit_agent_with_reason(harness: Harness, tmp_path: Path) -> None:
    # Default enrolled agents report no GPU, so a VRAM-hungry model cannot fit.
    manifest = make_manifest("vlm-big").model_copy(update={"min_vram_mb": 8000})
    await _register_model(harness, tmp_path, manifest)
    agent_id, token = await enrolled_idle_agent(harness.client)

    resp = await _fit(harness, "vlm-big")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned"] == []
    (skip,) = body["skipped"]
    assert skip["agent_id"] == agent_id
    assert "8000" in skip["reason"]
    assert await _desired(harness, agent_id, token) == []


async def test_fit_reports_already_serving_agent_as_kept(harness: Harness, tmp_path: Path) -> None:
    manifest = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, manifest)
    agent_id, token = await enrolled_idle_agent(harness.client)
    first = await _fit(harness, "vlm-small")
    assert first.json()["assigned"] == [agent_id]

    second = await _fit(harness, "vlm-small")

    body = second.json()
    assert body["assigned"] == []
    assert body["kept"] == [agent_id]
    assert await _desired(harness, agent_id, token) == ["vlm-small"]


async def test_fit_never_touches_an_agent_already_assigned_another_model(
    harness: Harness, tmp_path: Path
) -> None:
    """Running --fit largest-first gives each agent its tier deterministically."""
    big = make_manifest("vlm-big").model_copy(update={"min_ram_mb": 1024})
    small = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, big)
    await _register_model(harness, tmp_path, small)
    agent_a, token_a = await enrolled_idle_agent(harness.client)
    assert (await _fit(harness, "vlm-big")).json()["assigned"] == [agent_a]
    agent_b, token_b = await enrolled_idle_agent(harness.client)

    resp = await _fit(harness, "vlm-small")

    body = resp.json()
    assert body["assigned"] == [agent_b]
    (skip,) = body["skipped"]
    assert skip["agent_id"] == agent_a
    assert "vlm-big" in skip["reason"]
    assert await _desired(harness, agent_a, token_a) == ["vlm-big"]
    assert await _desired(harness, agent_b, token_b) == ["vlm-small"]


async def test_fit_skips_agent_that_has_not_heartbeated_yet(
    harness: Harness, tmp_path: Path
) -> None:
    """A fresh registrant reports zero free capacity until its first heartbeat,
    so a sweep skips it (with the fit numbers) rather than assigning blind."""
    manifest = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, manifest)
    token = await mint_enrollment_token(harness.client)
    agent_id, _device_token = await register_agent(harness.client, token)

    resp = await _fit(harness, "vlm-small")

    body = resp.json()
    assert body["assigned"] == []
    (skip,) = body["skipped"]
    assert skip["agent_id"] == agent_id


async def test_fit_lists_offline_agents_and_leaves_them_alone(
    harness: Harness, tmp_path: Path
) -> None:
    manifest = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, manifest)
    agent_id, _token = await enrolled_idle_agent(harness.client)
    harness.clock.advance(3600.0)  # well past offline_after_s

    resp = await _fit(harness, "vlm-small")

    body = resp.json()
    assert body["assigned"] == []
    assert agent_id in body["offline"]


async def test_fit_omits_revoked_agents_from_offline(harness: Harness, tmp_path: Path) -> None:
    manifest = make_manifest("vlm-small").model_copy(update={"min_ram_mb": 1024})
    await _register_model(harness, tmp_path, manifest)
    agent_id, _token = await enrolled_idle_agent(harness.client)
    revoked = await harness.client.post(
        f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers()
    )
    assert revoked.status_code == 204, revoked.text
    harness.clock.advance(3600.0)

    body = (await _fit(harness, "vlm-small")).json()

    assert body["offline"] == []
    assert body["assigned"] == body["kept"] == body["skipped"] == []


async def test_fit_unknown_model_is_404(harness: Harness) -> None:
    resp = await _fit(harness, "ghost")
    assert resp.status_code == 404


async def test_fit_requires_admin_key(harness: Harness) -> None:
    resp = await harness.client.post(_FIT_PATH, json={"model_id": "vlm-small"})
    assert resp.status_code == 401
