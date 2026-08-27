//go:build darwin

package hostinfo

import (
	"errors"

	"golang.org/x/sys/unix"
)

// errNoCPUTimes names the one probe macOS does not expose without cgo. The
// caller degrades to fallbackCPUPercent and logs this reason once.
var errNoCPUTimes = errors.New("macOS exposes CPU tick counters only through Mach calls that need cgo")

// vmFreePages is the sysctl carrying the kernel's free-page count. XNU exports
// it from bsd/vm/vm_unix.c as SYSCTL_UINT(_vm, OID_AUTO, page_free_count, ...),
// so it is a plain 32-bit read with no Mach call and no cgo.
const vmFreePages = "vm.page_free_count"

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

// availableMemMB reports the kernel's free pages. macOS publishes no
// MemAvailable equivalent — the reclaimable totals live behind
// host_statistics64, a Mach call — but the free-page count is a plain sysctl,
// and free pages are strictly fewer than what the machine could hand out under
// pressure: purgeable pages and the file cache are both excluded. That makes it
// an under-report by construction, which is the direction a capacity figure has
// to err in.
//
// It replaces a fixed quarter of installed RAM, which was not a reading at all:
// on a pressured 32 GB Mac with a few hundred MB actually free it claimed 8 GB,
// promising memory the machine did not have.
//
// A failed read reports 0, matching Linux: available memory has no positive
// minimum on the wire, so nothing is lost by admitting the probe did not answer.
func availableMemMB() int {
	pages, err := unix.SysctlUint32(vmFreePages)
	if err != nil {
		warnOnce("ram_avail", "cannot read sysctl %s (%v); reporting 0 MB", vmFreePages, err)
		return 0
	}
	return pagesMB(uint64(pages), unix.Getpagesize())
}

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
