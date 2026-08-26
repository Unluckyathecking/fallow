// Package hostinfo reads what this machine actually is: total and available
// RAM, free disk, CPU model, OS version and NVIDIA GPUs. It is the Go answer to
// the Python agent's PsutilSystemProbe/NvmlGpuProbe pair, and it feeds the same
// two wire shapes — DeviceCaps at enrollment and the live metrics in every
// heartbeat.
//
// Two rules hold everywhere. No cgo: the release builds with CGO_ENABLED=0, so
// every probe is a syscall, a /proc read or a lazily loaded DLL. And no probe
// may ever fail the agent: a failed read degrades to a conservative value —
// under-reporting capacity, never over-reporting it — and logs its reason once.
//
// The per-platform files are split by build tag. Windows is the production
// platform and carries the full set including NVML; Linux is real but
// GPU-less (the Linux agent is unsupported); darwin reads what sysctl exposes
// without cgo and names its fallbacks.
package hostinfo

import (
	"log"
	"os"
	"path/filepath"
	"sync"
)

// bytesPerMB matches fallow_agent.heartbeat.constants.BYTES_PER_MB.
const bytesPerMB = 1024 * 1024

// Values reported when a probe cannot answer. unknownCPUModel mirrors the
// Python UNKNOWN_CPU_MODEL; minRAMMB is a floor, not a guess: the coordinator
// requires ram_mb > 0, so a machine whose memory probe failed still enrolls,
// and at 1 GiB it is only ever excluded from models it might have fitted.
const (
	unknownCPUModel = "unknown"
	unknownOSVer    = "unknown"
	minRAMMB        = 1024
	// nvidiaVendor matches fallow_agent.heartbeat.constants.NVIDIA_VENDOR.
	nvidiaVendor = "nvidia"
)

// Snapshot is the static description of this machine, taken once at
// enrollment. Fields whose probe failed carry the conservative defaults above.
type Snapshot struct {
	CPUModel   string
	OSVersion  string
	RAMMB      int
	DiskFreeMB int
	GPUs       []GPU
}

// GPU is one installed accelerator, in the shape DeviceCaps.gpus expects.
type GPU struct {
	Index  int
	Name   string
	Vendor string
	VRAMMB int
}

// Metrics is the live sample that rides in every heartbeat.
type Metrics struct {
	CPUPercent     float64
	MemAvailableMB int
}

// Caps reads this machine's static capabilities, once, at enrollment.
// diskPath is the directory whose free space is reported — the agent's model
// cache. Every field degrades on its own; Caps itself never fails.
func Caps(diskPath string) Snapshot { return snapshot(existingDir(diskPath)) }

// existingDir walks up to the nearest directory that exists, so the disk probe
// still answers on a fresh install: enrollment happens before the first model
// download creates the cache directory, and both statfs and
// GetDiskFreeSpaceExW fail on a path that is not there.
func existingDir(path string) string {
	for {
		if _, err := os.Stat(path); err == nil {
			return path
		}
		parent := filepath.Dir(path)
		if parent == path {
			return path
		}
		path = parent
	}
}

// mb converts a byte count to whole megabytes, matching the Python probes'
// floor division.
func mb(bytes uint64) int { return int(bytes / bytesPerMB) }

var warned sync.Map

// warnOnce logs a probe failure the first time it is seen for key and stays
// silent afterwards, so a permanently absent probe cannot flood the log of a
// daemon that heartbeats every five seconds.
func warnOnce(key, format string, args ...any) {
	if _, dup := warned.LoadOrStore(key, struct{}{}); dup {
		return
	}
	log.Printf("fallow-agent: hostinfo: "+format, args...)
}
