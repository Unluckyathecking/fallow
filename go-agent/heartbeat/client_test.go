package heartbeat

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const (
	testAgentID     = "agent-42"
	testDeviceToken = "dev-tok-abc"
	testEnrollToken = "enroll-xyz"
	protocolVersion = 1
)

func noopSleep(time.Duration) {}

// newTestClient wires a Client to an httptest server with a seeded identity and
// an instant no-op sleeper, mirroring the Python make_client helper.
func newTestClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return NewClient(srv.URL, srv.Client(),
		WithIdentity(testAgentID, testDeviceToken), WithSleep(noopSleep))
}

func sampleCaps() protocol.DeviceCaps {
	return protocol.DeviceCaps{
		Hostname:     "box-1",
		Os:           protocol.OsFamilyLinux,
		OsVersion:    "6.1",
		CPUModel:     "Test CPU",
		CPUCores:     8,
		RAMMB:        32000,
		DiskFreeMB:   100000,
		AgentVersion: "0.1.0",
	}
}

func sampleHeartbeat() protocol.Heartbeat {
	return protocol.Heartbeat{
		AgentID:         testAgentID,
		Seq:             0,
		SentAt:          time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC),
		ProtocolVersion: protocolVersion,
		State:           protocol.AgentStateIdle,
		UserIdleS:       1.0,
		CPUPercent:      5.0,
		MemAvailableMB:  1000,
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return b
}

func TestRegisterStoresIdentityAndSendsNoBearer(t *testing.T) {
	var seenAuth string
	var seenPath string
	var seenBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenAuth = r.Header.Get("Authorization")
		seenPath = r.URL.Path
		seenBody, _ = io.ReadAll(r.Body)
		resp := protocol.RegisterResponse{AgentID: testAgentID, DeviceToken: testDeviceToken}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(mustJSON(t, resp))
	}))
	t.Cleanup(srv.Close)

	client := NewClient(srv.URL, srv.Client(), WithSleep(noopSleep))
	req := protocol.RegisterRequest{
		EnrollmentToken: testEnrollToken,
		ProtocolVersion: protocolVersion,
		Caps:            sampleCaps(),
	}

	resp, err := client.Register(context.Background(), req)
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if resp.AgentID != testAgentID {
		t.Errorf("agent id = %q", resp.AgentID)
	}
	if client.AgentID() != testAgentID || client.DeviceToken() != testDeviceToken {
		t.Errorf("identity not stored: %q %q", client.AgentID(), client.DeviceToken())
	}
	if seenAuth != "" {
		t.Errorf("registration carried a bearer: %q", seenAuth)
	}
	if seenPath != "/v1/agents/register" {
		t.Errorf("register path = %q", seenPath)
	}
	if !strings.Contains(string(seenBody), testEnrollToken) {
		t.Errorf("enrollment token missing from body: %s", seenBody)
	}
}

func TestHeartbeatReturnsParsedResponseWithBearer(t *testing.T) {
	var seenAuth string
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		seenAuth = r.Header.Get("Authorization")
		resp := protocol.HeartbeatResponse{DesiredModels: []string{"qwen"}, RevokedLeaseIDs: []string{"l1"}}
		_, _ = w.Write(mustJSON(t, resp))
	})

	resp, err := client.Heartbeat(context.Background(), sampleHeartbeat())
	if err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if len(resp.DesiredModels) != 1 || resp.DesiredModels[0] != "qwen" {
		t.Errorf("desired = %v", resp.DesiredModels)
	}
	if seenAuth != "Bearer "+testDeviceToken {
		t.Errorf("auth = %q", seenAuth)
	}
}

func TestPollWork204ReturnsNil(t *testing.T) {
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	lease, err := client.PollWork(context.Background(), 5.0)
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if lease != nil {
		t.Errorf("expected nil lease, got %+v", lease)
	}
}

func TestPollWork200ReturnsLeaseAndSendsTimeout(t *testing.T) {
	var seenTimeout string
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		seenTimeout = r.URL.Query().Get("timeout")
		lease := protocol.WorkUnitLease{
			WorkUnitID:   "u1",
			JobID:        "j1",
			Kind:         protocol.WorkerKindEmbed,
			ModelID:      "bge",
			InputURL:     "http://coordinator.test/input/u1",
			LeaseExpires: time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC),
			Attempt:      1,
		}
		_, _ = w.Write(mustJSON(t, lease))
	})

	lease, err := client.PollWork(context.Background(), 30.0)
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if lease == nil || lease.WorkUnitID != "u1" {
		t.Fatalf("lease = %+v", lease)
	}
	if seenTimeout != "30" {
		t.Errorf("timeout param = %q", seenTimeout)
	}
}

