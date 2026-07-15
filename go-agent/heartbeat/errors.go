package heartbeat

import "fmt"

// The error hierarchy mirrors fallow_agent.heartbeat.errors so callers react by
// class of failure without string-matching:
//
//   - AuthError: the coordinator rejected our identity (401/403). Retrying with
//     the same token is pointless; the heartbeat loop surfaces this and stops.
//   - TransientError: a connection-level failure (DNS, connect, reset, timeout)
//     or a 5xx server response. Safe to retry later; idempotent calls retry it
//     in-line, and the heartbeat loop keeps looping.
//   - ProtocolError: a well-formed HTTP exchange that violated the contract
//     (unexpected status, malformed body, missing device token). Deterministic:
//     retrying the same request will fail the same way.
//
// Each concrete error wraps an optional cause so errors.Is / errors.As and
// errors.Unwrap keep working.

// AuthError is raised when authentication/authorization is rejected (401/403).
type AuthError struct {
	msg   string
	cause error
}

func (e *AuthError) Error() string { return e.msg }
func (e *AuthError) Unwrap() error { return e.cause }

// TransientError is a retryable transport failure or 5xx server response.
type TransientError struct {
	msg   string
	cause error
}

func (e *TransientError) Error() string { return e.msg }
func (e *TransientError) Unwrap() error { return e.cause }

// ProtocolError is a non-retryable contract violation (bad status, malformed
// body, missing device token).
type ProtocolError struct {
	msg   string
	cause error
}

func (e *ProtocolError) Error() string { return e.msg }
func (e *ProtocolError) Unwrap() error { return e.cause }

func newAuthError(format string, args ...any) *AuthError {
	return &AuthError{msg: fmt.Sprintf(format, args...)}
}

func newTransientError(cause error, format string, args ...any) *TransientError {
	return &TransientError{msg: fmt.Sprintf(format, args...), cause: cause}
}

func newProtocolError(cause error, format string, args ...any) *ProtocolError {
	return &ProtocolError{msg: fmt.Sprintf(format, args...), cause: cause}
}
