package preempt

import (
	"reflect"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

var fixedTime = time.Date(2026, 7, 15, 12, 0, 0, 0, time.UTC)

type recordingSupervisor struct {
	log      *[]string
	statuses []protocol.ReplicaStatus
	stopped  []string
}

func (s *recordingSupervisor) SuspendAll() {
	*s.log = append(*s.log, "suspend_all")
}

func (s *recordingSupervisor) ResumeAll() {
	*s.log = append(*s.log, "resume_all")
}

func (s *recordingSupervisor) Stop(modelID string) {
	*s.log = append(*s.log, "stop_replica:"+modelID)
	s.stopped = append(s.stopped, modelID)
}

func (s *recordingSupervisor) Statuses() []protocol.ReplicaStatus {
	return s.statuses
}

type recordingSink struct {
	log    *[]string
	events []protocol.AgentEvent
}

func (s *recordingSink) Emit(event protocol.AgentEvent) {
	*s.log = append(*s.log, "emit:"+string(event.Kind))
	s.events = append(s.events, event)
}

func testConfig() protocol.AgentConfig {
	return protocol.AgentConfig{
		IdleThresholdS: 120, PollIntervalMs: 100, VRAMEvictAfterS: 60,
	}
}

func makeController(
	statuses []protocol.ReplicaStatus,
	monotonic func() time.Time,
) (*Controller, *[]string, *recordingSupervisor, *recordingSink) {
	log := []string{}
	supervisor := &recordingSupervisor{log: &log, statuses: statuses}
	sink := &recordingSink{log: &log}
	controller := newController(
		supervisor, sink, testConfig(), "agent-1", monotonic, func() time.Time { return fixedTime },
	)
	return controller, &log, supervisor, sink
}

func TestFreshInputSuspendsBeforeEmit(t *testing.T) {
	controller, log, _, sink := makeController(nil, func() time.Time { return fixedTime })
	state := controller.OnPoll(0, fixedTime)
	if state != protocol.AgentStateActive {
		t.Fatalf("state = %s", state)
	}
	if !reflect.DeepEqual(*log, []string{"suspend_all", "emit:user_returned"}) {
		t.Fatalf("call order = %#v", *log)
	}
	if sink.events[0].AgentID != "agent-1" {
		t.Fatalf("event = %#v", sink.events[0])
	}
}

func TestYieldMillisecondsUseInjectedMonotonic(t *testing.T) {
	controller, _, _, sink := makeController(nil, func() time.Time {
		return fixedTime.Add(50 * time.Millisecond)
	})
	controller.OnPoll(0, fixedTime)
	if sink.events[0].Detail[yieldMSKey] != "50.000" {
		t.Fatalf("yield_ms = %q", sink.events[0].Detail[yieldMSKey])
	}
}

func TestFreshInputDetection(t *testing.T) {
	t.Run("stale startup stays idle", func(t *testing.T) {
		controller, log, _, _ := makeController(nil, func() time.Time { return fixedTime })
		if controller.OnPoll(5*time.Second, fixedTime) != protocol.AgentStateIdle {
			t.Fatal("stale startup changed state")
		}
		if len(*log) != 0 {
			t.Fatalf("calls = %#v", *log)
		}
	})

	t.Run("idle counter drop is fresh", func(t *testing.T) {
		controller, _, _, _ := makeController(nil, func() time.Time { return fixedTime })
		controller.OnPoll(5*time.Second, fixedTime)
		controller.OnPoll(5100*time.Millisecond, fixedTime.Add(100*time.Millisecond))
		if controller.OnPoll(2*time.Second, fixedTime.Add(200*time.Millisecond)) != protocol.AgentStateActive {
			t.Fatal("idle drop did not activate")
		}
	})

	for _, idle := range []time.Duration{0, 50 * time.Millisecond} {
		t.Run(idle.String(), func(t *testing.T) {
			controller, _, _, _ := makeController(nil, func() time.Time { return fixedTime })
			if controller.OnPoll(idle, fixedTime) != protocol.AgentStateActive {
				t.Fatalf("idle %s did not activate", idle)
			}
		})
	}
}

func TestHysteresisRequiresContinuousIdleThreshold(t *testing.T) {
	controller, log, supervisor, _ := makeController(nil, func() time.Time { return fixedTime })
	controller.OnPoll(0, fixedTime)
	if controller.OnPoll(time.Second, fixedTime.Add(time.Second)) != protocol.AgentStateActive {
		t.Fatal("brief pause resumed work")
	}
	if contains(*log, "resume_all") || len(supervisor.stopped) != 0 {
		t.Fatalf("calls = %#v stopped = %#v", *log, supervisor.stopped)
	}
	if controller.OnPoll(120*time.Second, fixedTime.Add(120*time.Second)) != protocol.AgentStateIdle {
		t.Fatal("threshold did not resume work")
	}
	if !reflect.DeepEqual((*log)[len(*log)-2:], []string{"resume_all", "emit:user_idle"}) {
		t.Fatalf("tail = %#v", (*log)[len(*log)-2:])
	}
}

func TestEscalationStopsOnlySuspendedGPUReplicasOnce(t *testing.T) {
	statuses := []protocol.ReplicaStatus{
		{ModelID: "a", State: protocol.ReplicaStateSuspended, GPU: true},
		{ModelID: "c", State: protocol.ReplicaStateSuspended, GPU: true},
		{ModelID: "cpu-only", State: protocol.ReplicaStateSuspended, GPU: false},
		{ModelID: "b", State: protocol.ReplicaStateStopped, GPU: true},
	}
	controller, _, supervisor, _ := makeController(statuses, func() time.Time { return fixedTime })
	controller.OnPoll(0, fixedTime)
	controller.OnPoll(time.Second, fixedTime.Add(59*time.Second))
	if len(supervisor.stopped) != 0 {
		t.Fatalf("early stops = %#v", supervisor.stopped)
	}
	controller.OnPoll(time.Second, fixedTime.Add(61*time.Second))
	controller.OnPoll(time.Second, fixedTime.Add(62*time.Second))
	if !reflect.DeepEqual(supervisor.stopped, []string{"a", "c"}) {
		t.Fatalf("stopped = %#v", supervisor.stopped)
	}
}

func TestDrainingIsTerminalAndIdempotent(t *testing.T) {
	controller, log, _, sink := makeController(nil, func() time.Time { return fixedTime })
	controller.Drain()
	controller.Drain()
	baseline := append([]string(nil), (*log)...)
	if controller.OnPoll(0, fixedTime) != protocol.AgentStateDraining {
		t.Fatal("draining was not terminal")
	}
	if !reflect.DeepEqual(*log, baseline) {
		t.Fatalf("calls after drain = %#v", *log)
	}
	if len(sink.events) != 1 || sink.events[0].Kind != protocol.EventKindAgentStopping {
		t.Fatalf("events = %#v", sink.events)
	}
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}
