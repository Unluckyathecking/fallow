//go:build linux

package idle

import (
	"errors"
	"testing"
)

func TestLinuxDetectorReturnsDocumentedError(t *testing.T) {
	_, err := NewPlatformDetector().SecondsSinceInput()
	if !errors.Is(err, ErrLinuxUnsupported) {
		t.Fatalf("error = %v", err)
	}
}
