from __future__ import annotations

from fallow_bench.experiment.models import ArmName, ArmSpec, RunMode, RunSpec

DEFAULT_REPETITIONS = 3
LIVE_DURATION_S = 7_200
SMOKE_DURATION_S = 120
PAIRED_SEEDS = (17, 29, 43)

ARMS = (
    ArmSpec(name=ArmName.DEDICATED, scheduler="capability", churn_enabled=False),
    ArmSpec(name=ArmName.ROUND_ROBIN, scheduler="roundrobin", churn_enabled=True),
    ArmSpec(name=ArmName.CHURN_V2, scheduler="churn_v2", churn_enabled=True),
)


def build_plan(mode: RunMode = RunMode.LIVE) -> tuple[RunSpec, ...]:
    duration_s = LIVE_DURATION_S if mode is RunMode.LIVE else SMOKE_DURATION_S
    return tuple(
        RunSpec(
            arm=arm,
            repetition=repetition,
            seed=PAIRED_SEEDS[repetition - 1],
            duration_s=duration_s,
            mode=mode,
        )
        for arm in ARMS
        for repetition in range(1, DEFAULT_REPETITIONS + 1)
    )
