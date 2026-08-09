package runtime

import (
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/inference"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// ── site fakes ───────────────────────────────────────────────────────────────

// fakeReconciler records every desired set applied, including empty sets.
type fakeReconciler struct {
	mu    sync.Mutex
	calls [][]string
}

func (r *fakeReconciler) Apply(_ context.Context, desired []string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, append([]string(nil), desired...))
	return nil
}

func (r *fakeReconciler) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

func (r *fakeReconciler) appliedEmpty() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, c := range r.calls {
		if len(c) == 0 {
			return true
		}
	}
	return false
}

// countingClaimCoord counts Claim calls and blocks each one until its context is
// cancelled, so the runner never busy-spins and a presence change ends the wait.
type countingClaimCoord struct {
	mu     sync.Mutex
	claims int
}

func (c *countingClaimCoord) Claim(ctx context.Context, _ time.Duration) (*inference.Claim, error) {
	c.mu.Lock()
	c.claims++
	c.mu.Unlock()
	<-ctx.Done()
	return nil, ctx.Err()
}
func (c *countingClaimCoord) Upload(context.Context, inference.Claim, int, string, io.Reader) error {
	return nil
}
func (c *countingClaimCoord) Fail(context.Context, inference.Claim, inference.FailureCode, bool) error {
	return nil
}
func (c *countingClaimCoord) claimCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.claims
}

// validJoinFile writes a well-formed relay-v1 join bundle and returns its path.
func validJoinFile(t *testing.T, dir string) string {
	t.Helper()
	pin := "sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))
	body := `{"version":1,"site_id":"clfs-pilot",` +
		`"coordinator_urls":["https://10.24.8.10:8330"],` +
		`"coordinator_spki_sha256":["` + pin + `"],` +
		`"enrollment_token":"one-use-secret","mdns_service":null}`
	path := filepath.Join(dir, "join.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func siteSettings(t *testing.T) config.Settings {
	t.Helper()
	s := testSettings(t)
	s.CoordinatorURL = ""
	s.SiteJoinBundle = validJoinFile(t, filepath.Dir(s.StatePath))
	return s
}

func siteSeamsFor(fc *fakeCoordinator, fs *fakeSupervisor, det idle.Detector, tf *tickerFactory, rec modelReconciler, claim inference.Coordinator) Seams {
	s := seamsFor(fc, fs, det, tf)
	s.NewPinnedClient = func(siteclient.Profile) (*http.Client, error) { return &http.Client{}, nil }
	s.NewSiteCoordinator = func(_, agentID, deviceToken string, _ *http.Client) Coordinator {
		fc.seed(agentID, deviceToken)
		return fc
	}
	s.Reconciler = rec
	s.ClaimCoordinator = claim
	return s
}

// ── tests ────────────────────────────────────────────────────────────────────

// TestSiteEnrollmentPersistsProfileAndRemovesToken drives first-run Site Mode
// enrollment: register once, persist the token-free profile, then remove the
// installed join token from disk.
func TestSiteEnrollmentPersistsProfileAndRemovesToken(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if fc.registers != 1 {
		t.Fatalf("register called %d times, want 1", fc.registers)
	}
	id, err := state.Load(settings.StatePath)
	if err != nil || id == nil {
		t.Fatalf("identity not persisted: %v", err)
	}
	if id.Site == nil || id.Site.SiteID != "clfs-pilot" {
		t.Fatalf("token-free profile not persisted: %+v", id.Site)
	}
	if id.AgentID != "agent-site" {
		t.Errorf("persisted agent_id = %q", id.AgentID)
	}
	if _, err := os.Stat(settings.SiteJoinBundle); !os.IsNotExist(err) {
		t.Errorf("join token file was not removed after enrollment (err=%v)", err)
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestSiteAmbiguousRegistrationFailsClosed refuses to proceed when the
// coordinator returns no identity, and never persists or removes the token.
func TestSiteAmbiguousRegistrationFailsClosed(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{Config: testConfig()}} // empty ids
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))

	if err := rt.Run(context.Background()); err == nil {
		t.Fatal("expected an error on ambiguous registration")
	}
	if _, err := state.Load(settings.StatePath); err == nil {
		if id, _ := state.Load(settings.StatePath); id != nil {
			t.Error("identity was persisted despite ambiguous registration")
		}
	}
	if _, err := os.Stat(settings.SiteJoinBundle); err != nil {
		t.Error("join token file was removed despite failed enrollment")
	}
}

