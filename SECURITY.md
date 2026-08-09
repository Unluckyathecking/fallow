# Security policy

## Supported versions

Fallow is pre-alpha and has no supported production release. Security fixes are applied to the
default branch. Once releases begin, this table will identify supported release lines.

| Version | Supported |
| --- | --- |
| Default branch | Best effort |
| Tagged pre-1.0 releases | No guaranteed backports |

## Report a vulnerability

Please report suspected vulnerabilities privately using a
[GitHub security advisory](https://github.com/Unluckyathecking/fallow/security/advisories/new).
Do not open a public issue, discussion or pull request containing exploit details, secrets or
sensitive deployment information. If private reporting is unavailable, contact the maintainer
through their GitHub profile to establish a private channel before sharing details.

Include affected versions or commit, impact, reproduction steps or a proof of concept, and any
suggested mitigation. You should receive an acknowledgement within seven days and a status
update within fourteen days. Timelines are targets for this volunteer-maintained project, not a
service-level agreement.

Maintainers will validate the report, coordinate a fix and credit the reporter unless anonymity
is requested. Please allow a reasonable remediation window before public disclosure.

## Security boundaries

Fallow currently assumes deployment on a trusted private network. It does not yet provide mTLS,
rate limiting, multi-tenancy isolation, a hardened secrets store, high availability or a
completed production entrypoint. `llama-server` is unauthenticated, so each agent binds its
replicas to the agent's tailnet IP in production and to loopback only for single-machine
development; the supervisor rejects wildcard binds ([ADR 052](docs/adr/052-replica-bind-address-safety.md)).

Transport confidentiality comes from the tailnet (Tailscale or WireGuard), not from Fallow
itself. There is no application-layer TLS or mTLS yet. IT reviewers should treat the tailnet as
the encryption and access-control boundary for all agent and coordinator traffic; application-layer
mTLS is a planned addition, not a current control.

**LAN Site Mode is the exception, and only for agents that opt into it.** A Site Mode agent
reaches its coordinator over HTTPS pinned to the SHA-256 of the certificate's DER-encoded
SubjectPublicKeyInfo, checked before any authorization header, enrollment token or request body is
written. It ignores environment, WinHTTP, PAC and WPAD proxy settings, follows no redirects, and
never falls back to cleartext: a pin mismatch is a hard failure. Its `llama-server` replicas bind
`127.0.0.1` only, so no inference port is reachable from the LAN and no inbound firewall rule is
needed. Device credentials remain random per-device bearer tokens, hashed by the coordinator;
Site Mode adds no client certificates and no mTLS.

Two limits worth stating plainly. There is no per-token revocation route in this version — an
enrolled device token cannot be invalidated from the coordinator, and neither can an unused
enrollment token, so recovery from a compromised credential is a certificate rotation and a new
join file per machine. And a join file is a credential: it carries a single-use enrollment token
with no expiry, and it stays live until it is used. Legacy tailnet deployments are unaffected by
either.

Model files are executable-adjacent supply-chain inputs. Operators must verify provenance,
licensing and hashes and should not load untrusted models. Never commit real keys, enrollment
tokens, prompts, documents, model weights, databases or audit logs.

These limitations are tracked in the [roadmap](ROADMAP.md). A successful test suite is not a
security audit.
