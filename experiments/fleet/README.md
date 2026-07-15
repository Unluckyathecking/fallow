# Fleet host scaffold

This directory prepares a Linux agent host without tying the experiment to a
cloud provider. It renders files into a local bundle, checks that bundle, and
can install it on a host where an operator already has authenticated access.
None of the scripts provisions a machine or calls a cloud API.

The cloud-init template installs packages from the Ubuntu archive. It does not
clone Fallow, install Tailscale, fetch a model, or carry credentials. Build the
machine image with Tailscale and `llama-server` already present, or install them
through your usual image pipeline.

## Trust boundary

Treat cloud-init and rendered bundles as public. They may be copied into build
logs or retained by a provider. The renderer therefore accepts only these
non-secret values:

* the coordinator tailnet URL
* the agent tailnet bind address
* the checkout and `llama-server` paths
* the local service account

Join the host to the tailnet over an authenticated operator channel after boot.
Pass `TS_AUTHKEY` directly to your approved Tailscale enrollment command on the
host. Do not put that key in cloud-init, shell history, a rendered bundle, or a
committed file.

Mint a one-time Fallow enrollment token after tailnet admission. `setup.sh`
reads `FALLOW_ENROLLMENT_TOKEN` from its environment and writes it to
`/etc/fallow/agent.env` with mode `0600`. The systemd unit reads that file. The
agent consumes the token during its first registration and persists its device
identity under `/var/lib/fallow`. Remove the enrollment line after registration
if the installed agent version does not clear it itself.

## Render and inspect

Run these commands from the repository root:

```bash
experiments/fleet/scripts/render.sh \
  --output /tmp/fallow-fleet \
  --coordinator-url http://coordinator.example.ts.net:8080 \
  --bind-host 100.64.0.20 \
  --repo /opt/fallow \
  --llama-binary /usr/local/bin/llama-server \
  --run-user fallow

experiments/fleet/scripts/validate.sh /tmp/fallow-fleet
experiments/fleet/scripts/dry-run.sh
```

`dry-run.sh` renders into a temporary directory and validates the result. It
sets a sealed `PATH` containing local command stubs, so a regression that tries
to use a network client fails the check.

## Install on an admitted host

Copy the validated bundle and the matching Fallow checkout through the same
authenticated host channel. On the host, inspect both, then run:

```bash
sudo env FALLOW_ENROLLMENT_TOKEN="$(read-token-from-secure-source)" \
  /path/to/bundle/setup.sh /path/to/bundle
```

The setup script installs the config and unit locally, then enables the service.
It does not fetch dependencies or contact the coordinator itself. To stage the
filesystem without invoking systemd, set `FALLOW_ROOT` to an empty directory.

Stop the service with `scripts/stop.sh`. Remove the unit and config with
`scripts/cleanup.sh`. Cleanup keeps `/var/lib/fallow` by default; pass `--purge`
only when the host is being discarded.

## Files

* `templates/cloud-init.yaml.tmpl` prepares the local account and directories.
* `templates/agent.toml.tmpl` contains public agent settings.
* `templates/fallow-agent.service.tmpl` runs the agent under systemd.
* `scripts/render.sh` creates a bundle from public values.
* `scripts/validate.sh` rejects placeholders, credentials, and unsafe settings.
* `scripts/dry-run.sh` exercises rendering and validation without a network.
* `scripts/setup.sh`, `stop.sh`, and `cleanup.sh` manage one admitted host.
* `tests/test_fleet_scaffold.sh` checks syntax, rendering, secret handling, and
  the sealed dry run.
