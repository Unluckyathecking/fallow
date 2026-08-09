// Package runtime — site.go composes LAN Site Mode: it parses the join profile,
// establishes pinned HTTPS trust, enrolls once, persists a token-free profile,
// and wires model reconciliation and the loopback inference claim runner.
//
// Site Mode is strictly opt-in. When it is off (no join bundle configured and no
// persisted profile) the daemon keeps its legacy URL, proxy and bind behaviour
// byte for byte. When it is on the coordinator URL and its pinned certificates
// come from the profile, replicas serve only over loopback, and every outbound
// call rides the fail-closed pinned client.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"sync"

	"github.com/Unluckyathecking/fallow/go-agent/inference"
	"github.com/Unluckyathecking/fallow/go-agent/modelcache"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"github.com/Unluckyathecking/fallow/go-agent/reconcile"
	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/Unluckyathecking/fallow/go-agent/state"
)

// wiring is the resolved runtime composition: the coordinator client, the
// initial agent config, the sequence source, and the optional Site Mode parts.
type wiring struct {
	client Coordinator
	cfg    protocol.AgentConfig
	seq    seqSource
	site   *siteRuntime // nil for direct agents
}

// siteRuntime holds the Site Mode collaborators started alongside the loops.
type siteRuntime struct {
	availability *availability
	replicas     replicaTarget
	runner       inference.Runner
	reconciler   modelReconciler
	desired      chan []string
	poke         chan struct{}

	// eligible reports whether the machine may start replicas right now: idle and
	// not reclaimed. It gates reconciliation so a heartbeat's desired set can
	// never restart a replica the user's return, reclaim or VRAM eviction just
	// took down. Wired in Run once the controllers exist.
	eligible func() bool

	mu          sync.Mutex
	pending     []string
	havePending bool
}

func newSiteRuntime(av *availability, replicas replicaTarget, runner inference.Runner, reconciler modelReconciler) *siteRuntime {
	return &siteRuntime{
		availability: av,
		replicas:     replicas,
		runner:       runner,
		reconciler:   reconciler,
		desired:      make(chan []string, 1),
		poke:         make(chan struct{}, 1),
	}
}

// submitDesired hands the reconcile worker the latest desired model set,
// coalescing to newest so a slow reconcile never stalls the heartbeat loop.
// Empty and nil sets are valid and flow through: they remove every replica.
func (s *siteRuntime) submitDesired(desired []string) {
	select {
	case <-s.desired:
	default:
	}
	select {
	case s.desired <- desired:
	default:
	}
}

// nudge asks the worker to re-evaluate whether it can now apply a deferred set,
// called by the poll loop when the machine becomes serving-eligible again.
func (s *siteRuntime) nudge() {
	select {
	case s.poke <- struct{}{}:
	default:
	}
}

// reconcileWorker applies desired model sets serially off the heartbeat thread.
// It defers any set that arrives while the machine is not serving-eligible and
// applies the latest once eligibility returns, so reconciliation never fights
// the preemption, reclaim or eviction takedown.
func (s *siteRuntime) reconcileWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case d := <-s.desired:
			s.mu.Lock()
			s.pending = s.coalesce(d)
			s.havePending = true
			s.mu.Unlock()
		case <-s.poke:
		}
		s.applyIfEligible(ctx)
	}
}

// applyIfEligible applies the pending desired set only when the machine may
// serve; otherwise it keeps the set pending for the next eligibility edge.
func (s *siteRuntime) applyIfEligible(ctx context.Context) {
	s.mu.Lock()
	if !s.havePending || (s.eligible != nil && !s.eligible()) {
		s.mu.Unlock()
		return
	}
	desired := s.pending
	s.havePending = false
	s.mu.Unlock()
	if err := s.reconciler.Apply(ctx, desired); err != nil && ctx.Err() == nil {
		logf("reconcile desired models failed: %v", err)
	}
}

func (s *siteRuntime) coalesce(d []string) []string {
	for {
		select {
		case n := <-s.desired:
			d = n
		default:
			return d
		}
	}
}

