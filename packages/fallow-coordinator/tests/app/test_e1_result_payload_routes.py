"""Result payload upload and retrieval through the coordinator."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app_helpers import (
    MODEL_ID,
    Harness,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_replica,
)

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    JobSubmit,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)


async def _leased_embed_unit(harness: Harness, tmp_path: Path) -> tuple[str, str, WorkUnitLease]:
    agent_id, token = await enrolled_idle_agent(harness.client, replicas=(make_replica(),))
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id":"1","text":"hello"}\n', encoding="utf-8")
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref=str(corpus))
    submitted = await harness.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert submitted.status_code == 201, submitted.text
    leased = await harness.client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )
    assert leased.status_code == 200, leased.text
    return agent_id, token, WorkUnitLease.model_validate(leased.json())


async def test_payload_round_trips_only_after_accepted_completion(
    harness: Harness, tmp_path: Path
) -> None:
    agent_id, token, lease = await _leased_embed_unit(harness, tmp_path)
    payload = b'[{"id":"1","vector":[0.25,0.75]}]'
    digest = hashlib.sha256(payload).hexdigest()

    uploaded = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/payload",
        content=payload,
        headers={**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)},
    )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {"result_ref": digest}
    hidden = await harness.client.get(
        f"/v1/admin/work_units/{lease.work_unit_id}/payload", headers=admin_headers()
    )
    assert hidden.status_code == 404

    result = WorkResult(
        work_unit_id=lease.work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref=digest,
        metrics=WorkMetrics(duration_s=1.0, items=1),
    )
    completed = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
        json=result.model_dump(mode="json"),
        headers={**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)},
    )
    assert completed.status_code == 200, completed.text

    fetched = await harness.client.get(
        f"/v1/admin/work_units/{lease.work_unit_id}/payload", headers=admin_headers()
    )
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "application/octet-stream"
    assert fetched.content == payload


async def test_payload_upload_requires_the_current_lease(harness: Harness, tmp_path: Path) -> None:
    agent_id, token, lease = await _leased_embed_unit(harness, tmp_path)
    other_agent_id, other_token = await enrolled_idle_agent(harness.client)

    rejected = await harness.client.post(
        f"/v1/agents/{other_agent_id}/work_units/{lease.work_unit_id}/payload",
        content=b"not yours",
        headers={**bearer(other_token), "X-Fallow-Lease-Attempt": str(lease.attempt)},
    )

    assert rejected.status_code == 409
    assert not any(harness.config.result_dir.iterdir())
    assert agent_id != other_agent_id
    assert token != other_token


async def test_payload_upload_maps_size_limit_to_413(
    harness_small_payload: Harness, tmp_path: Path
) -> None:
    agent_id, token, lease = await _leased_embed_unit(harness_small_payload, tmp_path)

    rejected = await harness_small_payload.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/payload",
        content=b"123456",
        headers={**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)},
    )

    assert rejected.status_code == 413
    assert not any(harness_small_payload.config.result_dir.iterdir())


async def test_result_path_must_match_body(harness: Harness, tmp_path: Path) -> None:
    agent_id, token, lease = await _leased_embed_unit(harness, tmp_path)
    result = WorkResult(
        work_unit_id="different-unit",
        status=WorkResultStatus.FAILED,
        error="worker failed",
    )

    rejected = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
        json=result.model_dump(mode="json"),
        headers={**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)},
    )

    assert rejected.status_code == 409


async def test_success_result_must_match_the_attempt_payload_binding(
    harness: Harness, tmp_path: Path
) -> None:
    agent_id, token, lease = await _leased_embed_unit(harness, tmp_path)
    headers = {**bearer(token), "X-Fallow-Lease-Attempt": str(lease.attempt)}
    uploaded = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/payload",
        content=b"accepted bytes",
        headers=headers,
    )
    assert uploaded.status_code == 200
    forged = WorkResult(
        work_unit_id=lease.work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref="0" * 64,
    )

    rejected = await harness.client.post(
        f"/v1/agents/{agent_id}/work_units/{lease.work_unit_id}/result",
        json=forged.model_dump(mode="json"),
        headers=headers,
    )

    assert rejected.status_code == 409
    hidden = await harness.client.get(
        f"/v1/admin/work_units/{lease.work_unit_id}/payload", headers=admin_headers()
    )
    assert hidden.status_code == 404
