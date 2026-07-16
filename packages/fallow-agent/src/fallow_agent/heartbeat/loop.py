"""``HeartbeatLoop``: the agent's periodic liveness + telemetry beacon.

Every ``interval_s`` it assembles a :class:`Heartbeat` from injected sources
(state from a :class:`Preemptor`, replicas from a :class:`ProcessSupervisor`,
idle seconds from an :class:`IdleDetector`, lease ids from a callable, host
metrics from a callable) and sends it via :class:`CoordinatorClient`.

Failure policy (see ADR 009): the loop must be un-killable by anything short of
an auth rejection. Transient/protocol failures are logged and the loop keeps
beating; an unexpected exception is logged and swallowed. Only
:class:`CoordinatorAuthError` stops it — surfaced via ``on_auth_error``.
Reconciliation of each :class:`HeartbeatResponse` (desired models, revoked
leases, config) is *not* this module's job: it is handed to ``on_response`` and
acted on by wave-3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fallow_agent.heartbeat.client import CoordinatorClient
from fallow_agent.heartbeat.errors import CoordinatorAuthError, CoordinatorError
from fallow_agent.heartbeat.metrics import HeartbeatMetrics
from fallow_agent.idle import IdlePrediction
from fallow_protocol.interfaces import IdleDetector, Preemptor, ProcessSupervisor
from fallow_protocol.messages import Heartbeat, HeartbeatResponse

logger = logging.getLogger(__name__)

LeaseIdsFn = Callable[[], tuple[str, ...]]
MetricsFn = Callable[[], HeartbeatMetrics]
NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]
OnResponse = Callable[[HeartbeatResponse], None]
OnAuthError = Callable[[CoordinatorAuthError], None]
ServingPausedFn = Callable[[], bool]
PredictFn = Callable[[], IdlePrediction]


def _never_paused() -> bool:
    return False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HeartbeatLoop:
    """Periodic heartbeat sender driven by an asyncio task."""

    def __init__(
        self,
        *,
        client: CoordinatorClient,
        agent_id: str,
        protocol_version: int,
        interval_s: float,
        preemptor: Preemptor,
        supervisor: ProcessSupervisor,
        idle: IdleDetector,
        lease_ids: LeaseIdsFn,
        metrics: MetricsFn,
        on_response: OnResponse,
        on_auth_error: OnAuthError,
        now: NowFn = _utc_now,
        sleep: SleepFn = asyncio.sleep,
        serving_paused: ServingPausedFn = _never_paused,
        predict: PredictFn | None = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._protocol_version = protocol_version
        self._interval_s = interval_s
        self._preemptor = preemptor
        self._supervisor = supervisor
        self._idle = idle
        self._lease_ids = lease_ids
        self._metrics = metrics
        self._on_response = on_response
        self._on_auth_error = on_auth_error
        self._now = now
        self._sleep = sleep
        self._serving_paused = serving_paused
        self._predict = predict
        self._seq = 0
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def seq(self) -> int:
        return self._seq

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="fallow-heartbeat")

    def request_stop(self) -> None:
        """Ask the loop to exit after the current iteration (non-async)."""
        self._running = False

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        """Beat until stopped or an auth rejection is surfaced."""
        self._running = True
        while self._running:
            if not await self._tick():
                self._running = False
                return
            await self._sleep(self._interval_s)

    # ── One iteration ────────────────────────────────────────────────────────

    async def _tick(self) -> bool:
        """Send one heartbeat. Returns False iff the loop must stop (auth)."""
        keep_going = True
        try:
            response = await self._client.heartbeat(self._build_heartbeat())
            self._on_response(response)
        except CoordinatorAuthError as exc:
            logger.error("heartbeat auth rejected; stopping loop: %s", exc)
            self._on_auth_error(exc)
            keep_going = False
        except CoordinatorError as exc:
            logger.warning("heartbeat failed (transient/protocol): %s", exc)
        except Exception:
            logger.exception("unexpected heartbeat error; loop continues")
        finally:
            self._seq += 1
        return keep_going

    def _build_heartbeat(self) -> Heartbeat:
        metrics = self._metrics()
        prediction = self._predict() if self._predict is not None else None
        return Heartbeat(
            agent_id=self._agent_id,
            seq=self._seq,
            sent_at=self._now(),
            protocol_version=self._protocol_version,
            state=self._preemptor.state,
            user_idle_s=max(0.0, self._idle.seconds_since_input()),
            cpu_percent=metrics.cpu_percent,
            mem_available_mb=metrics.mem_available_mb,
            load_avg=metrics.load_avg,
            temp_cpu_c=metrics.temp_cpu_c,
            gpus=metrics.gpus,
            replicas=self._supervisor.statuses(),
            lease_ids=self._lease_ids(),
            serving_paused=self._serving_paused(),
            predicted_idle_remaining_s=(prediction.remaining_s if prediction is not None else None),
            predicted_idle_confidence=(prediction.confidence if prediction is not None else None),
        )
