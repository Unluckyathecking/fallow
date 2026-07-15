"""Every admin route matches the ``docs/admin-api.md`` shape (all 8) + auth."""

from __future__ import annotations

from pathlib import Path

from app_helpers import (
    MODEL_ID,
    admin_headers,
    enrolled_idle_agent,
    make_manifest,
    send_heartbeat,
)
from conftest import Harness

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobState, JobStatus, JobSubmit
from fallow_protocol.models import ModelManifest


def _write_embed_corpus(tmp: Path, n: int) -> Path:
    path = tmp / "corpus.jsonl"
    lines = [f'{{"id": "{i}", "text": "doc number {i}"}}' for i in range(n)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


async def test_enrollment_tokens_shape(harness: Harness) -> None:
    resp = await harness.client.post("/v1/admin/enrollment_tokens", headers=admin_headers())
    assert resp.status_code == 201
    assert isinstance(resp.json()["token"], str)


async def test_api_keys_shape(harness: Harness) -> None:
    resp = await harness.client.post(
        "/v1/admin/api_keys",
        json={"name": "team-a", "model_allowlist": [MODEL_ID]},
        headers=admin_headers(),
    )
    assert resp.status_code == 201
    assert isinstance(resp.json()["key"], str)

    # `model_allowlist` omitted (CLI sends exclude_none) means "all models".
    resp2 = await harness.client.post(
        "/v1/admin/api_keys", json={"name": "all-access"}, headers=admin_headers()
    )
    assert resp2.status_code == 201
    assert isinstance(resp2.json()["key"], str)


async def test_agents_shape(harness: Harness) -> None:
    resp = await harness.client.get("/v1/admin/agents", headers=admin_headers())
    assert resp.status_code == 200
    assert resp.json() == []


async def test_models_list_and_register(harness: Harness, tmp_path: Path) -> None:
    empty = await harness.client.get("/v1/admin/models", headers=admin_headers())
    assert empty.status_code == 200
    assert empty.json() == []

    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes")
    manifest = make_manifest()
    resp = await harness.client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": str(blob)},
        headers=admin_headers(),
    )
    assert resp.status_code == 201

    listed = await harness.client.get("/v1/admin/models", headers=admin_headers())
    models = [ModelManifest.model_validate(m) for m in listed.json()]
    assert [m.model_id for m in models] == [MODEL_ID]


async def test_register_model_missing_blob_is_422(harness: Harness) -> None:
    manifest = make_manifest()
    resp = await harness.client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": "/no/such/blob.gguf"},
        headers=admin_headers(),
    )
    assert resp.status_code == 422


async def test_assignments_replace(harness: Harness) -> None:
    agent_id, device_token = await enrolled_idle_agent(harness.client)
    resp = await harness.client.put(
        "/v1/admin/assignments",
        json={"model_id": MODEL_ID, "agent_ids": [agent_id]},
        headers=admin_headers(),
    )
    assert resp.status_code == 204

    # The assignment drives the agent's desired_models on its next heartbeat.
    hb = await send_heartbeat(harness.client, agent_id, device_token)
    assert hb.status_code == 200
    assert hb.json()["desired_models"] == [MODEL_ID]

    # Idempotent replace: assigning the empty set removes it again.
    cleared = await harness.client.put(
        "/v1/admin/assignments",
        json={"model_id": MODEL_ID, "agent_ids": []},
        headers=admin_headers(),
    )
    assert cleared.status_code == 204
    hb2 = await send_heartbeat(harness.client, agent_id, device_token)
    assert hb2.json()["desired_models"] == []


async def test_jobs_submit_and_status(harness: Harness, tmp_path: Path) -> None:
    corpus = _write_embed_corpus(tmp_path, n=3)
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref=str(corpus))
    resp = await harness.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 201
    status = JobStatus.model_validate(resp.json())
    assert status.state in (JobState.PENDING, JobState.RUNNING)
    assert status.total_units == 1  # 3 texts, default chunks_per_unit=32 → one unit

    got = await harness.client.get(f"/v1/admin/jobs/{status.job_id}", headers=admin_headers())
    assert got.status_code == 200
    assert JobStatus.model_validate(got.json()).job_id == status.job_id


async def test_unknown_job_is_404(harness: Harness) -> None:
    resp = await harness.client.get("/v1/admin/jobs/does-not-exist", headers=admin_headers())
    assert resp.status_code == 404


async def test_admin_auth_rejected(harness: Harness) -> None:
    resp = await harness.client.get("/v1/admin/agents", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    missing = await harness.client.get("/v1/admin/agents")
    assert missing.status_code == 401
