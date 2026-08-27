package hostinfo

import "testing"

// The NVML name buffer is a fixed-size C array the driver fills in, so the
// decode is exercised against fixtures on any platform.
func TestNvmlName(t *testing.T) {
	buf := make([]byte, nameBufferSize)
	copy(buf, "NVIDIA GeForce RTX 4090")
	if got := nvmlName(buf); got != "NVIDIA GeForce RTX 4090" {
		t.Fatalf("name = %q", got)
	}
	if got := nvmlName([]byte("unterminated")); got != "unterminated" {
		t.Fatalf("name = %q", got)
	}
	if got := nvmlName(make([]byte, nameBufferSize)); got != "" {
		t.Fatalf("name = %q, want empty", got)
	}
}
