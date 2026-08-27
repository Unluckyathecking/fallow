# LAN Site Mode operator runbook

The tested path for running a four-desk Site Mode pilot: one coordinator on the
school LAN, four Windows PCs serving inference over pinned HTTPS, no Tailscale
and no internet.

Two people share this document. The **coordinator operator** runs everything in
the `flw` sections and holds the admin key. **School IT** owns the network, the
Windows images and the endpoint-protection rules; their prerequisites are in
[`docs/pilot/it-checklist.md`](../pilot/it-checklist.md) and must be signed off
before pilot day, not during it.

Site Mode is opt-in. A coordinator without `[site].enabled` and an agent without
`site_join_bundle` behave exactly as they did before. See
[`docs/compatibility.md`](../compatibility.md).

Read [`README.md`](README.md) for the design and [`acceptance.md`](acceptance.md)
for the criteria this runbook exists to satisfy.

---

## 0. What you need before pilot day

- A coordinator host on the school LAN with **one stable address** (§1).
- A TLS certificate and key for that address (§2).
- The desk bundle, `fallow-site-agent_<version>_windows_amd64.zip`, from the
  GitHub Release. It carries `agentctl.exe` and the install scripts, so a desk
  needs that one zip plus its own join file and nothing else: no repository
  checkout. The Python agent has no Site Mode.
- A llama.cpp build staged on each PC (§4), which needs internet on the desk or
  a pre-staged copy carried to it.
- One GGUF model file on the coordinator host.
- The nominated pilot account logged in on each PC, with sleep and clock policy
  already set (see the IT checklist).

Everything below runs from the repository root on the coordinator host unless a
step says otherwise. Desk-side commands are written repo-relative
(`deploy\windows\doctor.ps1`); in the bundle the same script is one level up,
under `windows\`.

---

## 1. One stable coordinator address

Site Mode pins an address into every join file, and an already-enrolled agent
keeps dialling the origins it was given. Decide the address once, before you mint
anything.

Use a **DHCP reservation or an internal DNS record**. That is the dependable
school setup and the only one the pilot depends on. Optional mDNS (§7) supplies
address candidates for recovery; it never supplies trust and it is not the
baseline.

The coordinator refuses to start Site Mode on a wildcard bind, so `host` must be
the exact address it will serve on:

```toml
host = "10.24.8.10"
port = 8330
```

`[site].public_urls` is what agents dial. It must be an HTTPS root origin — no
path, query, fragment or user information — and it must resolve to the machine
holding the certificate. List a second origin only if you already have a second
name for the same coordinator; order is significant and agents try them in order.

Ask IT to confirm in writing that the address will not move for the duration of
the pilot. It moving is the one change that costs you a re-issue of every join
file unless mDNS recovery is enabled and working.

## 2. Certificate and pin preparation

The agent trusts the coordinator by the SHA-256 hash of its certificate's
DER-encoded SubjectPublicKeyInfo, not by the Windows trust store. A private CA is
fine. A self-signed certificate is fine. What matters is that the key does not
change without a planned pin rotation.

The coordinator enforces at startup that the certificate and key exist and form a
valid pair, that the certificate is inside its validity window, and that `host`
is an exact non-wildcard address.

Give the certificate a **long pilot lifetime**. Expiry mid-pilot is an outage on
every desk at once, and it surfaces as an opaque pinned-TLS error. Include the
address agents will dial in the SANs — the DNS name if you use internal DNS, the
IP literal if you use a DHCP reservation.

Rotating the key later means minting new join files, so plan the current/next
transition before you need it. `coordinator_spki_sha256` is a list: the join file
format, the Windows installer's preflight check and the Go agent's strict parser
all accept more than one pin, and an agent holding both the current and the next
pin survives the swap without a visit.

**`flw site join-bundles` does not mint a two-pin file.** It writes exactly one
pin — the SPKI of the certificate the coordinator is serving at the moment you
mint. Getting a second pin into a join file is a manual edit, done on the
coordinator host before the file goes to the desk: append the next certificate's
pin to the `coordinator_spki_sha256` array. Compute it the same way the
coordinator does — SHA-256 of the DER SubjectPublicKeyInfo, base64, prefixed
`sha256/`:

```bash
openssl x509 -in next-cert.pem -pubkey -noout \
  | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -binary \
  | openssl base64 \
  | sed 's|^|sha256/|'
```

Save the edited file as BOM-free UTF-8 and change nothing else; the installer
rejects a byte-order mark, a repeated key and a duplicate pin before it writes
anything.

Skip that edit and rotation is not a swap: it is a new join file and a fresh
enrollment on every desk, and each desk comes back as a **new agent id** (§8).
There is no network trust reset. If no trusted pin remains, recovery is a new
join file carried to each machine by hand.

Coordinator config for the pilot:

```toml
db_path = "/var/lib/fallow/coordinator.db"
blob_dir = "/var/lib/fallow/blobs"
unit_input_dir = "/var/lib/fallow/units"
result_dir = "/var/lib/fallow/results"
events_jsonl_path = "/var/lib/fallow/events.jsonl"
gateway_log_path = "/var/lib/fallow/gateway.jsonl"

admin_key = "replace-with-a-random-admin-key"
host = "10.24.8.10"
port = 8330

# Each desk takes the largest registered model its own hardware can hold when it
# enrols, so no desk needs a hand assignment (§6).
auto_assign_on_enroll = true

