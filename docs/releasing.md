# Release process

Only maintainers publish releases. Fallow currently has no public package release; the steps
below become operational after distribution names and trusted publishing are configured.

## Deploying a pilot

Deploy a pinned release tag, not `main`. `main` moves under active development and its state
between releases is not a supported target. For the school pilot, check out the `v0.3.0` tag
(the first pilot-ready release) and install from that commit, so every machine in the fleet
runs the same reviewed code.

## Version policy

- Use Semantic Versioning for package versions.
- Keep workspace package versions aligned unless a governance decision changes the policy.
- Update `fallow_protocol.version.__version__` and `PROTOCOL_VERSION` independently: the latter
  changes only when wire compatibility breaks.
- Move relevant entries from `Unreleased` into a dated changelog section.

## Release checklist

1. Confirm the intended release scope and compatibility impact.
2. Update versions, changelog, generated schemas and any migration notes in a release PR.
3. Run the complete contributing quality gate from a clean checkout on the release commit.
4. Build all packages and inspect wheel/sdist contents for credentials, local paths, model files
   and undeclared licences.
5. Merge the approved release PR, create a signed `vX.Y.Z` tag and push it.
6. Create a GitHub release from the changelog and attach hashes for all artifacts.
7. Publish with PyPI Trusted Publishing only after each distribution name and workflow
   environment has been approved by the project owner.
8. Verify installation into a fresh environment from the published index and run a protocol
   smoke test.

If verification fails after publishing, stop further publication, document impact publicly and
release a corrected version. Published artifacts and tags must not be silently replaced.
