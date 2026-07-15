# ADR 035: Verifiable offline install bundle

- **Status:** Accepted
- **Date:** 2026-07-15

## Context

Fleet machines may have no route to PyPI, GitHub, or a model store during
installation. The existing service installers assume a checked-out repository
and use `uv sync`, so they cannot establish a zero-egress installation by
themselves. Windows also needs two matching llama.cpp archives: the CUDA build
and its CUDA runtime DLLs.

## Decision

`deploy/bundle.sh build` creates one directory for both supported agent
platforms. `uv.lock` remains the dependency source of truth. The builder runs
`uv export --frozen`, builds every workspace wheel, downloads Python 3.12 wheels
for macOS arm64 and Windows x64, and stages the pinned llama.cpp release from the
same release and CUDA values used by the platform fetch scripts.

CI builds the directory without model weights. Local operators can pass
`--with-models DIR` to copy verified weights into the same artifact. The bundle
also carries example configuration and platform installers.

The builder writes a sorted SHA-256 manifest after staging is complete. Both
installers reject malformed or unsafe paths, missing or unlisted files,
duplicates, and hash mismatches. They finish all verification before changing
the target directory. Their preview mode performs the same verification and
does not write to the target.

## Consequences

The artifact is large because it contains wheels and llama.cpp binaries for two
platforms. CI omits model weights to keep the artifact manageable and avoid
redistributing separately licensed files. A machine still needs Python 3.12,
but package installation makes no network requests.

The manifest proves internal consistency, not publisher identity. Operators
must obtain the bundle through a trusted channel. A real install and service
registration remain target-machine checks because local CI cannot exercise the
GUI session and GPU runtime requirements.