[site]
enabled = true
site_id = "clfs-pilot"
public_urls = ["https://10.24.8.10:8330"]
tls_certfile = "/etc/fallow/site-cert.pem"
tls_keyfile = "/etc/fallow/site-key.pem"
```

Start it:

```bash
uv run python -m fallow_coordinator serve --config coordinator.toml
```

On Linux, prefer the service: `sudo deploy/coordinator/install.sh --ref v0.3.0`
installs the coordinator as `fallow-coordinator.service`, reading
`/etc/fallow/coordinator.toml`: put the config above there instead, and the
certificate and key where it points. The service is already running, so skip the
foreground command and check it with `systemctl status fallow-coordinator`; after
a config edit, `sudo systemctl restart fallow-coordinator`. See
[`deploy/README.md`](../../deploy/README.md) §3.

Point `flw` at it from a second terminal. The admin key has no flag on purpose,
so it never lands in shell history:

```bash
export FLW_COORDINATOR_URL="https://10.24.8.10:8330"
export FLW_ADMIN_KEY="replace-with-a-random-admin-key"
```

Confirm the coordinator answers before you mint anything:

```bash
uv run flw status
```

## 3. Four per-device join files

A join file names the coordinator, pins its certificate, and carries one
enrollment token. It is a credential. Treat it like a password on a USB stick.

```bash
uv run flw site join-bundles --count 4 --output ./join
```

It writes `desk-01.fallow-join` through `desk-04.fallow-join`, owner-readable
only, and prints one line per file:

```text
join/desk-01.fallow-join site=clfs-pilot origins=https://10.24.8.10:8330 pin=sha256/AbCdEfGh token=9f2c41ab77de
```

The `pin=` value is the first 16 characters of the pin the coordinator is
serving. The `token=` value is that token's id at the coordinator — not the
token — and it is what `flw enroll revoke` takes, so keep this output with the
record of which file went to which desk. Check it matches on every line, then check it against the certificate
you deployed. A mismatch means the coordinator is serving a different key from
the one you think it is. Stop and fix that before touching a desk.

Notes that matter operationally:

- Each token is **single-use**, consumed the first time a machine enrolls.
- There is **no expiry** in this version. An unused join file stays live until it
  is used or explicitly revoked by its id (§11). Mint four, use four, destroy the
  media afterwards.
- `join-bundles` refuses to overwrite an existing file. `--force` overwrites, and
  burns four fresh tokens doing it.
- One file per machine. Do not copy one file to four desks; the second machine
  gets a consumed token and fails enrollment.

Carry the files to the desks over USB or MDM. Do not email them and do not put
them on a share.

### Register the model before any desk enrols

Placement happens at enrolment and nowhere else (§6), so the model has to be in
the registry before the first machine in §4 runs. Register it here, on the
coordinator host. Registration records a coordinator-local path, so the file must
stay readable there.

If the coordinator host has internet, stage the file and register it in one step
from the curated catalog:

```bash
uv run flw models pull --catalog qwen2.5-0.5b-instruct-q4km \
  --model-id qwen2.5-0.5b-instruct
```

**The model is registered as `qwen2.5-0.5b-instruct`.** That is the id the rest
of this runbook uses: `flw assign`, `flw keys new --allow`, and the kill switch
in §11 all name it, and each of them accepts an id it does not know without
complaint, so a mismatch here shows up later as silent 403s or a fleet stuck at
`ready=0`. Flags beat the catalog, which is why `--model-id` is passed: without
it the entry registers under the catalog's own id, `qwen2.5-0.5b-instruct-q4km`.

That downloads into `~/.fallow/blobs`, checks the blob against the sha256 in the
catalog, reads the quantisation out of the GGUF header, sizes `min_ram_mb` from
the file, and registers the manifest. `uv run flw models pull --catalog` with an
unknown id prints the ids it knows. Any other GGUF works the same way with
`uv run flw models pull hf:<owner>/<repo>/<file.gguf> --model-id <id> --family <f>`.

**Only the coordinator host ever dials huggingface.co.** Agents are never given
an internet source: once a model is registered, each desk fetches the blob from
the coordinator over the same pinned HTTPS connection it uses for everything
else, so a Site Mode desk stays internal-only whether or not the coordinator has
egress. If the coordinator has none either (an air-gapped site), do not use
`pull`. Download the GGUF on a machine that does have internet, carry it to the
coordinator, and register the local file:

```bash
uv run flw models register \
  --file /srv/models/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  --model-id qwen2.5-0.5b-instruct \
  --family qwen2.5 \
  --quant Q4_K_M \
  --worker-kind chat
