package modelcache_test

// mmproj companion handling. Ported 1:1 from test_modelcache_mmproj.py.

import (
	"bytes"
	"context"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const mmprojName = "mmproj.gguf"

func mmprojManifest(body, mmproj []byte) protocol.ModelManifest {
	m := makeManifest(body, manifestOpts{})
	name := mmprojName
	sha := sha256Hex(mmproj)
	size := len(mmproj)
	m.MmprojFileName = &name
	m.MmprojSHA256 = &sha
	m.MmprojSizeBytes = &size
	return m
}

// dualHandler serves the main blob on /blob and the companion on /mmproj.
func dualHandler(body, mmproj []byte) http.HandlerFunc {
	main := blobHandler(body, false, nil)
	companion := blobHandler(mmproj, false, nil)
	return func(w http.ResponseWriter, req *http.Request) {
		if strings.HasSuffix(req.URL.Path, "/mmproj") {
			companion(w, req)
			return
		}
		main(w, req)
	}
}

func TestEnsureDownloadsAndVerifiesTheMmproj(t *testing.T) {
	body := bytes.Repeat([]byte("main"), 1000)
	mmproj := bytes.Repeat([]byte("proj"), 500)
	manifest := mmprojManifest(body, mmproj)
	store, dir := newStore(t, dualHandler(body, mmproj), nil, nil)

	if _, err := store.Ensure(context.Background(), manifest); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(dir, modelID, mmprojName))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, mmproj) {
		t.Fatal("mmproj bytes differ from body")
	}
	marker, err := os.ReadFile(filepath.Join(dir, modelID, mmprojName+".sha256"))
	if err != nil {
		t.Fatal(err)
	}
	if string(marker) != sha256Hex(mmproj) {
		t.Fatalf("mmproj marker = %q, want %q", marker, sha256Hex(mmproj))
	}
	if _, ok := store.PathIfPresent(manifest); !ok {
		t.Fatal("manifest should be present after ensure")
	}
}

func TestMissingMmprojIsRefetchedWhenMainIsCached(t *testing.T) {
	body := bytes.Repeat([]byte("main"), 1000)
	mmproj := bytes.Repeat([]byte("proj"), 500)
	manifest := mmprojManifest(body, mmproj)
	store, dir := newStore(t, dualHandler(body, mmproj), nil, nil)

	if _, err := store.Ensure(context.Background(), manifest); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if err := os.Remove(filepath.Join(dir, modelID, mmprojName)); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, modelID, mmprojName+".sha256")); err != nil {
		t.Fatal(err)
	}

	if _, ok := store.PathIfPresent(manifest); ok {
		t.Fatal("manifest must not be present without its mmproj")
	}
	if _, err := store.Ensure(context.Background(), manifest); err != nil {
		t.Fatalf("re-ensure: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dir, modelID, mmprojName))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, mmproj) {
		t.Fatal("mmproj bytes differ after refetch")
	}
}

func TestMmprojHashMismatchFailsEnsure(t *testing.T) {
	body := bytes.Repeat([]byte("main"), 1000)
	mmproj := bytes.Repeat([]byte("proj"), 500)
	manifest := mmprojManifest(body, mmproj)
	wrong := strings.Repeat("f", 64)
	manifest.MmprojSHA256 = &wrong
	store, _ := newStore(t, dualHandler(body, mmproj), nil, nil)

	if _, err := store.Ensure(context.Background(), manifest); err == nil {
		t.Fatal("ensure must fail on an mmproj hash mismatch")
	}
	if _, ok := store.PathIfPresent(manifest); ok {
		t.Fatal("manifest must not be present after a failed mmproj verify")
	}
}
