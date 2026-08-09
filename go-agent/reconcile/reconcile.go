// Package reconcile turns coordinator model assignments into local replicas.
package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

var ErrPortExhausted = errors.New("no local replica port available")

// ManifestSource retrieves authenticated manifests from the coordinator.
type ManifestSource interface {
	Manifest(context.Context, string) (protocol.ModelManifest, error)
}

// Cache ensures a manifest is present and verified locally.
type Cache interface {
	Ensure(context.Context, protocol.ModelManifest) (string, error)
}

// ReplicaSupervisor is the lifecycle surface needed by reconciliation.
type ReplicaSupervisor interface {
	Statuses() []protocol.ReplicaStatus
	StartReplica(protocol.ModelManifest, string, int) error
	StopReplica(string)
}

// PortRange is the inclusive configuration represented by Start and Count.
type PortRange struct{ Start, Count int }

// Reconciler applies desired model IDs serially. It owns no network trust policy.
type Reconciler struct {
	source     ManifestSource
	cache      Cache
	supervisor ReplicaSupervisor
	ports      PortRange
	mu         sync.Mutex
}

func New(source ManifestSource, cache Cache, supervisor ReplicaSupervisor, ports PortRange) (*Reconciler, error) {
	if source == nil || cache == nil || supervisor == nil {
		return nil, errors.New("reconcile dependencies must not be nil")
	}
	if ports.Start <= 0 || ports.Count <= 0 {
		return nil, errors.New("reconcile port range must be positive")
	}
	return &Reconciler{source: source, cache: cache, supervisor: supervisor, ports: ports}, nil
}

// Apply makes running replicas match desired. Duplicate desired IDs are ignored.
func (r *Reconciler) Apply(ctx context.Context, desired []string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	want := make(map[string]bool, len(desired))
	for _, id := range desired {
		if id != "" {
			want[id] = true
		}
	}
	statuses := r.supervisor.Statuses()
	running := make(map[string]protocol.ReplicaStatus, len(statuses))
	used := make(map[int]bool, len(statuses))
	for _, s := range statuses {
		if s.State != protocol.ReplicaStateStopped {
			running[s.ModelID] = s
			if s.Port > 0 {
				used[s.Port] = true
			}
		}
	}
	// Stop first so a removed replica's port can be reused in the same apply.
	for id := range running {
		if !want[id] {
			r.supervisor.StopReplica(id)
			delete(running, id)
		}
	}
	for id := range want {
		if _, ok := running[id]; ok {
			continue
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		manifest, err := r.source.Manifest(ctx, id)
		if err != nil {
			return fmt.Errorf("fetch manifest %q: %w", id, err)
		}
		path, err := r.cache.Ensure(ctx, manifest)
		if err != nil {
			return fmt.Errorf("cache model %q: %w", id, err)
		}
		port, ok := nextPort(r.ports, used)
		if !ok {
			return fmt.Errorf("%w: model %q", ErrPortExhausted, id)
		}
		if err := r.supervisor.StartReplica(manifest, path, port); err != nil {
			return fmt.Errorf("start replica %q: %w", id, err)
		}
		used[port] = true
		running[id] = protocol.ReplicaStatus{ModelID: id, Port: port, State: protocol.ReplicaStateLoading}
	}
	return nil
}

func nextPort(r PortRange, used map[int]bool) (int, bool) {
	for i := 0; i < r.Count; i++ {
		p := r.Start + i
		if !used[p] {
			return p, true
		}
	}
	return 0, false
}

// HTTPManifestSource fetches the coordinator's authenticated manifest endpoint.
type HTTPManifestSource struct {
	baseURL, token string
	client         *http.Client
}

func NewHTTPManifestSource(baseURL, deviceToken string, client *http.Client) *HTTPManifestSource {
	if client == nil {
		client = http.DefaultClient
	}
	return &HTTPManifestSource{strings.TrimRight(baseURL, "/"), deviceToken, client}
}
func (s *HTTPManifestSource) Manifest(ctx context.Context, modelID string) (protocol.ModelManifest, error) {
	endpoint := s.baseURL + "/v1/models/" + url.PathEscape(modelID) + "/manifest"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return protocol.ModelManifest{}, err
	}
	req.Header.Set("Authorization", "Bearer "+s.token)
	resp, err := s.client.Do(req)
	if err != nil {
		return protocol.ModelManifest{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return protocol.ModelManifest{}, fmt.Errorf("manifest request returned HTTP %s", resp.Status)
	}
	var m protocol.ModelManifest
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		return protocol.ModelManifest{}, err
	}
	return m, nil
}
