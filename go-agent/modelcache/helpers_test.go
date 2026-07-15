package modelcache_test

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/modelcache"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// Shared constants mirror the Python model-cache test helpers so both suites
// exercise identical on-disk names and endpoints.
const (
	deviceToken = "tok-abc123"
	modelID     = "qwen2.5-7b-instruct-q4km"
	fileName    = "model.gguf"
)

func sha256Hex(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// manifestOpts overrides the sha256/size a manifest advertises, for verification
// failure tests.
type manifestOpts struct {
	sha256    string
	sizeBytes int
	setSize   bool
}

func makeManifest(body []byte, opts manifestOpts) protocol.ModelManifest {
	sha := opts.sha256
	if sha == "" {
		sha = sha256Hex(body)
	}
	size := len(body)
	if opts.setSize {
		size = opts.sizeBytes
	}
	return protocol.ModelManifest{
		ModelID:    modelID,
		Family:     "qwen2.5",
		Quant:      "Q4_K_M",
		WorkerKind: protocol.WorkerKindChat,
		FileName:   fileName,
		SHA256:     sha,
		SizeBytes:  size,
	}
}

// recorder captures per-request metadata so tests can assert request counts and
// Range headers, safely under concurrency.
type recorder struct {
	mu     sync.Mutex
	count  int
	ranges []string
}

func (r *recorder) add(req *http.Request) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.count++
	r.ranges = append(r.ranges, req.Header.Get("Range"))
}

func (r *recorder) calls() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.count
}

func (r *recorder) firstRange() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.ranges) == 0 {
		return ""
	}
	return r.ranges[0]
}

// blobHandler serves body as a coordinator blob endpoint, honouring Range unless
// ignoreRange is set. rec may be nil.
func blobHandler(body []byte, ignoreRange bool, rec *recorder) http.HandlerFunc {
	return func(w http.ResponseWriter, req *http.Request) {
		if rec != nil {
			rec.add(req)
		}
		rng := req.Header.Get("Range")
		if rng != "" && !ignoreRange {
			start := parseRangeStart(rng)
			chunk := body[start:]
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, len(body)-1, len(body)))
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(chunk)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}
}

func parseRangeStart(rng string) int {
	spec := strings.TrimPrefix(rng, "bytes=")
	start, _, _ := strings.Cut(spec, "-")
	n, _ := strconv.Atoi(start)
	return n
}

// newStore spins up an httptest server for handler and returns a Store rooted at
// a fresh temp cache directory, plus that directory. Backoff sleeps are recorded
// but never actually wait.
func newStore(
	t *testing.T, handler http.HandlerFunc, sleeps *[]time.Duration, cfg *modelcache.Config,
) (*modelcache.Store, string) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	dir := t.TempDir()

	sleep := func(d time.Duration) {
		if sleeps != nil {
			*sleeps = append(*sleeps, d)
		}
	}
	opts := []modelcache.Option{
		modelcache.WithCacheDir(dir),
		modelcache.WithSleep(sleep),
	}
	if cfg != nil {
		opts = append(opts, modelcache.WithConfig(*cfg))
	}
	return modelcache.New(srv.URL, deviceToken, srv.Client(), opts...), dir
}
