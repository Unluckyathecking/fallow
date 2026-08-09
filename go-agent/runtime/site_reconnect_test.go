package runtime

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// superviseClaimRunner must return promptly when the context is cancelled while
// the wrapped runner keeps failing — no leaked goroutine, cancellation terminal.
func TestSuperviseClaimRunnerCancellationIsTerminal(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var calls atomic.Int64
	done := make(chan struct{})
	go func() {
		superviseClaimRunner(ctx, func(context.Context) error {
			calls.Add(1)
			return errors.New("transient relay error")
		})
		close(done)
	}()
	// Let it fail and enter its first backoff, then cancel.
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("superviseClaimRunner did not return promptly after cancellation")
	}
	if calls.Load() == 0 {
		t.Fatal("runner was never invoked")
	}
}

// A persistently failing runner must be rate-limited by the backoff rather than
// hot-looped: only a handful of attempts in a short window.
func TestSuperviseClaimRunnerBacksOffAndDoesNotHotLoop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var calls atomic.Int64
	done := make(chan struct{})
	go func() {
		superviseClaimRunner(ctx, func(context.Context) error {
			calls.Add(1)
			return errors.New("transient relay error")
		})
		close(done)
	}()
	// Over ~450ms with a 200ms initial backoff, expect only the first immediate
	// attempt plus roughly two more — never a hot loop of thousands.
	time.Sleep(450 * time.Millisecond)
	cancel()
	<-done
	if n := calls.Load(); n > 6 {
		t.Fatalf("runner hot-looped: %d attempts in ~450ms", n)
	}
	if calls.Load() < 2 {
		t.Fatalf("runner did not retry after a transient error: %d attempts", calls.Load())
	}
}

// A runner that recovers (returns nil once the coordinator is back) stops the
// supervisor cleanly without spinning.
func TestSuperviseClaimRunnerStopsWhenRunnerRecoversClean(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var calls atomic.Int64
	done := make(chan struct{})
	go func() {
		superviseClaimRunner(ctx, func(context.Context) error {
			if calls.Add(1) == 1 {
				return errors.New("transient relay error")
			}
			return nil // recovered: clean return ends supervision
		})
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("supervisor did not stop after a clean runner return")
	}
	if calls.Load() != 2 {
		t.Fatalf("expected exactly one retry then a clean stop, got %d calls", calls.Load())
	}
}
