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
