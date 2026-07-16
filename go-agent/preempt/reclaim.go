package preempt

// Reclaim is the user-triggered takedown: the sticky "this is my machine now"
// override. Automatic preemption (Controller) reacts to *detected* activity and
// resumes on its own once the machine looks idle again. Reclaim is the *explicit*
// override a person uses to reclaim their machine: it reuses the same
// suspend-first hot path for instant relief, then stops the replicas to free
// RAM/VRAM, and stays down until an explicit release regardless of idle
// detection. That stickiness is the whole point — the user's own work is never
// disrupted by fallow deciding the machine looks idle again.
//
// Control channel: a single flag file under the agent state directory (see ADR
// 042). Its presence means reclaimed, its absence means released. The daemon's
// poll loop checks it each tick via ReclaimController; the reclaim and release
// subcommands write and remove it. A local file is the simplest cross-platform
// mechanism (Windows has no POSIX signals) and is never reachable off-host. This
// is a 1:1 port of fallow_agent.preempt.reclaim.

import (
	"os"
	"path/filepath"
	"sync"
)

// ReclaimFilename is the control file whose presence means "reclaimed". It lives
// beside the agent state file.
const ReclaimFilename = "reclaim.flag"

// StopRunner dispatches the slow stop-replicas step off the hot path. Production
// spawns a goroutine so the poll tick returns immediately after the suspend;
// tests inject a synchronous runner to assert suspend-then-stop ordering.
type StopRunner func(func())

// ReclaimControlPath returns the reclaim flag file for a given state file, in the
// state file's own directory (mirrors the Python reclaim_control_path).
func ReclaimControlPath(statePath string) string {
	return filepath.Join(filepath.Dir(statePath), ReclaimFilename)
}

// RequestReclaim asks the running daemon to reclaim the machine by creating the
// flag file. It returns the flag path.
func RequestReclaim(statePath string) (string, error) {
	path := ReclaimControlPath(statePath)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return path, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return path, err
	}
	return path, f.Close()
}

// RequestRelease asks the running daemon to release the machine by removing the
// flag file. Removing an absent flag is a no-op, not an error.
func RequestRelease(statePath string) (string, error) {
	path := ReclaimControlPath(statePath)
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return path, err
	}
	return path, nil
}

func spawnStop(fn func()) { go fn() }

// ReclaimOptions configures optional ReclaimController collaborators. The zero
// value takes production defaults.
type ReclaimOptions struct {
	// StopRunner dispatches the stop-replicas step. Nil spawns a goroutine.
	StopRunner StopRunner
}

// ReclaimController drives the sticky reclaimed override from the poll thread.
// Each tick the poll loop calls OnPoll. On the rising edge (flag file appeared)
// it suspends every replica immediately — the same hot-path call automatic
// preemption uses — then stops them off the hot path to free memory. On the
// falling edge (flag removed) it clears the state; normal idle-based serving
// resumes.
type ReclaimController struct {
	supervisor  ProcessSupervisor
	controlFile string
	stopRunner  StopRunner

	mu        sync.Mutex
	reclaimed bool
}

// NewReclaimController builds a ReclaimController watching controlFile. A nil
// StopRunner falls back to spawning a goroutine.
func NewReclaimController(supervisor ProcessSupervisor, controlFile string, opts ReclaimOptions) *ReclaimController {
	stopRunner := opts.StopRunner
	if stopRunner == nil {
		stopRunner = spawnStop
	}
	return &ReclaimController{
		supervisor:  supervisor,
		controlFile: controlFile,
		stopRunner:  stopRunner,
	}
}

// IsReclaimed reports whether the machine is currently reclaimed (thread-safe).
func (c *ReclaimController) IsReclaimed() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.reclaimed
}

// OnPoll advances the reclaim state one tick and returns whether the machine is
// now reclaimed. Returning true tells the poll loop to skip automatic
// preemption: the machine belongs to the user and must not resume serving.
func (c *ReclaimController) OnPoll() bool {
	wantsReclaim := c.controlFilePresent()
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case wantsReclaim && !c.reclaimed:
		c.enterReclaimed()
	case !wantsReclaim && c.reclaimed:
		c.reclaimed = false
	}
	return c.reclaimed
}

func (c *ReclaimController) controlFilePresent() bool {
	_, err := os.Stat(c.controlFile)
	return err == nil
}

// enterReclaimed runs under c.mu.
func (c *ReclaimController) enterReclaimed() {
	// HOT PATH: suspend first for instant relief, then set state.
	c.supervisor.SuspendAll()
	c.reclaimed = true
	// Stopping waits on process exit, so it must not run on the poll thread. A
	// reclaim immediately followed by a release can race this background stop
	// into killing a replica that has just been relaunched; that is benign, the
	// next reconcile tick sees it stopped-and-desired and starts it again.
	c.stopRunner(c.stopAllReplicas)
}

func (c *ReclaimController) stopAllReplicas() {
	for _, status := range c.supervisor.Statuses() {
		c.supervisor.StopReplica(status.ModelID)
	}
}
