"""Wire the batch-work runner to the coordinator and to local replicas.

The workers module (A6) is pure — leases in, results out — with three injected
seams: how to resolve the local replica for a model, how to fetch a unit's
input, and how to upload its result. This module supplies the production
implementations, keeping that policy out of both A6 and the assembly.

- endpoint resolution: the local READY replica for ``model_id`` (from the
  supervisor's live table), dialled on the tailnet/loopback bind host.
- input fetch: ``GET lease.input_url`` with the device bearer token.
- result upload: persist the payload under the local results directory and
  return its path as the ``result_ref`` (v0.1 keeps results agent-local; a
  coordinator upload endpoint is future work — see ADR 015).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from fallow_agent.main.protocols import SupervisorLike
from fallow_agent.main.settings import AgentSettings
from fallow_agent.workers import (
    EmbedWorker,
    TranscribeConfig,
    TranscribeWorker,
    WorkerRegistry,
    WorkerUnavailableError,
    WorkUnitRunner,
)
from fallow_agent.workers.runner import FetchInput, Monotonic, UploadResult
from fallow_agent.workers.types import EndpointResolver, LocalEndpoint, Worker
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import WorkUnitLease
from fallow_protocol.models import ReplicaState

logger = logging.getLogger(__name__)


def endpoint_resolver(supervisor: SupervisorLike, bind_host: str) -> EndpointResolver:
    """Resolve a model_id to its local READY replica's endpoint."""

    def _resolve(model_id: str) -> LocalEndpoint:
        for status in supervisor.statuses():
            if status.model_id == model_id and status.state is ReplicaState.READY:
                return LocalEndpoint(host=bind_host, port=status.port)
        raise WorkerUnavailableError(f"no READY replica serving {model_id!r}")

    return _resolve


def make_fetch_input(client: httpx.AsyncClient, device_token: str) -> FetchInput:
    """Fetch a unit's input bytes from ``lease.input_url`` (device-token auth)."""
    headers = {"Authorization": f"Bearer {device_token}"}

    async def _fetch(lease: WorkUnitLease) -> bytes:
        response = await client.get(lease.input_url, headers=headers)
        response.raise_for_status()
        return response.content

    return _fetch


def make_upload(results_dir: Path) -> UploadResult:
    """Persist a result payload locally and return its path as the ref."""
    base = results_dir.expanduser()

    async def _upload(lease: WorkUnitLease, payload: bytes) -> str:
        base.mkdir(parents=True, exist_ok=True)
        dest = base / f"{lease.work_unit_id}.bin"
        dest.write_bytes(payload)
        return str(dest)

    return _upload


def build_workers(
    client: httpx.AsyncClient, resolver: EndpointResolver, settings: AgentSettings
) -> dict[WorkerKind, Worker]:
    """Build the worker instances this machine can actually run.

    A worker whose backend is unavailable (e.g. the whisper extra is missing) is
    logged and skipped so the scheduler never leases it work it cannot run.
    """
    registry = _register_workers(client, resolver, settings)
    workers: dict[WorkerKind, Worker] = {}
    for kind in registry.kinds:
        try:
            workers[kind] = registry.create(kind)
        except WorkerUnavailableError as exc:
            logger.warning("worker %s unavailable; skipping: %s", kind.value, exc)
    return workers


def _register_workers(
    client: httpx.AsyncClient, resolver: EndpointResolver, settings: AgentSettings
) -> WorkerRegistry:
    registry = WorkerRegistry().register(
        WorkerKind.EMBED,
        lambda: EmbedWorker(client=client, resolve_endpoint=resolver),
    )
    whisper = settings.whisper
    if whisper.model_size_or_path is not None:
        config = TranscribeConfig(
            model_size_or_path=whisper.model_size_or_path,
            device=whisper.device,
            compute_type=whisper.compute_type,
            beam_size=whisper.beam_size,
        )
        tmp_dir = settings.results_dir.expanduser() / "audio"
        registry = registry.register(
            WorkerKind.TRANSCRIBE,
            lambda: TranscribeWorker(config=config, tmp_dir=tmp_dir),
        )
    return registry


def build_runner(
    *,
    client: httpx.AsyncClient,
    device_token: str,
    supervisor: SupervisorLike,
    settings: AgentSettings,
    fetch_input: FetchInput | None,
    upload: UploadResult | None,
    monotonic: Monotonic,
) -> WorkUnitRunner:
    """Compose the fully-wired :class:`WorkUnitRunner`."""
    resolver = endpoint_resolver(supervisor, settings.bind_host)
    workers = build_workers(client, resolver, settings)
    fetch = fetch_input or make_fetch_input(client, device_token)
    up = upload or make_upload(settings.results_dir)
    return WorkUnitRunner(workers=workers, fetch_input=fetch, upload=up, monotonic=monotonic)
