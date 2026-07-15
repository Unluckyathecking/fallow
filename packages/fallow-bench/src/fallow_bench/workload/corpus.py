"""Prompt-corpus loading for the interactive workload.

Prompt files are plain text, one prompt per line; blank lines are skipped. All
listed files are concatenated in order into a single indexed corpus that the
arrival schedule's ``prompt_idx`` selects from.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def load_prompts(paths: Sequence[Path]) -> tuple[str, ...]:
    """Load and concatenate prompt lines from ``paths`` in order."""
    prompts: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        prompts.extend(line.strip() for line in text.splitlines() if line.strip())
    if not prompts:
        raise ValueError(f"no prompts found in {[str(p) for p in paths]}")
    return tuple(prompts)
