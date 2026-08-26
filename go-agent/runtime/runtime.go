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
	"github.com/Unluckyathecking/fallow/go-agent/hostinfo"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/supervisor"
)

const (
	awayIdleS         = 300.0 // reported when idle detection is unsupported
	finalHeartbeatTTL = 3 * time.Second
)

// Runtime builds, runs, and gracefully stops the per-machine Go agent.
type Runtime struct {
	settings config.Settings
	seams    Seams

	// Wired during Run.
	client       Coordinator
	supervisor   Supervisor
	controller   *preempt.Controller
	reclaim      *preempt.ReclaimController
	sink         *eventSink
	presenceSink preempt.EventSink // sequencing sink for Site Mode reclaim events
	cfg          protocol.AgentConfig
	site         *siteRuntime // nil for direct agents

	// metrics samples live CPU and memory for each heartbeat. Its zero value is
	// ready to use; it holds the previous CPU reading so each beat reports the
	// interval since the last one.
	metrics hostinfo.Sampler

	seq seqSource
	// presenceMu serialises sequence handout for Site Mode presence events and
	// heartbeats so the wire order matches the sequence order. It is held only
	// briefly, never across network I/O, and never together with the controller
	// lock, so it adds no hot-path or deadlock risk.
	presenceMu sync.Mutex
	// primed becomes true once the poll loop has taken its first authoritative
	// presence and reclaim sample. Until then reconciliation is held ineligible,
	// so a heartbeat arriving before the first poll cannot act on the constructor
	// defaults (IDLE, unreclaimed) and start work for an active or reclaimed user.
	primed    atomic.Bool
	fatalOnce sync.Once
	fatalErr  error
	cancel    context.CancelFunc
}

// beatSeq allocates the next heartbeat sequence. In Site Mode it takes the
// presence lock and then flushes queued presence events, guaranteeing every
// event stamped with a lower sequence is on the wire before this heartbeat is
// sent — so a heartbeat can never overtake and orphan a queued presence event.
func (r *Runtime) beatSeq() int {
	if r.site == nil {
		return r.nextSeq()
	}
	r.presenceMu.Lock()
	seq := r.nextSeq()
	r.presenceMu.Unlock()
	r.sink.flush()
	return seq
}

// nextSeq returns the next value of the monotonic sequence shared by heartbeats
// and presence events. Direct agents reset it per process; Site Mode resumes it
// above the persisted high-water mark so the coordinator's fence never regresses
// across a restart.
func (r *Runtime) nextSeq() int { return r.seq.next() }

// New builds a Runtime from settings and seams. Nil seam fields take production
// defaults, so New(settings, Seams{}) is the production constructor.
func New(settings config.Settings, seams Seams) *Runtime {
	return &Runtime{settings: settings, seams: seams.withDefaults()}
}

// Run enrolls or resumes, starts the loops, and blocks until ctx is cancelled
// (SIGINT/SIGTERM from the caller) or a fatal auth rejection fires, then stops
// cleanly. It returns the fatal error, if any.
func (r *Runtime) Run(ctx context.Context) error {
	sup, err := r.seams.NewSupervisor(r.supervisorConfig())
	if err != nil {
		return err
	}
	r.supervisor = sup

	w, err := r.resolveWiring(ctx)
	if err != nil {
		return err
	}
	r.client = w.client
	r.cfg = w.cfg
	r.seq = w.seq
	r.site = w.site

	// Site Mode stamps a shared sequence onto presence events and cancels claims
	// at a transition; direct agents keep their plain, unfenced event sink.
	r.sink = newEventSink(w.client)
	var controllerSink preempt.EventSink = r.sink
	if r.site != nil {
		// A presence transition that cannot be delivered would strand serving, so
		// fail closed rather than let a later heartbeat overtake it.
		r.sink.onFatal = func(err error) { r.fatal(err) }
		controllerSink = &sequencingSink{inner: r.sink, seq: r.seq, avail: r.site.availability, presence: &r.presenceMu}
		r.presenceSink = controllerSink
	}
	r.controller = preempt.NewController(sup, controllerSink, r.cfg, w.client.AgentID(), preempt.Options{
		Monotonic: r.seams.Monotonic,
		Now:       r.seams.Now,
	})
	r.reclaim = preempt.NewReclaimController(sup, preempt.ReclaimControlPath(r.settings.StatePath), preempt.ReclaimOptions{})
	if r.site != nil {
		// Gate reconciliation on serving-eligibility now that the controllers
		// exist: never start a replica while the user is active or the machine is
		// reclaimed (VRAM eviction happens while active, so idle covers it too).
		// The reconciler's supervisor is also guarded to re-check this immediately
		// before every start, closing the window where a user returns during a
		// slow cache download after the worker's outer check.
		r.site.eligible = r.servingEligible
	}

	loopCtx, cancel := context.WithCancel(ctx)
	r.cancel = cancel
	defer cancel()

	r.sink.start()
	logf("started (agent_id=%s)", w.client.AgentID())

	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); r.heartbeatLoop(loopCtx) }()
	go func() { defer wg.Done(); r.preemptLoop(loopCtx) }()
	go func() { defer wg.Done(); r.workLoop(loopCtx) }()
	if r.site != nil {
		wg.Add(2)
		// The claim runner and reconcile worker join the loop wait group so both
		// stop before the supervisor's replicas are torn down in shutdown.
		go func() { defer wg.Done(); r.site.reconcileWorker(loopCtx) }()
		go func() {
			defer wg.Done()
			// The claim runner is the additive serving path. It is supervised so a
			// transient relay/transport error (a coordinator restart) resumes held
			// polling after a bounded backoff instead of stopping serving for good;
			// context cancellation is terminal, and a genuine auth rejection is
			// surfaced fatally by the heartbeat loop, which shares the same device
			// token and cancels this context.
			superviseClaimRunner(loopCtx, func(ctx context.Context) error {
				return r.site.runner.Run(ctx, r.site.availability, r.site.replicas)
			})
		}()
	}

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

// servingEligible reports whether the machine may start replicas and serve
// claims right now: idle and not reclaimed. VRAM eviction happens while active,
// so requiring idle covers it too. It reads live controller state and is
// nil-safe before the controllers exist (reported not eligible, failing safe).
func (r *Runtime) servingEligible() bool {
	return r.primed.Load() && r.controller != nil && r.controller.State() == protocol.AgentStateIdle &&
		r.reclaim != nil && !r.reclaim.IsReclaimed()
}

// sendFinalHeartbeat sends one last DRAINING heartbeat on a fresh, short-lived
// context (the run context is already cancelled). Failure is logged, not fatal.
func (r *Runtime) sendFinalHeartbeat() {
	ctx, cancel := context.WithTimeout(context.Background(), finalHeartbeatTTL)
	defer cancel()
	if _, err := r.client.Heartbeat(ctx, r.buildHeartbeat(r.beatSeq())); err != nil {
		logf("final heartbeat failed: %v", err)
	}
}

// buildHeartbeat assembles one Heartbeat from the live component state.
func (r *Runtime) buildHeartbeat(seq int) protocol.Heartbeat {
	host := r.metrics.Sample()
	return protocol.Heartbeat{
		AgentID:         r.client.AgentID(),
		Seq:             seq,
		SentAt:          r.seams.Now(),
		ProtocolVersion: protocolVersion,
		State:           r.controller.State(),
		UserIdleS:       r.idleOrAway(),
		CPUPercent:      host.CPUPercent,
		MemAvailableMB:  host.MemAvailableMB,
		Replicas:        r.supervisor.Statuses(),
		ServingPaused:   r.reclaim.IsReclaimed(),
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
