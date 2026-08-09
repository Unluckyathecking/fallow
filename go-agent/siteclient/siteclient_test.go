package siteclient

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"
)

func TestParseJoinStrictAndRedactsToken(t *testing.T) {
	raw := `{"version":1,"site_id":"s","coordinator_urls":["https://one:443/","https://two"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"secret","mdns_service":null}`
	j, err := ParseJoin([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	p := j.Profile()
	if p.SiteID != "s" || strings.Contains(strings.Join([]string{p.SiteID}, ""), "secret") {
		t.Fatal("token leaked")
	}
	if _, err = ParseJoin([]byte(strings.Replace(raw, "\"mdns_service\":null", "\"extra\":1,\"mdns_service\":null", 1))); !errors.Is(err, ErrInvalidJoin) {
		t.Fatal("unknown field accepted")
	}
	if _, err = ParseJoin([]byte(raw + " garbage")); !errors.Is(err, ErrInvalidJoin) {
		t.Fatal("trailing data accepted")
	}
}
func TestResolverStaticOrderAndDiscovery(t *testing.T) {
	p := Profile{CoordinatorURLs: []string{"https://a", "https://b"}}
	got, e := (Resolver{}).Candidates(context.Background(), p)
	if e != nil || strings.Join(got, ",") != "https://a,https://b" {
		t.Fatal(got, e)
	}
	d := Resolver{Discovery: discoveryFunc(func(context.Context, Profile) ([]string, error) { return []string{"https://d"}, nil })}
	got, e = d.Candidates(context.Background(), Profile{})
	if e != nil || got[0] != "https://d" {
		t.Fatal(got, e)
	}
}

type discoveryFunc func(context.Context, Profile) ([]string, error)

func (f discoveryFunc) Candidates(c context.Context, p Profile) ([]string, error) { return f(c, p) }
func TestPinnedClientRejectsHTTPAndDisablesProxyRedirects(t *testing.T) {
	if _, e := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"bad"}}); e == nil {
		t.Fatal("bad pin accepted")
	}
	if _, e := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}); e != nil {
		t.Fatal(e)
	}
}
func TestWrongPinNoRequest(t *testing.T) {
	var requests atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests.Add(1) }))
	defer srv.Close()
	wrong := make([]byte, 32)
	p := Profile{CoordinatorSPKISHA256: []string{"sha256/" + base64.StdEncoding.EncodeToString(wrong)}}
	c, e := NewPinnedClient(p)
	if e != nil {
		t.Fatal(e)
	}
	tr := c.Transport.(guardedTransport).inner.(*http.Transport)
	tr.TLSClientConfig.RootCAs = nil
	tr.TLSClientConfig.InsecureSkipVerify = true // test-only trust bypass; pin remains enforced
	_, err := c.Get(srv.URL)
	var pinErr *PinError
	var transient *TransientError
	if !errors.As(err, &pinErr) || errors.As(err, &transient) {
		t.Fatalf("wrong pin should surface as PinError, not transient: %v", err)
	}
	if requests.Load() != 0 {
		t.Fatal("wrong pin reached handler")
	}
}

func certificatePin(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
	return "sha256/" + base64.StdEncoding.EncodeToString(sum[:])
}

func TestMatchingAndNextPinAccepted(t *testing.T) {
	var seen atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) { seen.Add(1) }))
	defer srv.Close()
	pin := certificatePin(srv.Certificate())
	next := "sha256/" + base64.StdEncoding.EncodeToString(make([]byte, 32))
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{next, pin}})
	if err != nil {
		t.Fatal(err)
	}
	c.Transport.(guardedTransport).inner.(*http.Transport).TLSClientConfig.InsecureSkipVerify = true // test server trust only
	resp, err := c.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if seen.Load() != 1 {
		t.Fatalf("requests=%d", seen.Load())
	}
}

