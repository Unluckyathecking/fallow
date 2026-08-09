package discovery

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
)

const testSite = "site-alpha"

func service(v string) *string { return &v }

// profile builds the minimal Site Mode profile the resolver reads.
func profile() siteclient.Profile {
	return siteclient.Profile{SiteID: testSite, MDNSService: service(ServiceName)}
}

// entry builds a well-formed answer for testSite that individual cases mutate.
func entry(name string, v4 string, port int) Entry {
	return Entry{Name: name, AddrV4: net.ParseIP(v4), Port: port, TXT: []string{"site_id=" + testSite}}
}

const instance = "coordinator._fallow._tcp.local."

// fixedLookup returns a Lookup seam that answers with entries and records the
// query it was handed.
func fixedLookup(entries []Entry, got *Query) Lookup {
	return func(_ context.Context, q Query) ([]Entry, error) {
		if got != nil {
			*got = q
		}
		return entries, nil
	}
}

func resolve(t *testing.T, entries []Entry) ([]string, error) {
	t.Helper()
	return Resolver{Lookup: fixedLookup(entries, nil)}.Candidates(context.Background(), profile())
}

// TestCandidateSelection covers what one query may and may not turn into a
// candidate: duplicates, malformed answers, hostile answers, and the IPv4/IPv6
// ordering. Every case runs through the public Candidates path.
func TestCandidateSelection(t *testing.T) {
	v6 := func(e Entry, addr string) Entry { e.AddrV6 = net.ParseIP(addr); return e }

	cases := []struct {
		name    string
		entries []Entry
		want    []string
	}{
		{
			name:    "single answer",
			entries: []Entry{entry(instance, "192.0.2.10", 8443)},
			want:    []string{"https://192.0.2.10:8443"},
		},
		{
			name: "duplicate answers collapse",
			entries: []Entry{
				entry(instance, "192.0.2.10", 8443),
				entry("other._fallow._tcp.local.", "192.0.2.10", 8443),
				entry(instance, "192.0.2.10", 8443),
			},
			want: []string{"https://192.0.2.10:8443"},
		},
		{
			name:    "one answer carrying both families yields both",
			entries: []Entry{v6(entry(instance, "192.0.2.10", 8443), "2001:db8::1")},
			want:    []string{"https://192.0.2.10:8443", "https://[2001:db8::1]:8443"},
		},
		{
			name: "ipv4 sorts before ipv6 and addresses sort numerically",
			entries: []Entry{
				v6(entry(instance, "192.0.2.20", 8443), "2001:db8::2"),
				v6(entry(instance, "192.0.2.9", 8443), "2001:db8::1"),
			},
			want: []string{
				"https://192.0.2.9:8443", "https://192.0.2.20:8443",
				"https://[2001:db8::1]:8443", "https://[2001:db8::2]:8443",
			},
		},
		{
			name: "same address on two ports sorts by port",
			entries: []Entry{
				entry(instance, "192.0.2.10", 9000),
				entry(instance, "192.0.2.10", 8443),
			},
			want: []string{"https://192.0.2.10:8443", "https://192.0.2.10:9000"},
		},
		{
			name:    "ipv6 unique-local is usable",
			entries: []Entry{{Name: instance, AddrV6: net.ParseIP("fd00::1"), Port: 8443, TXT: []string{"site_id=" + testSite}}},
			want:    []string{"https://[fd00::1]:8443"},
		},
		{
			name:    "loopback is usable so a same-machine coordinator is reachable",
			entries: []Entry{entry(instance, "127.0.0.1", 8443)},
			want:    []string{"https://127.0.0.1:8443"},
		},
		{
			// The advertisement the coordinator actually publishes (ADR 090): the
			// site id alongside a version key this resolver does not gate on.
			name: "the published advertisement is accepted whole",
			entries: []Entry{{
				Name: instance, AddrV4: net.ParseIP("192.0.2.10"), Port: 8443,
				TXT: []string{"version=1", "site_id=" + testSite},
			}},
			want: []string{"https://192.0.2.10:8443"},
		},
		{
			// Unknown keys, and a version this build has never heard of, are
			// tolerated: the certificate pin settles what an answer is worth, not
			// what the answer claims about itself.
			name: "unknown txt keys and versions are tolerated",
			entries: []Entry{{
				Name: instance, AddrV4: net.ParseIP("192.0.2.10"), Port: 8443,
				TXT: []string{"version=99", "site_id=" + testSite, "region=north", "bare"},
			}},
			want: []string{"https://192.0.2.10:8443"},
		},
		{
			// A name clash renames an advertiser to "<label>-2", so the instance
			// label is not stable and identity comes from the TXT site id.
			name:    "a renamed instance is still this site",
			entries: []Entry{entry("coordinator-2._fallow._tcp.local.", "192.0.2.10", 8443)},
			want:    []string{"https://192.0.2.10:8443"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := resolve(t, c.entries)
			if err != nil {
				t.Fatalf("Candidates: %v", err)
			}
			if strings.Join(got, ",") != strings.Join(c.want, ",") {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
}

// TestMalformedAndHostileAnswersAreDiscarded covers answers that must never
// become a candidate. Each case is the only answer on the wire, so a resolver
// that accepted it would return it.
func TestMalformedAndHostileAnswersAreDiscarded(t *testing.T) {
	bad := func(mutate func(*Entry)) Entry {
		e := entry(instance, "192.0.2.10", 8443)
		mutate(&e)
		return e
	}
	cases := []struct {
		name  string
		entry Entry
	}{
		{"port zero", bad(func(e *Entry) { e.Port = 0 })},
		{"port above range", bad(func(e *Entry) { e.Port = 70000 })},
		{"negative port", bad(func(e *Entry) { e.Port = -1 })},
		{"no address", bad(func(e *Entry) { e.AddrV4 = nil })},
		{"malformed address", bad(func(e *Entry) { e.AddrV4 = net.IP{1, 2, 3} })},
		{"unspecified address", bad(func(e *Entry) { e.AddrV4 = net.ParseIP("0.0.0.0") })},
		{"multicast address", bad(func(e *Entry) { e.AddrV4 = net.ParseIP("224.0.0.251") })},
		{"ipv6 unspecified", bad(func(e *Entry) { e.AddrV4 = net.ParseIP("::") })},
		{"ipv6 link-local has no zone to dial", bad(func(e *Entry) { e.AddrV4 = net.ParseIP("fe80::1") })},
		{"answer outside the service", bad(func(e *Entry) { e.Name = "coordinator._other._tcp.local." })},
		{"service name without an instance label", bad(func(e *Entry) { e.Name = ServiceName })},
		{"no txt at all", bad(func(e *Entry) { e.TXT = nil })},
		{"txt for another site", bad(func(e *Entry) { e.TXT = []string{"site_id=site-beta"} })},
		{"site value differing only in case", bad(func(e *Entry) { e.TXT = []string{"site_id=SITE-ALPHA"} })},
		{"site key prefix lookalike", bad(func(e *Entry) { e.TXT = []string{"site_identifier=" + testSite} })},
		{"two conflicting site values", bad(func(e *Entry) { e.TXT = []string{"site_id=" + testSite, "site_id=site-beta"} })},
		{"only the superseded site key", bad(func(e *Entry) { e.TXT = []string{"site=" + testSite} })},
		{"the same site value twice is still ambiguous", bad(func(e *Entry) {
			e.TXT = []string{"site_id=" + testSite, "site_id=" + testSite}
		})},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := resolve(t, []Entry{c.entry})
			if got != nil {
				t.Fatalf("accepted a discardable answer: %v", got)
			}
			var none *NoCandidateError
			if !errors.As(err, &none) {
				t.Fatalf("want NoCandidateError, got %v", err)
			}
			if none.Seen != 1 {
				t.Fatalf("Seen=%d, want 1 discarded answer reported", none.Seen)
			}
		})
	}
}

// TestSiteIDBeyondWhatADNSLabelCarries proves the match is on the TXT value and
// not on the instance label. A site id is free text up to 128 characters and the
// label is a folded, truncated derivative of it, so a resolver reading identity
// off the name would miss its own coordinator.
func TestSiteIDBeyondWhatADNSLabelCarries(t *testing.T) {
	longSite := strings.Repeat("a", 100) + " Building 4 (annexe)"
	p := siteclient.Profile{SiteID: longSite, MDNSService: service(ServiceName)}
	answers := []Entry{{
		Name:   strings.Repeat("a", 22) + "._fallow._tcp.local.", // folded and truncated
		TXT:    []string{"version=1", "site_id=" + longSite},
		AddrV4: net.ParseIP("192.0.2.10"),
		Port:   8443,
	}}
	got, err := Resolver{Lookup: fixedLookup(answers, nil)}.Candidates(context.Background(), p)
	if err != nil || len(got) != 1 || got[0] != "https://192.0.2.10:8443" {
		t.Fatalf("got %v, %v; want the answer matched on its TXT site id", got, err)
	}
}

// TestFloodIsCapped proves a responder shouting many well-formed answers cannot
// turn a bounded fallback into a long sequence of dials.
func TestFloodIsCapped(t *testing.T) {
	var entries []Entry
	for i := 0; i < MaxCandidates*4; i++ {
		entries = append(entries, entry(instance, fmt.Sprintf("192.0.2.%d", i+1), 8443))
	}
	got, err := resolve(t, entries)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != MaxCandidates {
		t.Fatalf("got %d candidates, want the cap of %d", len(got), MaxCandidates)
	}
	// The cap keeps the lowest addresses, so the list stays deterministic rather
	// than reflecting arrival order.
	if got[0] != "https://192.0.2.1:8443" {
		t.Fatalf("cap changed the ordering: %v", got[0])
	}
}

// TestOrderingIsStableAcrossArrivalOrder proves two agents on one segment build
// the same list even when answers arrive in different orders.
func TestOrderingIsStableAcrossArrivalOrder(t *testing.T) {
	a := []Entry{entry(instance, "192.0.2.3", 8443), entry(instance, "192.0.2.1", 8443), entry(instance, "192.0.2.2", 8443)}
	b := []Entry{a[1], a[2], a[0]}
	first, err := resolve(t, a)
	if err != nil {
		t.Fatal(err)
	}
	second, err := resolve(t, b)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(first, ",") != strings.Join(second, ",") {
		t.Fatalf("ordering depends on arrival: %v vs %v", first, second)
	}
}

// TestDisabledAndUnsupportedProfiles proves the resolver never queries for a
// profile that did not opt in or that names a service it will not speak.
func TestDisabledAndUnsupportedProfiles(t *testing.T) {
	queried := false
	lookup := Lookup(func(context.Context, Query) ([]Entry, error) {
		queried = true
		return nil, nil
	})
	cases := []struct {
		name    string
		profile siteclient.Profile
		want    error
	}{
		{"no mdns_service", siteclient.Profile{SiteID: testSite}, ErrNotConfigured},
		{"another service", siteclient.Profile{SiteID: testSite, MDNSService: service("_other._tcp.local.")}, ErrUnsupportedService},
		{"no site_id", siteclient.Profile{MDNSService: service(ServiceName)}, ErrNoSiteID},
		{"blank site_id", siteclient.Profile{SiteID: "  ", MDNSService: service(ServiceName)}, ErrNoSiteID},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			queried = false
			got, err := Resolver{Lookup: lookup}.Candidates(context.Background(), c.profile)
			if got != nil {
				t.Fatalf("returned candidates: %v", got)
			}
			if !errors.Is(err, c.want) {
				t.Fatalf("got %v, want %v", err, c.want)
			}
			if queried {
				t.Fatal("queried the network for a profile that should not be queried")
			}
		})
	}
}

