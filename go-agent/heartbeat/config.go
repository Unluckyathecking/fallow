package heartbeat

import "time"

// RetryConfig is the retry policy for idempotent coordinator calls (heartbeat,
// poll_work). It mirrors fallow_agent.heartbeat.config.ClientRetryConfig.
//
// MaxRetries is the number of retries after the initial attempt (so a value of
// 2 permits at most three total attempts). BackoffBase is the first backoff;
// each subsequent retry doubles it (exponential backoff).
type RetryConfig struct {
	MaxRetries  int
	BackoffBase time.Duration
}

// DefaultRetryConfig returns the client's default retry policy.
func DefaultRetryConfig() RetryConfig {
	return RetryConfig{MaxRetries: defaultMaxRetries, BackoffBase: defaultBackoffBase}
}
