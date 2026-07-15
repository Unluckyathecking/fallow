//go:build unix

package interop

import (
	"bytes"
	"sync"
)

// syncBuffer is a mutex-guarded bytes.Buffer so the coordinator subprocess can
// write stdout/stderr from an os/exec pump goroutine while the test reads it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
