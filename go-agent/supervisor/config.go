// Package supervisor owns fallow-launched inference child processes
// (llama-server, faster-whisper workers): it spawns them, gates them to READY
// with an HTTP /health probe, suspends and resumes them instantly on the
// preemption hot path, and stops them gracefully.
//
// It is a Go port of the Python fallow_agent.supervisor package and keeps the
// same lifecycle semantics. A single mutex guards the state maps and the cached
// status slice only; every blocking operation — spawning, suspend/resume
// syscalls, process wait/kill, /health probes, goroutine joins — runs outside
// the lock, so SuspendAll and ResumeAll stay hot-path fast (sub-millisecond,
// no network).
package supervisor

import (
	"errors"
	"time"
)

// Default configuration values. These mirror the Python supervisor defaults so
// both agents behave identically.
const (
	DefaultBindHost           = "127.0.0.1"
	DefaultStartupTimeout     = 180 * time.Second
	DefaultHealthPollInterval = 500 * time.Millisecond
	DefaultHealthTimeout      = 1 * time.Second
	DefaultHealthPath         = "/health"
	DefaultStopGrace          = 5 * time.Second
	DefaultParallel           = 2
	DefaultContextSize        = 8192
	DefaultGPULayers          = 999
	ForbiddenBindHost         = "0.0.0.0" // named to reject, never to bind to
)

// ErrForbiddenBindHost is returned by Config.Validate when BindHost is the
// wildcard address. llama-server has no authentication, so binding to all
// interfaces would expose an open inference endpoint.
var ErrForbiddenBindHost = errors.New(
	"bind_host must not be 0.0.0.0: llama-server has no auth; " +
		"bind to loopback or the tailnet interface only",
)

// Config is the immutable static configuration for the supervisor.
//
// LlamaBinary is the path to the llama-server executable. BindHost is the
// interface replicas bind to (loopback or a tailnet IP only). StartupTimeout is
// the maximum time a replica may stay LOADING before it is killed and marked
// STOPPED. HealthPollInterval is the delay between /health polls and the crash
// detection granularity once a replica is READY. HealthTimeout is the per
// request timeout for a single /health probe. HealthPath is the HTTP path
// polled for readiness. StopGrace is the grace period after terminate before
// kill. Parallel, ContextSize, and GPULayers map to llama-server's --parallel,
// -c, and -ngl flags.
type Config struct {
	LlamaBinary        string
	BindHost           string
	StartupTimeout     time.Duration
	HealthPollInterval time.Duration
	HealthTimeout      time.Duration
	HealthPath         string
	StopGrace          time.Duration
	Parallel           int
	ContextSize        int
	GPULayers          int
}

// DefaultConfig returns a Config populated with the default tunables for the
// given llama-server binary path. Callers override individual fields as needed.
func DefaultConfig(llamaBinary string) Config {
	return Config{
		LlamaBinary:        llamaBinary,
		BindHost:           DefaultBindHost,
		StartupTimeout:     DefaultStartupTimeout,
		HealthPollInterval: DefaultHealthPollInterval,
		HealthTimeout:      DefaultHealthTimeout,
		HealthPath:         DefaultHealthPath,
		StopGrace:          DefaultStopGrace,
		Parallel:           DefaultParallel,
		ContextSize:        DefaultContextSize,
		GPULayers:          DefaultGPULayers,
	}
}

// Validate reports whether the configuration is usable. It rejects the wildcard
// bind host; every other field is trusted (the caller owns ports and paths).
func (c Config) Validate() error {
	if c.BindHost == ForbiddenBindHost {
		return ErrForbiddenBindHost
	}
	return nil
}
