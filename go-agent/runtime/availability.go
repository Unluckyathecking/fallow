// Package runtime — availability.go is the shared availability view the Site Mode
// claim runner reads. It fuses three signals the daemon already tracks — the
// preemption state, the sticky reclaim override, and READY loopback replicas —
// into one snapshot, and advances a generation whenever the agent stops being
// eligible so a claim admitted before the user returned is never served after.
//
// Inputs are pushed in, never pulled: the sequencing sink calls setActive at a
// presence transition (so a returning user cancels a claim before the presence
// event reaches the wire), and the poll loop calls setReclaimed/setReplicaReady
// each tick. Pushing avoids reading the preemption controller while it holds its
// own lock during an event emit, which would deadlock.
package runtime

import (
	"sync"

	"github.com/Unluckyathecking/fallow/go-agent/inference"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// availability implements inference.AvailabilitySource. A Ready->NotReady
// transition closes the current change channel (cancelling any in-flight claim)
// and advances the generation so stale claims are rejected on re-entry.
type availability struct {
	mu        sync.Mutex
	active    bool // the user is present (set from the presence event)
	reclaimed bool // the sticky user takedown is engaged
	hasReady  bool // at least one local replica reports READY
	ready     bool
	gen       uint64
	code      inference.FailureCode
	changed   chan struct{}
}

func newAvailability() *availability {
	return &availability{changed: make(chan struct{})}
}

// Snapshot returns the current availability view. Changed is closed when the
// view is superseded.
func (a *availability) Snapshot() inference.AvailabilitySnapshot {
	a.mu.Lock()
	defer a.mu.Unlock()
	return inference.AvailabilitySnapshot{
		Ready:           a.ready,
		Generation:      a.gen,
		Changed:         a.changed,
		UnavailableCode: a.code,
	}
}

// setActive records a presence transition from a preemption event. It is called
// from the sequencing sink, before the event is forwarded to the wire.
func (a *availability) setActive(active bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.active = active
	a.recompute()
}

// setReclaimed and setReplicaReady are driven by the poll loop each tick.
func (a *availability) setReclaimed(reclaimed bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.reclaimed = reclaimed
	a.recompute()
}

func (a *availability) setReplicaReady(hasReady bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.hasReady = hasReady
	a.recompute()
}

// recompute publishes a new snapshot on any readiness change. Runs under a.mu.
func (a *availability) recompute() {
	next := !a.active && !a.reclaimed && a.hasReady
	if next == a.ready {
		return
	}
	if a.ready && !next {
		a.gen++
		a.code = a.unavailableReason()
	} else {
		a.code = ""
	}
	a.ready = next
	close(a.changed)
	a.changed = make(chan struct{})
}

// unavailableReason runs under a.mu; reclaim wins over an active user.
func (a *availability) unavailableReason() inference.FailureCode {
	switch {
	case a.reclaimed:
		return inference.Reclaimed
	case a.active:
		return inference.BecameActive
	default:
		return inference.Cancelled
	}
}

func hasReadyReplica(statuses []protocol.ReplicaStatus) bool {
	for _, s := range statuses {
		if s.State == protocol.ReplicaStateReady {
			return true
		}
	}
	return false
}

// replicaTarget implements inference.ReplicaTarget: a port may be dialed only
// when a locally-owned replica reports READY on it. Site Mode binds replicas to
// loopback (enforced by config), so a READY port is a loopback port.
type replicaTarget struct{ supervisor Supervisor }

func (r replicaTarget) ReadyLoopbackPort(port int) bool {
	if port < 1 || port > 65535 {
		return false
	}
	for _, s := range r.supervisor.Statuses() {
		if s.Port == port && s.State == protocol.ReplicaStateReady {
			return true
		}
	}
	return false
}
