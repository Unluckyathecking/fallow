package idle

import (
	"errors"
	"time"
)

var ErrLinuxUnsupported = errors.New(
	"idle detection is not implemented on Linux; X11, Wayland, and logind need separate sources",
)

type Detector interface {
	SecondsSinceInput() (time.Duration, error)
}

func elapsedMilliseconds(now, lastInput uint32) uint32 {
	return now - lastInput
}
