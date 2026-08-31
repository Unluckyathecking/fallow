package runtime

import (
	"context"
	"errors"
	"os"
	goruntime "runtime"

	"github.com/Unluckyathecking/fallow/go-agent/config"
	"github.com/Unluckyathecking/fallow/go-agent/hostinfo"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// protocolVersion is the wire version the daemon speaks; it must match
// fallow_protocol.version.PROTOCOL_VERSION and the one-shot subcommands.
const protocolVersion = 2

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

// resolveIdentity resumes from the passed-in persisted identity or enrolls a new
// one, mirroring fallow_agent.main.enroll.resolve_identity. It is the direct
// (legacy) path; the caller has already loaded the identity so Site Mode can
// inspect it first. It returns a client already seeded with the identity plus
// the initial agent config.
func resolveIdentity(ctx context.Context, s config.Settings, seams Seams, existing *state.Identity) (Coordinator, protocol.AgentConfig, error) {
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
		Caps:            makeCaps(s.CacheDir),
	})
	if err != nil {
		return nil, protocol.AgentConfig{}, err
	}
	id := state.Identity{AgentID: resp.AgentID, DeviceToken: resp.DeviceToken}
	if err := state.Save(s.StatePath, id); err != nil {
		return nil, protocol.AgentConfig{}, err
	}
	// Symmetrical with enrollSite: a marker left over from the identity this one
	// replaces is stale the moment this one is durable. Run lets a start through
	// on a marker with no identity beside it, so leaving it here would serve one
	// session and then refuse every start after it — the identity is present
	// again, and the marker condemns it.
	if err := state.ClearRevoked(s.StatePath); err != nil {
		logf("warning: %v", err)
	}
	return client, resp.Config, nil
}

// makeCaps reports this machine's real capabilities at enrollment, probed by
// the hostinfo package. cacheDir is the model cache, whose volume supplies the
// free-disk figure. Every probe degrades on its own to a conservative value, so
// makeCaps always returns something the coordinator accepts.
func makeCaps(cacheDir string) protocol.DeviceCaps {
	hostname, err := os.Hostname()
	if err != nil || hostname == "" {
		hostname = "unknown"
	}
	host := hostinfo.Caps(cacheDir)
	return protocol.DeviceCaps{
		Hostname:     hostname,
		Os:           osFamily(),
		OsVersion:    host.OSVersion,
		CPUModel:     host.CPUModel,
		CPUCores:     goruntime.NumCPU(),
		RAMMB:        host.RAMMB,
		DiskFreeMB:   host.DiskFreeMB,
		GPUs:         gpuInfo(host.GPUs),
		AgentVersion: agentVersion,
	}
}

// gpuInfo maps the probed GPUs onto the wire type. No GPUs stays nil, which the
// omitempty tag leaves off the request entirely, exactly as before.
func gpuInfo(gpus []hostinfo.GPU) []protocol.GpuInfo {
	if len(gpus) == 0 {
		return nil
	}
	out := make([]protocol.GpuInfo, 0, len(gpus))
	for _, g := range gpus {
		out = append(out, protocol.GpuInfo{
			Index:  g.Index,
			Name:   g.Name,
			Vendor: g.Vendor,
			VRAMMB: g.VRAMMB,
		})
	}
	return out
}

// gpuStatus maps the sampled GPU state onto the heartbeat's wire type. No GPUs
// stays nil, which omitempty leaves off the beat entirely. It is the live half
// of the pair above: enrollment fit reads the caps GPUs, and every fit
// afterwards — `flw assign`, GET /agents/{id}/fit — reads these, so a GPU desk
// must report both or the two disagree about the same machine.
func gpuStatus(gpus []hostinfo.GPUStatus) []protocol.GpuStatus {
	if len(gpus) == 0 {
		return nil
	}
	out := make([]protocol.GpuStatus, 0, len(gpus))
	for _, g := range gpus {
		out = append(out, protocol.GpuStatus{
			Index:       g.Index,
			VRAMFreeMB:  g.VRAMFreeMB,
			UtilPercent: g.UtilPercent,
		})
	}
	return out
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
