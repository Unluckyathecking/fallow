# state

Persists the agent's durable identity — its `agent_id` and bearer
`device_token` — so a machine enrolls exactly once. Ports
`fallow_agent.main.identity`.

## Contract

- `Load(path)` returns `(nil, nil)` when the file does not exist (the machine is
  unenrolled). It returns an **error** if the file exists but is unreadable or
  malformed: a corrupt credential must fail loudly, not silently trigger
  re-enrollment. Unknown JSON fields are rejected (schema drift fails loudly).
- `Save(path, id)` writes the identity **atomically**: it writes a temp file in
  the same directory, then `rename`s it over the target. A crash mid-write can
  never leave a half-written credential.

## Permissions

The device token is a bearer secret. On Unix the file is created with mode
`0600` (owner read/write only) via `O_CREATE` with an explicit `FileMode`, so it
is never world-readable. On Windows the POSIX mode is not meaningful; the
atomic-rename durability guarantee still holds. The mode assertion in
`identity_test.go` is guarded with `runtime.GOOS == "windows"` accordingly (CI
is Linux, dev is macOS — both exercise the 0600 path).
