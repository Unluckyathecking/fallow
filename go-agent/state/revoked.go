package state

// Revocation marker: the durable record that the coordinator rejected this
// machine's device token (ADR 104). Revocation is terminal for an identity —
// there is no un-revoke route — so the daemon records it beside the identity
// file and stays down instead of restarting into the same 401 every minute.
//
// The file is a plain one-line reason, not a credential, so it is written with
// ordinary permissions. Its presence is the fact; its contents are what the
// daemon log and `agentctl doctor` print back to the operator.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// RevokedFilename is the marker whose presence means "this identity is dead".
// It lives beside the agent state file, like the reclaim flag.
const RevokedFilename = "revoked.flag"

// defaultRevokedReason stands in for a marker that exists but says nothing.
const defaultRevokedReason = "the coordinator rejected this device token"

// RevokedPath returns the revocation marker path for a given state file.
func RevokedPath(statePath string) string {
	return filepath.Join(filepath.Dir(statePath), RevokedFilename)
}

// MarkRevoked records that the coordinator rejected this identity.
func MarkRevoked(statePath, reason string) error {
	path := RevokedPath(statePath)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("could not create state dir for %s: %w", path, err)
	}
	if err := os.WriteFile(path, []byte(strings.TrimSpace(reason)+"\n"), 0o644); err != nil {
		return fmt.Errorf("could not write revocation marker %s: %w", path, err)
	}
	return nil
}

// ClearRevoked removes the marker, if there is one. A fresh enrolment is the one
// event that makes it stale: the identity it condemned no longer exists, and the
// operator has already done the work the marker told them to do. Absent is not
// an error.
func ClearRevoked(statePath string) error {
	path := RevokedPath(statePath)
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("could not remove revocation marker %s: %w", path, err)
	}
	return nil
}

// Revoked reports whether this machine's identity was revoked, and the reason
// recorded when it was. A marker that exists but cannot be read still counts as
// revoked: the fact is its presence, and failing open would resume serving on a
// dead identity.
func Revoked(statePath string) (string, bool) {
	path := RevokedPath(statePath)
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return "", false
	}
	if err != nil {
		return fmt.Sprintf("%s (marker %s is unreadable: %v)", defaultRevokedReason, path, err), true
	}
	if reason := strings.TrimSpace(string(data)); reason != "" {
		return reason, true
	}
	return defaultRevokedReason, true
}
