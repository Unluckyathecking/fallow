"""``WorkLoop``: long-poll for batch work and run it while the machine is IDLE.

While IDLE it long-polls the coordinator for a lease, runs it through the
:class:`WorkUnitRunner`, and reports the result. While ACTIVE it does no work at
all — it sleeps cheaply and re-checks — so the user's machine is never touched.

In-flight yield (ADR 015, honestly documented): if the user returns mid-unit,
the preemptor suspends the local replica and the worker's HTTP call to it
stalls. The loop caps each unit with an ``asyncio.timeout`` sized to the lease's
remaining slack; on timeout it reports *nothing* and moves on — the coordinator's
lease expiry requeues the unit elsewhere. Double-running is harmless because work
units are content-addressed and completions dedup (ADR 005).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fallow_agent.heartbeat import CoordinatorClient, CoordinatorError
from fallow_agent.main.protocols import PreemptorLike
from fallow_agent.main.shared import LeaseRegistry
from fallow_agent.workers import DeferredWorkResult, WorkUnitRunner
from fallow_protocol.messages import AgentState, WorkUnitLease

logger = logging.getLogger(__name__)

NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]

# Floor on per-unit slack: never grant a unit less than this even if the lease
# is already near expiry (it will simply time out and requeue).
_MIN_SLACK_S = 0.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkLoop:
    """IDLE-gated batch-work poller/executor driven by an asyncio task."""

    def __init__(
        self,
        *,
        client: CoordinatorClient,
        runner: WorkUnitRunner,
        preemptor: PreemptorLike,
        leases: LeaseRegistry,
        poll_timeout_s: float,
        active_sleep_s: float,
        now: NowFn = _utc_now,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._client = client
        self._runner = runner
        self._preemptor = preemptor
        self._leases = leases
        self._poll_timeout_s = poll_timeout_s
        self._active_sleep_s = active_sleep_s
        self._now = now
        self._sleep = sleep
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="fallow-work")

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
            await self.tick()

    # ── One iteration ────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """Poll once (when IDLE) or sleep cheaply (when not). Never raises."""
        if self._preemptor.state is not AgentState.IDLE:
            await self._sleep(self._active_sleep_s)
            return
        lease = await self._poll()
        if lease is not None:
            await self._process(lease)

    async def _poll(self) -> WorkUnitLease | None:
        try:
            return await self._client.poll_work(self._poll_timeout_s)
        except CoordinatorError as exc:
            logger.warning("work poll failed: %s", exc)
            await self._sleep(self._active_sleep_s)
            return None

    async def _process(self, lease: WorkUnitLease) -> None:
        self._leases.set(lease.work_unit_id)
        try:
            await self._run_within_slack(lease)
        except TimeoutError:
            logger.warning(
                "unit %s exceeded lease slack; reporting nothing (requeues elsewhere)",
                lease.work_unit_id,
            )
        except CoordinatorError as exc:
            logger.warning("unit %s completion failed: %s", lease.work_unit_id, exc)
        finally:
            self._leases.clear()

    async def _run_within_slack(self, lease: WorkUnitLease) -> None:
        slack = max(_MIN_SLACK_S, (lease.lease_expires - self._now()).total_seconds())
        async with asyncio.timeout(slack):
            result = await self._runner.run_lease(lease)
            if isinstance(result, DeferredWorkResult):
                logger.warning(
                    "unit %s payload retained at %s; reporting nothing for lease-expiry retry",
                    lease.work_unit_id,
                    result.payload_path,
                )
                return
            await self._client.complete_unit(result, lease_attempt=lease.attempt)
