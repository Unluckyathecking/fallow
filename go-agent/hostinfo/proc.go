package hostinfo

import (
	"errors"
	"strconv"
	"strings"
)

// Parsers for the Linux text interfaces. They are pure string-to-value
// functions with no build tag so the fixtures test them on any platform; the
// file reads live in linux.go.

var errNoCPULine = errors.New("no aggregate cpu line in /proc/stat")

// parseMemKB returns the value of a /proc/meminfo key, in kB. Lines look like
// "MemAvailable:   15839612 kB".
func parseMemKB(meminfo, key string) (uint64, bool) {
	for _, line := range strings.Split(meminfo, "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok || name != key {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			return 0, false
		}
		kb, err := strconv.ParseUint(fields[0], 10, 64)
		if err != nil {
			return 0, false
		}
		return kb, true
	}
	return 0, false
}

// parseCPUModel returns the first "model name" in /proc/cpuinfo. ARM kernels
// print "Model name" or no model line at all, so an empty result is normal and
// the caller degrades.
func parseCPUModel(cpuinfo string) string {
	for _, line := range strings.Split(cpuinfo, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(key), "model name") {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

// parsePrettyName returns PRETTY_NAME from /etc/os-release, unquoted.
func parsePrettyName(osRelease string) string {
	for _, line := range strings.Split(osRelease, "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok || key != "PRETTY_NAME" {
			continue
		}
		return strings.Trim(strings.TrimSpace(value), `"'`)
	}
	return ""
}

// parseProcStat sums the aggregate "cpu" line of /proc/stat into busy and total
// jiffies. Busy is everything except idle and iowait, matching how psutil
// computes cpu_percent.
func parseProcStat(stat string) (cpuTimes, error) {
	for _, line := range strings.Split(stat, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 5 || fields[0] != "cpu" {
			continue
		}
		var times cpuTimes
		for i, field := range fields[1:] {
			value, err := strconv.ParseUint(field, 10, 64)
			if err != nil {
				return cpuTimes{}, err
			}
			times.total += value
			if i != 3 && i != 4 { // idle, iowait
				times.busy += value
			}
		}
		return times, nil
	}
	return cpuTimes{}, errNoCPULine
}