// resolveWiring dispatches to the direct or Site Mode composition. It reads the
// persisted identity once so a stored Site profile can never be silently
// replaced or downgraded.
func (r *Runtime) resolveWiring(ctx context.Context) (wiring, error) {
	existing, err := state.Load(r.settings.StatePath)
	if err != nil {
		return wiring{}, err
	}
	if existing != nil && existing.Site != nil && !r.settings.SiteMode() {
		return wiring{}, errors.New(
			"a persisted Site Mode profile was found but site_join_bundle is not configured; " +
				"keep site_join_bundle set so the daemon resumes Site Mode from the stored profile",
		)
	}
	if !r.settings.SiteMode() {
		client, cfg, err := resolveIdentity(ctx, r.settings, r.seams, existing)
		if err != nil {
			return wiring{}, err
		}
		return wiring{client: client, cfg: cfg, seq: &volatileSeq{}}, nil
	}
	return r.resolveSite(ctx, existing)
}

// resolveSite enrolls (first run) or resumes (restart) a Site Mode agent, then
// builds the pinned client, persistent sequence, reconciler and claim runner.
func (r *Runtime) resolveSite(ctx context.Context, existing *state.Identity) (wiring, error) {
	profile, id, cfg, err := r.siteIdentity(ctx, existing)
	if err != nil {
		return wiring{}, err
	}

	baseURL, err := firstCoordinatorURL(profile)
	if err != nil {
		return wiring{}, err
	}
	pinned, err := r.seams.NewPinnedClient(profile)
	if err != nil {
		return wiring{}, fmt.Errorf("build pinned client: %w", err)
	}
	client := r.seams.NewSiteCoordinator(baseURL, id.AgentID, id.DeviceToken, pinned)

	seq, err := newPersistentSeq(r.settings.StatePath, id, state.Save, func(err error) {
		r.fatal(fmt.Errorf("site sequence persist failed: %w", err))
	})
	if err != nil {
		return wiring{}, fmt.Errorf("initialize site sequence: %w", err)
	}

	reconciler, err := r.buildReconciler(baseURL, id.DeviceToken, pinned)
	if err != nil {
		return wiring{}, err
	}

	var relay inference.Coordinator = r.seams.ClaimCoordinator
	if relay == nil {
		relay = newRelayClient(baseURL, id.AgentID, id.DeviceToken, pinned)
	}
	av := newAvailability()
	replicas := replicaTarget{supervisor: r.supervisor}
	site := newSiteRuntime(av, replicas, inference.Runner{Coordinator: relay}, reconciler)
	return wiring{client: client, cfg: cfg, seq: seq, site: site}, nil
}

// siteIdentity returns the profile and identity for a Site Mode agent, enrolling
// on first run and resuming from the persisted profile on restart.
func (r *Runtime) siteIdentity(ctx context.Context, existing *state.Identity) (siteclient.Profile, state.Identity, protocol.AgentConfig, error) {
	if existing != nil {
		if existing.Site == nil {
			return siteclient.Profile{}, state.Identity{}, protocol.AgentConfig{}, errors.New(
				"an existing non-Site identity is present; refusing to convert it to Site Mode silently",
			)
		}
		return siteProfileFrom(existing.Site), *existing, defaultAgentConfig(), nil
	}
	return r.enrollSite(ctx)
}

// enrollSite performs the one-time first-run enrollment: parse the join file,
// verify the pin, register once, persist the identity and token-free profile,
// then remove the installed enrollment token from disk.
func (r *Runtime) enrollSite(ctx context.Context) (siteclient.Profile, state.Identity, protocol.AgentConfig, error) {
	var zeroP siteclient.Profile
	data, err := os.ReadFile(r.settings.SiteJoinBundle)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, fmt.Errorf("read join file %s: %w", r.settings.SiteJoinBundle, err)
	}
	bundle, err := siteclient.ParseJoin(data)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, fmt.Errorf("parse join file: %w", err)
	}
	profile := bundle.Profile()

	baseURL, err := firstCoordinatorURL(profile)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, err
	}
	pinned, err := r.seams.NewPinnedClient(profile)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, fmt.Errorf("build pinned client: %w", err)
	}
	enroller := r.seams.NewSiteCoordinator(baseURL, "", "", pinned)
	resp, err := enroller.Register(ctx, protocol.RegisterRequest{
		EnrollmentToken: bundle.EnrollmentToken,
		ProtocolVersion: protocolVersion,
		Caps:            makeCaps(),
	})
	if err != nil {
		// Register is never retried; an ambiguous outcome asks for a new token.
		return zeroP, state.Identity{}, protocol.AgentConfig{}, fmt.Errorf("site enrollment failed (not retried): %w", err)
	}
	if resp.AgentID == "" || resp.DeviceToken == "" {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, errors.New(
			"ambiguous registration: coordinator returned no identity; request a fresh join token",
		)
	}

	id := state.Identity{
		AgentID:     resp.AgentID,
		DeviceToken: resp.DeviceToken,
		Site:        stateProfile(profile),
		Seq:         0,
	}
	if err := state.Save(r.settings.StatePath, id); err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, fmt.Errorf("persist site identity: %w", err)
	}
	// The identity is durable; remove the installed enrollment token.
	if err := os.Remove(r.settings.SiteJoinBundle); err != nil && !os.IsNotExist(err) {
		logf("warning: could not remove join file %s after enrollment: %v", r.settings.SiteJoinBundle, err)
	}
	logf("site enrolled (agent_id=%s, site_id=%s)", id.AgentID, profile.SiteID)
	return profile, id, resp.Config, nil
}

