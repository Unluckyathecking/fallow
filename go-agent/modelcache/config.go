// Package modelcache is the agent-side model cache: it pulls GGUF blobs from
// the coordinator with resume and retry, verifies sha256 and size, and keeps
// verified files on local disk keyed by a cheap marker so the heartbeat hot
// path never rehashes a multi-GB file.
//
// It is a Go port of the Python fallow_agent.modelcache package and is
// byte-compatible with it on disk: the same blob path layout, the same ".part"
// partial-download file names, and the same "<file>.sha256" verification marker
// names. An operator can point either agent at the same cache directory.
package modelcache

import "time"

// On-disk layout suffixes. These must match the Python implementation so both
// agents share a cache directory.
const (
	PartSuffix   = ".part"
	MarkerSuffix = ".sha256"
	tmpSuffix    = ".tmp"
)

// DefaultCacheDir is the production cache root, relative to the user home
// directory. Callers pass an absolute directory to Store; ResolveDefaultCacheDir
// expands this value.
const DefaultCacheDir = ".fallow/models"

// BlobPathTemplate is the coordinator blob endpoint. The model_id is substituted
// at request time.
const BlobPathTemplate = "/v1/models/%s/blob"

// HTTP status codes the download path acts on explicitly.
const (
	httpOK              = 200
	httpPartialContent  = 206
	defaultMaxRetries   = 3
	defaultChunkSizeMiB = 1
)

// oneMiB is the streaming chunk size: bytes are hashed and flushed one chunk at
// a time so a multi-GB blob never sits in memory.
const oneMiB = 1024 * 1024

const defaultBackoffBase = 500 * time.Millisecond

// Config holds the immutable per-download tunables for a Store. The zero value
// is not valid; use DefaultConfig and override fields as needed.
type Config struct {
	// MaxRetries is the number of retries after the initial attempt for
	// transport failures and retryable (non-200/206) statuses.
	MaxRetries int
	// BackoffBase is the base delay for exponential backoff between retries.
	BackoffBase time.Duration
	// ChunkSize is the streaming/hashing chunk size in bytes.
	ChunkSize int
}

// DefaultConfig returns the production download tunables: three retries, a
// 500ms backoff base, and a 1 MiB chunk size.
func DefaultConfig() Config {
	return Config{
		MaxRetries:  defaultMaxRetries,
		BackoffBase: defaultBackoffBase,
		ChunkSize:   defaultChunkSizeMiB * oneMiB,
	}
}
