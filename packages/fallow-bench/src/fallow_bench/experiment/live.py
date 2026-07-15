from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Protocol

import httpx

from fallow_bench.churn.config import parse_churn_section
from fallow_bench.churn.injector import ChurnInjector
from fallow_bench.churn.models import ChurnSection
from fallow_bench.churn.runner import run_shell
from fallow_bench.churn.schedule import resolve_schedule
from fallow_bench.churn.writer import ChurnLog
from fallow_bench.experiment.layout import RunLayout
from fallow_bench.experiment.models import ArmName, RunSpec
from fallow_bench.experiment.runner import ExperimentRunner
from fallow_bench.experiment.templates import render_coordinator_config
from fallow_bench.workload.admin import BenchAdminClient
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.config import ExperimentConfig, load_config
from fallow_bench.workload.records import PowerSample
from fallow_bench.workload.runner import RunMetadata, WorkloadRunner
from fallow_bench.workload.writer import JsonlWriter
from fallow_protocol.messages import AgentState, EventKind
from fallow_protocol.models import ReplicaState

_HOST = "127.0.0.1"
_PORT = 8080
_READY_ATTEMPTS = 100
_READY_INTERVAL_S = 0.1
_FLEET_READY_ATTEMPTS = 1_200
_FLEET_READY_INTERVAL_S = 0.5
_BASELINE_DURATION_S = 30
_BASELINE_INTERVAL_S = 1


class CoordinatorProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


SpawnCoordinator = Callable[[Path, str], Awaitable[CoordinatorProcess]]
WaitReady = Callable[[str, str, CoordinatorProcess], Awaitable[None]]
WaitFleetReady = Callable[[str, str, frozenset[str], frozenset[str]], Awaitable[None]]
StopCoordinator = Callable[[CoordinatorProcess], Awaitable[None]]
RunWorkload = Callable[[ExperimentConfig, Path, RunLayout, str, str, RunMetadata], Awaitable[None]]
RunChurn = Callable[[ChurnSection, RunLayout], Awaitable[None]]
CaptureBaseline = Callable[[str, str, RunLayout], Awaitable[None]]
RunWithDeadline = Callable[[Awaitable[None], float], Awaitable[None]]
CopySeedDatabase = Callable[[Path, Path], None]
GitSha = Callable[[], str]
Now = Callable[[], datetime]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _digest(config: ExperimentConfig, spec: RunSpec) -> str:
    values = config.model_dump(mode="json")
    values["scheduler"] = spec.arm.scheduler
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _resolved_config(
    base: ExperimentConfig,
    spec: RunSpec,
    *,
    config_base_dir: Path,
    host: str,
    port: int,
) -> ExperimentConfig:
    churn = base.churn
    if churn is not None:
        churn = {**churn, "duration_s": spec.duration_s, "seed": spec.seed}
    corpus_path = Path(base.batch.corpus_path)
    if not corpus_path.is_absolute():
        corpus_path = config_base_dir / corpus_path
    submit_at_s = min(base.batch.submit_at_s, spec.duration_s / 2)
    batch = base.batch.model_copy(
        update={"corpus_path": str(corpus_path), "submit_at_s": submit_at_s}
    )
    return base.model_copy(
        update={
            "arm_label": str(spec.arm.name),
            "coordinator_url": f"http://{host}:{port}",
            "duration_s": spec.duration_s,
            "seed": spec.seed,
            "batch": batch,
            "churn": churn,
        }
    )


async def _spawn_coordinator(config_path: Path, admin_key: str) -> CoordinatorProcess:
    environment = {**os.environ, "FALLOW_COORD_ADMIN_KEY": admin_key}
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fallow_coordinator",
        "serve",
        "--config",
        str(config_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
    )


async def _wait_ready(base_url: str, admin_key: str, process: CoordinatorProcess) -> None:
    headers = {"Authorization": f"Bearer {admin_key}"}
    async with httpx.AsyncClient(base_url=base_url) as client:
        for _ in range(_READY_ATTEMPTS):
            if process.returncode is not None:
                raise RuntimeError(f"coordinator exited before readiness ({process.returncode})")
            try:
                response = await client.get("/v1/admin/agents", headers=headers)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_READY_INTERVAL_S)
    raise TimeoutError(f"coordinator did not become ready at {base_url}")