```

`register` derives nothing: the minimums are whatever you pass it. `--min-ram-mb`
and `--min-vram-mb` both default to `0`, which means "fits anywhere" to the
enrolment-time placement in §6, so on a mixed fleet set `--min-ram-mb`
deliberately: 115% of the file size in MiB plus 512 is the rule `pull` derives
its value from, and it is a floor, not a measurement.

## 4. Install on Windows

Per machine, in the pilot user's own session. Unzip the desk bundle somewhere
that will stay put (the scripts resolve each other and the staged llama build
by their position in that directory), then, from inside it:

```powershell
.\windows\fetch-llama.ps1
.\bootstrap.ps1 -JoinBundle D:\join\desk-01.fallow-join -GoBinary .\agentctl.exe
```

`fetch-llama.ps1` downloads the pinned llama.cpp build and stages it under
`bin\windows\`. A desk with no internet needs that directory populated by hand
before the install; the bundle's own `README.md` says how. Full detail on the
join file is in
[`deploy/windows/JOIN-README.md`](../../deploy/windows/JOIN-README.md).

`bootstrap.ps1` ships in the bundle and resolves `windows\install.ps1` from
beside itself. It reports the desk's RAM and GPU, warns when there is no NVIDIA
GPU (the pinned llama.cpp build is CUDA-only) or too little RAM, hands off to the
same `install.ps1`, and finishes with a self-test that the Scheduled Task is
registered and the config is present. `.\windows\install.ps1` with the same two
arguments is the identical install without that report. Use it if the bootstrap
misreads the machine.

From a repository checkout (the development path, not the pilot one), the same
install is `deploy\windows\fetch-llama.ps1` then `deploy\bootstrap.ps1
-JoinBundle <file> -GoBinary <agentctl.exe>`.

Deploying from Intune, ConfigMgr, PDQ or a GPO startup script instead of walking
to each desk? `install.ps1 -User <account>` does the same registration from an
elevated admin or SYSTEM context; see
[`docs/pilot/remote-install.md`](../pilot/remote-install.md).

`install.ps1` validates the join file before writing anything, copies it to
`%USERPROFILE%\.fallow\site\join.json` with an owner-only ACL, renders a
token-free `%USERPROFILE%\.fallow\agent.toml` bound to `127.0.0.1`, installs the
binary into `%USERPROFILE%\.fallow\bin\`, and registers the at-logon Scheduled
Task `Fallow\FallowAgent`.

The rendered config carries no token and no coordinator URL — Site Mode dials the
pinned origin from the join file:

```toml
site_join_bundle = "C:\\Users\\pilot\\.fallow\\site\\join.json"
bind_host = "127.0.0.1"
llama_server_binary = "C:\\...\\llama-server.exe"
```

On first run the agent enrolls, persists a token-free site profile beside its
`agent_id` and device token, and deletes its copy of the token. After that it
resumes from the stored profile across reboots and the join file is not needed
again.

Then **remove the original join file from the USB stick or MDM share.** The
installed copy's token is gone once enrollment succeeds; the original is not.

Rehearse `install.ps1 -WhatIf` on one machine first if you want a no-side-effect
walk of the whole path.

## 5. Doctor

Run this on every desk before it starts serving, and again whenever a desk goes
quiet.

```powershell
deploy\windows\doctor.ps1
```

One JSON object, exit non-zero if a required check fails. Keys:
`task_registered`, `task_running`, `interactive_session`, `config_acl`,
`loopback_bind`, `llama_binary`, `identity`, `idle`, `spki_tls`, `clock`, `ok`.
Each is `{ok, detail}` except `ok`, the overall verdict.

`task_running` and `interactive_session` are reported but do not decide the exit
code, because doctor is legitimately run before anyone has logged in. Read them
yourself: `"ok": true` on a machine with nobody signed in means the install is
sound and the desk is not serving.

`identity` reads the desk's own state directory and nothing else — doctor makes
no authenticated call. It fails with `device token rejected by the coordinator`
on a desk that was revoked (§11); that desk is finished until it is reinstalled
and re-enrolled from a fresh join file.

`idle` takes the same sample the daemon takes before it enrols. The agent refuses
to start where nothing can tell it whether someone is at the machine, so a failing
`idle` means this desk will not serve until it is fixed. On a desk it must read
`supported and sampling`; `assume_idle` passes the lane with a warning and belongs
only on a machine nobody uses.

A healthy freshly-installed desk:

```json
{
  "task_registered": {"ok": true, "detail": "registered"},
  "task_running": {"ok": true, "detail": "running"},
  "interactive_session": {"ok": true, "detail": "an interactive user session is active"},
  "config_acl": {"ok": true, "detail": "restricted: agent.toml, join.json"},
  "loopback_bind": {"ok": true, "detail": "bind_host=127.0.0.1; no replica port exposed off loopback"},
  "llama_binary": {"ok": true, "detail": "C:\\Users\\pilot\\.fallow\\bin\\llama-server.exe"},
  "identity": {"ok": true, "detail": "enrolled agent_id=agt_7f2a site_id=clfs-pilot"},
  "idle": {"ok": true, "detail": "supported and sampling"},
  "spki_tls": {"ok": true, "detail": "pins valid (persisted profile)"},
  "clock": {"ok": true, "detail": "offset +1s against the coordinator"},
  "ok": true
}
```

The Go agent's own read-only core, without the Windows-native lanes, is what to
reach for when `doctor.ps1` itself will not run. `install.ps1` puts the binary in
`%USERPROFILE%\.fallow\bin` and does not add it to `PATH`, so call it by path:

```powershell
& "$env:USERPROFILE\.fallow\bin\agentctl.exe" doctor -config "$env:USERPROFILE\.fallow\agent.toml"
```

### The clock lane

`clock` measures the signed offset between this PC and the coordinator's `Date`
header, over the pinned client, sending no token. Over **120 seconds** it fails
with `sync this PC's clock before pinned TLS fails`.

The lane is deliberately quiet about everything it cannot conclude. An
unreachable coordinator, an unusable profile or a missing `Date` header all
report `ok: true` with `skew unknown: ...` — `config` and `pinned_tls` own those
failures (`doctor.ps1` renames that lane `spki_tls`), and doctor will not blame
the clock for them.

The case worth knowing is a clock that is days or months out, from a dead CMOS
battery or a machine back from storage. It puts **every** certificate outside its
validity window, so the handshake fails before any `Date` header is served and
the offset cannot be measured at all. Doctor reports that one specially:

```text
skew unknown, certificate outside validity window: a clock that is days or
months out puts every certificate outside its window, so check this PC's date,
time zone and NTP sync before suspecting the certificate
```

Read it literally. Check the date, the time zone and NTP sync first. The
certificate is almost never the problem.

### Live reach probe

```powershell
deploy\windows\doctor.ps1 -Probe
```

Adds one live TCP and TLS test to the pinned coordinator, which tells apart three
failures that otherwise look identical:

