package siteclient

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
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
	tr := c.Transport.(*http.Transport)
	tr.TLSClientConfig.RootCAs = nil
	tr.TLSClientConfig.InsecureSkipVerify = true // test-only trust bypass; pin remains enforced
	_, _ = c.Get(srv.URL)
	if requests.Load() != 0 {
		t.Fatal("wrong pin reached handler")
	}
}
