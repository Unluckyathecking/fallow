//go:build !windows && !darwin && !linux

package idle

// newPlatformDetector reports unsupported on any OS without a dedicated
// implementation, mirroring the Python factory's NotImplementedError.
func newPlatformDetector() (Detector, error) {
	return nil, ErrUnsupported
}
