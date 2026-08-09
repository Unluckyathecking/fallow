package runtime

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/inference"
)

// validClaimJSON is a well-formed relay-v1 claim body the coordinator returns.
func validClaimJSON(gen int64, port int) []byte {
	body, _ := json.Marshal(map[string]any{
		"version":             1,
		"claim_id":            "claim-0123456789abcdef",
		"presence_generation": gen,
		"replica_port":        port,
		"method":              "POST",
		"path":                "/v1/chat/completions",
		"content_type":        "application/json",
		"body_b64":            base64.StdEncoding.EncodeToString([]byte(`{"model":"m"}`)),
		"deadline_ms":         30000,
	})
	return body
}

// relayServer mirrors the merged relay-v1 routes (agent_routes.py) so the client
// is exercised against the real path, header and status contract.
type relayServer struct {
	agentID string
	token   string

	mu             sync.Mutex
	claimHits      int
	responseHits   int
	failureHits    int
	gotGeneration  string
	gotUpstream    string
	gotFailBody    map[string]any
	responseStatus int
}

func newRelayServer(t *testing.T, agentID, token string) (*relayServer, *httptest.Server) {
	rs := &relayServer{agentID: agentID, token: token, responseStatus: http.StatusAccepted}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/agents/"+agentID+"/inference/claims", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		rs.mu.Lock()
		rs.claimHits++
		hits := rs.claimHits
		rs.mu.Unlock()
		if r.URL.Query().Get("timeout_s") == "" {
			t.Errorf("claim request missing timeout_s query")
		}
		if hits == 1 {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(validClaimJSON(42, 8100))
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/v1/agents/"+agentID+"/inference/claims/claim-0123456789abcdef/response",
		func(w http.ResponseWriter, r *http.Request) {
			rs.mu.Lock()
			rs.responseHits++
			rs.gotGeneration = r.Header.Get("X-Fallow-Presence-Generation")
			rs.gotUpstream = r.Header.Get("X-Fallow-Upstream-Status")
			status := rs.responseStatus
			rs.mu.Unlock()
			_, _ = io.Copy(io.Discard, r.Body)
			w.WriteHeader(status)
		})
	mux.HandleFunc("/v1/agents/"+agentID+"/inference/claims/claim-0123456789abcdef/failure",
		func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			rs.mu.Lock()
			rs.failureHits++
			rs.gotFailBody = body
			rs.mu.Unlock()
			w.WriteHeader(http.StatusAccepted)
		})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return rs, srv
}

// TestRelayClaimDecodesAndReturnsNilOn204 verifies the claim path against the
// merged route: a 200 returns a decoded claim, a 204 returns (nil, nil).
func TestRelayClaimDecodesAndReturnsNilOn204(t *testing.T) {
	rs, srv := newRelayServer(t, "agent-1", "dev-tok")
	c := newRelayClient(srv.URL, "agent-1", "dev-tok", srv.Client())

	claim, err := c.Claim(context.Background(), 25*time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if claim == nil || claim.ClaimID != "claim-0123456789abcdef" || claim.ReplicaPort != 8100 {
		t.Fatalf("claim = %+v", claim)
	}

	none, err := c.Claim(context.Background(), 25*time.Second)
	if err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if none != nil {
		t.Fatalf("expected nil claim on 204, got %+v", none)
	}
	_ = rs
}

// TestRelayUploadSendsFencingHeaders verifies the response upload sends the
// presence generation and upstream status the merged route reads, and treats
// 202 as success.
func TestRelayUploadSendsFencingHeaders(t *testing.T) {
	rs, srv := newRelayServer(t, "agent-1", "dev-tok")
	c := newRelayClient(srv.URL, "agent-1", "dev-tok", srv.Client())
	claim := inference.Claim{ClaimID: "claim-0123456789abcdef", PresenceGeneration: 42}

	err := c.Upload(context.Background(), claim, 200, "text/event-stream", strings.NewReader("data: hi\n\n"))
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	rs.mu.Lock()
	defer rs.mu.Unlock()
	if rs.responseHits != 1 {
		t.Fatalf("response route hit %d times, want 1", rs.responseHits)
	}
	if rs.gotGeneration != "42" {
		t.Errorf("X-Fallow-Presence-Generation = %q, want 42", rs.gotGeneration)
	}
	if rs.gotUpstream != "200" {
		t.Errorf("X-Fallow-Upstream-Status = %q, want 200", rs.gotUpstream)
	}
}

// TestRelayUploadFailsOnGone maps a 410 (newer generation invalidated the claim)
// to an error rather than swallowing it.
func TestRelayUploadFailsOnGone(t *testing.T) {
	rs, srv := newRelayServer(t, "agent-1", "dev-tok")
	rs.responseStatus = http.StatusGone
	c := newRelayClient(srv.URL, "agent-1", "dev-tok", srv.Client())
	claim := inference.Claim{ClaimID: "claim-0123456789abcdef", PresenceGeneration: 42}
	if err := c.Upload(context.Background(), claim, 200, "application/json", strings.NewReader("x")); err == nil {
		t.Fatal("expected an error on HTTP 410, got nil")
	}
}

// TestRelayFailSendsTypedBody verifies the failure report sends exactly the
// relay-v1 JSON (presence_generation, code, retryable) the route's forbid-extra
// model accepts.
func TestRelayFailSendsTypedBody(t *testing.T) {
	rs, srv := newRelayServer(t, "agent-1", "dev-tok")
	c := newRelayClient(srv.URL, "agent-1", "dev-tok", srv.Client())
	claim := inference.Claim{ClaimID: "claim-0123456789abcdef", PresenceGeneration: 42}

	if err := c.Fail(context.Background(), claim, inference.BecameActive, true); err != nil {
		t.Fatalf("fail: %v", err)
	}
	rs.mu.Lock()
	defer rs.mu.Unlock()
	if rs.failureHits != 1 {
		t.Fatalf("failure route hit %d times, want 1", rs.failureHits)
	}
	if rs.gotFailBody["code"] != "became_active" {
		t.Errorf("code = %v, want became_active", rs.gotFailBody["code"])
	}
	if rs.gotFailBody["retryable"] != true {
		t.Errorf("retryable = %v, want true", rs.gotFailBody["retryable"])
	}
	if _, ok := rs.gotFailBody["presence_generation"]; !ok {
		t.Error("failure body missing presence_generation")
	}
	if len(rs.gotFailBody) != 3 {
		t.Errorf("failure body has %d keys, want exactly 3 (forbid-extra route)", len(rs.gotFailBody))
	}
}
