package supervisor

import (
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// Lifecycle tests. Ported 1:1 from test_a3_supervisor.py.

func TestStartReportsLoadingThenReady(t *testing.T) {
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/models/tiny.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	statuses := s.Statuses()
	if len(statuses) != 1 {
		t.Fatalf("statuses len = %d, want 1", len(statuses))
	}
	if statuses[0].Port != 8080 {
		t.Fatalf("port = %d, want 8080", statuses[0].Port)
	}
	if statuses[0].Inflight != 0 {
		t.Fatalf("inflight = %d, want 0", statuses[0].Inflight)
	}
}

func TestDuplicateStartIsIgnored(t *testing.T) {
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 9999); err != nil { // ignored
		t.Fatal(err)
	}
	statuses := s.Statuses()
	if len(statuses) != 1 {
		t.Fatalf("statuses len = %d, want 1", len(statuses))
	}
	if statuses[0].Port != 8080 {
		t.Fatalf("port = %d, want 8080 (second start must be ignored)", statuses[0].Port)
	}
}

func TestStopReplicaKillsWithinGrace(t *testing.T) {
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	pid, ok := childPID(s, "tiny")
	if !ok {
		t.Fatal("no child pid")
	}
	started := time.Now()
	s.StopReplica("tiny")
	if elapsed := time.Since(started); elapsed >= deadline+time.Second {
		t.Fatalf("stop took %v, want < %v", elapsed, deadline+time.Second)
	}
	if st, _ := stateOf(s, "tiny"); st != protocol.ReplicaStateStopped {
		t.Fatalf("state = %q, want stopped", st)
	}
	if !waitFor(func() bool { return !pidAlive(pid) }, deadline) {
		t.Fatal("process still alive after stop")
	}
}

func TestSuspendAndResumeChangeState(t *testing.T) {
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	pid, _ := childPID(s, "tiny")

	s.SuspendAll()
	if st, _ := stateOf(s, "tiny"); st != protocol.ReplicaStateSuspended {
		t.Fatalf("state = %q, want suspended", st)
	}
	if runtime.GOOS == "linux" {
		if !waitFor(func() bool { return procState(pid) == 'T' }, deadline) {
			t.Fatalf("process not stopped after suspend (state %c)", procState(pid))
		}
	}

	s.ResumeAll()
	if st, _ := stateOf(s, "tiny"); st != protocol.ReplicaStateReady {
		t.Fatalf("state = %q, want ready", st)
	}
	if runtime.GOOS == "linux" {
		if !waitFor(func() bool { return procState(pid) != 'T' }, deadline) {
			t.Fatal("process still stopped after resume")
		}
	}
}

func TestSuspendAllIsFast(t *testing.T) {
	s := newSupervisor(t)
	for i := 0; i < 3; i++ {
		id := "m" + string(rune('0'+i))
		if err := s.StartReplica(manifest(id, 0), "/m.gguf", 8080+i); err != nil {
			t.Fatal(err)
		}
	}
	if !waitFor(func() bool {
		statuses := s.Statuses()
		if len(statuses) != 3 {
			return false
		}
		for _, st := range statuses {
			if st.State != protocol.ReplicaStateReady {
				return false
			}
		}
		return true
	}, deadline) {
		t.Fatal("not all replicas became READY")
	}
	started := time.Now()
	s.SuspendAll()
	if elapsed := time.Since(started); elapsed >= 50*time.Millisecond {
		t.Fatalf("suspend_all took %v, want < 50ms", elapsed)
	}
}

func TestSuspendAllWithVanishedProcessDoesNotPanic(t *testing.T) {
	cfg := fastConfig()
	cfg.HealthPollInterval = 30 * time.Second // keep the watch loop from reaping first
	s, err := New(cfg, sleeperCommand(t), WithHealthCheck(alwaysHealthy))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.StopAll)

	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	pid, _ := childPID(s, "tiny")
	if p, e := os.FindProcess(pid); e == nil {
		_ = p.Kill()
	}
	// Wait until the process is fully reaped so the signal path sees it vanished.
	if !waitFor(func() bool {
		s.mu.Lock()
		c := s.children["tiny"]
		s.mu.Unlock()
		return c != nil && c.hasExited()
	}, deadline) {
		t.Fatal("child never reaped")
	}

	s.SuspendAll() // must not panic despite the vanished process
	if st, _ := stateOf(s, "tiny"); st != protocol.ReplicaStateStopped {
		t.Fatalf("state = %q, want stopped", st)
	}
}

func TestCrashedChildBecomesStopped(t *testing.T) {
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	pid, _ := childPID(s, "tiny")
	if p, e := os.FindProcess(pid); e == nil { // external kill; the watch loop must notice
		_ = p.Kill()
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateStopped }, deadline) {
		t.Fatal("crashed replica never became STOPPED")
	}
}

func TestStartupTimeoutMarksStopped(t *testing.T) {
	cfg := fastConfig()
	cfg.StartupTimeout = 50 * time.Millisecond
	s, err := New(cfg, sleeperCommand(t), WithHealthCheck(neverHealthy))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.StopAll)

	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateStopped }, deadline) {
		t.Fatal("replica never timed out to STOPPED")
	}
}

func TestStopAllStopsEveryReplica(t *testing.T) {
	s := newSupervisor(t)
	for i := 0; i < 2; i++ {
		id := "m" + string(rune('0'+i))
		if err := s.StartReplica(manifest(id, 0), "/m.gguf", 8080+i); err != nil {
			t.Fatal(err)
		}
	}
	if !waitFor(func() bool {
		statuses := s.Statuses()
		if len(statuses) != 2 {
			return false
		}
		for _, st := range statuses {
			if st.State != protocol.ReplicaStateReady {
				return false
			}
		}
		return true
	}, deadline) {
		t.Fatal("not all replicas became READY")
	}
	s.StopAll()
	for _, st := range s.Statuses() {
		if st.State != protocol.ReplicaStateStopped {
			t.Fatalf("replica %s state = %q, want stopped", st.ModelID, st.State)
		}
	}
}

func TestNoHealthGoroutinesLeftAfterStop(t *testing.T) {
	baseline := runtime.NumGoroutine()
	s := newSupervisor(t)
	if err := s.StartReplica(manifest("tiny", 0), "/m.gguf", 8080); err != nil {
		t.Fatal(err)
	}
	if !waitFor(func() bool { st, _ := stateOf(s, "tiny"); return st == protocol.ReplicaStateReady }, deadline) {
		t.Fatal("replica never became READY")
	}
	s.StopReplica("tiny") // joins the health goroutine before returning
	// The reaper goroutine and health goroutine must both be gone.
	if !waitFor(func() bool { return runtime.NumGoroutine() <= baseline+1 }, deadline) {
		t.Fatalf("goroutines leaked: baseline %d, now %d", baseline, runtime.NumGoroutine())
	}
}

// pidAlive reports whether a process with pid is still alive (signal 0 probe).
func pidAlive(pid int) bool {
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}

// procState reads the Linux /proc state character for pid ('T' == stopped), or
// 0 if unavailable. Only meaningful on Linux.
func procState(pid int) byte {
	data, err := os.ReadFile("/proc/" + itoa(pid) + "/stat")
	if err != nil {
		return 0
	}
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return 0
	}
	fields := strings.Fields(s[idx+2:])
	if len(fields) == 0 {
		return 0
	}
	return fields[0][0]
}

func itoa(n int) string {
	return strconv.Itoa(n)
}
