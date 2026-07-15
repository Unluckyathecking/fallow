// Package idle answers exactly one question — "how many seconds since the user
// last touched this machine, right now?" — cheaply and without blocking or
// spawning a process. Polling and the yield/resume state machine live in the
// preempt package, not here.
//
// The concrete detector is chosen at compile time by build tags (see the
// detector_<goos>.go files) and constructed by CreateDetector. macOS uses a
// cgo call to CGEventSourceSecondsSinceLastEventType; Windows uses
// GetLastInputInfo via golang.org/x/sys/windows with unsigned wraparound
// arithmetic; Linux is an honest stub that reports unsupported (mirroring the
// Python fallow_agent.idle.linux semantics). FakeDetector is a deterministic,
// thread-safe implementation for tests.
package idle

import (
	"errors"
	"math"
)

// Detector reports seconds since the last user input. Implementations return a
// non-nil error on platforms where idle detection is unsupported, mirroring the
// Python detectors that raise NotImplementedError.
type Detector interface {
	SecondsSinceInput() (float64, error)
}

// ErrUnsupported is returned by detectors on platforms with no implementation.
var ErrUnsupported = errors.New("idle detection is not supported on this platform")

// CreateDetector returns the Detector for the current OS.
//
// benchEnabled and forceIdle drive the headless bench: forceIdle requires
// benchEnabled and yields a ConstantDetector reporting the largest finite idle
// duration, so an ordinary agent can never accidentally select it.
func CreateDetector(benchEnabled, forceIdle bool) (Detector, error) {
	if forceIdle {
		if !benchEnabled {
			return nil, errors.New("force_idle requires bench mode")
		}
		return ConstantDetector{}, nil
	}
	return newPlatformDetector()
}

// ConstantDetector reports the largest finite idle duration for a headless
// bench agent (mirrors the Python ConstantIdleDetector).
type ConstantDetector struct{}

// SecondsSinceInput returns the largest finite float64.
func (ConstantDetector) SecondsSinceInput() (float64, error) {
	return math.MaxFloat64, nil
}
