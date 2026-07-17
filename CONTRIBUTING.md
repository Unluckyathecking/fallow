# Contributing to Fallow

Thank you for helping build Fallow. Contributions of code, tests, documentation, designs and
field reports are welcome. By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

For a small fix, open a pull request directly. For a new feature, protocol change, dependency
addition, security-sensitive change or architectural decision, open an issue first so the
approach can be discussed before significant work begins. Security reports follow
[SECURITY.md](SECURITY.md), never a public issue.

The project is pre-alpha. Prefer small, reviewable changes and keep backward compatibility
unless a breaking change has been agreed and documented.

## Development setup

Install Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/Unluckyathecking/fallow.git
cd fallow
uv sync --frozen --dev
uv run pytest
```

Create a focused branch from the repository's default branch. Do not commit virtual
environments, model weights, databases, generated run output or credentials.

## Quality gate

Run these checks after your final edit:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest
uv run python -m fallow_protocol.export_schemas schemas/
git diff --exit-code schemas/
uv build --all-packages
```

Add or update tests for changed behaviour. Platform-specific code should include deterministic
tests with injected system boundaries. Avoid tests that require a network, GPU, model download
or a running inference server unless they are explicitly marked as integration tests.

## Pull requests

- Explain the user problem and the chosen approach, not just the files changed.
- Link the issue and call out breaking changes, migrations, security implications and new
  dependencies.
- Update documentation, schemas and `CHANGELOG.md` when users are affected.
- Keep unrelated formatting or refactors out of the change.
- Confirm the checklist in the pull request template.

Maintainers may ask for an architecture decision record under `docs/adr/` for changes that set
long-lived constraints. Use the next available number and describe context, decision,
alternatives and consequences.

## Compatibility and API changes

The supported environment matrix is defined in [docs/compatibility.md](docs/compatibility.md).
Public API and deprecation rules are defined in [docs/api-stability.md](docs/api-stability.md).
Breaking wire changes must bump `PROTOCOL_VERSION`, regenerate JSON schemas, document the
migration and be called out in the changelog.

## Dependencies

New runtime dependencies need a clear reason, a compatible license, active maintenance and a
security review. Prefer standard-library or existing dependencies when practical. Commit
`uv.lock` changes with the manifest change and describe material transitive changes in the PR.

## Licensing and Developer Certificate of Origin

All contributions are licensed under AGPL-3.0-or-later. Fallow uses the
[Developer Certificate of Origin 1.1](https://developercertificate.org/) instead of a separate
contributor licence agreement. Add a sign-off to every commit:

```text
Signed-off-by: Your Name <your.email@example.com>
```

Use `git commit -s` to add it. The sign-off certifies that you have the right to submit the work
under the project's licence. Do not contribute code, assets or model weights whose terms are
incompatible with AGPL-3.0-or-later or which you are not authorised to share.
