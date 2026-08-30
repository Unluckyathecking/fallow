package supervisor

import (
	"errors"
	"path/filepath"
	"slices"
	"testing"
)

// Command construction and config validation. Ported 1:1 from
// test_a3_supervisor.py.

func TestLlamaCommandCPUHasNoGPUFlags(t *testing.T) {
	cfg := DefaultConfig("/opt/llama-server")
	cfg.BindHost = "100.64.0.1"
	cmd := LlamaServerCommand(cfg)(manifest("tiny", 0), "/models/tiny.gguf", 8080)

	if cmd[0] != cfg.LlamaBinary {
		t.Fatalf("cmd[0] = %q, want %q", cmd[0], cfg.LlamaBinary)
	}
	if got := cmd[slices.Index(cmd, "--host")+1]; got != "100.64.0.1" {
		t.Fatalf("--host = %q, want 100.64.0.1", got)
	}
	if got := cmd[slices.Index(cmd, "--parallel")+1]; got != "2" {
		t.Fatalf("--parallel = %q, want 2", got)
	}
	if got := cmd[slices.Index(cmd, "-c")+1]; got != "8192" {
		t.Fatalf("-c = %q, want 8192", got)
	}
	if last := cmd[len(cmd)-2:]; last[0] != "--extra" || last[1] != "1" {
		t.Fatalf("tail = %v, want [--extra 1]", last)
	}
	if slices.Contains(cmd, "-ngl") || slices.Contains(cmd, "--flash-attn") {
		t.Fatalf("cpu command must not contain gpu flags: %v", cmd)
	}
}

func TestLlamaCommandGPUAppendsOffloadFlags(t *testing.T) {
	cmd := LlamaServerCommand(DefaultConfig("llama-server"))(manifest("tiny", 4096), "/models/tiny.gguf", 9000)

	tail := cmd[len(cmd)-3:]
	if tail[0] != "-ngl" || tail[1] != "999" || tail[2] != "--flash-attn" {
		t.Fatalf("tail = %v, want [-ngl 999 --flash-attn]", tail)
	}
	if slices.Index(cmd, "--extra") >= slices.Index(cmd, "-ngl") {
		t.Fatalf("default_args must precede gpu flags: %v", cmd)
	}
}

func TestLlamaCommandAppendsMmprojBesideModelBlob(t *testing.T) {
	m := manifest("tiny", 0)
	name := "mmproj.gguf"
	sha := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	size := 1
	m.MmprojFileName = &name
	m.MmprojSHA256 = &sha
	m.MmprojSizeBytes = &size

	cmd := LlamaServerCommand(DefaultConfig("llama-server"))(m, filepath.Join("/models", "tiny", "tiny.gguf"), 9000)

	idx := slices.Index(cmd, "--mmproj")
	if idx < 0 {
		t.Fatalf("command must contain --mmproj: %v", cmd)
	}
	want := filepath.Join("/models", "tiny", "mmproj.gguf")
	if cmd[idx+1] != want {
		t.Fatalf("--mmproj = %q, want %q", cmd[idx+1], want)
	}
}

func TestLlamaCommandOmitsMmprojWhenManifestHasNone(t *testing.T) {
	cmd := LlamaServerCommand(DefaultConfig("llama-server"))(manifest("tiny", 0), "/models/tiny.gguf", 9000)
	if slices.Contains(cmd, "--mmproj") {
		t.Fatalf("command must not contain --mmproj: %v", cmd)
	}
}

func TestConfigRejectsWildcardBindHost(t *testing.T) {
	cfg := DefaultConfig("x")
	cfg.BindHost = ForbiddenBindHost

	if err := cfg.Validate(); !errors.Is(err, ErrForbiddenBindHost) {
		t.Fatalf("Validate() = %v, want ErrForbiddenBindHost", err)
	}
	if _, err := New(cfg, LlamaServerCommand(cfg)); !errors.Is(err, ErrForbiddenBindHost) {
		t.Fatalf("New() = %v, want ErrForbiddenBindHost", err)
	}
}
