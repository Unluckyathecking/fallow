package inference

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// --- fakes ---------------------------------------------------------------

type fakeAvail struct {
	mu      sync.Mutex
	ready   bool
	gen     uint64
	changed chan struct{}
	code    FailureCode
}

func (a *fakeAvail) Snapshot() AvailabilitySnapshot {
	a.mu.Lock()
	defer a.mu.Unlock()
	return AvailabilitySnapshot{Ready: a.ready, Generation: a.gen, Changed: a.changed, UnavailableCode: a.code}
}

func (a *fakeAvail) unavailable(code FailureCode) {
	a.mu.Lock()
	a.ready = false
	a.code = code
	a.mu.Unlock()
}

// genAvail reports Ready the whole time but advances its generation after the
// first snapshot, modelling an away-and-back transition between a claim's
// admission and the point the runner would serve it.
type genAvail struct {
	mu    sync.Mutex
	calls int
}

func (a *genAvail) Snapshot() AvailabilitySnapshot {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls++
	gen := uint64(2)
	if a.calls == 1 {
		gen = 1
	}
	return AvailabilitySnapshot{Ready: true, Generation: gen}
}

// servingSnap is the admitting snapshot for a Ready, generation-0 slot.
func servingSnap() AvailabilitySnapshot { return AvailabilitySnapshot{Ready: true} }

type fakeReplica struct{ ports map[int]bool }

func (r fakeReplica) ReadyLoopbackPort(p int) bool { return r.ports[p] }

func readyPort(p int) fakeReplica { return fakeReplica{ports: map[int]bool{p: true}} }

type uploadRecord struct {
	status      int
	contentType string
	body        []byte
}

type failRecord struct {
	code      FailureCode
	retryable bool
}

type fakeCoord struct {
	mu sync.Mutex

	claimQueue []*Claim
	claimErr   error
	claimCalls int
	claimBlock chan struct{} // when set, Claim blocks on it after draining the queue

	uploadErr   error
	uploadDelay time.Duration
	uploads     []uploadRecord

	failErr error
	fails   []failRecord
}

func (c *fakeCoord) Claim(ctx context.Context, _ time.Duration) (*Claim, error) {
	c.mu.Lock()
	c.claimCalls++
	if c.claimErr != nil {
		err := c.claimErr
		c.mu.Unlock()
		return nil, err
	}
	if len(c.claimQueue) > 0 {
		cl := c.claimQueue[0]
		c.claimQueue = c.claimQueue[1:]
		c.mu.Unlock()
		return cl, nil
	}
	block := c.claimBlock
	c.mu.Unlock()
	if block != nil {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-block:
		}
	}
	// bounded 204 wait expired: no claim available.
	return nil, nil
}

func (c *fakeCoord) Upload(ctx context.Context, _ Claim, status int, ct string, r io.Reader) error {
	if c.uploadDelay > 0 {
		select {
		case <-time.After(c.uploadDelay):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	b, err := io.ReadAll(r)
	c.mu.Lock()
	c.uploads = append(c.uploads, uploadRecord{status: status, contentType: ct, body: b})
	c.mu.Unlock()
	if err != nil {
		return err
	}
	return c.uploadErr
}

func (c *fakeCoord) Fail(_ context.Context, _ Claim, code FailureCode, retryable bool) error {
	c.mu.Lock()
	c.fails = append(c.fails, failRecord{code: code, retryable: retryable})
	c.mu.Unlock()
	return c.failErr
}

func (c *fakeCoord) snapshotFails() []failRecord {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]failRecord(nil), c.fails...)
}

func (c *fakeCoord) snapshotUploads() []uploadRecord {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]uploadRecord(nil), c.uploads...)
}

// --- helpers -------------------------------------------------------------

func portOf(t *testing.T, s *httptest.Server) int {
	t.Helper()
	_, port, err := netSplit(s.URL)
	if err != nil {
		t.Fatalf("parse server url %q: %v", s.URL, err)
	}
	return port
}

