"""Local blob handling: sha256 hashing, resumable-free streaming download, and
manifest construction shared by ``flw models register`` and ``flw models pull``.

Hashing streams the file so multi-GB weights never load into memory. Downloads
render a progress bar on **stderr** so ``--json`` stdout stays clean.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from fallow_cli.errors import CliError
from fallow_protocol import ModelManifest, WorkerKind

BLOB_DIR = Path.home() / ".fallow" / "blobs"
_READ_CHUNK = 1024 * 1024


def hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hexdigest, size_bytes)`` by streaming ``path``."""
    if not path.is_file():
        raise CliError(f"file not found: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        raise CliError(f"file is empty: {path}")
    return digest.hexdigest(), size


def build_manifest(
    *,
    path: Path,
    model_id: str,
    family: str,
    quant: str,
    worker_kind: WorkerKind,
    min_ram_mb: int,
    min_vram_mb: int,
    source_url: str | None = None,
) -> ModelManifest:
    """Hash the local file and assemble a validated :class:`ModelManifest`."""
    sha256, size_bytes = hash_file(path)
    return ModelManifest(
        model_id=model_id,
        family=family,
        quant=quant,
        worker_kind=worker_kind,
        file_name=path.name,
        sha256=sha256,
        size_bytes=size_bytes,
        min_ram_mb=min_ram_mb,
        min_vram_mb=min_vram_mb,
        source_url=source_url,
    )


def dest_for(url: str, model_id: str) -> Path:
    """Choose a download destination under ``BLOB_DIR`` from the URL basename."""
    name = Path(httpx.URL(url).path).name or model_id
    return BLOB_DIR / name


def download_to(client: httpx.Client, url: str, dest: Path, console: Console) -> Path:
    """Stream ``url`` to ``dest`` with a stderr progress bar; return ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _stream(client, url, dest, console)
    except httpx.RequestError as exc:
        raise CliError(f"could not download {url}: {exc}") from exc
    return dest


def _stream(client: httpx.Client, url: str, dest: Path, console: Console) -> None:
    with client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise CliError(f"download failed: {url} returned HTTP {resp.status_code}")
        total = int(resp.headers.get("content-length", 0)) or None
        with _progress(console) as progress, dest.open("wb") as fh:
            task = progress.add_task(f"downloading {dest.name}", total=total)
            for chunk in resp.iter_bytes():
                fh.write(chunk)
                progress.update(task, advance=len(chunk))


def _progress(console: Console) -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
        transient=True,
    )
