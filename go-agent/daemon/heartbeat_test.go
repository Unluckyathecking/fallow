package daemon

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/coordinator"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

type heartbeatClientFake struct {
	heartbeats []protocol.Heartbeat
	errors     []error
}

func (client *heartbeatClientFake) Heartbeat(
	_ context.Context, heartbeat protocol.Heartbeat,
) (protocol.HeartbeatResponse, error) {
	client.heartbeats = append(client.heartbeats, heartbeat)
	index := len(client.heartbeats) - 1
	if index < len(client.errors) && client.errors[index] != nil {
		return protocol.HeartbeatResponse{}, client.errors[index]
	}
	return protocol.HeartbeatResponse{DesiredModels: []string{"model-1"}}, nil
}

type preemptorFake struct{ state protocol.AgentState }

func (p preemptorFake) State() protocol.AgentState { return p.state }

type idleFake struct {
	idle time.Duration
	err  error
}

func (i idleFake) SecondsSinceInput() (time.Duration, error) { return i.idle, i.err }

type replicasFake struct{ values []protocol.ReplicaStatus }

func (r replicasFake) Statuses() []protocol.ReplicaStatus { return r.values }

func heartbeatLoopForTest(
	client *heartbeatClientFake,
	onResponse func(protocol.HeartbeatResponse),
	onAuth func(error),
) *HeartbeatLoop {
	loop := NewHeartbeatLoop(
		client, "agent-1", 1, time.Second,
		preemptorFake{state: protocol.AgentStateIdle},
		idleFake{idle: 3500 * time.Millisecond},
		replicasFake{values: []protocol.ReplicaStatus{{ModelID: "qwen"}}},
		func() []string { return []string{"lease-1"} },
		func() Metrics { return Metrics{CPUPercent: 5, MemAvailableMB: 1024, GPUs: []protocol.GpuStatus{}} },
		onResponse,
		onAuth,
	)
	loop.now = func() time.Time { return fixedDaemonTime }
	return loop
}

var fixedDaemonTime = time.Date(2026, 7, 15, 12, 0, 0, 0, time.UTC)

func TestHeartbeatTickReportsStateAndMonotonicSequence(t *testing.T) {
	client := &heartbeatClientFake{}
	responses := []protocol.HeartbeatResponse{}
	loop := heartbeatLoopForTest(client, func(response protocol.HeartbeatResponse) {
		responses = append(responses, response)
	}, func(error) {})

	if !loop.tick(context.Background()) || !loop.tick(context.Background()) {
		t.Fatal("successful tick stopped loop")
	}
	if loop.Seq() != 2 {
		t.Fatalf("seq = %d", loop.Seq())
	}
	if !reflect.DeepEqual(
		[]int{client.heartbeats[0].Seq, client.heartbeats[1].Seq}, []int{0, 1},
	) {
		t.Fatalf("heartbeats = %#v", client.heartbeats)
	}
	first := client.heartbeats[0]
	if first.State != protocol.AgentStateIdle || first.UserIdleS != 3.5 ||
		len(first.LeaseIDs) != 1 || len(first.Replicas) != 1 {
		t.Fatalf("heartbeat = %#v", first)
	}
	if len(responses) != 2 || responses[0].DesiredModels[0] != "model-1" {
		t.Fatalf("responses = %#v", responses)
	}
}

func TestHeartbeatTickContinuesOnTransientAndStopsOnAuth(t *testing.T) {
	client := &heartbeatClientFake{errors: []error{
		&coordinator.TransientError{Message: "offline"},
		&coordinator.AuthError{Status: 401},
	}}
	authErrors := []error{}
	loop := heartbeatLoopForTest(client, func(protocol.HeartbeatResponse) {}, func(err error) {
		authErrors = append(authErrors, err)
	})
	if !loop.tick(context.Background()) {
		t.Fatal("transient error stopped loop")
	}
	if loop.tick(context.Background()) {
		t.Fatal("auth error did not stop loop")
	}
	if loop.Seq() != 2 || len(authErrors) != 1 {
		t.Fatalf("seq=%d auth=%#v", loop.Seq(), authErrors)
	}
}

func TestHeartbeatTickSurvivesIdleDetectorError(t *testing.T) {
	client := &heartbeatClientFake{}
	loop := heartbeatLoopForTest(client, func(protocol.HeartbeatResponse) {}, func(error) {})
	loop.idle = idleFake{err: errors.New("detector failed")}
	if !loop.tick(context.Background()) {
		t.Fatal("detector error stopped loop")
	}
	if loop.Seq() != 1 || len(client.heartbeats) != 0 {
		t.Fatalf("seq=%d heartbeats=%#v", loop.Seq(), client.heartbeats)
	}
}
