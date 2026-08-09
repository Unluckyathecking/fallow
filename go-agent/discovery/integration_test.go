package discovery

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/siteclient"
	"github.com/hashicorp/mdns"
)

// TestDiscoveredCandidateMustPassThePin is the trust test: a candidate learned
// from multicast is dialed by the existing pinned client, and a responder
// presenting the wrong certificate is refused before any request is written.
// Discovery supplies an address; it never supplies trust.
func TestDiscoveredCandidateMustPassThePin(t *testing.T) {
	var requests atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	host, port := hostPort(t, srv.URL)
	answers := []Entry{{
		Name:   instance,
		AddrV4: net.ParseIP(host),
		Port:   port,
		TXT:    []string{"site_id=" + testSite},
	}}

	wrongPin := "sha256/" + base64.StdEncoding.EncodeToString(make([]byte, sha256.Size))
	p := profile()
	p.CoordinatorSPKISHA256 = []string{wrongPin}

	got, err := (Resolver{Lookup: fixedLookup(answers, nil)}).Candidates(context.Background(), p)
	if err != nil {
		t.Fatalf("Candidates: %v", err)
	}
	want := "https://" + net.JoinHostPort(host, strconv.Itoa(port))
	if len(got) != 1 || got[0] != want {
		t.Fatalf("got %v, want [%s]", got, want)
	}

	client, err := siteclient.NewPinnedClient(p)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := client.Get(got[0])
	if err == nil {
		resp.Body.Close()
		t.Fatal("wrong certificate was accepted")
	}
	var pinErr *siteclient.PinError
	if !errors.As(err, &pinErr) {
		t.Fatalf("want a pin error, got %T %v", err, err)
	}
	if requests.Load() != 0 {
		t.Fatal("a request reached the wrong responder")
	}
	// The mismatch skips the candidate and leaves the pin set exactly as stored;
	// nothing about the certificate on the wire is learned.
	if len(p.CoordinatorSPKISHA256) != 1 || p.CoordinatorSPKISHA256[0] != wrongPin {
		t.Fatalf("pin set changed: %v", p.CoordinatorSPKISHA256)
	}

	// The same discovered candidate is reached once its certificate matches the
	// stored pin, so the skip above is the pin's decision and not the address's.
	sum := sha256.Sum256(srv.Certificate().RawSubjectPublicKeyInfo)
	p.CoordinatorSPKISHA256 = []string{"sha256/" + base64.StdEncoding.EncodeToString(sum[:])}
	client, err = siteclient.NewPinnedClient(p)
	if err != nil {
		t.Fatal(err)
	}
	resp, err = client.Get(got[0])
	if err != nil {
		t.Fatalf("matching pin was refused: %v", err)
	}
	resp.Body.Close()
	if requests.Load() != 1 {
		t.Fatalf("requests=%d, want exactly the pinned one", requests.Load())
	}
}

// TestMulticastLookupAgainstLocalResponder drives the production lookup against
// a real responder on this machine, so the hashicorp/mdns wiring — service name,
// domain, TXT and address decoding — is exercised rather than assumed. Multicast
// is not available in every sandbox, so an environment that cannot bind or
// cannot hear its own responder skips instead of failing.
func TestMulticastLookupAgainstLocalResponder(t *testing.T) {
	svc, err := mdns.NewMDNSService(
		"fallow-test", queryService, queryDomain, "fallow-test.local.",
		8443, []net.IP{net.ParseIP("127.0.0.1")}, []string{"site_id=" + testSite},
	)
	if err != nil {
		t.Fatalf("build responder: %v", err)
	}
	srv, err := mdns.NewServer(&mdns.Config{Zone: svc, Logger: quietLogger})
	if err != nil {
		t.Skipf("multicast responder unavailable here: %v", err)
	}
	defer func() { _ = srv.Shutdown() }()

	entries, err := multicastLookup(context.Background(), Query{
		Service: queryService, Domain: queryDomain, Timeout: 2 * time.Second,
	})
	if err != nil {
		t.Skipf("multicast query unavailable here: %v", err)
	}
	got := selectOrigins(entries, testSite)
	if len(got) == 0 {
		t.Skip("no multicast answer reached this process")
	}
	if got[0] != "https://127.0.0.1:8443" {
		t.Fatalf("got %v, want the local responder", got)
	}
}

// TestMulticastLookupLeavesNoGoroutine proves one query is one-shot: the lookup
// tears its sockets down before returning, so a fallback that runs on every
// unreachable static profile cannot accumulate background listeners.
func TestMulticastLookupLeavesNoGoroutine(t *testing.T) {
	run := func() {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		_, _ = multicastLookup(ctx, Query{Service: queryService, Domain: queryDomain, Timeout: 100 * time.Millisecond})
	}
	run() // warm up lazily started runtime goroutines before the baseline
	settle()
	baseline := runtime.NumGoroutine()
	for i := 0; i < 3; i++ {
		run()
	}
	if got := settledGoroutines(baseline); got > baseline+2 {
		t.Fatalf("goroutine leak: baseline=%d after=%d", baseline, got)
	}
}

// TestMulticastLookupSurvivesCancellation is the regression guard for the pinned
// library's teardown race: handed a cancellable context directly, v1.0.6 runs two
// concurrent closes and the detector fires. The lookup must stay clean when a
// query is cancelled while it is in flight, which is what a daemon shutdown does.
func TestMulticastLookupSurvivesCancellation(t *testing.T) {
	for i := 0; i < 5; i++ {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() {
			defer close(done)
			_, _ = multicastLookup(ctx, Query{Service: queryService, Domain: queryDomain, Timeout: 150 * time.Millisecond})
		}()
		time.Sleep(20 * time.Millisecond) // cancel while the query is listening
		cancel()
		<-done
	}
}

// TestQueryOnceHonoursAnEarlierDeadline proves the caller's deadline shortens the
// query rather than being ignored, so a bounded caller stays bounded even though
// the library is not handed the cancellation itself.
func TestQueryOnceHonoursAnEarlierDeadline(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, _ = queryOnce(ctx, Query{Service: queryService, Domain: queryDomain, Timeout: 10 * time.Second}, true)
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("query ran %s, want it clamped to the caller's deadline", elapsed)
	}

	// An already-expired context does not query at all.
	expired, cancelExpired := context.WithCancel(context.Background())
	cancelExpired()
	if _, err := queryOnce(expired, Query{Service: queryService, Domain: queryDomain, Timeout: time.Second}, true); !errors.Is(err, context.Canceled) {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}

func settle() {
	for i := 0; i < 5; i++ {
		runtime.GC()
		time.Sleep(20 * time.Millisecond)
	}
}

func settledGoroutines(baseline int) int {
	got := runtime.NumGoroutine()
	for i := 0; i < 50 && got > baseline+2; i++ {
		time.Sleep(20 * time.Millisecond)
		runtime.GC()
		got = runtime.NumGoroutine()
	}
	return got
}

func hostPort(t *testing.T, rawURL string) (string, int) {
	t.Helper()
	host, portStr, err := net.SplitHostPort(strings.TrimPrefix(rawURL, "https://"))
	if err != nil {
		t.Fatalf("split %s: %v", rawURL, err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		t.Fatalf("port %s: %v", portStr, err)
	}
	return host, port
}
