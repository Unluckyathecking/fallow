package runtime

import (
	"context"
	"errors"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/reconcile"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// ── fix (1): presence sampled before READY is exposed on the first poll ───────

// activeThenSettle reports the given idle value; the runtime samples it once per
// poll. A near-zero value makes the preemption controller flip to active.
// TestFirstPollDoesNotExposeReadyWhileUserActive drives one poll with an active
// user and a READY replica and asserts the claim runner is never offered work.
func TestFirstPollDoesNotExposeReadyWhileUserActive(t *testing.T) {
	settings := siteSettings(t)
	fc := &fakeCoordinator{registerResp: protocol.RegisterResponse{
		AgentID: "agent-site", DeviceToken: "dev-tok", Config: testConfig(),
	}}
	fs := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "chat", Port: 8100, State: protocol.ReplicaStateReady},
	}}
	det, _ := idle.NewFakeDetector(0) // user is actively typing
	claim := &countingClaimCoord{}
	tf := newTickerFactory()
	rt := New(settings, siteSeamsFor(fc, fs, det, tf, &fakeReconciler{}, claim))

	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire() // first poll: sample presence (active) before publishing READY
	pt.fire()
	waitFor(t, "suspended", func() bool { return fs.contains("suspend_all") })

	// The user is active, so the availability view must not be ready and the
	// claim runner must not have claimed.
	if rt.site.availability.Snapshot().Ready {
		t.Error("availability exposed READY while the user is active")
	}
	if claim.claimCount() != 0 {
		t.Errorf("claim runner claimed %d times while user active, want 0", claim.claimCount())
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// ── fix (2): the guard refuses a start if the user returns during a slow Ensure ─

type recordingSupervisor struct {
	fakeSupervisor
	mu      sync.Mutex
	started []string
}

func (s *recordingSupervisor) StartReplica(m protocol.ModelManifest, path string, port int) error {
	s.mu.Lock()
	s.started = append(s.started, m.ModelID)
	s.mu.Unlock()
	return nil
}
func (s *recordingSupervisor) startCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.started)
}

type stubManifestSource struct{}

func (stubManifestSource) Manifest(_ context.Context, id string) (protocol.ModelManifest, error) {
	return protocol.ModelManifest{ModelID: id, FileName: id + ".gguf"}, nil
}

// blockingCache blocks Ensure until released, modelling a slow model download.
type blockingCache struct {
	entered chan struct{}
	release chan struct{}
}

