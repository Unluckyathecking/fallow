// Package discovery resolves optional LAN Site Mode coordinator candidates over
// mDNS.
//
// Multicast is a hint, never a source of trust. The resolver learns nothing from
// the wire but an address and a port: it filters answers to the site the profile
// already names, produces a deterministic list of https origins, and hands them
// to the caller's pinned client, which decides whether any of them is the
// coordinator. There is no trust on first use, no token in TXT and no pin
// learned or changed here. A candidate that fails the stored SPKI pin is simply
// not the coordinator.
//
// Discovery is a fallback, not a path. Static coordinator URLs remain first and
// sufficient; a caller queries only when those are unreachable and the profile
// carries mdns_service. One bounded query runs per call: no browse loop, no
// background goroutine, and no fallback to plain HTTP, public DNS or a subnet
// scan when multicast is lost, which on a school VLAN is normal rather than
// exceptional.
package discovery

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
)

// ServiceName is the only mdns_service value this resolver serves, matching the
// value the join schema admits. Anything else is refused rather than queried.
const ServiceName = "_fallow._tcp.local."

const (
	queryService = "_fallow._tcp"
	queryDomain  = "local."

	// DefaultTimeout bounds one query. It is long enough for a responder on the
	// same VLAN to answer and short enough that a machine with no coordinator on
	// the wire gives up promptly and keeps its static profile.
	DefaultTimeout = 2 * time.Second

	// MaxCandidates bounds how many origins one query may yield, so a hostile or
	// broken responder flooding the segment cannot turn a fallback into a long
	// sequence of dials.
	MaxCandidates = 8

	// siteTXTKey is the TXT key carrying the advertised site identifier. It is a
	// public routing label used only to discard answers for other sites; it is
	// not a credential and confers no trust.
	siteTXTKey = "site="
)

var (
	// ErrNotConfigured reports that the profile did not opt into mDNS. The caller
	// keeps its static profile.
	ErrNotConfigured = errors.New("discovery: site profile has no mdns_service")
	// ErrUnsupportedService reports an mdns_service this resolver will not query.
	ErrUnsupportedService = errors.New("discovery: unsupported mdns_service")
	// ErrNoSiteID reports a profile with nothing to filter answers against.
	// Querying without it would accept any responder on the segment.
	ErrNoSiteID = errors.New("discovery: site profile has no site_id")
)

// QueryError reports that the multicast query itself could not run — typically a
// machine that refuses the multicast socket bind. It is a diagnostic: the static
// profile is untouched.
type QueryError struct{ Err error }

func (e *QueryError) Error() string { return "discovery: mdns query failed: " + e.Err.Error() }
func (e *QueryError) Unwrap() error { return e.Err }

// NoCandidateError reports that the bounded query elapsed without producing a
// usable candidate for this site, either because nothing answered or because
// every answer was for another site or malformed. It is a diagnostic, not a
// failure of the agent: the caller keeps its static profile unchanged.
type NoCandidateError struct {
	Service string
	SiteID  string
	Timeout time.Duration
	// Seen counts answers observed and discarded, separating "the segment was
	// silent" from "something answered but not for this site".
	Seen int
}

func (e *NoCandidateError) Error() string {
	return fmt.Sprintf(
		"discovery: no %s candidate for site %q within %s (%d answers discarded)",
		e.Service, e.SiteID, e.Timeout, e.Seen,
	)
}

// Query is one bounded mDNS lookup request.
type Query struct {
	Service string
	Domain  string
	Timeout time.Duration
}

// Entry is one service answer, reduced to the fields this resolver reads. It is
// deliberately independent of the mDNS library so the filtering rules are
// testable without a multicast socket.
type Entry struct {
	Name   string
	AddrV4 net.IP
	AddrV6 net.IP
	Port   int
	TXT    []string
}

// Lookup performs one bounded query and returns every answer observed before the
// bound elapsed. It must return when the query's timeout elapses or ctx is done,
// whichever comes first, and must not outlive the call. Production wraps
// hashicorp/mdns; tests inject a fake.
type Lookup func(ctx context.Context, q Query) ([]Entry, error)

// Resolver implements siteclient.Discovery over mDNS.
type Resolver struct {
	// Lookup is the injected query seam. Nil takes the multicast default.
	Lookup Lookup
	// Timeout bounds one query. Zero takes DefaultTimeout.
	Timeout time.Duration
}

