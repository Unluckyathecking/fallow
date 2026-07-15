from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from fallow_protocol import FallowModel


class ArmName(StrEnum):
    DEDICATED = "dedicated"
    ROUND_ROBIN = "round_robin"
    CHURN_V2 = "churn_v2"


class RunMode(StrEnum):
    LIVE = "live"
    SMOKE = "smoke"


class ArmSpec(FallowModel):
    name: ArmName
    scheduler: Literal["capability", "roundrobin", "churn_v2"]
    churn_enabled: bool


class RunSpec(FallowModel):
    arm: ArmSpec
    repetition: int = Field(ge=1)
    seed: int
    duration_s: int = Field(gt=0)
    mode: RunMode
