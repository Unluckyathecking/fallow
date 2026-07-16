package runtime

import (
	"context"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/state"
	"github.com/Unluckyathecking/fallow/go-agent/supervisor"
)

// ── deterministic fakes ──────────────────────────────────────────────────────

// fakeCoordinator records every call and can inject an auth rejection.
type fakeCoordinator struct {
	mu           sync.Mutex
	agentID      string
	deviceToken  string
	registerResp protocol.RegisterResponse
	hbResp       protocol.HeartbeatResponse
	heartbeats   []protocol.Heartbeat
	events       []protocol.AgentEvent
	registers    int
	polls        int

	authErr   error // returned once heartbeat count reaches authAfter (0 = never)
	authAfter int
}

func (f *fakeCoordinator) seed(agentID, deviceToken string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if agentID != "" {
		f.agentID = agentID
	}
	if deviceToken != "" {
		f.deviceToken = deviceToken
	}
}

func (f *fakeCoordinator) Register(_ context.Context, _ protocol.RegisterRequest) (protocol.RegisterResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.registers++
	f.agentID = f.registerResp.AgentID
	f.deviceToken = f.registerResp.DeviceToken
	return f.registerResp, nil
}

func (f *fakeCoordinator) Heartbeat(_ context.Context, hb protocol.Heartbeat) (protocol.HeartbeatResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.heartbeats = append(f.heartbeats, hb)
	if f.authAfter > 0 && len(f.heartbeats) >= f.authAfter {
		return protocol.HeartbeatResponse{}, f.authErr
	}
	return f.hbResp, nil
}

// PollWork blocks until ctx is cancelled, emulating a long poll with no work,
// so the work loop never busy-spins in the test.
func (f *fakeCoordinator) PollWork(ctx context.Context, _ float64) (*protocol.WorkUnitLease, error) {
	f.mu.Lock()
	f.polls++
	f.mu.Unlock()
	<-ctx.Done()
	return nil, ctx.Err()
}

func (f *fakeCoordinator) PushEvent(_ context.Context, event protocol.AgentEvent) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, event)
	return nil
}

func (f *fakeCoordinator) AgentID() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.agentID
}

func (f *fakeCoordinator) DeviceToken() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.deviceToken
}

func (f *fakeCoordinator) heartbeatCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.heartbeats)
}

func (f *fakeCoordinator) pollCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.polls
}

func (f *fakeCoordinator) hasHeartbeatState(state protocol.AgentState) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, hb := range f.heartbeats {
		if hb.State == state {
			return true
		}
	}
	return false
}

// fakeSupervisor records lifecycle calls and serves canned statuses.
type fakeSupervisor struct {
	mu       sync.Mutex
	log      []string
	statuses []protocol.ReplicaStatus
}

func (s *fakeSupervisor) record(call string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.log = append(s.log, call)
}

func (s *fakeSupervisor) SuspendAll()           { s.record("suspend_all") }
func (s *fakeSupervisor) ResumeAll()            { s.record("resume_all") }
func (s *fakeSupervisor) StopReplica(id string) { s.record("stop_replica:" + id) }
func (s *fakeSupervisor) StopAll()              { s.record("stop_all") }

func (s *fakeSupervisor) Statuses() []protocol.ReplicaStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]protocol.ReplicaStatus, len(s.statuses))
	copy(out, s.statuses)
	return out
}

func (s *fakeSupervisor) contains(call string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.log {
		if c == call {
			return true
		}
	}
	return false
}

// manualTicker fires only when the test calls fire().
type manualTicker struct{ c chan time.Time }

func (m *manualTicker) Chan() <-chan time.Time { return m.c }
func (m *manualTicker) Stop()                  {}

// fire delivers one tick and blocks until the loop receives it, giving the test
// a synchronisation point.
func (m *manualTicker) fire() { m.c <- time.Time{} }

// tickerFactory hands out one manualTicker per requested duration.
type tickerFactory struct {
	mu    sync.Mutex
	byDur map[time.Duration]*manualTicker
}

func newTickerFactory() *tickerFactory {
	return &tickerFactory{byDur: make(map[time.Duration]*manualTicker)}
}

func (f *tickerFactory) New(d time.Duration) Ticker {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &manualTicker{c: make(chan time.Time)}
	f.byDur[d] = t
	return t
}

