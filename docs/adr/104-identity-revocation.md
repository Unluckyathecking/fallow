# revoke an enrolled desk and an unused join token

## Status

Proposed

## Date

2026-08-26

## Goal

Give an operator a server-side answer to a lost credential.

Until now there was none. The Site Mode runbook said it plainly: an enrolled
device token could not be invalidated from the coordinator, and neither could an
unused enrollment token. A stolen laptop kept a working identity until someone
walked to it and uninstalled; a join file left on a USB stick stayed live until
somebody used it. The only real revocation in the product was rotating the
coordinator's certificate and re-enrolling every desk — a physical visit to every
machine in the pilot, to deal with one of them.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/registry/`: `schema.sql`,
  `sqlite_registry.py`, `records.py`, `tokens.py`, `errors.py`, `__init__.py`,
  `README.md`
- `packages/fallow-coordinator/src/fallow_coordinator/app/admin_routes.py`,
  `site/router.py`, `httpauth.py`
- `packages/fallow-cli/src/fallow_cli/`: `main.py`, `client.py`, `models.py`,
  `render.py`, `site/join_bundles.py`, `site/__init__.py`, `README.md`
- `go-agent/state/revoked.go`, `go-agent/runtime/runtime.go`,
  `go-agent/runtime/site.go`, `go-agent/cmd/agentctl/main.go`,
  `go-agent/heartbeat/{client,constants,errors}.go`
- `deploy/windows/install.ps1`, `deploy/windows/new-site-config.ps1`,
  `deploy/windows/tests/site-mode.Tests.ps1`
- tests: `packages/fallow-coordinator/tests/registry/test_registry_revocation.py`,
  `tests/app/test_admin_revocation.py`, `tests/app/test_site_routes.py`,
  `tests/site/test_status_route.py`; `packages/fallow-cli/tests/`;
  `tests/integration/site_mode/test_revocation.py`; `go-agent/**/*_test.go`
- docs: `docs/admin-api.md`, `docs/lan-site/operator-runbook.md` (§3, §5, §11,
  troubleshooting, honest gaps), `docs/school-pilot.md` (§5), `CHANGELOG.md`

No protocol change. Two nullable registry columns, three admin routes with no
request body and one read-only listing route, so `schemas/` and the Go codegen
are untouched. The one machine-readable 401 detail string is prose inside the
existing FastAPI error envelope, not a wire type.

## Decision

**A voided enrollment token is a spent one.** `DELETE
/v1/admin/enrollment_tokens/{token_id}` sets `used_at` through the same gate a
real enrolment uses, so a revoked join file fails with the message a re-used one
already gets and the hot enrolment path grows no new branch. A second column,
`revoked_at`, records that an operator spent it rather than an agent, which is
the only thing the listing needs to tell the two apart. Revoking an unknown or
already-spent id is a 404: there is nothing to void and nothing to undo.

**Tokens are named by a truncated digest.** The coordinator stores only
`sha256(token)`, so its first 12 hex characters are the natural public name for a
token nobody may print. `GET /v1/admin/enrollment_tokens` lists ids, modes,
states and mint times, and `flw site join-bundles` prints the same id on each
file's line (`token=9f2c41ab77de`). That line is what makes revocation operable:
without a desk-to-token map, "revoke the token on the stick that went missing" is
not a command anybody can type. The id is derivable from a join file the operator
still holds and from nothing else — truncating the hash of a 256-bit secret
leaks no part of it.

**A revoked agent is invisible, not deleted.** `POST
/v1/admin/agents/{agent_id}/revoke` sets `revoked_at` on the agent row.
`authenticate_agent` filters on it, which is the single choke point every
agent-facing route already goes through — heartbeat, events, work poll, result
upload, blob and manifest fetch, and the relay claim routes all return 401 with
no route-by-route change. `snapshots` and `replica_endpoints` filter on it too,
so the machine leaves interactive routing and `GET /agents` on the revoke call
itself rather than on some later heartbeat. Leaving the routing view is not the
same as vanishing, though, so the row keeps one surface of its own:
`GET /v1/admin/agents/revoked` (`flw agents list --revoked`) is where an operator
outside Site Mode can still tell a revoked desk from one that never enrolled.

**Eviction composes with what exists.** Revocation does not invent a takedown
path: it clears the agent's assignments (the kill switch of ADR 042, aimed at one
agent), then persists a newer presence generation and calls the relay broker's
`invalidate_agent` — the same fence a returning user raises (ADR 081) — so queued
and in-flight relayed requests are dropped at once instead of hanging until their
deadline.

**Revocation is terminal, and there is no un-revoke route.** The registry's model
for a machine that comes back is already re-enrolment: a wiped desk gets a fresh
join file and a new agent id, and the old identity stays as a dead row (runbook
§8). An un-revoke would be a second way for a credential to come back to life,
with no way to know whether the machine at the other end is still the one that
was lost. `revoke_agent` is idempotent — a second call changes nothing and keeps
the first revocation's timestamp — so a retried command is safe.

**A revoked desk exits, quietly, and stays down.** The Go runtime already
treated a 401 as fatal and tore down cleanly, so the question was only what
happens after the process ends. Windows runs the agent as a Scheduled Task with
`RestartOnFailure` at one-minute intervals, 999 times, so exiting non-zero on a
revoked identity is a restart loop: sixteen hours of a dead desk re-presenting a
dead token. So the runtime writes `revoked.flag` beside its state file when the
coordinator names the rejection as a revocation, `agentctl run` exits **0**, and a
later start — a logon, a reboot, a manual launch — sees the marker with the
condemned identity still beside it, logs one line and exits 0 without enrolling,
heartbeating or building a supervisor. That is
quieter than parking a live process on a slow retry and, unlike an in-process
back-off, it survives the reboot. Deleting `~\\.fallow` (what `uninstall.ps1
-Purge` does) clears it, which is the same act that clears the identity —
revocation and re-enrolment stay one decision.

**Coordinator identity loss is not revocation.** A coordinator restored from an
older backup, or started on the wrong `db_path`, does not know any device token
it ever issued, and rejects every desk with a 401 that is indistinguishable from
a revoked one. Writing the marker on *any* 401 would therefore let one bad
restore brick a whole fleet permanently, recoverable only by visiting every
machine — the worst outcome this ADR could produce, and strictly worse than what
it replaced. So the coordinator says which kind of rejection it is:
`authenticate_agent` raises `RevokedAgentError` for a token whose agent row is
revoked, and the HTTP layer turns that into the one stable detail string
`device token revoked` (`fallow_coordinator.httpauth.REVOKED_DEVICE_TOKEN_DETAIL`,
mirrored as `heartbeat.revokedDetail` in Go). Nothing on the wire changes: an
error detail is prose in an existing `{"detail": ...}` envelope, not a schema.
The Go agent records the marker only on that detail; every other 401 keeps the
pre-existing behaviour — fatal, exit non-zero, the Scheduled Task retries, and
the desk recovers by itself the moment the coordinator does. Because the choke
point is the one `authenticate_agent` every agent-facing route already shares,
the relay claim routes, the work poll and the blob/manifest fetches all report
the same detail with no route-by-route change.

**Re-enrolment clears a stale marker.** The marker condemns an *identity*, and a
reinstall from a fresh join file replaces that identity. `install.ps1` treats a
`revoked.flag` beside the state path as no identity at all, so it removes both
and stages the new bundle instead of silently skipping it and reporting success;
the daemon clears any marker still present when enrolment succeeds. The recovery
the runbook documents is therefore self-healing, and `uninstall.ps1 -Purge`
remains the full clean rather than the only way back.

**Doctor reads the marker, not the network.** The identity lane is offline by
design, and a new authenticated probe would be the only network call doctor makes
against a token it has reason to believe is dead. The daemon already knows, so
doctor reports what the daemon wrote: `device token rejected by the coordinator:
… ; this machine must re-enrol from a fresh join file`, and fails the lane so the
exit code is non-zero.

**The fleet view flags the row rather than dropping it.** A revoked site agent
leaves `snapshots`, so `flw site status` would otherwise show it as a desk that
merely stopped heartbeating. `site_fleet` carries the flag and the row reads
`presence=revoked`, `avail=no` — which is the confirmation an operator looks for
after typing the command.

## Verification

Registry unit tests cover the revoked token failing enrolment with
`EnrollmentTokenError`, a used token being unrevokable and not rewritten, the
revoked device token failing `authenticate_agent`, idempotence, `UnknownAgentError`
for a bad id, the agent leaving `snapshots` and `replica_endpoints`, and both
kinds of revocation surviving a close-and-reopen of the database file.

App tests drive the routes over ASGI: the listing that names tokens without
carrying one, a revoked token failing `POST /v1/agents/register` with the same
401 a spent one gets, a revoked agent getting 401 on heartbeat, work poll and the
model manifest — three routes chosen because they run through three different
auth call sites — that the revoked 401's detail differs from an unknown token's,
that `GET /agents/revoked` lists what `GET /agents` no longer does, and that a
malformed token id is not reported as "already spent". One test asserts the replicas leave routing and the assignments
are cleared; another, against a real relay broker, that an in-flight claim is
invalidated and a fresh claim is refused.

The site suite runs the whole thing against the built Go agent: a real desk
enrols from a minted join file, serves a relayed chat request, is revoked
mid-flight, and then the gateway sheds with 503, `GET /agents` is empty, the
daemon exits 0 on its own with the reason on stderr, `flw site status` reads
`presence=revoked`, and `agentctl doctor` fails its identity lane with the
rejection named.

Go tests cover the marker round-trip and its clearing, that only a
revoked-detail 401 records it — an unknown-token 401 tears the supervisor down
and exits non-zero with no marker written — that a marked runtime with an
identity beside it returns nil without touching the coordinator, and that a fresh
site enrolment clears a stale marker. A Pester case covers the install
disposition that replaces a revoked identity. A cross-package test binds the last
loose seam: the token id `flw site join-bundles` prints is the one that revokes
that file's token, and enrolment with it then fails.

## Compatibility

Additive. Two nullable columns are added by the existing `ALTER TABLE` migration
path, so an existing coordinator database opens unchanged and every row reads as
not revoked. Nothing on the wire changes: the three new routes carry no request
body, `AgentSnapshot` is untouched, and the join bundle is byte-for-byte what it
was — the token id is printed by the CLI, never carried in the file, so the Go
client's strict join parser is unaffected. An older agent binary against a newer
coordinator behaves as it always did on a 401 (fatal, exit non-zero); it just
restarts into the rejection, which is the behaviour this change removes. A newer
agent against an older coordinator that sends no revoked detail treats every 401
as retryable — the pre-M9 behaviour, and the safe direction to fail.

## Exclusions and honest gaps

**A compromised coordinator key is still a rotation.** Nothing here helps if the
coordinator's certificate or admin key leaks: every desk pins that certificate,
so the remedy remains a swap and a re-mint, with the pre-staged next pin (runbook
§2) as the only thing that keeps it from being a full re-enrolment.

**Client API keys still have no CLI revoke.** The registry has carried
`revoked_at` on `registry_api_keys` since ADR 006 and `authenticate_api_key`
honours it, but no route or command sets it. That is a smaller, well-defined gap
and was left out rather than widened into this change.

**A relayed request already streaming to a client is not un-sent.** Revocation
invalidates the claim, so the agent's next write fails and the stream ends, but
bytes already delivered are delivered. The same is true of the presence fence
this reuses. The batch work poll has the matching bounded gap: a lease a revoked
agent already holds runs to its deadline rather than being cut, because nothing
recalls a lease mid-flight — accepted, and bounded by the lease.

**Revocation does not reach the machine.** It stops the coordinator from
answering that identity; it does not wipe the token from the stolen disk, delete
cached model weights, or stop a `llama-server` that is already running locally on
loopback. Full-disk encryption and the uninstall path are what cover the device
itself, and the runbook says so.

**No audit trail beyond the timestamp.** `revoked_at` records when, not who or
why. Every admin call authenticates as the one bootstrap admin key, so there is
nobody to record; a real actor log is an admin-auth change, not a revocation one.

**No expiry.** An outstanding enrollment token still lives until it is used or
revoked. Minting with a TTL is the obvious complement and is not here — it would
change the enrolment check, and this change deliberately did not.
