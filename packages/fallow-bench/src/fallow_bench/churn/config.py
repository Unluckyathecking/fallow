"""Load a :class:`ChurnSection` from YAML.

Accepts either a standalone churn document or an experiment config (module B1)
that embeds the churn slice under a ``churn:`` key. Validation is delegated to
the frozen pydantic models, so a malformed scenario fails loudly at load time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from fallow_bench.churn import constants as k
from fallow_bench.churn.models import ChurnSection


def load_churn_section(path: str | Path) -> ChurnSection:
    """Parse ``path`` and return its validated :class:`ChurnSection`."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return parse_churn_section(raw)


def parse_churn_section(raw: Any) -> ChurnSection:
    """Validate a parsed mapping into a :class:`ChurnSection`.

    If a top-level ``churn:`` key is present (an embedded B1 experiment config),
    that subtree is used; otherwise the whole mapping is treated as the section.
    """
    if not isinstance(raw, dict):
        raise ValueError("churn config must be a mapping")
    section = raw.get(k.CHURN_SECTION_KEY, raw)
    return ChurnSection.model_validate(section)
