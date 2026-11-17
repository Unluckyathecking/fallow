"""Model catalogue and per-agent model assignments."""

from collections.abc import Sequence

from fallow_coordinator.registry.base import RegistryBase
from fallow_coordinator.registry.records import ModelRecord
from fallow_protocol.models import ModelManifest

_UPSERT_MODEL = """
INSERT INTO registry_models (model_id, manifest_json, blob_path, enabled, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(model_id) DO UPDATE SET
    manifest_json = excluded.manifest_json,
    blob_path     = excluded.blob_path,
    enabled       = excluded.enabled
"""


class CatalogueMixin(RegistryBase):
    """Model manifests and which agents should hold which models."""

    # ── model catalogue & assignments ────────────────────────────────────────

    async def put_model(
        self, manifest: ModelManifest, blob_path: str, enabled: bool = True
    ) -> None:
        await self._conn.execute(
            _UPSERT_MODEL,
            (
                manifest.model_id,
                manifest.model_dump_json(),
                blob_path,
                int(enabled),
                self._iso_now(),
            ),
        )
        await self._conn.commit()

    async def get_manifest(self, model_id: str) -> ModelManifest | None:
        record = await self.get_model(model_id)
        return None if record is None else record.manifest

    async def get_model(self, model_id: str) -> ModelRecord | None:
        cur = await self._conn.execute(
            "SELECT manifest_json, blob_path, enabled FROM registry_models WHERE model_id = ?",
            (model_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return ModelRecord(
            manifest=ModelManifest.model_validate_json(row["manifest_json"]),
            blob_path=str(row["blob_path"]),
            enabled=bool(row["enabled"]),
        )

    async def list_models(self) -> tuple[ModelManifest, ...]:
        cur = await self._conn.execute(
            "SELECT manifest_json FROM registry_models ORDER BY created_at, model_id"
        )
        rows = await cur.fetchall()
        return tuple(ModelManifest.model_validate_json(row["manifest_json"]) for row in rows)

    async def set_assignments(self, agent_id: str, model_ids: Sequence[str]) -> None:
        # Locked for the same reason the token writers are: this is a DELETE and
        # an INSERT reaching one COMMIT, with awaits between them on the shared
        # connection, and revocation clears a desk's models through it. A
        # ``register_agent`` rollback landing in that window discards the DELETE,
        # leaving a revoked agent still assigned and still desired (see
        # ``_write_lock``).
        async with self._write_lock:
            conn = self._conn
            await conn.execute("DELETE FROM registry_assignments WHERE agent_id = ?", (agent_id,))
            # The insert re-checks revocation in the same statement: a caller
            # that read the agent as live just before the revoke committed (the
            # fit sweep, an operator assign) must clear, never re-assign.
            await conn.executemany(
                "INSERT OR IGNORE INTO registry_assignments (model_id, agent_id)"
                " SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM registry_agents"
                " WHERE agent_id = ? AND revoked_at IS NOT NULL)",
                [(model_id, agent_id, agent_id) for model_id in model_ids],
            )
            await conn.commit()

    async def desired_models(self, agent_id: str) -> tuple[str, ...]:
        cur = await self._conn.execute(
            "SELECT model_id FROM registry_assignments WHERE agent_id = ? ORDER BY model_id",
            (agent_id,),
        )
        rows = await cur.fetchall()
        return tuple(str(row["model_id"]) for row in rows)
