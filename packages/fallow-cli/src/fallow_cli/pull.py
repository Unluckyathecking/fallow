"""Resolution for ``flw models pull``: where a blob comes from and what its
manifest should say.

Two stages, because half the answer is only knowable after the bytes land.
:func:`plan_source` turns a URL, an ``hf:`` spec or a catalog id into one URL
plus whatever the catalog already knows. :func:`resolve_fields` fills the rest in
once the file exists, reading the GGUF header for the quantisation and sizing the
RAM floor off the file. Operator flags win over the catalog, and the catalog wins
over anything derived from the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fallow_cli import hf
from fallow_cli.catalog import CatalogEntry, find
from fallow_cli.errors import CliError
from fallow_cli.gguf import GgufError, GgufHeader, derive_min_ram_mb, read_header
from fallow_protocol import ModelManifest, WorkerKind


@dataclass(frozen=True)
class Overrides:
    """Manifest fields the operator gave on the command line."""

    model_id: str | None = None
    family: str | None = None
    quant: str | None = None
    worker_kind: WorkerKind | None = None
    min_ram_mb: int | None = None
    min_vram_mb: int | None = None


@dataclass(frozen=True)
class PullPlan:
    """One download URL, its human-readable origin, and the catalog entry (if any)."""

    url: str
    origin: str
    entry: CatalogEntry | None = None

    @property
    def expected_sha256(self) -> str | None:
        """The hash the blob must have, when the catalog records one."""
        if self.entry is None or not self.entry.sha256:
            return None
        return self.entry.sha256


@dataclass(frozen=True)
class Fields:
    """The manifest metadata, fully resolved."""

    model_id: str
    family: str
    quant: str
    worker_kind: WorkerKind
    min_ram_mb: int
    min_vram_mb: int
    license: str | None


def plan_source(source: str | None, catalog_id: str | None) -> PullPlan:
    """Resolve exactly one of a source spec or a catalog id into a plan."""
    if source is not None and catalog_id is not None:
        raise CliError("pass a source URL / hf: spec or --catalog <id>, not both")
    if source is not None:
        if not hf.is_hf_spec(source):
            return PullPlan(url=source, origin=source)
        try:
            parsed = hf.parse(source)
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        return PullPlan(url=parsed.url, origin=str(parsed))
    if catalog_id is None:
        raise CliError("give a source URL / hf: spec, or --catalog <id>")
    entry = find(catalog_id)
    return PullPlan(url=entry.url, origin=entry.source, entry=entry)


def preflight(plan: PullPlan, overrides: Overrides) -> None:
    """Refuse a pull that cannot produce a manifest, before a byte is downloaded.

    ``resolve_fields`` catches the same two omissions, but only once the file has
    landed — a gigabyte of download followed by a usage error. Nothing but the
    catalog can supply an id or a family, so both are knowable up front.
    """
    if plan.entry is not None:
        return
    if overrides.model_id is None:
        raise CliError("--model-id is required unless the source is a catalog entry")
    if overrides.family is None:
        raise CliError("--family is required unless the source is a catalog entry")


def resolve_fields(plan: PullPlan, path: Path, overrides: Overrides) -> Fields:
    """Combine flags, catalog and GGUF header into the manifest's metadata."""
    entry = plan.entry
    header, header_error = _read_header(path)

    model_id = overrides.model_id or (entry.id if entry else None)
    if model_id is None:
        raise CliError("--model-id is required unless the source is a catalog entry")
    family = overrides.family or (entry.family if entry else None)
    if family is None:
        raise CliError("--family is required unless the source is a catalog entry")
    derived_quant = header.quant if header else None
    quant = overrides.quant or (entry.quant if entry else None) or derived_quant
    if quant is None:
        reason = _quant_reason(header, header_error)
        raise CliError(f"could not derive --quant from {path.name} ({reason}); pass --quant")

    if overrides.worker_kind is not None:
        worker_kind = overrides.worker_kind
    else:
        worker_kind = entry.worker_kind if entry else WorkerKind.CHAT
    return Fields(
        model_id=model_id,
        family=family,
        quant=quant,
        worker_kind=worker_kind,
        min_ram_mb=_pick_int(
            overrides.min_ram_mb,
            entry.min_ram_mb if entry else None,
            derive_min_ram_mb(path.stat().st_size),
        ),
        # No GPU placement is ever guessed: a model stays CPU-only unless the
        # operator declares a floor, because a non-zero value here is what makes
        # ADR 048's auto-assign prefer a GPU desk.
        min_vram_mb=_pick_int(overrides.min_vram_mb, entry.min_vram_mb if entry else None, 0),
        license=entry.license if entry else None,
    )


