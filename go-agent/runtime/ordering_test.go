package runtime

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/inference"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// recordingCoord records the order in which events reach the wire.
type recordingCoord struct {
	mu       sync.Mutex
	pushed   []string
	pushGate chan struct{} // if non-nil, each PushEvent blocks until it is signalled
}

func (c *recordingCoord) Register(context.Context, protocol.RegisterRequest) (protocol.RegisterResponse, error) {
	return protocol.RegisterResponse{}, nil
}
func (c *recordingCoord) Heartbeat(context.Context, protocol.Heartbeat) (protocol.HeartbeatResponse, error) {
	return protocol.HeartbeatResponse{}, nil
}
func (c *recordingCoord) PollWork(context.Context, float64) (*protocol.WorkUnitLease, error) {
	return nil, nil
}
func (c *recordingCoord) PushEvent(_ context.Context, event protocol.AgentEvent) error {
	if c.pushGate != nil {
		<-c.pushGate
	}
	c.mu.Lock()
	c.pushed = append(c.pushed, event.Detail["sequence"])
	c.mu.Unlock()
	return nil
}
func (c *recordingCoord) AgentID() string     { return "a" }
func (c *recordingCoord) DeviceToken() string { return "t" }
func (c *recordingCoord) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.pushed)
}

// TestEventSinkFlushIsDeliveryBarrier proves flush does not return until every
// event enqueued before it has actually been pushed — the barrier the Site Mode
// heartbeat relies on so it never overtakes a queued presence event.
func TestEventSinkFlushIsDeliveryBarrier(t *testing.T) {
	gate := make(chan struct{})
	coord := &recordingCoord{pushGate: gate}
	sink := newEventSink(coord)
	sink.start()
	defer sink.close()

	sink.Emit(protocol.AgentEvent{Kind: protocol.EventKindUserIdle, Detail: map[string]string{"sequence": "7"}})

	flushed := make(chan struct{})
	go func() { sink.flush(); close(flushed) }()

	// The event push is gated, so flush must still be blocked.
	select {
	case <-flushed:
		t.Fatal("flush returned before the queued event was delivered")
	case <-time.After(50 * time.Millisecond):
	}
	close(gate) // let the push complete
	select {
	case <-flushed:
	case <-time.After(2 * time.Second):
		t.Fatal("flush did not return after the event was delivered")
	}
	if coord.count() != 1 {
		t.Fatalf("expected 1 delivered event, got %d", coord.count())
	}
}

// TestHeartbeatSeqCannotOvertakeQueuedEvent is the ordering proof for #4: a
// presence event stamped with sequence N is delivered before the heartbeat that
// allocates N+1 is sent. beatSeq allocates the heartbeat sequence under the same
// presence lock the sink stamps under, then flushes, so the queued event wins.
func TestHeartbeatSeqCannotOvertakeQueuedEvent(t *testing.T) {
	coord := &recordingCoord{}
	rt := &Runtime{seq: &volatileSeq{}}
	rt.sink = newEventSink(coord)
	rt.sink.start()
	defer rt.sink.close()
	rt.site = &siteRuntime{} // mark Site Mode so beatSeq takes the ordered path
	sink := &sequencingSink{inner: rt.sink, seq: rt.seq, avail: newAvailability(), presence: &rt.presenceMu}

	// Emit a presence event (sequence 0), then take a heartbeat sequence.
	sink.Emit(protocol.AgentEvent{Kind: protocol.EventKindUserIdle})
	hbSeq := rt.beatSeq()

	if hbSeq <= 0 {
		t.Fatalf("heartbeat sequence = %d, want > the event's sequence 0", hbSeq)
	}
	// beatSeq flushed, so the event is already on the wire before the heartbeat
	// (which the caller sends only after beatSeq returns).
	if coord.count() != 1 || coord.pushed[0] != "0" {
		t.Fatalf("presence event not delivered before heartbeat seq %d: pushed=%v", hbSeq, coord.pushed)
	}
}

// TestReconcileWorkerGatesOnEligibility proves reconciliation never starts a
// replica while the machine is not serving-eligible (active, reclaimed or
// mid-eviction), and applies the latest desired set once eligibility returns.
func TestReconcileWorkerGatesOnEligibility(t *testing.T) {
	rec := &fakeReconciler{}
	var eligible atomic.Bool
	s := newSiteRuntime(newAvailability(), replicaTarget{}, inference.Runner{}, rec)
	s.eligible = eligible.Load

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.reconcileWorker(ctx)

	s.submitDesired([]string{"m1"})
	time.Sleep(30 * time.Millisecond)
	if rec.count() != 0 {
		t.Fatalf("reconciled while ineligible: %d applies", rec.count())
	}

	// A newer desired set arrives while still ineligible; it must coalesce.
	s.submitDesired([]string{"m1", "m2"})
	time.Sleep(30 * time.Millisecond)
	if rec.count() != 0 {
		t.Fatalf("reconciled while ineligible: %d applies", rec.count())
	}

	eligible.Store(true)
	s.nudge()
	waitFor(t, "reconcile after eligible", func() bool { return rec.count() >= 1 })
	rec.mu.Lock()
	last := rec.calls[len(rec.calls)-1]
	rec.mu.Unlock()
	if len(last) != 2 {
		t.Fatalf("applied %v, want the latest coalesced set [m1 m2]", last)
	}
}
