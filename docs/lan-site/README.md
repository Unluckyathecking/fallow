# LAN Site Mode

LAN Site Mode lets an on-site coordinator use nearby Windows agents without Tailscale or an internet connection. It is opt-in. Existing explicit URL and Tailscale deployments keep their current behaviour.

The pilot has a narrow purpose: prove that four school PCs can contribute inference safely, with little background overhead, and yield as soon as a user returns.

## Design

The coordinator is the only LAN listener. Each Windows agent connects to it over pinned HTTPS, claims inference work with a held HTTP request, calls its own loopback-only `llama-server`, and streams the response back over HTTPS. The coordinator never opens a connection to an agent.

A site agent is routable only when all of these are true:

- its heartbeat is fresh;
- the operating-system idle detector reports the nominated user as idle;
- the user has not reclaimed the machine;
- the assigned model fits the reported memory and VRAM;
- a matching replica is ready; and
- the agent is actively waiting for a relay claim.

Windows user presence comes from `GetLastInputInfo`, not a CPU-usage guess. CPU, RAM and GPU telemetry remain useful for capacity and health, but antivirus scans and school management tools make them poor evidence that a person is present.

When input resumes, the existing preemption controller suspends the model process before it reports the transition. The coordinator then removes the agent from new routing. If activity continues, the current eviction policy stops suspended GPU replicas. The site relay adds no second idle state or competing policy.

## Trust boundary

Each PC receives a small join file through USB, MDM or another trusted local channel. The file contains the coordinator address, a set of trusted SPKI fingerprints and one single-use enrollment token. The agent checks the pin before sending that token or any later credential.

Site traffic never uses a configured HTTP proxy, WPAD or PAC route. A pin mismatch, TLS error or blocked connection is reported and never retried over HTTP. `llama-server` listens on loopback only.

The MVP uses the existing random per-device bearer credential over pinned TLS. It does not add client certificates. Credentials remain independently revocable and are hashed by the coordinator. mTLS can be considered later if school IT requires it or the fleet grows enough to justify certificate lifecycle work.

## Addressing

Every join file contains at least one static HTTPS address. A DHCP reservation or internal DNS record is the dependable school setup. Optional mDNS support comes after the static-path acceptance test and supplies addresses only. It never supplies a pin, token or site identity.

## Scope

The production Site Mode agent is the Go Windows agent. The Python agent is unchanged. The implementation must finish the Go model reconciliation path so assignments actually start and stop loopback replicas.

The nominated pilot account must be logged in. This preserves Windows input detection and the current at-logon task model. Logout, sleep and fast-user switching make that agent unavailable; diagnostics must say so plainly.

Model staging is the coordinator's job and only the coordinator's. "No internet connection" is a statement about the desks: the coordinator host may be the one machine with egress, and `flw models pull` uses it to fetch a GGUF from huggingface.co. Agents never receive an internet source — they fetch blobs from the coordinator over pinned HTTPS. A site where the coordinator has no egress either stages the file elsewhere, carries it in, and registers it with `flw models register --file`. See the [operator runbook §3](operator-runbook.md#3-four-per-device-join-files).

## Contracts

- [Join file v1](join-bundle-v1.md)
- [HTTP relay v1](relay-v1.md)
- [Pilot acceptance](acceptance.md)
