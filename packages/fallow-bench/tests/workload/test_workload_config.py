"""Config schema + YAML loader, including the example experiment file."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_bench.workload.config import ExperimentConfig, load_config
from fallow_protocol import WorkerKind

_REPO = Path(__file__).resolve().parents[4]
_MAIN_YAML = _REPO / "experiments" / "main.yaml"

_MINIMAL = """
arm_label: test
coordinator_url: http://127.0.0.1:8080
api_key_env: FLW_API_KEY
model_id: qwen
duration_s: 10
seed: 5
interactive:
  rate_per_min: 30
  max_tokens: 128
  prompt_files: [prompts/rag.txt]
batch:
  corpus_path: corpora/x.jsonl
  submit_at_s: 2
  model_id: bge
sampling:
  admin_key_env: FLW_ADMIN_KEY
"""


def test_example_main_yaml_loads() -> None:
    config = load_config(_MAIN_YAML)
    assert config.arm_label == "roundrobin"
    assert config.interactive.prompt_files[0] == "prompts/rag.txt"
    assert config.batch.kind is WorkerKind.EMBED
    assert config.sampling.admin_poll_hz == 1.0


def test_embedded_churn_section_is_tolerated() -> None:
    # The shared YAML carries a churn: block (module B2); B1 must not reject it.
    config = load_config(_MAIN_YAML)
    assert config.churn is not None


def test_minimal_config_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "arm.yaml"
    path.write_text(_MINIMAL, encoding="utf-8")
    config = load_config(path)
    assert config.batch.poll_interval_s == 10.0
    assert config.interactive.request_timeout_s == 60.0
    assert config.churn is None


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(_MINIMAL + "bogus: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid experiment config"):
        load_config(path)


def test_nonpositive_rate_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(_MINIMAL.replace("rate_per_min: 30", "rate_per_min: 0"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_non_mapping_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_config_is_frozen() -> None:
    config = load_config(_MAIN_YAML)
    with pytest.raises((TypeError, ValueError)):
        config.seed = 9  # type: ignore[misc]


def test_experiment_config_is_exported() -> None:
    assert ExperimentConfig.__name__ == "ExperimentConfig"
