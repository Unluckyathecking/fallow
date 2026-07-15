"""Persisted agent identity: ``agent_id`` + bearer ``device_token``.

Written once at first-run enrollment and loaded on every subsequent start so a
machine enrolls exactly once. The token is a bearer secret, so the state file is
created with ``0600`` (owner read/write only) via ``os.open`` and written
atomically (temp file in the same directory, then ``os.replace``) so a crash
mid-write never leaves a half-written credential.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fallow_agent.main.errors import IdentityError

_STATE_MODE = 0o600
_TMP_SUFFIX = ".tmp"


class IdentityState(BaseModel):
    """The durable identity of one enrolled agent."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    device_token: str


def load_identity(path: Path) -> IdentityState | None:
    """Return the persisted identity, or ``None`` if this machine is unenrolled.

    Raises :class:`IdentityError` if the file exists but is unreadable or
    malformed (a corrupt credential must fail loudly, not silently re-enroll).
    """
    expanded = path.expanduser()
    if not expanded.exists():
        return None
    try:
        raw = expanded.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"could not read identity file {expanded}: {exc}") from exc
    try:
        return IdentityState.model_validate_json(raw)
    except ValueError as exc:
        raise IdentityError(f"malformed identity file {expanded}: {exc}") from exc


def save_identity(path: Path, state: IdentityState) -> None:
    """Persist ``state`` atomically with ``0600`` permissions."""
    expanded = path.expanduser()
    try:
        expanded.parent.mkdir(parents=True, exist_ok=True)
        tmp = expanded.with_name(expanded.name + _TMP_SUFFIX)
        _write_private(tmp, state.model_dump_json())
        os.replace(tmp, expanded)
    except OSError as exc:
        raise IdentityError(f"could not write identity file {expanded}: {exc}") from exc


def _write_private(path: Path, payload: str) -> None:
    """Create ``path`` with ``0600`` and write ``payload`` (owner-only)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _STATE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
