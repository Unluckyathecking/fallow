// Command agentctl is a thin, scriptable driver over the Go agent's coordinator
// client, used by the Python parity harness (E4.4) to exercise the real
// coordinator over loopback HTTP.
//
// Each subcommand performs exactly one agent->coordinator action and prints its
// result as a single JSON object on stdout, so the Python harness can thread the
// agent_id and device_token between calls and assert on coordinator state. This
// is not the production daemon (that lands later in E4); it deliberately owns no
// loop, no idle detection and no supervisor — only the wire calls the parity
// scenarios need. Errors go to stderr with a non-zero exit.
//
// Usage:
//
//	agentctl register  -url URL -token TOKEN [-hostname H] [-state PATH]
//	agentctl heartbeat -url URL -agent-id ID -token DEVTOK [-state-name idle|active|draining]
//	                   [-seq N] [-replica model:port:state ...]
//	agentctl poll      -url URL -agent-id ID -token DEVTOK [-timeout S]
//	agentctl upload    -url URL -agent-id ID -token DEVTOK -unit UID -attempt N -payload BYTES
//	agentctl complete  -url URL -agent-id ID -token DEVTOK -unit UID -attempt N [-result-ref REF]
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// protocolVersion is the wire version this driver speaks; it must match
// fallow_protocol.version.PROTOCOL_VERSION.
const protocolVersion = 1

// version and commit are stamped at release time via -ldflags -X (see
// go-agent/.goreleaser.yaml). They stay at these defaults for a plain
// `go build`.
var (
	version = "dev"
	commit  = "none"
)

// leaseAttemptHeader carries the lease attempt on result writes, matching the
// coordinator's X-Fallow-Lease-Attempt contract.
const leaseAttemptHeader = "X-Fallow-Lease-Attempt"

func main() {
	if len(os.Args) < 2 {
		fail("usage: agentctl <register|heartbeat|poll|upload|complete|version> [flags]")
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "version":
		err = emit(map[string]string{"version": version, "commit": commit})
	case "register":
		err = runRegister(args)
	case "heartbeat":
		err = runHeartbeat(args)
	case "poll":
		err = runPoll(args)
	case "upload":
		err = runUpload(args)
	case "complete":
		err = runComplete(args)
	default:
		fail("unknown subcommand %q", cmd)
	}
	if err != nil {
		fail("%s: %v", cmd, err)
	}
}

func runRegister(args []string) error {
	fs := newFlagSet("register")
	url := fs.String("url", "", "coordinator base URL")
	token := fs.String("token", "", "enrollment token")
	hostname := fs.String("hostname", "pc1", "device hostname")
	statePath := fs.String("state", "", "path to persist the identity (0600)")
	mustParse(fs, args)

	client := heartbeat.NewClient(*url, nil)
	resp, err := client.Register(context.Background(), protocol.RegisterRequest{
		EnrollmentToken: *token,
		ProtocolVersion: protocolVersion,
		Caps:            makeCaps(*hostname),
	})
	if err != nil {
		return err
	}
	id := state.Identity{AgentID: resp.AgentID, DeviceToken: resp.DeviceToken}
	if *statePath != "" {
		if err := state.Save(*statePath, id); err != nil {
			return err
		}
	}
	return emit(id)
}

func runHeartbeat(args []string) error {
	fs := newFlagSet("heartbeat")
	url := fs.String("url", "", "coordinator base URL")
	agentID := fs.String("agent-id", "", "enrolled agent id")
	token := fs.String("token", "", "device token")
	stateName := fs.String("state-name", "idle", "agent state: idle|active|draining")
	seq := fs.Int("seq", 1, "heartbeat sequence number")
	var replicas replicaFlags
	fs.Var(&replicas, "replica", "replica as model:port:state (repeatable)")
	mustParse(fs, args)

	parsed, err := replicas.parse()
	if err != nil {
		return err
	}
	client := heartbeat.NewClient(*url, nil, heartbeat.WithIdentity(*agentID, *token))
	resp, err := client.Heartbeat(context.Background(), protocol.Heartbeat{
		AgentID:         *agentID,
		Seq:             *seq,
		SentAt:          time.Now().UTC(),
		ProtocolVersion: protocolVersion,
		State:           protocol.AgentState(*stateName),
		UserIdleS:       300.0,
		CPUPercent:      5.0,
		MemAvailableMB:  8192,
		Replicas:        parsed,
	})
	if err != nil {
		return err
	}
	return emit(map[string][]string{"desired_models": resp.DesiredModels})
}

