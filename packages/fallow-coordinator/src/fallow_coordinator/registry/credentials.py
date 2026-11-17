"""Enrollment tokens, revocation, API keys, bearer authentication, and quota state."""

import json
from collections.abc import Sequence
from datetime import datetime

from fallow_coordinator.registry.base import RegistryBase
from fallow_coordinator.registry.errors import (
    EnrollmentTokenError,
    RevokedAgentError,
    UnknownAgentError,
)
from fallow_coordinator.registry.records import (
    ApiKeyInfo,
    ApiKeyQuotaSnapshot,
    EnrollmentTokenInfo,
    RevokedAgentInfo,
)
from fallow_coordinator.registry.tokens import (
    TOKEN_ID_CHARS,
    hash_token,
    normalize_token_id,
    token_matches,
)


def _token_state(used_at: str | None, revoked_at: str | None) -> str:
    if revoked_at is not None:
        return "revoked"
    return "used" if used_at is not None else "outstanding"


class CredentialsMixin(RegistryBase):
    """Token issuance and revocation, API keys, authentication, quota snapshots."""

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