async def _wait_fleet_ready(
    base_url: str,
    admin_key: str,
    expected_agents: frozenset[str],
    required_models: frozenset[str],
) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        admin = BenchAdminClient(client, admin_key)
        for _ in range(_FLEET_READY_ATTEMPTS):
            agents = await admin.list_agents()
            ready_agents = {
                agent.agent_id
                for agent in agents
                if agent.state is AgentState.IDLE and not agent.suspect
            }
            ready_models = {
                replica.model_id
                for agent in agents
                if agent.agent_id in ready_agents
                for replica in agent.replicas
                if replica.state is ReplicaState.READY
            }
            if ready_agents == expected_agents and required_models <= ready_models:
                return
            await asyncio.sleep(_FLEET_READY_INTERVAL_S)
    raise TimeoutError(
        "fleet did not reach the expected idle-agent and ready-model set before timeout"
    )


async def _stop_coordinator(process: CoordinatorProcess) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _create_api_key(base_url: str, admin_key: str) -> str:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.post(
            "/v1/admin/api_keys",
            json={"name": "experiment-run"},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        response.raise_for_status()
        key = response.json().get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError("coordinator returned an invalid experiment API key")
    return key


async def _capture_baseline(base_url: str, admin_key: str, layout: RunLayout) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        admin = BenchAdminClient(client, admin_key)
        with JsonlWriter(layout.power) as writer:
            for sample_index in range(_BASELINE_DURATION_S):
                agents = await admin.list_agents()
                captured_at = datetime.now(UTC)
                for agent in agents:
                    if not agent.gpus:
                        writer.write(
                            PowerSample(
                                t=captured_at,
                                agent_id=agent.agent_id,
                                state=str(agent.state),
                                gpu_index=None,
                                power_w=None,
                                util_percent=None,
                                vram_free_mb=None,
                            )
                        )
                    for gpu in agent.gpus:
                        writer.write(
                            PowerSample(
                                t=captured_at,
                                agent_id=agent.agent_id,
                                state=str(agent.state),
                                gpu_index=gpu.index,
                                power_w=gpu.power_w,
                                util_percent=gpu.util_percent,
                                vram_free_mb=gpu.vram_free_mb,
                            )
                        )
                if sample_index + 1 < _BASELINE_DURATION_S:
                    await asyncio.sleep(_BASELINE_INTERVAL_S)


async def _run_workload(
    config: ExperimentConfig,
    config_base_dir: Path,
    layout: RunLayout,
    api_key: str,
    admin_key: str,
    metadata: RunMetadata,
) -> None:
    async with (
        httpx.AsyncClient(base_url=config.coordinator_url) as interactive_client,
        httpx.AsyncClient(base_url=config.coordinator_url) as admin_client,
    ):
        await WorkloadRunner(
            config=config,
            base_dir=config_base_dir,
            out_dir=layout.directory,
            interactive_client=interactive_client,
            admin_client=admin_client,
            api_key=api_key,
            admin_key=admin_key,
            clocks=Clocks(),
            run_metadata=metadata,
        ).run()


async def _run_churn(section: ChurnSection, layout: RunLayout) -> None:
    agents = {agent.name: agent for agent in section.agents}
    async with httpx.AsyncClient() as client:
        await ChurnInjector(
            client=client,
            runner=run_shell,
            sink=ChurnLog(layout.churn).write,
            clock=time.monotonic,
            wall_clock=time.time,
            sleep=asyncio.sleep,
            agents=agents,
            commands=section.commands,
            verify=section.verify,
        ).run(resolve_schedule(section))


async def _run_with_deadline(awaitable: Awaitable[None], duration_s: float) -> None:
    task: asyncio.Future[None] = asyncio.ensure_future(awaitable)
    timer = asyncio.create_task(asyncio.sleep(duration_s))
    try:
        done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            await timer
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        if not timer.done():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)


def _copy_seed_database(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)


def _inspect_seed_database(path: Path) -> frozenset[str]:
    for suffix in ("-wal", "-shm"):
        if Path(f"{path}{suffix}").exists():
            raise ValueError(f"seed database has an uncheckpointed sidecar: {path}{suffix}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            agents = frozenset(
                str(row[0]) for row in connection.execute("SELECT agent_id FROM registry_agents")
            )
            job_count = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            unit_count = int(connection.execute("SELECT COUNT(*) FROM work_units").fetchone()[0])
    except sqlite3.Error as exc:
        raise ValueError(f"invalid coordinator seed database: {path}") from exc
    if job_count or unit_count:
        raise ValueError("seed database must contain no jobs or work units")
    if not agents:
        raise ValueError("seed database must contain at least one enrolled agent")
    return agents


