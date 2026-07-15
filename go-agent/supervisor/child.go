package supervisor

import (
	"os/exec"
	"sync"
	"time"
)

// SpawnFunc launches the given argv and returns the started command. It is the
// process-spawn seam: the default spawns a real OS process with detached stdio,
// and tests can substitute their own. The argv is always built by a
// CommandFactory (never a shell string).
type SpawnFunc func(argv []string) (*exec.Cmd, error)

// defaultSpawn starts a child process with no shell and discarded stdio.
//
// stdout/stderr/stdin are left nil so os/exec connects them to the null device:
// llama-server is chatty and the supervisor tracks liveness through the process
// handle and /health, not through its logs.
func defaultSpawn(argv []string) (*exec.Cmd, error) {
	cmd := exec.Command(argv[0], argv[1:]...) //nolint:gosec // argv is factory-built, never a shell string
	// Nil Stdin/Stdout/Stderr => connected to the null device by os/exec.
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// child is the handle the supervisor keeps for one live replica: identity, the
// OS process handle, and the private channels its health goroutine watches.
// Mutable lifecycle state (LOADING/READY/…) lives in the supervisor, guarded by
// its lock, so this record never changes after construction.
type child struct {
	modelID          string
	port             int
	cmd              *exec.Cmd
	pid              int
	startedMonotonic time.Time

	// shutdown is closed once to tell the health goroutine to exit promptly.
	shutdown     chan struct{}
	shutdownOnce sync.Once

	// exited is closed by the reaper goroutine once the process has been
	// waited on (reaped). Checking it is the non-blocking "has it died?" probe,
	// the Go analogue of subprocess.Popen.poll().
	exited chan struct{}
}

// newChild wraps a started command and launches the reaper goroutine that waits
// on (and thereby reaps) the process, closing exited when it terminates.
func newChild(modelID string, port int, cmd *exec.Cmd, startedMonotonic time.Time) *child {
	c := &child{
		modelID:          modelID,
		port:             port,
		cmd:              cmd,
		pid:              cmd.Process.Pid,
		startedMonotonic: startedMonotonic,
		shutdown:         make(chan struct{}),
		exited:           make(chan struct{}),
	}
	go func() {
		_ = cmd.Wait() // reap the process; exit status is not used
		close(c.exited)
	}()
	return c
}

// signalShutdown closes the shutdown channel exactly once.
func (c *child) signalShutdown() {
	c.shutdownOnce.Do(func() { close(c.shutdown) })
}

// hasExited reports whether the process has already been reaped, without
// blocking. It is the analogue of Popen.poll() returning non-None.
func (c *child) hasExited() bool {
	select {
	case <-c.exited:
		return true
	default:
		return false
	}
}

// waitExited blocks until the process is reaped or the timeout elapses. It
// returns true if the process exited within the timeout.
func (c *child) waitExited(timeout time.Duration) bool {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-c.exited:
		return true
	case <-timer.C:
		return false
	}
}
