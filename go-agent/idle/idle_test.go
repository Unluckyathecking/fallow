package idle

import "testing"

func TestElapsedMillisecondsUsesUint32Wraparound(t *testing.T) {
	tests := []struct {
		name      string
		now       uint32
		lastInput uint32
		want      uint32
	}{
		{name: "ordinary", now: 5000, lastInput: 2000, want: 3000},
		{name: "rollover", now: 100, lastInput: ^uint32(0) - 49, want: 150},
		{name: "maximum to zero", now: 0, lastInput: ^uint32(0), want: 1},
		{name: "same tick", now: 42, lastInput: 42, want: 0},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := elapsedMilliseconds(test.now, test.lastInput); got != test.want {
				t.Fatalf("elapsed = %d, want %d", got, test.want)
			}
		})
	}
}