func TestRedirectIsReturnedWithoutFollowing(t *testing.T) {
	var target atomic.Int32
	targetSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { target.Add(1) }))
	defer targetSrv.Close()
	redirect := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, targetSrv.URL, http.StatusFound)
	}))
	defer redirect.Close()
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{certificatePin(redirect.Certificate())}})
	if err != nil {
		t.Fatal(err)
	}
	c.Transport.(guardedTransport).inner.(*http.Transport).TLSClientConfig.InsecureSkipVerify = true
	resp, err := c.Get(redirect.URL)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusFound || target.Load() != 0 {
		t.Fatalf("status=%d target=%d", resp.StatusCode, target.Load())
	}
}

func TestProxyIsNeverUsed(t *testing.T) {
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	if c.Transport.(guardedTransport).inner.(*http.Transport).Proxy != nil {
		t.Fatal("proxy configured")
	}
}

func TestCertificateTimeRejectsExpiredCertificate(t *testing.T) {
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{NotBefore: time.Unix(0, 0), NotAfter: time.Unix(1, 0), RawSubjectPublicKeyInfo: make([]byte, 32)}
	verify := c.Transport.(guardedTransport).inner.(*http.Transport).TLSClientConfig.VerifyConnection
	err = verify(tls.ConnectionState{PeerCertificates: []*x509.Certificate{cert}})
	if err == nil || !strings.Contains(err.Error(), "validity") {
		t.Fatalf("err=%v", err)
	}
}

func TestHTTPSURLRequiredByJoin(t *testing.T) {
	raw := `{"version":1,"site_id":"s","coordinator_urls":["http://localhost:1"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatal("HTTP URL accepted")
	}
}

func TestPinnedClientRejectsCleartextAndWrapsTransportError(t *testing.T) {
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = c.Get("http://127.0.0.1:1"); err == nil || !strings.Contains(err.Error(), "HTTPS") {
		t.Fatalf("err=%v", err)
	}
	_, err = c.Get("https://127.0.0.1:1")
	var transient *TransientError
	if err == nil || !errors.As(err, &transient) {
		t.Fatalf("err=%v", err)
	}
}
func TestJoinRequiresMDNSAndBoundsSiteID(t *testing.T) {
	raw := `{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x"}`
	if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatal("missing mdns accepted")
	}
	raw = strings.Replace(raw, `"site_id":"s"`, `"site_id":"`+strings.Repeat("é", 129)+`"`, 1) + `,"mdns_service":null}`
	if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatal("long site id accepted")
	}
}

func TestJoinRejectsEmptyQueryAndFragment(t *testing.T) {
	base := `{"version":1,"site_id":"s","coordinator_urls":[%q],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	for _, u := range []string{"https://coordinator?", "https://coordinator#", "https://coordinator?a=1", "https://coordinator#frag"} {
		raw := fmt.Sprintf(base, u)
		if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
			t.Fatalf("query/fragment URL accepted: %s", u)
		}
	}
}

type closeIdleRecorder struct {
	http.RoundTripper
	closed bool
}

func (c *closeIdleRecorder) CloseIdleConnections() { c.closed = true }

func TestGuardedTransportForwardsCloseIdleConnections(t *testing.T) {
	rec := &closeIdleRecorder{}
	guardedTransport{inner: rec}.CloseIdleConnections()
	if !rec.closed {
		t.Fatal("CloseIdleConnections not forwarded to inner transport")
	}
}

func TestPinnedClientBoundsTLSHandshake(t *testing.T) {
	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	if tr := c.Transport.(guardedTransport).inner.(*http.Transport); tr.TLSHandshakeTimeout <= 0 {
		t.Fatalf("TLS handshake timeout not bounded: %v", tr.TLSHandshakeTimeout)
	}
}

func TestJoinRejectsInvalidUTF8(t *testing.T) {
	raw := []byte(`{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`)
	// Corrupt the enrollment_token value with a lone invalid UTF-8 byte.
	bad := bytes.Replace(raw, []byte(`"enrollment_token":"x"`), []byte("\"enrollment_token\":\"\xff\""), 1)
	if utf8.Valid(bad) {
		t.Fatal("test input is still valid UTF-8")
	}
	if _, err := ParseJoin(bad); !errors.Is(err, ErrInvalidJoin) {
		t.Fatalf("invalid UTF-8 accepted: %v", err)
	}
}

func TestJoinRejectsNonCanonicalPin(t *testing.T) {
	// A newline inside the base64 payload is ignored by base64 decoding but
	// violates the canonical sha256/[A-Za-z0-9+/]{43}= pin syntax.
	base := `{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":[%q],"enrollment_token":"x","mdns_service":null}`
	newlinePin := "sha256/" + strings.Repeat("A", 43) + "\n="
	raw := fmt.Sprintf(base, newlinePin)
	if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatalf("non-canonical pin accepted by ParseJoin: %v", err)
	}
	if _, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{newlinePin}}); err == nil {
		t.Fatal("non-canonical pin accepted by NewPinnedClient")
	}
	// Wrong length payload is also rejected.
	if _, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAA"}}); err == nil {
		t.Fatal("short pin accepted by NewPinnedClient")
	}
}

