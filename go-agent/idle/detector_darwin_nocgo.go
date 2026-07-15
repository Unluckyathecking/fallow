//go:build darwin && !cgo

package idle

import (
	"errors"
	"time"
)

type darwinNoCGODetector struct{}

func NewPlatformDetector() Detector {
	return darwinNoCGODetector{}
}

func (darwinNoCGODetector) SecondsSinceInput() (time.Duration, error) {
	return 0, errors.New("macOS idle detection requires cgo and ApplicationServices")
}
