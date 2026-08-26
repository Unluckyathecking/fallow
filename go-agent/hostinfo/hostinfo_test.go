package hostinfo

import (
	"path/filepath"
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
	if caps.CPUModel == "" || caps.CPUModel == unknownCPUModel {
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

func TestWarnOnceStaysQuiet(t *testing.T) {
	warnOnce("test_key", "first")
	if _, ok := warned.Load("test_key"); !ok {
		t.Fatal("warnOnce did not record the key")
	}
}
