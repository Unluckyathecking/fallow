package hostinfo

import (
	"path/filepath"
	"runtime"
	"testing"
)

// Caps runs the real probes for whichever platform the test is built for. The
// assertions are the ones that must hold everywhere: the numbers describe this
// machine, and none of them is the placeholder the daemon used to enrol with.
func TestCapsDescribesThisHost(t *testing.T) {
	caps := Caps(t.TempDir())
	if caps.RAMMB <= minRAMMB {
		t.Fatalf("ram = %d MB; expected the host's real memory, not the floor", caps.RAMMB)
	}
	if caps.DiskFreeMB <= 0 {
		t.Fatalf("disk free = %d MB", caps.DiskFreeMB)
	}
	// An arm64 /proc/cpuinfo publishes no "model name", so "unknown" is the
	// honest reading there; it is only a failure where a name is always there.
	if caps.CPUModel == "" || (caps.CPUModel == unknownCPUModel && runtime.GOARCH == "amd64") {
		t.Fatalf("cpu model = %q", caps.CPUModel)
	}
	if caps.OSVersion == "" || caps.OSVersion == unknownOSVer {
		t.Fatalf("os version = %q", caps.OSVersion)
	}
	t.Logf("caps: %+v", caps)
}

// Enrollment happens before the first download creates the model cache, so the
// disk probe must answer for a path that does not exist yet.
func TestCapsProbesMissingCacheDir(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "models", "not-yet")
	if free := Caps(missing).DiskFreeMB; free <= 0 {
		t.Fatalf("disk free = %d MB for an uncreated cache dir", free)
	}
}

// pagesMB is the darwin available-memory arithmetic, tested on every platform
// because the sysctl it reads cannot be. A page size the kernel would never
// report must yield nothing rather than a fabricated capacity.
func TestPagesMB(t *testing.T) {
	for _, tc := range []struct {
		name     string
		pages    uint64
		pageSize int
		want     int
	}{
		{"16k pages on apple silicon", 65536, 16384, 1024},
		{"4k pages on intel", 262144, 4096, 1024},
		{"partial megabyte floors", 1, 4096, 0},
		{"no free pages", 0, 16384, 0},
		{"zero page size reports nothing", 65536, 0, 0},
		{"negative page size reports nothing", 65536, -1, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := pagesMB(tc.pages, tc.pageSize); got != tc.want {
				t.Errorf("pagesMB(%d, %d) = %d, want %d", tc.pages, tc.pageSize, got, tc.want)
			}
		})
	}
}

func TestWarnOnceStaysQuiet(t *testing.T) {
	warnOnce("test_key", "first")
	if _, ok := warned.Load("test_key"); !ok {
		t.Fatal("warnOnce did not record the key")
	}
}
