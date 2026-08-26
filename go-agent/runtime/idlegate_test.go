package runtime

import (
	"context"
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
