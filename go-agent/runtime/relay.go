// Package runtime — relay.go is the agent side of the Site Mode inference relay
// (docs/lan-site/relay-v1.md). It speaks ordinary authenticated HTTPS over the
// pinned Site Mode client: hold a claim open, stream the loopback replica's raw
// response back, or report a typed terminal failure. It adds no trust of its
// own — the pinned client supplied by the Site Mode wiring is its only outbound
// path, so a bad pin fails the request closed.
package runtime

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

	"github.com/Unluckyathecking/fallow/go-agent/inference"
)

// relayClient implements inference.Coordinator against the relay-v1 endpoints.
type relayClient struct {
	base    string
	agentID string
	token   string
	http    *http.Client
}

// newRelayClient builds a relay client. baseURL is a pinned coordinator origin;
// httpClient is the pinned Site Mode client (never nil in Site Mode).
func newRelayClient(baseURL, agentID, deviceToken string, httpClient *http.Client) *relayClient {
	return &relayClient{
		base:    trimTrailingSlash(baseURL),
		agentID: agentID,
		token:   deviceToken,
		http:    httpClient,
	}
}

func trimTrailingSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

// Claim holds one request open for up to wait, returning a decoded claim, or nil
// when the bounded 204 wait expires without work.
func (c *relayClient) Claim(ctx context.Context, wait time.Duration) (*inference.Claim, error) {
	endpoint := fmt.Sprintf("%s/v1/agents/%s/inference/claims", c.base, url.PathEscape(c.agentID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	q := req.URL.Query()
	q.Set("timeout_s", strconv.FormatInt(int64(wait.Seconds()), 10))
	req.URL.RawQuery = q.Encode()
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer drainClose(resp)
	switch resp.StatusCode {
	case http.StatusNoContent:
		return nil, nil
	case http.StatusOK:
		body, err := io.ReadAll(io.LimitReader(resp.Body, maxClaimBody))
		if err != nil {
			return nil, err
		}
		claim, err := inference.DecodeClaim(body)
		if err != nil {
			return nil, err
		}
		return &claim, nil
	default:
		return nil, fmt.Errorf("relay claim: unexpected status %s", resp.Status)
	}
}

// Upload streams the replica's raw response bytes back to the coordinator. It
// fences the upload to the claim's presence generation and reports the replica's
// HTTP status, letting the coordinator relay chunks without reframing.
func (c *relayClient) Upload(ctx context.Context, claim inference.Claim, upstreamStatus int, contentType string, body io.Reader) error {
	endpoint := fmt.Sprintf("%s/v1/agents/%s/inference/claims/%s/response",
		c.base, url.PathEscape(c.agentID), url.PathEscape(claim.ClaimID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("X-Fallow-Presence-Generation", strconv.FormatInt(claim.PresenceGeneration, 10))
	req.Header.Set("X-Fallow-Upstream-Status", strconv.Itoa(upstreamStatus))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer drainClose(resp)
	if resp.StatusCode == http.StatusAccepted || claimTerminated(resp.StatusCode) {
		// 202 is a clean upload; 404/409/410 mean the claim is already gone —
		// a newer presence generation invalidated it, the deadline passed or the
		// client left. That is routine fencing, not a channel failure, so the
		// runner treats the claim as terminated and keeps polling.
		return nil
	}
	return fmt.Errorf("relay response upload: unexpected status %s", resp.Status)
}

// claimTerminated reports whether a status means the claim is no longer valid
// (unknown, wrong state or gone). These are expected under presence fencing.
func claimTerminated(status int) bool {
	return status == http.StatusNotFound || status == http.StatusConflict || status == http.StatusGone
}

// Fail reports a typed terminal failure before any response bytes were sent.
func (c *relayClient) Fail(ctx context.Context, claim inference.Claim, code inference.FailureCode, retryable bool) error {
	endpoint := fmt.Sprintf("%s/v1/agents/%s/inference/claims/%s/failure",
		c.base, url.PathEscape(c.agentID), url.PathEscape(claim.ClaimID))
	payload, err := json.Marshal(map[string]any{
		"presence_generation": claim.PresenceGeneration,
		"code":                string(code),
		"retryable":           retryable,
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer drainClose(resp)
	switch {
	case resp.StatusCode == http.StatusOK, resp.StatusCode == http.StatusAccepted,
		resp.StatusCode == http.StatusNoContent, claimTerminated(resp.StatusCode):
		// The report was accepted, or the claim was already gone — either way the
		// claim is terminated and the runner keeps polling rather than failing.
		return nil
	default:
		return fmt.Errorf("relay failure report: unexpected status %s", resp.Status)
	}
}

// maxClaimBody bounds the claim JSON we read; a v1 claim body is small.
const maxClaimBody = 64 * 1024

func drainClose(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
}
