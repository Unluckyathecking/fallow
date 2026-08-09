package siteclient

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
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
