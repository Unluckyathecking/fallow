"""Public E1.3 smoke command against the in-process coordinator test fleet."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from integration_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    EMBED_MODEL,
    create_api_key,
    enroll_agent,
    heartbeat,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    register_model,
    upload_result,
)
from stub_server import StubServer

from fallow_agent.heartbeat import CoordinatorClient
from fallow_bench.__main__ import main as bench_main
from fallow_bench.analysis import (
    AnalysisConfig,
    EnergyBaseline,
    ReportMeta,
    analyze,
    load_run,
)
from fallow_bench.experiment.layout import RunLayout
from fallow_bench.experiment.live import CoordinatorProcess, LiveRuntime
from fallow_bench.experiment.models import ArmName, RunSpec
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.config import ExperimentConfig
from fallow_bench.workload.runner import RunMetadata, WorkloadRunner
from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import AgentEvent, EventKind
from fallow_protocol.models import ReplicaState

_SSE = (
    b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
    b"data: [DONE]\n\n",
)


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class _FastClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.origin = datetime(2026, 7, 15, 12, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)

    async def sleep(self, delay_s: float) -> None:
        self.seconds += delay_s
        await asyncio.sleep(0)


def _base_config(prompt: Path, corpus: Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "arm_label": "placeholder",
            "coordinator_url": "http://unused.invalid",
            "api_key_env": "FLW_API_KEY",
            "model_id": CHAT_MODEL,
            "duration_s": 1,
            "seed": 1,
            "interactive": {
                "rate_per_min": 1,
                "max_tokens": 8,
                "prompt_files": [str(prompt)],
                "request_timeout_s": 5,
            },
            "batch": {
                "corpus_path": str(corpus),
                "submit_at_s": 0,
                "model_id": EMBED_MODEL,
                "poll_interval_s": 120,
            },
            "sampling": {"admin_poll_hz": 1, "admin_key_env": "FLW_ADMIN_KEY"},
        }
    )


def test_public_smoke_command_produces_analysis_ready_run(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.\n", encoding="utf-8")
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id":"0","text":"smoke"}\n', encoding="utf-8")
    seed_database = tmp_path / "seed.db"
    seed_database.touch()
    base_config = _base_config(prompt, corpus)
    template_root = Path(str(resources.files("fallow_bench.experiment").joinpath("arms")))
    clock = _FastClock()
    state: dict[str, object] = {}
    deadlines: list[float] = []

    async def spawn(config_path: Path, admin_key: str) -> CoordinatorProcess:
        assert admin_key == ADMIN_KEY
        layout_dir = config_path.parent
        stack = AsyncExitStack()
        stub = await stack.enter_async_context(StubServer(chunks=_SSE))
        config = CoordinatorConfig.model_validate(
            {
                "db_path": layout_dir / "coordinator.db",
                "blob_dir": layout_dir / "blobs",
                "unit_input_dir": layout_dir / "unit-inputs",
                "result_dir": layout_dir / "results",
                "events_jsonl_path": layout_dir / "events.jsonl",
                "gateway_log_path": layout_dir / "gateway.jsonl",
                "admin_key": ADMIN_KEY,
                "scheduler": "capability",
                "suspect_after_s": 5000,
                "offline_after_s": 10000,
                "requeue_interval_s": 3600,
            }
        )
        app = create_app(config, now=clock.now, sleep=asyncio.sleep)
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=ASGITransport(app=app, client=("127.0.0.1", 9999)),
                base_url="http://coord",
            )
        )
        process = _Process()
        state.update(stack=stack, stub=stub, client=client, process=process)
        return process

    async def ready(_url: str, _key: str, process: CoordinatorProcess) -> None:
        assert process.returncode is None
        client = state["client"]
        assert isinstance(client, httpx.AsyncClient)
        response = await client.get(
            "/v1/admin/agents", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert response.status_code == 200

    async def create_key(_url: str, _key: str) -> str:
        client = state["client"]
        assert isinstance(client, httpx.AsyncClient)
        return await create_api_key(client, "public-smoke")

    async def fleet_ready(
        _url: str,
        _key: str,
        _expected_agents: frozenset[str],
        _required_models: frozenset[str],
    ) -> None:
        return None

    async def prepare_fleet(_url: str, _key: str, layout: RunLayout) -> None:
        client = state["client"]
        stub = state["stub"]
        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(stub, StubServer)
        for model_id, kind in ((CHAT_MODEL, WorkerKind.CHAT), (EMBED_MODEL, WorkerKind.EMBED)):
            blob = layout.directory / f"{model_id}.gguf"
            blob.write_bytes(b"fake-gguf")
            await register_model(client, make_manifest(model_id, kind), str(blob))
        token = await mint_enrollment_token(client)
        agent = await enroll_agent(client, token, hostname="public-smoke")
        await heartbeat(
            agent,
            replicas=(
                make_replica(CHAT_MODEL, stub.port, ReplicaState.READY),
                make_replica(EMBED_MODEL, stub.port, ReplicaState.READY),
            ),
        )
        assert agent.agent_id is not None
        state["agent"] = agent

    async def record_event() -> None:
        agent = state["agent"]
        assert isinstance(agent, CoordinatorClient)
        assert agent.agent_id is not None
        await agent.push_event(
            AgentEvent(
                agent_id=agent.agent_id,
                kind=EventKind.USER_RETURNED,
                at=clock.now(),
                detail={"yield_ms": "1.0"},
            )
        )

    async def run_workload(
        config: ExperimentConfig,
        config_base_dir: Path,
        layout: RunLayout,
        api_key: str,
        admin_key: str,
        metadata: RunMetadata,
    ) -> None:
        client = state["client"]
        assert isinstance(client, httpx.AsyncClient)
        await WorkloadRunner(
            config=config,
            base_dir=config_base_dir,
            out_dir=layout.directory,
            interactive_client=client,
            admin_client=client,
            api_key=api_key,
            admin_key=admin_key,
            clocks=Clocks(monotonic=clock.monotonic, now=clock.now, sleep=clock.sleep),
            run_metadata=metadata,
        ).run()
        agent = state["agent"]
        assert isinstance(agent, CoordinatorClient)
        lease = await agent.poll_work(0.0)
        assert lease is not None
        result = await upload_result(client, agent, lease, b'{"vectors":[[1.0]]}')
        await agent.complete_unit(result, lease_attempt=lease.attempt)
        await record_event()

    async def deadline(awaitable: Awaitable[None], duration_s: float) -> None:
        deadlines.append(duration_s)
        await awaitable

    async def stop(process: CoordinatorProcess) -> None:
        stack = state["stack"]
        assert isinstance(stack, AsyncExitStack)
        await stack.aclose()
        process.terminate()

    def factory(
        _root: Path,
    ) -> Callable[[RunSpec, RunLayout], Awaitable[None]]:
        runtime = LiveRuntime(
            root=root,
            base_config=base_config,
            config_base_dir=tmp_path,
            template_root=template_root,
            seed_databases={ArmName.DEDICATED: seed_database},
            expected_agents={ArmName.DEDICATED: frozenset()},
            churn_history=None,
            admin_key=ADMIN_KEY,
            spawn_coordinator=spawn,
            wait_ready=ready,
            wait_fleet_ready=fleet_ready,
            stop_coordinator=stop,
            create_api_key=create_key,
            capture_baseline=prepare_fleet,
            run_workload=run_workload,
            run_with_deadline=deadline,
            now=clock.now,
            git_sha=lambda: "public-smoke",
        )
        return runtime.run

    with pytest.raises(SystemExit) as exit_info:
        bench_main(
            [
                "experiment",
                "--out",
                str(root),
                "--smoke",
                "--arm",
                "dedicated",
                "--repetition",
                "1",
            ],
            experiment_runner_factory=factory,
        )

    assert exit_info.value.code == 0
    layout_dir = root / "dedicated" / "rep-01"
    metadata = RunMetadata.model_validate_json((layout_dir / "run_meta.json").read_text())
    assert metadata.duration_s == 120
    assert deadlines == []
    assert clock.monotonic() >= 120
    stub = state["stub"]
    assert isinstance(stub, StubServer)
    assert stub.hits == 1
    canonical = (
        "coordinator.toml",
        "run_meta.json",
        "client_trace.jsonl",
        "gateway.jsonl",
        "events.jsonl",
        "churn.jsonl",
        "power.jsonl",
        "units.jsonl",
        "schedule.jsonl",
        "jobs.jsonl",
    )
    assert all((layout_dir / name).is_file() for name in canonical)
    config = AnalysisConfig(energy_baseline=EnergyBaseline(start_s=0, end_s=1))
    frames = load_run(layout_dir, config)
    assert frames.warnings == ()
    result = analyze(
        {"dedicated": layout_dir},
        tmp_path / "report",
        config,
        ReportMeta(git_sha="public-smoke"),
    )
    assert result.warnings == ()
    assert result.report_md.is_file()
    assert result.report_tex.is_file()
    assert all(path.is_file() for path in result.plots)
