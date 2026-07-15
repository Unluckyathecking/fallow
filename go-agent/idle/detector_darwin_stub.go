//go:build darwin && !cgo

package idle

// Without cgo the Quartz HID call is unreachable, so macOS idle detection is
// unsupported in a pure-Go (CGO_ENABLED=0) build. Reporting unsupported is
// honest — the alternative would be a silently-wrong idle time.
func newPlatformDetector() (Detector, error) {
	return nil, ErrUnsupported
}
