# Open-source launch checklist

Repository content now includes the documentation and automation needed for external
contribution. The project owner must still make and verify the following decisions before
changing repository visibility or announcing a release.

## Legal and identity

- [ ] Confirm the copyright holder(s) and that every committed contribution may be released
  under AGPL-3.0-or-later.
- [ ] Confirm that "Fallow" and its visual identity do not conflict with third-party marks;
  publish a trademark policy if the name becomes a project mark.
- [ ] Review dependency and model licences. AGPL-3.0-or-later covers Fallow source, not model weights,
  datasets, inference engines or downstream deployments.
- [ ] Choose a private conduct/security contact that can reliably receive reports.

## Repository settings

- [ ] Review commit history and run a dedicated full-history secret scanner immediately before
  publication; rotate anything questionable even if it has been removed.
- [ ] Enable private vulnerability reporting and GitHub Discussions.
- [ ] Create the labels referenced by issue forms and Dependabot.
- [ ] Protect the default branch: require pull requests, CI, conversation resolution and no
  force pushes; decide whether DCO sign-off is mandatory and automate it if so.
- [ ] Enable dependency graph, Dependabot alerts and secret scanning.
- [ ] Enable CodeQL and dependency review after confirming GitHub Advanced Security eligibility
  for the repository's visibility and ownership.
- [ ] Decide whether the active implementation branch should become the default branch. Do not
  publish an empty default branch while development lives elsewhere.

## Releases and adoption

- [ ] Resolve ownership of the five proposed distribution names on PyPI.
- [ ] Configure PyPI Trusted Publishing with protected GitHub environments; do not use long-lived
  API tokens.
- [ ] Complete the coordinator and agent entrypoints and an end-to-end quickstart before claiming
  the system is deployable.
- [ ] Publish reproducible compatibility results for inference-engine, GPU and OS combinations.
- [ ] Complete a threat model and external security review before recommending production use.
- [ ] Archive release artifacts, checksums and a software bill of materials.