func TestCompleteUnitPostsToResultPath(t *testing.T) {
	var seenPath, seenAttempt string
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.Path
		seenAttempt = r.Header.Get("X-Fallow-Lease-Attempt")
		w.WriteHeader(http.StatusNoContent)
	})
	result := protocol.WorkResult{WorkUnitID: "u9", Status: protocol.WorkResultStatusSucceeded}

	if err := client.CompleteUnit(context.Background(), result, 3); err != nil {
		t.Fatalf("complete: %v", err)
	}
	if seenPath != "/v1/agents/"+testAgentID+"/work_units/u9/result" {
		t.Errorf("path = %q", seenPath)
	}
	if seenAttempt != "3" {
		t.Errorf("attempt header = %q", seenAttempt)
	}
}

func TestPushEventPostsToEventsPath(t *testing.T) {
	var seenPath string
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.Path
		w.WriteHeader(http.StatusAccepted)
	})
	event := protocol.AgentEvent{
		AgentID: testAgentID,
		Kind:    protocol.EventKindUserReturned,
		At:      time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC),
	}
	if err := client.PushEvent(context.Background(), event); err != nil {
		t.Fatalf("push: %v", err)
	}
	if seenPath != "/v1/agents/"+testAgentID+"/events" {
		t.Errorf("path = %q", seenPath)
	}
}

func TestStatusCodeClassification(t *testing.T) {
	tests := []struct {
		name   string
		status int
		assert func(error) bool
	}{
		{"401 auth", http.StatusUnauthorized, isAuth},
		{"403 auth", http.StatusForbidden, isAuth},
		{"503 transient", http.StatusServiceUnavailable, isTransient},
		{"500 transient", http.StatusInternalServerError, isTransient},
		{"418 protocol", http.StatusTeapot, isProtocol},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
			})
			_, err := client.Heartbeat(context.Background(), sampleHeartbeat())
			if err == nil || !tc.assert(err) {
				t.Fatalf("status %d: got %v", tc.status, err)
			}
		})
	}
}

func TestMalformedBodyRaisesProtocolError(t *testing.T) {
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("{not json"))
	})
	_, err := client.Heartbeat(context.Background(), sampleHeartbeat())
	if !isProtocol(err) {
		t.Fatalf("expected protocol error, got %v", err)
	}
}

// failTransport fails the first failFor calls with a transport error, then
// delegates to next. It models httpx.ConnectError injection.
type failTransport struct {
	failFor int
	calls   *int
	next    http.RoundTripper
}

func (f failTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	*f.calls++
	if *f.calls <= f.failFor {
		return nil, errors.New("connect error boom")
	}
	if f.next != nil {
		return f.next.RoundTrip(req)
	}
	return nil, errors.New("connect error boom")
}

func TestTransportErrorRetriesThenSucceeds(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(mustJSON(t, protocol.HeartbeatResponse{}))
	}))
	t.Cleanup(srv.Close)

	calls := 0
	doer := &http.Client{Transport: failTransport{failFor: 1, calls: &calls, next: srv.Client().Transport}}
	client := NewClient(srv.URL, doer, WithIdentity(testAgentID, testDeviceToken), WithSleep(noopSleep))

	_, err := client.Heartbeat(context.Background(), sampleHeartbeat())
	if err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if calls != 2 {
		t.Errorf("calls = %d, want 2 (first fail, retry ok)", calls)
	}
}

func TestTransportErrorExhaustsRetriesThenRaisesTransient(t *testing.T) {
	calls := 0
	doer := &http.Client{Transport: failTransport{failFor: 100, calls: &calls}}
	client := NewClient("http://coordinator.test", doer,
		WithIdentity(testAgentID, testDeviceToken),
		WithRetry(RetryConfig{MaxRetries: 2, BackoffBase: time.Millisecond}),
		WithSleep(noopSleep))

	_, err := client.PollWork(context.Background(), 1.0)
	if !isTransient(err) {
		t.Fatalf("expected transient, got %v", err)
	}
	if calls != 3 {
		t.Errorf("calls = %d, want 3 (initial + 2 retries)", calls)
	}
}

func TestRegisterIsNeverRetriedOnTransportError(t *testing.T) {
	calls := 0
	doer := &http.Client{Transport: failTransport{failFor: 100, calls: &calls}}
	client := NewClient("http://coordinator.test", doer, WithSleep(noopSleep))
	req := protocol.RegisterRequest{EnrollmentToken: testEnrollToken, ProtocolVersion: protocolVersion, Caps: sampleCaps()}

	_, err := client.Register(context.Background(), req)
	if !isTransient(err) {
		t.Fatalf("expected transient, got %v", err)
	}
	if calls != 1 {
		t.Errorf("calls = %d, want 1 (no retry)", calls)
	}
}

func TestAuthedCallWithoutTokenRaisesProtocolError(t *testing.T) {
	client := NewClient("http://coordinator.test", http.DefaultClient, WithSleep(noopSleep))
	_, err := client.PollWork(context.Background(), 1.0)
	if !isProtocol(err) {
		t.Fatalf("expected protocol error, got %v", err)
	}
}

// ── typed-error helpers ──────────────────────────────────────────────────────

func isAuth(err error) bool      { var e *AuthError; return errors.As(err, &e) }
func isTransient(err error) bool { var e *TransientError; return errors.As(err, &e) }
func isProtocol(err error) bool  { var e *ProtocolError; return errors.As(err, &e) }
