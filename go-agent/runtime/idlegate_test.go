package runtime

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/Unluckyathecking/fallow/go-agent/idle"
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
