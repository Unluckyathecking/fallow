// Package runtime — sitesink.go stamps the shared monotonic sequence onto Site
// Mode presence events and cancels in-flight claims at a presence transition.
//
// It wraps the runtime event sink for Site Mode only; direct agents keep their
// unfenced events untouched. The sink is the natural interception point for the
// local ordering the relay contract requires: on a user-return it flips the
// availability view (cancelling any claim) before the event is enqueued for the
// wire, so the agent stops serving before the coordinator hears it is active.
package runtime

import (
	"strconv"
	"sync"

	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

type sequencingSink struct {
	inner    preempt.EventSink
	seq      seqSource
	avail    *availability
	presence *sync.Mutex
}

// Emit records the presence transition, stamps the shared sequence into
// detail["sequence"], and forwards the event. It runs under the controller lock,
// so it never blocks: availability update and sequence handout are bounded, and
// the disk write the sequence source may do is rare and off the yield hot path
// (the replicas were already suspended before this event was emitted).
//
// The sequence is allocated and the event enqueued under the shared presence
// lock, atomically, so a concurrent heartbeat cannot slip a higher sequence onto
// the wire ahead of this event: any event whose sequence is below a heartbeat's
// is guaranteed to be enqueued before that heartbeat allocates its own sequence.
func (s *sequencingSink) Emit(event protocol.AgentEvent) {
	switch event.Kind {
	case protocol.EventKindUserReturned, protocol.EventKindAgentStopping:
		s.avail.setActive(true)
	case protocol.EventKindUserIdle:
		s.avail.setActive(false)
	}
	detail := make(map[string]string, len(event.Detail)+1)
	for k, v := range event.Detail {
		detail[k] = v
	}
	s.presence.Lock()
	detail["sequence"] = strconv.Itoa(s.seq.next())
	event.Detail = detail
	s.inner.Emit(event)
	s.presence.Unlock()
}
