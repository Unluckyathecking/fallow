package runtime

import (
	"context"
	"errors"
	"os"
	goruntime "runtime"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// protocolVersion is the wire version the daemon speaks; it must match
// fallow_protocol.version.PROTOCOL_VERSION and the one-shot subcommands.
const protocolVersion = 1

// agentVersion is reported in the device capabilities at enrollment.
const agentVersion = "0.1.0"

// defaultAgentConfig mirrors the Python AgentConfig defaults, used when resuming
// from a persisted identity. The daemon runs on these values for the session;
// applying live config from heartbeat responses is future work (today the
// heartbeat response is read only for its desired-models list).
func defaultAgentConfig() protocol.AgentConfig {
	return protocol.AgentConfig{
		HeartbeatIntervalS: 5.0,
		IdleThresholdS:     120.0,
		PollIntervalMs:     100,
		VRAMEvictAfterS:    60.0,
	}
}

// resolveIdentity loads the persisted identity or enrolls a new one, mirroring
// fallow_agent.main.enroll.resolve_identity. It returns a client already seeded
// with the identity plus the initial agent config.
func resolveIdentity(ctx context.Context, s config.Settings, seams Seams) (Coordinator, protocol.AgentConfig, error) {
	existing, err := state.Load(s.StatePath)
	if err != nil {
		return nil, protocol.AgentConfig{}, err
	}
	if existing != nil {
		client := seams.NewCoordinator(s.CoordinatorURL, existing.AgentID, existing.DeviceToken)
		return client, defaultAgentConfig(), nil
	}
	if s.EnrollmentToken == "" {
		return nil, protocol.AgentConfig{}, errors.New(
			"no persisted identity and no enrollment_token configured; cannot enroll",
		)
	}
	client := seams.NewCoordinator(s.CoordinatorURL, "", "")
	resp, err := client.Register(ctx, protocol.RegisterRequest{
		EnrollmentToken: s.EnrollmentToken,
		ProtocolVersion: protocolVersion,
		Caps:            makeCaps(),
	})
	if err != nil {
		return nil, protocol.AgentConfig{}, err
	}
	id := state.Identity{AgentID: resp.AgentID, DeviceToken: resp.DeviceToken}
	if err := state.Save(s.StatePath, id); err != nil {
		return nil, protocol.AgentConfig{}, err
	}
	return client, resp.Config, nil
}

// placeholderRAMMB is reported until a real host-metrics probe lands. The
// coordinator requires ram_mb > 0, and under-reporting is the safe default: it
// only ever excludes this agent from models it might actually fit, never the
// reverse.
const placeholderRAMMB = 1024

// makeCaps reports this machine's capabilities at enrollment. CPU-core count is
// real; the remaining hardware numbers are conservative placeholders until a Go
// host-metrics probe lands (the coordinator gates placement on os/hostname and
// the ram/disk minimums, not on exact model strings).
func makeCaps() protocol.DeviceCaps {
	hostname, err := os.Hostname()
	if err != nil || hostname == "" {
		hostname = "unknown"
	}
	return protocol.DeviceCaps{
		Hostname:     hostname,
		Os:           osFamily(),
		OsVersion:    "unknown",
		CPUModel:     "unknown",
		CPUCores:     goruntime.NumCPU(),
		RAMMB:        placeholderRAMMB,
		DiskFreeMB:   0,
		AgentVersion: agentVersion,
	}
}

func osFamily() protocol.OsFamily {
	switch goruntime.GOOS {
	case "windows":
		return protocol.OsFamilyWindows
	case "darwin":
		return protocol.OsFamilyMacos
	default:
		return protocol.OsFamilyLinux
	}
}
