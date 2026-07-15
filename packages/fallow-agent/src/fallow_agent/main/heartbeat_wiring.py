"""Heartbeat callbacks: response reconciliation and the final drain beat.

``make_on_response`` threads each heartbeat response's ``desired_models`` into
the shared holder the reconcile loop reads; a pushed config update is logged
(v0.1 applies config on the next restart, not hot — see ADR 015).

``make_final_heartbeat`` builds the single best-effort DRAINING beat sent during
graceful shutdown so the coordinator learns the agent is leaving before its
replicas stop and its leases are released.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fallow_agent.heartbeat import CoordinatorClient
from fallow_agent.main.protocols import PreemptorLike, SupervisorLike
from fallow_agent.main.seams import MetricsFn, NowFn
from fallow_agent.main.shared import DesiredModels, LeaseRegistry
from fallow_protocol.interfaces import IdleDetector
from fallow_protocol.messages import Heartbeat, HeartbeatResponse
from fallow_protocol.version import PROTOCOL_VERSION

logger = logging.getLogger(__name__)


def make_on_response(desired: DesiredModels) -> Callable[[HeartbeatResponse], None]:
    """Return the heartbeat ``on_response`` handler (updates desired models)."""

    def _on_response(response: HeartbeatResponse) -> None:
        desired.update(response.desired_models)
        if response.config is not None:
            logger.info("coordinator pushed a config update (applied on next restart)")

    return _on_response


def make_final_heartbeat(
    *,
    client: CoordinatorClient,
    agent_id: str,
    preemptor: PreemptorLike,
    supervisor: SupervisorLike,
    idle: IdleDetector,
    leases: LeaseRegistry,
    metrics: MetricsFn,
    now: NowFn,
) -> Callable[[], Awaitable[None]]:
    """Return a coroutine that sends one DRAINING heartbeat."""

    async def _final() -> None:
        sample = metrics()
        beat = Heartbeat(
            agent_id=agent_id,
            seq=0,
            sent_at=now(),
            protocol_version=PROTOCOL_VERSION,
            state=preemptor.state,
            user_idle_s=max(0.0, idle.seconds_since_input()),
            cpu_percent=sample.cpu_percent,
            mem_available_mb=sample.mem_available_mb,
            load_avg=sample.load_avg,
            temp_cpu_c=sample.temp_cpu_c,
            gpus=sample.gpus,
            replicas=supervisor.statuses(),
            lease_ids=leases.current(),
        )
        await client.heartbeat(beat)

    return _final
