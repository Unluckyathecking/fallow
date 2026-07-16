package runtime

import (
	"context"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/supervisor"
)

// Coordinator is the subset of the coordinator client the daemon drives. The
// concrete *heartbeat.Client satisfies it; tests substitute a recording fake.
type Coordinator interface {
	Register(ctx context.Context, req protocol.RegisterRequest) (protocol.RegisterResponse, error)
	Heartbeat(ctx context.Context, hb protocol.Heartbeat) (protocol.HeartbeatResponse, error)
	PollWork(ctx context.Context, timeoutS float64) (*protocol.WorkUnitLease, error)
	PushEvent(ctx context.Context, event protocol.AgentEvent) error
	AgentID() string
	DeviceToken() string
}

// Supervisor is the replica supervisor the daemon composes: the preemptor's
// hot-path surface plus StopAll for shutdown. The concrete *supervisor.Supervisor
// satisfies it.
type Supervisor interface {
	preempt.ProcessSupervisor
	StopAll()
}

// Runner handles one leased work unit. There is no production default: until a
// Go worker is ported, no runner is wired and the work loop refuses to poll (see
// workLoop), because leasing a unit it cannot execute would consume the unit's
// attempts and dead-letter it.
type Runner interface {
	RunLease(ctx context.Context, lease protocol.WorkUnitLease) error
}

// Ticker is the injectable periodic clock the heartbeat and preempt loops wait
// on. Production wraps time.Ticker; tests fire the channel by hand.
type Ticker interface {
	Chan() <-chan time.Time
	Stop()
}

// Seams are the injectable collaborators. Every field has a production default
// filled by withDefaults; tests set only what they need to observe.
type Seams struct {
	// NewCoordinator builds a client for baseURL. A non-empty agentID/token seeds
	// an already-enrolled identity; both empty means "about to Register".
	NewCoordinator func(baseURL, agentID, deviceToken string) Coordinator
	// NewSupervisor builds the replica supervisor from its static config.
	NewSupervisor func(cfg supervisor.Config) (Supervisor, error)
	// Detector reports seconds since last user input.
	Detector idle.Detector
	// Runner executes a leased work unit. Nil disables work polling entirely.
	Runner Runner
	// Now is the wall clock for event and heartbeat timestamps.
	Now func() time.Time
	// Monotonic is the monotonic clock (seconds) the preempt controller measures
	// yield latency against.
	Monotonic func() float64
	// NewTicker builds a periodic ticker for the loops.
	NewTicker func(d time.Duration) Ticker
}

func (s Seams) withDefaults() Seams {
	if s.NewCoordinator == nil {
		s.NewCoordinator = defaultCoordinator
	}
	if s.NewSupervisor == nil {
		s.NewSupervisor = defaultSupervisor
	}
	if s.Detector == nil {
		// CreateDetector returns the OS detector (an honest unsupported stub on
		// Linux); a nil error still yields a usable value, and any construction
		// error is deferred to sample time where it is handled per loop.
		det, err := idle.CreateDetector(false, false)
		if err != nil || det == nil {
			det = unsupportedDetector{}
		}
		s.Detector = det
	}
	if s.Now == nil {
		s.Now = func() time.Time { return time.Now().UTC() }
	}
	if s.Monotonic == nil {
		s.Monotonic = defaultMonotonic
	}
	if s.NewTicker == nil {
		s.NewTicker = func(d time.Duration) Ticker { return realTicker{time.NewTicker(d)} }
	}
	return s
}

func defaultCoordinator(baseURL, agentID, deviceToken string) Coordinator {
	var opts []heartbeat.Option
	if agentID != "" || deviceToken != "" {
		opts = append(opts, heartbeat.WithIdentity(agentID, deviceToken))
	}
	return heartbeat.NewClient(baseURL, nil, opts...)
}

func defaultSupervisor(cfg supervisor.Config) (Supervisor, error) {
	return supervisor.New(cfg, supervisor.LlamaServerCommand(cfg))
}

var processStart = time.Now()

func defaultMonotonic() float64 { return time.Since(processStart).Seconds() }

// realTicker adapts *time.Ticker to Ticker.
type realTicker struct{ t *time.Ticker }

func (r realTicker) Chan() <-chan time.Time { return r.t.C }
func (r realTicker) Stop()                  { r.t.Stop() }

// unsupportedDetector is the fallback when no OS idle detector exists; it always
// reports unsupported so the loops treat the host as headless (never active).
type unsupportedDetector struct{}

func (unsupportedDetector) SecondsSinceInput() (float64, error) { return 0, idle.ErrUnsupported }
