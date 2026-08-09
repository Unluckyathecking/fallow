// Command agentctl runs the Go agent, either as the persistent daemon or as one
// of the scriptable one-shot subcommands.
//
// The `run` subcommand is the production daemon: it reads the shared agent TOML
// config, enrolls (or resumes from the 0600 state file), and composes the
// coordinator client, preemption controller, and process supervisor into one run
// loop that heartbeats, polls for work, drives the preempt state machine, and
// shuts down cleanly on SIGINT/SIGTERM.
//
// The `reclaim` and `release` subcommands are the user's instant takedown
// control (ADR 042): they write and remove the reclaim flag file the running
// daemon's poll loop watches, reading the same config so the flag lands beside
// the same state file.
//
// The one-shot subcommands (register, heartbeat, poll, upload, complete) each
// perform exactly one agent->coordinator action and print its result as a single
// JSON object on stdout. They own no loop, no idle detection, and no supervisor —
// only the wire calls the Python parity harness (E4.4) threads together to assert
// on coordinator state. Errors go to stderr with a non-zero exit.
//
// Usage:
//
//	agentctl run       -config PATH
//	agentctl reclaim   -config PATH
//	agentctl release   -config PATH
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
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/heartbeat"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/runtime"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
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
		fail("usage: agentctl <run|doctor|reclaim|release|register|heartbeat|poll|upload|complete|version> [flags]")
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "version":
		err = emit(map[string]string{"version": version, "commit": commit})
	case "run":
		err = runDaemon(args)
	case "doctor":
		err = runDoctor(args)
	case "reclaim":
		err = runControl(args, true)
	case "release":
		err = runControl(args, false)
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

// runDaemon reads the config and runs the agent until SIGINT/SIGTERM. Unlike the
// one-shot subcommands it prints no JSON: it logs to stderr and blocks.
func runDaemon(args []string) error {
	fs := newFlagSet("run")
	configPath := fs.String("config", "", "path to the agent TOML config")
	mustParse(fs, args)
	if *configPath == "" {
		return fmt.Errorf("-config is required")
	}

	settings, err := config.Load(*configPath, os.Getenv)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runtime.New(settings, runtime.Seams{}).Run(ctx)
}

// doctorReport is the single JSON object `doctor` prints. Each check is a small
// object so an operator or a script can read pass/fail plus a reason per lane.
type doctorReport struct {
	Mode      string      `json:"mode"`
	Config    doctorCheck `json:"config"`
	Identity  doctorCheck `json:"identity"`
	Llama     doctorCheck `json:"llama"`
	PinnedTLS doctorCheck `json:"pinned_tls"`
	Clock     doctorCheck `json:"clock"`
	OK        bool        `json:"ok"`
}

