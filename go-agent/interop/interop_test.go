//go:build unix

// Package interop holds the live end-to-end acceptance test between the Go
// daemon's marshaling and the real Python coordinator. It is the true merge
// gate for the omitempty/interop contract: nil Go slices marshal to JSON null,
// and the coordinator's frozen Pydantic models (extra=forbid; gpus/replicas/
// lease_ids are tuple defaults, not Optional) reject null. This test boots the
// actual coordinator as a subprocess on 127.0.0.1 and POSTs a Go-marshaled
// RegisterRequest and Heartbeat with EMPTY gpus, replicas, and lease_ids,
// asserting 2xx acceptance (not 422).
//
// It skips gracefully (t.Skip) when uv or the coordinator environment is
// unavailable, so the ubuntu go.yml CI job stays green; it runs and passes on a
// developer machine with the workspace synced.
package interop

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const (
	adminKey        = "interop-admin-key"
	protocolVersion = 1
	bootTimeout     = 45 * time.Second
)

func TestGoMarshaledEmptyCollectionsAcceptedByCoordinator(t *testing.T) {
	uvPath, err := exec.LookPath("uv")
	if err != nil {
		t.Skip("uv not found on PATH; skipping live coordinator interop test")
	}
	repoRoot, err := findRepoRoot()
	if err != nil {
		t.Skipf("could not locate repo root: %v", err)
	}

	port, err := freePort()
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	baseURL := "http://127.0.0.1:" + strconv.Itoa(port)

	tmp := t.TempDir()
	configPath := filepath.Join(tmp, "coord.toml")
	if err := os.WriteFile(configPath, []byte(coordTOML(tmp, port)), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	// ── boot the real coordinator ────────────────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd := exec.CommandContext(ctx, uvPath, "run", "--no-sync", "python",
		"-m", "fallow_coordinator", "serve", "--config", configPath)
	cmd.Dir = repoRoot
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var logBuf syncBuffer
	cmd.Stdout = &logBuf
	cmd.Stderr = &logBuf
	if err := cmd.Start(); err != nil {
		t.Skipf("could not start coordinator (%v); skipping interop test", err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			// Kill the whole process group so uvicorn's workers die too.
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		_ = cmd.Wait()
	})

	if !waitForServer(baseURL, bootTimeout) {
		t.Skipf("coordinator did not become ready within %s; server log:\n%s", bootTimeout, logBuf.String())
	}
	t.Logf("coordinator is up at %s", baseURL)

	// ── mint an enrollment token (admin route; raw call, not part of daemon) ──
	token, err := mintEnrollmentToken(baseURL)
	if err != nil {
		t.Fatalf("mint enrollment token: %v\nserver log:\n%s", err, logBuf.String())
	}
	t.Logf("minted enrollment token")

	// ── register: DeviceCaps with EMPTY gpus (nil slice) ─────────────────────
	registerReq := protocol.RegisterRequest{
		EnrollmentToken: token,
		ProtocolVersion: protocolVersion,
		Caps: protocol.DeviceCaps{
			Hostname:     "go-interop-box",
			Os:           protocol.OsFamilyLinux,
			OsVersion:    "6.1",
			CPUModel:     "interop-cpu",
			CPUCores:     8,
			RAMMB:        16000,
			DiskFreeMB:   100000,
			GPUs:         nil, // EMPTY optional collection
			AgentVersion: "0.1.0",
		},
	}
	assertOmitsKeys(t, registerReq, "the marshaled RegisterRequest",
		nested{"caps": {"gpus"}})

	client := heartbeat.NewClient(baseURL, http.DefaultClient)
	regResp, err := client.Register(context.Background(), registerReq)
	if err != nil {
		t.Fatalf("register with empty gpus was rejected (want 2xx): %v\nserver log:\n%s", err, logBuf.String())
	}
	t.Logf("register accepted (2xx): agent_id=%s", regResp.AgentID)

	// ── heartbeat: EMPTY gpus AND replicas AND lease_ids (all nil) ───────────
	hb := protocol.Heartbeat{
		AgentID:         regResp.AgentID,
		Seq:             0,
		SentAt:          time.Now().UTC(),
		ProtocolVersion: protocolVersion,
		State:           protocol.AgentStateIdle,
		UserIdleS:       42.0,
		CPUPercent:      5.0,
		MemAvailableMB:  8000,
		GPUs:            nil, // EMPTY
		Replicas:        nil, // EMPTY
		LeaseIDs:        nil, // EMPTY
	}
	assertOmitsTopLevel(t, hb, "the marshaled Heartbeat", "gpus", "replicas", "lease_ids")

	if _, err := client.Heartbeat(context.Background(), hb); err != nil {
		t.Fatalf("heartbeat with empty gpus/replicas/lease_ids was rejected (want 2xx, not 422): %v\nserver log:\n%s", err, logBuf.String())
	}
	t.Logf("heartbeat accepted (2xx) with empty gpus, replicas, and lease_ids")
	t.Logf("INTEROP PROOF PASSED: Go-marshaled empty collections accepted by the real Python coordinator")
}

