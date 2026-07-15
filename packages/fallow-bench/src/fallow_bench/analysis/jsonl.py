"""Tolerant JSON-Lines reading and time coercion.

The reducer must never crash on a producer's bad line, so :func:`read_jsonl`
skips malformed rows with a warning rather than raising. Timestamps may arrive as
epoch seconds or ISO-8601 (the coordinator serialises ``datetime`` as ISO); both
collapse to ``float`` seconds so metric arithmetic is uniform.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(records, warnings)`` for a JSONL file.

    A missing file yields ``([], ["<name> missing"])``; each unparseable or
    non-object line is skipped and noted. The function never raises on content.
    """
    if not path.exists():
        return [], [f"{path.name} missing"]
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    text = path.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"{path.name}:{lineno} not valid JSON, skipped")
            continue
        if not isinstance(obj, dict):
            warnings.append(f"{path.name}:{lineno} not a JSON object, skipped")
            continue
        records.append(obj)
    return records, warnings


def to_seconds(value: Any) -> float | None:
    """Coerce a timestamp field to float epoch/relative seconds, or ``None``.

    Accepts numeric seconds directly and ISO-8601 strings (with or without a
    trailing ``Z``). Anything else returns ``None`` so callers can drop the row.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None
