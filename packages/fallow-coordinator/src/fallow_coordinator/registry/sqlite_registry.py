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

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import aiosqlite

from fallow_coordinator.registry.config import RegistryConfig
from fallow_coordinator.registry.errors import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    RegistryNotOpenError,
    UnknownAgentError,
)
from fallow_coordinator.registry.mapping import ready_endpoints_for_row, snapshot_from_row
from fallow_coordinator.registry.records import ApiKeyInfo, ModelRecord
from fallow_coordinator.registry.serde import dump_caps, dump_gpus, dump_replicas
from fallow_coordinator.registry.tokens import hash_token, new_token, token_matches
from fallow_protocol.messages import (
    AgentConfig,
    AgentSnapshot,
    AgentState,
    Heartbeat,
    RegisterRequest,
    RegisterResponse,
    ReplicaEndpoint,
)
from fallow_protocol.models import ModelManifest
from fallow_protocol.version import PROTOCOL_VERSION

_SCHEMA = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")

_INSERT_AGENT = """
INSERT INTO registry_agents (
    agent_id, hostname, host, caps_json, device_token_hash, state,
    last_seen, user_idle_s, mem_available_mb, gpus_json, replicas_json, registered_at
) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, '[]', '[]', ?)
"""

_UPSERT_MODEL = """
INSERT INTO registry_models (model_id, manifest_json, blob_path, enabled, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(model_id) DO UPDATE SET
    manifest_json = excluded.manifest_json,
    blob_path     = excluded.blob_path,
    enabled       = excluded.enabled
"""


class SqliteRegistry:
    """Durable registry, auth, and model catalogue for the coordinator."""

    def __init__(
        self,
        db_path: str | Path,
        config: RegistryConfig,
        now: Callable[[], datetime],
        token_factory: Callable[[], str] = new_token,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config
        self._now = now
        self._new_token = token_factory
        self._db: aiosqlite.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        db = await aiosqlite.connect(self._db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA)
        await db.commit()
        self._db = db

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

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RegistryNotOpenError("registry connection is not open")
        return self._db

    def _iso_now(self) -> str:
        return self._now().isoformat()

    # ── token issuance ───────────────────────────────────────────────────────

    async def create_enrollment_token(self) -> str:
        token = self._new_token()
        await self._conn.execute(
            "INSERT INTO registry_enrollment_tokens (token_hash, created_at, used_at)"
            " VALUES (?, ?, NULL)",
            (hash_token(token), self._iso_now()),
        )
        await self._conn.commit()
        return token

    async def create_api_key(self, name: str, model_allowlist: Sequence[str] | None = None) -> str:
        key = self._new_token()
        allow_json = None if model_allowlist is None else json.dumps(list(model_allowlist))
        await self._conn.execute(
            "INSERT INTO registry_api_keys"
            " (key_hash, name, model_allowlist_json, created_at, revoked_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (hash_token(key), name, allow_json, self._iso_now()),
        )
        await self._conn.commit()
        return key

    # ── registration & heartbeats ────────────────────────────────────────────

    async def register_agent(self, request: RegisterRequest, host: str) -> RegisterResponse:
        if request.protocol_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(request.protocol_version, PROTOCOL_VERSION)
        conn = self._conn
        used_at = self._iso_now()
        cur = await conn.execute(
            "UPDATE registry_enrollment_tokens SET used_at = ?"
            " WHERE token_hash = ? AND used_at IS NULL",
            (used_at, hash_token(request.enrollment_token)),
        )
        if cur.rowcount != 1:
            await conn.rollback()
            raise EnrollmentTokenError("enrollment token is unknown or already used")
        agent_id = uuid4().hex
        device_token = self._new_token()
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
            ),
        )
        await conn.commit()
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

    async def record_heartbeat(self, agent_id: str, heartbeat: Heartbeat) -> None:
        cur = await self._conn.execute(
            "UPDATE registry_agents SET last_seen = ?, state = ?, user_idle_s = ?,"
            " mem_available_mb = ?, gpus_json = ?, replicas_json = ? WHERE agent_id = ?",
            (
                self._iso_now(),
                heartbeat.state.value,
                heartbeat.user_idle_s,
                heartbeat.mem_available_mb,
                dump_gpus(heartbeat.gpus),
                dump_replicas(heartbeat.replicas),
                agent_id,
            ),
        )
        await self._conn.commit()
        if cur.rowcount != 1:
            raise UnknownAgentError(agent_id)

    # ── authentication ───────────────────────────────────────────────────────

    async def authenticate_agent(self, bearer: str) -> str | None:
        cur = await self._conn.execute(
            "SELECT agent_id FROM registry_agents WHERE device_token_hash = ?",
            (hash_token(bearer),),
        )
        row = await cur.fetchone()
        return None if row is None else str(row["agent_id"])

    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None:
        if token_matches(bearer, hash_token(self._config.admin_key)):
            return ApiKeyInfo(name="admin", model_allowlist=None, is_admin=True)
        cur = await self._conn.execute(
            "SELECT name, model_allowlist_json FROM registry_api_keys"
            " WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_token(bearer),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        raw = row["model_allowlist_json"]
        allowlist = None if raw is None else tuple(json.loads(raw))
        return ApiKeyInfo(name=str(row["name"]), model_allowlist=allowlist, is_admin=False)

    # ── liveness views ───────────────────────────────────────────────────────

    async def snapshots(self, now: datetime) -> tuple[AgentSnapshot, ...]:
        cur = await self._conn.execute("SELECT * FROM registry_agents ORDER BY registered_at")
        rows = await cur.fetchall()
        out: list[AgentSnapshot] = []
        for row in rows:
            age = self._age_s(now, row["last_seen"])
            if age > self._config.offline_after_s:
                continue
            out.append(snapshot_from_row(row, suspect=age > self._config.suspect_after_s))
        return tuple(out)

    async def list_offline(self, now: datetime) -> tuple[str, ...]:
        cur = await self._conn.execute("SELECT agent_id, last_seen FROM registry_agents")
        rows = await cur.fetchall()
        return tuple(
            str(row["agent_id"])
            for row in rows
            if self._age_s(now, row["last_seen"]) > self._config.offline_after_s
        )

    async def replica_endpoints(self, model_id: str, now: datetime) -> tuple[ReplicaEndpoint, ...]:
        cur = await self._conn.execute("SELECT * FROM registry_agents")
        rows = await cur.fetchall()
        out: list[ReplicaEndpoint] = []
        for row in rows:
            if self._age_s(now, row["last_seen"]) > self._config.suspect_after_s:
                continue  # suspect or offline agents cannot serve interactive traffic
            if AgentState(row["state"]) != AgentState.IDLE:
                continue
            out.extend(ready_endpoints_for_row(row, model_id))
        return tuple(out)

    @staticmethod
    def _age_s(now: datetime, last_seen: str) -> float:
        return (now - datetime.fromisoformat(last_seen)).total_seconds()

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
        conn = self._conn
        await conn.execute("DELETE FROM registry_assignments WHERE agent_id = ?", (agent_id,))
        await conn.executemany(
            "INSERT OR IGNORE INTO registry_assignments (model_id, agent_id) VALUES (?, ?)",
            [(model_id, agent_id) for model_id in model_ids],
        )
        await conn.commit()

    async def desired_models(self, agent_id: str) -> tuple[str, ...]:
        cur = await self._conn.execute(
            "SELECT model_id FROM registry_assignments WHERE agent_id = ? ORDER BY model_id",
            (agent_id,),
        )
        rows = await cur.fetchall()
        return tuple(str(row["model_id"]) for row in rows)
