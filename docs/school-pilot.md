# Fallow school pilot — IT readiness

For the IT administrator deciding whether to allow a Fallow pilot on school
machines. It states what Fallow installs, what it touches on the network, how you
stop it, and how you remove it, with an acceptance test you can run before signing
off. Every claim here traces to the code and the design records; where something is
untested or missing, it says so plainly.

Fallow is pre-alpha and has not had a production security audit. Treat a pilot as
evaluation, not production. This page is the readiness brief; the hands-on install
steps live in the [IT checklist](pilot/it-checklist.md) and
[`deploy/README.md`](../deploy/README.md), and the day-to-day operator work in the
[admin runbook](pilot/admin-runbook.md).

## 1. What Fallow is, and what the pilot is allowed to do

Fallow turns spare capacity on desktops and workstations into a small, centrally
governed inference cluster. A **coordinator** on a machine that stays up owns
identity, policy, scheduling and audit; each participating machine runs an **agent**
that serves model requests only while the machine is idle and yields the instant a
person returns.

The pilot restricts itself to uses that are **not** high-risk under the EU AI Act.
In scope: private RAG search over institutional documents, an internal coding
assistant, and overnight indexing, embedding or transcription of institution-owned
material. Explicitly out of scope, and not to be enabled: anything touching
**grading, assessment or progression**, **admissions**, **behaviour monitoring,
proctoring or profiling**, and **emotion or biometric** processing. The full scope,
including why education is treated as a sensitive domain, is in
[`ai-act-scoping.md`](ai-act-scoping.md).

Responsible-use baseline for a pilot: process **public or synthetic data only, no
personal or student data**, until the deploying institution has completed its own
data-protection review. There is a sign-off template for that decision at
[`pilot/data-policy-signoff.md`](pilot/data-policy-signoff.md). Fallow keeps prompts,
documents and model weights on institution-controlled machines; it does not send them
to any external service.

## 2. Network requirements

**Everything rides on a tailnet.** Fallow v0.1 has no transport encryption of its own.
Confidentiality and access control come entirely from a Tailscale (WireGuard) tailnet
that every machine joins before anything else. If a machine is not on the tailnet,
nothing protects its traffic. State this to whoever owns your network: the tailnet is
the encryption and access-control boundary for the whole deployment
([ADR 000 §6](adr/000-architecture-baseline.md), [ADR 059](adr/059-transport-security-reconciliation.md)).

**No application-layer TLS.** There is no HTTPS, no mTLS, and no per-request
authentication on the inference servers. `llama-server` is unauthenticated. This is a
known, deliberate v0.1 limitation, not an oversight: Fallow delegates confidentiality
to the tailnet and layers mTLS in later. Do not expose any Fallow port to the LAN or
the internet.

**What binds where** ([ADR 052](adr/052-replica-bind-address-safety.md)):

| Component | Binds to | Port(s) |
| --- | --- | --- |
| Coordinator admin API + OpenAI-compatible gateway + `/metrics` | coordinator's tailnet IP | one HTTP port (8330 in the example config) |
| Agent `llama-server` replicas | the agent machine's **tailnet IP**, in production | a contiguous range (default 8100–8115: `port_range.start` 8100, `count` 16) |

In production each agent sets `bind_host` to its own `100.x.y.z` tailnet address, so
replicas are reachable only across the tailnet. Loopback binding is for
single-machine development only. The supervisor **refuses to start** if it is given a
wildcard bind (`0.0.0.0`, `::`, empty, or an equivalent unspecified address), because
that would put an unauthenticated inference endpoint on every interface including the
office LAN. Scope any inbound firewall rule for the replica ports to the **Tailscale
adapter only**.

The user-return control (section 5) is a local file, not a network listener, so it
adds no port and is never reachable off the machine.

## 3. What gets installed

Per participating machine (see [`deploy/README.md`](../deploy/README.md) for the exact
scripts):

- **The Fallow agent** — either a Python virtualenv built with `uv` from a git
  checkout, or a single prebuilt Go binary (`agentctl`). Same config, same behaviour.
- **`llama-server`** (from llama.cpp) — a pinned third-party binary the agent
  supervises to serve models. It is **not** bundled; you stage it once per platform
  with the fetch script, which records its SHA256 into `deploy/llama-version.lock`.
- **A per-user autostart entry** so the agent starts at login and restarts after a
  crash:
  - **macOS:** a per-user **LaunchAgent** loaded in `gui/$UID`, with `KeepAlive`.
  - **Windows:** an **at-logon Scheduled Task** running `pythonw -m fallow_agent run`
    in the user session (`InteractiveToken`, least privilege, no console window), with
    `RestartOnFailure`.

