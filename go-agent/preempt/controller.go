package preempt

import (
	"fmt"
	"sync"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const yieldMSKey = "yield_ms"

type Supervisor interface {
	SuspendAll()
	ResumeAll()
	Stop(modelID string)
	Statuses() []protocol.ReplicaStatus
}

type EventSink interface {
	Emit(protocol.AgentEvent)
}

type Controller struct {
	mu           sync.Mutex
	supervisor   Supervisor
	sink         EventSink
	config       protocol.AgentConfig
	agentID      string
	monotonic    func() time.Time
	now          func() time.Time
	pollInterval time.Duration
	state        protocol.AgentState
	previousIdle *time.Duration
	activeSince  *time.Time
	escalated    bool
}

func NewController(
	supervisor Supervisor,
	sink EventSink,
	config protocol.AgentConfig,
	agentID string,
) *Controller {
	return newController(supervisor, sink, config, agentID, time.Now, time.Now)
}

func newController(
	supervisor Supervisor,
	sink EventSink,
	config protocol.AgentConfig,
	agentID string,
	monotonic func() time.Time,
	now func() time.Time,
) *Controller {
	return &Controller{
		supervisor:   supervisor,
		sink:         sink,
		config:       config,
		agentID:      agentID,
		monotonic:    monotonic,
		now:          now,
		pollInterval: time.Duration(config.PollIntervalMs) * time.Millisecond,
		state:        protocol.AgentStateIdle,
	}
}

func (c *Controller) State() protocol.AgentState {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.state
}

func (c *Controller) OnPoll(idle time.Duration, monotonicNow time.Time) protocol.AgentState {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state == protocol.AgentStateDraining {
		return c.state
	}
	switch c.state {
	case protocol.AgentStateIdle:
		if c.isFreshInput(idle) {
			c.enterActive(monotonicNow)
		}
	case protocol.AgentStateActive:
		threshold := time.Duration(c.config.IdleThresholdS * float64(time.Second))
		if idle >= threshold {
			c.enterIdle()
		} else {
			c.maybeEscalate(monotonicNow)
		}
	}
	previous := idle
	c.previousIdle = &previous
	return c.state
}

func (c *Controller) Drain() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state == protocol.AgentStateDraining {
		return
	}
	c.state = protocol.AgentStateDraining
	c.emit(protocol.EventKindAgentStopping, map[string]string{})
}

func (c *Controller) isFreshInput(idle time.Duration) bool {
	if idle < c.pollInterval {
		return true
	}
	return c.previousIdle != nil && idle < *c.previousIdle
}

func (c *Controller) enterActive(monotonicNow time.Time) {
	// Suspend is the first observable side effect on the user-return path.
	c.supervisor.SuspendAll()
	yield := c.monotonic().Sub(monotonicNow)
	c.state = protocol.AgentStateActive
	activeSince := monotonicNow
	c.activeSince = &activeSince
	c.escalated = false
	c.emit(protocol.EventKindUserReturned, map[string]string{
		yieldMSKey: fmt.Sprintf("%.3f", float64(yield)/float64(time.Millisecond)),
	})
}

func (c *Controller) enterIdle() {
	c.supervisor.ResumeAll()
	c.state = protocol.AgentStateIdle
	c.activeSince = nil
	c.escalated = false
	c.emit(protocol.EventKindUserIdle, map[string]string{})
}

func (c *Controller) maybeEscalate(monotonicNow time.Time) {
	if c.escalated || c.activeSince == nil {
		return
	}
	delay := time.Duration(c.config.VRAMEvictAfterS * float64(time.Second))
	if monotonicNow.Sub(*c.activeSince) < delay {
		return
	}
	for _, status := range c.supervisor.Statuses() {
		if status.State == protocol.ReplicaStateSuspended && status.GPU {
			c.supervisor.Stop(status.ModelID)
		}
	}
	c.escalated = true
}

func (c *Controller) emit(kind protocol.EventKind, detail map[string]string) {
	c.sink.Emit(protocol.AgentEvent{
		AgentID: c.agentID,
		At:      c.now(),
		Detail:  detail,
		Kind:    kind,
	})
}
