from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fallow_bench.churn.models import ChurnSection
from fallow_bench.experiment import RunLayout, RunMode, RunSpec, build_plan, create_run_layout
from fallow_bench.experiment.live import (
    CoordinatorProcess,
    LiveRuntime,
    _digest,
    _inspect_seed_database,
    _resolved_config,
    _run_with_deadline,
    _validate_churn_history,
)
from fallow_bench.workload.config import ExperimentConfig
from fallow_bench.workload.runner import RunMetadata


class FakeProcess:
    def __init__(self) -> None:
        self._returncode: int | None = None
        self.terminated = 0

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated += 1
        self._returncode = 0

    def kill(self) -> None:
        self._returncode = -9

    async def wait(self) -> int:
        return self._returncode or 0


def _config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "arm_label": "old",
            "coordinator_url": "http://old.invalid",
            "api_key_env": "FLW_API_KEY",
            "model_id": "interactive-model",
            "duration_s": 1,
            "seed": 1,
            "interactive": {
                "rate_per_min": 1,
                "max_tokens": 8,
                "prompt_files": ["prompts.txt"],
            },
            "batch": {
                "corpus_path": "corpus.jsonl",
                "submit_at_s": 120,
                "model_id": "batch-model",
            },
            "sampling": {"admin_poll_hz": 1, "admin_key_env": "FLW_ADMIN_KEY"},
            "churn": {
                "duration_s": 1,
                "seed": 1,
                "agents": [{"name": "agent-a", "host": "127.0.0.1"}],
                "model": {
                    "idle_mu": 1,
                    "idle_sigma": 0,
                    "active_mu": 1,
                    "active_sigma": 0,
                },
            },
        }
    )


def _templates(root: Path) -> Path:
    root.mkdir()
    common = "\n".join(
        (
            "db_path = $db_path",
            "blob_dir = $blob_dir",
            "unit_input_dir = $unit_input_dir",
            "result_dir = $result_dir",
            "events_jsonl_path = $events_jsonl_path",
            "gateway_log_path = $gateway_log_path",
            "host = $host",
            "port = $port",
        )
    )
    for arm, scheduler in (
        ("dedicated", "capability"),
        ("round_robin", "roundrobin"),
        ("churn_v2", "churn_v2"),
    ):
        (root / f"{arm}.toml.in").write_text(
            f'{common}\nscheduler = "{scheduler}"\n', encoding="utf-8"
        )
    return root


