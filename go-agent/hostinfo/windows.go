//go:build windows

package hostinfo

import (
	"fmt"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// x/sys/windows exports neither GlobalMemoryStatusEx nor GetSystemTimes, so
// both are bound lazily from kernel32 the way the idle package binds
// GetLastInputInfo. GetDiskFreeSpaceEx and RtlGetVersion it does export.
var (
	kernel32               = windows.NewLazySystemDLL("kernel32.dll")
	procGlobalMemoryStatus = kernel32.NewProc("GlobalMemoryStatusEx")
	procGetSystemTimes     = kernel32.NewProc("GetSystemTimes")
)

// cpuRegistryKey holds the CPU description the firmware reported at boot.
const cpuRegistryKey = `HARDWARE\DESCRIPTION\System\CentralProcessor\0`

// memoryStatusEx mirrors Win32 MEMORYSTATUSEX. Only the two physical-memory
// fields are read; the rest are present so the struct is the size the API
// checks against dwLength.
type memoryStatusEx struct {
	length               uint32
	memoryLoad           uint32
	totalPhys            uint64
	availPhys            uint64
	totalPageFile        uint64
	availPageFile        uint64
	totalVirtual         uint64
	availVirtual         uint64
	availExtendedVirtual uint64
}

// snapshot reads the Windows capabilities, the full set: this is the platform
// the agent runs on in production.
func snapshot(diskPath string) Snapshot {
	return Snapshot{
		CPUModel:   cpuModel(),
		OSVersion:  osVersion(),
		RAMMB:      totalRAMMB(),
		DiskFreeMB: diskFreeMB(diskPath),
		GPUs:       nvmlGPUs(),
	}
}

func totalRAMMB() int {
	status, err := memoryStatus()
	if err != nil {
		warnOnce("ram_total", "GlobalMemoryStatusEx failed (%v); reporting %d MB", err, minRAMMB)
		return minRAMMB
	}
	return mb(status.totalPhys)
}

func availableMemMB() int {
	status, err := memoryStatus()
	if err != nil {
		warnOnce("ram_avail", "GlobalMemoryStatusEx failed (%v); reporting %d MB", err, minRAMMB)
		return minRAMMB
	}
	return mb(status.availPhys)
}

func memoryStatus() (memoryStatusEx, error) {
	status := memoryStatusEx{}
	status.length = uint32(unsafe.Sizeof(status))
	ret, _, err := procGlobalMemoryStatus.Call(uintptr(unsafe.Pointer(&status)))
	if ret == 0 {
		return memoryStatusEx{}, err
	}
	return status, nil
}

// diskFreeMB reports the space available to the calling user on the volume
// holding path — quota-aware, so it is what a model download may actually use.
func diskFreeMB(path string) int {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		warnOnce("disk_free", "bad disk path %s (%v); reporting 0 MB free", path, err)
		return 0
	}
	var freeToCaller, total, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(name, &freeToCaller, &total, &totalFree); err != nil {
		warnOnce("disk_free", "GetDiskFreeSpaceEx %s failed (%v); reporting 0 MB free", path, err)
		return 0
	}
	return mb(freeToCaller)
}

// cpuModel reads the processor name the firmware published in the registry.
func cpuModel() string {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, cpuRegistryKey, registry.QUERY_VALUE)
	if err != nil {
		warnOnce("cpu_model", "cannot open HKLM\\%s (%v)", cpuRegistryKey, err)
		return unknownCPUModel
	}
	defer func() { _ = key.Close() }()
	name, _, err := key.GetStringValue("ProcessorNameString")
	if err != nil || strings.TrimSpace(name) == "" {
		warnOnce("cpu_model", "cannot read ProcessorNameString (%v)", err)
		return unknownCPUModel
	}
	return strings.TrimSpace(name)
}

// osVersion renders RtlGetVersion as the plain major.minor.build triple (for
// example "10.0.19045"). RtlGetVersion is used rather than GetVersionEx
// because it is not subject to application compatibility shimming.
func osVersion() string {
	v := windows.RtlGetVersion()
	return fmt.Sprintf("%d.%d.%d", v.MajorVersion, v.MinorVersion, v.BuildNumber)
}

// readCPUTimes reads the system-wide CPU counters. The kernel time returned by
// GetSystemTimes includes idle, so kernel+user is the whole interval and busy
// is that minus idle.
func readCPUTimes() (cpuTimes, error) {
	var idle, kernel, user windows.Filetime
	ret, _, err := procGetSystemTimes.Call(
		uintptr(unsafe.Pointer(&idle)),
		uintptr(unsafe.Pointer(&kernel)),
		uintptr(unsafe.Pointer(&user)),
	)
	if ret == 0 {
		return cpuTimes{}, err
	}
	total := filetimeTicks(kernel) + filetimeTicks(user)
	return cpuTimes{busy: total - filetimeTicks(idle), total: total}, nil
}

// filetimeTicks flattens a FILETIME into its 64-bit count of 100ns units.
func filetimeTicks(ft windows.Filetime) uint64 {
	return uint64(ft.HighDateTime)<<32 | uint64(ft.LowDateTime)
}
