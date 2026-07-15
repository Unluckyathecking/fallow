package coordinator

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const stateMode os.FileMode = 0o600

type IdentityState struct {
	AgentID     string `json:"agent_id"`
	DeviceToken string `json:"device_token"`
}

func LoadIdentity(path string) (*IdentityState, error) {
	payload, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read identity file %s: %w", path, err)
	}
	var state IdentityState
	if err := json.Unmarshal(payload, &state); err != nil {
		return nil, fmt.Errorf("malformed identity file %s: %w", path, err)
	}
	if state.AgentID == "" || state.DeviceToken == "" {
		return nil, fmt.Errorf("malformed identity file %s: empty credential", path)
	}
	return &state, nil
}

func SaveIdentity(path string, state IdentityState) error {
	if state.AgentID == "" || state.DeviceToken == "" {
		return errors.New("identity fields must not be empty")
	}
	payload, err := json.Marshal(state)
	if err != nil {
		return fmt.Errorf("encode identity: %w", err)
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("create identity directory: %w", err)
	}
	temporary, err := os.CreateTemp(directory, ".fallow-state-*")
	if err != nil {
		return fmt.Errorf("create temporary identity: %w", err)
	}
	temporaryPath := temporary.Name()
	keep := false
	defer func() {
		_ = temporary.Close()
		if !keep {
			_ = os.Remove(temporaryPath)
		}
	}()
	if err := temporary.Chmod(stateMode); err != nil {
		return fmt.Errorf("set identity permissions: %w", err)
	}
	if _, err := temporary.Write(payload); err != nil {
		return fmt.Errorf("write identity: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		return fmt.Errorf("sync identity: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close identity: %w", err)
	}
	if err := replaceFile(temporaryPath, path); err != nil {
		return fmt.Errorf("replace identity: %w", err)
	}
	keep = true
	return nil
}

func ResolveIdentity(
	ctx context.Context,
	path string,
	enrollmentToken string,
	protocolVersion int,
	caps protocol.DeviceCaps,
	client *Client,
) (IdentityState, protocol.AgentConfig, error) {
	existing, err := LoadIdentity(path)
	if err != nil {
		return IdentityState{}, protocol.AgentConfig{}, err
	}
	if existing != nil {
		client.agentID = existing.AgentID
		client.deviceToken = existing.DeviceToken
		return *existing, DefaultAgentConfig(), nil
	}
	if enrollmentToken == "" {
		return IdentityState{}, protocol.AgentConfig{}, errors.New(
			"no persisted identity and no enrollment token configured",
		)
	}
	response, err := client.Register(ctx, protocol.RegisterRequest{
		EnrollmentToken: enrollmentToken,
		ProtocolVersion: protocolVersion,
		Caps:            caps,
	})
	if err != nil {
		return IdentityState{}, protocol.AgentConfig{}, err
	}
	state := IdentityState{AgentID: response.AgentID, DeviceToken: response.DeviceToken}
	if err := SaveIdentity(path, state); err != nil {
		return IdentityState{}, protocol.AgentConfig{}, err
	}
	return state, response.Config, nil
}

func DefaultAgentConfig() protocol.AgentConfig {
	return protocol.AgentConfig{
		AssignedModels:     []string{},
		HeartbeatIntervalS: 5,
		IdleThresholdS:     120,
		PollIntervalMs:     100,
		VRAMEvictAfterS:    60,
	}
}
