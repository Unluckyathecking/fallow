"""Append-only JSONL writer for record models.

One writer per output file. Each :meth:`write` serialises a frozen record and
flushes immediately, so a crash mid-run keeps every line already emitted. Under
asyncio (single-threaded) a synchronous write+flush never interleaves with
another task's line, so concurrent interactive request tasks may share one
writer safely.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from fallow_protocol import FallowModel


class JsonlWriter:
    """Serialises frozen records to a newline-delimited JSON file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, record: FallowModel) -> None:
        self._fh.write(record.model_dump_json())
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
