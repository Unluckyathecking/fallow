package daemon

import (
	"context"
	"errors"
	"log"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/coordinator"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

type HeartbeatClient interface {
	Heartbeat(context.Context, protocol.Heartbeat) (protocol.HeartbeatResponse, error)
}

type Preemptor interface {
	State() protocol.AgentState
}

type IdleDetector interface {
	SecondsSinceInput() (time.Duration, error)
}

type ReplicaSource interface {
	Statuses() []protocol.ReplicaStatus
}

type Metrics struct {
	CPUPercent     float64
	MemAvailableMB int
	LoadAvg        *float64
	TempCPUC       *float64
	GPUs           []protocol.GpuStatus
}

type HeartbeatLoop struct {
	client          HeartbeatClient
	agentID         string
	protocolVersion int
	interval        time.Duration
	preemptor       Preemptor
	idle            IdleDetector
	replicas        ReplicaSource
	leaseIDs        func() []string
	metrics         func() Metrics
	onResponse      func(protocol.HeartbeatResponse)
	onAuthError     func(error)
	now             func() time.Time
	sleep           coordinator.SleepFunc
	seq             int
}

func NewHeartbeatLoop(
	client HeartbeatClient,
	agentID string,
	protocolVersion int,
	interval time.Duration,
	preemptor Preemptor,
	idle IdleDetector,
	replicas ReplicaSource,
	leaseIDs func() []string,
	metrics func() Metrics,
	onResponse func(protocol.HeartbeatResponse),
	onAuthError func(error),
) *HeartbeatLoop {
	return &HeartbeatLoop{
		client: client, agentID: agentID, protocolVersion: protocolVersion, interval: interval,
		preemptor: preemptor, idle: idle, replicas: replicas, leaseIDs: leaseIDs,
		metrics: metrics, onResponse: onResponse, onAuthError: onAuthError,
		now: time.Now, sleep: sleepContext,
	}
}

func (loop *HeartbeatLoop) Seq() int {
	return loop.seq
}

func (loop *HeartbeatLoop) Run(ctx context.Context) error {
	for {
		keepGoing := loop.safeTick(ctx)
		if !keepGoing {
			return nil
		}
		if err := loop.sleep(ctx, loop.interval); err != nil {
			return err
		}
	}
}

func (loop *HeartbeatLoop) safeTick(ctx context.Context) (keepGoing bool) {
	keepGoing = true
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Printf("heartbeat iteration panicked: %v", recovered)
			keepGoing = true
		}
	}()
	return loop.tick(ctx)
}

func (loop *HeartbeatLoop) tick(ctx context.Context) bool {
	defer func() { loop.seq++ }()
	heartbeat, err := loop.buildHeartbeat()
	if err != nil {
		return true
	}
	response, err := loop.client.Heartbeat(ctx, heartbeat)
	if err == nil {
		loop.onResponse(response)
		return true
	}
	var auth *coordinator.AuthError
	if errors.As(err, &auth) {
		loop.onAuthError(err)
		return false
	}
	return true
}

func (loop *HeartbeatLoop) buildHeartbeat() (protocol.Heartbeat, error) {
	idle, err := loop.idle.SecondsSinceInput()
	if err != nil {
		return protocol.Heartbeat{}, err
	}
	if idle < 0 {
		idle = 0
	}
	metrics := loop.metrics()
	return protocol.Heartbeat{
		AgentID:         loop.agentID,
		CPUPercent:      metrics.CPUPercent,
		GPUs:            metrics.GPUs,
		LeaseIDs:        loop.leaseIDs(),
		LoadAvg:         metrics.LoadAvg,
		MemAvailableMB:  metrics.MemAvailableMB,
		ProtocolVersion: loop.protocolVersion,
		Replicas:        loop.replicas.Statuses(),
		SentAt:          loop.now(),
		Seq:             loop.seq,
		State:           loop.preemptor.State(),
		TempCPUC:        metrics.TempCPUC,
		UserIdleS:       idle.Seconds(),
	}, nil
}
