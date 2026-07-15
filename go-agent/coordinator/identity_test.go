package coordinator

import (
	"context"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

func TestIdentityRoundTripUsesOwnerOnlyMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "state.json")
	want := IdentityState{AgentID: testAgentID, DeviceToken: testToken}
	if err := SaveIdentity(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := LoadIdentity(path)
	if err != nil {
		t.Fatal(err)
	}
	if got == nil || *got != want {
		t.Fatalf("identity = %#v", got)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("mode = %o", info.Mode().Perm())
		}
	}
}

func TestSaveIdentityAtomicallyReplacesExistingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	if err := SaveIdentity(path, IdentityState{AgentID: "old", DeviceToken: "old-token"}); err != nil {
		t.Fatal(err)
	}
	want := IdentityState{AgentID: "new", DeviceToken: "new-token"}
	if err := SaveIdentity(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := LoadIdentity(path)
	if err != nil || got == nil || *got != want {
		t.Fatalf("identity = %#v, err = %v", got, err)
	}
}

func TestLoadIdentityDistinguishesMissingAndMalformed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	got, err := LoadIdentity(path)
	if err != nil || got != nil {
		t.Fatalf("missing identity = %#v, %v", got, err)
	}
	if err := os.WriteFile(path, []byte("not-json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadIdentity(path); err == nil {
		t.Fatal("malformed identity did not fail")
	}
}

func TestResolveIdentityEnrollsOnceThenLoads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	calls := 0
	doer := roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		return response(http.StatusOK, protocol.RegisterResponse{
			AgentID: testAgentID, DeviceToken: testToken,
			Config: protocol.AgentConfig{HeartbeatIntervalS: 7},
		}), nil
	})
	firstClient := NewClient(testBaseURL, doer, "", "")
	identity, config, err := ResolveIdentity(
		context.Background(), path, "enroll", 1, protocol.DeviceCaps{}, firstClient,
	)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || identity.AgentID != testAgentID || config.HeartbeatIntervalS != 7 {
		t.Fatalf("calls=%d identity=%#v config=%#v", calls, identity, config)
	}

	secondClient := NewClient(testBaseURL, roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("loaded identity attempted registration")
		return &http.Response{Body: io.NopCloser(strings.NewReader(""))}, nil
	}), "", "")
	identity, config, err = ResolveIdentity(
		context.Background(), path, "", 1, protocol.DeviceCaps{}, secondClient,
	)
	if err != nil {
		t.Fatal(err)
	}
	if identity.AgentID != testAgentID || config.HeartbeatIntervalS != 5 {
		t.Fatalf("identity=%#v config=%#v", identity, config)
	}
	if secondClient.AgentID() != testAgentID || secondClient.DeviceToken() != testToken {
		t.Fatal("loaded identity was not installed on client")
	}
}

func TestResolveIdentityRequiresEnrollmentToken(t *testing.T) {
	client := NewClient(testBaseURL, roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("request should not be sent")
		return nil, nil
	}), "", "")
	_, _, err := ResolveIdentity(
		context.Background(), filepath.Join(t.TempDir(), "state.json"), "", 1,
		protocol.DeviceCaps{}, client,
	)
	if err == nil {
		t.Fatal("missing enrollment token did not fail")
	}
}
