// Package config loads the agent's static, machine-local configuration from the
// same TOML file the Python agent reads, so one config file serves either agent.
//
// It resolves a TOML file with environment-variable overrides (env wins) into a
// frozen Settings value. The one security-critical validation mirrors the Python
// settings and the supervisor (ADR 003): bind_host must never be 0.0.0.0.
// llama-server has no auth, so binding to all interfaces would expose an open
// inference endpoint; bind to loopback or the tailnet interface only.
package config

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"
)

// Default tunables. These match the Python settings defaults so both agents
// behave identically off the same file.
const (
	DefaultStatePath        = "~/.fallow/agent-state.json"
	DefaultCacheDir         = "~/.fallow/models"
	DefaultPortStart        = 8100
	DefaultPortCount        = 16
	DefaultWorkPollTimeoutS = 20.0
	DefaultActiveSleepS     = 1.0
	forbiddenBindHost       = "0.0.0.0" // named to reject, never to bind to
)

// Environment override keys (env beats file), matching the Python agent so a
// single deployment can set them once for both.
const (
	envCoordinatorURL  = "FALLOW_COORDINATOR_URL"
	envEnrollmentToken = "FALLOW_ENROLLMENT_TOKEN"
	envBindHost        = "FALLOW_BIND_HOST"
	envStatePath       = "FALLOW_STATE_PATH"
	envCacheDir        = "FALLOW_CACHE_DIR"
	envLlamaBinary     = "FALLOW_LLAMA_SERVER_BINARY"
	envPortStart       = "FALLOW_PORT_START"
	envPortCount       = "FALLOW_PORT_COUNT"
	envSiteJoinBundle  = "FALLOW_SITE_JOIN_BUNDLE"
)

// PortRange is the contiguous local port range replicas bind within.
type PortRange struct {
	Start int `toml:"start"`
	Count int `toml:"count"`
}

// Settings is the fully resolved, immutable agent configuration. Only the subset
// the Go daemon composes is read; unknown keys the Python agent also accepts
// (whisper, bench, results_dir, …) are ignored rather than rejected, so a single
// richer file still loads here.
type Settings struct {
	CoordinatorURL    string
	EnrollmentToken   string
	BindHost          string
	LlamaServerBinary string
	StatePath         string
	CacheDir          string
	WorkPollTimeoutS  float64
	ActiveSleepS      float64
	PortRange         PortRange

	// SiteJoinBundle is the path to a LAN Site Mode join file. When empty (and no
	// site profile is persisted) the daemon runs in legacy mode with unchanged
	// URL, proxy and bind behaviour. When set, the coordinator URL and its pinned
	// certificates come from the join file (or the persisted profile on restart),
	// coordinator_url is ignored, and the bind host must be loopback.
	SiteJoinBundle string
}

// SiteMode reports whether this configuration opts into LAN Site Mode.
func (s Settings) SiteMode() bool { return s.SiteJoinBundle != "" }

// fileShape is the TOML decode target. Pointers distinguish "absent" (leave the
// default) from "set to zero", which matters for the numeric tunables.
type fileShape struct {
	CoordinatorURL    string     `toml:"coordinator_url"`
	EnrollmentToken   string     `toml:"enrollment_token"`
	BindHost          string     `toml:"bind_host"`
	LlamaServerBinary string     `toml:"llama_server_binary"`
	StatePath         string     `toml:"state_path"`
	CacheDir          string     `toml:"cache_dir"`
	WorkPollTimeoutS  *float64   `toml:"work_poll_timeout_s"`
	ActiveSleepS      *float64   `toml:"active_sleep_s"`
	PortRange         *PortRange `toml:"port_range"`
	SiteJoinBundle    string     `toml:"site_join_bundle"`
}

// Load reads config from path, applies environment overrides, then validates.
// getenv is injected so tests need not mutate the process environment; pass
// os.Getenv in production.
func Load(path string, getenv func(string) string) (Settings, error) {
	var raw fileShape
	if _, err := toml.DecodeFile(path, &raw); err != nil {
		return Settings{}, fmt.Errorf("could not read config file %s: %w", path, err)
	}
	s, err := resolve(raw, getenv)
	if err != nil {
		return Settings{}, err
	}
	if err := s.validate(); err != nil {
		return Settings{}, err
	}
	return s, nil
}

