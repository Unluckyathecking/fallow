package heartbeat

import "time"

// Coordinator v1 endpoints (agent-initiated). These are the exact paths the
// Python CoordinatorClient dials (fallow_agent.heartbeat.constants); the Go
// daemon must match them byte for byte.
const (
	registerPath          = "/v1/agents/register"
	heartbeatPathTemplate = "/v1/agents/%s/heartbeat"
	eventsPathTemplate    = "/v1/agents/%s/events"
	workPathTemplate      = "/v1/agents/%s/work"
	resultPathTemplate    = "/v1/agents/%s/work_units/%s/result"

	// leaseAttemptHeader carries the lease attempt on a result completion.
	leaseAttemptHeader = "X-Fallow-Lease-Attempt"

	// workTimeoutParam is the long-poll timeout query parameter.
	workTimeoutParam = "timeout"

	// authHeader and bearerScheme form the device-token Authorization header.
	authHeader   = "Authorization"
	bearerScheme = "Bearer "

	contentTypeHeader = "Content-Type"
	contentTypeJSON   = "application/json"
)

// HTTP status codes handled by the client. Mirrors the Python constants so the
// status-code semantics stay identical across the two implementations.
const (
	httpOK           = 200
	httpCreated      = 201
	httpAccepted     = 202
	httpNoContent    = 204
	httpUnauthorized = 401
	httpForbidden    = 403
	serverErrorMin   = 500
)

// okCodes carry a parseable success body (register, heartbeat, poll_work).
func isOKCode(code int) bool { return code == httpOK || code == httpCreated }

// acceptCodes are accepted for fire-and-forget writes (events, results).
func isAcceptCode(code int) bool {
	return code == httpOK || code == httpCreated || code == httpAccepted || code == httpNoContent
}

// authCodes mean "your token is not accepted".
func isAuthCode(code int) bool { return code == httpUnauthorized || code == httpForbidden }

// Retry / backoff defaults, matching the Python client.
const (
	defaultMaxRetries  = 3
	defaultBackoffBase = 500 * time.Millisecond
)
