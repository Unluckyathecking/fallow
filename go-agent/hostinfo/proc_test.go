package hostinfo

import "testing"

const meminfoFixture = `MemTotal:       16461084 kB
MemFree:        15633944 kB
MemAvailable:   15839612 kB
Buffers:           18764 kB
`

func TestParseMemKB(t *testing.T) {
	total, ok := parseMemKB(meminfoFixture, "MemTotal")
	if !ok || total != 16461084 {
		t.Fatalf("MemTotal = %d, %v; want 16461084, true", total, ok)
	}
	avail, ok := parseMemKB(meminfoFixture, "MemAvailable")
	if !ok || avail != 15839612 {
		t.Fatalf("MemAvailable = %d, %v; want 15839612, true", avail, ok)
	}
	if _, ok := parseMemKB(meminfoFixture, "MemMissing"); ok {
		t.Fatal("absent key reported as found")
	}
	if _, ok := parseMemKB("MemTotal:       not-a-number kB", "MemTotal"); ok {
		t.Fatal("unparsable value reported as found")
	}
}

func TestParseCPUModel(t *testing.T) {
	cpuinfo := "processor\t: 0\nmodel name\t: Intel(R) Xeon(R) Processor @ 2.80GHz\nstepping\t: 6\n"
	if got := parseCPUModel(cpuinfo); got != "Intel(R) Xeon(R) Processor @ 2.80GHz" {
		t.Fatalf("model = %q", got)
	}
	// An ARM kernel prints no model line at all; the caller degrades.
	if got := parseCPUModel("processor\t: 0\nBogoMIPS\t: 50.00\n"); got != "" {
		t.Fatalf("model = %q, want empty", got)
	}
}

func TestParsePrettyName(t *testing.T) {
	release := "PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"\nNAME=\"Ubuntu\"\nVERSION_ID=\"24.04\"\n"
	if got := parsePrettyName(release); got != "Ubuntu 24.04.4 LTS" {
		t.Fatalf("pretty name = %q", got)
	}
	if got := parsePrettyName("NAME=\"Ubuntu\"\n"); got != "" {
		t.Fatalf("pretty name = %q, want empty", got)
	}
}

func TestParseProcStat(t *testing.T) {
	stat := "cpu  100 10 40 800 50 0 0 0 0 0\ncpu0 50 5 20 400 25 0 0 0 0 0\n"
	times, err := parseProcStat(stat)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	// total is every field; busy is everything but idle (800) and iowait (50).
	if times.total != 1000 || times.busy != 150 {
		t.Fatalf("times = %+v; want busy 150, total 1000", times)
	}
	// guest (7) and guest_nice (3) are already inside user and nice, so neither
	// sum may grow by them.
	guest, err := parseProcStat("cpu  100 10 40 800 50 0 0 0 7 3\n")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if guest.total != 1000 || guest.busy != 150 {
		t.Fatalf("times = %+v with guest time; want busy 150, total 1000", guest)
	}
	if _, err := parseProcStat("intr 1 2 3\n"); err == nil {
		t.Fatal("missing cpu line accepted")
	}
	if _, err := parseProcStat("cpu  1 2 3 four five\n"); err == nil {
		t.Fatal("unparsable field accepted")
	}
}
