//go:build darwin && cgo

package idle

/*
#cgo LDFLAGS: -framework ApplicationServices
#include <ApplicationServices/ApplicationServices.h>

double fallowSecondsSinceInput(void) {
	return CGEventSourceSecondsSinceLastEventType(
		kCGEventSourceStateHIDSystemState,
		kCGAnyInputEventType
	);
}
*/
import "C"

import "time"

type darwinDetector struct {
	read func() float64
}

func NewPlatformDetector() Detector {
	return darwinDetector{read: readDarwinSeconds}
}

func (detector darwinDetector) SecondsSinceInput() (time.Duration, error) {
	return time.Duration(detector.read() * float64(time.Second)), nil
}

func readDarwinSeconds() float64 {
	return float64(C.fallowSecondsSinceInput())
}
