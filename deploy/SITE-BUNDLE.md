# Fallow Site Mode desk install

Everything one Windows desk needs to join a LAN Site, except two things: a
llama.cpp build (§1) and the desk's own join file, which the coordinator
operator hands you separately.

You do not need a checkout of the Fallow repository on this machine.

```text
agentctl.exe            the agent, already built for windows/amd64
agent.example.toml      the config install.ps1 seeds from
bootstrap.ps1           reads the machine, then runs the installer for you
manifest.sha256         SHA-256 of every file above and below
windows\install.ps1     the installer
windows\doctor.ps1      post-install diagnosis
windows\uninstall.ps1   removal
windows\fetch-llama.ps1 stages the llama.cpp build (§1)
windows\JOIN-README.md  what a join file is and how it is consumed
windows\...             supporting scripts the three above load
```

Unzip the whole thing to one directory and keep it there. The scripts resolve
each other and the staged llama build by their position in this layout.

## 1. Stage llama.cpp

The agent serves inference through `llama-server.exe`, which is not in this
bundle: the CUDA build and its runtime DLLs are far larger than everything
else here put together.

With internet access on this machine:

```powershell
.\windows\fetch-llama.ps1
```

It downloads the pinned build, checks it against `windows\llama-manifest.psd1`,
and stages it under `bin\windows\` inside this directory.

Without internet access, stage it by hand: copy a matching `llama-server.exe`
and its DLLs into `bin\windows\` yourself, from the offline bundle
(`deploy/bundle.sh`) or from a machine that has already fetched it. The
installer only requires that `bin\windows\` contains `llama-server.exe`; it
fails before writing anything if it does not.

The shipped Windows build is CUDA-only. On a desk with no NVIDIA GPU the
installer caps `LLAMA_ARG_THREADS` so the CPU build stays polite, but you must
stage a CPU build for it to run at all.

## 2. Install

```powershell
.\bootstrap.ps1 -JoinBundle D:\join\desk-01.fallow-join -GoBinary .\agentctl.exe
```

The join file is a credential carrying a single-use enrollment token. Add
`-WhatIf` first for a walk of the whole install with no side effects.

`bootstrap.ps1` reports the machine (RAM, GPU) and warns before it installs
(too little RAM for a shared desk, or no NVIDIA GPU when the pinned llama.cpp
build is CUDA-only), then hands off to `windows\install.ps1` with the same two
arguments and runs a post-install self-test: the Scheduled Task is registered and
the config is in place. Running `.\windows\install.ps1` with those arguments
directly does the identical install without the machine report or the self-test.

`install.ps1` validates the join file before it writes anything, copies it to
`%USERPROFILE%\.fallow\site\join.json` with an owner-only ACL, renders a
token-free `%USERPROFILE%\.fallow\agent.toml` bound to `127.0.0.1`, installs
`agentctl.exe` into `%USERPROFILE%\.fallow\bin\`, and registers the at-logon
Scheduled Task `Fallow\FallowAgent`. It is idempotent: re-running never replaces
an existing identity or clobbers a live config. Detail in
`windows\JOIN-README.md`.

Installing from an admin or SYSTEM context instead of the pilot user's session
(Intune, ConfigMgr, PDQ, a GPO startup script) is `.\windows\install.ps1 -User
<account>` with the same two arguments; see `docs/pilot/remote-install.md` in the
repository.

**Remove the original join file from the USB stick or share once the desk has
enrolled.** The installed copy's token is consumed on first run; the original is
not.

## 3. Check the desk

```powershell
.\windows\doctor.ps1
```

One JSON object, exit non-zero if a required check fails. Run it before the desk
starts serving and again whenever it goes quiet.

To remove the agent again: `.\windows\uninstall.ps1`.

## Verifying this bundle

`manifest.sha256` lists the SHA-256 of every other file. On a machine with a
shell:

```bash
cd fallow-site-agent_<version>_windows_amd64 && shasum -a 256 -c manifest.sha256
```

The bundle is **not** code-signed. Windows SmartScreen and endpoint protection
have to be arranged with IT ahead of time. See `docs/pilot/it-checklist.md` in
the repository.
