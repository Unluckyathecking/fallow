package supervisor

import (
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// suspendFn is the OS-specific suspend/resume seam. The default implementations
// live in the build-tagged suspend_unix.go and suspend_windows.go files; tests
// substitute a recorder.
type suspendFn func(pid int) error

// Supervisor is the concrete process supervisor over local OS processes. One
// replica per model_id; port allocation is the caller's responsibility. Clocks
// and I/O (spawn, health check, suspend/resume) are injected so lifecycle
// behaviour is deterministic in tests.
//
// A single mutex guards the state maps and the cached status slice only. Every
// blocking operation runs outside the lock, which is what keeps SuspendAll and
// ResumeAll fast on the preemption hot path.
type Supervisor struct {
	cfg     Config
	factory CommandFactory

	healthCheck HealthCheck
	now         func() time.Time
	spawn       SpawnFunc
	suspend     suspendFn
	resume      suspendFn

	mu         sync.Mutex
	children   map[string]*child
	healthDone map[string]chan struct{}
	states     map[string]protocol.ReplicaState
	ports      map[string]int
	gpu        map[string]bool
	preSuspend map[string]protocol.ReplicaState
	cached     []protocol.ReplicaStatus
}

// Option customises a Supervisor at construction. Every seam has a production
// default; options exist for deterministic testing.
type Option func(*Supervisor)

// WithHealthCheck overrides the readiness probe (default HTTPHealthCheck).
func WithHealthCheck(h HealthCheck) Option { return func(s *Supervisor) { s.healthCheck = h } }

// WithSpawn overrides the process spawner (default defaultSpawn).
func WithSpawn(spawn SpawnFunc) Option { return func(s *Supervisor) { s.spawn = spawn } }

// WithClock overrides the monotonic clock source (default time.Now).
func WithClock(now func() time.Time) Option { return func(s *Supervisor) { s.now = now } }

// WithSuspendResume overrides the OS suspend/resume seam. Tests use it to record
// calls without signalling a real process.
func WithSuspendResume(suspend, resume func(pid int) error) Option {
	return func(s *Supervisor) {
		s.suspend = suspend
		s.resume = resume
	}
}

// New constructs a Supervisor for cfg and factory. It returns an error if the
// configuration is invalid (see Config.Validate).
func New(cfg Config, factory CommandFactory, opts ...Option) (*Supervisor, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	s := &Supervisor{
		cfg:         cfg,
		factory:     factory,
		healthCheck: HTTPHealthCheck,
		now:         time.Now,
		spawn:       defaultSpawn,
		suspend:     suspendProcess,
		resume:      resumeProcess,
		children:    make(map[string]*child),
		healthDone:  make(map[string]chan struct{}),
		states:      make(map[string]protocol.ReplicaState),
		ports:       make(map[string]int),
		gpu:         make(map[string]bool),
		preSuspend:  make(map[string]protocol.ReplicaState),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s, nil
}

// ── Public API ───────────────────────────────────────────────────────────

// StartReplica launches a replica for the manifest at modelPath on port. A
// second start for a model_id that is already running is ignored. The returned
// error is only non-nil when the process fails to spawn.
func (s *Supervisor) StartReplica(manifest protocol.ModelManifest, modelPath string, port int) error {
	s.mu.Lock()
	_, running := s.children[manifest.ModelID]
	s.mu.Unlock()
	if running {
		return nil // already running; ignore start
	}

	argv := s.factory(manifest, modelPath, port)
	cmd, err := s.spawn(argv)
	if err != nil {
		return err
	}
	c := s.registerChild(manifest.ModelID, port, cmd, manifest.MinVRAMMB > 0)

	done := make(chan struct{})
	s.mu.Lock()
	s.healthDone[c.modelID] = done
	s.mu.Unlock()
	go s.healthLoop(c, done)
	return nil
}

// StopReplica stops one replica: it signals its health goroutine to exit,
// terminates the process (kill after the grace period), and joins the goroutine
// before returning. Unknown model_ids are a no-op.
func (s *Supervisor) StopReplica(modelID string) {
	s.mu.Lock()
	c := s.children[modelID]
	delete(s.children, modelID)
	done := s.healthDone[modelID]
	delete(s.healthDone, modelID)
	delete(s.preSuspend, modelID)
	s.setStateLocked(modelID, protocol.ReplicaStateStopped)
	s.mu.Unlock()

	if c == nil {
		return
	}
	c.signalShutdown()
	s.gracefulTerminate(c)
	if done != nil {
		timer := time.NewTimer(s.cfg.StopGrace + time.Second)
		select {
		case <-done:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// StopAll stops every running replica.
func (s *Supervisor) StopAll() {
	s.mu.Lock()
	ids := make([]string, 0, len(s.children))
	for id := range s.children {
		ids = append(ids, id)
	}
	s.mu.Unlock()
	for _, id := range ids {
		s.StopReplica(id)
	}
}

// SuspendAll suspends every live replica. It is the preemption hot path: it
// takes the lock only to snapshot children and commit states, runs the suspend
// syscalls in between, and never blocks, spawns, or touches the network.
// Suspending a vanished process never fails.
func (s *Supervisor) SuspendAll() { s.applySignal(true) }

// ResumeAll resumes every suspended replica, restoring each replica's
// pre-suspend state.
func (s *Supervisor) ResumeAll() { s.applySignal(false) }

// Statuses returns a snapshot of every known replica's status.
func (s *Supervisor) Statuses() []protocol.ReplicaStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]protocol.ReplicaStatus, len(s.cached))
	copy(out, s.cached)
	return out
}

// ── Registration ─────────────────────────────────────────────────────────

func (s *Supervisor) registerChild(modelID string, port int, cmd *exec.Cmd, gpu bool) *child {
	c := newChild(modelID, port, cmd, s.now())
	s.mu.Lock()
	s.children[modelID] = c
	s.ports[modelID] = port
	s.gpu[modelID] = gpu
	s.setStateLocked(modelID, protocol.ReplicaStateLoading)
	s.mu.Unlock()
	return c
}

// ── Health / crash-detection goroutine ─────────────────────────────────────

func (s *Supervisor) healthLoop(c *child, done chan struct{}) {
	defer close(done)
	if s.awaitReady(c) {
		s.watchUntilExit(c)
	}
}

// awaitReady polls until the replica reports healthy (READY) or fails: the
// process dies during startup, or the startup timeout elapses. It returns true
// only when the replica became READY.
func (s *Supervisor) awaitReady(c *child) bool {
	deadline := c.startedMonotonic.Add(s.cfg.StartupTimeout)
	for {
		select {
		case <-c.shutdown:
			return false
		default:
		}
		if c.hasExited() {
			s.markCrashed(c, "process exited during startup")
			return false
		}
		if s.probe(c) {
			s.markReady(c.modelID)
			return true
		}
		if !s.now().Before(deadline) {
			s.onStartupTimeout(c)
			return false
		}
		if s.sleepOrShutdown(c) {
			return false
		}
	}
}

// watchUntilExit polls a READY replica until it dies unexpectedly (STOPPED) or
// the supervisor tells the goroutine to shut down.
func (s *Supervisor) watchUntilExit(c *child) {
	for {
		select {
		case <-c.shutdown:
			return
		default:
		}
		if c.hasExited() {
			s.markCrashed(c, "process exited unexpectedly")
			return
		}
		if s.sleepOrShutdown(c) {
			return
		}
	}
}

// sleepOrShutdown waits one poll interval, returning true if a shutdown was
// requested in the meantime (the interruptible-sleep analogue of Python's
// Event.wait).
func (s *Supervisor) sleepOrShutdown(c *child) bool {
	timer := time.NewTimer(s.cfg.HealthPollInterval)
	defer timer.Stop()
	select {
	case <-c.shutdown:
		return true
	case <-timer.C:
		return false
	}
}

func (s *Supervisor) probe(c *child) bool {
	return s.healthCheck(s.cfg.BindHost, c.port, s.cfg.HealthPath, s.cfg.HealthTimeout)
}

func (s *Supervisor) onStartupTimeout(c *child) {
	s.forceKill(c)
	s.markCrashed(c, "startup timeout")
}

// ── State transitions (all mutate under the lock) ──────────────────────────

func (s *Supervisor) markReady(modelID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.states[modelID] == protocol.ReplicaStateLoading {
		s.setStateLocked(modelID, protocol.ReplicaStateReady)
	}
}

func (s *Supervisor) markCrashed(c *child, _ string) {
	s.mu.Lock()
	delete(s.children, c.modelID)
	delete(s.preSuspend, c.modelID)
	if _, ok := s.states[c.modelID]; ok {
		s.setStateLocked(c.modelID, protocol.ReplicaStateStopped)
	}
	s.mu.Unlock()
	s.reap(c)
}

// setStateLocked records a new state and rebuilds the status cache. A STOPPED
// state for a replica the supervisor never registered (no port) is ignored.
func (s *Supervisor) setStateLocked(modelID string, state protocol.ReplicaState) {
	if _, known := s.ports[modelID]; !known && state == protocol.ReplicaStateStopped {
		return
	}
	s.states[modelID] = state
	s.rebuildCacheLocked()
}

func (s *Supervisor) rebuildCacheLocked() {
	cached := make([]protocol.ReplicaStatus, 0, len(s.states))
	for modelID, state := range s.states {
		cached = append(cached, protocol.ReplicaStatus{
			ModelID:  modelID,
			Port:     s.ports[modelID],
			State:    state,
			GPU:      s.gpu[modelID],
			Inflight: 0,
		})
	}
	s.cached = cached
}

// ── Suspend / resume (hot path) ────────────────────────────────────────────

func (s *Supervisor) applySignal(suspend bool) {
	s.mu.Lock()
	snapshot := make([]*child, 0, len(s.children))
	for _, c := range s.children {
		snapshot = append(snapshot, c)
	}
	s.mu.Unlock()

	vanished := s.signalProcesses(snapshot, suspend)
	s.commitSignal(snapshot, vanished, suspend)
}

// signalProcesses sends the suspend or resume signal to each live child and
// returns the set of model_ids whose process has vanished (already reaped, or
// the signal reported the process no longer exists). It runs outside the lock.
func (s *Supervisor) signalProcesses(snapshot []*child, suspend bool) map[string]bool {
	vanished := make(map[string]bool)
	for _, c := range snapshot {
		// Check the handle we own before signalling. Signalling a stale or
		// reused PID would be unsafe on every platform.
		if c.hasExited() {
			vanished[c.modelID] = true
			continue
		}
		var err error
		if suspend {
			err = s.suspend(c.pid)
		} else {
			err = s.resume(c.pid)
		}
		if err != nil {
			vanished[c.modelID] = true
		}
	}
	return vanished
}

func (s *Supervisor) commitSignal(snapshot []*child, vanished map[string]bool, suspend bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range snapshot {
		modelID := c.modelID
		if vanished[modelID] {
			c.signalShutdown() // let its health goroutine exit promptly
			s.pruneLocked(modelID)
		} else if _, ok := s.states[modelID]; ok {
			s.transitionSignalLocked(modelID, suspend)
		}
	}
	s.rebuildCacheLocked()
}

func (s *Supervisor) transitionSignalLocked(modelID string, suspend bool) {
	current := s.states[modelID]
	if suspend {
		if current != protocol.ReplicaStateSuspended {
			s.preSuspend[modelID] = current
		}
		s.states[modelID] = protocol.ReplicaStateSuspended
		return
	}
	if prev, ok := s.preSuspend[modelID]; ok {
		s.states[modelID] = prev
		delete(s.preSuspend, modelID)
	} else {
		s.states[modelID] = current
	}
}

func (s *Supervisor) pruneLocked(modelID string) {
	delete(s.children, modelID)
	delete(s.healthDone, modelID)
	delete(s.preSuspend, modelID)
	if _, ok := s.states[modelID]; ok {
		s.states[modelID] = protocol.ReplicaStateStopped
	}
}

// ── Process teardown (never under the lock) ────────────────────────────────

func (s *Supervisor) gracefulTerminate(c *child) {
	s.terminate(c)
	if c.waitExited(s.cfg.StopGrace) {
		return
	}
	s.forceKill(c)
}

// terminate requests a graceful stop. On Unix this is SIGTERM; on Windows the
// signal is unsupported and the error is ignored, so the grace period elapses
// and forceKill (TerminateProcess) takes over — matching the Python path, which
// suppresses the OSError from terminate.
func (s *Supervisor) terminate(c *child) {
	_ = c.cmd.Process.Signal(syscall.SIGTERM)
}

func (s *Supervisor) forceKill(c *child) {
	_ = c.cmd.Process.Kill()
	c.waitExited(s.cfg.StopGrace)
}

func (s *Supervisor) reap(c *child) {
	c.waitExited(s.cfg.StopGrace)
}
