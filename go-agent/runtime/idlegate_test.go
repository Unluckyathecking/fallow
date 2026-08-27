package runtime

import (
	"context"
	"errors"
	"math"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// TestRunRefusesWithoutIdleDetection is the fail-closed guard: a build whose
// detector reports unsupported must not enroll and serve, because it would read
// as permanently idle and never yield the machine to its user.
func TestRunRefusesWithoutIdleDetection(t *testing.T) {
	fc := &fakeCoordinator{}
	rt := New(testSettings(t), seamsFor(fc, &fakeSupervisor{}, unsupportedDetector{}, newTickerFactory()))

	err := rt.Run(context.Background())
	if err == nil {
		t.Fatal("Run started with no idle detection")
	}
	if !strings.Contains(err.Error(), "assume_idle") {
		t.Errorf("Run error = %q, want the assume_idle override named", err)
	}
	fc.mu.Lock()
	defer fc.mu.Unlock()
	if fc.registers != 0 {
		t.Errorf("registered %d times before failing closed, want 0", fc.registers)
	}
}

// transientDetector fails the way a working platform detector does when one
// call does not come back: the build has idle detection, this sample failed.
type transientDetector struct{}

func (transientDetector) SecondsSinceInput() (float64, error) {
	return 0, errors.New("GetLastInputInfo failed")
}

// TestRunRefusesOnATransientProbeError pins that a failing probe is refused for
// what it is: the daemon still will not start, but it must not tell the operator
// their build has no idle detection when it does.
func TestRunRefusesOnATransientProbeError(t *testing.T) {
	rt := New(testSettings(t), Seams{Detector: transientDetector{}})

	err := rt.checkIdleDetection()
	if err == nil {
		t.Fatal("Run started on a detector that could not answer")
	}
	if !strings.Contains(err.Error(), "GetLastInputInfo failed") {
		t.Errorf("error = %q, want the probe's own failure named", err)
	}
	if strings.Contains(err.Error(), "this build has no idle detection") {
		t.Errorf("error = %q, want it not to blame the build", err)
	}
}

// nonFiniteDetector answers without an error but with nothing usable, the way
// some OS APIs do off a GUI session.
type nonFiniteDetector struct{ value float64 }

func (d nonFiniteDetector) SecondsSinceInput() (float64, error) { return d.value, nil }

// TestRunRefusesOnANonFiniteReading pins the startup gate against a detector
// that reports success and hands back NaN or Inf. sampleIdle rejects those, so
// every later sample fails too: accepting one at startup would run a daemon that
// can never see its user.
func TestRunRefusesOnANonFiniteReading(t *testing.T) {
	for name, value := range map[string]float64{
		"nan":     math.NaN(),
		"inf":     math.Inf(1),
		"neg_inf": math.Inf(-1),
	} {
		t.Run(name, func(t *testing.T) {
			rt := New(testSettings(t), Seams{Detector: nonFiniteDetector{value: value}})

			err := rt.checkIdleDetection()
			if err == nil {
				t.Fatal("Run started on a detector that answered with a non-number")
			}
			if !strings.Contains(err.Error(), "not a number of seconds") {
				t.Errorf("error = %q, want the non-finite reading named", err)
			}
			if strings.Contains(err.Error(), "this build has no idle detection") {
				t.Errorf("error = %q, want it not to blame the build", err)
			}
		})
	}
}

// TestAssumeIdleAllowsANonFiniteReading keeps the one override intact: a machine
// nobody uses still starts, whatever its detector answers.
func TestAssumeIdleAllowsANonFiniteReading(t *testing.T) {
	settings := testSettings(t)
	settings.AssumeIdle = true
	rt := New(settings, Seams{Detector: nonFiniteDetector{value: math.NaN()}})

	if err := rt.checkIdleDetection(); err != nil {
		t.Fatalf("assume_idle refused to start on a non-finite reading: %v", err)
	}
}

// TestIdleForPreemptTreatsAFailedSampleAsInput is the unit half of the
// serving-blind fix: the preempt loop must advance the controller with zero on a
// failed sample, because the controller — not the heartbeat's user_idle_s — is
// what suspends replicas, cancels claims and moves State off IDLE.
func TestIdleForPreemptTreatsAFailedSampleAsInput(t *testing.T) {
	rt := New(testSettings(t), Seams{Detector: transientDetector{}})
	idleS, ok := rt.idleForPreempt()
	if !ok {
		t.Fatal("idleForPreempt skipped the tick on a failed sample; the controller stays IDLE")
	}
	if idleS != 0 {
		t.Errorf("idleForPreempt = %v, want 0 (the user is at the keyboard)", idleS)
	}

	// assume_idle keeps its exemption: no detector by design, so no tick.
	settings := testSettings(t)
	settings.AssumeIdle = true
	assuming := New(settings, Seams{Detector: unsupportedDetector{}})
	if _, ok := assuming.idleForPreempt(); ok {
		t.Error("idleForPreempt advanced the controller under assume_idle; that machine must stay idle")
	}
}

// TestIdleSampleFailureReportsInUse is the runtime half: once running, a sample
// that stops answering must read as "someone is here", never as away. Away would
// let the coordinator schedule over the person at the keyboard.
func TestIdleSampleFailureReportsInUse(t *testing.T) {
	rt := New(testSettings(t), Seams{Detector: transientDetector{}})
	if got := rt.idleOrAway(); got != 0 {
		t.Errorf("idleOrAway = %v on a failed sample, want 0 (not idle)", got)
	}

	settings := testSettings(t)
	settings.AssumeIdle = true
	assuming := New(settings, Seams{Detector: transientDetector{}})
	if got := assuming.idleOrAway(); got != awayIdleS {
		t.Errorf("idleOrAway = %v under assume_idle, want the away value %v", got, awayIdleS)
	}
}

// flakyDetector answers normally until it is broken, then fails every call the
// way a working platform detector does when the OS stops answering.
type flakyDetector struct {
	mu     sync.Mutex
	idleS  float64
	broken bool
}

func (d *flakyDetector) SecondsSinceInput() (float64, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.broken {
		return 0, errors.New("GetLastInputInfo failed")
	}
	return d.idleS, nil
}

func (d *flakyDetector) set(idleS float64, broken bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.idleS, d.broken = idleS, broken
}

// lastHeartbeatState returns the state of the most recent heartbeat, so a test
// can tell "was ACTIVE at some point" from "is IDLE again now".
func (f *fakeCoordinator) lastHeartbeatState() (protocol.AgentState, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.heartbeats) == 0 {
		return "", false
	}
	return f.heartbeats[len(f.heartbeats)-1].State, true
}

