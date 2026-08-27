package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/Unluckyathecking/fallow/go-agent/state"
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

// TestClockCheckNamesTheClockOnAValidityWindowFailure covers the machine this
// check exists for: a clock so far out that every certificate reads as outside
// its validity window. The handshake fails before any Date header is served, so
// the offset cannot be measured - but the report must still send the operator
// to the clock rather than to the certificate.
func TestClockCheckNamesTheClockOnAValidityWindowFailure(t *testing.T) {
	srv := expiredCoordinator(t)

	got := clockCheck(pinnedClient(t, spkiPin(srv)), srv.URL, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: an expired certificate under a correct clock is locally indistinguishable (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "validity window") {
		t.Fatalf("detail = %q, want it to report the validity-window failure", got.Detail)
	}
	if !strings.Contains(got.Detail, "clock") || !strings.Contains(got.Detail, "NTP") {
		t.Fatalf("detail = %q, want it to name this PC's clock as a suspect and the fix", got.Detail)
	}
	if strings.Contains(got.Detail, "pinned TLS failed") {
		t.Fatalf("detail = %q, want the clock-suspect wording, not the generic pin message", got.Detail)
	}
}

// expiredCoordinator serves TLS with a self-signed certificate whose validity
// window closed a year ago, which is what a months-off local clock makes every
// certificate look like.
func expiredCoordinator(t *testing.T) *httptest.Server {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "expired-coordinator"},
		NotBefore:    time.Now().Add(-2 * 365 * 24 * time.Hour),
		NotAfter:     time.Now().Add(-365 * 24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
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

// TestDoctorClockReportsProfileWithNoCoordinatorURL covers the fail-closed
// branch: a persisted Site profile that carries pins but no origin to read a
// Date header from.
func TestDoctorClockReportsProfileWithNoCoordinatorURL(t *testing.T) {
	dir := t.TempDir()
	statePath := filepath.Join(dir, "state.json")
	if err := state.Save(statePath, state.Identity{
		AgentID:     "a1",
		DeviceToken: "t1",
		Site: &state.SiteProfile{
			SiteID:                "s1",
			CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
		},
	}); err != nil {
		t.Fatalf("state.Save: %v", err)
	}

	got := doctorClock(config.Settings{
		StatePath:      statePath,
		SiteJoinBundle: filepath.Join(dir, "join.json"),
	}, fixedNow(0))
	if !got.OK {
		t.Fatalf("ok = false, want true: a profile with no origin is pinned_tls's finding (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "no coordinator URL") {
		t.Fatalf("detail = %q, want it to report the missing coordinator URL", got.Detail)
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

// The idle lane answers the question `run` asks before it enrolls, so a desk
// hears about a build that cannot see its user before it is asked to serve.
// assume_idle passes the lane and says what it costs.
func TestDoctorIdleReportsWhatTheDaemonWouldRefuseOn(t *testing.T) {
	got := doctorIdle(config.Settings{})
	if err := sampleIdleDetector(); err != nil {
		if got.OK {
			t.Fatalf("ok = true on a platform with no idle detection (%q)", got.Detail)
		}
		if !strings.Contains(got.Detail, err.Error()) {
			t.Fatalf("detail = %q, want the detector's own reason", got.Detail)
		}
	} else if !got.OK {
		t.Fatalf("ok = false with a working detector (%q)", got.Detail)
	}

	assuming := doctorIdle(config.Settings{AssumeIdle: true})
	if !assuming.OK || !strings.Contains(assuming.Detail, "assume_idle") {
		t.Fatalf("assume_idle lane = %+v, want ok with the override named", assuming)
	}
}

// The identity lane is what an operator reads on a desk that stopped serving
// after the coordinator revoked it. Doctor makes no authenticated call, so the
// evidence is the marker the daemon wrote; the lane must fail and say so.
func TestDoctorIdentityReportsARevokedDeviceToken(t *testing.T) {
	dir := t.TempDir()
	statePath := filepath.Join(dir, "state.json")
	if err := state.Save(statePath, state.Identity{AgentID: "a1", DeviceToken: "t1"}); err != nil {
		t.Fatalf("state.Save: %v", err)
	}
	settings := config.Settings{StatePath: statePath}

	if got := doctorIdentity(settings); !got.OK || !strings.Contains(got.Detail, "a1") {
		t.Fatalf("before revocation the lane = %+v, want the enrolled identity", got)
	}
	if err := state.MarkRevoked(statePath, "coordinator rejected credentials (401)"); err != nil {
		t.Fatalf("MarkRevoked: %v", err)
	}

	got := doctorIdentity(settings)
	if got.OK {
		t.Fatalf("ok = true for a revoked identity (%q)", got.Detail)
	}
	if !strings.Contains(got.Detail, "device token rejected by the coordinator") {
		t.Fatalf("detail = %q, want it to name the rejection distinctly", got.Detail)
	}
	if !strings.Contains(got.Detail, "re-enrol") {
		t.Fatalf("detail = %q, want it to name the only way back", got.Detail)
	}
}
