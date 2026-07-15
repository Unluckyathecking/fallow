"""Work long-poll: 204 when idle-but-no-work, and 204 when the user has returned."""

from __future__ import annotations

from pathlib import Path

from app_helpers import (
    MODEL_ID,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_replica,
    make_user_returned,
)
from conftest import Harness

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobSubmit


async def _submit_one_unit_job(harness: Harness, tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id": "0", "text": "only chunk"}', encoding="utf-8")
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref=str(corpus))
    resp = await harness.client.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 201


async def test_long_poll_204_when_no_work(harness: Harness) -> None:
    agent_id, token = await enrolled_idle_agent(harness.client, replicas=(make_replica(),))
    resp = await harness.client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )
    assert resp.status_code == 204


async def test_long_poll_204_when_user_returned(harness: Harness, tmp_path: Path) -> None:
    # An idle agent with a READY replica AND a pending unit *would* lease — but a
    # user_returned event flips it ACTIVE, so routing sheds it to 204 immediately.
    agent_id, token = await enrolled_idle_agent(harness.client, replicas=(make_replica(),))
    await _submit_one_unit_job(harness, tmp_path)

    event = make_user_returned(agent_id)
    posted = await harness.client.post(
        f"/v1/agents/{agent_id}/events", json=event.model_dump(mode="json"), headers=bearer(token)
    )
    assert posted.status_code == 202

    resp = await harness.client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )
    assert resp.status_code == 204


async def test_events_appended_to_jsonl(harness: Harness) -> None:
    agent_id, token = await enrolled_idle_agent(harness.client)
    event = make_user_returned(agent_id)
    resp = await harness.client.post(
        f"/v1/agents/{agent_id}/events", json=event.model_dump(mode="json"), headers=bearer(token)
    )
    assert resp.status_code == 202
    lines = harness.config.events_jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert agent_id in lines[0]


async def test_active_agent_without_event_still_leases(harness: Harness, tmp_path: Path) -> None:
    # Sanity: the same setup WITHOUT the user_returned event does lease (200),
    # proving the 204 above is caused by the event, not a missing unit/replica.
    agent_id, token = await enrolled_idle_agent(harness.client, replicas=(make_replica(),))
    await _submit_one_unit_job(harness, tmp_path)
    resp = await harness.client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )
    assert resp.status_code == 200
