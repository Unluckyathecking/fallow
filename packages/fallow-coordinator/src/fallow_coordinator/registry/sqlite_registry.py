"""SQLite-backed agent registry, auth store, and model catalogue (module C2).

Async (aiosqlite, WAL). The coordinator app owns the connection lifecycle via
``open()``/``close()`` (or ``async with``) and injects ``now`` so liveness maths
and timestamps are deterministic under test.

Invariants
----------
* Tokens are stored only as sha256 hex; the plaintext is returned once.
* Enrollment tokens are single-use: consumption flips ``used_at`` atomically in
  the same transaction that inserts the agent, so a used token can never enrol.
* ``snapshots``/``replica_endpoints`` never surface offline agents (last heartbeat
  older than ``offline_after_s``); ``list_offline`` returns exactly those.
"""

from pathlib import Path
from types import TracebackType
from uuid import uuid4

import aiosqlite

from fallow_coordinator.registry.agents import AgentsMixin
from fallow_coordinator.registry.catalogue import CatalogueMixin
from fallow_coordinator.registry.credentials import CredentialsMixin
from fallow_coordinator.registry.errors import EnrollmentTokenError, ProtocolMismatchError
from fallow_coordinator.registry.serde import dump_caps
from fallow_coordinator.registry.tokens import hash_token
from fallow_coordinator.registry.tunnel_mode import transport_for_mode
from fallow_protocol.messages import AgentConfig, AgentState, RegisterRequest, RegisterResponse
from fallow_protocol.version import PROTOCOL_VERSION

_SCHEMA = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")

_INSERT_AGENT = """
INSERT INTO registry_agents (
    agent_id, hostname, host, caps_json, device_token_hash, state,
    last_seen, user_idle_s, mem_available_mb, gpus_json, replicas_json,
    registered_at, enrollment_mode, transport
) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, '[]', '[]', ?, ?, ?)
"""


class SqliteRegistry(CredentialsMixin, AgentsMixin, CatalogueMixin):
    """Durable registry, auth, and model catalogue for the coordinator."""

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        db = await aiosqlite.connect(self._db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA)
        await self._migrate_api_key_quota_columns(db)
        await self._migrate_serving_paused_column(db)
        await self._migrate_idle_prediction_columns(db)
        await self._migrate_presence_columns(db)
        await self._migrate_token_mode_column(db)
        await self._migrate_revocation_columns(db)
        await db.commit()
        self._db = db

    @staticmethod
    async def _migrate_api_key_quota_columns(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(registry_api_keys)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "rpm_limit" not in columns:
            await db.execute(
                "ALTER TABLE registry_api_keys ADD COLUMN rpm_limit INTEGER"
                " CHECK (rpm_limit IS NULL OR rpm_limit > 0)"
            )
        if "daily_limit" not in columns:
            await db.execute(
                "ALTER TABLE registry_api_keys ADD COLUMN daily_limit INTEGER"
                " CHECK (daily_limit IS NULL OR daily_limit > 0)"
            )

    @staticmethod
    async def _migrate_serving_paused_column(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(registry_agents)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "serving_paused" not in columns:
            await db.execute(
                "ALTER TABLE registry_agents ADD COLUMN serving_paused INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    async def _migrate_idle_prediction_columns(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(registry_agents)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "predicted_idle_remaining_s" not in columns:
            await db.execute(
                "ALTER TABLE registry_agents ADD COLUMN predicted_idle_remaining_s REAL"
            )
        if "predicted_idle_confidence" not in columns:
            await db.execute(
                "ALTER TABLE registry_agents ADD COLUMN predicted_idle_confidence REAL"
            )

    @staticmethod
    async def _migrate_token_mode_column(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(registry_enrollment_tokens)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "mode" not in columns:
            await db.execute(
                "ALTER TABLE registry_enrollment_tokens "
                "ADD COLUMN mode TEXT NOT NULL DEFAULT 'legacy'"
            )

    @staticmethod
    async def _migrate_revocation_columns(db: aiosqlite.Connection) -> None:
        for table in ("registry_agents", "registry_enrollment_tokens"):
            cursor = await db.execute(f"PRAGMA table_info({table})")
            columns = {str(row["name"]) for row in await cursor.fetchall()}
            if "revoked_at" not in columns:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN revoked_at TEXT")

    @staticmethod
    async def _migrate_presence_columns(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(registry_agents)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        for name, definition in (
            ("enrollment_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("transport", "TEXT NOT NULL DEFAULT 'direct'"),
            ("presence_sequence", "INTEGER NOT NULL DEFAULT -1"),
            ("presence_generation", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await db.execute(f"ALTER TABLE registry_agents ADD COLUMN {name} {definition}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "SqliteRegistry":
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ── registration & heartbeats ────────────────────────────────────────────

    async def register_agent(self, request: RegisterRequest, host: str) -> RegisterResponse:
        if request.protocol_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(request.protocol_version, PROTOCOL_VERSION)
        conn = self._conn
        used_at = self._iso_now()
        token_hash = hash_token(request.enrollment_token)
        # Held across the whole token-consuming transaction, both rollbacks
        # included: they are what discards another coroutine's uncommitted
        # revocation on this shared connection (see _write_lock).
        async with self._write_lock:
            token_cur = await conn.execute(
                "SELECT mode FROM registry_enrollment_tokens"
                " WHERE token_hash = ? AND used_at IS NULL",
                (token_hash,),
            )
            token_row = await token_cur.fetchone()
            cur = await conn.execute(
                "UPDATE registry_enrollment_tokens SET used_at = ? "
                "WHERE token_hash = ? AND used_at IS NULL",
                (used_at, token_hash),
            )
            if cur.rowcount != 1:
                await conn.rollback()
                raise EnrollmentTokenError("enrollment token is unknown or already used")
            agent_id = uuid4().hex
            device_token = self._new_token()
            try:
                await conn.execute(
                    _INSERT_AGENT,
                    (
                        agent_id,
                        request.caps.hostname,
                        host,
                        dump_caps(request.caps),
                        hash_token(device_token),
                        AgentState.ACTIVE.value,
                        used_at,
                        used_at,
                        "legacy" if token_row is None else str(token_row["mode"]),
                        transport_for_mode(
                            "legacy" if token_row is None else str(token_row["mode"])
                        ),
                    ),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        assigned = await self.desired_models(agent_id)
        config = self._agent_config(assigned)
        return RegisterResponse(agent_id=agent_id, device_token=device_token, config=config)

    def _agent_config(self, assigned_models: tuple[str, ...]) -> AgentConfig:
        c = self._config
        return AgentConfig(
            heartbeat_interval_s=c.heartbeat_interval_s,
            idle_threshold_s=c.idle_threshold_s,
            poll_interval_ms=c.poll_interval_ms,
            vram_evict_after_s=c.vram_evict_after_s,
            bench_mode=c.bench_mode,
            assigned_models=assigned_models,
        )