func netSplit(rawURL string) (string, int, error) {
	rawURL = strings.TrimPrefix(rawURL, "http://")
	host, portStr, ok := strings.Cut(rawURL, ":")
	if !ok {
		return "", 0, errors.New("no port")
	}
	port, err := strconv.Atoi(portStr)
	return host, port, err
}

func claimFor(port int, path string) Claim {
	return Claim{
		Version:            1,
		ClaimID:            "claim-0123456789abcdef",
		PresenceGeneration: 7,
		ReplicaPort:        port,
		Method:             "POST",
		Path:               path,
		ContentType:        "application/json",
		BodyB64:            base64.StdEncoding.EncodeToString([]byte(`{"prompt":"hi"}`)),
		DeadlineMS:         2000,
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		if cond() {
			return
		}
		select {
		case <-deadline:
			t.Fatalf("timed out waiting for %s", what)
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// --- runClaim: served responses ------------------------------------------

func TestRunClaimBufferedJSON(t *testing.T) {
	var gotBody []byte
	var gotAuth, gotCT string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		gotBody, _ = io.ReadAll(req.Body)
		gotAuth = req.Header.Get("Authorization")
		gotCT = req.Header.Get("Content-Type")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	port := portOf(t, srv)

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim); err != nil {
		t.Fatalf("runClaim: %v", err)
	}
	ups := coord.snapshotUploads()
	if len(ups) != 1 || ups[0].status != 200 || ups[0].contentType != "application/json" || string(ups[0].body) != `{"ok":true}` {
		t.Fatalf("upload mismatch: %+v", ups)
	}
	if string(gotBody) != `{"prompt":"hi"}` || gotCT != "application/json" {
		t.Fatalf("forwarded body/ct wrong: body=%q ct=%q", gotBody, gotCT)
	}
	if gotAuth != "" {
		t.Fatalf("client Authorization must never reach llama, got %q", gotAuth)
	}
	if fails := coord.snapshotFails(); len(fails) != 0 {
		t.Fatalf("unexpected fails: %+v", fails)
	}
}

func TestRunClaimStreamsRawSSE(t *testing.T) {
	chunk := strings.Repeat("data: token\n\n", 8000) // larger than one 32 KiB read
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		_, _ = io.WriteString(w, chunk)
	}))
	defer srv.Close()
	port := portOf(t, srv)

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim); err != nil {
		t.Fatalf("runClaim: %v", err)
	}
	ups := coord.snapshotUploads()
	if len(ups) != 1 || ups[0].contentType != "text/event-stream" || string(ups[0].body) != chunk {
		t.Fatalf("stream not forwarded verbatim: len=%d ct=%q equal=%v", len(ups), ups[0].contentType, string(ups[0].body) == chunk)
	}
}

func TestRunClaimNon2xxForwarded(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_, _ = w.Write([]byte(`{"error":"overloaded"}`))
	}))
	defer srv.Close()
	port := portOf(t, srv)

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim); err != nil {
		t.Fatalf("runClaim: %v", err)
	}
	ups := coord.snapshotUploads()
	if len(ups) != 1 || ups[0].status != 503 || string(ups[0].body) != `{"error":"overloaded"}` {
		t.Fatalf("non-2xx not forwarded as-is: %+v", ups)
	}
	if fails := coord.snapshotFails(); len(fails) != 0 {
		t.Fatalf("non-2xx upstream response must not be a relay failure: %+v", fails)
	}
}

// --- runClaim: reported failures (return nil, coordinator gets the code) --

func TestRunClaimWrongPortConnectFailed(t *testing.T) {
	coord := &fakeCoord{}
	claim := claimFor(9100, "/v1/chat/completions")
	err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, fakeReplica{ports: map[int]bool{}}, servingSnap(), claim)
	if err != nil {
		t.Fatalf("a reported failure must return nil, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != ConnectFailed || fails[0].retryable {
		t.Fatalf("want single non-retryable connect_failed, got %+v", fails)
	}
	if len(coord.snapshotUploads()) != 0 {
		t.Fatal("must not dial or upload for an unowned port")
	}
}

func TestRunClaimReplicaRefusedConnectFailed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	port := portOf(t, srv)
	srv.Close() // port is now unroutable

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim); err != nil {
		t.Fatalf("reported failure must return nil, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != ConnectFailed {
		t.Fatalf("want connect_failed on refused loopback dial, got %+v", fails)
	}
}

