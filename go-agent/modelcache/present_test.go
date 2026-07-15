package modelcache_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// path_if_present trust-boundary and single-download concurrency tests. Ported
// 1:1 from test_modelcache_present.py.

func TestPathIfPresentNoneWhenAbsent(t *testing.T) {
	body := []byte("nope")
	manifest := makeManifest(body, manifestOpts{})
	store, _ := newStore(t, blobHandler(body, false, nil), nil, nil)

	if _, ok := store.PathIfPresent(manifest); ok {
		t.Fatal("expected not present")
	}
}

func TestPathIfPresentTrustsMarkerWithoutRehash(t *testing.T) {
	body := bytes.Repeat([]byte("trusted-"), 300)
	manifest := makeManifest(body, manifestOpts{})
	store, _ := newStore(t, blobHandler(body, false, nil), nil, nil)
	path, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}

	// Corrupt the blob AFTER verification, leaving the marker intact. The store
	// trusts the marker and does NOT rehash the (now wrong) bytes.
	if err := os.WriteFile(path, []byte("CORRUPTED"), 0o644); err != nil {
		t.Fatal(err)
	}

	if p, ok := store.PathIfPresent(manifest); !ok || p != path {
		t.Fatalf("PathIfPresent = %q,%v want %q,true", p, ok, path)
	}
}

func TestPathIfPresentNoneWhenMarkerMissing(t *testing.T) {
	body := bytes.Repeat([]byte("has-marker-"), 50)
	manifest := makeManifest(body, manifestOpts{})
	store, dir := newStore(t, blobHandler(body, false, nil), nil, nil)
	if _, err := store.Ensure(context.Background(), manifest); err != nil {
		t.Fatal(err)
	}

	if err := os.Remove(filepath.Join(dir, modelID, fileName+".sha256")); err != nil {
		t.Fatal(err)
	}

	if _, ok := store.PathIfPresent(manifest); ok {
		t.Fatal("expected not present after marker removed")
	}
}

func TestPathIfPresentNoneOnMarkerMismatch(t *testing.T) {
	body := bytes.Repeat([]byte("real-body-"), 50)
	manifest := makeManifest(body, manifestOpts{})
	store, _ := newStore(t, blobHandler(body, false, nil), nil, nil)
	if _, err := store.Ensure(context.Background(), manifest); err != nil {
		t.Fatal(err)
	}

	// Same file on disk, but a manifest that expects a different digest.
	other := makeManifest(body, manifestOpts{sha256: strings64('a')})
	if _, ok := store.PathIfPresent(other); ok {
		t.Fatal("expected not present on marker mismatch")
	}
}

func TestConcurrentEnsureDownloadsOnce(t *testing.T) {
	body := bytes.Repeat([]byte("concurrent-"), 500)
	manifest := makeManifest(body, manifestOpts{})
	rec := &recorder{}
	store, _ := newStore(t, blobHandler(body, false, rec), nil, nil)

	var wg sync.WaitGroup
	results := make([]string, 2)
	errs := make([]error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = store.Ensure(context.Background(), manifest)
		}(i)
	}
	wg.Wait()

	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if results[0] != results[1] {
		t.Fatalf("paths differ: %q vs %q", results[0], results[1])
	}
	if rec.calls() != 1 {
		t.Fatalf("requests = %d, want 1 (per-model lock should collapse the second fetch)", rec.calls())
	}
}

func strings64(c byte) string {
	b := make([]byte, 64)
	for i := range b {
		b[i] = c
	}
	return string(b)
}
