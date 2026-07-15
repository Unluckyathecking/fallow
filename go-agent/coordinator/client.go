package coordinator

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const (
	registerPath       = "/v1/agents/register"
	leaseAttemptHeader = "X-Fallow-Lease-Attempt"
)

type HTTPDoer interface {
	Do(*http.Request) (*http.Response, error)
}

type SleepFunc func(context.Context, time.Duration) error

type RetryConfig struct {
	MaxRetries int
	Backoff    time.Duration
}

func DefaultRetryConfig() RetryConfig {
	return RetryConfig{MaxRetries: 3, Backoff: 500 * time.Millisecond}
}

type Client struct {
	baseURL     string
	http        HTTPDoer
	agentID     string
	deviceToken string
	retry       RetryConfig
	sleep       SleepFunc
}

func NewClient(baseURL string, httpClient HTTPDoer, agentID, deviceToken string) *Client {
	return &Client{
		baseURL:     strings.TrimRight(baseURL, "/"),
		http:        httpClient,
		agentID:     agentID,
		deviceToken: deviceToken,
		retry:       DefaultRetryConfig(),
		sleep:       sleepContext,
	}
}

func (c *Client) SetRetry(config RetryConfig, sleep SleepFunc) {
	c.retry = config
	if sleep != nil {
		c.sleep = sleep
	}
}

func (c *Client) AgentID() string {
	return c.agentID
}

func (c *Client) DeviceToken() string {
	return c.deviceToken
}

func (c *Client) Register(
	ctx context.Context, request protocol.RegisterRequest,
) (protocol.RegisterResponse, error) {
	var response protocol.RegisterResponse
	body, err := json.Marshal(request)
	if err != nil {
		return response, &ProtocolError{Message: fmt.Sprintf("encode RegisterRequest: %v", err)}
	}
	httpResponse, err := c.sendOnce(ctx, http.MethodPost, c.baseURL+registerPath, body, nil)
	if err != nil {
		closeOnError(httpResponse)
		return response, &TransientError{Message: "register transport error", Cause: err}
	}
	if err := parseOK(httpResponse, &response); err != nil {
		return response, err
	}
	c.agentID = response.AgentID
	c.deviceToken = response.DeviceToken
	return response, nil
}

func (c *Client) Heartbeat(
	ctx context.Context, heartbeat protocol.Heartbeat,
) (protocol.HeartbeatResponse, error) {
	var response protocol.HeartbeatResponse
	body, err := json.Marshal(heartbeat)
	if err != nil {
		return response, &ProtocolError{Message: fmt.Sprintf("encode Heartbeat: %v", err)}
	}
	path := fmt.Sprintf("/v1/agents/%s/heartbeat", url.PathEscape(heartbeat.AgentID))
	httpResponse, err := c.sendIdempotent(ctx, http.MethodPost, c.baseURL+path, body)
	if err != nil {
		return response, err
	}
	if err := parseOK(httpResponse, &response); err != nil {
		return response, err
	}
	return response, nil
}

