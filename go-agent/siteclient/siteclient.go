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
	"regexp"
	"strconv"
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

// Timeout preserves the standard net timeout classification of the wrapped
// error so an *url.Error built around a TransientError still reports timeouts.
func (e *TransientError) Timeout() bool {
	var t interface{ Timeout() bool }
	return errors.As(e.Err, &t) && t.Timeout()
}

var allowedJoinKeys = map[string]bool{
	"version": true, "site_id": true, "coordinator_urls": true,
	"coordinator_spki_sha256": true, "enrollment_token": true, "mdns_service": true,
}

var errDuplicateKey = errors.New("duplicate json key")

// checkDuplicateKeys walks the JSON token stream and rejects any object that
// repeats a property name. encoding/json silently keeps the last occurrence, so
// a join bundle that repeats a sensitive field such as enrollment_token would
// otherwise parse into an ambiguous artifact. data must already be valid JSON.
func checkDuplicateKeys(dec *json.Decoder) error {
	t, err := dec.Token()
	if err != nil {
		return err
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil // scalar value
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return err
			}
			key := kt.(string)
			if seen[key] {
				return errDuplicateKey
			}
			seen[key] = true
			if err := checkDuplicateKeys(dec); err != nil {
				return err
			}
		}
		_, err = dec.Token() // closing }
		return err
	case '[':
		for dec.More() {
			if err := checkDuplicateKeys(dec); err != nil {
				return err
			}
		}
		_, err = dec.Token() // closing ]
		return err
	}
	return nil
}

// hasLoneSurrogateEscape reports whether valid JSON contains a \uXXXX escape in
// the surrogate range that is not part of a proper high+low pair. encoding/json
// would otherwise decode such an escape to U+FFFD, silently altering a credential
// or identifier. data must already be valid JSON so backslashes are real escapes.
func hasLoneSurrogateEscape(data []byte) bool {
	for i := 0; i+1 < len(data); {
		if data[i] != '\\' {
			i++
			continue
		}
		if data[i+1] != 'u' || i+6 > len(data) {
			i += 2 // an escape such as \\, \", or \n consumes two bytes
			continue
		}
		hi, ok := parseHex4(data[i+2 : i+6])
		if !ok {
			i += 2
			continue
		}
		switch {
		case hi >= 0xD800 && hi <= 0xDBFF: // high surrogate, needs a low pair
			if i+12 > len(data) || data[i+6] != '\\' || data[i+7] != 'u' {
				return true
			}
			lo, ok := parseHex4(data[i+8 : i+12])
			if !ok || lo < 0xDC00 || lo > 0xDFFF {
				return true
			}
			i += 12
		case hi >= 0xDC00 && hi <= 0xDFFF: // low surrogate with no preceding high
			return true
		default:
			i += 6
		}
	}
	return false
}

func parseHex4(b []byte) (rune, bool) {
	var v rune
	for _, c := range b {
		v <<= 4
		switch {
		case c >= '0' && c <= '9':
			v |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			v |= rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v |= rune(c-'A') + 10
		default:
			return 0, false
		}
	}
	return v, true
}

func ParseJoin(data []byte) (JoinBundle, error) {
	if !utf8.Valid(data) {
		return JoinBundle{}, ErrInvalidJoin
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields["mdns_service"] == nil {
		return JoinBundle{}, ErrInvalidJoin
	}
	if hasLoneSurrogateEscape(data) {
		return JoinBundle{}, ErrInvalidJoin
	}
	if checkDuplicateKeys(json.NewDecoder(strings.NewReader(string(data)))) != nil {
		return JoinBundle{}, ErrInvalidJoin
	}
	for k := range fields {
		if !allowedJoinKeys[k] {
			return JoinBundle{}, ErrInvalidJoin
		}
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
		if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || strings.Contains(raw, "#") || (u.Path != "" && u.Path != "/") || seen[raw] {
			return JoinBundle{}, ErrInvalidJoin
		}
		if port := u.Port(); port != "" {
			n, err := strconv.Atoi(port)
			if err != nil || n < 1 || n > 65535 {
				return JoinBundle{}, ErrInvalidJoin
			}
		}
		seen[raw] = true
	}
	seen = map[string]bool{}
	for _, p := range j.CoordinatorSPKISHA256 {
		if _, ok := decodePin(p); !ok || seen[p] {
			return JoinBundle{}, ErrInvalidJoin
		}
		seen[p] = true
	}
	return j, nil
}

var pinPattern = regexp.MustCompile(`^sha256/[A-Za-z0-9+/]{43}=$`)

// decodePin accepts only the canonical sha256/<44-char base64> pin spelling and
// returns the decoded 32-byte SPKI hash. It rejects any value base64 decoding
// would otherwise tolerate, such as a payload with an embedded CR or LF or
// non-canonical trailing bits.
func decodePin(p string) ([]byte, bool) {
	if !pinPattern.MatchString(p) {
		return nil, false
	}
	payload := strings.TrimPrefix(p, "sha256/")
	b, err := base64.StdEncoding.DecodeString(payload)
	if err != nil || len(b) != sha256.Size || base64.StdEncoding.EncodeToString(b) != payload {
		return nil, false
	}
	return b, true
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
		if r.Body != nil {
			r.Body.Close()
		}
		return nil, &ConfigError{errors.New("HTTPS is required")}
	}
	resp, err := t.inner.RoundTrip(r)
	if err != nil {
		var pinErr *PinError
		var configErr *ConfigError
		// Pin and config failures keep their type; a deliberate cancellation is
		// not a transport fault and must not look retryable to callers.
		if errors.As(err, &pinErr) || errors.As(err, &configErr) || errors.Is(err, context.Canceled) {
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
		b, ok := decodePin(s)
		if !ok {
			return nil, &ConfigError{errors.New("invalid certificate pin")}
		}
		pins[i] = b
	}
	// A host application may legally replace http.DefaultTransport with a custom
	// RoundTripper, so guard the type assertion instead of panicking at startup.
	tr, ok := http.DefaultTransport.(*http.Transport)
	if ok {
		tr = tr.Clone()
	} else {
		tr = &http.Transport{}
	}
	tr.Proxy = nil
	// A custom DialTLS hook inherited via Clone would hand net/http a connection
	// it treats as already handshaken, bypassing TLSClientConfig and the pin
	// check. Clear both so pinning always runs.
	tr.DialTLS = nil
	tr.DialTLSContext = nil
	// Guarantee a bounded handshake even when the base transport had none.
	if tr.TLSHandshakeTimeout <= 0 {
		tr.TLSHandshakeTimeout = 10 * time.Second
	}
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
