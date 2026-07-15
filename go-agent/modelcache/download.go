package modelcache

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"io"
	"net/http"
	"os"
)

// downloadResult is the outcome of one completed stream: the running hash and
// total byte count.
type downloadResult struct {
	sha256 string
	size   int64
}

// retryableStatusError signals a blob request returned a status the download
// path neither appends to (206) nor restarts from (200).
type retryableStatusError struct {
	statusCode int
}

func (e *retryableStatusError) Error() string {
	return fmt.Sprintf("unexpected blob status %d", e.statusCode)
}

// seedFromExisting feeds an existing ".part" prefix through hasher and returns
// its size. The partial file may have been written by an earlier process, so we
// cannot assume any in-memory hash state carried over — we rehash the prefix
// once.
func seedFromExisting(hasher hash.Hash, part string, blockSize int) (int64, error) {
	fh, err := os.Open(part)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, err
	}
	defer fh.Close()

	buf := make([]byte, blockSize)
	var size int64
	for {
		n, readErr := fh.Read(buf)
		if n > 0 {
			hasher.Write(buf[:n])
			size += int64(n)
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return 0, readErr
		}
	}
	return size, nil
}

// resolveDisposition maps (status, resume offset) to (append?, bytes already
// counted). 206 appends to the prefix; 200 restarts from zero even if a range
// was requested (the coordinator ignored it); anything else is retryable.
func resolveDisposition(statusCode int, existing int64) (bool, int64, error) {
	switch statusCode {
	case httpPartialContent:
		return true, existing, nil
	case httpOK:
		return false, 0, nil
	default:
		return false, 0, &retryableStatusError{statusCode: statusCode}
	}
}

// streamToPart performs one resume-aware download attempt into part. It owns
// exactly one HTTP attempt; retry, backoff, and verification live in Store.
func streamToPart(
	ctx context.Context,
	client *http.Client,
	url string,
	headers map[string]string,
	part string,
	chunkSize int,
) (downloadResult, error) {
	hasher := sha256.New()
	existing, err := seedFromExisting(hasher, part, chunkSize)
	if err != nil {
		return downloadResult{}, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return downloadResult{}, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if existing > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", existing))
	}

	resp, err := client.Do(req)
	if err != nil {
		return downloadResult{}, err
	}
	defer resp.Body.Close()

	appendMode, total, err := resolveDisposition(resp.StatusCode, existing)
	if err != nil {
		return downloadResult{}, err
	}
	if !appendMode {
		hasher = sha256.New() // restart / full: discard the seeded prefix
	}

	flags := os.O_CREATE | os.O_WRONLY
	if appendMode {
		flags |= os.O_APPEND
	} else {
		flags |= os.O_TRUNC
	}
	fh, err := os.OpenFile(part, flags, 0o644)
	if err != nil {
		return downloadResult{}, err
	}

	written, copyErr := streamChunks(fh, resp.Body, hasher, chunkSize)
	total += written
	if closeErr := fh.Close(); closeErr != nil && copyErr == nil {
		copyErr = closeErr
	}
	if copyErr != nil {
		return downloadResult{}, copyErr
	}

	return downloadResult{sha256: hex.EncodeToString(hasher.Sum(nil)), size: total}, nil
}

// streamChunks copies body into fh in fixed-size chunks, updating hasher as
// bytes arrive, and returns the number of bytes written.
func streamChunks(fh io.Writer, body io.Reader, hasher hash.Hash, chunkSize int) (int64, error) {
	buf := make([]byte, chunkSize)
	var total int64
	for {
		n, readErr := body.Read(buf)
		if n > 0 {
			if _, writeErr := fh.Write(buf[:n]); writeErr != nil {
				return total, writeErr
			}
			hasher.Write(buf[:n])
			total += int64(n)
		}
		if readErr == io.EOF {
			return total, nil
		}
		if readErr != nil {
			return total, readErr
		}
	}
}
