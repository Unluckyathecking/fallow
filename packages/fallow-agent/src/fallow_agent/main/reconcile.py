"""``ReconcileLoop``: drive local replicas toward the coordinator's wishes.

Every ``interval_s`` it compares ``desired_models`` (threaded in from the latest
heartbeat response) against the supervisor's live replicas and closes the gap:
start missing/stopped desired models, stop running-but-undesired ones. It only
ever acts while the preemptor is IDLE — the moment the user is present, every
fallow child is suspended and reconciliation defers. Replicas killed by VRAM
escalation reappear as STOPPED here and restart naturally once IDLE again.

Every per-model action is wrapped: a failed manifest fetch, download, or spawn
is logged and skipped, never fatal to the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from fallow_agent.main.ports import PortAllocator
from fallow_agent.main.protocols import PreemptorLike, SupervisorLike
from fallow_protocol.interfaces import ModelStore
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ModelManifest, ReplicaState

logger = logging.getLogger(__name__)

FetchManifest = Callable[[str], Awaitable[ModelManifest]]
DesiredFn = Callable[[], tuple[str, ...]]
SleepFn = Callable[[float], Awaitable[None]]
ReclaimedFn = Callable[[], bool]


def _never_reclaimed() -> bool:
    return False


# A replica occupying its port/model slot (anything but a dead STOPPED shell).
_LIVE_STATES = frozenset({ReplicaState.LOADING, ReplicaState.READY, ReplicaState.SUSPENDED})


class ReconcileLoop:
    """Periodic desired-vs-current replica reconciler (IDLE-gated)."""

    def __init__(
        self,
        *,
        supervisor: SupervisorLike,
        modelstore: ModelStore,
        fetch_manifest: FetchManifest,
        preemptor: PreemptorLike,
        ports: PortAllocator,
        desired: DesiredFn,
        interval_s: float,
        sleep: SleepFn = asyncio.sleep,
        reclaimed: ReclaimedFn = _never_reclaimed,
    ) -> None:
        self._supervisor = supervisor
        self._modelstore = modelstore
        self._fetch_manifest = fetch_manifest
        self._preemptor = preemptor
        self._ports = ports
        self._desired = desired
        self._interval_s = interval_s
        self._sleep = sleep
        self._reclaimed = reclaimed
        self._ports_by_model: dict[str, int] = {}
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="fallow-reconcile")

    def request_stop(self) -> None:
        self._running = False

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            await self.reconcile_once()
            await self._sleep(self._interval_s)

    # ── One reconciliation pass ──────────────────────────────────────────────

    async def reconcile_once(self) -> None:
        """Perform one desired-vs-current pass. Never raises."""
        if self._reclaimed():
            return  # user reclaimed the machine: stay down until release
        if self._preemptor.state is not AgentState.IDLE:
            return
        desired = set(self._desired())
        live = {s.model_id for s in self._supervisor.statuses() if s.state in _LIVE_STATES}
        for model_id in desired - live:
            await self._start_model(model_id)
        for model_id in live - desired:
            self._stop_model(model_id)

    async def _start_model(self, model_id: str) -> None:
        try:
            manifest = await self._fetch_manifest(model_id)
            path = await self._modelstore.ensure(manifest)
            port = self._reserve_port(model_id)
            self._supervisor.start_replica(manifest, path, port)
        except Exception:
            logger.exception("reconcile: failed to start replica %s", model_id)

    def _stop_model(self, model_id: str) -> None:
        try:
            self._supervisor.stop_replica(model_id)
        except Exception:
            logger.exception("reconcile: failed to stop replica %s", model_id)
        finally:
            self._free_port(model_id)

    def _reserve_port(self, model_id: str) -> int:
        # Release any stale port from a prior (now STOPPED) incarnation first so
        # an escalation-killed replica does not leak its port on restart.
        self._free_port(model_id)
        port = self._ports.allocate()
        self._ports_by_model[model_id] = port
        return port

    def _free_port(self, model_id: str) -> None:
        port = self._ports_by_model.pop(model_id, None)
        if port is not None:
            self._ports.release(port)
