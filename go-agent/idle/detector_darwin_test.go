//go:build darwin && cgo

package idle

import (
	"testing"
	"time"
)

func TestDarwinDetectorConvertsCoreGraphicsSeconds(t *testing.T) {
	detector := darwinDetector{read: func() float64 { return 12.5 }}
	idle, err := detector.SecondsSinceInput()
	if err != nil {
		t.Fatal(err)
	}
	if idle != 12500*time.Millisecond {
		t.Fatalf("idle = %s", idle)
	}
}
