package modelcache

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// Store is a download-with-resume model cache backed by an injected HTTP client.
// It implements the same contract as the Python HttpModelStore and shares its
// on-disk layout.
type Store struct {
	baseURL     string
	client      *http.Client
	cacheDir    string
	cfg         Config
	sleep       func(time.Duration)
	authHeaders map[string]string

	locksMu sync.Mutex
	locks   map[string]*sync.Mutex
}

// Option customises a Store at construction.
type Option func(*Store)

// WithCacheDir sets the cache root directory (default: ResolveDefaultCacheDir).
func WithCacheDir(dir string) Option { return func(s *Store) { s.cacheDir = dir } }

// WithConfig overrides the retry/backoff/chunk tuning (default: DefaultConfig).
func WithConfig(cfg Config) Option { return func(s *Store) { s.cfg = cfg } }

// WithSleep overrides the backoff sleep (default: time.Sleep). Tests use it to
// record backoff durations without waiting.
func WithSleep(sleep func(time.Duration)) Option { return func(s *Store) { s.sleep = sleep } }

// ResolveDefaultCacheDir expands the default cache root against the user home
// directory.
func ResolveDefaultCacheDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, DefaultCacheDir), nil
}

// New constructs a Store for the given coordinator base URL and device token.
func New(baseURL, deviceToken string, client *http.Client, opts ...Option) *Store {
	s := &Store{
		baseURL:     strings.TrimRight(baseURL, "/"),
		client:      client,
		cfg:         DefaultConfig(),
		sleep:       time.Sleep,
		authHeaders: map[string]string{"Authorization": "Bearer " + deviceToken},
		locks:       make(map[string]*sync.Mutex),
	}
	for _, opt := range opts {
		opt(s)
	}
	if s.cacheDir == "" {
		if dir, err := ResolveDefaultCacheDir(); err == nil {
			s.cacheDir = dir
		}
	}
	return s
}

// PathIfPresent returns the blob path and true iff the blob exists and its
// marker matches the manifest sha256. It trusts the marker and does NOT rehash
// the file, so it is safe to call on the heartbeat hot path.
func (s *Store) PathIfPresent(m protocol.ModelManifest) (string, bool) {
	blob := blobPath(s.cacheDir, m)
	if _, err := os.Stat(blob); err != nil {
		return "", false
	}
	if readMarker(markerPath(s.cacheDir, m)) == m.SHA256 {
		return blob, true
	}
	return "", false
}

// Ensure returns a verified local path, downloading (with resume) if needed. A
// per-model lock serialises concurrent callers so a model is fetched at most
// once; the second caller re-checks presence under the lock.
func (s *Store) Ensure(ctx context.Context, m protocol.ModelManifest) (string, error) {
	if present, ok := s.PathIfPresent(m); ok {
		return present, nil
	}
	lock := s.lockFor(m.ModelID)
	lock.Lock()
	defer lock.Unlock()

	if present, ok := s.PathIfPresent(m); ok {
		return present, nil
	}
	return s.downloadAndVerify(ctx, m)
}

func (s *Store) lockFor(modelID string) *sync.Mutex {
	s.locksMu.Lock()
	defer s.locksMu.Unlock()
	lock := s.locks[modelID]
	if lock == nil {
		lock = &sync.Mutex{}
		s.locks[modelID] = lock
	}
	return lock
}

func (s *Store) blobURL(modelID string) string {
	return s.baseURL + fmt.Sprintf(BlobPathTemplate, modelID)
}

func (s *Store) downloadAndVerify(ctx context.Context, m protocol.ModelManifest) (string, error) {
	part := partPath(s.cacheDir, m)
	if err := os.MkdirAll(filepath.Dir(part), 0o755); err != nil {
		return "", err
	}
	result, err := s.fetchWithRetries(ctx, s.blobURL(m.ModelID), part)
	if err != nil {
		return "", err
	}
	if err := verifyOrDelete(m, part, result); err != nil {
		return "", err
	}
	blob := blobPath(s.cacheDir, m)
	if err := writeMarkerAtomic(markerPath(s.cacheDir, m), m.SHA256); err != nil {
		return "", err
	}
	if err := os.Rename(part, blob); err != nil { // atomic publish of the verified file
		return "", err
	}
	return blob, nil
}

// verifyOrDelete checks the downloaded bytes against the manifest. On mismatch
// it deletes the partial file and returns ErrVerification.
func verifyOrDelete(m protocol.ModelManifest, part string, result downloadResult) error {
	if result.sha256 == m.SHA256 && result.size == int64(m.SizeBytes) {
		return nil
	}
	_ = os.Remove(part)
	return fmt.Errorf(
		"%w for %s: sha256 %s vs %s, size %d vs %d",
		ErrVerification, m.ModelID, result.sha256, m.SHA256, result.size, m.SizeBytes,
	)
}

func (s *Store) fetchWithRetries(ctx context.Context, url, part string) (downloadResult, error) {
	attempt := 0
	for {
		result, err := streamToPart(ctx, s.client, url, s.authHeaders, part, s.cfg.ChunkSize)
		if err == nil {
			return result, nil
		}
		if !isRetryable(err) {
			return downloadResult{}, err
		}
		attempt++
		if attempt > s.cfg.MaxRetries {
			return downloadResult{}, fmt.Errorf(
				"%w: %s after %d attempt(s): %v", ErrFetch, url, attempt, err,
			)
		}
		s.sleep(s.cfg.BackoffBase * time.Duration(1<<(attempt-1)))
	}
}

// isRetryable reports whether an error from one download attempt should be
// retried: transport-level failures (surfaced by net/http as *url.Error) and
// retryable HTTP statuses. Content and filesystem errors are not retried.
//
// Context cancellation/deadline is checked FIRST: net/http wraps those in a
// *url.Error, so without this guard a cancelled download would spin through the
// full retry/backoff budget (~seconds of wasted work) instead of returning at
// once. A cancelled context will not become un-cancelled, so retrying is futile.
func isRetryable(err error) bool {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var status *retryableStatusError
	if errors.As(err, &status) {
		return true
	}
	var urlErr *url.Error
	return errors.As(err, &urlErr)
}