type doctorCheck struct {
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

// runDoctor performs read-only config, identity, llama-path, pinned-TLS and
// clock checks and prints one JSON object. It never registers or claims work.
// The pinned-TLS check validates the pin set statically; only the Site Mode
// clock check opens a connection, and it sends no token. It exits non-zero when
// a required check fails.
func runDoctor(args []string) error {
	fs := newFlagSet("doctor")
	configPath := fs.String("config", "", "path to the agent TOML config")
	mustParse(fs, args)
	if *configPath == "" {
		return fmt.Errorf("-config is required")
	}

	rep := doctorReport{Mode: "direct"}
	settings, err := config.Load(*configPath, os.Getenv)
	if err != nil {
		rep.Config = doctorCheck{OK: false, Detail: err.Error()}
		return emitDoctor(rep)
	}
	rep.Config = doctorCheck{OK: true}
	if settings.SiteMode() {
		rep.Mode = "site"
	}

	rep.Identity = doctorIdentity(settings)
	rep.Llama = doctorLlama(settings)
	rep.PinnedTLS = doctorPinnedTLS(settings)
	rep.Clock = doctorClock(settings, time.Now)

	rep.OK = rep.Config.OK && rep.Identity.OK && rep.Llama.OK && rep.PinnedTLS.OK && rep.Clock.OK
	return emitDoctor(rep)
}

// doctorIdentity reports whether a durable identity is on disk. An unenrolled
// machine is reported, not failed: doctor runs before first enrollment too.
func doctorIdentity(settings config.Settings) doctorCheck {
	id, err := state.Load(settings.StatePath)
	if err != nil {
		return doctorCheck{OK: false, Detail: err.Error()}
	}
	if id == nil {
		return doctorCheck{OK: true, Detail: "not enrolled"}
	}
	detail := "enrolled agent_id=" + id.AgentID
	if id.Site != nil {
		detail += " site_id=" + id.Site.SiteID
	}
	return doctorCheck{OK: true, Detail: detail}
}

// doctorLlama reports whether the configured llama-server binary exists.
func doctorLlama(settings config.Settings) doctorCheck {
	if settings.LlamaServerBinary == "" {
		return doctorCheck{OK: false, Detail: "llama_server_binary is unset"}
	}
	info, err := os.Stat(settings.LlamaServerBinary)
	if err != nil {
		return doctorCheck{OK: false, Detail: err.Error()}
	}
	if info.IsDir() {
		return doctorCheck{OK: false, Detail: "llama_server_binary is a directory"}
	}
	return doctorCheck{OK: true, Detail: settings.LlamaServerBinary}
}

// doctorPinnedTLS statically validates the pinned client for Site Mode. It is a
// no-op (and always OK) for direct agents. It builds the pinned client from the
// persisted profile or the join file, which fails closed on a malformed pin set,
// but opens no network connection.
func doctorPinnedTLS(settings config.Settings) doctorCheck {
	if !settings.SiteMode() {
		return doctorCheck{OK: true, Detail: "not site mode"}
	}
	profile, source, err := doctorProfile(settings)
	if err != nil {
		return doctorCheck{OK: false, Detail: err.Error()}
	}
	// Fail closed on a corrupt profile with no usable coordinator origin before
	// anything indexes into the URL list.
	if len(profile.CoordinatorURLs) == 0 {
		return doctorCheck{OK: false, Detail: "site profile has no coordinator URL (" + source + ")"}
	}
	if u, perr := url.Parse(profile.CoordinatorURLs[0]); perr != nil || u.Scheme != "https" || u.Hostname() == "" {
		return doctorCheck{OK: false, Detail: "site profile coordinator URL is not a usable https origin (" + source + ")"}
	}
	if _, err := siteclient.NewPinnedClient(profile); err != nil {
		return doctorCheck{OK: false, Detail: err.Error()}
	}
	return doctorCheck{OK: true, Detail: "pins valid (" + source + ")"}
}

// doctorProfile resolves the Site Mode profile from the persisted identity if it
// exists, else from the configured join file, without consuming the token.
func doctorProfile(settings config.Settings) (siteclient.Profile, string, error) {
	if id, err := state.Load(settings.StatePath); err == nil && id != nil && id.Site != nil {
		return siteclient.Profile{
			SiteID:                id.Site.SiteID,
			CoordinatorURLs:       id.Site.CoordinatorURLs,
			CoordinatorSPKISHA256: id.Site.CoordinatorSPKISHA256,
			MDNSService:           id.Site.MDNSService,
		}, "persisted profile", nil
	}
	data, err := os.ReadFile(settings.SiteJoinBundle)
	if err != nil {
		return siteclient.Profile{}, "", fmt.Errorf("read join file: %w", err)
	}
	bundle, err := siteclient.ParseJoin(data)
	if err != nil {
		return siteclient.Profile{}, "", fmt.Errorf("parse join file: %w", err)
	}
	return bundle.Profile(), "join file", nil
}

// Clock-check bounds. maxClockSkew is the offset above which a drifted PC clock
// is flagged: certificate validity is the first thing a bad clock breaks, and it
// breaks as an opaque pinned-TLS error. clockProbeTimeout bounds the single
// request so doctor stays fast on a LAN with no coordinator listening.
const (
	maxClockSkew      = 120 * time.Second
	clockProbeTimeout = 5 * time.Second
)

// doctorClock reports the signed offset between local time and the coordinator's
// clock for a Site Mode agent, and is a no-op for direct agents.
//
// It is not OK only when the offset was measured and exceeds maxClockSkew. Every
// other outcome — no usable profile, an unreachable coordinator, a pin failure,
// no parsable Date header — is reported as OK with the reason named, because
// doctor cannot conclude the clock is wrong from any of them, and config and
// pinned_tls already own those failures.
func doctorClock(settings config.Settings, now func() time.Time) doctorCheck {
	if !settings.SiteMode() {
		return doctorCheck{OK: true, Detail: "not site mode"}
	}
	profile, source, err := doctorProfile(settings)
	if err != nil {
		return doctorCheck{OK: true, Detail: "skew unknown: " + err.Error()}
	}
	if len(profile.CoordinatorURLs) == 0 {
		return doctorCheck{OK: true, Detail: "skew unknown: site profile has no coordinator URL (" + source + ")"}
	}
	client, err := siteclient.NewPinnedClient(profile)
	if err != nil {
		return doctorCheck{OK: true, Detail: "skew unknown: " + err.Error()}
	}
	return clockCheck(client, profile.CoordinatorURLs[0], now)
}

// clockCheck reads the coordinator's Date header over the pinned client and
// compares it with local time. The request carries no token: the Date header
// arrives on the unauthenticated response the coordinator returns before
// authorization, so any status code answers the question.
func clockCheck(client *http.Client, origin string, now func() time.Time) doctorCheck {
	ctx, cancel := context.WithTimeout(context.Background(), clockProbeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, origin, nil)
	if err != nil {
		return doctorCheck{OK: true, Detail: "skew unknown: " + err.Error()}
	}
	resp, err := client.Do(req)
	if err != nil {
		var pinErr *siteclient.PinError
		if errors.As(err, &pinErr) {
			return doctorCheck{OK: true, Detail: "skew unknown, pinned TLS failed: " + err.Error()}
		}
		return doctorCheck{OK: true, Detail: "skew unknown, coordinator unreachable: " + err.Error()}
	}
	local := now()
	defer func() { _ = resp.Body.Close() }()

	served, err := http.ParseTime(resp.Header.Get("Date"))
	if err != nil {
		return doctorCheck{OK: true, Detail: "skew unknown: no usable Date header from the coordinator"}
	}
	offset := local.Sub(served).Round(time.Second)
	detail := fmt.Sprintf("offset %+ds against the coordinator", int(offset.Seconds()))
	if offset > maxClockSkew || offset < -maxClockSkew {
		return doctorCheck{OK: false, Detail: fmt.Sprintf(
			"%s, over the %ds limit; sync this PC's clock before pinned TLS fails",
			detail, int(maxClockSkew.Seconds()),
		)}
	}
	return doctorCheck{OK: true, Detail: detail}
}

// emitDoctor prints the report and returns an error when a required check failed
// so the process exits non-zero.
func emitDoctor(rep doctorReport) error {
	if err := emit(rep); err != nil {
		return err
	}
	if !rep.OK {
		os.Exit(1)
	}
	return nil
}

// runControl writes or removes the reclaim flag file the running daemon watches,
// the user's instant takedown control (see ADR 042). reclaim=true stops all local
// serving until release; reclaim=false restores normal idle-based serving. It
// reads the same config the daemon does so the flag lands beside the same state
// file, then prints the flag path as JSON.
func runControl(args []string, reclaim bool) error {
	name := "release"
	if reclaim {
		name = "reclaim"
	}
	fs := newFlagSet(name)
	configPath := fs.String("config", "", "path to the agent TOML config")
	mustParse(fs, args)
	if *configPath == "" {
		return fmt.Errorf("-config is required")
	}
	settings, err := config.Load(*configPath, os.Getenv)
	if err != nil {
		return err
	}
	if reclaim {
		path, err := preempt.RequestReclaim(settings.StatePath)
		if err != nil {
			return err
		}
		return emit(map[string]string{"reclaimed": path})
	}
	path, err := preempt.RequestRelease(settings.StatePath)
	if err != nil {
		return err
	}
	return emit(map[string]string{"released": path})
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
