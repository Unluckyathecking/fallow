"""ReconcileLoop: start missing, stop undesired, defer while ACTIVE."""

from __future__ import annotations

from pathlib import Path

from main_helpers import (
    FakeModelStore,
    FakePreemptor,
    FakeSupervisor,
    instant_sleep,
    manifest,
    status,
)

from fallow_agent.main.ports import PortAllocator
from fallow_agent.main.reconcile import ReconcileLoop
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ModelManifest, ReplicaState


def _loop(
    tmp_path: Path,
    *,
    supervisor: FakeSupervisor,
    preemptor: FakePreemptor,
    desired: tuple[str, ...],
    store: FakeModelStore | None = None,
) -> ReconcileLoop:
    async def _fetch(model_id: str) -> ModelManifest:
        return manifest(model_id)

    return ReconcileLoop(
        supervisor=supervisor,
        modelstore=store or FakeModelStore(tmp_path / "m1.gguf"),
        fetch_manifest=_fetch,
        preemptor=preemptor,
        ports=PortAllocator(8100, 4),
        desired=lambda: desired,
        interval_s=5.0,
        sleep=instant_sleep,
    )


async def test_starts_missing_desired_replica(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(statuses=())
    store = FakeModelStore(tmp_path / "m1.gguf")
    loop = _loop(
        tmp_path,
        supervisor=supervisor,
        preemptor=FakePreemptor(AgentState.IDLE),
        desired=("m1",),
        store=store,
    )
    await loop.reconcile_once()

    assert store.ensured == ["m1"]
    assert supervisor.started == [("m1", 8100)]  # allocated the low port


async def test_stops_undesired_running_replica(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(statuses=(status("m1", ReplicaState.READY),))
    loop = _loop(
        tmp_path,
        supervisor=supervisor,
        preemptor=FakePreemptor(AgentState.IDLE),
        desired=(),
    )
    await loop.reconcile_once()

    assert supervisor.stopped == ["m1"]
    assert supervisor.started == []


async def test_defers_while_active(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(statuses=())
    loop = _loop(
        tmp_path,
        supervisor=supervisor,
        preemptor=FakePreemptor(AgentState.ACTIVE),
        desired=("m1",),
    )
    await loop.reconcile_once()

    assert supervisor.started == []  # user present → no work done


async def test_stopped_desired_replica_is_restarted(tmp_path: Path) -> None:
    # A replica killed by escalation shows up STOPPED; desired ⇒ restart.
    supervisor = FakeSupervisor(statuses=(status("m1", ReplicaState.STOPPED),))
    loop = _loop(
        tmp_path,
        supervisor=supervisor,
        preemptor=FakePreemptor(AgentState.IDLE),
        desired=("m1",),
    )
    await loop.reconcile_once()

    assert supervisor.started == [("m1", 8100)]


async def test_defers_while_reclaimed(tmp_path: Path) -> None:
    # Reclaim is sticky even while IDLE: replicas must not relaunch until release.
    supervisor = FakeSupervisor(statuses=(status("m1", ReplicaState.STOPPED),))
    loop = ReconcileLoop(
        supervisor=supervisor,
        modelstore=FakeModelStore(tmp_path / "m1.gguf"),
        fetch_manifest=_immediate_manifest,
        preemptor=FakePreemptor(AgentState.IDLE),
        ports=PortAllocator(8100, 4),
        desired=lambda: ("m1",),
        interval_s=5.0,
        sleep=instant_sleep,
        reclaimed=lambda: True,
    )
    await loop.reconcile_once()

    assert supervisor.started == []  # machine reclaimed → nothing relaunches


async def _immediate_manifest(model_id: str) -> ModelManifest:
    return manifest(model_id)
