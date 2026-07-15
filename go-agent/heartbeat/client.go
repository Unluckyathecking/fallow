// Package heartbeat is the Go agent's typed HTTP client to the coordinator.
//
// Every agent->coordinator call goes through Client. It is a stateful
// connection object: it holds the agent_id and bearer device_token learned at
// registration (connection state, not domain data; the wire messages
// themselves stay frozen). All I/O goes through an injected Doer so tests drive
// it with an httptest server or a stub RoundTripper and never guess at sockets.
//
// Retry policy (see ADR 009 / ADR 037): idempotent calls (Heartbeat, PollWork)
// retry transport failures with an injected sleep and exponential backoff.
// Register is never retried (a duplicate enrollment is not idempotent). 5xx
// responses map to *TransientError but are not retried in-line; the caller (the
// heartbeat loop / event sink) decides how to react. The status-code handling
// is identical to the Python CoordinatorClient:
//
//   - register accepts 200/201;
//   - heartbeat / poll_work accept 200/201 (204 = no work for poll);
//   - fire-and-forget event/result accept 200/201/202/204;
//   - 401/403 -> *AuthError, >=500 -> *TransientError, any other -> *ProtocolError.
package heartbeat

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// Doer is the subset of *http.Client the client depends on, so tests can
// substitute an httptest server client or a failure-injecting RoundTripper.
type Doer interface {
	Do(req *http.Request) (*http.Response, error)
}

// SleepFunc is the injectable backoff sleeper (defaults to time.Sleep).
type SleepFunc func(time.Duration)

// Client is a typed, retrying HTTP client for the coordinator's agent API.
type Client struct {
	base        string
	http        Doer
	agentID     string
	deviceToken string
	retry       RetryConfig
	sleep       SleepFunc
}

// Option configures a Client.
type Option func(*Client)

// WithIdentity seeds the agent_id and device_token (as if already enrolled).
func WithIdentity(agentID, deviceToken string) Option {
	return func(c *Client) {
		c.agentID = agentID
		c.deviceToken = deviceToken
	}
}

// WithRetry overrides the default retry policy.
func WithRetry(retry RetryConfig) Option {
	return func(c *Client) { c.retry = retry }
}

// WithSleep overrides the backoff sleeper (tests inject an instant no-op).
func WithSleep(sleep SleepFunc) Option {
	return func(c *Client) { c.sleep = sleep }
}

// NewClient builds a Client. baseURL has any trailing slash trimmed. If doer is
// nil, http.DefaultClient is used.
func NewClient(baseURL string, doer Doer, opts ...Option) *Client {
	if doer == nil {
		doer = http.DefaultClient
	}
	c := &Client{
		base:  trimTrailingSlash(baseURL),
		http:  doer,
		retry: DefaultRetryConfig(),
		sleep: time.Sleep,
	}
	for _, opt := range opts {
		opt(c)
	}
	return c
}

// AgentID returns the enrolled agent id, or "" if not yet registered.
func (c *Client) AgentID() string { return c.agentID }

// DeviceToken returns the bearer device token, or "" if not yet registered.
func (c *Client) DeviceToken() string { return c.deviceToken }

func trimTrailingSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

// ── Registration (never retried, no bearer) ─────────────────────────────────

