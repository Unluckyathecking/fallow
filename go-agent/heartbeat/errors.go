package heartbeat

import (
	"errors"
	"fmt"
)

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
//
// revoked separates the one terminal rejection from the rest. A coordinator that
// says this identity was revoked is stating a decision an operator made; any
// other 401 — including the one every desk gets from a coordinator that lost its
// database — is a rejection this process may live to see reversed (ADR 104).
type AuthError struct {
	msg     string
	cause   error
	revoked bool
}

func (e *AuthError) Error() string { return e.msg }
func (e *AuthError) Unwrap() error { return e.cause }

// Revoked reports whether the coordinator named this identity as revoked.
func (e *AuthError) Revoked() bool { return e.revoked }

// IsRevocation reports whether err is the coordinator's revoked-identity
// rejection, the only auth failure the agent treats as permanent.
func IsRevocation(err error) bool {
	var authErr *AuthError
	return errors.As(err, &authErr) && authErr.revoked
}

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

func newAuthError(revoked bool, format string, args ...any) *AuthError {
	return &AuthError{msg: fmt.Sprintf(format, args...), revoked: revoked}
}

func newTransientError(cause error, format string, args ...any) *TransientError {
	return &TransientError{msg: fmt.Sprintf(format, args...), cause: cause}
}

func newProtocolError(cause error, format string, args ...any) *ProtocolError {
	return &ProtocolError{msg: fmt.Sprintf(format, args...), cause: cause}
}
