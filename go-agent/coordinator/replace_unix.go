//go:build !windows

package coordinator

import "os"

func replaceFile(source, destination string) error {
	return os.Rename(source, destination)
}
