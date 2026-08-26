//go:build darwin

package hostinfo

import (
	"errors"

	"golang.org/x/sys/unix"
)

// errNoCPUTimes names the one probe macOS does not expose without cgo. The
// caller degrades to fallbackCPUPercent and logs this reason once.
var errNoCPUTimes = errors.New("macOS exposes CPU tick counters only through Mach calls that need cgo")

// availMemDivisor is the fraction of installed RAM reported as available on
// macOS. See availableMemMB.
const availMemDivisor = 4

// snapshot reads the macOS capabilities. No GPU probe runs here, matching the
// Python agent, whose GPU probe is NVML-only and reports nothing on a Mac
// either.
func snapshot(diskPath string) Snapshot {
	return Snapshot{
		CPUModel:   sysctlString("machdep.cpu.brand_string", "cpu_model", unknownCPUModel),
		OSVersion:  sysctlString("kern.osproductversion", "os_version", unknownOSVer),
		RAMMB:      totalRAMMB(),
		DiskFreeMB: diskFreeMB(diskPath),
	}
}

func totalRAMMB() int {
	bytes, err := unix.SysctlUint64("hw.memsize")
	if err != nil {
		warnOnce("ram_total", "cannot read hw.memsize (%v); reporting %d MB", err, minRAMMB)
		return minRAMMB
	}
	return mb(bytes)
}

// availableMemMB has no cgo-free source on macOS: the free-page counts live
// behind host_statistics64, a Mach call. Rather than fabricate precision we
// report a fixed quarter of installed RAM — a 32 GB Mac always looks like it
// has 8 GB free — which under-reports on an idle machine and never promises
// memory the machine does not have.
func availableMemMB() int { return totalRAMMB() / availMemDivisor }

func readCPUTimes() (cpuTimes, error) { return cpuTimes{}, errNoCPUTimes }

// sysctlString reads a string sysctl, degrading to fallback and logging once
// under key when the kernel does not publish that node.
func sysctlString(name, key, fallback string) string {
	value, err := unix.Sysctl(name)
	if err != nil || value == "" {
		warnOnce(key, "cannot read sysctl %s (%v); reporting %q", name, err, fallback)
		return fallback
	}
	return value
}
