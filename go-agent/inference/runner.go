// Package inference serves Site Mode inference claims through local replicas.
package inference

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

const (
	claimWait       = 25 * time.Second
	maxRequestBody  = 2 * 1024 * 1024
	maxResponseRead = 32 * 1024
)

// FailureCode is the closed set of relay-v1 failure codes.
type FailureCode string

const (
	BecameActive  FailureCode = "became_active"
	Reclaimed     FailureCode = "reclaimed"
	ConnectFailed FailureCode = "connect_failed"
	TimedOut      FailureCode = "timeout"
	Cancelled     FailureCode = "cancelled"
	UpstreamError FailureCode = "upstream_error"
)

// AvailabilitySnapshot describes whether a single local replica slot may serve.
// Changed is closed when the snapshot is no longer valid. When a transition
// makes a slot unavailable, UnavailableCode identifies the relay-v1 failure to
// report; an empty value is reported as cancelled.
type AvailabilitySnapshot struct {
	Ready           bool
	Generation      uint64
	Changed         <-chan struct{}
	UnavailableCode FailureCode
}

// AvailabilitySource is shared with the presence and reclaim controllers. A
// runner never serves a claim after the snapshot that admitted it changes.
type AvailabilitySource interface {
	Snapshot() AvailabilitySnapshot
}

// ReplicaTarget accepts only ports owned by READY loopback replicas.
type ReplicaTarget interface {
	ReadyLoopbackPort(port int) bool
}

// Coordinator is the network seam for relay-v1. Implementations authenticate
// requests and decode the claim body before returning it to the runner.
type Coordinator interface {
	Claim(context.Context, time.Duration) (*Claim, error)
	Upload(context.Context, Claim, int, string, io.Reader) error
	Fail(context.Context, Claim, FailureCode, bool) error
}

// Claim is the v1 relay claim body.
type Claim struct {
	Version            int    `json:"version"`
	ClaimID            string `json:"claim_id"`
	PresenceGeneration int64  `json:"presence_generation"`
	ReplicaPort        int    `json:"replica_port"`
	Method             string `json:"method"`
	Path               string `json:"path"`
	ContentType        string `json:"content_type"`
	BodyB64            string `json:"body_b64"`
	DeadlineMS         int    `json:"deadline_ms"`
}

// DecodeClaim strictly decodes and validates one v1 claim body.
func DecodeClaim(body []byte) (Claim, error) {
	var claim Claim
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&claim); err != nil {
		return Claim{}, fmt.Errorf("decode claim: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return Claim{}, errors.New("decode claim: trailing data")
	}
	if err := validate(claim); err != nil {
		return Claim{}, err
	}
	return claim, nil
}

// Runner holds at most one claim. It deliberately has no runtime wiring: Site
// Mode remains opt-in until the Site Mode runtime supplies the three seams.
type Runner struct {
	Coordinator Coordinator
}

// Run claims work while availability permits it. A nil claim is the relay's
// bounded 204 wait and simply starts another wait. Context cancellation is the
// only normal terminal condition.
func (r Runner) Run(ctx context.Context, availability AvailabilitySource, replicas ReplicaTarget) error {
	if r.Coordinator == nil || availability == nil || replicas == nil {
		return errors.New("inference: missing dependency")
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		snapshot := availability.Snapshot()
		if !snapshot.Ready {
			waitForChange(ctx, snapshot.Changed)
			continue
		}

		claimCtx, stop := cancelOnChange(ctx, snapshot.Changed)
		claim, err := r.Coordinator.Claim(claimCtx, claimWait)
		// Capture a presence-driven cancellation before stop() cancels claimCtx.
		racedPresence := claimCtx.Err() != nil && ctx.Err() == nil
		stop()
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if racedPresence {
				// A presence change cancelled the bounded wait; re-evaluate
				// availability rather than treating it as a claim error.
				continue
			}
			return err
		}
		if claim == nil {
			// The bounded 204 wait expired without work.
			continue
		}
		// runClaim reports handled per-claim failures to the coordinator and
		// returns nil for them, so the loop keeps polling. It returns non-nil only
		// when the coordinator channel itself fails; context cancellation ends the
		// loop. The admitting snapshot fences the claim to its generation.
		if err := r.runClaim(ctx, availability, replicas, snapshot, *claim); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}
	}
}

func waitForChange(ctx context.Context, changed <-chan struct{}) {
	if changed == nil {
		<-ctx.Done()
		return
	}
	select {
	case <-ctx.Done():
	case <-changed:
	}
}

func cancelOnChange(ctx context.Context, changed <-chan struct{}) (context.Context, func()) {
	claimCtx, cancel := context.WithCancel(ctx)
	if changed == nil {
		return claimCtx, cancel
	}
	done := make(chan struct{})
	go func() {
		select {
		case <-changed:
			cancel()
		case <-done:
		}
	}()
	return claimCtx, func() {
		close(done)
		cancel()
	}
}