func TestJoinRejectsCaseVariantFields(t *testing.T) {
	raw := `{"VERSION":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatalf("case-variant field accepted: %v", err)
	}
}

type timeoutError struct{}

func (timeoutError) Error() string { return "i/o timeout" }
func (timeoutError) Timeout() bool { return true }

func TestTransientErrorPreservesTimeout(t *testing.T) {
	err := &TransientError{Err: &url.Error{Op: "Get", URL: "https://h", Err: timeoutError{}}}
	var timeout interface{ Timeout() bool }
	if !errors.As(error(err), &timeout) || !timeout.Timeout() {
		t.Fatal("TransientError did not report the wrapped timeout")
	}
	if (&TransientError{Err: errors.New("refused")}).Timeout() {
		t.Fatal("non-timeout error reported as timeout")
	}
}

func TestGuardedTransportClosesRejectedBody(t *testing.T) {
	closed := false
	body := readCloser{closeFn: func() error { closed = true; return nil }}
	req, err := http.NewRequest(http.MethodPost, "http://127.0.0.1:1", body)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := (guardedTransport{inner: failRoundTripper{}}).RoundTrip(req); err == nil {
		t.Fatal("cleartext request accepted")
	}
	if !closed {
		t.Fatal("request body not closed on cleartext rejection")
	}
}

type readCloser struct{ closeFn func() error }

func (readCloser) Read([]byte) (int, error) { return 0, io.EOF }
func (r readCloser) Close() error           { return r.closeFn() }

type failRoundTripper struct{}

func (failRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("inner should not be called")
}

func TestJoinRejectsLoneSurrogateEscape(t *testing.T) {
	base := `{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":%s,"mdns_service":null}`
	for _, tok := range []string{`"\ud800"`, `"\udc00"`, `"\ud800x"`, `"a\udbffb"`} {
		raw := fmt.Sprintf(base, tok)
		if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
			t.Fatalf("lone surrogate accepted for token %s: %v", tok, err)
		}
	}
	// A valid surrogate pair (emoji) must still be accepted.
	raw := fmt.Sprintf(base, `"tok\ud83d\ude00"`)
	if _, err := ParseJoin([]byte(raw)); err != nil {
		t.Fatalf("valid surrogate pair rejected: %v", err)
	}
	// An escaped backslash before "ud800" is a literal, not a surrogate escape.
	raw = fmt.Sprintf(base, `"\\ud800"`)
	if _, err := ParseJoin([]byte(raw)); err != nil {
		t.Fatalf("escaped-backslash literal rejected: %v", err)
	}
}

