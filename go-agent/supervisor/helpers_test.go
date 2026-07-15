package supervisor

import (
	"os/exec"
	"strconv"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// Supervisor tests drive real tiny child processes (a `sleep` sleeper) through
// an injected CommandFactory, plus a fake health check so no HTTP happens.
// SIGSTOP via the default suspend seam works on macOS and Linux dev + CI, so
// the suspend/resume test inspects real process status where it can.

const (
	sleepSeconds = 60
	fastPoll     = 10 * time.Millisecond
	deadline     = 500 * time.Millisecond
)

// sleeperCommand returns a CommandFactory that launches a long-lived `sleep`
// process, standing in for a real replica. It skips the test if sleep is
// unavailable.
func sleeperCommand(t *testing.T) CommandFactory {
	t.Helper()
	bin, err := exec.LookPath("sleep")
	if err != nil {
		t.Skipf("sleep binary not available: %v", err)
	}
	return func(protocol.ModelManifest, string, int) []string {
		return []string{bin, strconv.Itoa(sleepSeconds)}
	}
}

func fastConfig() Config {
	cfg := DefaultConfig("/usr/bin/true")
	cfg.StartupTimeout = deadline
	cfg.HealthPollInterval = fastPoll
	cfg.StopGrace = deadline
	return cfg
}

func manifest(modelID string, minVRAMMB int) protocol.ModelManifest {
	return protocol.ModelManifest{
		ModelID:     modelID,
		Family:      "tiny",
		Quant:       "Q4_K_M",
		WorkerKind:  protocol.WorkerKindChat,
		FileName:    "tiny.gguf",
		SHA256:      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		SizeBytes:   1,
		MinVRAMMB:   minVRAMMB,
		DefaultArgs: []string{"--extra", "1"},
	}
}

func alwaysHealthy(string, int, string, time.Duration) bool { return true }
func neverHealthy(string, int, string, time.Duration) bool  { return false }

func waitFor(predicate func() bool, timeout time.Duration) bool {
	deadlineAt := time.Now().Add(timeout)
	for time.Now().Before(deadlineAt) {
		if predicate() {
			return true
		}
		time.Sleep(fastPoll)
	}
	return predicate()
}

func stateOf(s *Supervisor, modelID string) (protocol.ReplicaState, bool) {
	for _, status := range s.Statuses() {
		if status.ModelID == modelID {
			return status.State, true
		}
	}
	return "", false
}

func childPID(s *Supervisor, modelID string) (int, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.children[modelID]
	if !ok {
		return 0, false
	}
	return c.pid, true
}

// newSupervisor builds a supervisor with the fast config, a sleeper command, and
// an always-healthy probe, and registers cleanup that stops every replica.
func newSupervisor(t *testing.T) *Supervisor {
	t.Helper()
	s, err := New(fastConfig(), sleeperCommand(t), WithHealthCheck(alwaysHealthy))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.StopAll)
	return s
}
