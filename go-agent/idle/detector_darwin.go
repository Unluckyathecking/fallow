//go:build darwin && cgo

package idle

/*
#cgo LDFLAGS: -framework CoreGraphics
#include <CoreGraphics/CoreGraphics.h>

// Wrap the Quartz call so the Go side needs no CoreGraphics enum constants.
static double fallow_seconds_since_last_input(void) {
    return (double)CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateHIDSystemState, kCGAnyInputEventType);
}
*/
import "C"

// DarwinDetector reports seconds since the last system-wide HID event via
// Quartz CGEventSourceSecondsSinceLastEventType. That call already returns
// whole-system idle seconds, so no arithmetic is needed here (mirrors the
// Python DarwinIdleDetector).
type DarwinDetector struct{}

// SecondsSinceInput returns seconds since the last HID event.
func (DarwinDetector) SecondsSinceInput() (float64, error) {
	return float64(C.fallow_seconds_since_last_input()), nil
}

// newPlatformDetector returns the macOS detector (cgo build).
func newPlatformDetector() (Detector, error) {
	return DarwinDetector{}, nil
}
