package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/coordinator"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

type EventClient interface {
	PushEvent(context.Context, protocol.AgentEvent) error
}

type EventSinkConfig struct {
	MaxPushAttempts int
	Backoff         time.Duration
}

func DefaultEventSinkConfig() EventSinkConfig {
	return EventSinkConfig{MaxPushAttempts: 3, Backoff: 500 * time.Millisecond}
}

type EventSink struct {
	client   EventClient
	jsonl    string
	config   EventSinkConfig
	sleep    coordinator.SleepFunc
	mu       sync.Mutex
	queue    []protocol.AgentEvent
	wake     chan struct{}
	stop     chan struct{}
	done     chan struct{}
	start    sync.Once
	stopOnce sync.Once
	doneOnce sync.Once
	started  atomic.Bool
}

func NewEventSink(client EventClient, jsonlPath string) *EventSink {
	return newEventSink(client, jsonlPath, DefaultEventSinkConfig(), sleepContext)
}

func newEventSink(
	client EventClient,
	jsonlPath string,
	config EventSinkConfig,
	sleep coordinator.SleepFunc,
) *EventSink {
	return &EventSink{
		client: client, jsonl: jsonlPath, config: config, sleep: sleep,
		wake: make(chan struct{}, 1), stop: make(chan struct{}), done: make(chan struct{}),
	}
}

func (sink *EventSink) Emit(event protocol.AgentEvent) {
	sink.mu.Lock()
	sink.queue = append(sink.queue, event)
	sink.mu.Unlock()
	select {
	case sink.wake <- struct{}{}:
	default:
	}
}

func (sink *EventSink) Start(ctx context.Context) {
	sink.start.Do(func() {
		sink.started.Store(true)
		go sink.run(ctx)
	})
}

func (sink *EventSink) Stop() {
	sink.stopOnce.Do(func() { close(sink.stop) })
	if !sink.started.Load() {
		sink.drain(context.Background())
		sink.doneOnce.Do(func() { close(sink.done) })
	}
	<-sink.done
}

func (sink *EventSink) run(ctx context.Context) {
	defer sink.doneOnce.Do(func() { close(sink.done) })
	for {
		select {
		case <-ctx.Done():
			sink.drain(context.WithoutCancel(ctx))
			return
		case <-sink.stop:
			sink.drain(context.WithoutCancel(ctx))
			return
		case <-sink.wake:
			sink.drain(ctx)
		}
	}
}

func (sink *EventSink) drain(ctx context.Context) {
	for {
		sink.mu.Lock()
		if len(sink.queue) == 0 {
			sink.mu.Unlock()
			return
		}
		event := sink.queue[0]
		sink.queue[0] = protocol.AgentEvent{}
		sink.queue = sink.queue[1:]
		sink.mu.Unlock()
		sink.appendJSONL(event)
		sink.pushBestEffort(ctx, event)
	}
}

func (sink *EventSink) appendJSONL(event protocol.AgentEvent) {
	if err := os.MkdirAll(filepath.Dir(sink.jsonl), 0o755); err != nil {
		return
	}
	file, err := os.OpenFile(sink.jsonl, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer file.Close()
	payload, err := json.Marshal(event)
	if err != nil {
		return
	}
	_, _ = fmt.Fprintf(file, "%s\n", payload)
}

func (sink *EventSink) pushBestEffort(ctx context.Context, event protocol.AgentEvent) {
	for attempt := 0; attempt < sink.config.MaxPushAttempts; attempt++ {
		err := sink.client.PushEvent(ctx, event)
		if err == nil {
			return
		}
		var auth *coordinator.AuthError
		if errors.As(err, &auth) {
			return
		}
		if attempt+1 < sink.config.MaxPushAttempts {
			if err := sink.sleep(ctx, sink.config.Backoff*(1<<attempt)); err != nil {
				return
			}
		}
	}
}

func sleepContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
