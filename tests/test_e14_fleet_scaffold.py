from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="fleet scripts target Linux hosts")
def test_fleet_scaffold_is_secret_free_and_offline() -> None:
    script = (
        Path(__file__).parents[1] / "experiments" / "fleet" / "tests" / "test_fleet_scaffold.sh"
    )
    subprocess.run(["sh", str(script)], check=True)
