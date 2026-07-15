//go:build linux

package idle

// LinuxDetector is an honest stub. A correct Linux implementation must span X11
// (XScreenSaver), Wayland (per-compositor idle-notify protocols), and
// headless/logind sessions, with no single microsecond-cost API covering all
// three. Rather than ship a silently-wrong detector, v0.1 reports unsupported
// so callers fail loudly on unsupported hosts (mirrors the Python
// LinuxIdleDetector, which raises NotImplementedError).
type LinuxDetector struct{}

// SecondsSinceInput always returns ErrUnsupported.
func (LinuxDetector) SecondsSinceInput() (float64, error) {
	return 0, ErrUnsupported
}

// newPlatformDetector returns the Linux stub detector.
func newPlatformDetector() (Detector, error) {
	return LinuxDetector{}, nil
}
