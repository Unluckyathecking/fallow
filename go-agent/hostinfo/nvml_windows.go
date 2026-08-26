//go:build windows

package hostinfo

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// NVML ships with the NVIDIA display driver, so nvml.dll is simply absent on a
// machine without one. It is therefore loaded lazily and every step is
// checked: a missing DLL, a missing symbol or any non-zero NVML status reports
// zero GPUs and logs the reason once. A GPU probe never fails the agent.
var (
	nvmlDLL              = windows.NewLazySystemDLL("nvml.dll")
	procNvmlInit         = nvmlDLL.NewProc("nvmlInit_v2")
	procNvmlShutdown     = nvmlDLL.NewProc("nvmlShutdown")
	procNvmlDeviceCount  = nvmlDLL.NewProc("nvmlDeviceGetCount_v2")
	procNvmlDeviceHandle = nvmlDLL.NewProc("nvmlDeviceGetHandleByIndex_v2")
	procNvmlDeviceName   = nvmlDLL.NewProc("nvmlDeviceGetName")
	procNvmlDeviceMemory = nvmlDLL.NewProc("nvmlDeviceGetMemoryInfo")
	procNvmlDeviceUtil   = nvmlDLL.NewProc("nvmlDeviceGetUtilizationRates")
)

// nvmlMemory mirrors nvmlMemory_t: total, free and used VRAM in bytes.
type nvmlMemory struct {
	total uint64
	free  uint64
	used  uint64
}

// nvmlUtilization mirrors nvmlUtilization_t: the busy share of the GPU and of
// its memory bus, each a whole percent.
type nvmlUtilization struct {
	gpu    uint32
	memory uint32
}

// nvmlSession loads and initialises NVML for one read, returning the shutdown
// to defer. ok is false when this machine has no usable NVML, which every
// caller reports as no GPUs.
func nvmlSession() (shutdown func(), ok bool) {
	if err := nvmlDLL.Load(); err != nil {
		warnOnce("nvml_load", "nvml.dll not loaded (%v); reporting no GPUs", err)
		return nil, false
	}
	if err := nvmlCall(procNvmlInit); err != nil {
		warnOnce("nvml_init", "NVML init failed (%v); reporting no GPUs", err)
		return nil, false
	}
	return func() { _ = nvmlCall(procNvmlShutdown) }, true
}

// nvmlGPUs returns the installed NVIDIA GPUs, or nil on any failure.
func nvmlGPUs() []GPU {
	shutdown, ok := nvmlSession()
	if !ok {
		return nil
	}
	defer shutdown()
	gpus, err := readGPUs()
	if err != nil {
		warnOnce("nvml_read", "NVML read failed (%v); reporting no GPUs", err)
		return nil
	}
	return gpus
}

// gpuStatuses samples the live per-GPU state for one heartbeat. It re-enters
// NVML per sample because free VRAM is exactly what changes between beats, and
// it is what the coordinator's fit check reads: a GPU desk whose heartbeat
// carried no gpus was judged to have no VRAM at all, so a model it had picked
// for itself at enrollment no longer fitted it.
func gpuStatuses() []GPUStatus {
	shutdown, ok := nvmlSession()
	if !ok {
		return nil
	}
	defer shutdown()
	statuses, err := readGPUStatuses()
	if err != nil {
		warnOnce("nvml_status", "NVML status read failed (%v); reporting no GPUs", err)
		return nil
	}
	return statuses
}

func readGPUs() ([]GPU, error) {
	var count uint32
	if err := nvmlCall(procNvmlDeviceCount, uintptr(unsafe.Pointer(&count))); err != nil {
		return nil, err
	}
	gpus := make([]GPU, 0, count)
	for index := uint32(0); index < count; index++ {
		gpu, err := readGPU(index)
		if err != nil {
			return nil, err
		}
		gpus = append(gpus, gpu)
	}
	return gpus, nil
}

func readGPU(index uint32) (GPU, error) {
	var device uintptr
	if err := nvmlCall(procNvmlDeviceHandle, uintptr(index), uintptr(unsafe.Pointer(&device))); err != nil {
		return GPU{}, err
	}
	name := make([]byte, nameBufferSize)
	if err := nvmlCall(procNvmlDeviceName, device, uintptr(unsafe.Pointer(&name[0])), uintptr(len(name))); err != nil {
		return GPU{}, err
	}
	var memory nvmlMemory
	if err := nvmlCall(procNvmlDeviceMemory, device, uintptr(unsafe.Pointer(&memory))); err != nil {
		return GPU{}, err
	}
	return GPU{
		Index:  int(index),
		Name:   nvmlName(name),
		Vendor: nvidiaVendor,
		VRAMMB: mb(memory.total),
	}, nil
}

func readGPUStatuses() ([]GPUStatus, error) {
	var count uint32
	if err := nvmlCall(procNvmlDeviceCount, uintptr(unsafe.Pointer(&count))); err != nil {
		return nil, err
	}
	statuses := make([]GPUStatus, 0, count)
	for index := uint32(0); index < count; index++ {
		status, err := readGPUStatus(index)
		if err != nil {
			return nil, err
		}
		statuses = append(statuses, status)
	}
	return statuses, nil
}

func readGPUStatus(index uint32) (GPUStatus, error) {
	var device uintptr
	if err := nvmlCall(procNvmlDeviceHandle, uintptr(index), uintptr(unsafe.Pointer(&device))); err != nil {
		return GPUStatus{}, err
	}
	var memory nvmlMemory
	if err := nvmlCall(procNvmlDeviceMemory, device, uintptr(unsafe.Pointer(&memory))); err != nil {
		return GPUStatus{}, err
	}
	return GPUStatus{Index: int(index), VRAMFreeMB: mb(memory.free), UtilPercent: utilPercent(device)}, nil
}

// utilPercent reads the GPU's busy share, reporting 0 when the driver cannot
// answer. Utilisation is telemetry — nothing schedules on it — so an older
// driver without the symbol must not cost the sample its free-VRAM figure,
// which does decide placement.
func utilPercent(device uintptr) float64 {
	var rates nvmlUtilization
	if err := nvmlCall(procNvmlDeviceUtil, device, uintptr(unsafe.Pointer(&rates))); err != nil {
		warnOnce("nvml_util", "NVML utilisation unavailable (%v); reporting 0%%", err)
		return 0
	}
	return float64(rates.gpu)
}

// nvmlCall invokes one NVML entry point, reporting an absent symbol (an older
// driver) and a non-zero NVML status as errors.
func nvmlCall(proc *windows.LazyProc, args ...uintptr) error {
	if err := proc.Find(); err != nil {
		return err
	}
	if status, _, _ := proc.Call(args...); status != nvmlSuccess {
		return fmt.Errorf("%s returned NVML status %d", proc.Name, status)
	}
	return nil
}