// TestSiteRegistrationErrorIsNotRetried surfaces a register error as terminal.
func TestSiteRegistrationErrorIsNotRetried(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerErr: errTest}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))

	if err := rt.Run(context.Background()); err == nil {
		t.Fatal("expected the register error to be terminal")
	}
	if fc.registers != 1 {
		t.Errorf("register attempted %d times, want exactly 1 (never retried)", fc.registers)
	}
}

// TestSiteReconcilesDesiredModelsIncludingEmpty proves every heartbeat response
// drives reconciliation, including an empty desired set.
func TestSiteReconcilesDesiredModelsIncludingEmpty(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{
		registerResp: protocol.RegisterResponse{AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig()},
		hbResp:       protocol.HeartbeatResponse{DesiredModels: nil}, // empty set
	}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rec := &fakeReconciler{}
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), rec, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "reconcile applied", func() bool { return rec.count() >= 1 })
	if !rec.appliedEmpty() {
		t.Error("an empty desired set was not reconciled")
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestSiteClaimRunnerStartsWhenEligible proves the claim runner begins claiming
// only once the agent is idle with a READY loopback replica.
func TestSiteClaimRunnerStartsWhenEligible(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "chat", Port: 8100, State: protocol.ReplicaStateReady},
	}}
	det, _ := idle.NewFakeDetector(200)
	claim := &countingClaimCoord{}
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, &fakeReconciler{}, claim))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	// A poll tick publishes the READY replica into the availability view, which
	// wakes the runner to claim.
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire()
	waitFor(t, "claim runner started", func() bool { return claim.claimCount() >= 1 })

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
	if !fs.contains("stop_all") {
		t.Error("supervisor.StopAll not called on shutdown (runner must stop before replicas)")
	}
}

