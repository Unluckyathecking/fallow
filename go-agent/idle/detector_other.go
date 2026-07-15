//go:build !windows && !darwin && !linux

package idle

import (
	"fmt"
	"runtime"
	"time"
)

type unsupportedDetector struct{}

func NewPlatformDetector() Detector {
	return unsupportedDetector{}
}

func (unsupportedDetector) SecondsSinceInput() (time.Duration, error) {
	return 0, fmt.Errorf("idle detection is not implemented on %s", runtime.GOOS)
}
