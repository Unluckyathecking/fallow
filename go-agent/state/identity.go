// Package state persists the agent's durable identity: its agent_id and bearer
// device_token, learned once at first-run enrollment and loaded on every later
// start so a machine enrolls exactly once.
//
// The token is a bearer secret, so the state file is written atomically (a temp
// file in the same directory, then rename) and, on Unix, created with mode 0600
// (owner read/write only) so a crash mid-write never leaves a half-written or
// world-readable credential. This mirrors fallow_agent.main.identity.
package state

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const (
	// stateFileMode is the owner-only permission for the credential file.
	stateFileMode os.FileMode = 0o600
	tmpSuffix                 = ".tmp"
)

// Identity is the durable identity of one enrolled agent.
type Identity struct {
	AgentID     string `json:"agent_id"`
	DeviceToken string `json:"device_token"`
}

func (i Identity) valid() bool { return i.AgentID != "" && i.DeviceToken != "" }

// Load returns the persisted identity, or (nil, nil) if this machine is
// unenrolled (the file does not exist). It returns an error if the file exists
// but is unreadable or malformed: a corrupt credential must fail loudly, not
// silently trigger re-enrollment.
func Load(path string) (*Identity, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("could not read identity file %s: %w", path, err)
	}
	var id Identity
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&id); err != nil {
		return nil, fmt.Errorf("malformed identity file %s: %w", path, err)
	}
	if !id.valid() {
		return nil, fmt.Errorf("malformed identity file %s: missing agent_id or device_token", path)
	}
	return &id, nil
}

// Save persists id atomically. On Unix the file is created with mode 0600; the
// temp file is written in the same directory and renamed over the target so the
// swap is atomic on POSIX filesystems.
func Save(path string, id Identity) error {
	if !id.valid() {
		return errors.New("refusing to persist identity with empty agent_id or device_token")
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("could not create identity dir %s: %w", dir, err)
	}
	payload, err := json.Marshal(id)
	if err != nil {
		return fmt.Errorf("could not marshal identity: %w", err)
	}
	tmp := path + tmpSuffix
	if err := writePrivate(tmp, payload); err != nil {
		return fmt.Errorf("could not write identity file %s: %w", path, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("could not finalize identity file %s: %w", path, err)
	}
	return nil
}

// writePrivate creates path with owner-only permissions and writes payload,
// cleaning up on any failure.
func writePrivate(path string, payload []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, stateFileMode)
	if err != nil {
		return err
	}
	if _, err := f.Write(payload); err != nil {
		_ = f.Close()
		_ = os.Remove(path)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(path)
		return err
	}
	return nil
}
