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

// TestSaveThenLoadSiteProfile round-trips the token-free Site profile and the
// persisted sequence high-water beside the credential fields.
func TestSaveThenLoadSiteProfile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	mdns := "_fallow._tcp.local."
	want := Identity{
		AgentID:     "agent-1",
		DeviceToken: "tok-abc",
		Site: &SiteProfile{
			SiteID:                "clfs-pilot",
			CoordinatorURLs:       []string{"https://10.24.8.10:8330"},
			CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
			MDNSService:           &mdns,
		},
		Seq: 48,
	}
	if err := Save(path, want); err != nil {
		t.Fatalf("save: %v", err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got == nil || got.Site == nil {
		t.Fatal("site profile did not round-trip")
	}
	if got.Site.SiteID != "clfs-pilot" || len(got.Site.CoordinatorURLs) != 1 ||
		got.Site.CoordinatorURLs[0] != "https://10.24.8.10:8330" {
		t.Errorf("site profile = %+v", got.Site)
	}
	if got.Seq != 48 {
		t.Errorf("seq = %d, want 48", got.Seq)
	}
	if got.Site.MDNSService == nil || *got.Site.MDNSService != mdns {
		t.Errorf("mdns_service did not round-trip: %+v", got.Site.MDNSService)
	}
}

// TestLegacyIdentityStillLoads pins backward compatibility: a two-field identity
// written before Site Mode existed loads with no Site profile and a zero seq.
func TestLegacyIdentityStillLoads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	body := `{"agent_id":"a","device_token":"t"}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatalf("legacy identity failed to load: %v", err)
	}
	if got == nil || got.AgentID != "a" || got.DeviceToken != "t" {
		t.Fatalf("legacy identity = %+v", got)
	}
	if got.Site != nil {
		t.Errorf("legacy identity gained a site profile: %+v", got.Site)
	}
	if got.Seq != 0 {
		t.Errorf("legacy identity seq = %d, want 0", got.Seq)
	}
}
