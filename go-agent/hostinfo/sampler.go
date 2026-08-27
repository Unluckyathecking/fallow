package hostinfo

import "sync"

// fallbackCPUPercent is reported when the host's CPU counters cannot be read at
// all. It is deliberately pessimistic: the machine looks half busy rather than
// idle, so a failed probe never makes this agent look more attractive to the
// scheduler than it is.
const fallbackCPUPercent = 50.0

// cpuTimes is one reading of the host's cumulative CPU counters, in whatever
// unit the platform counts in (ticks on Linux, 100ns units on Windows). Only
// the ratio of the deltas is used, so the unit never leaves this package.
type cpuTimes struct {
	busy  uint64
	total uint64
}

// Sampler reads the live host metrics for one heartbeat. It keeps the previous
// CPU-time reading so each sample reports the busy share of the interval since
// the last heartbeat rather than since boot; the first sample of a process has
// no previous reading and therefore reports the average since boot, which is a
// real measurement rather than the meaningless 0.0 psutil returns on its first
// call.
//
// The zero value is ready to use and safe for concurrent use.
type Sampler struct {
	mu   sync.Mutex
	prev cpuTimes
}

// Sample takes one live reading of CPU busy percentage, available memory and
// per-GPU state. No probe can fail the caller: each degrades to a conservative
// value and logs the reason once.
func (s *Sampler) Sample() Metrics {
	return Metrics{
		CPUPercent:     s.cpuPercent(),
		MemAvailableMB: availableMemMB(),
		GPUs:           gpuStatuses(),
	}
}

func (s *Sampler) cpuPercent() float64 {
	cur, err := readCPUTimes()
	if err != nil {
		warnOnce("cpu_times", "cpu times unavailable (%v); reporting %.0f%% busy", err, fallbackCPUPercent)
		return fallbackCPUPercent
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	prev := s.prev
	s.prev = cur
	return busyPercent(prev, cur)
}

// busyPercent is the busy share of the interval between two readings, clamped
// to 0..100. A counter that did not advance, or went backwards (a suspended
// machine, a counter reset), measured nothing, so it reports the pessimistic
// fallback rather than 0: an unmeasured interval must never be the most
// attractive reading the scheduler can see.
func busyPercent(prev, cur cpuTimes) float64 {
	if cur.total <= prev.total || cur.busy < prev.busy {
		return fallbackCPUPercent
	}
	busy := float64(cur.busy - prev.busy)
	total := float64(cur.total - prev.total)
	pct := busy / total * 100
	if pct > 100 {
		return 100
	}
	return pct
}
