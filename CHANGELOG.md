# Changelog

All notable changes to Fallow will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow Semantic
Versioning once public packages are published.

## [Unreleased]

### Added

- Protocol, coordinator, agent, CLI and benchmark workspace foundations.
- Cross-platform idle detection, preemption and process supervision modules.
- Registry, queue, model-serving, scheduling and OpenAI-compatible gateway modules.
- Community health files, compatibility policy, stability policy and release process.

### Security

- Documented the trusted-network assumption and unsupported production security boundaries.

### Fixed

- Avoid signalling an already-exited supervised child, including the Windows process-handle
  behaviour where a reaped process can otherwise surface as access denied.

No public release has been published yet.
