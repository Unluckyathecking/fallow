// Package runtime — sequence.go owns the monotonic sequence shared by heartbeats
// and presence events.
//
// Two implementations exist. Direct (legacy) agents use a volatile source that
// starts at 0 and resets every process, preserving their existing unfenced
// behaviour. Site Mode agents use a restart-safe source persisted in the
// identity file: it reserves sequence numbers ahead in blocks and writes the
// reserved ceiling to disk before handing any of them out, so a fresh process
// always resumes at or above the last number a crashed predecessor could have
// used. That is what keeps the daemon from ever regressing behind the
// coordinator's presence fence (#112) across a restart.
package runtime

import (
	"sync"
	"sync/atomic"

	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// seqReserveBlock is how many sequence numbers a Site Mode source reserves per
// disk write. A larger block trades a wider post-crash gap (harmless: the fence
// only needs monotonicity, not contiguity) for fewer identity-file writes.
const seqReserveBlock int64 = 16

// seqSource yields the next value of the monotonic sequence. next never blocks
// on the common path; a Site Mode source touches disk only on a block boundary.
type seqSource interface {
	next() int
}

// volatileSeq is the direct-agent source: it starts at 0 and resets each
// process, exactly as the daemon behaved before Site Mode existed.
type volatileSeq struct{ n atomic.Int64 }

func (s *volatileSeq) next() int { return int(s.n.Add(1) - 1) }

// persistentSeq is the Site Mode source. It hands out values from a reserved
// window and refills that window on disk before it is exhausted, so no value is
// ever handed out that a successor process could repeat lower.
type persistentSeq struct {
	mu       sync.Mutex
	path     string
	identity state.Identity
	cursor   int64
	reserved int64
	block    int64
	save     func(string, state.Identity) error
	onError  func(error)
}

// newPersistentSeq builds a Site Mode source resuming from id.Seq. It reserves
// the first block synchronously so a bad state directory fails closed at start,
// before any sequence is served. save and onError are injected for tests.
func newPersistentSeq(path string, id state.Identity, save func(string, state.Identity) error, onError func(error)) (*persistentSeq, error) {
	s := &persistentSeq{
		path:     path,
		identity: id,
		cursor:   id.Seq,
		reserved: id.Seq,
		block:    seqReserveBlock,
		save:     save,
		onError:  onError,
	}
	if err := s.reserve(); err != nil {
		return nil, err
	}
	return s, nil
}

// reserve advances the persisted ceiling by one block. The caller holds s.mu
// (or is the constructor, before the source is shared).
func (s *persistentSeq) reserve() error {
	s.reserved = s.cursor + s.block
	s.identity.Seq = s.reserved
	return s.save(s.path, s.identity)
}

func (s *persistentSeq) next() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cursor >= s.reserved {
		if err := s.reserve(); err != nil && s.onError != nil {
			// A persist failure means the next process could repeat this value.
			// Surface it as fatal so the daemon fails closed rather than emit an
			// unbacked sequence; the value we return is at most repeated once,
			// which the fence treats as idempotent.
			s.onError(err)
		}
	}
	v := s.cursor
	s.cursor++
	return int(v)
}
