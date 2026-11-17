// Command agentctl runs the Go agent, either as the persistent daemon or as one
// of the scriptable one-shot subcommands.
//
// The `run` subcommand is the production daemon: it reads the shared agent TOML
// config, enrolls (or resumes from the 0600 state file), and composes the
// coordinator client, preemption controller, and process supervisor into one run
// loop that heartbeats, polls for work, drives the preempt state machine, and
// shuts down cleanly on SIGINT/SIGTERM.
//
// The `reclaim` and `release` subcommands are the user's instant takedown
// control (ADR 042): they write and remove the reclaim flag file the running
// daemon's poll loop watches, reading the same config so the flag lands beside
// the same state file.
//
// The one-shot subcommands (register, heartbeat, poll, upload, complete) each
// perform exactly one agent->coordinator action and print its result as a single
// JSON object on stdout. They own no loop, no idle detection, and no supervisor —
// only the wire calls the Python parity harness (E4.4) threads together to assert
// on coordinator state. Errors go to stderr with a non-zero exit.
//
// Usage:
//
//	agentctl run       -config PATH
//	agentctl reclaim   -config PATH
//	agentctl release   -config PATH
//	agentctl register  -url URL -token TOKEN [-hostname H] [-state PATH]
//	agentctl heartbeat -url URL -agent-id ID -token DEVTOK [-state-name idle|active|draining]
//	                   [-seq N] [-replica model:port:state ...]
//	agentctl poll      -url URL -agent-id ID -token DEVTOK [-timeout S]
//	agentctl upload    -url URL -agent-id ID -token DEVTOK -unit UID -attempt N -payload BYTES
//	agentctl complete  -url URL -agent-id ID -token DEVTOK -unit UID -attempt N [-result-ref REF]
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/preempt"
	"github.com/Unluckyathecking/fallow/go-agent/runtime"
)

// version and commit are stamped at release time via -ldflags -X (see
// go-agent/.goreleaser.yaml). They stay at these defaults for a plain
// `go build`.
var (
	version = "dev"
	commit  = "none"
)

func main() {
	if len(os.Args) < 2 {
		fail("usage: agentctl <run|doctor|reclaim|release|register|heartbeat|poll|upload|complete|version> [flags]")
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "version":
		err = emit(map[string]string{"version": version, "commit": commit})
	case "run":
		err = runDaemon(args)
	case "doctor":
		err = runDoctor(args)
	case "reclaim":
		err = runControl(args, true)
	case "release":
		err = runControl(args, false)
	case "register":
		err = runRegister(args)
	case "heartbeat":
		err = runHeartbeat(args)
	case "poll":
		err = runPoll(args)
	case "upload":
		err = runUpload(args)
	case "complete":
		err = runComplete(args)
	default:
		fail("unknown subcommand %q", cmd)
	}
	if err != nil {
		fail("%s: %v", cmd, err)
	}
}

// runDaemon reads the config and runs the agent until SIGINT/SIGTERM. Unlike the
// one-shot subcommands it prints no JSON: it logs to stderr and blocks.
func runDaemon(args []string) error {
	fs := newFlagSet("run")
	configPath := fs.String("config", "", "path to the agent TOML config")
	mustParse(fs, args)
	if *configPath == "" {
		return fmt.Errorf("-config is required")
	}

	settings, err := config.Load(*configPath, os.Getenv)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	err = runtime.New(settings, runtime.Seams{}).Run(ctx)
	if err != nil && runtime.IsRevocation(err) {
		// The coordinator named this identity as revoked, so there is nothing to
		// retry and nothing left running. Exiting 0 ends it quietly: the Windows
		// Scheduled Task restarts only on failure, and a non-zero exit here
		// would relaunch the daemon into the same rejection every minute. The
		// runtime has recorded the reason beside the state file; `doctor` reads
		// it, and re-enrolling from a fresh join file is the only way back.
		//
		// Any other 401 falls through and exits non-zero on purpose: a
		// coordinator that lost its database refuses every desk, and the task's
		// retry is what brings the fleet back when the coordinator returns.
		fmt.Fprintf(os.Stderr, "agent stopped: %v; re-enrol from a fresh join file to serve again\n", err)
		return nil
	}
	return err
}

// runControl writes or removes the reclaim flag file the running daemon watches,
// the user's instant takedown control (see ADR 042). reclaim=true stops all local
// serving until release; reclaim=false restores normal idle-based serving. It
// reads the same config the daemon does so the flag lands beside the same state
// file, then prints the flag path as JSON.
func runControl(args []string, reclaim bool) error {
	name := "release"
	if reclaim {
		name = "reclaim"
	}
	fs := newFlagSet(name)
	configPath := fs.String("config", "", "path to the agent TOML config")
	mustParse(fs, args)
	if *configPath == "" {
		return fmt.Errorf("-config is required")
	}
	settings, err := config.Load(*configPath, os.Getenv)
	if err != nil {
		return err
	}
	if reclaim {
		path, err := preempt.RequestReclaim(settings.StatePath)
		if err != nil {
			return err
		}
		return emit(map[string]string{"reclaimed": path})
	}
	path, err := preempt.RequestRelease(settings.StatePath)
	if err != nil {
		return err
	}
	return emit(map[string]string{"released": path})
}

func newFlagSet(name string) *flag.FlagSet {
	return flag.NewFlagSet(name, flag.ContinueOnError)
}

func mustParse(fs *flag.FlagSet, args []string) {
	if err := fs.Parse(args); err != nil {
		fail("%s: %v", fs.Name(), err)
	}
}

// emit writes v as a single compact JSON line on stdout.
func emit(v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	fmt.Println(string(data))
	return nil
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
