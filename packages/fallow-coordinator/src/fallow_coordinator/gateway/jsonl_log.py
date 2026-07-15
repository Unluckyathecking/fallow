"""Append-only JSONL implementation of :class:`RequestLog`.

One JSON object per line, keyed exactly by the :class:`GatewayLogEntry` fields.
Opened in append mode per call so concurrent coordinators (and crash restarts)
never truncate the record. Writes are line-buffered and flushed on close.
"""

from pathlib import Path

from fallow_coordinator.gateway.logentry import GatewayLogEntry


class JsonlRequestLog:
    """Write each :class:`GatewayLogEntry` as one JSON line to ``path``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def log(self, entry: GatewayLogEntry) -> None:
        line = entry.model_dump_json()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
