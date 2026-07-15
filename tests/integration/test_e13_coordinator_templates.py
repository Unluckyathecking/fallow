from __future__ import annotations

from pathlib import Path

import pytest

from fallow_bench.experiment import ARMS, RunMode, RunSpec, create_run_layout
from fallow_bench.experiment.models import ArmSpec
from fallow_bench.experiment.templates import render_coordinator_config
from fallow_coordinator.app import CoordinatorConfig, load_config

_TEMPLATE_ROOT = Path(__file__).parents[2] / "experiments" / "arms"


@pytest.mark.parametrize("arm", ARMS, ids=lambda arm: arm.name)
def test_arm_template_loads_with_isolated_scheduler_and_paths(tmp_path: Path, arm: ArmSpec) -> None:
    run = RunSpec(arm=arm, repetition=1, seed=101, duration_s=60, mode=RunMode.SMOKE)
    layout = create_run_layout(tmp_path, run)

    rendered = render_coordinator_config(
        _TEMPLATE_ROOT,
        layout,
        arm,
        admin_key="experiment-secret",
        host="127.0.0.9",
        port=9101,
    )
    config = load_config(rendered)

    assert config.scheduler == arm.scheduler
    assert config.admin_key == "experiment-secret"
    assert (config.host, config.port) == ("127.0.0.9", 9101)
    assert {
        config.db_path,
        config.blob_dir,
        config.unit_input_dir,
        config.result_dir,
        config.events_jsonl_path,
        config.gateway_log_path,
    } == {
        layout.database,
        layout.blobs,
        layout.unit_inputs,
        layout.results,
        layout.events,
        layout.gateway,
    }
    assert all(path.is_relative_to(layout.directory) for path in _mutable_paths(config))


def _mutable_paths(config: CoordinatorConfig) -> tuple[Path, ...]:
    return (
        config.db_path,
        config.blob_dir,
        config.unit_input_dir,
        config.result_dir,
        config.events_jsonl_path,
        config.gateway_log_path,
    )
