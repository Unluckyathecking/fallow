package inference

import (
	"context"
	"encoding/base64"
	"net/http"
	"strings"
	"testing"
)

func TestDecodeClaimStrict(t *testing.T) {
	valid := `{"version":1,"claim_id":"claim-0123456789abcdef","presence_generation":7,` +
		`"replica_port":8100,"method":"POST","path":"/v1/chat/completions",` +
		`"content_type":"application/json","body_b64":"` +
		base64.StdEncoding.EncodeToString([]byte(`{"x":1}`)) + `","deadline_ms":30000}`

	if _, err := DecodeClaim([]byte(valid)); err != nil {
		t.Fatalf("valid claim rejected: %v", err)
	}

	cases := map[string]string{
		"unknown field": strings.Replace(valid, `"version":1,`, `"version":1,"extra":true,`, 1),
		"trailing data": valid + `{"another":1}`,
		"malformed":     valid[:len(valid)-5],
		"empty":         ``,
		"bad version":   strings.Replace(valid, `"version":1`, `"version":2`, 1),
	}
	for name, body := range cases {
		if _, err := DecodeClaim([]byte(body)); err == nil {
			t.Errorf("%s: expected decode error", name)
		}
	}
}

func TestValidateRejects(t *testing.T) {
	base := func() Claim {
		return Claim{
			Version:            1,
			ClaimID:            "claim-0123456789abcdef",
			PresenceGeneration: 1,
			ReplicaPort:        8100,
			Method:             "POST",
			Path:               "/v1/chat/completions",
			ContentType:        "application/json",
			DeadlineMS:         30000,
		}
	}
	if err := validate(base()); err != nil {
		t.Fatalf("base claim should be valid: %v", err)
	}

	mutate := map[string]func(*Claim){
		"version":           func(c *Claim) { c.Version = 2 },
		"short id":          func(c *Claim) { c.ClaimID = "short" },
		"long id":           func(c *Claim) { c.ClaimID = strings.Repeat("x", 129) },
		"neg generation":    func(c *Claim) { c.PresenceGeneration = -1 },
		"port zero":         func(c *Claim) { c.ReplicaPort = 0 },
		"port high":         func(c *Claim) { c.ReplicaPort = 70000 },
		"method get":        func(c *Claim) { c.Method = "GET" },
		"content type":      func(c *Claim) { c.ContentType = "text/plain" },
		"deadline zero":     func(c *Claim) { c.DeadlineMS = 0 },
		"deadline huge":     func(c *Claim) { c.DeadlineMS = 300001 },
		"path unknown":      func(c *Claim) { c.Path = "/v1/models" },
		"path query":        func(c *Claim) { c.Path = "/v1/chat/completions?x=1" },
		"path traversal":    func(c *Claim) { c.Path = "/v1//chat/completions" },
		"path not absolute": func(c *Claim) { c.Path = "v1/chat/completions" },
	}
	for name, m := range mutate {
		c := base()
		m(&c)
		if err := validate(c); err == nil {
			t.Errorf("%s: expected validation error", name)
		}
	}
}

func TestLoopbackRequestTargetsLocalhost(t *testing.T) {
	claim := claimFor(8100, "/v1/embeddings")
	req, err := localRequest(context.Background(), claim, []byte(`{}`))
	if err != nil {
		t.Fatalf("localRequest: %v", err)
	}
	if req.URL.Scheme != "http" || req.URL.Hostname() != "127.0.0.1" || req.URL.Port() != "8100" {
		t.Fatalf("request must target loopback, got %s://%s", req.URL.Scheme, req.URL.Host)
	}
	if req.URL.Path != "/v1/embeddings" {
		t.Fatalf("unexpected path %q", req.URL.Path)
	}
	if got := req.Header.Get("Content-Type"); got != "application/json" {
		t.Fatalf("content-type not set: %q", got)
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Fatalf("Authorization must not be forwarded, got %q", got)
	}
}

func TestLoopbackClientHasNoProxyAndNoRedirect(t *testing.T) {
	tr, ok := loopbackClient.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("unexpected transport type %T", loopbackClient.Transport)
	}
	if tr.Proxy != nil {
		t.Fatal("loopback transport must not use a proxy")
	}
	if loopbackClient.CheckRedirect == nil {
		t.Fatal("loopback client must refuse redirects")
	}
	if err := loopbackClient.CheckRedirect(nil, nil); err != http.ErrUseLastResponse {
		t.Fatalf("redirects must not be followed, got %v", err)
	}
}