func (c *blockingCache) Ensure(ctx context.Context, _ protocol.ModelManifest) (string, error) {
	close(c.entered)
	select {
	case <-c.release:
		return "/models/m.gguf", nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// TestGuardRefusesStartWhenUserReturnsDuringEnsure is the #2 race proof: the
// outer eligibility check passes, Ensure blocks (slow download), the user
// returns, and the guard must refuse the StartReplica that would otherwise land
// after suspension.
func TestGuardRefusesStartWhenUserReturnsDuringEnsure(t *testing.T) {
	sup := &recordingSupervisor{}
	cache := &blockingCache{entered: make(chan struct{}), release: make(chan struct{})}
	var eligible sync.Mutex
	isEligible := true
	guarded := guardedSupervisor{Supervisor: sup, eligible: func() bool {
		eligible.Lock()
		defer eligible.Unlock()
		return isEligible
	}}
	rec, err := reconcile.New(stubManifestSource{}, cache, guarded, reconcile.PortRange{Start: 8100, Count: 4})
	if err != nil {
		t.Fatal(err)
	}

	done := make(chan error, 1)
	go func() { done <- rec.Apply(context.Background(), []string{"m1"}) }()

	<-cache.entered // Apply is now inside the slow Ensure, past the outer check
	eligible.Lock()
	isEligible = false // the user returns
	eligible.Unlock()
	close(cache.release)

	<-done
	if sup.startCount() != 0 {
		t.Fatalf("guard allowed %d replica start(s) after the user returned", sup.startCount())
	}
}

// ── fix (3): a failed presence push fails closed, never acking the flush early ─

type failingCoord struct {
	recordingCoord
	fail atomic.Bool
}

func (c *failingCoord) PushEvent(ctx context.Context, event protocol.AgentEvent) error {
	if c.fail.Load() {
		return errors.New("push failed")
	}
	return c.recordingCoord.PushEvent(ctx, event)
}

// TestUndeliverablePresenceEventFailsClosed proves a presence event that cannot
// be delivered triggers the fail-closed callback instead of silently dropping
// and letting a later heartbeat overtake the lost transition.
func TestUndeliverablePresenceEventFailsClosed(t *testing.T) {
	coord := &failingCoord{}
	coord.fail.Store(true)
	sink := newEventSink(coord)
	sink.sleep = func(time.Duration) {} // no backoff delay in the test
	var fatal atomic.Bool
	sink.onFatal = func(error) { fatal.Store(true) }
	sink.start()
	defer sink.close()

	sink.Emit(protocol.AgentEvent{Kind: protocol.EventKindUserIdle, Detail: map[string]string{"sequence": "4"}})
	// The flush must not return until the critical event resolves (fail closed).
	sink.flush()
	if !fatal.Load() {
		t.Fatal("an undeliverable presence event did not fail closed")
	}
}

// TestNonCriticalEventStaysBestEffort confirms a non-presence event that fails
// to push is logged best-effort and never fails the daemon closed.
func TestNonCriticalEventStaysBestEffort(t *testing.T) {
	coord := &failingCoord{}
	coord.fail.Store(true)
	sink := newEventSink(coord)
	sink.sleep = func(time.Duration) {}
	var fatal atomic.Bool
	sink.onFatal = func(error) { fatal.Store(true) }
	sink.start()
	defer sink.close()

	sink.Emit(protocol.AgentEvent{Kind: protocol.EventKindReplicaReady})
	sink.flush()
	if fatal.Load() {
		t.Fatal("a best-effort event failed the daemon closed")
	}
}

// ── fix (4): a corrupt persisted profile fails closed, never panics on index ──

// TestFirstCoordinatorURLRejectsCorruptProfile proves the profile guard fails
// closed on an empty or malformed coordinator URL list rather than indexing it.
func TestFirstCoordinatorURLRejectsCorruptProfile(t *testing.T) {
	cases := []siteclient.Profile{
		{CoordinatorURLs: nil},
		{CoordinatorURLs: []string{}},
		{CoordinatorURLs: []string{"http://insecure:8330"}},
		{CoordinatorURLs: []string{"https:///no-host"}},
		{CoordinatorURLs: []string{"::not a url"}},
	}
	for _, p := range cases {
		if _, err := firstCoordinatorURL(p); err == nil {
			t.Errorf("firstCoordinatorURL accepted a corrupt profile %+v", p.CoordinatorURLs)
		}
	}
	if got, err := firstCoordinatorURL(siteclient.Profile{CoordinatorURLs: []string{"https://10.24.8.10:8330"}}); err != nil || got != "https://10.24.8.10:8330" {
		t.Errorf("firstCoordinatorURL rejected a valid profile: %q, %v", got, err)
	}
}

// TestSiteRestartFailsClosedOnCorruptProfile drives the runtime with a persisted
// Site identity whose profile has no coordinator URL and asserts it fails closed.
func TestSiteRestartFailsClosedOnCorruptProfile(t *testing.T) {
	settings := siteSettings(t)
	corrupt := state.Identity{
		AgentID: "agent-site", DeviceToken: "dev-tok",
		Site: &state.SiteProfile{SiteID: "clfs", CoordinatorURLs: nil,
			CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}},
	}
	if err := state.Save(settings.StatePath, corrupt); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(settings.SiteJoinBundle)

	fc := &fakeCoordinator{}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	rt := New(settings, siteSeamsFor(fc, fs, det, newTickerFactory(), &fakeReconciler{}, &countingClaimCoord{}))
	if err := rt.Run(context.Background()); err == nil {
		t.Fatal("expected a fail-closed error on a corrupt persisted profile")
	}
}