| `spki_tls` detail | Means |
| --- | --- |
| `blocked TCP: ... did not accept a connection in 5s` | Firewall or VLAN, not TLS. |
| `TLS handshake failed ...; a TLS-intercepting proxy or wrong port is likely` | Inspection, or the wrong port. |
| `pin mismatch: server SPKI ... is not in the pin set` | An intercepting proxy is terminating your HTTPS. Do not proceed. |
| `reachable; presented cert SPKI matches a pinned key` | Good. |

On Windows PowerShell 5.1 the probe cannot compute the presented SPKI, so it
reports reachability only and defers to `agentctl`'s pin result rather than
claiming a pass an intercepting proxy would also earn. Run it from `pwsh` 7+
where you can.

### When the school's inspection proxy is in the path

The school inspects HTTPS. Site Mode pins the coordinator's public key, so an
inspection proxy re-signing the connection presents a key that is not in the pin
set and the agent refuses it. **Tell IT to expect this before pilot day.** It is
the design working, not a bug to be escalated, and the only fix is an exemption
for the coordinator's host and port. The pin is never relaxed and there is no
flag that relaxes it.

What the agent does when it meets one, rehearsed in CI against a loopback TLS
terminator holding the coordinator's hostname under a different key
(`tests/integration/site_mode/test_interception.py`):

- It completes the TLS handshake, fails the pin check, and stops. **No request
  line, no `Authorization` header and not one byte of the enrollment token
  reaches the middlebox.**
- It does not downgrade to cleartext and does not dial around the pin. Every
  proxy variable — `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and their lowercase
  forms — is ignored.
- A refused enrollment **persists no identity and does not consume the join
  file's token.** Once IT adds the exemption, re-run the install with the same
  join file; you do not need to mint a fresh batch.
- Interception that appears in front of an *already enrolled* desk leaves its
  enrollment untouched. When the middlebox goes away the agent resumes claims on
  the same agent id, with no second registration.

Where you read it depends on which tool you run:

| Run this | What it tells you about interception |
| --- | --- |
| `deploy\windows\doctor.ps1` (no `-Probe`) | Not enough. `spki_tls` validates the pin set statically without connecting, so it passes. The `clock` lane does open one pinned connection, and against a middlebox its detail reads `skew unknown, pinned TLS failed: ... certificate pin mismatch` — distinct from `coordinator unreachable` — but the lane reports `ok: true` either way, so the run still exits 0. Read the detail; do not trust the verdict. |
| `deploy\windows\doctor.ps1 -Probe` | `spki_tls`: `pin mismatch: server SPKI ... is not in the pin set ...; this is the signature of a TLS-intercepting proxy - do not proceed` |
| The agent's own log | The daemon names `pin mismatch` and exits non-zero. A coordinator that is merely down reports a connection failure instead, with no mention of a pin. |

That distinction is the one to hold on to: **pin mismatch means a middlebox,
connect failure means the coordinator or the path.** They are different problems
with different owners. A plain `doctor.ps1` run hints at it in the `clock`
detail but still exits 0; `-Probe` and the agent's own log are what actually
fail on it.

## 6. Model assignment

The GGUF is already registered: §3 does it, because placement happens at
enrolment and a desk that enrols first never gets a second look.

```bash
uv run flw site status              # read the four agent ids, and ready=1
```

With `auto_assign_on_enroll = true` in the §2 config, each desk is given the
largest registered model its own hardware can hold at the moment it enrols: RAM,
and VRAM on a machine with an NVIDIA GPU, as the agent reports them. Nothing to
run per desk.

A desk that enrolled while nothing was registered stays at `ready=0` until you
assign it yourself; auto-assign does not revisit it. A desk that already has a
model keeps it: an existing assignment is never overridden.

`flw assign` is the override, and the way to place a model on a desk that has
already enrolled:

```bash
uv run flw assign qwen2.5-0.5b-instruct AGENT_1 AGENT_2 AGENT_3 AGENT_4
```

It is an **exact replace**, not an append: afterwards the model runs on exactly
the agents you named. Re-run with the full list to change the set.

The change is not instant. An agent learns its new desired set on the next
heartbeat and starts the replica on a following reconcile pass, and **reconcile
runs only while the machine is idle**. A desk someone is using keeps its current
replicas until they leave. Confirm from `flw site status` rather than assuming.

Create a client key for whatever will send requests:

```bash
uv run flw keys new pilot --allow qwen2.5-0.5b-instruct
```

## 7. Verify the LAN advertisement (only if you enabled mDNS)

**Skip this section unless `[site].mdns_service` is set.** Static addressing is
the baseline; mDNS is optional recovery for a coordinator that changes address.

This check exists because of a named gap in
[ADR 092](../adr/092-lan-site-discovery-acceptance.md). Both ends of discovery
are covered by tests, but the multicast hop **between** them is not: the
coordinator's advertiser answers on the interface it advertises, and the Go
agent's resolver queries the host's default multicast interface. On a loopback CI
host those two never meet, so no automated test proves an agent can hear this
coordinator. Pilot day is where that gets proven, on the real LAN, before you
rely on discovery for anything.

Do it from an agent machine on the same VLAN, with the coordinator running:

```bash
# Linux
avahi-browse -rt _fallow._tcp

