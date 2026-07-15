# API stability and deprecation policy

Fallow is pre-1.0. Interfaces may change, but changes must be intentional, documented and easy
for early adopters to evaluate.

## Public surfaces

The following are public compatibility surfaces:

- Models and constants exported by `fallow_protocol`
- JSON schemas committed under `schemas/`
- The wire `PROTOCOL_VERSION`
- Documented HTTP routes and request/response bodies
- The `flw` command names, options, exit codes and machine-readable JSON output
- Objects explicitly re-exported from a package's top-level `__init__.py`

Underscore-prefixed objects, test helpers, experiment scripts, undocumented module internals and
the contents of SQLite databases are private implementation details.

## Versioning

Published packages will use Semantic Versioning. Before 1.0, a minor release may contain a
breaking change; patch releases must remain backward compatible. All workspace packages share a
coordinated release version unless maintainers document a different policy before publishing.

`PROTOCOL_VERSION` is independent of package versions. Any incompatible wire change must:

1. bump `PROTOCOL_VERSION`;
2. regenerate and review JSON schemas;
3. update coordinator/agent mismatch tests;
4. add a migration note to `CHANGELOG.md`;
5. avoid silently accepting an incompatible peer.

## Deprecation

When practical, a public Python, HTTP or CLI surface will be deprecated for at least one minor
release before removal. The deprecation must appear in documentation and the changelog and emit
an actionable warning where that can be done safely. Security fixes or demonstrably unusable
interfaces may require faster removal; the release notes must explain why.

No stability guarantee applies until the first public release. Even during pre-alpha, pull
requests should preserve compatibility unless an issue or ADR explicitly approves a break.
