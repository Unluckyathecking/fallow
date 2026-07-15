"""The composition root: turn settings + seams into a running agent.

``AgentAssembly.build`` resolves identity (register-or-load), constructs every
component through the injected seams, wires them together, and returns the
:class:`AgentServices` lifecycle plus a handle to release the shared HTTP client.
It is the *only* place the concrete modules are named together — the layer DAG
(``main`` → heartbeat|workers → idle|preempt|supervisor|modelcache) is honoured
here and enforced in CI by import-linter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import httpx

from fallow_agent.bench import BenchIdleDetector, BenchListener
from fallow_agent.heartbeat import CoordinatorClient, HeartbeatLoop, HttpEventSink
from fallow_agent.main.enroll import resolve_identity
from fallow_agent.main.heartbeat_wiring import make_final_heartbeat, make_on_response
from fallow_agent.main.identity import IdentityState
from fallow_agent.main.manifest import ManifestFetcher
from fallow_agent.main.ports import PortAllocator
from fallow_agent.main.protocols import SupervisorLike
from fallow_agent.main.reconcile import ReconcileLoop
from fallow_agent.main.runner_wiring import build_runner
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.main.services import AgentServices
from fallow_agent.main.settings import AgentSettings
from fallow_agent.main.shared import DesiredModels, LeaseRegistry
from fallow_agent.main.work import WorkLoop
from fallow_agent.modelcache import HttpModelStore
from fallow_agent.preempt import PollLoop, PreemptController
from fallow_agent.supervisor import SupervisorConfig, llama_server_command
from fallow_protocol.interfaces import IdleDetector, ProcessSupervisor
from fallow_protocol.messages import AgentConfig
from fallow_protocol.version import PROTOCOL_VERSION


@dataclass(frozen=True)
class BuiltAgent:
    """The assembled, not-yet-started agent plus its teardown handle."""

    services: AgentServices
    aclose: Callable[[], Awaitable[None]]


class AgentAssembly:
    """Builds an :class:`AgentServices` from settings and injectable seams."""

    def __init__(
        self,
        settings: AgentSettings,
        seams: RuntimeSeams,
        *,
        on_fatal: Callable[[], None],
    ) -> None:
        self._s = settings
        self._seams = seams
        self._on_fatal = on_fatal

    async def build(self) -> BuiltAgent:
        http = self._seams.http_client_factory()
        identity, config = await resolve_identity(
            self._s, http, caps_factory=self._seams.caps_factory
        )
        supervisor = self._build_supervisor()
        idle, bench_detector = self._resolve_idle()
        client = self._build_client(http, identity)
        event_sink = HttpEventSink(client=client, jsonl_path=self._s.events_jsonl_path)
        preemptor = PreemptController(
            cast(ProcessSupervisor, supervisor),
            event_sink,
            config,
            identity.agent_id,
            monotonic=self._seams.monotonic,
            now=self._seams.now,
        )
        poll_loop = PollLoop(idle, preemptor, config, monotonic=self._seams.monotonic)
        desired = DesiredModels()
        leases = LeaseRegistry()
        services = AgentServices(
            preemptor=preemptor,
            supervisor=supervisor,
            event_sink=event_sink,
            poll_loop=poll_loop,
            heartbeat=self._build_heartbeat(
                client, identity, config, preemptor, supervisor, idle, desired, leases
            ),
            reconcile_loop=self._build_reconcile(http, identity, supervisor, preemptor, desired),
            work_loop=self._build_work(http, client, identity, supervisor, preemptor, leases),
            final_heartbeat=make_final_heartbeat(
                client=client,
                agent_id=identity.agent_id,
                preemptor=preemptor,
                supervisor=supervisor,
                idle=idle,
                leases=leases,
                metrics=self._seams.metrics,
                now=self._seams.now,
            ),
            bench_listener=self._make_bench_listener(bench_detector, preemptor),
        )
        return BuiltAgent(services=services, aclose=http.aclose)

    # ── Component builders ───────────────────────────────────────────────────

    def _resolve_idle(self) -> tuple[IdleDetector, BenchIdleDetector | None]:
        """Build the OS idle detector, wrapping it for bench when enabled.

        Returns the detector to wire everywhere plus the ``BenchIdleDetector``
        (or ``None``) the bench listener drives.
        """
        raw = self._seams.idle_factory()
        if not self._s.bench.enabled:
            return raw, None
        bench = BenchIdleDetector(raw, monotonic=self._seams.monotonic)
        return bench, bench

    def _make_bench_listener(
        self, bench_detector: BenchIdleDetector | None, preemptor: PreemptController
    ) -> BenchListener | None:
        if bench_detector is None:
            return None
        return BenchListener(
            host=self._s.bind_host,
            port=self._s.bench.port,
            detector=bench_detector,
            state_source=preemptor,
        )

    def _build_supervisor(self) -> SupervisorLike:
        sup_config = SupervisorConfig(
            llama_binary=self._s.llama_server_binary,
            bind_host=self._s.bind_host,
        )
        return self._seams.supervisor_factory(sup_config, llama_server_command(sup_config))

    def _build_client(self, http: httpx.AsyncClient, identity: IdentityState) -> CoordinatorClient:
        return CoordinatorClient(
            base_url=self._s.coordinator_url,
            client=http,
            agent_id=identity.agent_id,
            device_token=identity.device_token,
        )

    def _build_heartbeat(
        self,
        client: CoordinatorClient,
        identity: IdentityState,
        config: AgentConfig,
        preemptor: PreemptController,
        supervisor: SupervisorLike,
        idle: IdleDetector,
        desired: DesiredModels,
        leases: LeaseRegistry,
    ) -> HeartbeatLoop:
        return HeartbeatLoop(
            client=client,
            agent_id=identity.agent_id,
            protocol_version=PROTOCOL_VERSION,
            interval_s=config.heartbeat_interval_s,
            preemptor=preemptor,
            supervisor=cast(ProcessSupervisor, supervisor),
            idle=idle,
            lease_ids=leases.current,
            metrics=self._seams.metrics,
            on_response=make_on_response(desired),
            on_auth_error=lambda _exc: self._on_fatal(),
            now=self._seams.now,
        )

    def _build_reconcile(
        self,
        http: httpx.AsyncClient,
        identity: IdentityState,
        supervisor: SupervisorLike,
        preemptor: PreemptController,
        desired: DesiredModels,
    ) -> ReconcileLoop:
        modelstore = HttpModelStore(
            base_url=self._s.coordinator_url,
            device_token=identity.device_token,
            client=http,
            cache_dir=self._s.cache_dir,
        )
        fetcher = ManifestFetcher(
            base_url=self._s.coordinator_url,
            device_token=identity.device_token,
            client=http,
        )
        return ReconcileLoop(
            supervisor=supervisor,
            modelstore=modelstore,
            fetch_manifest=fetcher.fetch,
            preemptor=preemptor,
            ports=PortAllocator(self._s.port_range.start, self._s.port_range.count),
            desired=desired.current,
            interval_s=self._s.reconcile_interval_s,
            sleep=self._seams.sleep,
        )

    def _build_work(
        self,
        http: httpx.AsyncClient,
        client: CoordinatorClient,
        identity: IdentityState,
        supervisor: SupervisorLike,
        preemptor: PreemptController,
        leases: LeaseRegistry,
    ) -> WorkLoop:
        runner = build_runner(
            client=http,
            agent_id=identity.agent_id,
            device_token=identity.device_token,
            supervisor=supervisor,
            settings=self._s,
            fetch_input=self._seams.fetch_input,
            upload=self._seams.upload,
            monotonic=self._seams.monotonic,
        )
        return WorkLoop(
            client=client,
            runner=runner,
            preemptor=preemptor,
            leases=leases,
            poll_timeout_s=self._s.work_poll_timeout_s,
            active_sleep_s=self._s.active_sleep_s,
            now=self._seams.now,
            sleep=self._seams.sleep,
        )
