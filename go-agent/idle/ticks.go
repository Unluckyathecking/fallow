package idle

// Windows tick arithmetic lives here, in a build-tag-free file, so the pure
// wraparound logic and the reader-injected WindowsDetector are compiled and
// unit-tested on every platform (Linux CI, macOS dev) — not only on Windows.
// The OS seam that actually reads the counters lives in detector_windows.go.
//
// GetTickCount() and LASTINPUTINFO.dwTime are both unsigned 32-bit DWORDs that
// wrap to zero every 2**32 ms (~49.7 days). Using uint32 makes the subtraction
// wrap modulo 2**32 automatically, which is exactly the elapsed-time semantics
// we need across a rollover.

const msPerSecond = 1000.0

// InputTicks is one co-temporal reading of the tick counter and last-input tick.
type InputTicks struct {
	NowMS       uint32
	LastInputMS uint32
}

// elapsedMS returns milliseconds since last input, with unsigned 32-bit
// wraparound handled by uint32 modular subtraction.
func elapsedMS(t InputTicks) uint32 {
	return t.NowMS - t.LastInputMS
}

// TicksReader is the OS seam: it reads GetTickCount() and the last-input tick
// together. Injected in tests; the production reader lives per-platform.
type TicksReader func() (InputTicks, error)

// WindowsDetector is a Detector backed by the Win32 GetLastInputInfo API. Its
// arithmetic is platform-neutral; only the default reader is Windows-only.
type WindowsDetector struct {
	reader TicksReader
}

// NewWindowsDetector builds a WindowsDetector. A nil reader uses the default
// OS seam (which errors on non-Windows hosts).
func NewWindowsDetector(reader TicksReader) *WindowsDetector {
	if reader == nil {
		reader = defaultTicksReader
	}
	return &WindowsDetector{reader: reader}
}

// SecondsSinceInput returns seconds since the last input event.
func (d *WindowsDetector) SecondsSinceInput() (float64, error) {
	ticks, err := d.reader()
	if err != nil {
		return 0, err
	}
	return float64(elapsedMS(ticks)) / msPerSecond, nil
}