def resolve_fields_or_discard(plan: PullPlan, path: Path, overrides: Overrides) -> Fields:
    """:func:`resolve_fields`, deleting the downloaded file when it cannot answer.

    The download has already landed by the time this runs, and a pull that cannot
    build a manifest is over. There is no resume: the operator's next attempt
    re-downloads the file whatever happens here, so a multi-GB file left behind
    only fills the disk of the machine they are standing at. :func:`verify`
    deletes on the same principle.

    ``path`` is the unverified ``.part``, never a registered blob, so this only
    ever removes bytes that were downloaded during this run.
    """
    try:
        return resolve_fields(plan, path, overrides)
    except CliError as exc:
        path.unlink(missing_ok=True)
        raise CliError(f"{exc.message} (downloaded file {path} deleted)") from exc


def verify(manifest: ModelManifest, plan: PullPlan, path: Path) -> None:
    """Refuse a download whose hash contradicts the catalog, and delete it.

    ``path`` is the ``.part``, which has not taken the destination's name yet, so
    refusing here costs the operator this download and nothing else. Any blob
    already registered at that destination is untouched — which is the point: a
    mismatch means the file that just arrived is the suspect one, not the one
    that has been serving.
    """
    expected = plan.expected_sha256
    if expected is None or manifest.sha256 == expected:
        return
    path.unlink(missing_ok=True)
    raise CliError(
        f"sha256 mismatch for {plan.origin}: catalog says {expected}, "
        f"downloaded file is {manifest.sha256} (download deleted; any blob "
        f"already registered at this path is unchanged)"
    )


def provenance_line(manifest: ModelManifest, plan: PullPlan) -> str:
    """One log line recording where a registered blob came from.

    ``ModelManifest`` has no free-form field and adding one would ripple into the
    exported schemas and the Go codegen, so the parts that already fit —
    ``source_url`` and ``license`` — go in the manifest, and the rest is stated
    here, once, at the moment of registration.
    """
    return (
        f"registered {manifest.model_id} from {plan.origin} "
        f"quant={manifest.quant} min_ram_mb={manifest.min_ram_mb} "
        f"min_vram_mb={manifest.min_vram_mb} license={manifest.license or 'unstated'} "
        f"sha256={manifest.sha256}"
    )


def _pick_int(override: int | None, from_catalog: int | None, derived: int) -> int:
    if override is not None:
        return override
    return from_catalog if from_catalog is not None else derived


def _read_header(path: Path) -> tuple[GgufHeader | None, str]:
    """The header, or ``None`` plus the reason it could not be read."""
    try:
        return read_header(path), ""
    except GgufError as exc:
        return None, str(exc)


def _quant_reason(header: GgufHeader | None, header_error: str) -> str:
    """Why the file yielded no quantisation, in terms the operator can act on.

    A header that parsed leaves ``header_error`` empty, so the three cases have
    to be told apart here — the message read ``(...)`` with nothing between the
    brackets for the two where the file was fine and only the ftype was not.
    """
    if header is None:
        return header_error
    if header.file_type is None:
        return "its header has no general.file_type key"
    return (
        f"its header sets general.file_type {header.file_type}, which names no known quantisation"
    )
