package idle

import (
	"errors"
	"sync"
)

// FakeDetector is a settable, thread-safe Detector for unit tests and the bench
// churn injector. The poll thread reads while a test/bench thread mutates the
// value, so the stored idle value is guarded by a mutex and never tears.
//
// Invariant: the reported idle value is always >= 0.
type FakeDetector struct {
	mu    sync.Mutex
	idleS float64
}

var errNegativeIdle = errors.New("idle_s must be >= 0")

// NewFakeDetector builds a FakeDetector starting at idleS (must be >= 0).
func NewFakeDetector(idleS float64) (*FakeDetector, error) {
	if idleS < 0 {
		return nil, errNegativeIdle
	}
	return &FakeDetector{idleS: idleS}, nil
}

// SecondsSinceInput returns the current idle value.
func (f *FakeDetector) SecondsSinceInput() (float64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.idleS, nil
}

// SetIdle sets the reported idle value (must be >= 0).
func (f *FakeDetector) SetIdle(idleS float64) error {
	if idleS < 0 {
		return errNegativeIdle
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.idleS = idleS
	return nil
}

// Advance increases the idle value by deltaS (simulates time passing). The
// resulting value must remain >= 0.
func (f *FakeDetector) Advance(deltaS float64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	next := f.idleS + deltaS
	if next < 0 {
		return errNegativeIdle
	}
	f.idleS = next
	return nil
}

// SimulateInput resets idle to zero: the user touched the machine.
func (f *FakeDetector) SimulateInput() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.idleS = 0
}
