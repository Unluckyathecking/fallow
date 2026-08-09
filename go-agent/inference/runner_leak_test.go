package inference

import (
	"context"
	"net/http"
	"net/http/httptest"
	"runtime"
	"testing"
	"time"
)

// TestNoGoroutineLeak runs many complete claims and asserts the goroutine
// population settles back to its baseline, catching leaked cancel watchers,
// request bodies or idle connections.
func TestNoGoroutineLeak(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	port := portOf(t, srv)

	runOnce := func() {
		changed := make(chan struct{})
		coord := &fakeCoord{}
		claim := claimFor(port, "/v1/chat/completions")
		admit := AvailabilitySnapshot{Ready: true, Changed: changed}
		_ = (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true, changed: changed}, readyPort(port), admit, claim)
		close(changed)
	}

	// Warm up so lazily-started runtime goroutines exist before the baseline.
	for i := 0; i < 5; i++ {
		runOnce()
	}
	settle()
	baseline := runtime.NumGoroutine()

	for i := 0; i < 50; i++ {
		runOnce()
	}

	if got := settledGoroutines(baseline); got > baseline+2 {
		t.Fatalf("goroutine leak: baseline=%d after=%d", baseline, got)
	}
}

func settle() {
	for i := 0; i < 5; i++ {
		runtime.GC()
		time.Sleep(20 * time.Millisecond)
	}
}

func settledGoroutines(baseline int) int {
	got := runtime.NumGoroutine()
	for i := 0; i < 50 && got > baseline+2; i++ {
		time.Sleep(20 * time.Millisecond)
		runtime.GC()
		got = runtime.NumGoroutine()
	}
	return got
}