func (c *Client) PollWork(
	ctx context.Context, timeout time.Duration,
) (*protocol.WorkUnitLease, error) {
	agentID, err := c.requireAgentID()
	if err != nil {
		return nil, err
	}
	path := fmt.Sprintf("/v1/agents/%s/work?timeout=%s", url.PathEscape(agentID), seconds(timeout))
	httpResponse, err := c.sendIdempotent(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	if httpResponse.StatusCode == http.StatusNoContent {
		drainAndClose(httpResponse.Body)
		return nil, nil
	}
	var lease protocol.WorkUnitLease
	if err := parseOK(httpResponse, &lease); err != nil {
		return nil, err
	}
	return &lease, nil
}

func (c *Client) PushEvent(ctx context.Context, event protocol.AgentEvent) error {
	path := fmt.Sprintf("/v1/agents/%s/events", url.PathEscape(event.AgentID))
	return c.postAccepted(ctx, c.baseURL+path, event, nil)
}

func (c *Client) CompleteUnit(
	ctx context.Context, result protocol.WorkResult, leaseAttempt int,
) error {
	agentID, err := c.requireAgentID()
	if err != nil {
		return err
	}
	path := fmt.Sprintf(
		"/v1/agents/%s/work_units/%s/result",
		url.PathEscape(agentID),
		url.PathEscape(result.WorkUnitID),
	)
	headers := http.Header{leaseAttemptHeader: []string{strconv.Itoa(leaseAttempt)}}
	return c.postAccepted(ctx, c.baseURL+path, result, headers)
}

func (c *Client) postAccepted(
	ctx context.Context, endpoint string, value any, extra http.Header,
) error {
	body, err := json.Marshal(value)
	if err != nil {
		return &ProtocolError{Message: fmt.Sprintf("encode request: %v", err)}
	}
	headers, err := c.authHeaders()
	if err != nil {
		return err
	}
	for name, values := range extra {
		headers[name] = append([]string(nil), values...)
	}
	response, err := c.sendOnce(ctx, http.MethodPost, endpoint, body, headers)
	if err != nil {
		closeOnError(response)
		return &TransientError{Message: "POST transport error", Cause: err}
	}
	defer drainAndClose(response.Body)
	if response.StatusCode == http.StatusOK ||
		response.StatusCode == http.StatusCreated ||
		response.StatusCode == http.StatusAccepted ||
		response.StatusCode == http.StatusNoContent {
		return nil
	}
	return classify(response.StatusCode)
}

func (c *Client) sendIdempotent(
	ctx context.Context, method, endpoint string, body []byte,
) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		headers, err := c.authHeaders()
		if err != nil {
			return nil, err
		}
		response, err := c.sendOnce(ctx, method, endpoint, body, headers)
		if err == nil {
			return response, nil
		}
		closeOnError(response)
		if attempt >= c.retry.MaxRetries {
			return nil, &TransientError{
				Message: fmt.Sprintf("%s %s failed after %d attempt(s)", method, endpoint, attempt+1),
				Cause:   err,
			}
		}
		if err := c.sleep(ctx, c.retry.Backoff*(1<<attempt)); err != nil {
			return nil, &TransientError{Message: "retry interrupted", Cause: err}
		}
	}
}

func (c *Client) sendOnce(
	ctx context.Context, method, endpoint string, body []byte, headers http.Header,
) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return nil, err
	}
	request.Header = make(http.Header)
	for name, values := range headers {
		request.Header[name] = append([]string(nil), values...)
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	return c.http.Do(request)
}

func (c *Client) authHeaders() (http.Header, error) {
	if c.deviceToken == "" {
		return nil, &ProtocolError{Message: "device token unset; call Register first"}
	}
	return http.Header{"Authorization": []string{"Bearer " + c.deviceToken}}, nil
}

func (c *Client) requireAgentID() (string, error) {
	if c.agentID == "" {
		return "", &ProtocolError{Message: "agent ID unknown; call Register first"}
	}
	return c.agentID, nil
}

func parseOK(response *http.Response, destination any) error {
	defer drainAndClose(response.Body)
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusCreated {
		return classify(response.StatusCode)
	}
	decoder := json.NewDecoder(response.Body)
	if err := decoder.Decode(destination); err != nil {
		return &ProtocolError{Message: fmt.Sprintf("malformed response body: %v", err)}
	}
	if err := ensureEOF(decoder); err != nil {
		return &ProtocolError{Message: fmt.Sprintf("malformed response body: %v", err)}
	}
	return nil
}

func ensureEOF(decoder *json.Decoder) error {
	var extra any
	err := decoder.Decode(&extra)
	if errors.Is(err, io.EOF) {
		return nil
	}
	if err == nil {
		return errors.New("multiple JSON values")
	}
	return err
}

func classify(status int) error {
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return &AuthError{Status: status}
	}
	if status >= http.StatusInternalServerError {
		return &TransientError{Message: fmt.Sprintf("coordinator server error %d", status)}
	}
	return &ProtocolError{Message: fmt.Sprintf("unexpected coordinator status %d", status)}
}

func drainAndClose(body io.ReadCloser) {
	_, _ = io.Copy(io.Discard, body)
	_ = body.Close()
}

func closeOnError(response *http.Response) {
	if response != nil && response.Body != nil {
		drainAndClose(response.Body)
	}
}

func seconds(duration time.Duration) string {
	value := strconv.FormatFloat(duration.Seconds(), 'f', -1, 64)
	if !strings.Contains(value, ".") {
		value += ".0"
	}
	return value
}

func sleepContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
