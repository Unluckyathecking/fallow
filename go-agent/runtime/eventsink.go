package runtime

import (
	"context"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// eventBuffer bounds the in-memory event queue. Preemption events are small and
// infrequent; a full buffer only ever happens under pathological churn, where
// dropping the oldest surplus event is the right trade against blocking the poll
// thread.
const eventBuffer = 128

// eventSink forwards preemption events to the coordinator off the hot path. Emit
// never blocks the caller (the preempt controller runs it under its lock); a
// background worker drains the queue to Coordinator.PushEvent. This is the Go
// analogue of the Python HttpEventSink's non-blocking enqueue.
type eventSink struct {
	client Coordinator
	ch     chan protocol.AgentEvent
	done   chan struct{}
}

func newEventSink(client Coordinator) *eventSink {
	return &eventSink{
		client: client,
		ch:     make(chan protocol.AgentEvent, eventBuffer),
		done:   make(chan struct{}),
	}
}

// start launches the drain worker.
func (s *eventSink) start() {
	go s.run()
}

// Emit enqueues an event without blocking. If the buffer is full the event is
// dropped rather than stalling the preemption hot path.
func (s *eventSink) Emit(event protocol.AgentEvent) {
	select {
	case s.ch <- event:
	default:
		logf("event buffer full; dropped %s", event.Kind)
	}
}

// close stops accepting events and flushes those already queued before returning.
// It must be called after every Emitter (the preempt loop and Drain) has stopped.
func (s *eventSink) close() {
	close(s.ch)
	<-s.done
}

func (s *eventSink) run() {
	defer close(s.done)
	for event := range s.ch {
		if err := s.client.PushEvent(context.Background(), event); err != nil {
			logf("push event %s failed: %v", event.Kind, err)
		}
	}
}
