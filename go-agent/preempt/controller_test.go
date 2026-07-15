package preempt

import (
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// ── deterministic fakes (ported from preempt_fakes.py) ───────────────────────

// fakeClock is a settable monotonic source; T is what the next call returns.
type fakeClock struct{ T float64 }

func (c *fakeClock) read() float64 { return c.T }

func fixedNow() time.Time { return time.Date(2026, 7, 15, 12, 0, 0, 0, time.UTC) }

// recordingSupervisor records call order into a shared log and serves canned
// statuses.
type recordingSupervisor struct {
	log      *[]string
	statuses []protocol.ReplicaStatus
	stopped  []string
}

func (s *recordingSupervisor) SuspendAll() { *s.log = append(*s.log, "suspend_all") }
func (s *recordingSupervisor) ResumeAll()  { *s.log = append(*s.log, "resume_all") }
func (s *recordingSupervisor) StopReplica(id string) {
	*s.log = append(*s.log, "stop_replica:"+id)
	s.stopped = append(s.stopped, id)
}
func (s *recordingSupervisor) Statuses() []protocol.ReplicaStatus { return s.statuses }

// recordingSink records emitted events into a shared log and its own list.
type recordingSink struct {
	log    *[]string
	events []protocol.AgentEvent
}

func (s *recordingSink) Emit(event protocol.AgentEvent) {
	*s.log = append(*s.log, "emit:"+string(event.Kind))
	s.events = append(s.events, event)
}

func suspended(modelID string, port int, gpu bool) protocol.ReplicaStatus {
	return protocol.ReplicaStatus{ModelID: modelID, Port: port, State: protocol.ReplicaStateSuspended, GPU: gpu}
}

func stoppedStatus(modelID string, port int) protocol.ReplicaStatus {
	return protocol.ReplicaStatus{ModelID: modelID, Port: port, State: protocol.ReplicaStateStopped}
}

func testConfig(overrides map[string]float64) protocol.AgentConfig {
	cfg := protocol.AgentConfig{
		IdleThresholdS:  120.0,
		PollIntervalMs:  100,
		VRAMEvictAfterS: 60.0,
	}
	if v, ok := overrides["idle_threshold_s"]; ok {
		cfg.IdleThresholdS = v
	}
	if v, ok := overrides["poll_interval_ms"]; ok {
		cfg.PollIntervalMs = int(v)
	}
	if v, ok := overrides["vram_evict_after_s"]; ok {
		cfg.VRAMEvictAfterS = v
	}
	return cfg
}

type harness struct {
	controller *Controller
	log        *[]string
	supervisor *recordingSupervisor
	sink       *recordingSink
}

func makeHarness(statuses []protocol.ReplicaStatus, clock *fakeClock, cfg *protocol.AgentConfig) harness {
	log := &[]string{}
	sup := &recordingSupervisor{log: log, statuses: statuses}
	sink := &recordingSink{log: log}
	if clock == nil {
		clock = &fakeClock{}
	}
	config := testConfig(nil)
	if cfg != nil {
		config = *cfg
	}
	controller := NewController(sup, sink, config, "agent-1", Options{
		Monotonic: clock.read,
		Now:       fixedNow,
	})
	return harness{controller: controller, log: log, supervisor: sup, sink: sink}
}

func indexIn(log []string, want string) int {
	for i, s := range log {
		if s == want {
			return i
		}
	}
	return -1
}

func contains(log []string, want string) bool { return indexIn(log, want) >= 0 }

// ── ported cases from test_preempt_controller.py ─────────────────────────────

func TestFreshInputSuspendsBeforeEmit(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	state := h.controller.OnPoll(0.0, 0.0)

	if state != protocol.AgentStateActive {
		t.Fatalf("state = %v, want active", state)
	}
	if indexIn(*h.log, "suspend_all") >= indexIn(*h.log, "emit:user_returned") {
		t.Errorf("suspend must precede emit; log=%v", *h.log)
	}
	if h.sink.events[0].Kind != protocol.EventKindUserReturned {
		t.Errorf("first event = %v", h.sink.events[0].Kind)
	}
	if h.sink.events[0].AgentID != "agent-1" {
		t.Errorf("agent id = %q", h.sink.events[0].AgentID)
	}
}

func TestYieldMSMeasuredFromInjectedMonotonic(t *testing.T) {
	h := makeHarness(nil, &fakeClock{T: 0.05}, nil)

	h.controller.OnPoll(0.0, 0.0)

	if got := h.sink.events[0].Detail[yieldMSKey]; got != "50.000" {
		t.Errorf("yield_ms = %q, want 50.000", got)
	}
}

func TestStaleIdleOnStartupStaysIdle(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	if state := h.controller.OnPoll(5.0, 0.0); state != protocol.AgentStateIdle {
		t.Fatalf("state = %v, want idle", state)
	}
	if contains(*h.log, "suspend_all") {
		t.Errorf("must not suspend on stale startup idle; log=%v", *h.log)
	}
}

func TestFreshInputViaIdleDrop(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	h.controller.OnPoll(5.0, 0.0)
	h.controller.OnPoll(5.1, 0.1) // still climbing -> Idle
	if h.controller.State() != protocol.AgentStateIdle {
		t.Fatalf("expected idle before drop")
	}
	if state := h.controller.OnPoll(2.0, 0.2); state != protocol.AgentStateActive {
		t.Fatalf("counter reset should trigger active; got %v", state)
	}
}

func TestHysteresisBriefPauseDoesNotResume(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	h.controller.OnPoll(0.0, 0.0) // user returns -> Active
	if state := h.controller.OnPoll(1.0, 1.0); state != protocol.AgentStateActive {
		t.Fatalf("brief pause should stay active; got %v", state)
	}
	if contains(*h.log, "resume_all") {
		t.Errorf("must not resume after brief pause")
	}
	if len(h.supervisor.stopped) != 0 {
		t.Errorf("nothing should be stopped; got %v", h.supervisor.stopped)
	}

	state := h.controller.OnPoll(120.0, 120.0)
	if state != protocol.AgentStateIdle {
		t.Fatalf("continuous idle past threshold should resume; got %v", state)
	}
	if !contains(*h.log, "resume_all") {
		t.Errorf("resume_all missing; log=%v", *h.log)
	}
	if last := (*h.log)[len(*h.log)-1]; last != "emit:user_idle" {
		t.Errorf("last log = %q, want emit:user_idle", last)
	}
}

func TestEscalationKillsOnlySuspendedGPUReplicasAfterEvictDelay(t *testing.T) {
	statuses := []protocol.ReplicaStatus{
		suspended("a", 8001, true),
		suspended("c", 8003, true),
		suspended("cpu-only", 8004, false), // suspended CPU replica survives
		stoppedStatus("b", 8002),
	}
	h := makeHarness(statuses, nil, nil)

	h.controller.OnPoll(0.0, 0.0)  // Active at t=0
	h.controller.OnPoll(1.0, 59.0) // below evict delay
	if len(h.supervisor.stopped) != 0 {
		t.Fatalf("no eviction before delay; got %v", h.supervisor.stopped)
	}

	h.controller.OnPoll(1.0, 61.0) // past evict delay
	if got := h.supervisor.stopped; !equal(got, []string{"a", "c"}) {
		t.Fatalf("stopped = %v, want [a c]", got)
	}

	h.controller.OnPoll(1.0, 62.0) // idempotent: no re-kill
	if got := h.supervisor.stopped; !equal(got, []string{"a", "c"}) {
		t.Fatalf("stopped changed after escalation: %v", got)
	}
}

func TestDrainingIsTerminalAndIgnoresTransitions(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	h.controller.Drain()
	if h.controller.State() != protocol.AgentStateDraining {
		t.Fatalf("state = %v, want draining", h.controller.State())
	}
	if last := h.sink.events[len(h.sink.events)-1].Kind; last != protocol.EventKindAgentStopping {
		t.Errorf("last event = %v", last)
	}

	baseline := append([]string(nil), *h.log...)
	if state := h.controller.OnPoll(0.0, 0.0); state != protocol.AgentStateDraining {
		t.Fatalf("draining must ignore fresh input; got %v", state)
	}
	if !equal(*h.log, baseline) {
		t.Errorf("log changed after drain: %v vs %v", *h.log, baseline)
	}
	if contains(*h.log, "suspend_all") {
		t.Errorf("must not suspend after drain")
	}
}

func TestDrainIsIdempotent(t *testing.T) {
	h := makeHarness(nil, nil, nil)

	h.controller.Drain()
	h.controller.Drain()

	stopping := 0
	for _, e := range h.sink.events {
		if e.Kind == protocol.EventKindAgentStopping {
			stopping++
		}
	}
	if stopping != 1 {
		t.Errorf("agent_stopping emitted %d times, want 1", stopping)
	}
}

func TestFreshInputBoundaryWithinPollInterval(t *testing.T) {
	// poll_interval_s = 0.1; idle below it counts as fresh input.
	for _, idleS := range []float64{0.0, 0.05} {
		h := makeHarness(nil, nil, nil)
		if state := h.controller.OnPoll(idleS, 0.0); state != protocol.AgentStateActive {
			t.Errorf("idle_s=%v: state = %v, want active", idleS, state)
		}
	}
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
