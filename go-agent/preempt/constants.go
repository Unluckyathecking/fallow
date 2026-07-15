package preempt

// Named constants for the preemption state machine — no magic numbers in the
// hot path.
const (
	// msPerSecond converts poll periods and yield latencies to milliseconds.
	msPerSecond = 1000.0

	// yieldMSKey carries the measured yield latency on a user_returned event.
	yieldMSKey = "yield_ms"
)
