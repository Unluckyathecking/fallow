"""``flw ocr prepare``: normalize source documents into a page-image corpus.

The OCR chunker takes a directory of page images (one work unit per page), so
this module turns whatever the operator has — PDFs, scans, office documents —
into that shape on the submitting machine, keeping the coordinator and agents
free of document-rendering dependencies. ``corpus.json`` maps each original
file to its sha256 and page images so results can be joined back to sources.

Rendering (pypdfium2, from the ``ocr`` extra) and office conversion
(LibreOffice's ``soffice``) are injected seams so tests need neither.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from fallow_cli.errors import CliError

DEFAULT_DPI = 200
# Copied into the corpus verbatim: single-frame formats every vision replica
# decodes and the chunker accepts.
PASSTHROUGH_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
# Accepted as inputs but transcoded to one PNG per frame: container formats
# (multi-page tiff) and formats replicas may reject by media type (bmp).
TRANSCODE_SUFFIXES = frozenset({".bmp", ".tif", ".tiff"})
OFFICE_SUFFIXES = frozenset({".doc", ".docx", ".odt", ".odp", ".ppt", ".pptx", ".rtf"})
_SOFFICE = "soffice"
# Page files are named <sha prefix>-p<index>.<ext>: unique per source, sortable.
_SHA_PREFIX_LEN = 12

# Render a PDF at the given DPI into one PNG per page.
RenderPdfFn = Callable[[Path, int], Iterable[bytes]]
# Convert a non-PDF document into a PDF under the given working directory.
ConvertToPdfFn = Callable[[Path, Path], Path]


def default_render_pdf(pdf_path: Path, dpi: int) -> Iterable[bytes]:
    try:
        import pypdfium2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CliError(
            "pypdfium2 is not installed; install the fallow-cli 'ocr' extra to render PDFs"
        ) from exc
    import io

    document = pypdfium2.PdfDocument(pdf_path)
    for page in document:
        image = page.render(scale=dpi / 72.0).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        yield buffer.getvalue()


def _expand_image_frames(source: Path) -> Iterable[bytes]:
    """Every frame of ``source`` as PNG bytes (pillow, from the ``ocr`` extra)."""
    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise CliError(
            f"pillow is not installed; install the fallow-cli 'ocr' extra to convert {source.name}"
        ) from exc
    import io

    with Image.open(source) as image:
        for frame in ImageSequence.Iterator(image):
            buffer = io.BytesIO()
            frame.convert("RGB").save(buffer, format="PNG")
            yield buffer.getvalue()


def default_convert_to_pdf(source: Path, workdir: Path) -> Path:
    if shutil.which(_SOFFICE) is None:
        raise CliError(f"cannot convert {source.name}: LibreOffice ({_SOFFICE!r}) is not on PATH")
    try:
        subprocess.run(
            [_SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(workdir), str(source)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise CliError(f"conversion failed for {source.name}: {exc}") from exc
    pdf = workdir / f"{source.stem}.pdf"
    if not pdf.is_file():
        raise CliError(f"conversion produced no PDF for {source.name}")
    return pdf


def prepare_corpus(
    inputs: Sequence[Path],
    out_dir: Path,
    *,
    dpi: int = DEFAULT_DPI,
    render_pdf: RenderPdfFn | None = None,
    convert_to_pdf: ConvertToPdfFn | None = None,
) -> dict[str, Any]:
    """Write page images plus ``corpus.json`` into ``out_dir``; return the summary."""
    if not inputs:
        raise CliError("no input files given")
    render = render_pdf or default_render_pdf
    convert = convert_to_pdf or default_convert_to_pdf
    # Page names are content-derived, so pages from an earlier run would
    # silently join the new corpus while corpus.json no longer describes them.
    if out_dir.exists() and any(out_dir.iterdir()):
        raise CliError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    pages_by_sha: dict[str, list[str]] = {}  # identical source bytes render once
    for source in inputs:
        if not source.is_file():
            raise CliError(f"file not found: {source}")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if sha not in pages_by_sha:
            pages_by_sha[sha] = _emit_pages(source, sha, out_dir, dpi, render, convert)
        sources.append({"file": source.name, "sha256": sha, "pages": pages_by_sha[sha]})
    document = {"dpi": dpi, "sources": sources}
    (out_dir / "corpus.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def _emit_pages(
    source: Path,
    sha: str,
    out_dir: Path,
    dpi: int,
    render: RenderPdfFn,
    convert: ConvertToPdfFn,
) -> list[str]:
    suffix = source.suffix.lower()
    prefix = sha[:_SHA_PREFIX_LEN]
    if suffix in PASSTHROUGH_SUFFIXES:
        name = f"{prefix}-p00000{suffix}"
        (out_dir / name).write_bytes(source.read_bytes())
        return [name]
    if suffix in TRANSCODE_SUFFIXES:
        return _write_rendered(source, out_dir, prefix, _expand_image_frames(source))
    if suffix == ".pdf":
        return _write_rendered(source, out_dir, prefix, render(source, dpi))
    if suffix in OFFICE_SUFFIXES:
        with tempfile.TemporaryDirectory() as tmp:
            return _write_rendered(source, out_dir, prefix, render(convert(source, Path(tmp)), dpi))
    raise CliError(f"unsupported input type for OCR: {source.name}")


def _write_rendered(source: Path, out_dir: Path, prefix: str, pages: Iterable[bytes]) -> list[str]:
    names: list[str] = []
    for idx, body in enumerate(pages):
        name = f"{prefix}-p{idx:05d}.png"
        (out_dir / name).write_bytes(body)
        names.append(name)
    if not names:
        raise CliError(f"document rendered to no pages: {source.name}")
    return names