func resolve(raw fileShape, getenv func(string) string) (Settings, error) {
	s := Settings{
		CoordinatorURL:    override(raw.CoordinatorURL, getenv(envCoordinatorURL)),
		EnrollmentToken:   override(raw.EnrollmentToken, getenv(envEnrollmentToken)),
		BindHost:          override(raw.BindHost, getenv(envBindHost)),
		LlamaServerBinary: override(raw.LlamaServerBinary, getenv(envLlamaBinary)),
		StatePath:         orDefault(override(raw.StatePath, getenv(envStatePath)), DefaultStatePath),
		CacheDir:          orDefault(override(raw.CacheDir, getenv(envCacheDir)), DefaultCacheDir),
		WorkPollTimeoutS:  floatOrDefault(raw.WorkPollTimeoutS, DefaultWorkPollTimeoutS),
		ActiveSleepS:      floatOrDefault(raw.ActiveSleepS, DefaultActiveSleepS),
		PortRange:         resolvePortRange(raw.PortRange),
		SiteJoinBundle:    expandHome(override(raw.SiteJoinBundle, getenv(envSiteJoinBundle))),
	}
	if err := applyPortEnv(&s.PortRange, getenv); err != nil {
		return Settings{}, err
	}
	s.StatePath = expandHome(s.StatePath)
	s.CacheDir = expandHome(s.CacheDir)
	return s, nil
}

func resolvePortRange(pr *PortRange) PortRange {
	out := PortRange{Start: DefaultPortStart, Count: DefaultPortCount}
	if pr != nil {
		if pr.Start != 0 {
			out.Start = pr.Start
		}
		if pr.Count != 0 {
			out.Count = pr.Count
		}
	}
	return out
}

func applyPortEnv(pr *PortRange, getenv func(string) string) error {
	if v := getenv(envPortStart); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return fmt.Errorf("%s must be an integer, got %q", envPortStart, v)
		}
		pr.Start = n
	}
	if v := getenv(envPortCount); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return fmt.Errorf("%s must be an integer, got %q", envPortCount, v)
		}
		pr.Count = n
	}
	return nil
}

func (s Settings) validate() error {
	if s.SiteMode() {
		// Site Mode dials the coordinator URL pinned in the join profile, not
		// coordinator_url, and llama replicas are served only over loopback. Fail
		// closed on anything that would expose an unauthenticated LAN endpoint.
		if s.BindHost == "" {
			return fmt.Errorf("bind_host must be set to a loopback address in Site Mode")
		}
		if !isLoopback(s.BindHost) {
			return fmt.Errorf(
				"bind_host must be loopback in Site Mode (got %q): "+
					"Site Mode serves llama replicas over 127.0.0.1 only",
				s.BindHost,
			)
		}
	} else {
		if !strings.HasPrefix(s.CoordinatorURL, "http://") && !strings.HasPrefix(s.CoordinatorURL, "https://") {
			return fmt.Errorf("coordinator_url must start with http:// or https://, got %q", s.CoordinatorURL)
		}
		if s.BindHost == "" {
			return fmt.Errorf("bind_host must be set (loopback or tailnet IP)")
		}
		if s.BindHost == forbiddenBindHost {
			return fmt.Errorf(
				"bind_host must not be 0.0.0.0: llama-server has no auth; " +
					"bind to loopback or the tailnet interface only",
			)
		}
	}
	if s.LlamaServerBinary == "" {
		return fmt.Errorf("llama_server_binary must be set")
	}
	if s.PortRange.Start <= 0 || s.PortRange.Count <= 0 {
		return fmt.Errorf("port_range.start and port_range.count must be positive")
	}
	if s.WorkPollTimeoutS <= 0 {
		return fmt.Errorf("work_poll_timeout_s must be positive")
	}
	if s.ActiveSleepS <= 0 {
		return fmt.Errorf("active_sleep_s must be positive")
	}
	return nil
}

// override returns env if it is non-empty, else the file value.
func override(fileValue, env string) string {
	if env != "" {
		return env
	}
	return fileValue
}

func orDefault(value, def string) string {
	if value == "" {
		return def
	}
	return value
}

func floatOrDefault(value *float64, def float64) float64 {
	if value == nil {
		return def
	}
	return *value
}

// isLoopback reports whether host is a loopback address the Site Mode replica
// bind is confined to. It accepts the IPv4 loopback block (127.0.0.0/8), the
// IPv6 loopback, and the "localhost" name; anything else is rejected so a Site
// Mode agent can never expose llama beyond the local host.
func isLoopback(host string) bool {
	if host == "localhost" || host == "::1" {
		return true
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return false
}

// expandHome resolves a leading ~ to the user's home directory, matching the
// Python agent's Path.expanduser handling of the default paths.
func expandHome(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(path, "~"))
		}
	}
	return path
}
