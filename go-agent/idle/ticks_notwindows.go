//go:build !windows

package idle

// defaultTicksReader errors off Windows so a WindowsDetector constructed with a
// nil reader on a non-Windows host fails loudly rather than reporting a bogus
// idle time. Tests inject their own reader and never hit this path.
func defaultTicksReader() (InputTicks, error) {
	return InputTicks{}, ErrUnsupported
}