// Register enrolls this machine. On success it stores the agent_id and
// device_token learned from the response. Register is never retried: a
// duplicate enrollment is not idempotent.
func (c *Client) Register(ctx context.Context, req protocol.RegisterRequest) (protocol.RegisterResponse, error) {
	var zero protocol.RegisterResponse
	body, err := json.Marshal(req)
	if err != nil {
		return zero, newProtocolError(err, "marshal register request: %v", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+registerPath, bytes.NewReader(body))
	if err != nil {
		return zero, newProtocolError(err, "build register request: %v", err)
	}
	httpReq.Header.Set(contentTypeHeader, contentTypeJSON)

	resp, err := c.http.Do(httpReq)
	if err != nil {
		return zero, newTransientError(err, "register transport error: %v", err)
	}
	out, err := parseOK[protocol.RegisterResponse](resp)
	if err != nil {
		return zero, err
	}
	c.agentID = out.AgentID
	c.deviceToken = out.DeviceToken
	return out, nil
}

// ── Heartbeat / work (idempotent, retried) ──────────────────────────────────

// Heartbeat sends one heartbeat and returns the parsed response.
func (c *Client) Heartbeat(ctx context.Context, hb protocol.Heartbeat) (protocol.HeartbeatResponse, error) {
	var zero protocol.HeartbeatResponse
	body, err := json.Marshal(hb)
	if err != nil {
		return zero, newProtocolError(err, "marshal heartbeat: %v", err)
	}
	resp, err := c.sendIdempotent(ctx, http.MethodPost, fmt.Sprintf(c.base+heartbeatPathTemplate, hb.AgentID), body, nil)
	if err != nil {
		return zero, err
	}
	return parseOK[protocol.HeartbeatResponse](resp)
}

// PollWork long-polls for one work-unit lease. A 204 (no work) returns
// (nil, nil). timeoutS is sent as the coordinator's long-poll timeout param.
func (c *Client) PollWork(ctx context.Context, timeoutS float64) (*protocol.WorkUnitLease, error) {
	agentID, err := c.requireAgentID()
	if err != nil {
		return nil, err
	}
	query := url.Values{}
	query.Set(workTimeoutParam, strconv.FormatFloat(timeoutS, 'f', -1, 64))
	resp, err := c.sendIdempotent(ctx, http.MethodGet, fmt.Sprintf(c.base+workPathTemplate, agentID), nil, query)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == httpNoContent {
		drainAndClose(resp)
		return nil, nil
	}
	lease, err := parseOK[protocol.WorkUnitLease](resp)
	if err != nil {
		return nil, err
	}
	return &lease, nil
}

// ── Fire-and-forget writes (not retried in-line) ────────────────────────────

// PushEvent POSTs an event to the coordinator. Accepts 200/201/202/204.
func (c *Client) PushEvent(ctx context.Context, event protocol.AgentEvent) error {
	body, err := json.Marshal(event)
	if err != nil {
		return newProtocolError(err, "marshal event: %v", err)
	}
	return c.postExpectAccept(ctx, fmt.Sprintf(c.base+eventsPathTemplate, event.AgentID), body, nil)
}

// CompleteUnit reports a work-unit result. leaseAttempt is sent in the
// X-Fallow-Lease-Attempt header. Accepts 200/201/202/204.
func (c *Client) CompleteUnit(ctx context.Context, result protocol.WorkResult, leaseAttempt int) error {
	agentID, err := c.requireAgentID()
	if err != nil {
		return err
	}
	body, err := json.Marshal(result)
	if err != nil {
		return newProtocolError(err, "marshal work result: %v", err)
	}
	extra := map[string]string{leaseAttemptHeader: strconv.Itoa(leaseAttempt)}
	url := fmt.Sprintf(c.base+resultPathTemplate, agentID, result.WorkUnitID)
	return c.postExpectAccept(ctx, url, body, extra)
}

// ── internals ───────────────────────────────────────────────────────────────

func (c *Client) requireAgentID() (string, error) {
	if c.agentID == "" {
		return "", newProtocolError(nil, "agent_id unknown; call Register() first")
	}
	return c.agentID, nil
}

func (c *Client) authHeader() (string, error) {
	if c.deviceToken == "" {
		return "", newProtocolError(nil, "device token unset; call Register() first")
	}
	return bearerScheme + c.deviceToken, nil
}

// sendIdempotent retries only transport errors, up to retry.MaxRetries, with
// exponential backoff — matching the Python _send_idempotent loop.
func (c *Client) sendIdempotent(ctx context.Context, method, rawURL string, body []byte, query url.Values) (*http.Response, error) {
	bearer, err := c.authHeader()
	if err != nil {
		return nil, err
	}
	attempt := 0
	for {
		req, err := c.buildRequest(ctx, method, rawURL, body, query)
		if err != nil {
			return nil, err
		}
		req.Header.Set(authHeader, bearer)
		resp, err := c.http.Do(req)
		if err == nil {
			return resp, nil
		}
		attempt++
		if attempt > c.retry.MaxRetries {
			return nil, newTransientError(err, "%s %s failed after %d attempt(s): %v", method, rawURL, attempt, err)
		}
		c.sleep(c.retry.BackoffBase * (1 << (attempt - 1)))
	}
}

func (c *Client) postExpectAccept(ctx context.Context, rawURL string, body []byte, extra map[string]string) error {
	bearer, err := c.authHeader()
	if err != nil {
		return err
	}
	req, err := c.buildRequest(ctx, http.MethodPost, rawURL, body, nil)
	if err != nil {
		return err
	}
	req.Header.Set(authHeader, bearer)
	for k, v := range extra {
		req.Header.Set(k, v)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return newTransientError(err, "POST %s transport error: %v", rawURL, err)
	}
	defer drainAndClose(resp)
	if isAcceptCode(resp.StatusCode) {
		return nil
	}
	return classifyFailure(resp.StatusCode)
}

func (c *Client) buildRequest(ctx context.Context, method, rawURL string, body []byte, query url.Values) (*http.Request, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	if len(query) > 0 {
		rawURL = rawURL + "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, reader)
	if err != nil {
		return nil, newProtocolError(err, "build %s request: %v", method, err)
	}
	if body != nil {
		req.Header.Set(contentTypeHeader, contentTypeJSON)
	}
	return req, nil
}

// parseOK decodes a 200/201 body into T, else classifies the failure by status.
func parseOK[T any](resp *http.Response) (T, error) {
	var out T
	defer drainAndClose(resp)
	if !isOKCode(resp.StatusCode) {
		return out, classifyFailure(resp.StatusCode)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, newTransientError(err, "read response body: %v", err)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&out); err != nil {
		return out, newProtocolError(err, "malformed %T body: %v", out, err)
	}
	return out, nil
}

// classifyFailure maps a non-success status to the right typed error, matching
// the Python _classify_failure ordering (auth, then 5xx, then protocol).
func classifyFailure(code int) error {
	if isAuthCode(code) {
		return newAuthError("coordinator rejected credentials (%d)", code)
	}
	if code >= serverErrorMin {
		return newTransientError(nil, "coordinator server error %d", code)
	}
	return newProtocolError(nil, "unexpected coordinator status %d", code)
}

// drainAndClose lets the transport reuse the connection.
func drainAndClose(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
}
