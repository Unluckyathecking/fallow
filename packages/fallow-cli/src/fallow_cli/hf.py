"""``hf:`` source specs — the one place a Hugging Face path becomes a URL.

``hf:<owner>/<repo>/<file.gguf>[@<revision>]`` resolves to the canonical
resolve URL. Every segment is validated against a conservative character set, so
a spec can never smuggle ``..``, a query string, or an absolute URL into the
download path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = "hf:"
HOST = "https://huggingface.co"
DEFAULT_REVISION = "main"

# Hugging Face owner, repo, file and revision segments. Leading dot excluded so
# ``.`` and ``..`` cannot appear; no slash, colon, question mark or percent, so
# a segment cannot start a new path, query or escape.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class HfSource:
    """A parsed ``hf:`` spec."""

    owner: str
    repo: str
    file_path: str
    revision: str = DEFAULT_REVISION

    @property
    def url(self) -> str:
        return f"{HOST}/{self.owner}/{self.repo}/resolve/{self.revision}/{self.file_path}"

    def __str__(self) -> str:
        return f"{PREFIX}{self.owner}/{self.repo}/{self.file_path}@{self.revision}"


def is_hf_spec(source: str) -> bool:
    return source.startswith(PREFIX)


def parse(source: str) -> HfSource:
    """Parse ``hf:owner/repo/file.gguf[@revision]``; raise ``ValueError`` if invalid."""
    if not is_hf_spec(source):
        raise ValueError("Hugging Face sources start with 'hf:'")
    body, _, revision = source.removeprefix(PREFIX).partition("@")
    revision = revision or DEFAULT_REVISION
    segments = body.split("/")
    if len(segments) < 3:
        raise ValueError(
            f"expected {PREFIX}<owner>/<repo>/<file.gguf>[@<revision>], got {source!r}"
        )
    for segment in (*segments, revision):
        if not _SEGMENT.fullmatch(segment):
            raise ValueError(f"invalid segment {segment!r} in Hugging Face source {source!r}")
    if not segments[-1].endswith(".gguf"):
        raise ValueError(f"Hugging Face source must name a .gguf file, got {source!r}")
    return HfSource(
        owner=segments[0],
        repo=segments[1],
        file_path="/".join(segments[2:]),
        revision=revision,
    )
