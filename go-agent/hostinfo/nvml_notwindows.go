//go:build !windows

package hostinfo

// gpuStatuses reports no GPUs off Windows. NVML is the only GPU probe either
// agent has, and neither the Linux nor the macOS snapshot has a GPU inventory
// to sample, so the live heartbeat has nothing to report either.
func gpuStatuses() []GPUStatus { return nil }
