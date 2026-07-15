//go:build windows

package idle

import (
	"fmt"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	user32               = windows.NewLazySystemDLL("user32.dll")
	kernel32             = windows.NewLazySystemDLL("kernel32.dll")
	getLastInputInfoProc = user32.NewProc("GetLastInputInfo")
	getTickCountProc     = kernel32.NewProc("GetTickCount")
)

type lastInputInfo struct {
	Size uint32
	Time uint32
}

type inputTicks struct {
	now       uint32
	lastInput uint32
}

type windowsDetector struct {
	read func() (inputTicks, error)
}

func NewPlatformDetector() Detector {
	return windowsDetector{read: readInputTicks}
}

func (detector windowsDetector) SecondsSinceInput() (time.Duration, error) {
	ticks, err := detector.read()
	if err != nil {
		return 0, err
	}
	return time.Duration(elapsedMilliseconds(ticks.now, ticks.lastInput)) * time.Millisecond, nil
}

func readInputTicks() (inputTicks, error) {
	info := lastInputInfo{Size: uint32(unsafe.Sizeof(lastInputInfo{}))}
	ok, _, callErr := getLastInputInfoProc.Call(uintptr(unsafe.Pointer(&info)))
	if ok == 0 {
		return inputTicks{}, fmt.Errorf("GetLastInputInfo failed: %w", callErr)
	}
	now, _, _ := getTickCountProc.Call()
	return inputTicks{now: uint32(now), lastInput: info.Time}, nil
}