// runClaim serves one claim admitted under admit. It fences the work to admit's
// generation: an away-and-back transition that advances the generation must not
// serve the stale claim. admit.Changed cancels the local request and upload the
// moment that generation is superseded. Handled per-claim failures are reported
// and return nil so the caller keeps polling.
func (r Runner) runClaim(ctx context.Context, availability AvailabilitySource, replicas ReplicaTarget, admit AvailabilitySnapshot, claim Claim) error {
	claimCtx, stop := cancelOnChange(ctx, admit.Changed)
	defer stop()

	current := availability.Snapshot()
	if !current.Ready {
		return r.reportFailure(ctx, claim, unavailableCode(current), errors.New("replica is unavailable"))
	}
	if current.Generation != admit.Generation {
		// The agent went away and returned under a new generation; the claim
		// admitted under the old generation is stale and must not be served.
		return r.reportFailure(ctx, claim, BecameActive, errors.New("availability generation advanced"))
	}
	if err := validate(claim); err != nil {
		return r.reportFailure(ctx, claim, UpstreamError, err)
	}
	if !replicas.ReadyLoopbackPort(claim.ReplicaPort) {
		return r.reportFailure(ctx, claim, ConnectFailed, errors.New("replica is not ready on loopback"))
	}
	body, err := base64.StdEncoding.DecodeString(claim.BodyB64)
	if err != nil {
		return r.reportFailure(ctx, claim, UpstreamError, fmt.Errorf("decode request body: %w", err))
	}
	if len(body) > maxRequestBody {
		return r.reportFailure(ctx, claim, UpstreamError, errors.New("request body exceeds 2 MiB"))
	}

	requestCtx, cancel := context.WithTimeout(claimCtx, time.Duration(claim.DeadlineMS)*time.Millisecond)
	defer cancel()
	request, err := localRequest(requestCtx, claim, body)
	if err != nil {
		return r.reportFailure(ctx, claim, ConnectFailed, err)
	}
	response, err := loopbackClient.Do(request)
	if err != nil {
		return r.reportFailure(ctx, claim, codeFor(requestCtx, availability), err)
	}
	defer response.Body.Close()

	err = r.Coordinator.Upload(requestCtx, claim, response.StatusCode, response.Header.Get("Content-Type"), chunkReader{response.Body})
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if requestCtx.Err() != nil {
		return r.reportFailure(ctx, claim, codeFor(requestCtx, availability), err)
	}
	// The coordinator upload channel failed for a reason other than deadline or
	// cancellation; surface it so the run loop stops.
	return err
}

// reportFailure reports a handled per-claim failure to the coordinator. It uses
// the parent context (not the cancelled work context) so a presence-cancelled
// claim can still notify best-effort. It returns nil once the failure is
// reported, so the run loop treats the claim as handled and keeps polling; it
// returns the context error while shutting down, and a non-nil error only when
// the coordinator's failure channel itself fails, which stops the loop.
func (r Runner) reportFailure(parent context.Context, claim Claim, code FailureCode, cause error) error {
	if parent.Err() != nil {
		return parent.Err()
	}
	if code == "" {
		code = Cancelled
	}
	reportCtx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	if err := r.Coordinator.Fail(reportCtx, claim, code, code == BecameActive || code == Reclaimed); err != nil {
		return fmt.Errorf("%w (report %s failure: %v)", cause, code, err)
	}
	return nil
}

func codeFor(requestCtx context.Context, availability AvailabilitySource) FailureCode {
	snapshot := availability.Snapshot()
	if !snapshot.Ready {
		return unavailableCode(snapshot)
	}
	if errors.Is(requestCtx.Err(), context.DeadlineExceeded) {
		return TimedOut
	}
	if errors.Is(requestCtx.Err(), context.Canceled) {
		return Cancelled
	}
	// The local exchange failed without a context signal: the replica refused or
	// dropped the loopback connection.
	return ConnectFailed
}

func unavailableCode(snapshot AvailabilitySnapshot) FailureCode {
	switch snapshot.UnavailableCode {
	case BecameActive, Reclaimed, Cancelled:
		return snapshot.UnavailableCode
	default:
		return Cancelled
	}
}

func localRequest(ctx context.Context, claim Claim, body []byte) (*http.Request, error) {
	request, err := http.NewRequestWithContext(ctx, claim.Method, "http://127.0.0.1:"+strconv.Itoa(claim.ReplicaPort)+claim.Path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", claim.ContentType)
	return request, nil
}

// loopbackClient is the runner's only outbound path to a replica. It never uses
// a proxy, never follows redirects, and disables keep-alives so each exchange
// closes its connection and leaves no lingering idle-connection goroutine.
var loopbackClient = func() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableKeepAlives = true
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}()

type chunkReader struct{ io.Reader }

func (r chunkReader) Read(p []byte) (int, error) {
	if len(p) > maxResponseRead {
		p = p[:maxResponseRead]
	}
	return r.Reader.Read(p)
}

func validate(claim Claim) error {
	if claim.Version != 1 || len(claim.ClaimID) < 16 || len(claim.ClaimID) > 128 || claim.PresenceGeneration < 0 || claim.ReplicaPort < 1 || claim.ReplicaPort > 65535 || claim.Method != http.MethodPost || claim.ContentType != "application/json" || claim.DeadlineMS < 1 || claim.DeadlineMS > 300000 {
		return errors.New("invalid claim")
	}
	if claim.Path != "/v1/chat/completions" && claim.Path != "/v1/embeddings" {
		return errors.New("invalid claim path")
	}
	return nil
}