// TestDetectorFailureWhileServingYieldsTheMachine is the end-to-end guard for
// the serving-blind path: an agent that is IDLE and serving, whose detector then
// stops answering, must actually yield — replicas suspended and the heartbeat's
// State off IDLE, which is what the coordinator schedules on. Reporting
// user_idle_s = 0 while State stayed IDLE left the machine serving over its
// user. Recovery is the other half: once samples return, ordinary idle-based
// serving resumes with no intervention.
func TestDetectorFailureWhileServingYieldsTheMachine(t *testing.T) {
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
	det := &flakyDetector{idleS: 200} // starts idle, so the startup gate passes
	tf := newTickerFactory()

	rt := New(settings, seamsFor(fc, fs, det, tf))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runErr := make(chan error, 1)
	go func() { runErr <- rt.Run(ctx) }()

	waitFor(t, "first heartbeat", func() bool { return fc.heartbeatCount() >= 1 })
	if state, _ := fc.lastHeartbeatState(); state != protocol.AgentStateIdle {
		t.Fatalf("first heartbeat state = %v, want idle before the detector breaks", state)
	}

	// The detector stops answering while the machine is idle and serving.
	det.set(0, true)
	pt := tf.get(t, 100*time.Millisecond)
	pt.fire()
	pt.fire() // the second tick guarantees the first OnPoll completed

	waitFor(t, "replicas suspended", func() bool { return fs.contains("suspend_all") })
	if got := rt.controller.State(); got == protocol.AgentStateIdle {
		t.Error("controller stayed IDLE on a blind detector; the machine keeps serving over its user")
	}
	if rt.servingEligible() {
		t.Error("servingEligible on a blind detector; reconciliation would start replicas")
	}
	ht := tf.get(t, 5*time.Second)
	ht.fire()
	waitFor(t, "a heartbeat off IDLE", func() bool {
		state, ok := fc.lastHeartbeatState()
		return ok && state != protocol.AgentStateIdle
	})

	// Samples return: the ordinary idle transition resumes serving.
	det.set(200, false)
	pt.fire()
	pt.fire()
	waitFor(t, "replicas resumed", func() bool { return fs.contains("resume_all") })
	ht.fire()
	waitFor(t, "a heartbeat back on IDLE", func() bool {
		state, ok := fc.lastHeartbeatState()
		return ok && state == protocol.AgentStateIdle
	})

	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run returned %v, want nil", err)
	}
}

// TestAssumeIdleAllowsUnsupportedDetector covers the one override: a machine
// nobody uses (a test harness, a dedicated headless host) may run without idle
// detection when its config says so.
func TestAssumeIdleAllowsUnsupportedDetector(t *testing.T) {
	settings := testSettings(t)
	settings.AssumeIdle = true
	rt := New(settings, Seams{Detector: unsupportedDetector{}})

	if err := rt.checkIdleDetection(); err != nil {
		t.Fatalf("assume_idle still refused to start: %v", err)
	}
}

// TestIdleDetectionAcceptsAWorkingDetector pins that the gate is about the
// detector, not the config: a supported platform needs no override.
func TestIdleDetectionAcceptsAWorkingDetector(t *testing.T) {
	det, err := idle.NewFakeDetector(12)
	if err != nil {
		t.Fatal(err)
	}
	rt := New(testSettings(t), Seams{Detector: det})
	if err := rt.checkIdleDetection(); err != nil {
		t.Fatalf("checkIdleDetection rejected a working detector: %v", err)
	}
}