The agent must run **inside the logged-in user's GUI session**, not as a system
service. Idle detection reads a per-session input timer that returns nothing from
session 0, so a service would read the machine as permanently idle and never yield to
the person using it. The consequence: the agent starts at the **next login**, not at
boot before anyone has logged in, and it stops on logout. There is no headless service.

**Where state lives:** everything the agent writes is under `~/.fallow/` —
`agent.toml` (config), a `0600` identity file, the model cache (`~/.fallow/models`),
the event log (`events.jsonl`), results, and logs (`~/.fallow/logs/`). Nothing is
written outside the user profile.

Only macOS and Windows agents are provisioned. Linux agents on ordinary user machines
are **not** supported in v0.1 (the coordinator can run on Linux).

## 4. Binary and model provenance

Fallow verifies bytes by hash before it trusts them, but nothing is cryptographically
code-signed yet — plan for unsigned binaries in the pilot.

- **Offline install bundle:** the installer verifies every file against a
  `manifest.sha256` before it changes anything, rejects any file not listed in the
  manifest and any unsafe path, and installs with `--no-index`. A `--dry-run` preview
  runs the same verification without touching the target. See
  [`deploy/OFFLINE.md`](../deploy/OFFLINE.md).
- **Model blobs:** before a replica loads a model, the agent's model cache checks the
  downloaded blob's **SHA256 and byte size** against the manifest the coordinator
  registered, and raises a verification error (refusing to serve) on any mismatch.
  Models are pulled from the coordinator over the tailnet, not the public internet.
- **`llama-server`:** pinned to a recorded SHA256 in `deploy/llama-version.lock` so
  every machine runs identical bytes. llama.cpp publishes no per-asset checksum, so
  verify the pinned release tag and asset names against the upstream releases page on
  first use.

