package reconcile

import (
	"context"
	"errors"
	"github.com/Unluckyathecking/fallow/go-agent/protocol"
	"testing"
)

type fakeSource struct {
	m     protocol.ModelManifest
	err   error
	calls int
}

func (f *fakeSource) Manifest(context.Context, string) (protocol.ModelManifest, error) {
	f.calls++
	return f.m, f.err
}

type fakeCache struct {
	path  string
	err   error
	calls int
}

func (f *fakeCache) Ensure(context.Context, protocol.ModelManifest) (string, error) {
	f.calls++
	return f.path, f.err
}

type fakeSup struct {
	statuses []protocol.ReplicaStatus
	starts   []int
	stops    []string
	startErr error
}

func (f *fakeSup) Statuses() []protocol.ReplicaStatus {
	return append([]protocol.ReplicaStatus(nil), f.statuses...)
}
func (f *fakeSup) StartReplica(protocol.ModelManifest, string, int) error {
	f.starts = append(f.starts, int(1))
	return f.startErr
}
func (f *fakeSup) StopReplica(id string) { f.stops = append(f.stops, id) }
func TestApplyAddRemoveAndIdempotent(t *testing.T) {
	src := &fakeSource{m: protocol.ModelManifest{ModelID: "a"}}
	cache := &fakeCache{path: "/cache/a"}
	sup := &fakeSup{}
	r, _ := New(src, cache, sup, PortRange{Start: 8100, Count: 2})
	if err := r.Apply(context.Background(), []string{"a"}); err != nil {
		t.Fatal(err)
	}
	if len(sup.starts) != 1 {
		t.Fatalf("starts=%d", len(sup.starts))
	}
	if err := r.Apply(context.Background(), []string{"a"}); err != nil {
		t.Fatal(err)
	}
	sup.statuses = []protocol.ReplicaStatus{{ModelID: "a", Port: 8100, State: protocol.ReplicaStateReady}}
	if err := r.Apply(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if len(sup.stops) != 1 {
		t.Fatalf("stops=%d", len(sup.stops))
	}
}
func TestApplyPortExhaustion(t *testing.T) {
	src := &fakeSource{m: protocol.ModelManifest{ModelID: "b"}}
	c := &fakeCache{path: "x"}
	s := &fakeSup{statuses: []protocol.ReplicaStatus{{ModelID: "a", Port: 1, State: protocol.ReplicaStateReady}}}
	r, _ := New(src, c, s, PortRange{Start: 1, Count: 1})
	if err := r.Apply(context.Background(), []string{"a", "b"}); !errors.Is(err, ErrPortExhausted) {
		t.Fatalf("err=%v", err)
	}
}
