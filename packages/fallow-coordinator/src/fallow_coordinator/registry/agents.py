"""Agent heartbeats, presence fencing, Site Mode routing, and liveness views."""

from datetime import datetime
from typing import NamedTuple

from fallow_coordinator.registry.base import RegistryBase
from fallow_coordinator.registry.errors import UnknownAgentError
from fallow_coordinator.registry.mapping import ready_endpoints_for_row, snapshot_from_row
from fallow_coordinator.registry.serde import dump_gpus, dump_replicas
from fallow_coordinator.registry.tunnel_mode import EnrollmentMode, Transport
from fallow_protocol.messages import AgentSnapshot, AgentState, Heartbeat, ReplicaEndpoint


class SiteFleetEntry(NamedTuple):
    """One Site Mode agent's persisted enrollment facts plus its heartbeat age."""

    agent_id: str
    enrollment_mode: EnrollmentMode
    transport: Transport
    heartbeat_age_s: float
    presence_generation: int
    revoked: bool


class AgentsMixin(RegistryBase):
    """Per-agent state writes and the read views routing is built on."""

    # ── heartbeats & presence ────────────────────────────────────────────────

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
        the ``revoked_at IS NULL`` that ``snapshots`` already carries.
        Without it the generation bump at revocation was the whole
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
        # Revoked agents are deliberately included: the offline eviction loop
        # reads this view to requeue a dead agent's leases, and a revoked direct
        # agent's leases must not wait out their deadline.
        cur = await self._conn.execute("SELECT agent_id, last_seen FROM registry_agents")
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