**Honest limits:** the `manifest.sha256` is a hash manifest, not a cryptographic
signature; there is no code-signing of the launcher or `llama-server` in v0.1
(code-signing is a later consideration). On Windows, expect Defender/SmartScreen to
flag the unsigned `llama-server.exe` and `pythonw.exe`; allowlisting is an
organisational task with days-to-weeks lead time — prefer a hash-based allow rule
pinned to the lockfile SHA256. Details in
[`deploy/README.md` §5.1](../deploy/README.md#51-defender--smartscreen-allowlisting-plan-ahead--org-lead-time).

## 5. Stopping it: kill switch and instant reclaim

Two independent controls, one for the person at the machine and one for the operator.

**Instant reclaim (the person at the machine)** — `python -m fallow_agent reclaim`
stops all local serving now and keeps it stopped until `python -m fallow_agent
release`. It suspends running work first for immediate relief (engineering target p99
under 300 ms), then stops the replicas to free RAM and VRAM, and it stays down
regardless of idle detection until released. A response mid-stream can be cut off;
that is intended — the point is to give the person their machine back immediately
([ADR 042](adr/042-instant-takedown.md)). This is on top of automatic preemption,
which already yields when it detects the user typing or moving the mouse.

**Fleet kill switch (the operator)** — remove a model from every agent so no replica
serves it, via an admin-authenticated `PUT /v1/admin/assignments` with an empty agent
list. It is **not instantaneous**: each agent picks up the empty set on its next
heartbeat and drops the replica on a following idle-gated reconcile pass, so an agent
whose user is active keeps serving until it next goes idle. Confirm from
`flw agents list` rather than assuming. It does not delete the model blob or revoke
API keys — see the [admin runbook](pilot/admin-runbook.md#kill-switch) for the exact
call and its limits.

## 6. Clean uninstall

The uninstall scripts remove the autostart entry and stop the agent:

- **macOS:** `deploy/macos/uninstall.sh` removes the LaunchAgent; add `--purge` to
  also delete `~/.fallow`.
- **Windows:** `deploy\windows\uninstall.ps1` removes the Scheduled Task; add
  `-Purge` to also delete `~\.fallow`.

Without the purge flag, `~/.fallow` (config, identity, model cache, logs) is left in
place so a reinstall keeps the machine's enrolled identity. With it, the agent leaves
nothing behind in the user profile. It installs nothing outside the user profile and
no system service, so a purge uninstall returns the machine to its prior state. Any
Windows Defender/AppLocker allow rules you added in section 4 are yours to remove
separately.

## 7. Rollback

- **Agent version:** the agent is a virtualenv or a single binary under the user
  profile. To roll back, run the installer against the previous git checkout, or point
  it at the previous released `agentctl` with `--go-binary` (macOS) / `-GoBinary`
  (Windows). The enrolled identity in `~/.fallow` survives, so no re-enrollment.
- **`llama-server` version:** bump `LLAMA_RELEASE` in the fetch script back to the
  prior tag and re-fetch; the lockfile re-pins the bytes.
- **A bad model:** un-assign it fleet-wide (section 5) and assign the previous model.
  Blobs stay registered, so reverting is a re-assignment, not a re-download.
- **Full stop:** the purge uninstall in section 6 removes the agent entirely from a
  machine.

## 8. Phase-A acceptance test

Run this on a small set of representative machines before a wider pilot. Each row is
an observable behaviour with a check IT can perform. "Expected" is what a correct
pilot does; investigate anything that deviates. Note that the LaunchAgent / Scheduled
Task registration itself is **untested in the project's sandbox** and is one of the
things you are verifying here.

| # | Test | How to run it | Expected result |
| --- | --- | --- | --- |
| 1 | Clean install | Run the platform installer on a fresh machine; `flw agents list` on the coordinator | Agent enrolls, appears in the list, serves a request when idle |
| 2 | Reboot persistence | Reboot, then log the user back in | Agent restarts automatically at login (not at boot — it needs the GUI session); reappears in `flw agents list` |
| 3 | User-return preemption | While the agent is serving, move the mouse / type at the machine | Serving yields promptly; replica suspends (target p99 under 300 ms) |
| 4 | Agent-killed reroute | Kill the agent process on one machine while another serves the same model | Coordinator marks it suspect (~15 s) then offline (~45 s); interactive traffic reroutes to the other replica; leases requeue |
| 5 | Coordinator-restart recovery | Restart the coordinator process | Agents re-register on their next heartbeat and reappear; in-flight batch leases requeue; no data loss (state is persisted) |
| 6 | Network-removed, no storm | Drop the machine off the tailnet, then rejoin | Agent backs off and reconnects without a tight reconnect loop; check `~/.fallow/logs/agent.err.log` for steady backoff, not a flood |
| 7 | Model-corrupt rejection | Corrupt a staged model blob, then have the agent load it | Agent refuses to serve and logs a verification error (SHA256 / size mismatch); no replica starts on the bad file |
| 8 | Active-user suspend | Keep the machine actively in use, then assign it a new model | No new replica starts while the user is active; reconcile defers until the machine is idle |
| 9 | Log hygiene | Inspect `gateway.jsonl` on the coordinator after some requests | Only per-request **metadata** (client key name, model, agent, timestamps, status, prompt-length count) — **no** prompt text, document or response content, and no end-user identity |

For the reclaim path specifically: run `python -m fallow_agent reclaim` on a serving
machine and confirm serving stops and traffic reroutes; run `release` and confirm
normal idle-based serving resumes.

## 9. Network diagram

```text
                    school tailnet (Tailscale / WireGuard)
                    — the only confidentiality + access boundary; no app-layer TLS
   ┌───────────────────────────────────────────────────────────────────────┐
   │                                                                         │
   │   clients ──> OpenAI-compatible gateway ─┐                              │
   │   (staff/lab apps)                        │                             │
   │                                           v                             │
   │                                     coordinator                         │
   │                         (admin API + gateway + /metrics,                │
   │                          bound to its tailnet IP, e.g. :8330)           │
   │                                           │                             │
   │                     assign / heartbeat / model blobs                    │
   │                 ┌─────────────────────────┼─────────────────────────┐   │
   │                 v                         v                         v   │
   │              agent                     agent                     agent │
   │        llama-server replicas    llama-server replicas    (idle only)   │
   │        bound to THIS machine's   bound to THIS machine's                │
   │        tailnet IP, ports 8100+   tailnet IP, ports 8100+                │
   │                                                                         │
   └───────────────────────────────────────────────────────────────────────┘

   Off-tailnet (LAN / internet): nothing. No Fallow port is exposed there.
```

## Current limitations, stated plainly

- **Installers are pilot-grade.** The scripts were authored in a sandbox with no
  network and no macOS/Windows service host. Python packaging, hash verification and
  config handling are tested; the `launchd` / Task Scheduler registration and the
  downloads are **not** — verify them on one real machine of each kind (that is what
  section 8 is for).
- **CUDA-vs-CPU backend selection is still being hardened.** No llama.cpp revision,
  GPU driver, CUDA toolkit or model format is certified yet; the CPU-only, Apple
  Silicon and NVIDIA paths are still being shaken out. Expect to pin and test the
  `llama-server` build per machine class rather than assume automatic backend
  selection is correct. NVIDIA telemetry needs a compatible driver; CPU-only and
  Apple Silicon machines must not require it.
- **No application-layer TLS or mTLS, no per-request auth on `llama-server`, no
  rate limiting or multi-tenancy isolation.** The tailnet is the boundary. See
  [SECURITY.md](../SECURITY.md).
- **No production security audit.** A passing test suite is not a security audit.
- **Binaries are unsigned.** Provenance is by hash (section 4), not by code signature.

These are tracked on the [roadmap](../ROADMAP.md). If any of them is a blocker for
your environment, hold the pilot until it is addressed rather than working around it.
