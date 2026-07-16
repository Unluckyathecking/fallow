"""Prometheus metrics derived from coordinator snapshots and request logs."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState

_GATEWAY_STATUSES = ("served", "shed", "error")
_KNOWN_GATEWAY_STATUSES = (*_GATEWAY_STATUSES, "cancelled")
_REPLICA_STATES = (ReplicaState.READY, ReplicaState.STOPPED)

InflightCounts = Mapping[tuple[str, int], int]
GetInflight = Callable[[], InflightCounts]


@dataclass(frozen=True)
class GatewayCounters:
    """Terminal gateway-request counts read from the append-only audit log."""

    served: int = 0
    shed: int = 0
    error: int = 0
    retried: int = 0


def read_gateway_counters(path: Path) -> GatewayCounters:
    """Read relevant counters without changing the gateway audit log."""
    counts: Counter[str] = Counter()
    if not path.exists():
        return GatewayCounters()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = _decode_entry(line)
        if entry is None:
            continue
        status = entry.get("status")
        if status not in _KNOWN_GATEWAY_STATUSES:
            continue
        if status in _GATEWAY_STATUSES:
            counts[str(status)] += 1
        if entry.get("retried") is True:
            counts["retried"] += 1
    return GatewayCounters(
        served=counts["served"],
        shed=counts["shed"],
        error=counts["error"],
        retried=counts["retried"],
    )


def format_metrics(
    snapshots: Iterable[AgentSnapshot],
    gateway: GatewayCounters,
    local_inflight: InflightCounts | None = None,
) -> str:
    """Format one coordinator view using Prometheus text exposition 0.0.4."""
    agents = tuple(snapshots)
    agent_counts = _agent_counts(agents)
    replica_counts, model_ids = _replica_counts(agents)
    lines = [
        "# HELP fallow_agents_total Number of online agents by state.",
        "# TYPE fallow_agents_total gauge",
        f"fallow_agents_total {agent_counts['total']}",
    ]
    lines.extend(
        f'fallow_agents_total{{state="{state}"}} {agent_counts[state]}' for state in AgentState
    )
    lines.extend(
        [
            "# HELP fallow_agents_suspect_total Number of online agents with stale heartbeats.",
            "# TYPE fallow_agents_suspect_total gauge",
            f"fallow_agents_suspect_total {agent_counts['suspect']}",
        ]
    )
    lines.extend(
        [
            "# HELP fallow_replicas_total Number of model replicas by state.",
            "# TYPE fallow_replicas_total gauge",
        ]
    )
    for model_id in model_ids:
        label = _escape_label(model_id)
        for state in _REPLICA_STATES:
            count = replica_counts[(model_id, state)]
            lines.append(
                f'fallow_replicas_total{{model_id="{label}",state="{state.value}"}} {count}'
            )
    lines.extend(_gateway_lines(gateway))
    lines.extend(
        [
            "# HELP fallow_inflight_total Requests currently in flight across replicas.",
            "# TYPE fallow_inflight_total gauge",
            f"fallow_inflight_total {_inflight_total(agents, local_inflight or {})}",
        ]
    )
    return "\n".join(lines) + "\n"


def _decode_entry(line: str) -> Mapping[str, object] | None:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _agent_counts(agents: tuple[AgentSnapshot, ...]) -> Counter[str]:
    counts: Counter[str] = Counter(total=len(agents))
    for state in AgentState:
        counts[state] = sum(agent.state == state for agent in agents)
    counts["suspect"] = sum(agent.suspect for agent in agents)
    return counts


def _replica_counts(
    agents: tuple[AgentSnapshot, ...],
) -> tuple[Counter[tuple[str, ReplicaState]], tuple[str, ...]]:
    counts: Counter[tuple[str, ReplicaState]] = Counter()
    model_ids: set[str] = set()
    for agent in agents:
        for replica in agent.replicas:
            model_ids.add(replica.model_id)
            if replica.state in _REPLICA_STATES:
                counts[(replica.model_id, replica.state)] += 1
    return counts, tuple(sorted(model_ids))


def _gateway_lines(gateway: GatewayCounters) -> list[str]:
    lines = [
        "# HELP fallow_gateway_requests_total Gateway requests by terminal status.",
        "# TYPE fallow_gateway_requests_total counter",
    ]
    for status in _GATEWAY_STATUSES:
        lines.append(
            f'fallow_gateway_requests_total{{status="{status}"}} {getattr(gateway, status)}'
        )
    lines.extend(
        [
            "# HELP fallow_gateway_retried_total Gateway requests retried before completion.",
            "# TYPE fallow_gateway_retried_total counter",
            f"fallow_gateway_retried_total {gateway.retried}",
        ]
    )
    return lines


def _inflight_total(agents: tuple[AgentSnapshot, ...], local_inflight: InflightCounts) -> int:
    return sum(
        max(replica.inflight, local_inflight.get((agent.host, replica.port), 0))
        for agent in agents
        for replica in agent.replicas
    )


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