# macOS, or Windows with Bonjour installed
dns-sd -B _fallow._tcp local.
dns-sd -L <instance-name> _fallow._tcp local.
```

You are checking three things, in order:

1. an instance of `_fallow._tcp.local.` appears at all;
2. its TXT carries `site_id=<your site_id>` and `version=1`;
3. its SRV target and port are the coordinator address you configured in §1.

If the `site_id` in TXT does not match the one in your join files, agents will
correctly ignore that responder and discovery buys you nothing.

If nothing appears, multicast is filtered on this VLAN. That is a supported
outcome, not a fault: the static address keeps working, agents keep serving, and
`doctor` keeps reading the same site id and the same valid pins. Record it and
move on — but do not then plan on discovery rescuing an address change.

The advertisement carries a version and a site id and nothing else. It is an
address hint. Trust still comes from the join file's site id and pin set, so a
responder on the wrong key receives no request from an agent at all.

## 8. Live availability: reading `flw site status`

This is the pilot-day pane. One row per Site Mode agent, ten fields, as
`key=value` so nothing gets truncated:

```text
agt_7f2a mode=site transport=site_relay hb_age_s=1.2 presence=idle gen=4 avail=yes ready=1 last_claim=finished claim_code=-
agt_91bd mode=site transport=site_relay hb_age_s=0.9 presence=active gen=7 avail=no ready=1 last_claim=failed claim_code=became_active
agt_c40e mode=site transport=site_relay hb_age_s=0.8 presence=reclaimed gen=2 avail=no ready=0 last_claim=none claim_code=-
agt_1d55 mode=site transport=site_relay hb_age_s=612.4 presence=offline gen=1 avail=no ready=0 last_claim=finished claim_code=-
```

`flw --json site status` prints the same fields as a machine-readable array.
The flag is on the root command, so it goes before `site`.

| Field | Reading it |
| --- | --- |
| `mode` | Always `site` here. Transport is derived from enrollment mode, and this view lists site-transport agents only, so a token-enrolled agent never appears — use `flw agents list` for those. |
| `transport` | `site_relay` on every row — the view lists Site Mode agents only. |
| `hb_age_s` | Seconds since the last heartbeat. Single digits is healthy. |
| `presence` | `idle`, `active`, `draining`, `reclaimed` (the user hit the takedown), `revoked` (an operator revoked this identity — §11, terminal) or `offline` (stopped heartbeating). |
| `gen` | Presence generation. It only goes up, and it is how a late older heartbeat is rejected. |
| `avail` | Whether routing would consider this agent right now: fresh, idle and unpaused. Model-independent. |
| `ready` | READY replicas on this machine. `avail=yes ready=0` means routable but nothing loaded yet. |
| `last_claim` | `finished`, `failed`, `invalid`, or `none` if it has never taken one. |
| `claim_code` | The typed reason a claim failed: `became_active`, `reclaimed`, `connect_failed`, `timeout`, `cancelled`, `upstream_error`, `deadline_expired`, `client_disconnect`. `-` when the claim ended cleanly. |

The row you want on a working desk is `presence=idle avail=yes ready=1`.

`avail=yes ready=0` right after an assignment is normal — the replica has not
finished starting. `avail=no` with a fresh heartbeat is the machine being used,
which is the system working as designed.

### Re-enrolled machines leave a permanent offline row

A machine that is wiped and re-enrolled from a **new** join file comes back as a
**new agent id**. Its old identity stays in the fleet view for good, as an
`offline` row whose `hb_age_s` grows without bound.

This is expected, not a fault. No route deletes an agent record, and the view
deliberately keeps agents past the offline threshold because a desk that stopped
heartbeating is exactly what it exists to show.

The current identity is the row with a fresh `hb_age_s`. Keep a note of which
agent id belongs to which desk as you enrol — with four desks and one re-image
you will otherwise be guessing, and `flw assign` takes ids, not hostnames.

## 9. Active-user preemption

The behaviour to demonstrate to the school, and the one they will judge the pilot
on: a person sitting down gets their machine back immediately.

On an idle desk that is serving, sit down and move the mouse. What happens, in
order:

1. Windows `GetLastInputInfo` reports input. Not CPU load — antivirus scans and
   management tools make CPU a poor proxy for a person being present.
2. The preemption controller **suspends the model process first**, then reports
   the transition.
3. The coordinator drops the agent from new gateway routing and new relay claims
   as soon as the event arrives, without waiting for the next heartbeat.
4. Any in-flight claim on that desk terminates with `claim_code=became_active`.

Verify from the coordinator:

```bash
uv run flw site status
```

Expect that desk's row at `presence=active avail=no`, with `last_claim=failed
claim_code=became_active` if it was mid-request.

Work already in flight is cancelled locally. It may be retried once elsewhere
only if no response byte had reached the client — once bytes are on the wire the
request truncates rather than being silently re-run.

Continuous activity keeps the replica suspended and eventually trips the existing
GPU eviction timer. Serving resumes only after a continuous idle period; the
desktop default is 120 seconds without input.

For an explicit takedown, on the machine itself:

```powershell
& "$env:USERPROFILE\.fallow\bin\agentctl.exe" reclaim -config "$env:USERPROFILE\.fallow\agent.toml"
& "$env:USERPROFILE\.fallow\bin\agentctl.exe" release -config "$env:USERPROFILE\.fallow\agent.toml"
```

`reclaim` shows as `presence=reclaimed avail=no` and holds there until `release`,
regardless of idle state.

## 10. Restart

**Agent restart.** Log out and back in, or reboot. The at-logon task starts the
agent, which resumes from its stored token-free site profile. It does **not**
enroll again and does not need the join file. Expect the same agent id back in
`flw site status` with a fresh `hb_age_s`.

If a desk comes back as a *new* id, its state directory did not survive. See the
persistent-state gate in the IT checklist, and §8 on the offline row the old
identity leaves behind.

**Coordinator restart.** In-flight claims are dropped without replay. Agents
notice the connection drop and resume held polling after reconnect; they do not
re-enroll. Requests in flight at the moment of restart fail and must be re-sent.

Expect every row's `hb_age_s` to spike and then settle within a few seconds. A
row that stays high after the coordinator is back means that desk cannot reach
it. Run `doctor.ps1 -Probe` there.

**Logout, sleep, fast user switching.** All three make an agent unavailable, and
all three look the same from the coordinator: `presence` goes `offline` with a
growing `hb_age_s`. `doctor.ps1` on the machine is what tells them apart:
`interactive_session` reports plainly whether anyone is signed in.

## 11. Revocation

Two things can be revoked from the coordinator, and both are one command. Both
are **terminal**: there is no un-revoke, and nothing here needs a visit to the
desk.

*Revoke an unused join file* — a stick goes missing, or a desk is never built.
Name the token by its id and void it:

```bash
uv run flw enroll list
uv run flw enroll revoke 9f2c41ab77de
```

`enroll list` prints one row per minted token: its id, whether it is
`outstanding`, `used` or already `revoked`, and when it was minted. It never
prints a token. The id is also on the mint line for each join file, which is why
§3 tells you to keep that output:

```text
join/desk-01.fallow-join site=clfs-pilot origins=https://10.24.8.10:8330 pin=sha256/AbCdEfGh token=9f2c41ab77de
```

A revoked token then fails enrollment exactly as an already-used one does, so a
desk that tries it gets the same message it would get from a copied join file.
Destroy the media anyway — revocation stops the token, not the pin or the address
printed beside it.

*Revoke an enrolled machine* — a laptop is stolen, or a desk leaves the pilot:

```bash
uv run flw agents revoke 8f10c0f3f4e14a0b9e07d3c9d5a0c111
```

From that moment the coordinator refuses every call that device token makes.
Its replicas leave routing immediately — not on the next heartbeat — its model
assignments are cleared, and any relayed request it was holding is dropped. The
desk itself notices within one heartbeat: it stops serving, kills its replicas,
writes down why, and exits quietly rather than retrying a dead token every
minute. It stays down across logons and reboots, and `doctor.ps1` on that machine
reports `identity: device token rejected by the coordinator`.

The row stays in `flw site status` with `presence=revoked` and `avail=no`, which
is what you check to confirm. There is deliberately no un-revoke: a machine you
get back is wiped, reinstalled and enrolled from a fresh join file, and comes
back as a new agent id. That is the same path a reimaged desk already takes (§8).

The two older, softer controls are still the right tool when nothing is lost and
you only want serving to stop:

*Stop one machine serving* — remove it from the assignment. `flw assign` is an
exact replace, so name the machines that should keep the model:

```bash
uv run flw assign qwen2.5-0.5b-instruct AGENT_2 AGENT_3 AGENT_4
```

*Stop the whole fleet serving a model* — the kill switch, an empty agent list.
The CLI cannot express the empty set (`flw assign <model-id>` alone is rejected
with `Missing argument 'AGENT_IDS...'`), so call the admin API:

```bash
curl -sS -X PUT "https://10.24.8.10:8330/v1/admin/assignments" \
  -H "Authorization: Bearer ${FLW_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model_id": "qwen2.5-0.5b-instruct", "agent_ids": []}'
