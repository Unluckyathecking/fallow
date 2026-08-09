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
	"io"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"

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

// Site Mode reconnect backoff bounds for the supervised claim runner. A transient
// relay/transport error (a coordinator that dropped its socket) restarts polling
// after a bounded, context-cancellable wait so a coordinator down/up cycle
// resumes held polling without a hot loop.
const (
	claimRunnerBackoffInitial = 200 * time.Millisecond
	claimRunnerBackoffMax     = 5 * time.Second
	claimRunnerHealthyRun     = 10 * time.Second
)

// superviseClaimRunner runs the claim runner and restarts it after a bounded,
// exponential, context-cancellable backoff whenever it returns a transient error,
// so the additive serving path survives a coordinator restart. Context
// cancellation is terminal: on shutdown the loop returns at once. A genuine auth
// or config failure is not special-cased here — the heartbeat loop shares the
// device token, fails closed on an auth rejection, and cancels the run context,
// which this loop observes as terminal. run is injected so the supervision policy
// is unit-testable without the full availability/replica wiring.
func superviseClaimRunner(ctx context.Context, run func(context.Context) error) {
	backoff := claimRunnerBackoffInitial
	for {
		start := time.Now()
		err := run(ctx)
		if ctx.Err() != nil {
			return
		}
		if err == nil {
			// A clean return without cancellation is not expected from the runner,
			// but treat it as terminal rather than spinning.
			return
		}
		// A run that stayed up a while before failing means the channel was
		// healthy; reset the backoff so an isolated blip does not inflate it.
		if time.Since(start) >= claimRunnerHealthyRun {
			backoff = claimRunnerBackoffInitial
		}
		logf("claim runner error, resuming polling after %s: %v", backoff, err)
		if !sleepCtx(ctx, backoff) {
			return
		}
		if backoff < claimRunnerBackoffMax {
			backoff *= 2
			if backoff > claimRunnerBackoffMax {
				backoff = claimRunnerBackoffMax
			}
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
	profile, id, cfg, dial, err := r.siteIdentity(ctx, existing)
	if err != nil {
		return wiring{}, err
	}
	// A first run already resolved a coordinator to enroll against; reuse it
	// rather than probing and querying a second time, which would also risk
	// enrolling against one coordinator and then dialing another.
	if dial == nil {
		dial, err = r.dialSite(ctx, profile)
		if err != nil {
			return wiring{}, err
		}
	}
	baseURL, pinned := dial.baseURL, dial.pinned
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
// It returns the dial it used when it enrolled, and nil on the restart path,
// where no coordinator has been contacted yet.
func (r *Runtime) siteIdentity(ctx context.Context, existing *state.Identity) (siteclient.Profile, state.Identity, protocol.AgentConfig, *siteDial, error) {
	if existing != nil {
		if existing.Site == nil {
			return siteclient.Profile{}, state.Identity{}, protocol.AgentConfig{}, nil, errors.New(
				"an existing non-Site identity is present; refusing to convert it to Site Mode silently",
			)
		}
		return siteProfileFrom(existing.Site), *existing, defaultAgentConfig(), nil, nil
	}
	return r.enrollSite(ctx)
}

// siteDial is a resolved Site Mode connection: the pinned client and the
// coordinator origin chosen for it. The two travel together because the origin
// was chosen by probing through that very client, so pairing them elsewhere
// would silently drop the guarantee that the origin already answered on a
// stored pin.
type siteDial struct {
	pinned  *http.Client
	baseURL string
}

// dialSite builds the pinned client for a profile and chooses the coordinator
// origin to dial through it.
func (r *Runtime) dialSite(ctx context.Context, profile siteclient.Profile) (*siteDial, error) {
	pinned, err := r.seams.NewPinnedClient(profile)
	if err != nil {
		return nil, fmt.Errorf("build pinned client: %w", err)
	}
	baseURL, err := r.siteBaseURL(ctx, profile, pinned)
	if err != nil {
		return nil, err
	}
	return &siteDial{pinned: pinned, baseURL: baseURL}, nil
}

// enrollSite performs the one-time first-run enrollment: parse the join file,
// verify the pin, register once, persist the identity and token-free profile,
// then remove the installed enrollment token from disk.
func (r *Runtime) enrollSite(ctx context.Context) (siteclient.Profile, state.Identity, protocol.AgentConfig, *siteDial, error) {
	var zeroP siteclient.Profile
	data, err := os.ReadFile(r.settings.SiteJoinBundle)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, fmt.Errorf("read join file %s: %w", r.settings.SiteJoinBundle, err)
	}
	bundle, err := siteclient.ParseJoin(data)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, fmt.Errorf("parse join file: %w", err)
	}
	profile := bundle.Profile()

	dial, err := r.dialSite(ctx, profile)
	if err != nil {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, err
	}
	enroller := r.seams.NewSiteCoordinator(dial.baseURL, "", "", dial.pinned)
	resp, err := enroller.Register(ctx, protocol.RegisterRequest{
		EnrollmentToken: bundle.EnrollmentToken,
		ProtocolVersion: protocolVersion,
		Caps:            makeCaps(),
	})
	if err != nil {
		// Register is never retried; an ambiguous outcome asks for a new token.
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, fmt.Errorf("site enrollment failed (not retried): %w", err)
	}
	if resp.AgentID == "" || resp.DeviceToken == "" {
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, errors.New(
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
		return zeroP, state.Identity{}, protocol.AgentConfig{}, nil, fmt.Errorf("persist site identity: %w", err)
	}
	// The identity is durable; remove the installed enrollment token.
	if err := os.Remove(r.settings.SiteJoinBundle); err != nil && !os.IsNotExist(err) {
		logf("warning: could not remove join file %s after enrollment: %v", r.settings.SiteJoinBundle, err)
	}
	logf("site enrolled (agent_id=%s, site_id=%s)", id.AgentID, profile.SiteID)
	return profile, id, resp.Config, dial, nil
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

// siteProbeTimeout bounds one reachability probe, so a profile listing several
// dead origins cannot stall startup for long before the fallback opens.
const siteProbeTimeout = 3 * time.Second

// siteBaseURL chooses the coordinator origin to dial.
//
// Static URLs stay first and sufficient. A profile that did not opt into mDNS
// takes its first static origin with no extra network call at all, exactly as
// before: the fallback cannot change the behaviour of an agent that has nothing
// to fall back to. Only a profile carrying mdns_service probes its static
// origins, and only their unreachability opens a query.
//
// Discovery never widens trust: a candidate is dialed through the same pinned
// client as a static URL, so one that cannot present a stored SPKI pin is
// skipped and the pin set is untouched. The probe itself carries no credential —
// it is an unauthenticated GET whose only question is whether a pinned peer
// answers. Discovery also never narrows availability: a query that times out,
// fails or yields nothing usable leaves the static profile in place rather than
// failing startup, so a silent segment costs a bounded delay and nothing else.
func (r *Runtime) siteBaseURL(ctx context.Context, p siteclient.Profile, pinned *http.Client) (string, error) {
	static, err := firstCoordinatorURL(p)
	if err != nil {
		return "", err
	}
	if p.MDNSService == nil {
		return static, nil
	}
	if origin := firstReachable(ctx, pinned, p.CoordinatorURLs); origin != "" {
		return origin, nil
	}
	logf("site static coordinators are unreachable; querying %s", *p.MDNSService)
	candidates, err := r.seams.Discovery.Candidates(ctx, p)
	if err != nil {
		logf("site discovery found no candidate, keeping the static profile: %v", err)
		return static, nil
	}
	if origin := firstReachable(ctx, pinned, candidates); origin != "" {
		logf("site discovery selected coordinator %s", origin)
		return origin, nil
	}
	logf("no discovered candidate answered on a pinned certificate; keeping the static profile")
	return static, nil
}

// firstReachable returns the first origin that answers over the pinned client,
// or "" when none does. Order is significant and preserved: the caller's
// preference decides, not whichever host happens to answer first.
func firstReachable(ctx context.Context, pinned *http.Client, origins []string) string {
	for _, origin := range origins {
		if u, err := url.Parse(origin); err != nil || u.Scheme != "https" || u.Hostname() == "" {
			continue
		}
		if err := probeOrigin(ctx, pinned, origin); err != nil {
			logf("site coordinator %s is not usable: %v", origin, err)
			continue
		}
		return origin
	}
	return ""
}

// probeOrigin asks whether a pinned peer answers at origin. It sends no
// credential and reads no meaning from the response: any status at all proves
// the host is up and its certificate matched a stored pin, which is the whole
// question. A little of the body is drained so the pinned connection can be
// reused by the enrollment or heartbeat call that follows.
func probeOrigin(ctx context.Context, pinned *http.Client, origin string) error {
	ctx, cancel := context.WithTimeout(ctx, siteProbeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, origin, nil)
	if err != nil {
		return err
	}
	resp, err := pinned.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, probeDrainLimit))
	return nil
}

// probeDrainLimit caps how much of a probe response is read before the body is
// closed, so a large or hostile response cannot be pulled into memory.
const probeDrainLimit = 4 << 10

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
