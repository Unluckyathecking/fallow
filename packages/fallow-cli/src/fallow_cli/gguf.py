"""Minimal GGUF header reader — stdlib only, header bytes only.

Model files are multi-GB, so nothing here reads past the metadata block: the
magic is checked first and a non-GGUF file is rejected on the first four bytes.
Values we do not need (arrays, floats) are seeked over rather than materialised,
so a 150k-entry tokenizer costs seeks, not memory.

Only GGUF v2/v3 are read. v1 used 32-bit lengths for strings and counts, so its
layout is a different parser; it is rejected by name rather than misread.

Every malformed input raises :class:`GgufError`. Callers treat that as "no
derived metadata" and fall back to operator-supplied flags — a header we cannot
read is never a reason to fail a download.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import BinaryIO

MAGIC = b"GGUF"
_SUPPORTED_VERSIONS = (2, 3)

# Bounds on a plausible header. Generous enough for real models (Qwen2.5 carries
# ~30 KVs and a 152k-token vocabulary), finite so a corrupt length cannot make us
# allocate or spin.
_MAX_KV = 4096
_MAX_STRING = 1 << 20
_MAX_ARRAY = 1 << 24

_TYPE_STRING = 8
_TYPE_ARRAY = 9
# value type -> (byte width, signed)
_INT_TYPES = {
    0: (1, False),  # uint8
    1: (1, True),  # int8
    2: (2, False),  # uint16
    3: (2, True),  # int16
    4: (4, False),  # uint32
    5: (4, True),  # int32
    7: (1, False),  # bool
    10: (8, False),  # uint64
    11: (8, True),  # int64
}
_FLOAT_TYPES = {6: 4, 12: 8}

# llama.cpp's ``llama_ftype`` as written to ``general.file_type``. Only the
# values that name a quantisation an operator would recognise are mapped; an
# unlisted or withdrawn ftype derives no quant and the flag stays required.
FILE_TYPES = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    21: "Q2_K_S",
    22: "IQ3_XS",
    23: "IQ3_XXS",
    24: "IQ1_S",
    25: "IQ4_NL",
    26: "IQ3_S",
    27: "IQ3_M",
    28: "IQ2_S",
    29: "IQ2_M",
    30: "IQ4_XS",
    31: "IQ1_M",
    32: "BF16",
    36: "TQ1_0",
    37: "TQ2_0",
}

_MIB = 1024 * 1024
# Weights are the floor, not the cost. llama.cpp also holds the KV cache, the
# compute buffers and its own scratch on top of the mapped file; at the small
# contexts a desk pilot runs (a few thousand tokens) that lands well inside a
# fifth of the weights for the sizes in the catalog. 15% of the file covers it
# with room to spare, and the flat term covers the runtime, the tokenizer and
# the loader's own peak, which do not scale with the file. Both numbers are
# deliberately blunt: ADR 048 only ever compares this against an agent's free
# RAM, so erring high costs one skipped desk while erring low costs a swap
# storm. A long context or a large batch is the operator's to declare with
# ``--min-ram-mb``.
_WEIGHT_HEADROOM = 1.15
_RUNTIME_OVERHEAD_MB = 512


class GgufError(Exception):
    """The file is not a readable GGUF v2/v3 header."""


@dataclass(frozen=True)
class GgufHeader:
    """The handful of header facts the CLI derives model metadata from."""

    version: int
    tensor_count: int
    architecture: str | None = None
    name: str | None = None
    file_type: int | None = None

    @property
    def quant(self) -> str | None:
        """The quantisation name for ``general.file_type``, when it is known."""
        if self.file_type is None:
            return None
        return FILE_TYPES.get(self.file_type)


def derive_min_ram_mb(size_bytes: int) -> int:
    """Conservative RAM floor for serving a blob of ``size_bytes`` (see above)."""
    return ceil(size_bytes / _MIB * _WEIGHT_HEADROOM) + _RUNTIME_OVERHEAD_MB


def read_header(path: Path) -> GgufHeader:
    """Read ``path``'s GGUF header; raise :class:`GgufError` if it is not one."""
    try:
        with path.open("rb") as fh:
            return _read(_Reader(fh))
    except OSError as exc:
        raise GgufError(f"could not read {path}: {exc}") from exc


class _Reader:
    """Exact reads and bounds-checked skips over the head of a file."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self._size = os.fstat(fh.fileno()).st_size

    def read(self, count: int) -> bytes:
        raw = self._fh.read(count)
        if len(raw) != count:
            raise GgufError("truncated GGUF header")
        return raw

    def uint(self, width: int, *, signed: bool = False) -> int:
        return int.from_bytes(self.read(width), "little", signed=signed)

    def skip(self, count: int) -> None:
        # A seek past EOF succeeds silently, so truncation is checked here rather
        # than being discovered several fields later.
        target = self._fh.tell() + count
        if count < 0 or target > self._size:
            raise GgufError("truncated GGUF header")
        self._fh.seek(target)


def _read(reader: _Reader) -> GgufHeader:
    if reader.read(4) != MAGIC:
        raise GgufError("not a GGUF file")
    version = reader.uint(4)
    if version not in _SUPPORTED_VERSIONS:
        raise GgufError(f"unsupported GGUF version {version} (this reader handles 2 and 3)")
    tensor_count = reader.uint(8)
    kv_count = reader.uint(8)
    if kv_count > _MAX_KV:
        raise GgufError(f"implausible GGUF metadata count {kv_count}")
    wanted = {"general.architecture", "general.name", "general.file_type"}
    found: dict[str, str | int] = {}
    for _ in range(kv_count):
        key = _read_string(reader)
        value = _read_value(reader, reader.uint(4))
        if key in wanted and value is not None:
            found[key] = value
    architecture = found.get("general.architecture")
    name = found.get("general.name")
    file_type = found.get("general.file_type")
    return GgufHeader(
        version=version,
        tensor_count=tensor_count,
        architecture=architecture if isinstance(architecture, str) else None,
        name=name if isinstance(name, str) else None,
        file_type=file_type if isinstance(file_type, int) else None,
    )


def _read_string(reader: _Reader) -> str:
    length = reader.uint(8)
    if length > _MAX_STRING:
        raise GgufError(f"implausible GGUF string length {length}")
    return reader.read(length).decode("utf-8", errors="replace")


def _read_value(reader: _Reader, value_type: int) -> str | int | None:
    """Return the value when it is a string or integer; skip everything else."""
    if value_type == _TYPE_STRING:
        return _read_string(reader)
    if value_type == _TYPE_ARRAY:
        _skip_array(reader)
        return None
    if value_type in _INT_TYPES:
        width, signed = _INT_TYPES[value_type]
        return reader.uint(width, signed=signed)
    if value_type in _FLOAT_TYPES:
        reader.skip(_FLOAT_TYPES[value_type])
        return None
    raise GgufError(f"unknown GGUF value type {value_type}")


def _skip_array(reader: _Reader) -> None:
    element_type = reader.uint(4)
    count = reader.uint(8)
    if count > _MAX_ARRAY:
        raise GgufError(f"implausible GGUF array length {count}")
    if element_type == _TYPE_STRING:
        for _ in range(count):
            reader.skip(reader.uint(8))
        return
    if element_type in _INT_TYPES:
        reader.skip(_INT_TYPES[element_type][0] * count)
        return
    if element_type in _FLOAT_TYPES:
        reader.skip(_FLOAT_TYPES[element_type] * count)
        return
    raise GgufError(f"unknown GGUF array element type {element_type}")
