//go:build linux

package idle

import "time"

type linuxDetector struct{}

func NewPlatformDetector() Detector {
	return linuxDetector{}
}

func (linuxDetector) SecondsSinceInput() (time.Duration, error) {
	return 0, ErrLinuxUnsupported
}
