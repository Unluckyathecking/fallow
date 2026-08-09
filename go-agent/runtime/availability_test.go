package runtime

import (
	"testing"

	"github.com/Unluckyathecking/fallow/go-agent/inference"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

func isClosed(ch <-chan struct{}) bool {
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// TestAvailabilityReadyRequiresIdleUnreclaimedAndReplica pins the eligibility
// gate: a claim may only be served while idle, not reclaimed, with a READY
// replica present.
func TestAvailabilityReadyRequiresIdleUnreclaimedAndReplica(t *testing.T) {
	a := newAvailability()
	if a.Snapshot().Ready {
		t.Fatal("fresh availability should not be ready (no replica yet)")
	}
	a.setReplicaReady(true)
	if !a.Snapshot().Ready {
		t.Fatal("should be ready once a replica is READY and the user is idle")
	}
	a.setActive(true)
	if a.Snapshot().Ready {
		t.Fatal("an active user must make the agent unavailable")
	}
	a.setActive(false)
	a.setReclaimed(true)
	if a.Snapshot().Ready {
		t.Fatal("a reclaimed machine must make the agent unavailable")
	}
}

// TestAvailabilityAdvancesGenerationAndCancelsOnUserReturn is the reclaim /
// preemption ordering proof at the availability layer: going unavailable closes
// the change channel (cancelling any in-flight claim) and advances the
// generation so a stale claim admitted before the return is rejected.
func TestAvailabilityAdvancesGenerationAndCancelsOnUserReturn(t *testing.T) {
	a := newAvailability()
	a.setReplicaReady(true)
	before := a.Snapshot()
	if !before.Ready {
		t.Fatal("expected ready before the user returns")
	}

	a.setActive(true) // user returns
	after := a.Snapshot()
	if after.Ready {
		t.Fatal("expected not ready after the user returns")
	}
	if !isClosed(before.Changed) {
		t.Fatal("the admitting snapshot's Changed channel was not closed on user return")
	}
	if after.Generation != before.Generation+1 {
		t.Fatalf("generation = %d, want %d (advanced on going away)", after.Generation, before.Generation+1)
	}
	if after.UnavailableCode != inference.BecameActive {
		t.Fatalf("unavailable code = %q, want became_active", after.UnavailableCode)
	}
}

// TestAvailabilityReclaimReportsReclaimedCode checks that a reclaim from an
// eligible state classifies the failure as reclaimed.
func TestAvailabilityReclaimReportsReclaimedCode(t *testing.T) {
	a := newAvailability()
	a.setReplicaReady(true)
	if !a.Snapshot().Ready {
		t.Fatal("expected ready before reclaim")
	}
	a.setReclaimed(true)
	if got := a.Snapshot().UnavailableCode; got != inference.Reclaimed {
		t.Fatalf("unavailable code = %q, want reclaimed", got)
	}
}

// TestAwayAndBackAdvancesGeneration proves an away-and-back cycle lands on a new
// generation, so a claim admitted before the cycle is never served after it.
func TestAwayAndBackAdvancesGeneration(t *testing.T) {
	a := newAvailability()
	a.setReplicaReady(true)
	gen0 := a.Snapshot().Generation
	a.setActive(true)  // away
	a.setActive(false) // back
	back := a.Snapshot()
	if !back.Ready {
		t.Fatal("expected ready again after returning to idle")
	}
	if back.Generation == gen0 {
		t.Fatal("generation did not advance across an away-and-back cycle")
	}
}

// TestReplicaTargetAcceptsOnlyReadyPorts confirms the loopback-only replica
// gate: only a port owned by a READY replica may be dialed.
func TestReplicaTargetAcceptsOnlyReadyPorts(t *testing.T) {
	sup := &fakeSupervisor{statuses: []protocol.ReplicaStatus{
		{ModelID: "ready", Port: 8100, State: protocol.ReplicaStateReady},
		{ModelID: "loading", Port: 8101, State: protocol.ReplicaStateLoading},
		{ModelID: "suspended", Port: 8102, State: protocol.ReplicaStateSuspended},
	}}
	rt := replicaTarget{supervisor: sup}
	if !rt.ReadyLoopbackPort(8100) {
		t.Error("a READY replica port was rejected")
	}
	for _, port := range []int{8101, 8102, 9999, 0, -1, 70000} {
		if rt.ReadyLoopbackPort(port) {
			t.Errorf("port %d was accepted but is not a READY replica port", port)
		}
	}
}
