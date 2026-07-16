"""Speculative backup dispatch, end to end over the agent poll route (ADR 056).

Proves the feature flag gates the whole path: with it on, an idle agent that
finds no pending work is handed a backup copy of an at-risk tail unit; with it
off, the same poll is a plain 204. The holder is unseen by the (empty) churn
model, so its survival is the optimistic prior — below a deliberately high
threshold here, which is what makes it "at risk".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from app_helpers import ADMIN_KEY, MODEL_ID, FakeClock, bearer, enrolled_idle_agent, make_replica
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.app.state import CoordinatorState
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobSubmit, WorkUnitSpec

TAIL_UNIT_ID = "unit-tail"


def _config(tmp_path: Path, *, enabled: bool) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        requeue_interval_s=3600.0,
        poll_sleep_s=0.01,
        admission_timeout_s=0,
        speculative_backup_enabled=enabled,
        # High threshold: the unseen holder's prior survival (0.9) falls below it,
        # so the tail unit reads as at-risk without seeding churn history.
        speculative_survival_threshold=0.95,
        speculative_tail_max_units=2,
    )


@asynccontextmanager
async def _app(
    tmp_path: Path, *, enabled: bool
) -> AsyncIterator[tuple[httpx.AsyncClient, CoordinatorState]]:
    app = create_app(_config(tmp_path, enabled=enabled), now=FakeClock(), sleep=asyncio.sleep)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord")
        try:
            yield client, app.state.coordinator
        finally:
            await client.aclose()


async def _submit_one_unit(state: CoordinatorState) -> None:
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=MODEL_ID, payload_ref="corpus://tail")
    unit = WorkUnitSpec(work_unit_id=TAIL_UNIT_ID, idx=0, input_ref=f"input://{TAIL_UNIT_ID}")
    await state.queue.submit_job(job, [unit])


async def _poll_work(client: httpx.AsyncClient, agent_id: str, token: str) -> httpx.Response:
    return await client.get(
        f"/v1/agents/{agent_id}/work", params={"timeout": 0}, headers=bearer(token)
    )


async def test_enabled_offers_a_backup_of_the_tail_unit(tmp_path: Path) -> None:
    async with _app(tmp_path, enabled=True) as (client, state):
        await _submit_one_unit(state)
        replicas = (make_replica(),)
        holder, holder_token = await enrolled_idle_agent(client, replicas=replicas)
        helper, helper_token = await enrolled_idle_agent(client, replicas=replicas)

        # The holder leases the only unit — the job is now at its tail.
        held = await _poll_work(client, holder, holder_token)
        assert held.status_code == 200, held.text
        assert held.json()["work_unit_id"] == TAIL_UNIT_ID

        # No pending work is left, so the second idle agent is offered a backup.
        backup = await _poll_work(client, helper, helper_token)
        assert backup.status_code == 200, backup.text
        body = backup.json()
        assert body["work_unit_id"] == TAIL_UNIT_ID
        assert body["attempt"] == 5  # reserved backup attempt (max_attempts=4 + 1)


async def test_disabled_leaves_the_poll_at_204(tmp_path: Path) -> None:
    async with _app(tmp_path, enabled=False) as (client, state):
        await _submit_one_unit(state)
        replicas = (make_replica(),)
        holder, holder_token = await enrolled_idle_agent(client, replicas=replicas)
        helper, helper_token = await enrolled_idle_agent(client, replicas=replicas)

        held = await _poll_work(client, holder, holder_token)
        assert held.status_code == 200, held.text

        # Feature off: no speculation, so the second agent just gets nothing to do.
        backup = await _poll_work(client, helper, helper_token)
        assert backup.status_code == 204, backup.text
