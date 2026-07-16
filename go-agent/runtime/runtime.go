// Package runtime is the Go agent daemon: the top-level loop that composes the
// existing building blocks — the coordinator client, the preemption controller,
// the process supervisor, and the idle detector — into a persistent agent.
//
// It mirrors the Python fallow_agent.main.runtime/AgentRuntime structure: build
// the agent (enroll or resume, wire everything), start the loops, block until a
// shutdown signal or a fatal auth rejection, then tear down gracefully in the
// ADR-015 order (drain the preemptor, stop the loops, send one final DRAINING
// heartbeat, stop the replicas, flush the event sink). It owns no protocol logic
// of its own; every wire call goes through the packages it composes.
package runtime

import (
	"context"
	"errors"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/supervisor"
)

// Static telemetry placeholders. A Go host-metrics probe is future work; the
// coordinator does not gate on these fields today, and the one-shot subcommands
// send fixed values too.
const (
	staticCPUPercent  = 5.0
	staticMemAvailMB  = 8192
	awayIdleS         = 300.0 // reported when idle detection is unsupported
	finalHeartbeatTTL = 3 * time.Second
)

// Runtime builds, runs, and gracefully stops the per-machine Go agent.
type Runtime struct {
	settings config.Settings
	seams    Seams

	// Wired during Run.
	client     Coordinator
	supervisor Supervisor
	controller *preempt.Controller
	sink       *eventSink
	cfg        protocol.AgentConfig

	seq       atomic.Int64
	fatalOnce sync.Once
	fatalErr  error
	cancel    context.CancelFunc
}

// nextSeq returns the next monotonic heartbeat sequence number, starting at 0.
// It is shared by the heartbeat loop and the final shutdown beat so every beat
// carries a distinct, non-negative seq.
func (r *Runtime) nextSeq() int { return int(r.seq.Add(1) - 1) }

// New builds a Runtime from settings and seams. Nil seam fields take production
// defaults, so New(settings, Seams{}) is the production constructor.
func New(settings config.Settings, seams Seams) *Runtime {
	return &Runtime{settings: settings, seams: seams.withDefaults()}
}

// Run enrolls or resumes, starts the loops, and blocks until ctx is cancelled
// (SIGINT/SIGTERM from the caller) or a fatal auth rejection fires, then stops
// cleanly. It returns the fatal error, if any.
func (r *Runtime) Run(ctx context.Context) error {
	client, cfg, err := resolveIdentity(ctx, r.settings, r.seams)
	if err != nil {
		return err
	}
	sup, err := r.seams.NewSupervisor(r.supervisorConfig())
	if err != nil {
		return err
	}
	r.client = client
	r.cfg = cfg
	r.supervisor = sup
	r.sink = newEventSink(client)
	r.controller = preempt.NewController(sup, r.sink, cfg, client.AgentID(), preempt.Options{
		Monotonic: r.seams.Monotonic,
		Now:       r.seams.Now,
	})

	loopCtx, cancel := context.WithCancel(ctx)
	r.cancel = cancel
	defer cancel()

	r.sink.start()
	logf("started (agent_id=%s)", client.AgentID())

	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); r.heartbeatLoop(loopCtx) }()
	go func() { defer wg.Done(); r.preemptLoop(loopCtx) }()
	go func() { defer wg.Done(); r.workLoop(loopCtx) }()

	<-loopCtx.Done()
	logf("shutting down")
	r.shutdown(&wg)
	return r.fatalErr
}

// shutdown runs the graceful teardown in the ADR-015 order.
func (r *Runtime) shutdown(wg *sync.WaitGroup) {
	r.controller.Drain() // stop accepting work, emit agent_stopping
	r.cancel()           // ensure the loops observe the stop
	wg.Wait()
	r.sendFinalHeartbeat() // one DRAINING beat, best effort
	r.supervisor.StopAll()
	r.sink.close() // flush queued events, including agent_stopping
}

// fatal records the first fatal error and cancels the loops. Subsequent calls
// are no-ops.
func (r *Runtime) fatal(err error) {
	r.fatalOnce.Do(func() {
		r.fatalErr = err
		if r.cancel != nil {
			r.cancel()
		}
	})
}

func (r *Runtime) supervisorConfig() supervisor.Config {
	cfg := supervisor.DefaultConfig(r.settings.LlamaServerBinary)
	cfg.BindHost = r.settings.BindHost
	return cfg
}

// sendFinalHeartbeat sends one last DRAINING heartbeat on a fresh, short-lived
// context (the run context is already cancelled). Failure is logged, not fatal.
func (r *Runtime) sendFinalHeartbeat() {
	ctx, cancel := context.WithTimeout(context.Background(), finalHeartbeatTTL)
	defer cancel()
	if _, err := r.client.Heartbeat(ctx, r.buildHeartbeat(r.nextSeq())); err != nil {
		logf("final heartbeat failed: %v", err)
	}
}

// buildHeartbeat assembles one Heartbeat from the live component state.
func (r *Runtime) buildHeartbeat(seq int) protocol.Heartbeat {
	return protocol.Heartbeat{
		AgentID:         r.client.AgentID(),
		Seq:             seq,
		SentAt:          r.seams.Now(),
		ProtocolVersion: protocolVersion,
		State:           r.controller.State(),
		UserIdleS:       r.idleOrAway(),
		CPUPercent:      staticCPUPercent,
		MemAvailableMB:  staticMemAvailMB,
		Replicas:        r.supervisor.Statuses(),
	}
}

// idleOrAway samples the idle detector for a heartbeat, falling back to a large
// "away" value when idle detection is unavailable (a headless host is treated as
// idle rather than shipping a bogus number to the coordinator).
func (r *Runtime) idleOrAway() float64 {
	if s, ok := r.sampleIdle(); ok {
		return s
	}
	return awayIdleS
}

// sampleIdle reads seconds-since-input, reporting ok=false when the detector is
// unsupported or returns a non-finite value (some OS APIs yield NaN off a GUI
// session). A negative reading is clamped to zero. Both the heartbeat and the
// preempt loop go through here so a bad sample never drives the state machine or
// reaches the wire.
func (r *Runtime) sampleIdle() (float64, bool) {
	s, err := r.seams.Detector.SecondsSinceInput()
	if err != nil || math.IsNaN(s) || math.IsInf(s, 0) {
		return 0, false
	}
	if s < 0 {
		return 0, true
	}
	return s, true
}

// isAuthError reports whether err is a coordinator auth rejection.
func isAuthError(err error) bool {
	var authErr *heartbeat.AuthError
	return errors.As(err, &authErr)
}
