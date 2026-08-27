package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRevokedRoundTrip(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "agent-state.json")

	if reason, revoked := Revoked(statePath); revoked {
		t.Fatalf("a fresh state dir reports revoked (%q)", reason)
	}
	if err := MarkRevoked(statePath, " coordinator rejected credentials (401)\n"); err != nil {
		t.Fatalf("MarkRevoked: %v", err)
	}
	reason, revoked := Revoked(statePath)
	if !revoked {
		t.Fatal("revoked = false after MarkRevoked")
	}
	if reason != "coordinator rejected credentials (401)" {
		t.Fatalf("reason = %q, want the trimmed recorded reason", reason)
	}
	if RevokedPath(statePath) != filepath.Join(filepath.Dir(statePath), RevokedFilename) {
		t.Fatalf("RevokedPath = %q, want it beside the state file", RevokedPath(statePath))
	}
}

func TestClearRevokedRemovesTheMarkerAndToleratesItsAbsence(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "agent-state.json")

	if err := ClearRevoked(statePath); err != nil {
		t.Fatalf("ClearRevoked with no marker: %v", err)
	}
	if err := MarkRevoked(statePath, "coordinator rejected credentials (401)"); err != nil {
		t.Fatalf("MarkRevoked: %v", err)
	}
	if err := ClearRevoked(statePath); err != nil {
		t.Fatalf("ClearRevoked: %v", err)
	}
	if reason, revoked := Revoked(statePath); revoked {
		t.Fatalf("still revoked after ClearRevoked (%q)", reason)
	}
}

// An empty marker still means revoked: the file's presence is the fact.
func TestRevokedWithNoReasonStillReportsRevoked(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "agent-state.json")
	if err := os.WriteFile(RevokedPath(statePath), nil, 0o644); err != nil {
		t.Fatalf("write marker: %v", err)
	}
	reason, revoked := Revoked(statePath)
	if !revoked || reason != defaultRevokedReason {
		t.Fatalf("Revoked = (%q, %v), want the default reason and true", reason, revoked)
	}
}