var _ siteclient.Discovery = Resolver{}

// Candidates performs one bounded query for the profile's service, keeps only
// answers advertising the profile's site, and returns deterministically ordered
// https origins. Every returned origin is a candidate only: the caller's pinned
// client is what decides whether one of them is the coordinator, and no secret
// may be sent to any of them before that check passes.
func (r Resolver) Candidates(ctx context.Context, p siteclient.Profile) ([]string, error) {
	if p.MDNSService == nil {
		return nil, ErrNotConfigured
	}
	if *p.MDNSService != ServiceName {
		return nil, fmt.Errorf("%w: %q", ErrUnsupportedService, *p.MDNSService)
	}
	if strings.TrimSpace(p.SiteID) == "" {
		return nil, ErrNoSiteID
	}
	timeout := r.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	lookup := r.Lookup
	if lookup == nil {
		lookup = multicastLookup
	}

	entries, err := lookup(ctx, Query{Service: queryService, Domain: queryDomain, Timeout: timeout})
	if err != nil {
		return nil, &QueryError{Err: err}
	}
	origins := selectOrigins(entries, p.SiteID)
	if len(origins) == 0 {
		return nil, &NoCandidateError{Service: ServiceName, SiteID: p.SiteID, Timeout: timeout, Seen: len(entries)}
	}
	return origins, nil
}

// endpoint is one accepted address and port, kept in structured form so the
// ordering is over addresses rather than over the spelling of a URL.
type endpoint struct {
	ip   net.IP
	port int
}

// selectOrigins filters answers to siteID, deduplicates them, orders them
// deterministically and caps the result. Ordering is IPv4 before IPv6, then by
// address bytes, then by port, so two agents on the same segment dial the same
// candidate first and a rerun of the same query produces the same list.
func selectOrigins(entries []Entry, siteID string) []string {
	seen := map[string]bool{}
	var eps []endpoint
	for _, e := range entries {
		if !servesSite(e, siteID) || e.Port < 1 || e.Port > 65535 {
			continue
		}
		for _, ip := range []net.IP{e.AddrV4, e.AddrV6} {
			if !usableAddress(ip) {
				continue
			}
			key := net.JoinHostPort(ip.String(), fmt.Sprint(e.Port))
			if seen[key] {
				continue
			}
			seen[key] = true
			eps = append(eps, endpoint{ip: ip, port: e.Port})
		}
	}
	sort.Slice(eps, func(i, j int) bool { return less(eps[i], eps[j]) })
	if len(eps) > MaxCandidates {
		eps = eps[:MaxCandidates]
	}
	out := make([]string, 0, len(eps))
	for _, ep := range eps {
		u := url.URL{Scheme: "https", Host: net.JoinHostPort(ep.ip.String(), fmt.Sprint(ep.port))}
		out = append(out, u.String())
	}
	return out
}

func less(a, b endpoint) bool {
	if av, bv := a.ip.To4() != nil, b.ip.To4() != nil; av != bv {
		return av
	}
	if n := bytes.Compare(a.ip.To16(), b.ip.To16()); n != 0 {
		return n < 0
	}
	return a.port < b.port
}

// servesSite reports whether an answer belongs to this service and advertises
// exactly this site. An answer outside the service, carrying no site key, or
// carrying more than one site value is discarded: an ambiguous advertisement is
// not something to guess at.
func servesSite(e Entry, siteID string) bool {
	if !strings.HasSuffix(strings.ToLower(e.Name), "."+ServiceName) {
		return false
	}
	var values []string
	for _, f := range e.TXT {
		if v, ok := strings.CutPrefix(f, siteTXTKey); ok {
			values = append(values, v)
		}
	}
	return len(values) == 1 && values[0] == siteID
}

// usableAddress rejects addresses that cannot name a coordinator on this
// segment: an absent or malformed address, the unspecified address, a multicast
// address, and IPv6 link-local, which needs a zone this resolver does not carry
// through. Loopback is kept so a coordinator on the same machine is reachable,
// and IPv4 link-local is kept because a VLAN without DHCP still self-assigns.
func usableAddress(ip net.IP) bool {
	if ip == nil || ip.To16() == nil || ip.IsUnspecified() || ip.IsMulticast() {
		return false
	}
	return !(ip.To4() == nil && ip.IsLinkLocalUnicast())
}
