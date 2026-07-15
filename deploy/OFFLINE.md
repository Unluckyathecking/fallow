# Offline bundle

The bundle installs Fallow without contacting a package index or download
server. It contains locked Python dependencies, workspace wheels, pinned
llama.cpp binaries for both supported agent platforms, example configuration,
and an optional `models` directory.

Run a preview first. It checks every hash and prints the target path without
creating or changing that path.

On an Apple Silicon Mac:

```bash
./install.sh install --dry-run --prefix "$HOME/.fallow/offline"
```

On Windows x64 in PowerShell:

```powershell
.\install.ps1 Install -DryRun -Prefix "$HOME\.fallow\offline"
```

Remove the preview flag to install. The installer verifies every file listed in
`manifest.sha256`, rejects unlisted files and unsafe paths, then creates a Python
3.12 virtual environment. It installs with `--no-index` and only uses the wheel
directories in this bundle. An existing `agent.toml` is left unchanged.

The shell installer uses `python3` and refuses any version other than 3.12. Set
`FALLOW_PYTHON` to an absolute Python 3.12 path when it is not the default.

The Windows llama.cpp directory includes the pinned CUDA build and matching
CUDA runtime DLLs. Keep those files together. Model weights are absent from the
CI artifact. A local builder can include them with:

```bash
deploy/bundle.sh build --output dist/fallow-offline-bundle \
  --with-models /path/to/verified-models
```

CI builds the bundle and tests the install preview. A real install and service
registration still need testing on each target machine.
