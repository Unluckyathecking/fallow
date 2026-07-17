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

Model files are executable-adjacent supply-chain inputs. Operators must verify provenance,
licensing and hashes and should not load untrusted models. Never commit real keys, enrollment
tokens, prompts, documents, model weights, databases or audit logs.

These limitations are tracked in the [roadmap](ROADMAP.md). A successful test suite is not a
security audit.
