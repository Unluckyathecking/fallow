//go:build unix

package supervisor

import "syscall"

// suspendProcess stops a running process. On Unix this sends SIGSTOP to the
// single process (matching psutil.Process.suspend, which signals the process
// itself, not its group — the supervisor never puts children in a new session).
func suspendProcess(pid int) error {
	return syscall.Kill(pid, syscall.SIGSTOP)
}

// resumeProcess continues a stopped process with SIGCONT (matching
// psutil.Process.resume).
func resumeProcess(pid int) error {
	return syscall.Kill(pid, syscall.SIGCONT)
}