func TestRunClaimDeadlineTimeout(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		select {
		case <-release:
		case <-req.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(release)
	port := portOf(t, srv)

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	claim.DeadlineMS = 100
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim); err != nil {
		t.Fatalf("reported failure must return nil, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != TimedOut {
		t.Fatalf("want timeout, got %+v", fails)
	}
}

func TestRunClaimActiveBeforeStart(t *testing.T) {
	// Admitted while Ready, but availability is no longer Ready when handled.
	coord := &fakeCoord{}
	claim := claimFor(9100, "/v1/chat/completions")
	avail := &fakeAvail{ready: false, code: BecameActive}
	admit := AvailabilitySnapshot{Ready: true, Generation: 1}
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), avail, readyPort(9100), admit, claim); err != nil {
		t.Fatalf("reported failure must return nil, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != BecameActive || !fails[0].retryable {
		t.Fatalf("want retryable became_active, got %+v", fails)
	}
	if len(coord.snapshotUploads()) != 0 {
		t.Fatal("must not dial when already active")
	}
}

func TestRunClaimGenerationAdvancedFenced(t *testing.T) {
	// The slot is Ready again but under a newer generation than admitted it.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("must not dial a replica for a stale-generation claim")
	}))
	defer srv.Close()
	port := portOf(t, srv)

	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")
	avail := &fakeAvail{ready: true, gen: 2}
	admit := AvailabilitySnapshot{Ready: true, Generation: 1}
	if err := (Runner{Coordinator: coord}).runClaim(context.Background(), avail, readyPort(port), admit, claim); err != nil {
		t.Fatalf("reported failure must return nil, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != BecameActive || !fails[0].retryable {
		t.Fatalf("want retryable became_active for advanced generation, got %+v", fails)
	}
	if len(coord.snapshotUploads()) != 0 {
		t.Fatal("must not upload a stale-generation claim")
	}
}

func TestRunClaimReclaimMidstream(t *testing.T) {
	started := make(chan struct{})
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		_, _ = io.WriteString(w, "data: first\n\n")
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		close(started)
		select {
		case <-block:
		case <-req.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(block)
	port := portOf(t, srv)

	changed := make(chan struct{})
	avail := &fakeAvail{ready: true, changed: changed}
	admit := AvailabilitySnapshot{Ready: true, Changed: changed}
	coord := &fakeCoord{}
	claim := claimFor(port, "/v1/chat/completions")

	done := make(chan error, 1)
	go func() {
		done <- (Runner{Coordinator: coord}).runClaim(context.Background(), avail, readyPort(port), admit, claim)
	}()
	<-started
	avail.unavailable(Reclaimed)
	close(changed)

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("reported failure must return nil, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("runClaim did not stop on reclaim")
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != Reclaimed || !fails[0].retryable {
		t.Fatalf("want retryable reclaimed after midstream cancel, got %+v", fails)
	}
}

func TestRunClaimUploadDisconnect(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte("ok"))
	}))
	defer srv.Close()
	port := portOf(t, srv)

	disconnect := errors.New("coordinator disconnected")
	coord := &fakeCoord{uploadErr: disconnect}
	claim := claimFor(port, "/v1/chat/completions")
	err := (Runner{Coordinator: coord}).runClaim(context.Background(), &fakeAvail{ready: true}, readyPort(port), servingSnap(), claim)
	if !errors.Is(err, disconnect) {
		t.Fatalf("an upload-channel error must surface to stop the loop, got %v", err)
	}
	if fails := coord.snapshotFails(); len(fails) != 0 {
		t.Fatalf("a coordinator upload error is not a relay failure report: %+v", fails)
	}
}

// --- Run loop ------------------------------------------------------------

func TestRunMissingDependency(t *testing.T) {
	if err := (Runner{}).Run(context.Background(), &fakeAvail{}, readyPort(1)); err == nil {
		t.Fatal("want error for missing coordinator")
	}
}

