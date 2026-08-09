package runtime

import (
	"context"
	"fmt"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// eventBuffer bounds the in-memory event queue. Preemption events are small and
// infrequent; a full buffer only ever happens under pathological churn, where
// dropping the oldest surplus event is the right trade against blocking the poll
// thread.
const eventBuffer = 128

// eventPushTTL caps each push so a hung coordinator connection cannot block the
// shutdown flush — and therefore daemon exit — indefinitely.
const eventPushTTL = 3 * time.Second

// Site Mode presence events (user_returned/user_idle) carry the fence sequence
// that advances the coordinator's presence generation. Losing one silently would
// let a higher-sequence heartbeat overtake the transition and strand serving, so
// they are retried; if still undeliverable the daemon fails closed rather than
// acknowledge an undelivered transition.
const (
	criticalPushAttempts = 3
	criticalPushBackoff  = 250 * time.Millisecond
)

// eventSink forwards preemption events to the coordinator off the hot path. Emit
// never blocks the caller (the preempt controller runs it under its lock); a
// background worker drains the queue to Coordinator.PushEvent. This is the Go
// analogue of the Python HttpEventSink's non-blocking enqueue.
//
// flush lets the Site Mode heartbeat loop guarantee that every event already
// enqueued (and therefore stamped with a lower sequence) has reached the wire
// before a higher-sequence heartbeat is sent, so a delayed presence event can
// never be overtaken and dropped by the coordinator's monotonic fence.
type eventSink struct {
	client Coordinator
	ch     chan sinkItem
	done   chan struct{}
	// onFatal, when set (Site Mode), is called if a critical presence event
	// cannot be delivered after retries, so the daemon fails closed instead of
	// acknowledging the flush and letting a heartbeat overtake the lost transition.
	onFatal func(error)
	// sleep is the retry backoff sleeper, injected in tests.
	sleep func(time.Duration)
}

// sinkItem carries either an event to push or a flush barrier to acknowledge.
type sinkItem struct {
	event   *protocol.AgentEvent
	flushed chan struct{}
}

func newEventSink(client Coordinator) *eventSink {
	return &eventSink{
		client: client,
		ch:     make(chan sinkItem, eventBuffer),
		done:   make(chan struct{}),
		sleep:  time.Sleep,
	}
}

// isCriticalPresence reports whether an event carries the presence fence sequence
// whose loss would strand serving. Only these are retried and fail closed.
func isCriticalPresence(kind protocol.EventKind) bool {
	return kind == protocol.EventKindUserReturned || kind == protocol.EventKindUserIdle
}

// start launches the drain worker.
func (s *eventSink) start() {
	go s.run()
}

// Emit enqueues an event without blocking. If the buffer is full the event is
// dropped rather than stalling the preemption hot path.
func (s *eventSink) Emit(event protocol.AgentEvent) {
	e := event
	select {
	case s.ch <- sinkItem{event: &e}:
	default:
		logf("event buffer full; dropped %s", event.Kind)
	}
}

// flush blocks until every event enqueued before this call has been pushed. The
// channel is FIFO with a single drain worker, so the barrier is acknowledged
// only after all prior events are delivered. It must not be called after close.
func (s *eventSink) flush() {
	done := make(chan struct{})
	s.ch <- sinkItem{flushed: done}
	<-done
}

// close stops accepting events and flushes those already queued before returning.
// It must be called after every Emitter (the preempt loop and Drain) has stopped.
func (s *eventSink) close() {
	close(s.ch)
	<-s.done
}

func (s *eventSink) run() {
	defer close(s.done)
	for item := range s.ch {
		if item.flushed != nil {
			// The barrier is acknowledged only after every preceding event has
			// been delivered (or the daemon has been failed closed), because a
			// critical presence event blocks this loop until it succeeds.
			close(item.flushed)
			continue
		}
		s.deliver(*item.event)
	}
}

// deliver pushes one event. A critical presence event is retried and, if still
// undeliverable, fails the daemon closed (when onFatal is wired) so a heartbeat
// can never overtake a lost presence transition. Other events are best effort.
func (s *eventSink) deliver(event protocol.AgentEvent) {
	critical := isCriticalPresence(event.Kind) && s.onFatal != nil
	attempts := 1
	if critical {
		attempts = criticalPushAttempts
	}
	var err error
	for attempt := 0; attempt < attempts; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), eventPushTTL)
		err = s.client.PushEvent(ctx, event)
		cancel()
		if err == nil {
			return
		}
		if attempt < attempts-1 {
			s.sleep(criticalPushBackoff)
		}
	}
	if critical {
		s.onFatal(fmt.Errorf("could not deliver presence event %s after %d attempts: %w", event.Kind, attempts, err))
		return
	}
	logf("push event %s failed: %v", event.Kind, err)
}
