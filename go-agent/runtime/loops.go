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
		if !r.sendHeartbeat(ctx, r.nextSeq()) {
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
	if len(resp.DesiredModels) > 0 {
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
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.Chan():
		}
		idleS, ok := r.sampleIdle()
		if !ok {
			continue // unsupported or non-finite: never drive the machine on it
		}
		r.controller.OnPoll(idleS, r.seams.Monotonic())
	}
}

// workLoop long-polls for batch work while the machine is IDLE and hands each
// lease to the runner. While the user is active it does no work at all — it
// sleeps cheaply and re-checks — so the machine is never touched.
func (r *Runtime) workLoop(ctx context.Context) {
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
