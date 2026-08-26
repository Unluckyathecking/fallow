"""The curated model catalog shipped beside this module.

``model_catalog.toml`` lives inside the package rather than under ``deploy/`` so
that ``flw models pull --catalog <id>`` resolves the same way from an installed
wheel as from a repository checkout — a coordinator host installs the CLI, it
does not necessarily hold the source tree.

Entries are parsed as frozen, extra-forbidding models, so a typo in the file is
a load-time error naming the field rather than a silently ignored key.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

from pydantic import Field, ValidationError

from fallow_cli import hf
from fallow_cli.errors import CliError
from fallow_protocol import FallowModel, WorkerKind

CATALOG_FILE = "model_catalog.toml"


class CatalogEntry(FallowModel):
    """One known-good GGUF an operator can pull by id."""

    id: str = Field(min_length=1)
    source: str
    family: str
    quant: str
    worker_kind: WorkerKind
    sha256: str = Field(pattern=r"^([0-9a-f]{64})?$")  # "" until a pull confirms it
    size_bytes: int = Field(gt=0)
    min_ram_mb: int = Field(ge=0)
    min_vram_mb: int = Field(ge=0)
    license: str
    note: str

    @property
    def url(self) -> str:
        return hf.parse(self.source).url


def load_catalog(path: Path | None = None) -> tuple[CatalogEntry, ...]:
    """Parse the catalog (the packaged one unless ``path`` overrides it)."""
    raw = path.read_bytes() if path is not None else _packaged_bytes()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
        entries = tuple(CatalogEntry(**entry) for entry in document.get("model", []))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, TypeError, ValidationError) as exc:
        raise CliError(f"model catalog is unreadable: {exc}") from exc
    for entry in entries:
        try:
            hf.parse(entry.source)
        except ValueError as exc:
            raise CliError(f"model catalog entry {entry.id}: {exc}") from exc
    return entries


def find(entry_id: str, path: Path | None = None) -> CatalogEntry:
    """Look up one entry by id, listing the known ids when it is missing."""
    entries = load_catalog(path)
    for entry in entries:
        if entry.id == entry_id:
            return entry
    known = ", ".join(entry.id for entry in entries) or "(catalog is empty)"
    raise CliError(f"unknown catalog model {entry_id!r}; known ids: {known}")


def _packaged_bytes() -> bytes:
    return (resources.files("fallow_cli") / CATALOG_FILE).read_bytes()
