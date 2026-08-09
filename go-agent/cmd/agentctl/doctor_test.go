package main

import (
	"crypto/sha256"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
)

// coordinatorTime is the fixed instant the test coordinator serves in its Date
// header. Local time is injected relative to it, so every case is deterministic.
var coordinatorTime = time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)

// newPinnedCoordinator starts a TLS test server that serves coordinatorTime as
// its Date header and returns a client pinned to the server's real SPKI, so the
// check runs over the same pinned transport production uses.
func newPinnedCoordinator(t *testing.T) (*httptest.Server, *http.Client) {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Date", coordinatorTime.Format(http.TimeFormat))
		w.WriteHeader(http.StatusUnauthorized)
	}))
	t.Cleanup(srv.Close)
	return srv, pinnedClient(t, spkiPin(srv))
}

// spkiPin returns the canonical sha256/<base64> pin of the test server's leaf.
func spkiPin(srv *httptest.Server) string {
	sum := sha256.Sum256(srv.Certificate().RawSubjectPublicKeyInfo)
	return "sha256/" + base64.StdEncoding.EncodeToString(sum[:])
}

func pinnedClient(t *testing.T, pin string) *http.Client {
	t.Helper()
	client, err := siteclient.NewPinnedClient(siteclient.Profile{
		SiteID:                "test-site",
		CoordinatorSPKISHA256: []string{pin},
	})
	if err != nil {
		t.Fatalf("NewPinnedClient: %v", err)
	}
	return client
}

func fixedNow(offset time.Duration) func() time.Time {
	return func() time.Time { return coordinatorTime.Add(offset) }
}

func TestClockCheckOffset(t *testing.T) {
	cases := []struct {
		name   string
		offset time.Duration
		wantOK bool
		detail string
	}{
		{name: "in sync", offset: 0, wantOK: true, detail: "+0s"},
		{name: "small lead", offset: 3 * time.Second, wantOK: true, detail: "+3s"},
		{name: "small lag", offset: -3 * time.Second, wantOK: true, detail: "-3s"},
		{name: "at the limit", offset: 120 * time.Second, wantOK: true, detail: "+120s"},
		{name: "ahead over the limit", offset: 315 * time.Second, wantOK: false, detail: "+315s"},
		{name: "behind over the limit", offset: -315 * time.Second, wantOK: false, detail: "-315s"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, client := newPinnedCoordinator(t)
			got := clockCheck(client, srv.URL, fixedNow(tc.offset))
			if got.OK != tc.wantOK {
				t.Fatalf("ok = %v, want %v (detail %q)", got.OK, tc.wantOK, got.Detail)
			}
			if !strings.Contains(got.Detail, tc.detail) {
				t.Fatalf("detail = %q, want it to contain %q", got.Detail, tc.detail)
			}
			if !tc.wantOK && !strings.Contains(got.Detail, "120s limit") {
				t.Fatalf("detail = %q, want it to name the 120s limit", got.Detail)
			}
		})
	}
}

func TestClockCheckReportsUnreachableCoordinator(t *testing.T) {
	srv, client := newPinnedCoordinator(t)
	url := srv.URL
	srv.Close() // nothing listens on this origin any more

	got := clockCheck(client, url, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: an unreachable coordinator proves nothing about the clock (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "unreachable") {
		t.Fatalf("detail = %q, want it to report the coordinator as unreachable", got.Detail)
	}
	if strings.Contains(got.Detail, "offset") {
		t.Fatalf("detail = %q, want no guess at an offset", got.Detail)
	}
}

func TestClockCheckReportsPinFailure(t *testing.T) {
	srv, _ := newPinnedCoordinator(t)
	wrong := pinnedClient(t, "sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

	got := clockCheck(wrong, srv.URL, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: a pin failure is pinned_tls's finding, not the clock's (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "pinned TLS") {
		t.Fatalf("detail = %q, want it to name the pin failure distinctly", got.Detail)
	}
	if strings.Contains(got.Detail, "unreachable") || strings.Contains(got.Detail, "offset") {
		t.Fatalf("detail = %q, want neither an unreachable claim nor an offset guess", got.Detail)
	}
}

func TestClockCheckReportsMissingDateHeader(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header()["Date"] = nil // suppress net/http's automatic Date
		w.WriteHeader(http.StatusUnauthorized)
	}))
	t.Cleanup(srv.Close)

	got := clockCheck(pinnedClient(t, spkiPin(srv)), srv.URL, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: a missing Date header proves nothing about the clock (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "Date header") {
		t.Fatalf("detail = %q, want it to report the missing Date header", got.Detail)
	}
}

func TestDoctorClockSkipsDirectAgents(t *testing.T) {
	got := doctorClock(config.Settings{}, fixedNow(0))
	if !got.OK || got.Detail != "not site mode" {
		t.Fatalf("doctorClock = %+v, want a no-op check for a direct agent", got)
	}
}

func TestDoctorClockReportsUnusableProfile(t *testing.T) {
	settings := config.Settings{
		StatePath:      t.TempDir() + "/no-state.json",
		SiteJoinBundle: t.TempDir() + "/no-join.json",
	}
	got := doctorClock(settings, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: an unreadable profile is config's finding (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "skew unknown") {
		t.Fatalf("detail = %q, want it to say the skew is unknown", got.Detail)
	}
}