```

Both take effect on the next heartbeat plus a following idle-gated reconcile
pass — later still on a machine in use. Confirm from `flw site status`, do not
assume. Neither invalidates a credential; revocation is what does that.

*Remove a machine's credential locally* — uninstall on the machine (§13). That
deletes the identity, which is the only place the device token exists in usable
form. Do this **as well as** revoking when you have the machine in front of you;
revoke first when you do not.

**What still has no revocation.** A compromised or lost *coordinator* key is
unchanged: rotate the certificate and mint new join files for every desk. That is
a physical visit to four machines, and hand-adding the next pin ahead of time
(§2) is what turns it into a certificate swap rather than a re-enrollment. Client
API keys minted with `flw keys new` also have no CLI revoke route yet.

## 12. Rollback

Site Mode is additive and off by default, so rollback is turning it off, not
undoing it.

**One machine back to a direct agent.** Uninstall with `-Purge` (§13), then
install without `-JoinBundle` using an ordinary token from `flw enroll new-token`.
That machine rejoins over the tailnet path with its previous behaviour unchanged:
direct replica routing, tailnet `bind_host`, no pinning. It comes back as a new
agent id.

**The whole site back to legacy.** Kill-switch the model (§11), remove `[site]`
from `coordinator.toml`, restart the coordinator. Site agents can no longer
enroll or claim; legacy agents are untouched throughout. Nothing in the site path
changes how an existing explicit-URL or Tailscale deployment behaves.

**Abandon the pilot entirely.** Kill-switch the model, uninstall on all four
desks, stop the coordinator. The registered model, the coordinator database and
the logs remain; delete them separately if the data-policy sign-off requires it.

Keep the join files' pin values written down until rollback is complete. If you
have to stand the site back up, matching the pin against what the coordinator
serves is the fastest way to tell a moved address from a changed key.

## 13. Removal

Per machine, in the pilot user's session:

```powershell
deploy\windows\uninstall.ps1          # stop the task and processes, keep ~\.fallow
deploy\windows\uninstall.ps1 -Purge   # also delete ~\.fallow: config, identity, join copy, models, logs
```

Uninstall stops and unregisters `Fallow\FallowAgent`, stops any running
`agentctl.exe` and `llama-server.exe` so no port stays bound, and removes the
`LLAMA_ARG_THREADS` cap the CPU install sets. `-WhatIf` shows what it would do and
changes nothing.

For a pilot teardown use `-Purge`. Without it the site identity survives and the
machine re-enrolls itself as the same agent at the next login, which is not what
you want on a returned PC.

Ask IT to reverse whatever they added: the EDR allowlist entries, the coordinator
egress rule, and the TLS-inspection exception.

---

## Troubleshooting

| Symptom | Likely cause | Do this |
| --- | --- | --- |
| `clock`: `skew unknown, certificate outside validity window` | This PC's clock is days or months out; the certificate is almost never the problem | Check date, time zone and NTP sync on the PC, then re-run `doctor.ps1` |
| `clock`: `offset +NNNs ... over the 120s limit` | Clock drift, or no NTP source on this VLAN | Point the PC at a reachable time server. Pinned TLS fails next if you leave it |
| `spki_tls`: `pin mismatch: server SPKI ...` | A TLS-inspecting proxy is terminating the connection | Exempt the coordinator host and port from interception, then retry with the same join file. Never relax the pin |
| The agent exits with `pin mismatch` in its log, but `doctor.ps1` says nothing is wrong | Plain `doctor` validates pins without connecting, so it cannot see a middlebox | Re-run with `-Probe`, which is the lane that opens the connection |
| `-Probe`: `blocked TCP` | Firewall or VLAN, not TLS at all | Open outbound TCP to the coordinator's exact address and port |
| Enrolled but `avail=no` with a fresh heartbeat | Someone is using the machine, or a takedown is set | Expected. Check `presence`: `active` is a person, `reclaimed` needs `agentctl release` |
| `avail=yes ready=0` after assigning | The replica has not started; reconcile runs only while idle | Wait for the machine to go idle, then re-check `flw site status` |
| Enrollment fails on the second desk | The same join file was used twice; tokens are single-use | Use that desk's own file, or mint a fresh batch |
| Enrollment reports an ambiguous result | Registration may have reached the coordinator but the response was lost | Mint a new join file for that desk. Do not retry the old one |
| Enrollment fails with `pin mismatch` | Interception, not an ambiguous result — nothing was written and the token was not consumed | Get the exemption, then retry the same join file |
| A desk is permanently `offline` with a growing `hb_age_s`, but works | It re-enrolled; this is the old identity | Expected. The current identity is the row with a fresh `hb_age_s` |
| A desk stopped serving and its row reads `presence=revoked` | Someone revoked that agent (§11) | Terminal by design. Reinstall the desk with a fresh join file: `install.ps1` sees the revoked identity, replaces it, and logs that it did. `uninstall.ps1 -Purge` first is the full clean, and is what to use if anything else on the desk is suspect |
| `doctor`: `identity: device token rejected by the coordinator` | Same, read from the desk's side | The agent will not start again on this identity. Re-enrol it |
| Every desk stops serving at once, `flw site status` lists none of them, and each agent log repeats `coordinator rejected credentials (401): invalid device token` | The coordinator lost its database, was restored from an older copy, or was started on the wrong `db_path` | Not revocation, and deliberately not recorded as one: `doctor` still reports each desk's identity as enrolled, and the Scheduled Task keeps retrying every minute. Fix the coordinator's `db_path` or restore its database and the fleet comes back on its own, with no visit to any desk |
| The coordinator refuses to start | Wildcard bind, an HTTP public URL, or missing/expired TLS files | Read the startup error; it names which one |
| `flw site status`: `admin key rejected` | `FLW_ADMIN_KEY` unset or wrong | Exit code 2 means auth. Re-export it |
| `flw site status`: `coordinator unreachable at ...` | Wrong URL, coordinator down, or your own TLS path | Check the `FLW_COORDINATOR_URL` scheme and port first |
| Nothing on `avahi-browse` / `dns-sd` | Multicast is filtered on the VLAN | Supported outcome. Static addressing keeps working; do not rely on discovery |

---

## Acceptance

Two columns, and they are not interchangeable. Everything under **Automated
evidence** is proven in CI on the built Go binary against a real pinned-HTTPS
coordinator. Everything under **School-only** can only be proven on the school's
own network and hardware, and stays a manual gate until someone runs it there and
records the result.

### Automated evidence

| Claim | Where |
| --- | --- |
| Buffered and SSE requests traverse client → coordinator → Go agent → loopback llama with unchanged status, content type and body bytes | `tests/integration/site_mode/test_site_acceptance.py::test_static_site_vertical_buffered_and_sse` |
| A request truncates rather than silently re-running once a byte has reached the client | `…::test_no_retry_after_first_byte_truncates_e2e` |
| Reclaim suspends serving; release resumes it | `…::test_reclaim_suspends_then_release_resumes` |
| Agent restart resumes from the stored profile with no re-enrollment | `…::test_agent_restart_resumes_without_reenrollment` |
| Coordinator restart drops claims and agents resume held polling | `…::test_coordinator_restart_resumes_held_polling` |
| With `auto_assign_on_enroll`, a desk is assigned by fit at enrolment and serves with no operator assignment, passing over a model it cannot hold | `…::test_auto_assign_on_enroll_serves_without_an_assignment` |
| A wrong pin sends no token, bearer or body | `tests/integration/site_mode/test_site_trust.py::test_wrong_pin_enrollment_fails_and_leaks_no_token` |
| Proxy environment variables are ignored on enrollment | `…::test_proxy_env_is_ignored_on_enrollment` |
| An intercepted origin gets a handshake and nothing else: no request bytes, no bearer, no token, and no fallback to cleartext or a proxy | `tests/integration/site_mode/test_interception.py::test_interception_writes_no_request_bytes_and_no_credential` |
| A pin mismatch reads apart from an unreachable coordinator on the same origin | `…::test_pin_mismatch_reads_apart_from_an_unreachable_coordinator` |
| The recording listeners capture a client that skips the pin check, so an empty recording is silence rather than a deaf instrument | `…::test_the_listeners_record_a_client_that_does_not_check_the_pin` |
| Interception in front of a serving desk leaves enrollment intact, and claims resume on the same identity once it is gone | `…::test_interception_leaves_enrollment_intact_and_claims_resume` |
| The coordinator refuses a cleartext public URL, a wildcard bind, or missing TLS files | `tests/integration/site_mode/test_site_trust.py::test_coordinator_rejects_cleartext_public_url`, `…::test_coordinator_rejects_wildcard_bind`, `…::test_coordinator_rejects_missing_tls` |
| `doctor` rejects a non-loopback bind in Site Mode | `…::test_agent_doctor_rejects_non_loopback_site_bind` |
| Legacy explicit-URL behaviour is unchanged | `tests/integration/site_mode/test_site_parity.py::test_direct_mode_parity_unchanged` |
| `flw site status` reports the live fleet and carries no join material | `tests/integration/site_mode/test_fleet_status.py::test_status_reports_the_live_harness_agent`, `packages/fallow-coordinator/tests/site/test_status_route.py::test_status_carries_no_join_material` |
| A moved coordinator is recovered without re-enrollment | `tests/integration/site_discovery/test_address_move.py::test_moved_coordinator_is_recovered_without_re_enrolment` |
| A wrong-key responder receives no request | `tests/integration/site_discovery/test_wrong_key.py::test_a_wrong_key_responder_receives_no_request` |
| A blocked segment leaves the static profile and its pins byte for byte | `tests/integration/site_discovery/test_static_fallback.py::test_a_silent_segment_keeps_the_profile_and_the_pins` |
| A legacy agent starts no discovery | `tests/integration/site_discovery/test_legacy_mode.py::test_legacy_direct_mode_starts_no_discovery` |

Run the site lanes locally:

```bash
uv run pytest tests/integration/site_mode tests/integration/site_discovery
```

A skip is a failure in these lanes. They build and drive the real `agentctl`
binary, and a missing Go toolchain fails loudly rather than passing quietly.

### School-only checks

Each has a command and the exact output that counts as a pass. None of it is
proven by CI.

| # | Check | Command | Pass looks like |
| --- | --- | --- | --- |
| S1 | The desktop reaches the coordinator port with no proxy in the way | `deploy\windows\doctor.ps1 -Probe` | `spki_tls`: `reachable; presented cert SPKI matches a pinned key` |
| S2 | TLS inspection is exempted for the coordinator | as S1 | *not* `pin mismatch: server SPKI ...`, and no `pin mismatch` in the agent's log |
| S3 | EDR and SmartScreen permit `agentctl.exe` and `llama-server.exe` | `deploy\windows\doctor.ps1` | `task_running`: `running`, and no quarantine alert on the security console |
| S4 | The pilot account is logged in and the task can run | `deploy\windows\doctor.ps1` | `interactive_session`: `an interactive user session is active` |
| S5 | The PC clock is inside 120s of the coordinator | `& "$env:USERPROFILE\.fallow\bin\agentctl.exe" doctor -config "$env:USERPROFILE\.fallow\agent.toml"` | `clock`: `offset ±Ns against the coordinator`, and `"ok": true` |
| S6 | Replicas are loopback-only on the real network | `deploy\windows\doctor.ps1` | `loopback_bind`: `bind_host=127.0.0.1; no replica port exposed off loopback` |
| S7 | No replica is visible from another pilot machine | port scan of the replica range from a second desk | no open port in `8100`–`8115` |
| S8 | Identity survives reboot, profile cleanup and any reimaging product | reboot, then `uv run flw site status` | the **same** agent id, `hb_age_s` back to single digits |
| S9 | Sleep policy keeps the machine available overnight | `uv run flw site status` the next morning | `hb_age_s` in single digits, not thousands |
| S10 | The mDNS advertisement is audible on the school LAN *(only if mDNS is on)* | `avahi-browse -rt _fallow._tcp` from an agent machine | an instance carrying `site_id=<your site_id>` and `version=1` in TXT, SRV pointing at the §1 address |
| S11 | A user sitting down takes the machine back | move the mouse on a serving desk, then `uv run flw site status` | that row at `presence=active avail=no`, and `claim_code=became_active` if it was mid-request |

S8 is a blocker, not a nice-to-have. If the identity does not survive the
school's reimaging policy, the pilot stops until IT provides persistent storage
or enrollment is redesigned. A machine that re-enrolls on every boot burns a join
file per boot and leaves a new offline row each time.

## Honest gaps

Named here rather than left to be discovered:

- **The multicast hop between our own two components is covered by no test.** §7
  is its closer, and it is a pilot-day check on the real LAN. Static addressing is
  the baseline precisely because of this.
- **The Windows install path was authored without a Windows host.** CI now
  installs, registers and uninstalls it for real on a `windows-latest` runner
  (both the walk-to-the-desk and the `-User` admin path), so the Task Scheduler
  and ACL calls are marked `(exercised in CI on windows-latest - verify on
  target)`. What no runner can prove is the part that needs a person: that the
  task starts at a real logon, that the desk serves, that EDR and SmartScreen
  allow it. Prove it on one machine before rolling out to four.
- **School VLAN, proxy, EDR, power and reimage behaviour are not proven by
  sandbox tests.** They are the school-only table above.
- **Revocation covers desks and join tokens, not the coordinator's own key.**
  `flw enroll revoke` and `flw agents revoke` are terminal and take effect at
  once (§11). A compromised coordinator certificate still means rotating it and
  re-minting every join file, and client API keys still have no CLI revoke. An
  unused join file in the field also stays live until someone revokes it by id —
  minting prints that id, and `flw enroll list` recovers it, but nothing expires
  on its own.
- **`llama-server` is unauthenticated.** Loopback binding is the whole control:
  the config refuses a non-loopback bind in Site Mode, and `doctor.ps1` fails if a
  replica port is listening off loopback.
- **Fallow is pre-alpha and has had no production security audit.** A green test
  suite is not one.
