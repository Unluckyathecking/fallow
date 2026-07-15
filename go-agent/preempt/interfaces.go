// Package preempt is Fallow's yield/resume/escalate state machine — the
// agent's signature feature. PreemptController implements the Preemptor
// contract and is driven one tick at a time by an external poll loop. The
// single hard rule: when the user returns, the first observable side effect is
// ProcessSupervisor.SuspendAll — nothing may run before it.
//
// State transitions (states are protocol.AgentState):
//
//	Idle     --fresh user input-->        Active   (SuspendAll, emit user_returned)
//	Active   --held vram_evict_after_s--> Active   (stop suspended GPU replicas once)
//	Active   --idle >= idle_threshold_s-> Idle     (ResumeAll, emit user_idle)
//	any      --Drain()-->                 Draining (terminal; emit agent_stopping)
//
// Hysteresis is intrinsic: idle_s is seconds since last input, so a brief pause
// after a return can never reach idle_threshold_s — work only resumes after
// genuinely continuous idleness. This is a 1:1 port of the Python
// fallow_agent.preempt.controller.PreemptController.
package preempt

import "github.com/Unluckyathecking/fallow/go-agent/protocol"

// ProcessSupervisor is the subset of the agent's replica supervisor the
// controller drives. All methods are non-blocking (the real supervisor signals
// child processes and returns immediately).
type ProcessSupervisor interface {
	// SuspendAll pauses every running replica. This is the hot-path yield.
	SuspendAll()
	// ResumeAll resumes every suspended replica.
	ResumeAll()
	// StopReplica terminates the replica serving modelID (VRAM eviction).
	StopReplica(modelID string)
	// Statuses returns the current replica statuses.
	Statuses() []protocol.ReplicaStatus
}

// EventSink receives agent events emitted by the controller. Emit must never
// block the poll thread (the production sink enqueues and returns).
type EventSink interface {
	Emit(event protocol.AgentEvent)
}

// Preemptor is the contract the controller implements and the heartbeat loop
// reads.
type Preemptor interface {
	State() protocol.AgentState
	OnPoll(idleS, monotonicNow float64) protocol.AgentState
}
