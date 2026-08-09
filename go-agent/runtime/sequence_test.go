package runtime

import (
	"errors"
	"sync"
	"testing"

	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// TestVolatileSeqStartsAtZeroAndResets pins the direct-agent behaviour: a fresh
// source starts at 0 and a new process resets, matching legacy unfenced agents.
func TestVolatileSeqStartsAtZeroAndResets(t *testing.T) {
	s := &volatileSeq{}
	for want := 0; want < 5; want++ {
		if got := s.next(); got != want {
			t.Fatalf("volatileSeq.next() = %d, want %d", got, want)
		}
	}
	fresh := &volatileSeq{}
	if got := fresh.next(); got != 0 {
		t.Fatalf("a new process did not reset: got %d, want 0", got)
	}
}

// saver records persisted identities so a test can observe the high-water mark.
type saver struct {
	mu    sync.Mutex
	last  state.Identity
	saved int
	err   error
}

func (s *saver) save(_ string, id state.Identity) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return s.err
	}
	s.last = id
	s.saved++
	return nil
}

func (s *saver) highWater() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.last.Seq
}

// TestPersistentSeqResumesAboveHighWater is the core restart-monotonic proof: a
// second process built from the persisted identity resumes strictly above every
// value the first process could have handed out, so the coordinator fence never
// regresses across a restart.
func TestPersistentSeqResumesAboveHighWater(t *testing.T) {
	sv := &saver{}
	id := state.Identity{AgentID: "a", DeviceToken: "t", Site: &state.SiteProfile{SiteID: "s"}}

	first, err := newPersistentSeq("path", id, sv.save, nil)
	if err != nil {
		t.Fatal(err)
	}
	var maxUsed int
	for i := 0; i < 40; i++ {
		maxUsed = first.next()
	}

	// A crash loses the in-memory cursor; the successor resumes from disk.
	persisted := state.Identity{AgentID: "a", DeviceToken: "t", Site: id.Site, Seq: sv.highWater()}
	second, err := newPersistentSeq("path", persisted, sv.save, nil)
	if err != nil {
		t.Fatal(err)
	}
	resumed := second.next()
	if resumed <= maxUsed {
		t.Fatalf("restart resumed at %d, not above the last used value %d", resumed, maxUsed)
	}
}

// TestPersistentSeqIsMonotonicUnderConcurrency confirms every value is distinct
// and the persisted ceiling always stays ahead of the values handed out.
func TestPersistentSeqIsMonotonicUnderConcurrency(t *testing.T) {
	sv := &saver{}
	id := state.Identity{AgentID: "a", DeviceToken: "t", Site: &state.SiteProfile{SiteID: "s"}}
	s, err := newPersistentSeq("path", id, sv.save, nil)
	if err != nil {
		t.Fatal(err)
	}

	const workers, each = 8, 50
	var wg sync.WaitGroup
	seen := make(chan int, workers*each)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < each; i++ {
				seen <- s.next()
			}
		}()
	}
	wg.Wait()
	close(seen)

	unique := map[int]bool{}
	maxV := -1
	for v := range seen {
		if unique[v] {
			t.Fatalf("duplicate sequence value %d", v)
		}
		unique[v] = true
		if v > maxV {
			maxV = v
		}
	}
	if int64(maxV) >= sv.highWater() {
		t.Fatalf("handed out %d at or beyond the persisted ceiling %d", maxV, sv.highWater())
	}
}

// TestPersistentSeqFailsClosedAtStart proves a bad state directory fails closed
// before any sequence is served.
func TestPersistentSeqFailsClosedAtStart(t *testing.T) {
	sv := &saver{err: errors.New("disk full")}
	id := state.Identity{AgentID: "a", DeviceToken: "t", Site: &state.SiteProfile{SiteID: "s"}}
	if _, err := newPersistentSeq("path", id, sv.save, nil); err == nil {
		t.Fatal("expected a startup error when the sequence cannot be persisted")
	}
}

// TestPersistentSeqReportsMidRunPersistFailure surfaces a later persist failure
// through onError so the daemon fails closed rather than emit an unbacked value.
func TestPersistentSeqReportsMidRunPersistFailure(t *testing.T) {
	sv := &saver{}
	id := state.Identity{AgentID: "a", DeviceToken: "t", Site: &state.SiteProfile{SiteID: "s"}}
	s, err := newPersistentSeq("path", id, sv.save, func(error) {})
	if err != nil {
		t.Fatal(err)
	}
	var reported error
	s.onError = func(e error) { reported = e }
	sv.mu.Lock()
	sv.err = errors.New("disk full")
	sv.mu.Unlock()
	// Exhaust the reserved block until a refill is attempted and fails.
	for i := 0; i < int(seqReserveBlock)+2; i++ {
		s.next()
	}
	if reported == nil {
		t.Fatal("a mid-run persist failure was not reported")
	}
}