// get waits until a ticker of duration d has been created, then returns it.
func (f *tickerFactory) get(t *testing.T, d time.Duration) *manualTicker {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		tk := f.byDur[d]
		f.mu.Unlock()
		if tk != nil {
			return tk
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("no ticker created for %v", d)
	return nil
}

func fixedNow() time.Time { return time.Date(2026, 7, 16, 12, 0, 0, 0, time.UTC) }

func testSettings(t *testing.T) config.Settings {
	t.Helper()
	return config.Settings{
		CoordinatorURL:    "http://coord",
		EnrollmentToken:   "enroll-token",
		BindHost:          "127.0.0.1",
		LlamaServerBinary: "/usr/bin/true",
		StatePath:         filepath.Join(t.TempDir(), "agent-state.json"),
		CacheDir:          t.TempDir(),
		WorkPollTimeoutS:  20,
		ActiveSleepS:      1,
		PortRange:         config.PortRange{Start: 8100, Count: 16},
	}
}

func testConfig() protocol.AgentConfig {
	return protocol.AgentConfig{
		HeartbeatIntervalS: 5,
		IdleThresholdS:     120,
		PollIntervalMs:     100,
		VRAMEvictAfterS:    60,
	}
}

func seamsFor(fc *fakeCoordinator, fs *fakeSupervisor, det idle.Detector, tf *tickerFactory) Seams {
	return Seams{
		NewCoordinator: func(_, agentID, deviceToken string) Coordinator {
			fc.seed(agentID, deviceToken)
			return fc
		},
		NewSupervisor: func(supervisor.Config) (Supervisor, error) { return fs, nil },
		Detector:      det,
		Now:           fixedNow,
		Monotonic:     func() float64 { return 0 },
		NewTicker:     tf.New,
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// authErrorValue returns a genuine *heartbeat.AuthError, obtained by driving the
// real client against a 401, so the fake can inject the exact error type the
// runtime keys its fatal path on.
func authErrorValue(t *testing.T) error {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	t.Cleanup(srv.Close)
	client := heartbeat.NewClient(srv.URL, nil, heartbeat.WithIdentity("a", "t"))
	_, err := client.Heartbeat(context.Background(), protocol.Heartbeat{AgentID: "a"})
	if !isAuthError(err) {
		t.Fatalf("expected an auth error from the 401 stub, got %v", err)
	}
	return err
}

// ── tests ────────────────────────────────────────────────────────────────────

// TestRuntimeEnrollsHeartbeatsPollsAndPreempts drives the whole wiring: enroll,
// first heartbeat, work poll, and a preemption tick, all against fakes.
func TestRuntimeEnrollsHeartbeatsPollsAndPreempts(t *testing.T) {
	settings := testSettings(t)
	fc := &fakeCoordinator{
		registerResp: protocol.RegisterResponse{
			AgentID:     "agent-xyz",
			DeviceToken: "device-tok",
			Config:      testConfig(),
		},
	}
	fs := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "chat-model", Port: 8100, State: protocol.ReplicaStateReady},
	}}
	det, err := idle.NewFakeDetector(200) // idle: machine stays idle
	if err != nil {
		t.Fatal(err)
	}
	tf := newTickerFactory()

	rt := New(settings, seamsFor(fc, fs, det, tf))
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	// Enrolled once, identity persisted 0600, and the first heartbeat is idle
	// and carries the supervisor's replica set.
	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if fc.registers != 1 {
		t.Fatalf("register called %d times, want 1", fc.registers)
	}
	assertState0600(t, settings.StatePath)
	first := fc.heartbeats[0]
	if first.AgentID != "agent-xyz" {
		t.Errorf("heartbeat agent_id = %q, want agent-xyz", first.AgentID)
	}
	if first.State != protocol.AgentStateIdle {
		t.Errorf("first heartbeat state = %v, want idle", first.State)
	}
	if len(first.Replicas) != 1 || first.Replicas[0].ModelID != "chat-model" {
		t.Errorf("heartbeat replicas = %v, want the supervisor's set", first.Replicas)
	}

	// The work loop long-polls while idle.
	waitFor(t, "work poll", func() bool { return fc.pollCount() >= 1 })

	// A fresh-input tick drives the controller into Active and suspends replicas.
	if err := det.SetIdle(0); err != nil {
		t.Fatal(err)
	}
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire() // second tick guarantees the first OnPoll completed
	waitFor(t, "suspend_all", func() bool { return fs.contains("suspend_all") })

	// Clean shutdown on cancellation: drain, final DRAINING heartbeat, stop all.
	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
	if !fc.hasHeartbeatState(protocol.AgentStateDraining) {
		t.Error("no final DRAINING heartbeat was sent")
	}
	if !fs.contains("stop_all") {
		t.Error("supervisor.StopAll was not called on shutdown")
	}
}