func runPoll(args []string) error {
	fs := newFlagSet("poll")
	url := fs.String("url", "", "coordinator base URL")
	agentID := fs.String("agent-id", "", "enrolled agent id")
	token := fs.String("token", "", "device token")
	timeout := fs.Float64("timeout", 0.0, "long-poll timeout in seconds")
	mustParse(fs, args)

	client := heartbeat.NewClient(*url, nil, heartbeat.WithIdentity(*agentID, *token))
	lease, err := client.PollWork(context.Background(), *timeout)
	if err != nil {
		return err
	}
	return emit(struct {
		Lease *protocol.WorkUnitLease `json:"lease"`
	}{Lease: lease})
}

// runUpload posts the result payload the coordinator must bind before it accepts
// a SUCCEEDED completion, and prints the returned sha256 result_ref. The Go
// client has no result-upload helper yet, so this drives the raw endpoint.
func runUpload(args []string) error {
	fs := newFlagSet("upload")
	url := fs.String("url", "", "coordinator base URL")
	agentID := fs.String("agent-id", "", "enrolled agent id")
	token := fs.String("token", "", "device token")
	unit := fs.String("unit", "", "work-unit id")
	attempt := fs.Int("attempt", 0, "lease attempt")
	payload := fs.String("payload", "", "result payload bytes")
	mustParse(fs, args)

	endpoint := fmt.Sprintf(
		"%s/v1/agents/%s/work_units/%s/payload", strings.TrimRight(*url, "/"), *agentID, *unit,
	)
	req, err := http.NewRequestWithContext(
		context.Background(), http.MethodPost, endpoint, bytes.NewReader([]byte(*payload)),
	)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+*token)
	req.Header.Set(leaseAttemptHeader, strconv.Itoa(*attempt))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("payload upload HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var out struct {
		ResultRef string `json:"result_ref"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return err
	}
	return emit(map[string]string{"result_ref": out.ResultRef})
}

func runComplete(args []string) error {
	fs := newFlagSet("complete")
	url := fs.String("url", "", "coordinator base URL")
	agentID := fs.String("agent-id", "", "enrolled agent id")
	token := fs.String("token", "", "device token")
	unit := fs.String("unit", "", "work-unit id")
	attempt := fs.Int("attempt", 0, "lease attempt")
	resultRef := fs.String("result-ref", "", "result ref (defaults to result://<unit>)")
	mustParse(fs, args)

	ref := *resultRef
	if ref == "" {
		ref = "result://" + *unit
	}
	items := 1
	client := heartbeat.NewClient(*url, nil, heartbeat.WithIdentity(*agentID, *token))
	err := client.CompleteUnit(context.Background(), protocol.WorkResult{
		WorkUnitID: *unit,
		Status:     protocol.WorkResultStatusSucceeded,
		ResultRef:  &ref,
		Metrics:    &protocol.WorkMetrics{DurationS: 1.0, Items: &items},
	}, *attempt)
	if err != nil {
		return err
	}
	return emit(map[string]bool{"ok": true})
}

// makeCaps mirrors integration_helpers.make_caps so the parity harness registers
// with the same device capabilities the Python agent tests use.
func makeCaps(hostname string) protocol.DeviceCaps {
	return protocol.DeviceCaps{
		Hostname:     hostname,
		Os:           protocol.OsFamilyLinux,
		OsVersion:    "6.1",
		CPUModel:     "test-cpu",
		CPUCores:     8,
		RAMMB:        16384,
		DiskFreeMB:   100000,
		AgentVersion: "0.1.0",
	}
}

// replicaFlags collects repeated -replica model:port:state values.
type replicaFlags []string

func (r *replicaFlags) String() string { return strings.Join(*r, ",") }

func (r *replicaFlags) Set(v string) error {
	*r = append(*r, v)
	return nil
}

func (r replicaFlags) parse() ([]protocol.ReplicaStatus, error) {
	if len(r) == 0 {
		return nil, nil
	}
	out := make([]protocol.ReplicaStatus, 0, len(r))
	for _, spec := range r {
		parts := strings.Split(spec, ":")
		if len(parts) != 3 {
			return nil, fmt.Errorf("bad replica %q, want model:port:state", spec)
		}
		port, err := strconv.Atoi(parts[1])
		if err != nil {
			return nil, fmt.Errorf("bad replica port in %q: %w", spec, err)
		}
		out = append(out, protocol.ReplicaStatus{
			ModelID: parts[0],
			Port:    port,
			State:   protocol.ReplicaState(parts[2]),
		})
	}
	return out, nil
}

func newFlagSet(name string) *flag.FlagSet {
	return flag.NewFlagSet(name, flag.ContinueOnError)
}

func mustParse(fs *flag.FlagSet, args []string) {
	if err := fs.Parse(args); err != nil {
		fail("%s: %v", fs.Name(), err)
	}
}

// emit writes v as a single compact JSON line on stdout.
func emit(v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	fmt.Println(string(data))
	return nil
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
