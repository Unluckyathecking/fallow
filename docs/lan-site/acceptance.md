# LAN Site Mode pilot acceptance

The pilot passes only when the static-address path works without Tailscale, an internet connection or inbound desktop firewall rules.

## Network and trust

- The coordinator listens on one approved LAN address over HTTPS. Site Mode cannot start with missing TLS files, a wildcard address or an HTTP public URL.
- Each desktop can open the chosen coordinator TCP port directly without an HTTP proxy.
- A correct SPKI pin enrolls. A wrong pin sends no token, bearer or request body.
- Site clients ignore proxy environment variables, PAC, WPAD and WinHTTP proxy settings.
- Device tokens are unique, hashed by the coordinator, independently revocable and absent from logs and command lines.
- `llama-server` is visible on loopback only. A scan from another pilot machine finds no replica listener.

## Availability and user return

- A fresh IDLE heartbeat with a READY replica and a held claim makes the agent routable.
- Fresh keyboard or mouse input suspends the replica before the presence event is sent.
- The coordinator removes that agent from new gateway routing and relay claims as soon as the event arrives, without waiting for the next five-second heartbeat.
- A delayed older IDLE heartbeat cannot undo a newer active event.
- Existing in-flight work is cancelled locally. It may be retried once elsewhere only if no response byte reached the client.
- Continuous activity keeps the replica suspended and triggers the existing GPU eviction timer. Continuous idleness is required before serving resumes.
- `serving_paused`, a suspect heartbeat, no claim waiter or no READY replica all make the agent unavailable.

## Model lifecycle and load

- The Go agent consumes `desired_models`, fetches the authenticated manifest and blob, verifies the model, starts it on loopback and reports READY.
- Removed assignments stop their replicas and release resources.
- Model requirements are checked against current RAM and VRAM before assignment. The pilot does not infer human presence from CPU load; Windows last-input time is the direct signal.
- One buffered request and one SSE request traverse client, coordinator, Go agent and loopback fake llama with unchanged status, content type and body bytes.

## Recovery

- Coordinator restart drops claims without replay and agents resume held polling after reconnect.
- Agent restart uses its stored token-free site profile and does not enroll again.
- Logout, sleep and fast-user switching appear as agent unavailable. Diagnostics distinguish this from a blocked port, TLS failure, pin mismatch and revoked credential.
- The installed state survives the school PC's confirmed reboot or reimage policy. If it does not, the pilot is blocked until IT provides persistent storage or enrollment is redesigned.
- Existing explicit URL and Tailscale tests remain green and retain direct replica routing.

## Optional discovery

After the static pilot path passes, mDNS may advertise `_fallow._tcp.local.`. Its output is only an address candidate. The agent still requires the join file's site identifier and SPKI pin. Blocking multicast must leave the static path healthy.