@pytest.mark.asyncio
async def test_live_runtime_wires_resolved_config_and_always_stops_coordinator(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    process = FakeProcess()
    workload_started = asyncio.Event()
    churn_started = asyncio.Event()
    captured: dict[str, object] = {}
    deadlines: list[float] = []

    async def spawn(path: Path, admin_key: str) -> CoordinatorProcess:
        events.append("spawn")
        assert path.name == "coordinator.toml"
        assert admin_key == "admin"
        return process

    async def ready(url: str, admin_key: str, actual: CoordinatorProcess) -> None:
        events.append("ready")
        assert (url, admin_key, actual) == ("http://127.0.0.1:8123", "admin", process)

    async def create_key(url: str, admin_key: str) -> str:
        events.append("key")
        assert (url, admin_key) == ("http://127.0.0.1:8123", "admin")
        return "run-key"

    async def fleet_ready(
        url: str,
        admin_key: str,
        expected_agents: frozenset[str],
        required_models: frozenset[str],
    ) -> None:
        events.append("fleet-ready")
        assert (url, admin_key) == ("http://127.0.0.1:8123", "admin")
        assert expected_agents == frozenset({"agent-a"})
        assert required_models == frozenset({"interactive-model", "batch-model"})

    async def baseline(url: str, admin_key: str, layout: RunLayout) -> None:
        events.append("baseline")
        assert (url, admin_key) == ("http://127.0.0.1:8123", "admin")
        assert layout.run_meta.is_file()

    async def workload(
        config: ExperimentConfig,
        config_base_dir: Path,
        layout: RunLayout,
        api_key: str,
        admin_key: str,
        metadata: RunMetadata,
    ) -> None:
        events.append("workload")
        captured.update(config=config, metadata=metadata)
        assert config_base_dir == tmp_path
        assert (api_key, admin_key) == ("run-key", "admin")
        assert layout.run_meta.is_file()
        workload_started.set()
        await churn_started.wait()

    async def churn(section: ChurnSection, layout: RunLayout) -> None:
        events.append("churn")
        assert section.duration_s == 120
        assert section.seed == 17
        assert layout.run_meta.is_file()
        churn_started.set()
        await workload_started.wait()

    async def stop(actual: CoordinatorProcess) -> None:
        events.append("stop")
        assert actual is process
        actual.terminate()

    async def deadline(awaitable, duration_s: float):  # type: ignore[no-untyped-def]
        deadlines.append(duration_s)
        await awaitable

    spec = build_plan(RunMode.SMOKE)[3]
    layout = create_run_layout(tmp_path / "runs", spec)
    seed_database = tmp_path / "seed.db"
    seed_database.write_bytes(b"seed-database")
    times = iter(
        (
            datetime(2026, 7, 15, 12, tzinfo=UTC),
            datetime(2026, 7, 15, 12, 0, 30, tzinfo=UTC),
        )
    )
    runtime = LiveRuntime(
        root=tmp_path / "runs",
        base_config=_config(),
        config_base_dir=tmp_path,
        template_root=_templates(tmp_path / "templates"),
        seed_databases={spec.arm.name: seed_database},
        expected_agents={spec.arm.name: frozenset({"agent-a"})},
        churn_history=None,
        admin_key="admin",
        port=8123,
        spawn_coordinator=spawn,
        wait_ready=ready,
        wait_fleet_ready=fleet_ready,
        stop_coordinator=stop,
        create_api_key=create_key,
        capture_baseline=baseline,
        run_workload=workload,
        run_churn=churn,
        run_with_deadline=deadline,
        now=lambda: next(times),
        git_sha=lambda: "abc123",
    )

    await runtime.run(spec, layout)

    config = captured["config"]
    metadata = captured["metadata"]
    assert isinstance(config, ExperimentConfig)
    assert isinstance(metadata, RunMetadata)
    assert config.arm_label == "round_robin"
    assert config.duration_s == 120
    assert config.seed == 17
    assert config.batch.submit_at_s == 60
    assert config.batch.corpus_path == str(tmp_path / "corpus.jsonl")
    assert metadata.config_digest == json.loads(layout.run_meta.read_text())["config_digest"]
    assert metadata.started_at == datetime(2026, 7, 15, 12, 0, 30, tzinfo=UTC)
    assert layout.database.read_bytes() == b"seed-database"
    assert events[:5] == ["spawn", "ready", "key", "fleet-ready", "baseline"]
    assert set(events[5:7]) == {"workload", "churn"}
    assert events[-1] == "stop"
    assert process.terminated == 1
    assert deadlines == [120]


@pytest.mark.asyncio
async def test_live_runtime_skips_churn_for_dedicated_and_stops_after_failure(
    tmp_path: Path,
) -> None:
    process = FakeProcess()
    churn_called = False

    async def spawn(_path: Path, _admin_key: str) -> CoordinatorProcess:
        return process

    async def ready(_url: str, _key: str, _process: CoordinatorProcess) -> None:
        return None

    async def create_key(_url: str, _key: str) -> str:
        return "run-key"

    async def fleet_ready(
        _url: str,
        _key: str,
        _expected_agents: frozenset[str],
        _required_models: frozenset[str],
    ) -> None:
        return None

    async def baseline(_url: str, _key: str, _layout: RunLayout) -> None:
        return None

    async def workload(
        _config: ExperimentConfig,
        _config_base_dir: Path,
        _layout: RunLayout,
        _api_key: str,
        _admin_key: str,
        _metadata: RunMetadata,
    ) -> None:
        raise RuntimeError("load failed")

    async def churn(_section: ChurnSection, _layout: RunLayout) -> None:
        nonlocal churn_called
        churn_called = True

    async def stop(actual: CoordinatorProcess) -> None:
        if actual.returncode is None:
            actual.terminate()

    spec = build_plan(RunMode.SMOKE)[0]
    layout = create_run_layout(tmp_path / "runs", spec)
    seed_database = tmp_path / "seed.db"
    seed_database.write_bytes(b"seed-database")
    runtime = LiveRuntime(
        root=tmp_path / "runs",
        base_config=_config(),
        config_base_dir=tmp_path,
        template_root=_templates(tmp_path / "templates"),
        seed_databases={spec.arm.name: seed_database},
        expected_agents={spec.arm.name: frozenset({"agent-a"})},
        churn_history=None,
        admin_key="admin",
        spawn_coordinator=spawn,
        wait_ready=ready,
        wait_fleet_ready=fleet_ready,
        stop_coordinator=stop,
        create_api_key=create_key,
        capture_baseline=baseline,
        run_workload=workload,
        run_churn=churn,
    )

    with pytest.raises(RuntimeError, match="load failed"):
        await runtime.run(spec, layout)

    assert churn_called is False
    assert process.terminated == 1
    assert layout.churn.read_text(encoding="utf-8") == ""


def test_cli_uses_default_runtime_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fallow_bench.experiment import cli, live

    observed: list[tuple[RunSpec, Path]] = []

    config_path = tmp_path / "experiment.yaml"
    config_path.write_text("config", encoding="utf-8")
    seed_database = tmp_path / "seed.db"
    seed_database.write_bytes(b"seed")

    def factory(  # type: ignore[no-untyped-def]
        root: Path,
        *,
        config_path: Path,
        seed_database: Path | None,
        dedicated_seed_database: Path | None,
        churn_history: Path | None,
        host: str,
        port: int,
        revision: str | None,
    ):
        async def run(spec: RunSpec, layout: RunLayout) -> None:
            observed.append((spec, layout.directory))

        assert root == tmp_path
        assert config_path == tmp_path / "experiment.yaml"
        assert seed_database is None
        assert dedicated_seed_database == tmp_path / "seed.db"
        assert churn_history is None
        assert host == "100.64.0.10"
        assert port == 9123
        assert revision == "abc123"
        return run

    monkeypatch.setattr(live, "default_runner_factory", factory)

    result = cli.main(
        [
            "--out",
            str(tmp_path),
            "--config",
            str(config_path),
            "--dedicated-seed-db",
            str(seed_database),
            "--host",
            "100.64.0.10",
            "--port",
            "9123",
            "--revision",
            "abc123",
            "--smoke",
            "--arm",
            "dedicated",
            "--repetition",
            "1",
        ]
    )

    assert result == 0
    assert observed[0][0].arm.name == "dedicated"
    assert observed[0][1] == tmp_path / "dedicated" / "rep-01"


def test_cli_validates_seed_database_before_allocating_run_directory(tmp_path: Path) -> None:
    from fallow_bench.experiment import cli

    with pytest.raises(SystemExit, match="--dedicated-seed-db is required"):
        cli.main(["--out", str(tmp_path), "--smoke", "--arm", "dedicated"])

    assert not (tmp_path / "dedicated").exists()


def test_resolved_config_digest_is_stable_and_changes_with_arm_or_seed() -> None:
    base = _config()
    first = build_plan(RunMode.SMOKE)[0]
    other_arm = build_plan(RunMode.SMOKE)[3]
    other_seed = build_plan(RunMode.SMOKE)[1]

    resolved = _resolved_config(
        base, first, config_base_dir=Path("/experiment"), host="127.0.0.1", port=8080
    )

    assert _digest(resolved, first) == _digest(resolved, first)
    assert _digest(resolved, first) != _digest(
        _resolved_config(
            base,
            other_arm,
            config_base_dir=Path("/experiment"),
            host="127.0.0.1",
            port=8080,
        ),
        other_arm,
    )
    assert _digest(resolved, first) != _digest(
        _resolved_config(
            base,
            other_seed,
            config_base_dir=Path("/experiment"),
            host="127.0.0.1",
            port=8080,
        ),
        other_seed,
    )


def test_seed_database_must_be_checkpointed_and_work_free(tmp_path: Path) -> None:
    path = tmp_path / "seed.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE registry_agents (agent_id TEXT PRIMARY KEY);
            CREATE TABLE jobs (job_id TEXT PRIMARY KEY);
            CREATE TABLE work_units (work_unit_id TEXT PRIMARY KEY);
            INSERT INTO registry_agents VALUES ('agent-a');
            """
        )

    assert _inspect_seed_database(path) == frozenset({"agent-a"})

    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO jobs VALUES ('job-a')")
    with pytest.raises(ValueError, match="no jobs or work units"):
        _inspect_seed_database(path)

    Path(f"{path}-wal").touch()
    with pytest.raises(ValueError, match="uncheckpointed sidecar"):
        _inspect_seed_database(path)


def test_churn_history_requires_an_empirical_idle_session(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"agent_id":"agent-a","kind":"user_idle","at":"2026-07-15T09:00:00Z"}\n'
        '{"agent_id":"agent-a","kind":"user_returned",'
        '"at":"2026-07-15T09:05:00Z"}\n',
        encoding="utf-8",
    )

    _validate_churn_history(path)

    path.write_text(
        '{"agent_id":"agent-a","kind":"user_idle","at":"not-a-time"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid completed idle session"):
        _validate_churn_history(path)


@pytest.mark.asyncio
async def test_deadline_keeps_early_phase_alive_and_cancels_late_phase() -> None:
    early = asyncio.create_task(_run_with_deadline(asyncio.sleep(0), 0.05))
    done, _ = await asyncio.wait({early}, timeout=0.01)
    assert done == set()
    await early

    cancelled = False

    async def late() -> None:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    await _run_with_deadline(late(), 0.01)
    assert cancelled is True
