package idle

import (
	"math"
	"sync"
	"testing"
)

// ── Windows wraparound arithmetic (ported from test_idle_windows.py) ─────────

func TestElapsedMS(t *testing.T) {
	const maxDword = uint32(1<<32 - 1)
	tests := []struct {
		name string
		now  uint32
		last uint32
		want uint32
	}{
		{"no wrap", 5000, 2000, 3000},
		{"tick after rollover", 100, maxDword - 49, 150}, // 2**32-50 == max-49
		{"last input near max", 0, maxDword, 1},
		{"zero when equal", 42, 42, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := elapsedMS(InputTicks{NowMS: tc.now, LastInputMS: tc.last})
			if got != tc.want {
				t.Errorf("elapsedMS(%d,%d) = %d, want %d", tc.now, tc.last, got, tc.want)
			}
		})
	}
}

func TestWindowsDetectorConvertsMsToSeconds(t *testing.T) {
	d := NewWindowsDetector(func() (InputTicks, error) {
		return InputTicks{NowMS: 7500, LastInputMS: 0}, nil
	})
	got, err := d.SecondsSinceInput()
	if err != nil || got != 7.5 {
		t.Fatalf("got %v, %v; want 7.5", got, err)
	}
}

func TestWindowsDetectorUsesWraparoundForSeconds(t *testing.T) {
	d := NewWindowsDetector(func() (InputTicks, error) {
		return InputTicks{NowMS: 250, LastInputMS: uint32(1<<32 - 250)}, nil
	})
	got, err := d.SecondsSinceInput()
	if err != nil || got != 0.5 {
		t.Fatalf("got %v, %v; want 0.5", got, err)
	}
}

// ── FakeDetector (ported from test_idle_fake.py) ─────────────────────────────

func TestFakeDefaultsAndSetters(t *testing.T) {
	d, err := NewFakeDetector(0)
	if err != nil {
		t.Fatal(err)
	}
	if v, _ := d.SecondsSinceInput(); v != 0 {
		t.Errorf("default = %v, want 0", v)
	}
	if err := d.SetIdle(120); err != nil {
		t.Fatal(err)
	}
	if v, _ := d.SecondsSinceInput(); v != 120 {
		t.Errorf("after set = %v, want 120", v)
	}
	if err := d.Advance(2.5); err != nil {
		t.Fatal(err)
	}
	if v, _ := d.SecondsSinceInput(); v != 122.5 {
		t.Errorf("after advance = %v, want 122.5", v)
	}
	d.SimulateInput()
	if v, _ := d.SecondsSinceInput(); v != 0 {
		t.Errorf("after simulate input = %v, want 0", v)
	}
}

func TestFakeRejectsNegative(t *testing.T) {
	if _, err := NewFakeDetector(-1); err == nil {
		t.Error("expected error for negative initial value")
	}
	d, _ := NewFakeDetector(1)
	if err := d.SetIdle(-0.001); err == nil {
		t.Error("expected error for negative set")
	}
	if v, _ := d.SecondsSinceInput(); v != 1 {
		t.Errorf("value changed after rejected set: %v", v)
	}
	if err := d.Advance(-5); err == nil {
		t.Error("expected error for advance below zero")
	}
	if v, _ := d.SecondsSinceInput(); v != 1 {
		t.Errorf("value changed after rejected advance: %v", v)
	}
}

func TestFakeThreadSafeUnderContention(t *testing.T) {
	d, _ := NewFakeDetector(0)
	const iterations = 2000
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				_ = d.SetIdle(float64(j))
				_, _ = d.SecondsSinceInput()
				d.SimulateInput()
			}
		}()
	}
	wg.Wait()
	if v, _ := d.SecondsSinceInput(); v < 0 {
		t.Errorf("invariant violated: %v", v)
	}
}

// ── Factory / ConstantDetector ───────────────────────────────────────────────

func TestForceIdleRequiresBench(t *testing.T) {
	if _, err := CreateDetector(false, true); err == nil {
		t.Error("force_idle without bench should error")
	}
	d, err := CreateDetector(true, true)
	if err != nil {
		t.Fatal(err)
	}
	v, _ := d.SecondsSinceInput()
	if v <= 0 || math.IsInf(v, 1) {
		t.Errorf("constant detector = %v; want large finite", v)
	}
}

func TestCreateDetectorReturnsSomethingForThisOS(t *testing.T) {
	// On the supported dev/CI OSes (darwin, linux) CreateDetector must not
	// error at construction; whether SecondsSinceInput works is OS-specific.
	d, err := CreateDetector(false, false)
	if err != nil && err != ErrUnsupported {
		t.Fatalf("unexpected construction error: %v", err)
	}
	if err == nil && d == nil {
		t.Fatal("nil detector with nil error")
	}
}
