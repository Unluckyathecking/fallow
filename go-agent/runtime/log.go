package runtime

import "log"

// logf is the daemon's single logging seam. It prefixes the agent so operator
// logs are greppable, and keeps every call site terse.
func logf(format string, args ...any) {
	log.Printf("fallow-agent: "+format, args...)
}
