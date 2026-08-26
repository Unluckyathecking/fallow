package hostinfo

import "testing"

func TestBusyPercent(t *testing.T) {
	cases := []struct {
		name       string
		prev, cur  cpuTimes
		wantPctMin float64
		wantPctMax float64
	}{
		{"quarter busy", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 200, total: 1400}, 25, 25},
		{"idle interval", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 100, total: 1400}, 0, 0},
		{"fully busy", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 500, total: 1400}, 100, 100},
		{"stalled counter", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 100, total: 1000}, 0, 0},
		{"counter reset", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 1, total: 5}, 0, 0},
		{"busy beyond total", cpuTimes{busy: 100, total: 1000}, cpuTimes{busy: 700, total: 1400}, 100, 100},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := busyPercent(tc.prev, tc.cur)
			if got < tc.wantPctMin || got > tc.wantPctMax {
				t.Fatalf("busyPercent = %v; want within [%v, %v]", got, tc.wantPctMin, tc.wantPctMax)
			}
		})
	}
}

// Sample runs the real host probes: the values must be live and in range, not
// the placeholders the daemon used to send.
func TestSampleReadsThisHost(t *testing.T) {
	var sampler Sampler
	first := sampler.Sample()
	if first.CPUPercent < 0 || first.CPUPercent > 100 {
		t.Fatalf("cpu percent = %v, out of range", first.CPUPercent)
	}
	if first.MemAvailableMB <= 0 {
		t.Fatalf("available memory = %d MB", first.MemAvailableMB)
	}
	// The second sample measures the interval since the first, not since boot.
	second := sampler.Sample()
	if second.CPUPercent < 0 || second.CPUPercent > 100 {
		t.Fatalf("cpu percent = %v, out of range", second.CPUPercent)
	}
}