// TestRuntimeResumesFromPersistedIdentity skips registration when a state file
// already exists.
func TestRuntimeResumesFromPersistedIdentity(t *testing.T) {
	settings := testSettings(t)
	writeIdentity(t, settings.StatePath, "saved-agent", "saved-token")
	fc := &fakeCoordinator{}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	tf := newTickerFactory()

	rt := New(settings, seamsFor(fc, fs, det, tf))
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if fc.registers != 0 {
		t.Errorf("register called %d times on resume, want 0", fc.registers)
	}
	if got := fc.heartbeats[0].AgentID; got != "saved-agent" {
		t.Errorf("heartbeat agent_id = %q, want saved-agent", got)
	}

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestRuntimeStopsOnAuthRejection surfaces a heartbeat auth error as fatal and
// still tears down cleanly.
func TestRuntimeStopsOnAuthRejection(t *testing.T) {
	settings := testSettings(t)
	fc := &fakeCoordinator{
		registerResp: protocol.RegisterResponse{
			AgentID:     "agent-xyz",
			DeviceToken: "device-tok",
			Config:      testConfig(),
		},
		authErr:   authErrorValue(t),
		authAfter: 1,
	}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	tf := newTickerFactory()

	rt := New(settings, seamsFor(fc, fs, det, tf))
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(context.Background()) }()

	err := <-runErr
	if !isAuthError(err) {
		t.Fatalf("Run returned %v, want an auth error", err)
	}
	if !fs.contains("stop_all") {
		t.Error("supervisor.StopAll was not called after the fatal auth error")
	}
}

// TestResolveIdentityRequiresEnrollmentToken refuses to start unenrolled with no
// token.
func TestResolveIdentityRequiresEnrollmentToken(t *testing.T) {
	settings := testSettings(t)
	settings.EnrollmentToken = ""
	fc := &fakeCoordinator{}
	fs := &fakeSupervisor{}
	det, _ := idle.NewFakeDetector(200)
	tf := newTickerFactory()

	rt := New(settings, seamsFor(fc, fs, det, tf))
	err := rt.Run(context.Background())
	if err == nil {
		t.Fatal("expected an error when unenrolled with no token")
	}
	if fc.registers != 0 {
		t.Errorf("register called %d times, want 0", fc.registers)
	}
}

// nanDetector reports a non-finite idle value with no error, as some OS idle
// APIs do when there is no GUI session.
type nanDetector struct{}

func (nanDetector) SecondsSinceInput() (float64, error) { return math.NaN(), nil }

// TestSampleIdleRejectsNonFinite pins the guard that a NaN idle reading never
// reaches the wire (it once broke enrollment by shipping NaN user_idle_s).
func TestSampleIdleRejectsNonFinite(t *testing.T) {
	rt := New(testSettings(t), Seams{Detector: nanDetector{}})
	if _, ok := rt.sampleIdle(); ok {
		t.Error("sampleIdle accepted a NaN reading")
	}
	if got := rt.idleOrAway(); got != awayIdleS {
		t.Errorf("idleOrAway = %v on NaN, want the away fallback %v", got, awayIdleS)
	}
}

func assertState0600(t *testing.T, path string) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("state file not written: %v", err)
	}
	if runtime.GOOS == "windows" {
		return // Windows has no POSIX modes
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("state file mode = %o, want 600", perm)
	}
}

func writeIdentity(t *testing.T, path, agentID, deviceToken string) {
	t.Helper()
	if err := state.Save(path, state.Identity{AgentID: agentID, DeviceToken: deviceToken}); err != nil {
		t.Fatalf("seed identity: %v", err)
	}
}
