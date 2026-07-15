from pathlib import Path

import pytest

from fallow_bench.analysis import RUN_FILES
from fallow_bench.experiment import ArmName, RunMode, build_plan, create_run_layout


def test_e13_builds_nine_runs_in_stable_arm_then_repetition_order() -> None:
    plan = build_plan()

    assert [(run.arm.name, run.repetition) for run in plan] == [
        (ArmName.DEDICATED, 1),
        (ArmName.DEDICATED, 2),
        (ArmName.DEDICATED, 3),
        (ArmName.ROUND_ROBIN, 1),
        (ArmName.ROUND_ROBIN, 2),
        (ArmName.ROUND_ROBIN, 3),
        (ArmName.CHURN_V2, 1),
        (ArmName.CHURN_V2, 2),
        (ArmName.CHURN_V2, 3),
    ]
    assert all(run.mode is RunMode.LIVE for run in plan)


def test_e13_uses_exact_arm_policies_and_paired_seeds() -> None:
    plan = build_plan()

    assert [(run.arm.name, run.arm.scheduler, run.arm.churn_enabled) for run in plan[::3]] == [
        (ArmName.DEDICATED, "capability", False),
        (ArmName.ROUND_ROBIN, "roundrobin", True),
        (ArmName.CHURN_V2, "churn_v2", True),
    ]
    for repetition in (1, 2, 3):
        assert len({run.seed for run in plan if run.repetition == repetition}) == 1
    assert len({run.seed for run in plan}) == 3
    assert {run.duration_s for run in plan} == {7_200}


def test_e13_layout_names_every_canonical_artifact(tmp_path: Path) -> None:
    run = build_plan(RunMode.SMOKE)[4]

    layout = create_run_layout(tmp_path, run)

    assert layout.directory == tmp_path / "round_robin" / "rep-02"
    assert {path.name for path in layout.artifacts} == {
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
    }
    assert {
        layout.client_trace.name,
        layout.gateway.name,
        layout.events.name,
        layout.units.name,
        layout.churn.name,
        layout.power.name,
        layout.run_meta.name,
    } == set(RUN_FILES.__dict__.values())
    assert run.duration_s == 120


def test_e13_refuses_to_reuse_a_run_directory(tmp_path: Path) -> None:
    run = build_plan()[0]
    create_run_layout(tmp_path, run)

    with pytest.raises(FileExistsError):
        create_run_layout(tmp_path, run)
