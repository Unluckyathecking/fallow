"""Registry configuration and constants.

The coordinator app constructs a single :class:`RegistryConfig` and hands it to
:class:`~fallow_coordinator.registry.sqlite_registry.SqliteRegistry`. The
liveness thresholds and the ``AgentConfig`` defaults returned at registration
all live here so nothing is hardcoded deeper in the module.
"""

from pydantic import BaseModel, ConfigDict, Field

# Bytes of entropy for every minted token (enrollment, device, api key).
TOKEN_NBYTES = 32

# Liveness thresholds (seconds). An agent whose last heartbeat is older than
# SUSPECT is flagged ``suspect``; older than OFFLINE it is treated as gone.
DEFAULT_SUSPECT_AFTER_S = 15.0
DEFAULT_OFFLINE_AFTER_S = 45.0


class RegistryConfig(BaseModel):
    """Immutable registry settings, including the ``AgentConfig`` defaults.

    ``admin_key`` is the bootstrap super-key; it authenticates as an API key
    with unrestricted model access without a database row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    admin_key: str = Field(min_length=1)

    # AgentConfig defaults handed back at registration.
    heartbeat_interval_s: float = 5.0
    idle_threshold_s: float = 120.0
    poll_interval_ms: int = 100
    vram_evict_after_s: float = 60.0
    bench_mode: bool = False

    # Liveness thresholds consumed by snapshots()/list_offline().
    suspect_after_s: float = DEFAULT_SUSPECT_AFTER_S
    offline_after_s: float = DEFAULT_OFFLINE_AFTER_S
