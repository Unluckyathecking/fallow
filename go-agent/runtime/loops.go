package runtime

import (
	"context"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// heartbeatLoop sends a heartbeat every configured interval. It is un-killable
// by anything short of an auth rejection: transient and protocol failures are
// logged and the loop keeps beating (ADR 009). An auth rejection is surfaced as
// fatal and stops the daemon.
func (r *Runtime) heartbeatLoop(ctx context.Context) {
	ticker := r.seams.NewTicker(seconds(r.cfg.HeartbeatIntervalS))
	defer ticker.Stop()
	for {
		if !r.sendHeartbeat(ctx, r.beatSeq()) {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.Chan():
		}
	}
}

// sendHeartbeat sends one beat. It returns false only when the loop must stop
// (an auth rejection, surfaced as fatal).
func (r *Runtime) sendHeartbeat(ctx context.Context, seq int) bool {
	resp, err := r.client.Heartbeat(ctx, r.buildHeartbeat(seq))
	if err != nil {
		if isAuthError(err) {
			logf("heartbeat auth rejected; stopping: %v", err)
			r.fatal(err)
			return false
		}
		logf("heartbeat failed (transient/protocol): %v", err)
		return true
	}
	if r.site != nil {
		// Every response drives reconciliation, including an empty set (which
		// removes all replicas). The worker coalesces to the newest desired set.
		r.site.submitDesired(resp.DesiredModels)
	} else if len(resp.DesiredModels) > 0 {
		logf("coordinator desires models: %v", resp.DesiredModels)
	}
	return true
}

// preemptLoop drives the preemption state machine one tick per poll interval:
// sample the idle detector, then advance the controller. A detector that reports
// unsupported (a headless host) is skipped so the machine never falsely flips to
// active. The loop never dies on a per-iteration error.
func (r *Runtime) preemptLoop(ctx context.Context) {
	ticker := r.seams.NewTicker(millis(r.cfg.PollIntervalMs))
	defer ticker.Stop()
	reclaimed := false
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.Chan():
		}
		// The user's explicit takedown wins over automatic preemption. While
		// reclaimed the machine belongs to them, so skip the idle-driven state
		// machine entirely — nothing may resume serving until an explicit
		// release, regardless of detected idleness.
		nowReclaimed := r.reclaim.OnPoll()
		if nowReclaimed != reclaimed {
			logReclaimEdge(nowReclaimed)
			r.onReclaimEdge(nowReclaimed)
			reclaimed = nowReclaimed
		}
		// Drive the preemption state machine first, so a returning user flips the
		// availability view to active (via the sequencing sink) before READY is
		// ever exposed on this tick. Publishing replica readiness before sampling
		// presence could momentarily offer a claim on the first startup poll while
		// the user is actually active but not yet detected.
		if !nowReclaimed {
			if idleS, ok := r.sampleIdle(); ok {
				r.controller.OnPoll(idleS, r.seams.Monotonic())
			}
		}
		// Then publish the availability inputs the claim runner reads: reclaim
		// state and READY replica presence gate whether claims may be served.
		if r.site != nil {
			r.site.availability.setReclaimed(nowReclaimed)
			r.site.availability.setReplicaReady(hasReadyReplica(r.supervisor.Statuses()))
			r.site.nudge() // re-apply any deferred reconcile once eligible again
		}
	}
}

// onReclaimEdge publishes the durable presence transition a reclaim edge needs
// in Site Mode. Reclaim runs outside the preemption state machine, so nothing
// else advances the coordinator's presence generation for it. On release the
// machine returns to normal idle-based serving; without a sequenced user_idle
// event the persisted route generation would stay behind the broker's fence
// (raised by the serving_paused heartbeats during reclaim) and every claim would
// be rejected as stale. Emitting user_idle on release advances the generation to
// match the fence so serving resumes. Engagement needs no event: the immediate
// availability flip cancels in-flight claims and the serving_paused heartbeats
// fence the broker.
func (r *Runtime) onReclaimEdge(reclaimed bool) {
	if r.presenceSink == nil || reclaimed {
		return
	}
	r.presenceSink.Emit(protocol.AgentEvent{
		AgentID: r.client.AgentID(),
		Kind:    protocol.EventKindUserIdle,
		At:      r.seams.Now(),
	})
}

func logReclaimEdge(reclaimed bool) {
	if reclaimed {
		logf("reclaimed: user took the machine; serving paused until release")
		return
	}
	logf("released: normal idle-based serving restored")
}

// workLoop long-polls for batch work while the machine is IDLE and hands each
// lease to the runner. While the user is active it does no work at all — it
// sleeps cheaply and re-checks — so the machine is never touched.
//
// It refuses to poll unless a runner is wired. Leasing a unit increments its
// attempt (the coordinator's CLAIM_UNIT), and a unit leased and dropped four
// times is dead-lettered, so an agent that cannot execute work must never lease
// it. With no runner the loop simply waits for shutdown.
func (r *Runtime) workLoop(ctx context.Context) {
	if r.seams.Runner == nil {
		logf("work polling disabled: no work runner is wired")
		<-ctx.Done()
		return
	}
	for {
		if ctx.Err() != nil {
			return
		}
		if r.controller.State() != protocol.AgentStateIdle {
			if !sleepCtx(ctx, seconds(r.settings.ActiveSleepS)) {
				return
			}
			continue
		}
		lease, err := r.client.PollWork(ctx, r.settings.WorkPollTimeoutS)
		if err != nil {
			if isAuthError(err) {
				logf("work poll auth rejected; stopping: %v", err)
				r.fatal(err)
				return
			}
			if !sleepCtx(ctx, seconds(r.settings.ActiveSleepS)) {
				return
			}
			continue
		}
		if lease == nil {
			continue // 204: no work available
		}
		if err := r.seams.Runner.RunLease(ctx, *lease); err != nil {
			logf("run lease %s failed: %v", lease.WorkUnitID, err)
		}
	}
}

// sleepCtx sleeps for d unless ctx is cancelled first. It returns false if the
// sleep was cut short by cancellation.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func seconds(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}

func millis(ms int) time.Duration {
	return time.Duration(ms) * time.Millisecond
}
