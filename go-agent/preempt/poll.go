package preempt

import (
	"log"
	"sync"
	"sync/atomic"
	"time"
)

type IdleDetector interface {
	SecondsSinceInput() (time.Duration, error)
}

type Logger interface {
	Printf(string, ...any)
}

type PollLoop struct {
	detector   IdleDetector
	controller pollController
	period     time.Duration
	monotonic  func() time.Time
	logger     Logger
	stop       chan struct{}
	done       chan struct{}
	startOnce  sync.Once
	stopOnce   sync.Once
	doneOnce   sync.Once
	started    atomic.Bool
}

type pollController interface {
	OnPoll(time.Duration, time.Time)
}

type ControllerAdapter struct {
	Controller *Controller
}

func (a ControllerAdapter) OnPoll(idle time.Duration, now time.Time) {
	a.Controller.OnPoll(idle, now)
}

func NewPollLoop(detector IdleDetector, controller *Controller, interval time.Duration) *PollLoop {
	return newPollLoop(detector, ControllerAdapter{Controller: controller}, interval, time.Now, log.Default())
}

func newPollLoop(
	detector IdleDetector,
	controller pollController,
	interval time.Duration,
	monotonic func() time.Time,
	logger Logger,
) *PollLoop {
	return &PollLoop{
		detector: detector, controller: controller, period: interval,
		monotonic: monotonic, logger: logger, stop: make(chan struct{}), done: make(chan struct{}),
	}
}

func (loop *PollLoop) Start() {
	loop.startOnce.Do(func() {
		loop.started.Store(true)
		go loop.run()
	})
}

func (loop *PollLoop) Stop() {
	loop.stopOnce.Do(func() { close(loop.stop) })
	if !loop.started.Load() {
		loop.doneOnce.Do(func() { close(loop.done) })
	}
	<-loop.done
}

func (loop *PollLoop) run() {
	defer loop.doneOnce.Do(func() { close(loop.done) })
	for {
		select {
		case <-loop.stop:
			return
		default:
		}
		started := loop.monotonic()
		loop.safePoll(started)
		remaining := loop.period - loop.monotonic().Sub(started)
		if remaining <= 0 {
			loop.logger.Printf("preempt poll overrun by %.1f ms", float64(-remaining)/float64(time.Millisecond))
			continue
		}
		timer := time.NewTimer(remaining)
		select {
		case <-loop.stop:
			if !timer.Stop() {
				<-timer.C
			}
			return
		case <-timer.C:
		}
	}
}

func (loop *PollLoop) safePoll(now time.Time) {
	defer func() {
		if recovered := recover(); recovered != nil {
			loop.logger.Printf("preempt poll iteration panicked: %v", recovered)
		}
	}()
	idle, err := loop.detector.SecondsSinceInput()
	if err != nil {
		loop.logger.Printf("preempt poll iteration failed: %v", err)
		return
	}
	loop.controller.OnPoll(idle, now)
}
