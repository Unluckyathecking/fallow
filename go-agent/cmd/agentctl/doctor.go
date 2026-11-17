package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/idle"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// doctorReport is the single JSON object `doctor` prints. Each check is a small
// object so an operator or a script can read pass/fail plus a reason per lane.
type doctorReport struct {
	Mode      string      `json:"mode"`
	Config    doctorCheck `json:"config"`
	Identity  doctorCheck `json:"identity"`
	Llama     doctorCheck `json:"llama"`
	Idle      doctorCheck `json:"idle"`
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
	rep.Idle = doctorIdle(settings)
	rep.PinnedTLS = doctorPinnedTLS(settings)
	rep.Clock = doctorClock(settings, time.Now)

	rep.OK = rep.Config.OK && rep.Identity.OK && rep.Llama.OK && rep.Idle.OK &&
		rep.PinnedTLS.OK && rep.Clock.OK
	return emitDoctor(rep)
}

// doctorIdentity reports whether a durable identity is on disk. An unenrolled
// machine is reported, not failed: doctor runs before first enrollment too.
//
// A revoked identity is a failure and is named as one. Doctor stays offline, so
// the evidence is the marker the daemon wrote when the coordinator rejected its
// device token — not a probe of our own.
func doctorIdentity(settings config.Settings) doctorCheck {
	if reason, revoked := state.Revoked(settings.StatePath); revoked {
		return doctorCheck{OK: false, Detail: "device token rejected by the coordinator: " +
			reason + "; this machine must re-enrol from a fresh join file"}
	}
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

// doctorIdle reports whether this build can see the user on this machine — the
// same sample `run` refuses to start without, taken here before the desk is
// asked to serve. assume_idle is honoured, not silently trusted: it passes with
// a detail saying the machine will be reported permanently idle.
func doctorIdle(settings config.Settings) doctorCheck {
	if settings.AssumeIdle {
		return doctorCheck{OK: true, Detail: "assume_idle is set: this machine is reported " +
			"permanently idle and never yields — only ever right where nobody uses it"}
	}
	if err := sampleIdleDetector(); err != nil {
		return doctorCheck{OK: false, Detail: err.Error()}
	}
	return doctorCheck{OK: true, Detail: "supported and sampling"}
}

// sampleIdleDetector builds the platform detector and takes one reading,
// returning why it cannot answer. It is read-only, like every other lane.
func sampleIdleDetector() error {
	det, err := idle.CreateDetector(false, false)
	if err != nil {
		return err
	}
	return checkIdleSample(det)
}

// checkIdleSample applies the gate `run` applies: an error, and equally a
// reading that is not a finite number of seconds. The daemon's
// probeIdleDetection refuses both, so doctor must fail on both or it passes a
// desk that `run` then refuses to start on. The non-finite case says what it
// read rather than borrowing the unsupported-platform wording: the build is
// fine, this reading was not.
func checkIdleSample(det idle.Detector) error {
	s, err := det.SecondsSinceInput()
	if err != nil {
		return err
	}
	if math.IsNaN(s) || math.IsInf(s, 0) {
		return fmt.Errorf("the idle detector answered %v, which is not a number of seconds", s)
	}
	return nil
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

// validityWindowFailure is the message siteclient's pinned VerifyConnection
// gives a certificate rejected against the local clock. It matters here because
// a clock that is days or months out - the dead CMOS battery this check exists
// for - fails the handshake on that check, so no Date header is ever served and
// the offset cannot be measured. Reporting only "pinned TLS failed" would send
// the operator after the certificate when the clock is the likelier cause.
//
// siteclient owns the wording, so this is matched as a string. The narrow
// validity-window case in doctor_test.go drives the real pinned client, so a
// reworded or restructured siteclient error fails that test rather than
// silently degrading this lane to the generic pin message.
const validityWindowFailure = "certificate outside validity window"

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
			if strings.Contains(pinErr.Err.Error(), validityWindowFailure) {
				return doctorCheck{OK: true, Detail: "skew unknown, " + validityWindowFailure +
					": a clock that is days or months out puts every certificate outside its window, " +
					"so check this PC's date, time zone and NTP sync before suspecting the certificate"}
			}
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
