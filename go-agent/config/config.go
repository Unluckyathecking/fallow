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
}

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
	if s.LlamaServerBinary == "" {
		return fmt.Errorf("llama_server_binary must be set")
	}
	if s.PortRange.Start <= 0 || s.PortRange.Count <= 0 {
		return fmt.Errorf("port_range.start and port_range.count must be positive")
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
