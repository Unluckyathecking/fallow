package modelcache_test

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/modelcache"
)

// Verification and retry/backoff tests. Ported 1:1 from test_modelcache_verify.py.

func TestHashMismatchDeletesPartAndRaises(t *testing.T) {
	body := bytes.Repeat([]byte("actual-bytes-"), 100)
	manifest := makeManifest(body, manifestOpts{sha256: strings64('0')}) // valid format, wrong digest
	store, dir := newStore(t, blobHandler(body, false, nil), nil, nil)

	_, err := store.Ensure(context.Background(), manifest)
	if !errors.Is(err, modelcache.ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if _, err := os.Stat(filepath.Join(dir, modelID, fileName+".part")); !os.IsNotExist(err) {
		t.Fatal(".part should be deleted")
	}
	if _, err := os.Stat(filepath.Join(dir, modelID, fileName)); !os.IsNotExist(err) {
		t.Fatal("blob should not exist")
	}
}

func TestSizeMismatchRaises(t *testing.T) {
	body := bytes.Repeat([]byte("x"), 500)
	manifest := makeManifest(body, manifestOpts{sizeBytes: 999, setSize: true}) // sha correct, size wrong
	store, _ := newStore(t, blobHandler(body, false, nil), nil, nil)

	_, err := store.Ensure(context.Background(), manifest)
	if !errors.Is(err, modelcache.ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
}

func TestRetriesThenSucceeds(t *testing.T) {
	body := bytes.Repeat([]byte("payload-"), 200)
	manifest := makeManifest(body, manifestOpts{})
	var calls int32
	handler := func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) <= 2 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}
	var sleeps []time.Duration
	store, _ := newStore(t, handler, &sleeps, nil)

	path, err := store.Ensure(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, body) {
		t.Fatal("blob bytes differ")
	}
	if got := atomic.LoadInt32(&calls); got != 3 {
		t.Fatalf("calls = %d, want 3", got)
	}
	want := []time.Duration{500 * time.Millisecond, 1000 * time.Millisecond}
	if len(sleeps) != len(want) || sleeps[0] != want[0] || sleeps[1] != want[1] {
		t.Fatalf("sleeps = %v, want %v (exponential backoff off a 0.5s base)", sleeps, want)
	}
}

func TestExhaustsRetriesRaisesFetchError(t *testing.T) {
	body := bytes.Repeat([]byte("z"), 32)
	manifest := makeManifest(body, manifestOpts{})
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	cfg := modelcache.DefaultConfig()
	cfg.MaxRetries = 2
	store, _ := newStore(t, handler, nil, &cfg)

	_, err := store.Ensure(context.Background(), manifest)
	if !errors.Is(err, modelcache.ErrFetch) {
		t.Fatalf("err = %v, want ErrFetch", err)
	}
}

func TestTransportErrorIsRetriedThenFails(t *testing.T) {
	body := bytes.Repeat([]byte("z"), 32)
	manifest := makeManifest(body, manifestOpts{})
	var calls int32
	handler := func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		hj, ok := w.(http.Hijacker)
		if !ok {
			http.Error(w, "no hijack", http.StatusInternalServerError)
			return
		}
		conn, _, err := hj.Hijack()
		if err != nil {
			return
		}
		_ = conn.Close() // abort the connection -> transport error on the client
	}
	cfg := modelcache.DefaultConfig()
	cfg.MaxRetries = 1
	store, _ := newStore(t, handler, nil, &cfg)

	_, err := store.Ensure(context.Background(), manifest)
	if !errors.Is(err, modelcache.ErrFetch) {
		t.Fatalf("err = %v, want ErrFetch", err)
	}
	if got := atomic.LoadInt32(&calls); got != 2 { // initial attempt + one retry
		t.Fatalf("calls = %d, want 2", got)
	}
}