// buildReconciler returns the injected reconciler or the production one over the
// pinned client and the shared model cache. The supervisor is wrapped in a guard
// that re-checks serving-eligibility immediately before every replica start.
func (r *Runtime) buildReconciler(baseURL, deviceToken string, pinned *http.Client) (modelReconciler, error) {
	if r.seams.Reconciler != nil {
		return r.seams.Reconciler, nil
	}
	source := reconcile.NewHTTPManifestSource(baseURL, deviceToken, pinned)
	store := modelcache.New(baseURL, deviceToken, pinned, modelcache.WithCacheDir(r.settings.CacheDir))
	guarded := guardedSupervisor{Supervisor: r.supervisor, eligible: r.servingEligible}
	return reconcile.New(source, store, guarded, reconcile.PortRange{
		Start: r.settings.PortRange.Start,
		Count: r.settings.PortRange.Count,
	})
}

// errNotServingEligible is returned by the guard when a replica start is
// attempted while the machine is not serving-eligible.
var errNotServingEligible = errors.New("machine is not serving-eligible; refusing to start replica")

// guardedSupervisor re-checks serving-eligibility immediately before every
// replica start. A user who returns (or a reclaim) during a slow cache download,
// after the reconcile worker's outer eligibility check, must not cause a
// StartReplica after the machine was suspended. StopReplica and Statuses pass
// straight through the embedded supervisor.
type guardedSupervisor struct {
	Supervisor
	eligible func() bool
}

func (g guardedSupervisor) StartReplica(manifest protocol.ModelManifest, modelPath string, port int) error {
	if g.eligible != nil && !g.eligible() {
		return errNotServingEligible
	}
	return g.Supervisor.StartReplica(manifest, modelPath, port)
}

// firstCoordinatorURL validates that the profile carries a usable first
// coordinator origin and returns it (order is significant, so the runner dials
// the first). It fails closed on a corrupt persisted profile — an empty list or
// a non-HTTPS or host-less first entry — rather than panicking on an index.
func firstCoordinatorURL(p siteclient.Profile) (string, error) {
	if len(p.CoordinatorURLs) == 0 {
		return "", errors.New("site profile has no coordinator URL")
	}
	raw := p.CoordinatorURLs[0]
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" {
		return "", fmt.Errorf("site profile coordinator URL %q is not a usable https origin", raw)
	}
	return raw, nil
}

// stateProfile and profile convert between the persisted and wire profiles.
func stateProfile(p siteclient.Profile) *state.SiteProfile {
	return &state.SiteProfile{
		SiteID:                p.SiteID,
		CoordinatorURLs:       append([]string(nil), p.CoordinatorURLs...),
		CoordinatorSPKISHA256: append([]string(nil), p.CoordinatorSPKISHA256...),
		MDNSService:           p.MDNSService,
	}
}

func siteProfileFrom(sp *state.SiteProfile) siteclient.Profile {
	return siteclient.Profile{
		SiteID:                sp.SiteID,
		CoordinatorURLs:       append([]string(nil), sp.CoordinatorURLs...),
		CoordinatorSPKISHA256: append([]string(nil), sp.CoordinatorSPKISHA256...),
		MDNSService:           sp.MDNSService,
	}
}
