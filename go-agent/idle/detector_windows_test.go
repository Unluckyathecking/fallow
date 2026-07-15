//go:build windows

package idle

import (
	"testing"
	"time"
)

func TestWindowsDetectorConvertsWrappedTicks(t *testing.T) {
	detector := windowsDetector{read: func() (inputTicks, error) {
		return inputTicks{now: 250, lastInput: ^uint32(0) - 249}, nil
	}}
	idle, err := detector.SecondsSinceInput()
	if err != nil {
		t.Fatal(err)
	}
	if idle != 500*time.Millisecond {
		t.Fatalf("idle = %s", idle)
	}
}