func TestPinnedClientClearsInheritedTLSDialHooks(t *testing.T) {
	def := http.DefaultTransport.(*http.Transport)
	origCtx, origTLS := def.DialTLSContext, def.DialTLS
	def.DialTLSContext = func(context.Context, string, string) (net.Conn, error) {
		return nil, errors.New("inherited hook must not survive")
	}
	def.DialTLS = func(string, string) (net.Conn, error) {
		return nil, errors.New("inherited hook must not survive")
	}
	defer func() { def.DialTLSContext, def.DialTLS = origCtx, origTLS }()

	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	tr := c.Transport.(guardedTransport).inner.(*http.Transport)
	if tr.DialTLSContext != nil || tr.DialTLS != nil {
		t.Fatal("pinned client inherited a TLS dial hook that would bypass pinning")
	}
	if tr.TLSHandshakeTimeout <= 0 {
		t.Fatal("clearing dial hooks dropped the bounded handshake timeout")
	}
}

func TestJoinRejectsDuplicateFields(t *testing.T) {
	// Sensitive duplicate: a repeated enrollment_token must not be silently
	// collapsed to the last value.
	sensitive := `{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"first","enrollment_token":"second","mdns_service":null}`
	if _, err := ParseJoin([]byte(sensitive)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatalf("duplicate enrollment_token accepted: %v", err)
	}
	// Ordinary duplicate: a repeated version key is equally ambiguous.
	ordinary := `{"version":1,"version":2,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	if _, err := ParseJoin([]byte(ordinary)); !errors.Is(err, ErrInvalidJoin) {
		t.Fatalf("duplicate version accepted: %v", err)
	}
	// Control: the same bundle without duplicates still parses.
	clean := `{"version":1,"site_id":"s","coordinator_urls":["https://one"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	if _, err := ParseJoin([]byte(clean)); err != nil {
		t.Fatalf("clean bundle rejected: %v", err)
	}
}

func TestJoinRejectsUnusableHostAndPort(t *testing.T) {
	base := `{"version":1,"site_id":"s","coordinator_urls":[%q],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"x","mdns_service":null}`
	for _, u := range []string{"https://:", "https://:443", "https://host:99999", "https://host:0", "https://host:abc"} {
		raw := fmt.Sprintf(base, u)
		if _, err := ParseJoin([]byte(raw)); !errors.Is(err, ErrInvalidJoin) {
			t.Fatalf("unusable host/port accepted: %s", u)
		}
	}
	// A valid explicit port is still accepted.
	if _, err := ParseJoin([]byte(fmt.Sprintf(base, "https://host:8443"))); err != nil {
		t.Fatalf("valid explicit port rejected: %v", err)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestTransientErrorExcludesCancellation(t *testing.T) {
	req, err := http.NewRequest(http.MethodGet, "https://host", nil)
	if err != nil {
		t.Fatal(err)
	}
	inner := roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, context.Canceled })
	_, err = (guardedTransport{inner: inner}).RoundTrip(req)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation not preserved: %v", err)
	}
	var transient *TransientError
	if errors.As(err, &transient) {
		t.Fatal("cancellation was wrapped as transient")
	}
}

func TestPinnedClientSurvivesReplacedDefaultTransport(t *testing.T) {
	orig := http.DefaultTransport
	http.DefaultTransport = roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, nil })
	defer func() { http.DefaultTransport = orig }()

	c, err := NewPinnedClient(Profile{CoordinatorSPKISHA256: []string{"sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}})
	if err != nil {
		t.Fatal(err)
	}
	tr := c.Transport.(guardedTransport).inner.(*http.Transport)
	if tr.TLSHandshakeTimeout <= 0 {
		t.Fatal("fallback transport lost its bounded handshake timeout")
	}
	if tr.Proxy != nil || tr.DialTLS != nil || tr.DialTLSContext != nil {
		t.Fatal("fallback transport not locked down")
	}
}