// TestTimeoutReturnsTypedDiagnostic covers the normal school-VLAN outcome: the
// bounded query elapses with nothing on the wire. The caller must be able to
// classify it and keep its static profile.
func TestTimeoutReturnsTypedDiagnostic(t *testing.T) {
	r := Resolver{Timeout: 25 * time.Millisecond, Lookup: func(ctx context.Context, q Query) ([]Entry, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(q.Timeout):
			return nil, nil // the bound elapsed; nothing answered
		}
	}}
	got, err := r.Candidates(context.Background(), profile())
	if got != nil {
		t.Fatalf("returned candidates: %v", got)
	}
	var none *NoCandidateError
	if !errors.As(err, &none) {
		t.Fatalf("want NoCandidateError, got %T %v", err, err)
	}
	if none.Seen != 0 || none.SiteID != testSite || none.Timeout != 25*time.Millisecond {
		t.Fatalf("diagnostic lost its context: %+v", none)
	}
	if !strings.Contains(none.Error(), testSite) {
		t.Fatalf("diagnostic does not name the site: %s", none)
	}
}

// TestQueryFailureIsTyped covers a machine that refuses the multicast socket:
// distinguishable from "nothing answered", and still not fatal.
func TestQueryFailureIsTyped(t *testing.T) {
	sentinel := errors.New("bind: operation not permitted")
	r := Resolver{Lookup: func(context.Context, Query) ([]Entry, error) { return nil, sentinel }}
	_, err := r.Candidates(context.Background(), profile())
	var qe *QueryError
	if !errors.As(err, &qe) || !errors.Is(err, sentinel) {
		t.Fatalf("want QueryError wrapping the cause, got %T %v", err, err)
	}
}

// TestCancellationReachesTheLookup proves a shutdown stops the query rather than
// waiting out its bound.
func TestCancellationReachesTheLookup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r := Resolver{Lookup: func(ctx context.Context, _ Query) ([]Entry, error) { return nil, ctx.Err() }}
	_, err := r.Candidates(ctx, profile())
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}

// TestQueryShapeAndDefaultTimeout pins the one query this resolver ever sends.
func TestQueryShapeAndDefaultTimeout(t *testing.T) {
	var got Query
	entries := []Entry{entry(instance, "192.0.2.10", 8443)}
	if _, err := (Resolver{Lookup: fixedLookup(entries, &got)}).Candidates(context.Background(), profile()); err != nil {
		t.Fatal(err)
	}
	if got.Service != "_fallow._tcp" || got.Domain != "local." {
		t.Fatalf("unexpected query target: %+v", got)
	}
	if got.Timeout != DefaultTimeout {
		t.Fatalf("timeout=%s, want the default %s", got.Timeout, DefaultTimeout)
	}
	want := ServiceName
	if strings.TrimSuffix(got.Service, ".")+"."+got.Domain != want {
		t.Fatalf("query does not spell %s", want)
	}
}
