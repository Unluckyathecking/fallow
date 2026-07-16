package preempt

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// The preemption latency budget the suspend must clear (the issue: p99 well
// under 300ms). The suspend is the same hot-path call automatic preemption uses.
const reclaimBudget = 300 * time.Millisecond

// syncStopRunner runs the stop step inline so suspend-then-stop order is
// observable in the shared call log.
func syncStopRunner(fn func()) { fn() }

func makeReclaim(flag string, statuses []protocol.ReplicaStatus) (*ReclaimController, *[]string, *recordingSupervisor) {
	log := &[]string{}
	sup := &recordingSupervisor{log: log, statuses: statuses}
	c := NewReclaimController(sup, flag, ReclaimOptions{StopRunner: syncStopRunner})
	return c, log, sup
}

func touch(t *testing.T, path string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("touch %s: %v", path, err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("close %s: %v", path, err)
	}
}

// ── ported cases from test_preempt_reclaim.py ────────────────────────────────

func TestReclaimSuspendsImmediatelyThenStops(t *testing.T) {
	flag := filepath.Join(t.TempDir(), ReclaimFilename)
	statuses := []protocol.ReplicaStatus{suspended("m1", 8100, false), suspended("m2", 8101, false)}
	c, log, _ := makeReclaim(flag, statuses)

	// Idle: no flag, nothing happens.
	if c.OnPoll() {
		t.Fatal("no flag: must not be reclaimed")
	}
	if len(*log) != 0 {
		t.Fatalf("no flag: expected no calls, got %v", *log)
	}

	// User reclaims: flag appears -> suspend-all first, then stop every replica.
	touch(t, flag)
	if !c.OnPoll() {
		t.Fatal("flag present: must be reclaimed")
	}
	if !c.IsReclaimed() {
		t.Fatal("IsReclaimed should be true")
	}
	want := []string{"suspend_all", "stop_replica:m1", "stop_replica:m2"}
	if !equal(*log, want) {
		t.Fatalf("log = %v, want %v", *log, want)
	}
}

func TestReclaimIsStickyUntilRelease(t *testing.T) {
	flag := filepath.Join(t.TempDir(), ReclaimFilename)
	c, log, _ := makeReclaim(flag, nil)
	touch(t, flag)
	c.OnPoll()
	*log = (*log)[:0]

	// Still reclaimed on later ticks; the transition work does not repeat.
	if !c.OnPoll() {
		t.Fatal("still reclaimed on later ticks")
	}
	if len(*log) != 0 {
		t.Fatalf("transition must not repeat, got %v", *log)
	}

	// Release: flag removed -> normal serving restored on the next tick.
	if err := os.Remove(flag); err != nil {
		t.Fatal(err)
	}
	if c.OnPoll() {
		t.Fatal("released: must not be reclaimed")
	}
	if c.IsReclaimed() {
		t.Fatal("IsReclaimed should be false after release")
	}
}

func TestReclaimSuspendClearsLatencyBudget(t *testing.T) {
	flag := filepath.Join(t.TempDir(), ReclaimFilename)
	c, log, _ := makeReclaim(flag, nil) // sync runner, empty statuses
	touch(t, flag)

	start := time.Now()
	if !c.OnPoll() {
		t.Fatal("expected reclaimed")
	}
	elapsed := time.Since(start)

	if len(*log) == 0 || (*log)[0] != "suspend_all" {
		t.Fatalf("suspend_all must be the first side effect, log=%v", *log)
	}
	if elapsed > reclaimBudget {
		t.Fatalf("OnPoll took %v, over the %v budget", elapsed, reclaimBudget)
	}
}

func TestRequestReclaimAndReleaseToggleTheFlag(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "state", "agent-state.json")

	path, err := RequestReclaim(statePath)
	if err != nil {
		t.Fatal(err)
	}
	if path != ReclaimControlPath(statePath) {
		t.Fatalf("path = %q, want %q", path, ReclaimControlPath(statePath))
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("flag not created: %v", err)
	}

	if _, err := RequestRelease(statePath); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("flag not removed: stat err = %v", err)
	}
	// Releasing when already released is a no-op, not an error.
	if _, err := RequestRelease(statePath); err != nil {
		t.Fatalf("second release errored: %v", err)
	}
}
