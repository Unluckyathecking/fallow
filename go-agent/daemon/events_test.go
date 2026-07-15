package daemon

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/coordinator"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

type eventClientFake struct {
	mu       sync.Mutex
	received []protocol.AgentEvent
	err      error
}

func (client *eventClientFake) PushEvent(
	_ context.Context, event protocol.AgentEvent,
) error {
	client.mu.Lock()
	defer client.mu.Unlock()
	client.received = append(client.received, event)
	return client.err
}

func (client *eventClientFake) Count() int {
	client.mu.Lock()
	defer client.mu.Unlock()
	return len(client.received)
}

func event(sequence string) protocol.AgentEvent {
	return protocol.AgentEvent{
		AgentID: "agent-1", At: fixedDaemonTime,
		Detail: map[string]string{"seq": sequence}, Kind: protocol.EventKindUserIdle,
	}
}

func TestEventSinkPersistsAndPushesInOrder(t *testing.T) {
	client := &eventClientFake{}
	path := filepath.Join(t.TempDir(), "events.jsonl")
	sink := newEventSink(client, path, DefaultEventSinkConfig(), instantSleep)
	sink.Emit(event("0"))
	sink.Start(context.Background())
	sink.Emit(event("1"))
	sink.Emit(event("2"))
	sink.Stop()

	if client.Count() != 3 {
		t.Fatalf("pushes = %d", client.Count())
	}
	if got := eventSequences(t, path); !reflectStrings(got, []string{"0", "1", "2"}) {
		t.Fatalf("sequences = %#v", got)
	}
}

func TestEventSinkRetriesCoordinatorErrorsButNotAuth(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want int
	}{
		{name: "transient", err: &coordinator.TransientError{Message: "offline"}, want: 3},
		{name: "protocol", err: &coordinator.ProtocolError{Message: "bad status"}, want: 3},
		{name: "auth", err: &coordinator.AuthError{Status: 401}, want: 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := &eventClientFake{err: test.err}
			path := filepath.Join(t.TempDir(), "events.jsonl")
			sink := newEventSink(client, path, DefaultEventSinkConfig(), instantSleep)
			sink.Start(context.Background())
			sink.Emit(event("0"))
			sink.Stop()
			if client.Count() != test.want {
				t.Fatalf("pushes = %d, want %d", client.Count(), test.want)
			}
			if got := eventSequences(t, path); !reflectStrings(got, []string{"0"}) {
				t.Fatalf("durable events = %#v", got)
			}
		})
	}
}

func TestEventSinkEmitDoesNotWaitForNetwork(t *testing.T) {
	blocking := &blockingEventClient{entered: make(chan struct{}), release: make(chan struct{})}
	sink := newEventSink(
		blocking, filepath.Join(t.TempDir(), "events.jsonl"), DefaultEventSinkConfig(), instantSleep,
	)
	sink.Start(context.Background())
	sink.Emit(event("0"))
	<-blocking.entered
	done := make(chan struct{})
	go func() {
		sink.Emit(event("1"))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(50 * time.Millisecond):
		t.Fatal("Emit waited for network")
	}
	close(blocking.release)
	sink.Stop()
}

type blockingEventClient struct {
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (client *blockingEventClient) PushEvent(
	_ context.Context, _ protocol.AgentEvent,
) error {
	client.once.Do(func() { close(client.entered) })
	<-client.release
	return nil
}

func eventSequences(t *testing.T, path string) []string {
	t.Helper()
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	sequences := []string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		var event protocol.AgentEvent
		if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
			t.Fatal(err)
		}
		sequences = append(sequences, event.Detail["seq"])
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return sequences
}

func instantSleep(context.Context, time.Duration) error { return nil }

func reflectStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