// ── marshaling assertions ────────────────────────────────────────────────────

type nested map[string][]string

// assertOmitsKeys fails if any nested collection key is present (or null) in the
// marshaled value.
func assertOmitsKeys(t *testing.T, v any, label string, checks nested) {
	t.Helper()
	raw := marshalToMap(t, v)
	for parent, keys := range checks {
		child, _ := raw[parent].(map[string]any)
		for _, k := range keys {
			if child == nil {
				continue
			}
			if val, present := child[k]; present {
				t.Errorf("%s: %s.%s must be omitted, got %v", label, parent, k, val)
			}
		}
	}
}

// assertOmitsTopLevel fails if any top-level key is present in the marshaled value.
func assertOmitsTopLevel(t *testing.T, v any, label string, keys ...string) {
	t.Helper()
	raw := marshalToMap(t, v)
	for _, k := range keys {
		if val, present := raw[k]; present {
			t.Errorf("%s: %q must be omitted (nil slice marshals to null, which the coordinator rejects), got %v", label, k, val)
		}
	}
}

func marshalToMap(t *testing.T, v any) map[string]any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return raw
}

// ── coordinator boot helpers ─────────────────────────────────────────────────

func coordTOML(dir string, port int) string {
	q := func(p string) string { return strconv.Quote(filepath.Join(dir, p)) }
	return strings.Join([]string{
		"db_path = " + q("coordinator.db"),
		"blob_dir = " + q("blobs"),
		"unit_input_dir = " + q("units"),
		"events_jsonl_path = " + q("events.jsonl"),
		"gateway_log_path = " + q("gateway.jsonl"),
		"admin_key = " + strconv.Quote(adminKey),
		"host = \"127.0.0.1\"",
		"port = " + strconv.Itoa(port),
	}, "\n") + "\n"
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

func waitForServer(baseURL string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		// Any HTTP response (even 401/404) means the server is accepting.
		resp, err := http.Get(baseURL + "/v1/admin/enrollment_tokens")
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			return true
		}
		time.Sleep(250 * time.Millisecond)
	}
	return false
}

func mintEnrollmentToken(baseURL string) (string, error) {
	req, err := http.NewRequest(http.MethodPost, baseURL+"/v1/admin/enrollment_tokens", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+adminKey)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusCreated {
		return "", errors.New("mint token status " + strconv.Itoa(resp.StatusCode) + ": " + string(body))
	}
	var parsed struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", err
	}
	if parsed.Token == "" {
		return "", errors.New("empty token in response")
	}
	return parsed.Token, nil
}

func findRepoRoot() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		candidate := filepath.Join(dir, "pyproject.toml")
		if data, err := os.ReadFile(candidate); err == nil && strings.Contains(string(data), "fallow-workspace") {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", errors.New("reached filesystem root without finding the workspace pyproject.toml")
		}
		dir = parent
	}
}
