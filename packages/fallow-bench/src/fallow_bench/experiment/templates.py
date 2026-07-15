from __future__ import annotations

import json
from pathlib import Path
from string import Template

from fallow_bench.experiment.layout import RunLayout
from fallow_bench.experiment.models import ArmSpec


def render_coordinator_config(
    template_root: Path,
    layout: RunLayout,
    arm: ArmSpec,
    *,
    admin_key: str,
    host: str,
    port: int,
) -> Path:
    """Render one arm's coordinator config into its isolated run directory."""
    template_path = template_root / f"{arm.name}.toml.in"
    template = Template(template_path.read_text(encoding="utf-8"))
    values: dict[str, str] = {
        "db_path": _toml_string(layout.database),
        "blob_dir": _toml_string(layout.blobs),
        "unit_input_dir": _toml_string(layout.unit_inputs),
        "result_dir": _toml_string(layout.results),
        "events_jsonl_path": _toml_string(layout.events),
        "gateway_log_path": _toml_string(layout.gateway),
        "admin_key": _toml_string(admin_key),
        "host": _toml_string(host),
        "port": str(port),
    }
    layout.coordinator_config.write_text(template.substitute(values), encoding="utf-8")
    return layout.coordinator_config


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))
