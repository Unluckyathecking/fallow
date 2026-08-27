//go:build linux || darwin

package hostinfo

import "golang.org/x/sys/unix"

// diskFreeMB reports the space available to this (unprivileged) user under
// path, so the number is what a model download can actually use. A failed
// statfs reports 0 MB, the most conservative value the wire allows.
func diskFreeMB(path string) int {
	var stat unix.Statfs_t
	if err := unix.Statfs(path, &stat); err != nil {
		warnOnce("disk_free", "cannot statfs %s (%v); reporting 0 MB free", path, err)
		return 0
	}
	return mb(uint64(stat.Bavail) * uint64(stat.Bsize))
}
