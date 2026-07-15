//go:build windows

package idle

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// The Win32 idle seam. x/sys/windows does not export GetLastInputInfo /
// GetTickCount directly, so we bind them lazily through its LazyDLL loader
// (user32.dll / kernel32.dll) and declare the LASTINPUTINFO struct ourselves.
var (
	user32           = windows.NewLazySystemDLL("user32.dll")
	kernel32         = windows.NewLazySystemDLL("kernel32.dll")
	procGetLastInput = user32.NewProc("GetLastInputInfo")
	procGetTickCount = kernel32.NewProc("GetTickCount")
)

// lastInputInfo mirrors Win32 LASTINPUTINFO: cbSize (UINT) + dwTime (DWORD),
// both unsigned 32-bit.
type lastInputInfo struct {
	cbSize uint32
	dwTime uint32
}

// defaultTicksReader is the Windows OS seam: it reads GetTickCount() and
// LASTINPUTINFO.dwTime together. The agent must run inside the user's own
// interactive desktop session — a Session 0 service reads nothing useful from
// GetLastInputInfo and would report the machine as permanently idle.
func defaultTicksReader() (InputTicks, error) {
	info := lastInputInfo{}
	info.cbSize = uint32(unsafe.Sizeof(info))
	ret, _, err := procGetLastInput.Call(uintptr(unsafe.Pointer(&info)))
	if ret == 0 {
		return InputTicks{}, err
	}
	now, _, _ := procGetTickCount.Call()
	return InputTicks{NowMS: uint32(now), LastInputMS: info.dwTime}, nil
}

// newPlatformDetector returns the Windows detector.
func newPlatformDetector() (Detector, error) {
	return NewWindowsDetector(nil), nil
}
