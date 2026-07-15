package modelcache_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
)

// Download-path tests: full fetch, resume, and Range-ignored restart. Ported
// 1:1 from test_modelcache_download.py.

func TestFullDownloadWritesVerifiedBlob(t *testing.T) {
	body := bytes.Repeat([]byte("gguf-bytes-"), 5000)
	manifest := makeManifest(body, manifestOpts{})
	store, dir := newStore(t, blobHandler(body, false, nil), nil, nil)

	path, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}

	wantPath := filepath.Join(dir, modelID, fileName)
	if path != wantPath {
		t.Fatalf("path = %q, want %q", path, wantPath)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, body) {
		t.Fatal("blob bytes differ from body")
	}
	marker, err := os.ReadFile(filepath.Join(dir, modelID, fileName+".sha256"))
	if err != nil {
		t.Fatal(err)
	}
	if string(marker) != sha256Hex(body) {
		t.Fatalf("marker = %q, want %q", marker, sha256Hex(body))
	}
	if _, err := os.Stat(filepath.Join(dir, modelID, fileName+".part")); !os.IsNotExist(err) {
		t.Fatal(".part file should not remain")
	}
	if p, ok := store.PathIfPresent(manifest); !ok || p != path {
		t.Fatalf("PathIfPresent = %q,%v want %q,true", p, ok, path)
	}
}

func TestEnsureReturnsCachedPathWithoutSecondDownload(t *testing.T) {
	body := bytes.Repeat([]byte("cached"), 1000)
	manifest := makeManifest(body, manifestOpts{})
	rec := &recorder{}
	store, _ := newStore(t, blobHandler(body, false, rec), nil, nil)

	first, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("paths differ: %q vs %q", first, second)
	}
	if rec.calls() != 1 {
		t.Fatalf("requests = %d, want 1", rec.calls())
	}
}

func TestResumeSendsRangeAndAppends(t *testing.T) {
	body := make([]byte, 1024000) // 1,024,000 bytes: spans several 1 MiB chunks
	for i := range body {
		body[i] = byte(i % 256)
	}
	manifest := makeManifest(body, manifestOpts{})
	rec := &recorder{}
	store, dir := newStore(t, blobHandler(body, false, rec), nil, nil)

	const prefixLen = 1000
	modelDir := filepath.Join(dir, modelID)
	if err := os.MkdirAll(modelDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(modelDir, fileName+".part"), body[:prefixLen], 0o644); err != nil {
		t.Fatal(err)
	}

	path, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, body) {
		t.Fatal("resumed blob bytes differ from body")
	}
	if want := "bytes=1000-"; rec.firstRange() != want {
		t.Fatalf("Range = %q, want %q", rec.firstRange(), want)
	}
}

func TestRangeIgnoredRestartsFromZero(t *testing.T) {
	body := bytes.Repeat([]byte("complete-body-"), 2000)
	manifest := makeManifest(body, manifestOpts{})
	store, dir := newStore(t, blobHandler(body, true, nil), nil, nil)

	modelDir := filepath.Join(dir, modelID)
	if err := os.MkdirAll(modelDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// A stale partial that is NOT a prefix of body; a 200 response must discard it.
	if err := os.WriteFile(filepath.Join(modelDir, fileName+".part"), []byte("STALE-PARTIAL-DATA"), 0o644); err != nil {
		t.Fatal(err)
	}

	path, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, body) {
		t.Fatal("restarted blob bytes differ from body")
	}
}
