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
)

// nvmlMemory mirrors nvmlMemory_t: total, free and used VRAM in bytes.
type nvmlMemory struct {
	total uint64
	free  uint64
	used  uint64
}

// nvmlGPUs returns the installed NVIDIA GPUs, or nil on any failure.
func nvmlGPUs() []GPU {
	if err := nvmlDLL.Load(); err != nil {
		warnOnce("nvml_load", "nvml.dll not loaded (%v); reporting no GPUs", err)
		return nil
	}
	if err := nvmlCall(procNvmlInit); err != nil {
		warnOnce("nvml_init", "NVML init failed (%v); reporting no GPUs", err)
		return nil
	}
	defer func() { _ = nvmlCall(procNvmlShutdown) }()
	gpus, err := readGPUs()
	if err != nil {
		warnOnce("nvml_read", "NVML read failed (%v); reporting no GPUs", err)
		return nil
	}
	return gpus
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
