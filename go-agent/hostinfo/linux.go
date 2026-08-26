//go:build linux

package hostinfo

import (
	"os"

	"golang.org/x/sys/unix"
)

const (
	procMeminfo = "/proc/meminfo"
	procCPUinfo = "/proc/cpuinfo"
	procStat    = "/proc/stat"
	osRelease   = "/etc/os-release"
)

// snapshot reads the Linux capabilities. No GPU probe runs here: the Linux
// agent is a development target, not a supported user platform, and the Python
// agent's GPU probe is NVML-only too.
func snapshot(diskPath string) Snapshot {
	return Snapshot{
		CPUModel:   cpuModel(),
		OSVersion:  osVersion(),
		RAMMB:      totalRAMMB(),
		DiskFreeMB: diskFreeMB(diskPath),
	}
}

func totalRAMMB() int {
	kb, err := readMemKB("MemTotal")
	if err != nil {
		warnOnce("ram_total", "cannot read MemTotal (%v); reporting %d MB", err, minRAMMB)
		return minRAMMB
	}
	return int(kb / 1024)
}

func availableMemMB() int {
	kb, err := readMemKB("MemAvailable")
	if err != nil {
		warnOnce("ram_avail", "cannot read MemAvailable (%v); reporting %d MB", err, minRAMMB)
		return minRAMMB
	}
	return int(kb / 1024)
}

func readMemKB(key string) (uint64, error) {
	data, err := os.ReadFile(procMeminfo)
	if err != nil {
		return 0, err
	}
	kb, ok := parseMemKB(string(data), key)
	if !ok {
		return 0, os.ErrNotExist
	}
	return kb, nil
}

func cpuModel() string {
	data, err := os.ReadFile(procCPUinfo)
	if err != nil {
		warnOnce("cpu_model", "cannot read %s (%v)", procCPUinfo, err)
		return unknownCPUModel
	}
	if model := parseCPUModel(string(data)); model != "" {
		return model
	}
	return unknownCPUModel
}

// osVersion prefers the distribution's PRETTY_NAME and falls back to the kernel
// release from uname, which is always available.
func osVersion() string {
	if data, err := os.ReadFile(osRelease); err == nil {
		if name := parsePrettyName(string(data)); name != "" {
			return name
		}
	}
	var uts unix.Utsname
	if err := unix.Uname(&uts); err != nil {
		warnOnce("os_version", "cannot read os-release or uname (%v)", err)
		return unknownOSVer
	}
	return unix.ByteSliceToString(uts.Release[:])
}

func readCPUTimes() (cpuTimes, error) {
	data, err := os.ReadFile(procStat)
	if err != nil {
		return cpuTimes{}, err
	}
	return parseProcStat(string(data))
}
