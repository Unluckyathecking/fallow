//go:build windows

package supervisor

import (
	"fmt"
	"syscall"
)

// processSuspendResume is the access right required to suspend or resume a
// process (PROCESS_SUSPEND_RESUME).
const processSuspendResume = 0x0800

// ntdll exposes the undocumented but stable whole-process suspend/resume
// primitives that psutil uses on Windows. They act on every thread of the
// process atomically, which is what we want: a partially suspended replica
// could still touch the GPU on the preemption hot path.
var (
	ntdll               = syscall.NewLazyDLL("ntdll.dll")
	procNtSuspendProc   = ntdll.NewProc("NtSuspendProcess")
	procNtResumeProcess = ntdll.NewProc("NtResumeProcess")
)

// suspendProcess stops every thread of the target process via
// NtSuspendProcess, mirroring psutil.Process.suspend on Windows.
func suspendProcess(pid int) error {
	return ntProcessControl(pid, procNtSuspendProc)
}

// resumeProcess resumes every thread of the target process via
// NtResumeProcess, mirroring psutil.Process.resume on Windows.
func resumeProcess(pid int) error {
	return ntProcessControl(pid, procNtResumeProcess)
}

// ntProcessControl opens the process with suspend/resume rights, invokes the
// given ntdll routine on its handle, and closes the handle. The NTSTATUS return
// value is zero on success.
func ntProcessControl(pid int, proc *syscall.LazyProc) error {
	handle, err := syscall.OpenProcess(processSuspendResume, false, uint32(pid))
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(handle)

	status, _, _ := proc.Call(uintptr(handle))
	if status != 0 {
		return fmt.Errorf("%s failed: NTSTATUS 0x%x", proc.Name, status)
	}
	return nil
}
