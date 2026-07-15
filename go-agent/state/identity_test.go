package state

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestLoadReturnsNilWhenUnenrolled(t *testing.T) {
	id, err := Load(filepath.Join(t.TempDir(), "identity.json"))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if id != nil {
		t.Errorf("expected nil identity, got %+v", id)
	}
}

func TestSaveThenLoadRoundTrips(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "identity.json")
	want := Identity{AgentID: "agent-1", DeviceToken: "tok-abc"}

	if err := Save(path, want); err != nil {
		t.Fatalf("save: %v", err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got == nil || *got != want {
		t.Errorf("round-trip = %+v, want %+v", got, want)
	}
}

func TestSaveUsesOwnerOnlyModeOnUnix(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX file mode not meaningful on Windows")
	}
	path := filepath.Join(t.TempDir(), "identity.json")
	if err := Save(path, Identity{AgentID: "a", DeviceToken: "t"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("mode = %o, want 600", perm)
	}
}

func TestSaveIsAtomicAndLeavesNoTemp(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "identity.json")
	if err := Save(path, Identity{AgentID: "a", DeviceToken: "t"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if filepath.Ext(e.Name()) == tmpSuffix {
			t.Errorf("temp file left behind: %s", e.Name())
		}
	}
}

func TestLoadRejectsMalformed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Error("expected error for malformed identity file")
	}
}

func TestLoadRejectsUnknownFields(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	body := `{"agent_id":"a","device_token":"t","extra":"x"}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Error("expected error for unknown field (schema drift must fail loudly)")
	}
}

func TestSaveRejectsEmptyIdentity(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	if err := Save(path, Identity{}); err == nil {
		t.Error("expected error saving empty identity")
	}
}
