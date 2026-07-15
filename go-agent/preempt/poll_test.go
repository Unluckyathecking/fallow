package preempt

import (
	"errors"
	"sync"
	"testing"
	"time"
)

type testDetector struct {
	mu    sync.Mutex
	idle  time.Duration
	err   error
	calls int
}

func (d *testDetector) SecondsSinceInput() (time.Duration, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls++
	return d.idle, d.err
}

func (d *testDetector) Calls() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}

type recordingPollController struct {
	mu    sync.Mutex
	polls []poll
}

type poll struct {
	idle time.Duration
	now  time.Time
}

func (c *recordingPollController) OnPoll(idle time.Duration, now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.polls = append(c.polls, poll{idle: idle, now: now})
}

func (c *recordingPollController) Polls() []poll {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]poll(nil), c.polls...)
}

type discardLogger struct{}

func (discardLogger) Printf(string, ...any) {}

func TestPollLoopSurvivesDetectorErrors(t *testing.T) {
	detector := &testDetector{err: errors.New("detector boom")}
	controller := &recordingPollController{}
	loop := newPollLoop(detector, controller, time.Millisecond, time.Now, discardLogger{})
	loop.Start()
	waitFor(t, func() bool { return detector.Calls() >= 3 })
	loop.Stop()
	if len(controller.Polls()) != 0 {
		t.Fatalf("polls = %#v", controller.Polls())
	}
}

func TestPollLoopForwardsIdleAndMonotonic(t *testing.T) {
	detector := &testDetector{idle: 3500 * time.Millisecond}
	controller := &recordingPollController{}
	loop := newPollLoop(
		detector, controller, time.Millisecond,
		func() time.Time { return fixedTime }, discardLogger{},
	)
	loop.Start()
	waitFor(t, func() bool { return len(controller.Polls()) >= 1 })
	loop.Stop()
	got := controller.Polls()[0]
	if got.idle != 3500*time.Millisecond || !got.now.Equal(fixedTime) {
		t.Fatalf("poll = %#v", got)
	}
}

func TestPollLoopStopBeforeStartIsSafe(t *testing.T) {
	loop := newPollLoop(
		&testDetector{}, &recordingPollController{}, time.Millisecond, time.Now, discardLogger{},
	)
	loop.Stop()
}

func waitFor(t *testing.T, predicate func() bool) {
	t.Helper()
	deadline := time.Now().Add(400 * time.Millisecond)
	for time.Now().Before(deadline) {
		if predicate() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition did not become true")
}
