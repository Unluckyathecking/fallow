package preempt

import (
	"fmt"
	"sync"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// MonotonicFunc is an injectable monotonic clock (seconds). Defaults to a
// process-start-relative reading of the Go monotonic clock.
type MonotonicFunc func() float64

// NowFunc is an injectable wall clock for event timestamps.
type NowFunc func() time.Time

// Controller is the hot-path preemption decision engine. It implements
// Preemptor.
//
// Thread-safety: OnPoll (poll thread) and Drain (shutdown thread) mutate state
// under an uncontended mutex. The lock guards only in-memory bookkeeping and
// non-blocking supervisor/sink calls, so acquiring it costs nanoseconds and
// never delays the actual suspend.
type Controller struct {
	supervisor    ProcessSupervisor
	sink          EventSink
	config        protocol.AgentConfig
	agentID       string
	monotonic     MonotonicFunc
	now           NowFunc
	pollIntervalS float64

	mu          sync.Mutex
	state       protocol.AgentState
	prevIdleS   *float64
	activeSince *float64
	escalated   bool
}

// Options configures optional Controller collaborators (clocks). Zero values
// fall back to production defaults.
type Options struct {
	Monotonic MonotonicFunc
	Now       NowFunc
}

// NewController builds a Controller starting in the Idle state.
func NewController(
	supervisor ProcessSupervisor,
	sink EventSink,
	config protocol.AgentConfig,
	agentID string,
	opts Options,
) *Controller {
	monotonic := opts.Monotonic
	if monotonic == nil {
		monotonic = defaultMonotonic
	}
	now := opts.Now
	if now == nil {
		now = func() time.Time { return time.Now().UTC() }
	}
	return &Controller{
		supervisor:    supervisor,
		sink:          sink,
		config:        config,
		agentID:       agentID,
		monotonic:     monotonic,
		now:           now,
		pollIntervalS: float64(config.PollIntervalMs) / msPerSecond,
		state:         protocol.AgentStateIdle,
	}
}

var processStart = time.Now()

func defaultMonotonic() float64 { return time.Since(processStart).Seconds() }

// State returns the current agent state.
func (c *Controller) State() protocol.AgentState {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.state
}

// OnPoll advances the state machine one tick. idleS is seconds since last
// input; monotonicNow is the monotonic timestamp at which idleS was sampled.
func (c *Controller) OnPoll(idleS, monotonicNow float64) protocol.AgentState {
	c.mu.Lock()
	defer c.mu.Unlock()

	switch c.state {
	case protocol.AgentStateDraining:
		return c.state
	case protocol.AgentStateIdle:
		if c.isFreshInput(idleS) {
			c.enterActive(monotonicNow)
		}
	case protocol.AgentStateActive:
		if idleS >= c.config.IdleThresholdS {
			c.enterIdle()
		} else {
			c.maybeEscalate(monotonicNow)
		}
	}
	c.prevIdleS = ptr(idleS)
	return c.state
}

// Drain enters the terminal Draining state. Idempotent; accepts no new work.
func (c *Controller) Drain() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state == protocol.AgentStateDraining {
		return
	}
	c.state = protocol.AgentStateDraining
	c.emit(protocol.EventKindAgentStopping, nil)
}

// ── transitions (all run under c.mu) ─────────────────────────────────────────

func (c *Controller) isFreshInput(idleS float64) bool {
	if idleS < c.pollIntervalS {
		return true
	}
	return c.prevIdleS != nil && idleS < *c.prevIdleS
}

func (c *Controller) enterActive(monotonicNow float64) {
	// HOT PATH: suspend first, measure second, emit third. Do not reorder.
	c.supervisor.SuspendAll()
	yieldMS := (c.monotonic() - monotonicNow) * msPerSecond
	c.state = protocol.AgentStateActive
	c.activeSince = ptr(monotonicNow)
	c.escalated = false
	c.emit(protocol.EventKindUserReturned, map[string]string{
		yieldMSKey: fmt.Sprintf("%.3f", yieldMS),
	})
}

func (c *Controller) enterIdle() {
	c.supervisor.ResumeAll()
	c.state = protocol.AgentStateIdle
	c.activeSince = nil
	c.escalated = false
	c.emit(protocol.EventKindUserIdle, nil)
}

func (c *Controller) maybeEscalate(monotonicNow float64) {
	if c.escalated || c.activeSince == nil {
		return
	}
	if monotonicNow-*c.activeSince < c.config.VRAMEvictAfterS {
		return
	}
	// Only GPU replicas: a suspended CPU replica costs nothing the user
	// notices, but pinned VRAM breaks whatever the returning user launches.
	for _, status := range c.supervisor.Statuses() {
		if status.State == protocol.ReplicaStateSuspended && status.GPU {
			c.supervisor.StopReplica(status.ModelID)
		}
	}
	c.escalated = true
}

func (c *Controller) emit(kind protocol.EventKind, detail map[string]string) {
	c.sink.Emit(protocol.AgentEvent{
		AgentID: c.agentID,
		Kind:    kind,
		At:      c.now(),
		Detail:  detail,
	})
}

func ptr(v float64) *float64 { return &v }
