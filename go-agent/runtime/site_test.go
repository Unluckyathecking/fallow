package runtime

import (
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/discovery"
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
	// The pinned client and the discovery seam are both stubbed so a test whose
	// subject is enrollment, sequencing or reconciliation never reaches a real
	// socket. A profile carrying mdns_service probes its static origins before
	// dialing, and an unstubbed seam would turn that into a live dial and a live
	// multicast query. The stub answers every origin, so the static profile is
	// reachable and no fallback is needed; tests about the fallback itself
	// override both seams.
	s.NewPinnedClient = func(siteclient.Profile) (*http.Client, error) {
		return newProbeRecorder(nil).client(), nil
	}
	s.Discovery = &fakeDiscovery{err: discovery.ErrNotConfigured}
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
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, rec, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	// Reconciliation is held ineligible until the first authoritative presence
	// and reclaim sample; a poll tick primes it (idle, unreclaimed here).
	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire()
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

// ── new P1: reconciliation must be ineligible until the first authoritative
//    presence AND reclaim sample, not the constructor defaults on restart ──────

// seedSiteIdentity persists a resumable Site identity for a restart test.
func seedSiteIdentity(t *testing.T, path string) {
	t.Helper()
	id := state.Identity{
		AgentID: "agent-site", DeviceToken: "dev-tok",
		Site: &state.SiteProfile{
			SiteID:                "clfs-pilot",
			CoordinatorURLs:       []string{"https://10.24.8.10:8330"},
			CoordinatorSPKISHA256: []string{"sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))},
		},
	}
	if err := state.Save(path, id); err != nil {
		t.Fatal(err)
	}
}

// TestRestartDoesNotReconcileForActiveUserBeforeFirstPoll proves that on restart
// a heartbeat arriving before the first poll cannot reconcile off the
// constructor-default IDLE state while the user is in fact active. Without the
// priming gate, servingEligible would read the default (idle, unreclaimed) and
// start a model download for an active user.
func TestRestartDoesNotReconcileForActiveUserBeforeFirstPoll(t *testing.T) {
	settings := siteSettings(t)
	seedSiteIdentity(t, settings.StatePath)
	_ = os.Remove(settings.SiteJoinBundle)

	fc := &fakeCoordinator{
		hbResp: protocol.HeartbeatResponse{DesiredModels: []string{"m1"}},
	}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(0) // the user is active
	rec := &fakeReconciler{}
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, rec, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	// The first heartbeat submits the desired set before any poll has run.
	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	time.Sleep(50 * time.Millisecond) // give the reconcile worker a chance to (wrongly) apply
	if rec.count() != 0 {
		t.Fatalf("reconciled %d time(s) off constructor defaults before the first poll", rec.count())
	}

	// The first poll detects the active user; reconciliation stays deferred.
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire()
	waitFor(t, "suspended", func() bool { return fs.contains("suspend_all") })
	time.Sleep(50 * time.Millisecond)
	if rec.count() != 0 {
		t.Fatalf("reconciled %d time(s) for an active user", rec.count())
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestRestartDoesNotReconcileWithReclaimFlagBeforeFirstPoll proves that a
// reclaim.flag present at startup is honoured before the first poll: the reclaim
// controller only reads the flag on OnPoll, so without the priming gate a
// heartbeat before the first poll would reconcile off the default unreclaimed
// state and start work on a machine the user has taken.
func TestRestartDoesNotReconcileWithReclaimFlagBeforeFirstPoll(t *testing.T) {
	settings := siteSettings(t)
	seedSiteIdentity(t, settings.StatePath)
	_ = os.Remove(settings.SiteJoinBundle)
	if _, err := preempt.RequestReclaim(settings.StatePath); err != nil {
		t.Fatal(err)
	}

	fc := &fakeCoordinator{
		hbResp: protocol.HeartbeatResponse{DesiredModels: []string{"m1"}},
	}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200) // idle, so only the reclaim flag withholds serving
	rec := &fakeReconciler{}
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, rec, &countingClaimCoord{}))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	time.Sleep(50 * time.Millisecond)
	if rec.count() != 0 {
		t.Fatalf("reconciled %d time(s) off default unreclaimed state before the first poll", rec.count())
	}

	// The first poll reads the reclaim flag; reconciliation stays deferred.
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire()
	waitFor(t, "reclaimed", func() bool { return rt.reclaim.IsReclaimed() })
	time.Sleep(50 * time.Millisecond)
	if rec.count() != 0 {
		t.Fatalf("reconciled %d time(s) while reclaimed", rec.count())
	}

	// Releasing the reclaim then makes it eligible and the deferred set applies.
	if _, err := preempt.RequestRelease(settings.StatePath); err != nil {
		t.Fatal(err)
	}
	pt.fire()
	pt.fire()
	waitFor(t, "reconcile after release", func() bool { return rec.count() >= 1 })

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// ── discovery fallback ───────────────────────────────────────────────────────

const testSiteService = "_fallow._tcp.local."

// mdnsProfile builds a Site profile that has opted into mDNS, so the base URL
// choice probes its static origins instead of taking the first one blind.
func mdnsProfile(urls ...string) siteclient.Profile {
	svc := testSiteService
	return siteclient.Profile{
		SiteID:                "clfs-pilot",
		CoordinatorURLs:       urls,
		CoordinatorSPKISHA256: []string{"sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))},
		MDNSService:           &svc,
	}
}

type probeRoundTripper func(*http.Request) (*http.Response, error)

func (f probeRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// probeRecorder is a pinned-client stand-in that answers each origin according
// to a script and records the order origins were probed in, so a test can assert
// both which coordinator was chosen and that static origins were tried first.
type probeRecorder struct {
	mu      sync.Mutex
	probed  []string
	answers map[string]error // absent or nil means the origin answers
}

func newProbeRecorder(answers map[string]error) *probeRecorder {
	return &probeRecorder{answers: answers}
}

func (p *probeRecorder) client() *http.Client {
	return &http.Client{Transport: probeRoundTripper(func(r *http.Request) (*http.Response, error) {
		origin := "https://" + r.URL.Host
		p.mu.Lock()
		p.probed = append(p.probed, origin)
		err := p.answers[origin]
		p.mu.Unlock()
		if err != nil {
			return nil, err
		}
		// Any status proves a pinned peer is up; the probe reads no meaning from it.
		return &http.Response{
			StatusCode: http.StatusNotFound,
			Body:       io.NopCloser(strings.NewReader("not found")),
			Header:     make(http.Header),
			Request:    r,
		}, nil
	})}
}

func (p *probeRecorder) order() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.probed...)
}

// fakeDiscovery is the injected mDNS seam.
type fakeDiscovery struct {
	mu    sync.Mutex
	calls int
	out   []string
	err   error
}

func (d *fakeDiscovery) Candidates(context.Context, siteclient.Profile) ([]string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls++
	return d.out, d.err
}

func (d *fakeDiscovery) count() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}

var errUnreachable = errTestType("connection refused")

// baseURLFor runs the base URL choice against a scripted network.
func baseURLFor(t *testing.T, p siteclient.Profile, probes *probeRecorder, disc *fakeDiscovery) (string, error) {
	t.Helper()
	rt := New(config.Settings{}, Seams{Discovery: disc})
	return rt.siteBaseURL(context.Background(), p, probes.client())
}

// TestSiteWithoutMDNSTakesStaticWithoutTouchingTheNetwork is the compatibility
// case: an agent that never opted into mDNS must behave exactly as it did before
// the fallback existed — first static origin, no probe, no query.
func TestSiteWithoutMDNSTakesStaticWithoutTouchingTheNetwork(t *testing.T) {
	p := mdnsProfile("https://10.24.8.10:8330", "https://10.24.8.11:8330")
	p.MDNSService = nil
	probes := newProbeRecorder(map[string]error{"https://10.24.8.10:8330": errUnreachable})
	disc := &fakeDiscovery{out: []string{"https://192.0.2.5:8330"}}

	got, err := baseURLFor(t, p, probes, disc)
	if err != nil || got != "https://10.24.8.10:8330" {
		t.Fatalf("got %q, %v; want the first static origin", got, err)
	}
	if len(probes.order()) != 0 {
		t.Fatalf("probed the network without mdns_service: %v", probes.order())
	}
	if disc.count() != 0 {
		t.Fatal("queried mDNS without mdns_service")
	}
}

// TestSiteStaticRemainsFirstAndSufficient proves a reachable static origin ends
// the search: mDNS is a fallback, never a path.
func TestSiteStaticRemainsFirstAndSufficient(t *testing.T) {
	p := mdnsProfile("https://10.24.8.10:8330", "https://10.24.8.11:8330")
	probes := newProbeRecorder(nil)
	disc := &fakeDiscovery{out: []string{"https://192.0.2.5:8330"}}

	got, err := baseURLFor(t, p, probes, disc)
	if err != nil || got != "https://10.24.8.10:8330" {
		t.Fatalf("got %q, %v; want the first static origin", got, err)
	}
	if disc.count() != 0 {
		t.Fatal("queried mDNS while a static origin was reachable")
	}
	if len(probes.order()) != 1 {
		t.Fatalf("probed past the first reachable origin: %v", probes.order())
	}
}

// TestSiteTriesEveryStaticBeforeQuerying proves the query opens only once all
// static origins are unreachable, in listed order.
func TestSiteTriesEveryStaticBeforeQuerying(t *testing.T) {
	p := mdnsProfile("https://10.24.8.10:8330", "https://10.24.8.11:8330")
	probes := newProbeRecorder(map[string]error{"https://10.24.8.10:8330": errUnreachable})
	disc := &fakeDiscovery{}

	got, err := baseURLFor(t, p, probes, disc)
	if err != nil || got != "https://10.24.8.11:8330" {
		t.Fatalf("got %q, %v; want the second static origin", got, err)
	}
	if disc.count() != 0 {
		t.Fatal("queried mDNS while a later static origin was reachable")
	}
	want := []string{"https://10.24.8.10:8330", "https://10.24.8.11:8330"}
	if strings.Join(probes.order(), ",") != strings.Join(want, ",") {
		t.Fatalf("probe order %v, want %v", probes.order(), want)
	}
}

// TestSiteFallsBackToADiscoveredCandidate is the feature: every static origin is
// unreachable, so the query runs and its candidate is dialed.
func TestSiteFallsBackToADiscoveredCandidate(t *testing.T) {
	p := mdnsProfile("https://10.24.8.10:8330")
	probes := newProbeRecorder(map[string]error{"https://10.24.8.10:8330": errUnreachable})
	disc := &fakeDiscovery{out: []string{"https://192.0.2.5:8330"}}

	got, err := baseURLFor(t, p, probes, disc)
	if err != nil || got != "https://192.0.2.5:8330" {
		t.Fatalf("got %q, %v; want the discovered candidate", got, err)
	}
	if disc.count() != 1 {
		t.Fatalf("discovery called %d times, want exactly one bounded query", disc.count())
	}
	want := []string{"https://10.24.8.10:8330", "https://192.0.2.5:8330"}
	if strings.Join(probes.order(), ",") != strings.Join(want, ",") {
		t.Fatalf("probe order %v, want static before discovered %v", probes.order(), want)
	}
}

// TestSiteSkipsADiscoveredCandidateFailingThePin is the trust case: a responder
// on the segment answers, its certificate misses the stored pin, and it is
// skipped without the pin set changing.
func TestSiteSkipsADiscoveredCandidateFailingThePin(t *testing.T) {
	p := mdnsProfile("https://10.24.8.10:8330")
	pinsBefore := strings.Join(p.CoordinatorSPKISHA256, ",")
	probes := newProbeRecorder(map[string]error{
		"https://10.24.8.10:8330": errUnreachable,
		"https://192.0.2.4:8330":  &siteclient.PinError{Err: errTestType("certificate pin mismatch")},
	})
	disc := &fakeDiscovery{out: []string{"https://192.0.2.4:8330", "https://192.0.2.5:8330"}}

	got, err := baseURLFor(t, p, probes, disc)
	if err != nil || got != "https://192.0.2.5:8330" {
		t.Fatalf("got %q, %v; want the candidate that passed the pin", got, err)
	}
	if strings.Join(p.CoordinatorSPKISHA256, ",") != pinsBefore {
		t.Fatalf("pin set changed: %v", p.CoordinatorSPKISHA256)
	}
}

// TestSiteKeepsTheStaticProfileWhenDiscoveryFindsNothing covers the ordinary
// school-VLAN outcome. A timeout, a refused socket or a segment answering only
// for other sites must leave the static profile intact rather than fail startup.
func TestSiteKeepsTheStaticProfileWhenDiscoveryFindsNothing(t *testing.T) {
	cases := []struct {
		name string
		disc *fakeDiscovery
	}{
		{"query elapsed with nothing usable", &fakeDiscovery{err: &discovery.NoCandidateError{
			Service: testSiteService, SiteID: "clfs-pilot", Timeout: time.Second,
		}}},
		{"multicast socket refused", &fakeDiscovery{err: &discovery.QueryError{Err: errTestType("bind failed")}}},
		{"profile did not opt in after all", &fakeDiscovery{err: discovery.ErrNotConfigured}},
		{"query returned no candidate at all", &fakeDiscovery{}},
		{"every candidate refused the pin", &fakeDiscovery{out: []string{"https://192.0.2.4:8330"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := mdnsProfile("https://10.24.8.10:8330")
			probes := newProbeRecorder(map[string]error{
				"https://10.24.8.10:8330": errUnreachable,
				"https://192.0.2.4:8330":  &siteclient.PinError{Err: errTestType("certificate pin mismatch")},
			})
			got, err := baseURLFor(t, p, probes, c.disc)
			if err != nil {
				t.Fatalf("startup failed on a lost query: %v", err)
			}
			if got != "https://10.24.8.10:8330" {
				t.Fatalf("got %q, want the static profile kept intact", got)
			}
		})
	}
}

// TestSiteCorruptProfileStillFailsClosed proves the fallback did not soften the
// existing check on a stored profile with no usable origin.
func TestSiteCorruptProfileStillFailsClosed(t *testing.T) {
	for _, urls := range [][]string{nil, {"http://10.24.8.10:8330"}, {"https://"}} {
		p := mdnsProfile(urls...)
		if _, err := baseURLFor(t, p, newProbeRecorder(nil), &fakeDiscovery{}); err == nil {
			t.Fatalf("corrupt profile %v was accepted", urls)
		}
	}
}

// TestSiteEnrollmentDialsTheDiscoveredCoordinator drives the whole Site Mode
// composition rather than the choice alone: a join file that opts into mDNS,
// an unreachable static origin, and the enrollment call landing on the
// discovered coordinator.
func TestSiteEnrollmentDialsTheDiscoveredCoordinator(t *testing.T) {
	settings := testSettings(t)
	settings.CoordinatorURL = ""
	pin := "sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))
	body := `{"version":1,"site_id":"clfs-pilot",` +
		`"coordinator_urls":["https://10.24.8.10:8330"],` +
		`"coordinator_spki_sha256":["` + pin + `"],` +
		`"enrollment_token":"one-use-secret","mdns_service":"` + testSiteService + `"}`
	settings.SiteJoinBundle = filepath.Join(filepath.Dir(settings.StatePath), "join.json")
	if err := os.WriteFile(settings.SiteJoinBundle, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	probes := newProbeRecorder(map[string]error{"https://10.24.8.10:8330": errUnreachable})
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	det, _ := idle.NewFakeDetector(200)
	var dialed string
	seams := siteSeamsFor(fc, &fakeSupervisor{}, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{})
	seams.NewPinnedClient = func(siteclient.Profile) (*http.Client, error) { return probes.client(), nil }
	seams.NewSiteCoordinator = func(baseURL, agentID, deviceToken string, _ *http.Client) Coordinator {
		dialed = baseURL
		fc.seed(agentID, deviceToken)
		return fc
	}
	seams.Discovery = &fakeDiscovery{out: []string{"https://192.0.2.5:8330"}}

	if _, err := New(settings, seams).resolveWiring(context.Background()); err != nil {
		t.Fatalf("resolveWiring: %v", err)
	}
	if dialed != "https://192.0.2.5:8330" {
		t.Fatalf("enrolled against %q, want the discovered coordinator", dialed)
	}
	// The enrollment token still went out only after a pinned peer answered.
	if len(probes.order()) != 2 {
		t.Fatalf("probe order %v, want the static origin then the candidate", probes.order())
	}
}
