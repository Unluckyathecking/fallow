// Package siteclient implements the trust boundary for LAN Site Mode.
package siteclient

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

type JoinBundle struct {
	Version               int      `json:"version"`
	SiteID                string   `json:"site_id"`
	CoordinatorURLs       []string `json:"coordinator_urls"`
	CoordinatorSPKISHA256 []string `json:"coordinator_spki_sha256"`
	EnrollmentToken       string   `json:"enrollment_token"`
	MDNSService           *string  `json:"mdns_service"`
}

type Profile struct {
	SiteID                string
	CoordinatorURLs       []string
	CoordinatorSPKISHA256 []string
	MDNSService           *string
}

func (j JoinBundle) Profile() Profile {
	return Profile{j.SiteID, append([]string(nil), j.CoordinatorURLs...), append([]string(nil), j.CoordinatorSPKISHA256...), j.MDNSService}
}

var ErrInvalidJoin = errors.New("invalid join bundle")

type ConfigError struct{ Err error }

func (e *ConfigError) Error() string { return "site client configuration: " + e.Err.Error() }
func (e *ConfigError) Unwrap() error { return e.Err }

type PinError struct{ Err error }

func (e *PinError) Error() string { return "site client certificate pin: " + e.Err.Error() }
func (e *PinError) Unwrap() error { return e.Err }

type TransientError struct{ Err error }

func (e *TransientError) Error() string { return "site client transport: " + e.Err.Error() }
func (e *TransientError) Unwrap() error { return e.Err }

func ParseJoin(data []byte) (JoinBundle, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields["mdns_service"] == nil {
		return JoinBundle{}, ErrInvalidJoin
	}
	var j JoinBundle
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&j); err != nil {
		return JoinBundle{}, fmt.Errorf("%w: %v", ErrInvalidJoin, err)
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return JoinBundle{}, fmt.Errorf("%w: trailing data", ErrInvalidJoin)
	}
	if j.Version != 1 || utf8.RuneCountInString(j.SiteID) > 128 || strings.TrimSpace(j.SiteID) == "" || strings.TrimSpace(j.EnrollmentToken) == "" || len(j.CoordinatorURLs) == 0 || len(j.CoordinatorSPKISHA256) == 0 || j.MDNSService != nil && *j.MDNSService != "_fallow._tcp.local." {
		return JoinBundle{}, ErrInvalidJoin
	}
	seen := map[string]bool{}
	for _, raw := range j.CoordinatorURLs {
		u, e := url.Parse(raw)
		if e != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || strings.Contains(raw, "#") || (u.Path != "" && u.Path != "/") || seen[raw] {
			return JoinBundle{}, ErrInvalidJoin
		}
		seen[raw] = true
	}
	seen = map[string]bool{}
	for _, p := range j.CoordinatorSPKISHA256 {
		if !strings.HasPrefix(p, "sha256/") {
			return JoinBundle{}, ErrInvalidJoin
		}
		b, e := base64.StdEncoding.DecodeString(strings.TrimPrefix(p, "sha256/"))
		if e != nil || len(b) != sha256.Size || seen[p] {
			return JoinBundle{}, ErrInvalidJoin
		}
		seen[p] = true
	}
	return j, nil
}

type Discovery interface {
	Candidates(context.Context, Profile) ([]string, error)
}
type Resolver struct{ Discovery Discovery }

func (r Resolver) Candidates(ctx context.Context, p Profile) ([]string, error) {
	out := append([]string(nil), p.CoordinatorURLs...)
	if len(out) > 0 {
		return out, nil
	}
	if r.Discovery != nil {
		return r.Discovery.Candidates(ctx, p)
	}
	return nil, &ConfigError{errors.New("no coordinator candidates")}
}

type guardedTransport struct{ inner http.RoundTripper }

func (t guardedTransport) CloseIdleConnections() {
	if c, ok := t.inner.(interface{ CloseIdleConnections() }); ok {
		c.CloseIdleConnections()
	}
}

func (t guardedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL == nil || r.URL.Scheme != "https" {
		return nil, &ConfigError{errors.New("HTTPS is required")}
	}
	resp, err := t.inner.RoundTrip(r)
	if err != nil {
		var pinErr *PinError
		var configErr *ConfigError
		if errors.As(err, &pinErr) || errors.As(err, &configErr) {
			return nil, err
		}
		return nil, &TransientError{Err: err}
	}
	return resp, nil
}

func NewPinnedClient(p Profile) (*http.Client, error) {
	if len(p.CoordinatorSPKISHA256) == 0 {
		return nil, &ConfigError{errors.New("no certificate pins")}
	}
	pins := make([][]byte, len(p.CoordinatorSPKISHA256))
	for i, s := range p.CoordinatorSPKISHA256 {
		if !strings.HasPrefix(s, "sha256/") {
			return nil, &ConfigError{errors.New("invalid certificate pin")}
		}
		b, e := base64.StdEncoding.DecodeString(strings.TrimPrefix(s, "sha256/"))
		if e != nil || len(b) != sha256.Size {
			return nil, &ConfigError{errors.New("invalid certificate pin")}
		}
		pins[i] = b
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = nil
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: true, VerifyConnection: func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return &PinError{errors.New("peer sent no certificate")}
		}
		now := time.Now()
		cert := cs.PeerCertificates[0]
		if now.Before(cert.NotBefore) || now.After(cert.NotAfter) {
			return &PinError{errors.New("certificate outside validity window")}
		}
		sum := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
		for _, pin := range pins {
			if subtle.ConstantTimeCompare(sum[:], pin) == 1 {
				return nil
			}
		}
		return &PinError{errors.New("certificate pin mismatch")}
	}}
	return &http.Client{Transport: guardedTransport{inner: tr}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
}
