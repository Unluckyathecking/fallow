package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "agent.toml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func noEnv(string) string { return "" }

func TestLoadDefaultsAndFileValues(t *testing.T) {
	path := writeConfig(t, `
coordinator_url = "http://coord:8000/"
enrollment_token = "tok-123"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
`)
	s, err := Load(path, noEnv)
	if err != nil {
		t.Fatal(err)
	}
	if s.CoordinatorURL != "http://coord:8000/" {
		t.Errorf("coordinator_url = %q", s.CoordinatorURL)
	}
	if s.EnrollmentToken != "tok-123" {
		t.Errorf("enrollment_token = %q", s.EnrollmentToken)
	}
	if s.PortRange.Start != DefaultPortStart || s.PortRange.Count != DefaultPortCount {
		t.Errorf("port range = %+v, want defaults", s.PortRange)
	}
	if s.WorkPollTimeoutS != DefaultWorkPollTimeoutS || s.ActiveSleepS != DefaultActiveSleepS {
		t.Errorf("timeouts = %v/%v, want defaults", s.WorkPollTimeoutS, s.ActiveSleepS)
	}
}

func TestLoadEnvOverridesFile(t *testing.T) {
	path := writeConfig(t, `
coordinator_url = "http://file-url"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
`)
	env := map[string]string{
		envCoordinatorURL:  "http://env-url",
		envEnrollmentToken: "env-tok",
		envPortStart:       "9000",
	}
	s, err := Load(path, func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if s.CoordinatorURL != "http://env-url" {
		t.Errorf("env did not override coordinator_url: %q", s.CoordinatorURL)
	}
	if s.EnrollmentToken != "env-tok" {
		t.Errorf("env enrollment_token = %q", s.EnrollmentToken)
	}
	if s.PortRange.Start != 9000 {
		t.Errorf("port start = %d, want 9000 from env", s.PortRange.Start)
	}
}

func TestLoadPortRangeFromFile(t *testing.T) {
	path := writeConfig(t, `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"

[port_range]
start = 8200
count = 8
`)
	s, err := Load(path, noEnv)
	if err != nil {
		t.Fatal(err)
	}
	if s.PortRange.Start != 8200 || s.PortRange.Count != 8 {
		t.Errorf("port range = %+v, want {8200 8}", s.PortRange)
	}
}

func TestLoadToleratesPythonOnlyKeys(t *testing.T) {
	// A config rich enough for the Python agent must still load here.
	path := writeConfig(t, `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
results_dir = "/var/fallow/results"

[whisper]
device = "cpu"

[bench]
enabled = true
`)
	if _, err := Load(path, noEnv); err != nil {
		t.Fatalf("rich Python config should load: %v", err)
	}
}

func TestLoadExpandsHome(t *testing.T) {
	path := writeConfig(t, `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
state_path = "~/state.json"
`)
	s, err := Load(path, noEnv)
	if err != nil {
		t.Fatal(err)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home dir")
	}
	want := filepath.Join(home, "state.json")
	if s.StatePath != want {
		t.Errorf("state_path = %q, want %q", s.StatePath, want)
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := map[string]string{
		"wildcard bind host": `
coordinator_url = "http://coord"
bind_host = "0.0.0.0"
llama_server_binary = "/opt/llama/llama-server"
`,
		"non-http url": `
coordinator_url = "ftp://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
`,
		"missing binary": `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
`,
		"zero work poll timeout": `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
work_poll_timeout_s = 0
`,
		"zero active sleep": `
coordinator_url = "http://coord"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
active_sleep_s = 0
`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Load(writeConfig(t, body), noEnv); err == nil {
				t.Fatal("expected a validation error")
			}
		})
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "nope.toml"), noEnv); err == nil {
		t.Fatal("expected an error for a missing config file")
	}
}

// TestSiteModeConfig covers the Site Mode branch: the join bundle opts in, the
// coordinator URL is optional (it comes from the profile), and the bind host is
// confined to loopback so llama is never exposed on the LAN.
func TestSiteModeConfig(t *testing.T) {
	path := writeConfig(t, `
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
site_join_bundle = "/etc/fallow/join.json"
`)
	s, err := Load(path, noEnv)
	if err != nil {
		t.Fatal(err)
	}
	if !s.SiteMode() {
		t.Fatal("SiteMode() = false with site_join_bundle set")
	}
	if s.SiteJoinBundle != "/etc/fallow/join.json" {
		t.Errorf("site_join_bundle = %q", s.SiteJoinBundle)
	}
	if s.CoordinatorURL != "" {
		t.Errorf("coordinator_url = %q, want empty (unused in Site Mode)", s.CoordinatorURL)
	}
}

// TestSiteModeEnvOptsIn verifies the env override enables Site Mode.
func TestSiteModeEnvOptsIn(t *testing.T) {
	path := writeConfig(t, `
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
`)
	env := map[string]string{envSiteJoinBundle: "/tmp/join.json"}
	s, err := Load(path, func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if !s.SiteMode() {
		t.Fatal("env FALLOW_SITE_JOIN_BUNDLE did not enable Site Mode")
	}
}

// TestSiteModeRejectsNonLoopbackBind fails closed on any non-loopback bind, and
// TestLoopbackVariants accepts the loopback forms.
func TestSiteModeRejectsNonLoopbackBind(t *testing.T) {
	for _, bind := range []string{"0.0.0.0", "10.24.8.10", "192.168.1.5", "::"} {
		body := "" +
			"bind_host = \"" + bind + "\"\n" +
			"llama_server_binary = \"/opt/llama/llama-server\"\n" +
			"site_join_bundle = \"/etc/fallow/join.json\"\n"
		if _, err := Load(writeConfig(t, body), noEnv); err == nil {
			t.Errorf("bind_host %q was accepted in Site Mode, want rejected", bind)
		}
	}
}

func TestSiteModeLoopbackVariants(t *testing.T) {
	for _, bind := range []string{"127.0.0.1", "127.0.0.5", "::1", "localhost"} {
		body := "" +
			"bind_host = \"" + bind + "\"\n" +
			"llama_server_binary = \"/opt/llama/llama-server\"\n" +
			"site_join_bundle = \"/etc/fallow/join.json\"\n"
		if _, err := Load(writeConfig(t, body), noEnv); err != nil {
			t.Errorf("loopback bind_host %q rejected in Site Mode: %v", bind, err)
		}
	}
}

// TestSiteModeIgnoresCoordinatorURLScheme confirms a leftover coordinator_url
// does not trip the legacy scheme check when Site Mode is on.
func TestSiteModeIgnoresCoordinatorURLScheme(t *testing.T) {
	path := writeConfig(t, `
coordinator_url = "not-a-url"
bind_host = "127.0.0.1"
llama_server_binary = "/opt/llama/llama-server"
site_join_bundle = "/etc/fallow/join.json"
`)
	if _, err := Load(path, noEnv); err != nil {
		t.Fatalf("Site Mode should ignore coordinator_url scheme, got %v", err)
	}
}