def _validate_churn_history(path: Path) -> None:
    open_sessions: dict[str, datetime] = {}
    completed_sessions = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        agent_id = record.get("agent_id")
        kind = record.get("kind")
        at_raw = record.get("at")
        if (
            not isinstance(agent_id, str)
            or not isinstance(kind, str)
            or not isinstance(at_raw, str)
        ):
            continue
        try:
            at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None or at.utcoffset() is None:
            continue
        if kind == EventKind.USER_IDLE.value:
            open_sessions[agent_id] = at
        elif kind == EventKind.USER_RETURNED.value:
            started_at = open_sessions.pop(agent_id, None)
            if started_at is not None and at >= started_at:
                completed_sessions += 1
    if completed_sessions == 0:
        raise ValueError("churn history must contain a valid completed idle session")


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_repo_root(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class LiveRuntime:
    """Default adapter from one canonical run spec to the live drivers."""

    def __init__(
        self,
        *,
        root: Path,
        base_config: ExperimentConfig,
        config_base_dir: Path,
        template_root: Path,
        seed_databases: Mapping[ArmName, Path],
        expected_agents: Mapping[ArmName, frozenset[str]],
        churn_history: Path | None,
        admin_key: str,
        host: str = _HOST,
        port: int = _PORT,
        spawn_coordinator: SpawnCoordinator = _spawn_coordinator,
        wait_ready: WaitReady = _wait_ready,
        wait_fleet_ready: WaitFleetReady = _wait_fleet_ready,
        stop_coordinator: StopCoordinator = _stop_coordinator,
        create_api_key: Callable[[str, str], Awaitable[str]] = _create_api_key,
        capture_baseline: CaptureBaseline = _capture_baseline,
        run_workload: RunWorkload = _run_workload,
        run_churn: RunChurn = _run_churn,
        run_with_deadline: RunWithDeadline = _run_with_deadline,
        copy_seed_database: CopySeedDatabase = _copy_seed_database,
        now: Now = lambda: datetime.now(UTC),
        git_sha: GitSha = _git_sha,
    ) -> None:
        self._root = root
        self._base_config = base_config
        self._config_base_dir = config_base_dir
        self._template_root = template_root
        self._seed_databases = dict(seed_databases)
        self._expected_agents = dict(expected_agents)
        self._churn_history = churn_history
        self._admin_key = admin_key
        self._host = host
        self._port = port
        self._spawn_coordinator = spawn_coordinator
        self._wait_ready = wait_ready
        self._wait_fleet_ready = wait_fleet_ready
        self._stop_coordinator = stop_coordinator
        self._create_api_key = create_api_key
        self._capture_baseline = capture_baseline
        self._run_workload = run_workload
        self._run_churn = run_churn
        self._run_with_deadline = run_with_deadline
        self._copy_seed_database = copy_seed_database
        self._now = now
        self._git_sha = git_sha

    async def run(self, spec: RunSpec, layout: RunLayout) -> None:
        config = _resolved_config(
            self._base_config,
            spec,
            config_base_dir=self._config_base_dir,
            host=self._host,
            port=self._port,
        )
        if spec.arm.name is ArmName.CHURN_V2 and self._churn_history is None:
            raise RuntimeError("churn_v2 requires an immutable churn history file")
        process: CoordinatorProcess | None = None
        try:
            seed_database = self._seed_databases[spec.arm.name]
            expected_agents = self._expected_agents[spec.arm.name]
        except KeyError as exc:
            raise RuntimeError(f"no fleet snapshot configured for {spec.arm.name}") from exc
        try:
            self._copy_seed_database(seed_database, layout.database)
            render_coordinator_config(
                self._template_root,
                layout,
                spec.arm,
                churn_history_path=self._churn_history,
                host=self._host,
                port=self._port,
            )
            process = await self._spawn_coordinator(layout.coordinator_config, self._admin_key)
            await self._wait_ready(config.coordinator_url, self._admin_key, process)
            api_key = await self._create_api_key(config.coordinator_url, self._admin_key)
            await self._wait_fleet_ready(
                config.coordinator_url,
                self._admin_key,
                expected_agents,
                frozenset((config.model_id, config.batch.model_id)),
            )
        except BaseException:
            if process is not None:
                await self._stop_coordinator(process)
            raise

        metadata = RunMetadata(
            started_at=self._now(),
            arm_label=str(spec.arm.name),
            rep=spec.repetition,
            seed=spec.seed,
            duration_s=spec.duration_s,
            config_digest=_digest(config, spec),
            git_sha=self._git_sha(),
        )

        async def baseline(*, spec: RunSpec, layout: RunLayout) -> None:
            nonlocal metadata
            del spec
            await self._capture_baseline(config.coordinator_url, self._admin_key, layout)
            metadata = metadata.model_copy(update={"started_at": self._now()})
            layout.run_meta.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")

        async def workload(*, spec: RunSpec, layout: RunLayout) -> None:
            await self._run_with_deadline(
                self._run_workload(
                    config,
                    self._config_base_dir,
                    layout,
                    api_key,
                    self._admin_key,
                    metadata,
                ),
                spec.duration_s,
            )

        async def churn(*, spec: RunSpec, layout: RunLayout) -> None:
            if config.churn is None:
                raise RuntimeError("churn-enabled experiment arm has no churn configuration")
            await self._run_with_deadline(
                self._run_churn(parse_churn_section(config.churn), layout),
                spec.duration_s,
            )

        async def cleanup(*, spec: RunSpec, layout: RunLayout) -> None:
            del spec, layout
            if process is not None:
                await self._stop_coordinator(process)

        runner = ExperimentRunner(
            root=self._root,
            baseline=baseline,
            workload=workload,
            churn=churn,
            cleanup=cleanup,
            now=lambda: metadata.started_at,
            config_digest=metadata.config_digest,
            git_sha=metadata.git_sha,
        )
        await runner.run(spec, layout=layout)


def default_runner_factory(
    root: Path,
    *,
    config_path: Path,
    seed_database: Path | None,
    dedicated_seed_database: Path | None,
    churn_history: Path | None,
    host: str,
    port: int,
    revision: str | None,
) -> Callable[[RunSpec, RunLayout], Awaitable[None]]:
    if not config_path.is_file():
        raise FileNotFoundError(f"experiment config does not exist: {config_path}")
    base_config = load_config(config_path)
    seed_databases: dict[ArmName, Path] = {}
    expected_agents: dict[ArmName, frozenset[str]] = {}
    if seed_database is not None:
        if not seed_database.is_file():
            raise FileNotFoundError(f"seed database does not exist: {seed_database}")
        seed_databases[ArmName.ROUND_ROBIN] = seed_database
        seed_databases[ArmName.CHURN_V2] = seed_database
        fleet_agents = _inspect_seed_database(seed_database)
        expected_agents[ArmName.ROUND_ROBIN] = fleet_agents
        expected_agents[ArmName.CHURN_V2] = fleet_agents
    if dedicated_seed_database is not None:
        if not dedicated_seed_database.is_file():
            raise FileNotFoundError(
                f"dedicated seed database does not exist: {dedicated_seed_database}"
            )
        seed_databases[ArmName.DEDICATED] = dedicated_seed_database
        dedicated_agents = _inspect_seed_database(dedicated_seed_database)
        if len(dedicated_agents) != 1:
            raise ValueError("dedicated seed database must contain exactly one agent")
        expected_agents[ArmName.DEDICATED] = dedicated_agents
    if churn_history is not None and not churn_history.is_file():
        raise FileNotFoundError(f"churn history does not exist: {churn_history}")
    if churn_history is not None:
        _validate_churn_history(churn_history)
    admin_key_env = base_config.sampling.admin_key_env
    admin_key = os.environ.get(admin_key_env)
    if not admin_key:
        raise RuntimeError(f"environment variable {admin_key_env!r} is not set")
    template_root = Path(str(resources.files("fallow_bench.experiment").joinpath("arms")))
    missing = [
        name
        for name in ("dedicated", "round_robin", "churn_v2")
        if not (template_root / f"{name}.toml.in").is_file()
    ]
    if missing:
        raise RuntimeError(f"missing installed coordinator templates: {', '.join(missing)}")
    for prompt_ref in base_config.interactive.prompt_files:
        prompt_path = Path(prompt_ref)
        if not prompt_path.is_absolute():
            prompt_path = config_path.parent / prompt_path
        if not prompt_path.is_file():
            raise FileNotFoundError(f"experiment prompt does not exist: {prompt_path}")
    corpus_path = Path(base_config.batch.corpus_path)
    if not corpus_path.is_absolute():
        corpus_path = config_path.parent / corpus_path
    if not corpus_path.is_file():
        raise FileNotFoundError(f"experiment corpus does not exist: {corpus_path}")
    runtime = LiveRuntime(
        root=root,
        base_config=base_config,
        config_base_dir=config_path.parent,
        template_root=template_root,
        seed_databases=seed_databases,
        expected_agents=expected_agents,
        churn_history=churn_history,
        admin_key=admin_key,
        host=host,
        port=port,
        git_sha=(lambda: revision) if revision is not None else _git_sha,
    )
    return runtime.run
