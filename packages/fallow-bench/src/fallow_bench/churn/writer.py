"""Append-only JSONL sink for executed churn events.

Mirrors the coordinator's ``EventsWriter``: one line per record under an
``asyncio.Lock`` so concurrent verifications never interleave a partial line.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fallow_bench.churn.models import ChurnRecord


class ChurnLog:
    """Serialised, append-only writer for ``churn.jsonl``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def write(self, record: ChurnRecord) -> None:
        line = record.model_dump_json()
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
