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

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import NamedTuple
from uuid import uuid4

import aiosqlite

from fallow_coordinator.registry.config import RegistryConfig
from fallow_coordinator.registry.errors import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    RegistryNotOpenError,
    RevokedAgentError,
    UnknownAgentError,
)
from fallow_coordinator.registry.mapping import ready_endpoints_for_row, snapshot_from_row
from fallow_coordinator.registry.records import (
    ApiKeyInfo,
    ApiKeyQuotaSnapshot,
    EnrollmentTokenInfo,
    ModelRecord,
    RevokedAgentInfo,
)
from fallow_coordinator.registry.serde import dump_caps, dump_gpus, dump_replicas
from fallow_coordinator.registry.tokens import (
    TOKEN_ID_CHARS,
    hash_token,
    new_token,
    normalize_token_id,
    token_matches,
)
from fallow_coordinator.registry.tunnel_mode import (
    EnrollmentMode,
    Transport,
    transport_for_mode,
)
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
    last_seen, user_idle_s, mem_available_mb, gpus_json, replicas_json,
    registered_at, enrollment_mode, transport
) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, '[]', '[]', ?, ?, ?)
"""

_UPSERT_MODEL = """
INSERT INTO registry_models (model_id, manifest_json, blob_path, enabled, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(model_id) DO UPDATE SET
    manifest_json = excluded.manifest_json,
    blob_path     = excluded.blob_path,
    enabled       = excluded.enabled
"""


class SiteFleetEntry(NamedTuple):
    """One Site Mode agent's persisted enrollment facts plus its heartbeat age."""

    agent_id: str
    enrollment_mode: EnrollmentMode
    transport: Transport
    heartbeat_age_s: float
    presence_generation: int
    revoked: bool


def _token_state(used_at: str | None, revoked_at: str | None) -> str:
    if revoked_at is not None:
        return "revoked"
    return "used" if used_at is not None else "outstanding"


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
        # Serialises the writes that span more than one statement. aiosqlite runs
        # each statement on one worker thread, so no statement tears, but a
        # transaction is not a statement: every ``await`` between them is a point
        # where another coroutine runs on this same connection, inside this same
        # implicit transaction. ``register_agent`` rolls back there, and a
        # rollback discards whatever else is uncommitted — a revocation whose
        # ``UPDATE`` had landed but not yet committed was silently undone while
        # its route still answered 204.
        #
        # These four writers are the ones that must not interleave: the three
        # that consume or void a token, and ``set_assignments``, whose DELETE and
        # INSERTs are what revocation clears a desk's models with. The rest
        # commit a single statement, which can only commit a pending revocation
        # early, never unwind it; the rollback is what destroys work, and it
        # lives only in ``register_agent``.
        self._write_lock = asyncio.Lock()

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

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RegistryNotOpenError("registry connection is not open")
        return self._db

    def _iso_now(self) -> str:
        return self._now().isoformat()

    # ── token issuance ───────────────────────────────────────────────────────

    async def create_enrollment_token(self, *, mode: str = "legacy") -> str:
        if mode not in ("legacy", "site"):
            raise ValueError("mode must be legacy or site")
        token = self._new_token()
        await self._conn.execute(
            "INSERT INTO registry_enrollment_tokens (token_hash, created_at, used_at, mode)"
            " VALUES (?, ?, NULL, ?)",
            (hash_token(token), self._iso_now(), mode),
        )
        await self._conn.commit()
        return token

    async def list_enrollment_tokens(self) -> tuple[EnrollmentTokenInfo, ...]:
        """Every minted enrollment token by its public id, never its secret."""
        cur = await self._conn.execute(
            "SELECT token_hash, created_at, used_at, mode, revoked_at"
            " FROM registry_enrollment_tokens ORDER BY created_at, token_hash"
        )
        rows = await cur.fetchall()
        return tuple(
            EnrollmentTokenInfo(
                token_id=str(row["token_hash"])[:TOKEN_ID_CHARS],
                mode=str(row["mode"]),
                state=_token_state(row["used_at"], row["revoked_at"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    async def revoke_enrollment_token(self, token_id: str) -> bool:
        """Void an unused enrollment token; False when there is none to void.

        Revocation spends the token through the same ``used_at`` gate a real
        enrollment does, so a join file carrying it fails afterwards exactly as
        a re-used one does. ``revoked_at`` records that an operator spent it, so
        the listing can tell the two apart.

        The id is normalized first, and the row it names is resolved before
        anything is written: a prefix is only a name while it is unique, and an
        ``UPDATE`` on a prefix that matched two rows would void a token the
        operator never asked about. Raises :class:`EnrollmentTokenError` when the
        id is malformed or ambiguous — neither is "nothing to void".
        """
        normalized = normalize_token_id(token_id)
        # The lock covers the resolve as well as the write: a token this run
        # decided was unique must not be enrolled out from under the UPDATE that
        # follows, and the UPDATE must reach its COMMIT with no registration
        # rollback in between (see _write_lock).
        async with self._write_lock:
            cur = await self._conn.execute(
                "SELECT token_hash FROM registry_enrollment_tokens"
                " WHERE substr(token_hash, 1, ?) = ? AND used_at IS NULL LIMIT 2",
                (TOKEN_ID_CHARS, normalized),
            )
            matches = [str(row["token_hash"]) for row in await cur.fetchall()]
            if not matches:
                return False
            if len(matches) > 1:
                raise EnrollmentTokenError(
                    f"token id {normalized} names more than one outstanding token; "
                    "refusing to guess which"
                )
            now = self._iso_now()
            cur = await self._conn.execute(
                "UPDATE registry_enrollment_tokens SET used_at = ?, revoked_at = ?"
                " WHERE token_hash = ? AND used_at IS NULL",
                (now, now, matches[0]),
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def revoke_agent(self, agent_id: str) -> None:
        """Revoke an enrolled agent's device token. Idempotent and terminal.

        Every later call presenting that token fails ``authenticate_agent``, and
        the agent leaves the routing views at once. There is no un-revoke: a
        wiped machine re-enrolls from a fresh join file as a new agent.
        """
        # Same window as revoke_enrollment_token: the UPDATE must reach its
        # COMMIT with no registration rollback in between (see _write_lock).
        async with self._write_lock:
            cur = await self._conn.execute(
                "UPDATE registry_agents SET revoked_at = COALESCE(revoked_at, ?)"
                " WHERE agent_id = ?",
                (self._iso_now(), agent_id),
            )
            await self._conn.commit()
        if cur.rowcount != 1:
            raise UnknownAgentError(agent_id)

    async def list_revoked_agents(self) -> tuple[RevokedAgentInfo, ...]:
        """Every revoked agent, oldest revocation first.

        The one place a revoked row is still visible outside Site Mode. It leaves
        every routing view on the revoke call itself, so without this an operator
        could not tell a revoked desk from one that stopped heartbeating.
        """
        cur = await self._conn.execute(
            "SELECT agent_id, hostname, revoked_at FROM registry_agents"
            " WHERE revoked_at IS NOT NULL ORDER BY revoked_at, agent_id"
        )
        rows = await cur.fetchall()
        return tuple(
            RevokedAgentInfo(
                agent_id=str(row["agent_id"]),
                hostname=str(row["hostname"]),
                revoked_at=datetime.fromisoformat(str(row["revoked_at"])),
            )
            for row in rows
        )

    async def create_api_key(
        self,
        name: str,
        model_allowlist: Sequence[str] | None = None,
        rpm_limit: int | None = None,
        daily_limit: int | None = None,
    ) -> str:
        self._validate_quota_limit("rpm_limit", rpm_limit)
        self._validate_quota_limit("daily_limit", daily_limit)
        key = self._new_token()
        key_hash = hash_token(key)
        allow_json = None if model_allowlist is None else json.dumps(list(model_allowlist))
        conn = self._conn
        await conn.execute(
            "INSERT INTO registry_api_keys"
            " (key_hash, name, model_allowlist_json, rpm_limit, daily_limit,"
            " created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (key_hash, name, allow_json, rpm_limit, daily_limit, self._iso_now()),
        )
        await conn.commit()
        return key

    @staticmethod
    def _validate_quota_limit(name: str, value: int | None) -> None:
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{name} must be a positive integer")

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

    async def record_heartbeat(self, agent_id: str, heartbeat: Heartbeat) -> None:
        cur = await self._conn.execute(
            "UPDATE registry_agents SET last_seen = ?, "
            "state = CASE WHEN transport = 'direct' OR ? >= presence_sequence "
            "THEN ? ELSE state END, "
            "user_idle_s = ? ,"
            " mem_available_mb = ?, gpus_json = ?, replicas_json = ?, "
            "serving_paused = CASE WHEN transport = 'direct' OR ? >= presence_sequence "
            "THEN ? ELSE serving_paused END,"
            " predicted_idle_remaining_s = ?, predicted_idle_confidence = ?, "
            "presence_sequence = MAX(presence_sequence, ?)"
            " WHERE agent_id = ?",
            (
                self._iso_now(),
                heartbeat.seq,
                heartbeat.state.value,
                heartbeat.user_idle_s,
                heartbeat.mem_available_mb,
                dump_gpus(heartbeat.gpus),
                dump_replicas(heartbeat.replicas),
                heartbeat.seq,
                int(heartbeat.serving_paused),
                heartbeat.predicted_idle_remaining_s,
                heartbeat.predicted_idle_confidence,
                heartbeat.seq,
                agent_id,
            ),
        )
        await self._conn.commit()
        if cur.rowcount != 1:
            raise UnknownAgentError(agent_id)

    async def set_agent_state(self, agent_id: str, state: AgentState) -> None:
        """Set routing-visible state directly (event path).

        user_returned/user_idle events must affect routing immediately —
        interactive routing must never wait for the next heartbeat (ADR-000).
        """
        cur = await self._conn.execute(
            "UPDATE registry_agents SET state = ?, last_seen = ? WHERE agent_id = ?",
            (state.value, self._iso_now(), agent_id),
        )
        await self._conn.commit()
        if cur.rowcount != 1:
            raise UnknownAgentError(agent_id)

    async def apply_presence_event(self, agent_id: str, kind: str, sequence: int) -> int:
        """Persist a monotonic user-presence fence and return its generation."""
        if kind not in ("user_returned", "user_idle", "reclaim") or sequence < 0:
            raise ValueError("invalid presence event")
        state = "active" if kind in ("user_returned", "reclaim") else "idle"
        cur = await self._conn.execute(
            "UPDATE registry_agents SET state = ?, serving_paused = ?, "
            "presence_sequence = ?, presence_generation = presence_generation + 1, "
            "last_seen = ? WHERE agent_id = ? AND presence_sequence < ?",
            (state, int(kind == "reclaim"), sequence, self._iso_now(), agent_id, sequence),
        )
        await self._conn.commit()
        if cur.rowcount != 1:
            check = await self._conn.execute(
                "SELECT presence_generation FROM registry_agents WHERE agent_id = ?", (agent_id,)
            )
            row = await check.fetchone()
            if row is None:
                raise UnknownAgentError(agent_id)
            return int(row["presence_generation"])
        result = await self._conn.execute(
            "SELECT presence_generation FROM registry_agents WHERE agent_id = ?", (agent_id,)
        )
        row = await result.fetchone()
        assert row is not None
        return int(row["presence_generation"])

    async def bump_presence_generation(self, agent_id: str) -> int:
        """Advance the durable presence fence by one and return it.

        Used by the heartbeat/offline relay-fence path to persist the same
        generation it raises the in-memory broker fence to, so the durable fence
        never lags the broker and a restarted agent claims at a generation the
        broker still honours. ``last_seen`` is deliberately untouched so an
        offline sweep does not resurrect the agent as live.
        """
        cur = await self._conn.execute(
            "UPDATE registry_agents SET presence_generation = presence_generation + 1 "
            "WHERE agent_id = ?",
            (agent_id,),
        )
        await self._conn.commit()
        if cur.rowcount != 1:
            raise UnknownAgentError(agent_id)
        result = await self._conn.execute(
            "SELECT presence_generation FROM registry_agents WHERE agent_id = ?", (agent_id,)
        )
        row = await result.fetchone()
        assert row is not None
        return int(row["presence_generation"])

    async def site_route(self, agent_id: str) -> tuple[Transport, int] | None:
        """Return the agent's persisted transport and presence generation.

        The routing integration (ADR 082) reads this to decide whether to relay a
        site agent or dial a direct one, and to fence a claim on the current
        generation. ``None`` when the agent is unknown or revoked.

        Revoked is part of that filter, not an extra: this is the read every
        relay path goes through, and it is the only agent read that was missing
        the ``revoked_at IS NULL`` that ``snapshots`` and ``list_offline``
        already carry. Without it the generation bump at revocation was the whole
        fence, and a one-time bump only fences the waiters that already exist — a
        claim that had passed ``_authorize_self`` a moment before the revocation
        committed could resolve the revoked row afterwards, register at the NEW
        generation, and take one more gateway request. Answering ``None`` is
        permanent, so no waiter forms and the gateway has no relay route to that
        machine at all.
        """
        cur = await self._conn.execute(
            "SELECT transport, presence_generation FROM registry_agents"
            " WHERE agent_id = ? AND revoked_at IS NULL",
            (agent_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Transport(row["transport"]), int(row["presence_generation"])

    async def agent_transport(self, agent_id: str) -> Transport | None:
        """The agent's persisted transport, revoked rows included; None if unknown.

        Deliberately not ``site_route``: that read is the external revocation
        fence and hides a revoked row for good. The eviction decision inside
        the revoke call has to survive its own partial failure — revoked_at
        committed, broker eviction failed — so the retry can still tell a
        relay agent from a direct one and finish the eviction. Routing never
        reads this.
        """
        cur = await self._conn.execute(
            "SELECT transport FROM registry_agents WHERE agent_id = ?", (agent_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Transport(row["transport"])

    async def site_fleet(self, now: datetime) -> tuple[SiteFleetEntry, ...]:
        """Every site-transport agent's enrollment facts and heartbeat age.

        Unlike ``snapshots``, this keeps agents whose last heartbeat is older than
        ``offline_after_s``: a desk that stopped heartbeating is exactly what the
        read-only fleet view exists to show, and its age is the evidence. A
        revoked desk stays listed for the same reason, flagged rather than
        hidden. Purely a read; routing never consults it.
        """
        cur = await self._conn.execute(
            "SELECT agent_id, enrollment_mode, transport, last_seen, presence_generation, "
            "revoked_at FROM registry_agents WHERE transport = ? ORDER BY registered_at",
            (Transport.SITE_RELAY.value,),
        )
        rows = await cur.fetchall()
        return tuple(
            SiteFleetEntry(
                agent_id=str(row["agent_id"]),
                enrollment_mode=EnrollmentMode(row["enrollment_mode"]),
                transport=Transport(row["transport"]),
                heartbeat_age_s=self._age_s(now, row["last_seen"]),
                presence_generation=int(row["presence_generation"]),
                revoked=row["revoked_at"] is not None,
            )
            for row in rows
        )

    # ── authentication ───────────────────────────────────────────────────────

    async def authenticate_agent(self, bearer: str) -> str | None:
        """Resolve a device token, telling a revoked identity from an unknown one.

        Both are rejections, but they are not the same fact. A revoked row is a
        decision an operator made about this machine; an unrecognised token can
        mean this coordinator lost or has not yet restored its database, which
        every desk would otherwise read as its own revocation.
        """
        cur = await self._conn.execute(
            "SELECT agent_id, revoked_at FROM registry_agents WHERE device_token_hash = ?",
            (hash_token(bearer),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            raise RevokedAgentError(str(row["agent_id"]))
        return str(row["agent_id"])

    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None:
        if token_matches(bearer, hash_token(self._config.admin_key)):
            return ApiKeyInfo(
                name="admin",
                key_id=hash_token(self._config.admin_key),
                model_allowlist=None,
                is_admin=True,
            )
        cur = await self._conn.execute(
            "SELECT key_hash, name, model_allowlist_json, rpm_limit, daily_limit"
            " FROM registry_api_keys"
            " WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_token(bearer),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        raw = row["model_allowlist_json"]
        allowlist = None if raw is None else tuple(json.loads(raw))
        return ApiKeyInfo(
            name=str(row["name"]),
            key_id=str(row["key_hash"]),
            model_allowlist=allowlist,
            rpm_limit=row["rpm_limit"],
            daily_limit=row["daily_limit"],
            is_admin=False,
        )

    async def load_quota_snapshots(self) -> tuple[ApiKeyQuotaSnapshot, ...]:
        cur = await self._conn.execute(
            "SELECT key_hash, bucket_tokens, bucket_updated_at, day, daily_count,"
            " snapshotted_at FROM registry_api_key_quota_snapshots"
        )
        rows = await cur.fetchall()
        return tuple(
            ApiKeyQuotaSnapshot(
                key_id=str(row["key_hash"]),
                bucket_tokens=float(row["bucket_tokens"]),
                bucket_updated_at=datetime.fromisoformat(row["bucket_updated_at"]),
                day=str(row["day"]),
                daily_count=int(row["daily_count"]),
                snapshotted_at=datetime.fromisoformat(row["snapshotted_at"]),
            )
            for row in rows
        )

    async def save_quota_snapshots(self, snapshots: Sequence[ApiKeyQuotaSnapshot]) -> None:
        await self._conn.executemany(
            "INSERT INTO registry_api_key_quota_snapshots"
            " (key_hash, bucket_tokens, bucket_updated_at, day, daily_count, snapshotted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key_hash) DO UPDATE SET"
            " bucket_tokens = excluded.bucket_tokens,"
            " bucket_updated_at = excluded.bucket_updated_at,"
            " day = excluded.day, daily_count = excluded.daily_count,"
            " snapshotted_at = excluded.snapshotted_at",
            [
                (
                    item.key_id,
                    item.bucket_tokens,
                    item.bucket_updated_at.isoformat(),
                    item.day,
                    item.daily_count,
                    item.snapshotted_at.isoformat(),
                )
                for item in snapshots
            ],
        )
        await self._conn.commit()

    # ── liveness views ───────────────────────────────────────────────────────

    async def snapshots(self, now: datetime) -> tuple[AgentSnapshot, ...]:
        cur = await self._conn.execute(
            "SELECT * FROM registry_agents WHERE revoked_at IS NULL ORDER BY registered_at"
        )
        rows = await cur.fetchall()
        out: list[AgentSnapshot] = []
        for row in rows:
            age = self._age_s(now, row["last_seen"])
            if age > self._config.offline_after_s:
                continue
            out.append(snapshot_from_row(row, suspect=age > self._config.suspect_after_s))
        return tuple(out)

    async def list_offline(self, now: datetime) -> tuple[str, ...]:
        # Revoked agents stay in the revoked view (list_revoked_agents), never here.
        cur = await self._conn.execute(
            "SELECT agent_id, last_seen FROM registry_agents WHERE revoked_at IS NULL"
        )
        rows = await cur.fetchall()
        return tuple(
            str(row["agent_id"])
            for row in rows
            if self._age_s(now, row["last_seen"]) > self._config.offline_after_s
        )

    async def replica_endpoints(self, model_id: str, now: datetime) -> tuple[ReplicaEndpoint, ...]:
        cur = await self._conn.execute("SELECT * FROM registry_agents WHERE revoked_at IS NULL")
        rows = await cur.fetchall()
        out: list[ReplicaEndpoint] = []
        for row in rows:
            if self._age_s(now, row["last_seen"]) > self._config.suspect_after_s:
                continue  # suspect or offline agents cannot serve interactive traffic
            if row["serving_paused"]:
                continue  # user reclaimed the machine; never route here
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
        # Locked for the same reason the token writers are: this is a DELETE and
        # an INSERT reaching one COMMIT, with awaits between them on the shared
        # connection, and revocation clears a desk's models through it. A
        # ``register_agent`` rollback landing in that window discards the DELETE,
        # leaving a revoked agent still assigned and still desired (see
        # ``_write_lock``).
        async with self._write_lock:
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
