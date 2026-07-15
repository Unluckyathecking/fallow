"""OpenAI-compatible inference gateway (module C5).

Public API: :func:`create_gateway_router`, the ASGI router that authenticates
API keys, resolves a replica via an injected ``pick_replica`` policy, and proxies
``/v1/chat/completions`` and ``/v1/embeddings`` verbatim to llama-server (raw SSE
passthrough for streaming), plus the request-log types and the timeout config.
"""

from fallow_coordinator.gateway.affinity import AffinityMap
from fallow_coordinator.gateway.config import GatewayConfig
from fallow_coordinator.gateway.inflight import InflightTracker
from fallow_coordinator.gateway.jsonl_log import JsonlRequestLog
from fallow_coordinator.gateway.logentry import AffinityState, GatewayLogEntry, LogStatus
from fallow_coordinator.gateway.protocols import GatewayRegistry, PickReplica, RequestLog
from fallow_coordinator.gateway.router import create_gateway_router

__all__ = [
    "AffinityMap",
    "AffinityState",
    "GatewayConfig",
    "GatewayLogEntry",
    "GatewayRegistry",
    "InflightTracker",
    "JsonlRequestLog",
    "LogStatus",
    "PickReplica",
    "RequestLog",
    "create_gateway_router",
]