// TestSiteRestartResumesWithoutReenrollingAndAboveHighWater proves a restart
// resumes from the persisted profile without re-registering and continues the
// sequence above the persisted high-water mark, never regressing the fence.
func TestSiteRestartResumesWithoutReenrollingAndAboveHighWater(t *testing.T) {
	settings := siteSettings(t)

	// Seed a persisted Site identity with a high-water sequence, as a prior
	// process would have left behind. The join file is gone after first run.
	mdns := "_fallow._tcp.local."
	seeded := state.Identity{
		AgentID: "agent-site", DeviceToken: "dev-tok",
		Site: &state.SiteProfile{
			SiteID:                "clfs-pilot",
			CoordinatorURLs:       []string{"https://10.24.8.10:8330"},
			CoordinatorSPKISHA256: []string{"sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))},
			MDNSService:           &mdns,
		},
		Seq: 500,
	}
	if err := state.Save(settings.StatePath, seeded); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(settings.SiteJoinBundle) // token consumed on first run

	fc := &fakeCoordinator{}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if fc.registers != 0 {
		t.Errorf("re-registered on restart %d times, want 0", fc.registers)
	}
	if got := fc.heartbeats[0].Seq; int64(got) < seeded.Seq {
		t.Errorf("first heartbeat seq = %d, below the persisted high-water %d", got, seeded.Seq)
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestSiteRefusesToConvertLegacyIdentity fails closed rather than silently
// converting an existing non-Site identity to Site Mode.
func TestSiteRefusesToConvertLegacyIdentity(t *testing.T) {
	settings := siteSettings(t)
	if err := state.Save(settings.StatePath, state.Identity{AgentID: "old", DeviceToken: "tok"}); err != nil {
		t.Fatal(err)
	}
	fc := &fakeCoordinator{}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))
	if err := rt.Run(context.Background()); err == nil {
		t.Fatal("expected a fail-closed error converting a legacy identity to Site Mode")
	}
	if fc.registers != 0 {
		t.Errorf("register attempted %d times, want 0", fc.registers)
	}
}

var errTest = errTestType("boom")

type errTestType string

func (e errTestType) Error() string { return string(e) }

// TestDirectModeIgnoresSiteSeams pins the disabled-parity guarantee: with no
// join bundle and no persisted profile, the daemon runs the legacy path and
// never touches the Site Mode seams (no pinned client, no site coordinator, no
// reconciler, no claim runner).
func TestDirectModeIgnoresSiteSeams(t *testing.T) {
	settings := testSettings(t) // no SiteJoinBundle
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-xyz", DeviceToken: "device-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)

	var pinnedCalled, siteCoordCalled bool
	claim := &countingClaimCoord{}
	seams := seamsFor(fc, fs, det, newTickerFactory())
	seams.NewPinnedClient = func(siteclient.Profile) (*http.Client, error) { pinnedCalled = true; return &http.Client{}, nil }
	seams.NewSiteCoordinator = func(_, _, _ string, _ *http.Client) Coordinator { siteCoordCalled = true; return fc }
	seams.Reconciler = &fakeReconciler{}
	seams.ClaimCoordinator = claim

	rt := New(settings, seams)
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if rt.site != nil {
		t.Error("direct mode built a site runtime")
	}
	if pinnedCalled || siteCoordCalled {
		t.Error("direct mode invoked a Site Mode seam")
	}
	if claim.claimCount() != 0 {
		t.Error("direct mode ran the claim runner")
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// eventCoord extends fakeCoordinator semantics with ordered event capture for
// the reclaim-release presence test.
func (f *fakeCoordinator) userIdleEventSequences() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, e := range f.events {
		if e.Kind == protocol.EventKindUserIdle {
			out = append(out, e.Detail["sequence"])
		}
	}
	return out
}

// TestSiteReclaimReleasePublishesSequencedUserIdle proves the #3 fix: releasing a
// reclaim publishes a sequenced user_idle presence event so the coordinator's
// durable presence generation advances to match the fence raised while paused,
// letting serving resume. Engagement needs no event (availability cancels claims
// and serving_paused heartbeats fence the broker).
func TestSiteReclaimReleasePublishesSequencedUserIdle(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "chat", Port: 8100, State: protocol.ReplicaStateReady},
	}}
	det, _ := idle.NewFakeDetector(200)
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, &fakeReconciler{}, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	pt := tf.get(t, 100*time.Millisecond)

	// Reclaim: no presence event is required on engagement.
	if _, err := preempt.RequestReclaim(settings.StatePath); err != nil {
		t.Fatal(err)
	}
	pt.fire()
	pt.fire()
	waitFor(t, "reclaimed", func() bool { return rt.reclaim.IsReclaimed() })

	// Release: a sequenced user_idle presence event must be published.
	if _, err := preempt.RequestRelease(settings.StatePath); err != nil {
		t.Fatal(err)
	}
	pt.fire()
	pt.fire()
	waitFor(t, "released", func() bool { return !rt.reclaim.IsReclaimed() })
	waitFor(t, "user_idle presence event", func() bool { return len(fc.userIdleEventSequences()) >= 1 })

	seqs := fc.userIdleEventSequences()
	if seqs[0] == "" {
		t.Fatal("reclaim-release user_idle event carried no sequence")
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// erroringClaimCoord always returns an unexpected error from Claim, exercising
// the non-fatal claim-runner path.
type erroringClaimCoord struct{}

func (erroringClaimCoord) Claim(ctx context.Context, _ time.Duration) (*inference.Claim, error) {
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	return nil, errTest
}
func (erroringClaimCoord) Upload(context.Context, inference.Claim, int, string, io.Reader) error {
	return nil
}
func (erroringClaimCoord) Fail(context.Context, inference.Claim, inference.FailureCode, bool) error {
	return nil
}

// TestSiteClaimRunnerErrorDoesNotKillDaemon proves the #1 fix at the runtime
// level: an unexpected claim-runner error stops serving but the daemon keeps
// heartbeating (a genuine auth rejection is caught by the heartbeat loop, which
// shares the token). The agent must not shut down on it.
func TestSiteClaimRunnerErrorDoesNotKillDaemon(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "chat", Port: 8100, State: protocol.ReplicaStateReady},
	}}
	det, _ := idle.NewFakeDetector(200)
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, &fakeReconciler{}, erroringClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	// Make the runner eligible so it actually claims and hits the error path.
	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire()
	// The daemon must keep heartbeating despite the failing claim runner.
	before := fc.heartbeatCount()
	tf.get(t, 5*time.Second).fire()
	waitFor(t, "daemon still heartbeating", func() bool { return fc.heartbeatCount() > before })

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil (claim error must not be fatal)", err)
	}
}
