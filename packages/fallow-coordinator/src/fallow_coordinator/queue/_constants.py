"""Tunable constants for the SQLite queue store.

Kept out of the store module so nothing is hardcoded at a call site.
"""

from typing import Final

# Lease sizing: a unit's lease lasts long enough to actually run it.
LEASE_EST_MULTIPLIER: Final[float] = 2.0
DEFAULT_LEASE_S: Final[float] = 120.0

# Retry budget: a unit may be leased this many times before it goes DEAD.
DEFAULT_MAX_ATTEMPTS: Final[int] = 4

# Connection pragmas. WAL gives crash-safe concurrent reads; busy_timeout keeps
# a briefly-locked writer from raising instead of waiting.
BUSY_TIMEOUT_MS: Final[int] = 5000

CONNECTION_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)

SCHEMA_FILENAME: Final[str] = "schema.sql"
