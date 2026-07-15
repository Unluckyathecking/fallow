package modelcache

import "errors"

// ErrFetch is returned when a blob could not be downloaded after exhausting the
// retry budget. It covers transport-level failures (connection reset, timeout)
// and non-resumable HTTP status codes. It is distinct from a content failure so
// callers can retry a fetch later without treating the model as poisoned.
// Wrapped causes are available via errors.Unwrap / errors.Is.
var ErrFetch = errors.New("model fetch failed")

// ErrVerification is returned when a fully downloaded blob failed sha256 or size
// verification. It is deterministic given the bytes, so it is never retried: the
// partial file is deleted and the caller must decide whether the manifest itself
// is wrong.
var ErrVerification = errors.New("model verification failed")