func TestRunNilClaimKeepsWaiting(t *testing.T) {
	coord := &fakeCoord{}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- (Runner{Coordinator: coord}).Run(ctx, &fakeAvail{ready: true}, readyPort(1))
	}()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return after cancel")
	}
	coord.mu.Lock()
	calls := coord.claimCalls
	coord.mu.Unlock()
	if calls < 2 {
		t.Fatalf("expected repeated claim polls, got %d", calls)
	}
	if len(coord.snapshotUploads()) != 0 || len(coord.snapshotFails()) != 0 {
		t.Fatal("204 waits must not upload or fail")
	}
}

func TestRunClaimErrorPropagates(t *testing.T) {
	boom := errors.New("auth failed")
	coord := &fakeCoord{claimErr: boom}
	err := (Runner{Coordinator: coord}).Run(context.Background(), &fakeAvail{ready: true}, readyPort(1))
	if !errors.Is(err, boom) {
		t.Fatalf("want claim error surfaced, got %v", err)
	}
}

func TestRunPresenceChangeCancelsClaimWait(t *testing.T) {
	changed := make(chan struct{})
	block := make(chan struct{})
	defer close(block)
	coord := &fakeCoord{claimBlock: block}
	avail := &fakeAvail{ready: true, changed: changed}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- (Runner{Coordinator: coord}).Run(ctx, avail, readyPort(1))
	}()
	time.Sleep(50 * time.Millisecond)
	avail.unavailable(BecameActive)
	close(changed)
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return")
	}
	if len(coord.snapshotFails()) != 0 {
		t.Fatal("cancelling an in-flight claim wait is not a failure to report")
	}
}

func TestRunResumesAfterReportedFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	port := portOf(t, srv)

	bad := claimFor(9999, "/v1/chat/completions") // unowned port -> connect_failed
	good := claimFor(port, "/v1/chat/completions")
	block := make(chan struct{})
	defer close(block)
	coord := &fakeCoord{claimQueue: []*Claim{&bad, &good}, claimBlock: block}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- (Runner{Coordinator: coord}).Run(ctx, &fakeAvail{ready: true}, readyPort(port))
	}()
	// The good claim can only be served if the loop kept polling past the failure.
	waitFor(t, "served claim after a reported failure", func() bool { return len(coord.snapshotUploads()) >= 1 })
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != ConnectFailed {
		t.Fatalf("want one reported connect_failed, got %+v", fails)
	}
	ups := coord.snapshotUploads()
	if len(ups) != 1 || ups[0].status != 200 {
		t.Fatalf("want the later claim served, got %+v", ups)
	}
}

func TestRunTerminatesOnFailReportError(t *testing.T) {
	bad := claimFor(9999, "/v1/chat/completions")
	coord := &fakeCoord{claimQueue: []*Claim{&bad}, failErr: errors.New("failure channel down")}
	err := (Runner{Coordinator: coord}).Run(context.Background(), &fakeAvail{ready: true}, fakeReplica{ports: map[int]bool{}})
	if err == nil || !strings.Contains(err.Error(), "report connect_failed failure") {
		t.Fatalf("a broken failure channel must stop the loop, got %v", err)
	}
}

func TestRunGenerationRaceDoesNotServe(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("must not dial for a stale-generation claim")
	}))
	defer srv.Close()
	port := portOf(t, srv)

	claim := claimFor(port, "/v1/chat/completions")
	block := make(chan struct{})
	defer close(block)
	coord := &fakeCoord{claimQueue: []*Claim{&claim}, claimBlock: block}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- (Runner{Coordinator: coord}).Run(ctx, &genAvail{}, readyPort(port))
	}()
	// The generation advances between admission and serving, so the claim is fenced.
	waitFor(t, "the stale-generation claim to be fenced", func() bool { return len(coord.snapshotFails()) >= 1 })
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
	if ups := coord.snapshotUploads(); len(ups) != 0 {
		t.Fatalf("a stale-generation claim must not be served: %+v", ups)
	}
	fails := coord.snapshotFails()
	if len(fails) != 1 || fails[0].code != BecameActive || !fails[0].retryable {
		t.Fatalf("want retryable became_active for the advanced generation, got %+v", fails)
	}
}
