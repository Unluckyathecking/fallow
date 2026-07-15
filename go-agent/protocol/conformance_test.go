package protocol

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestGoldenJSONRoundTrips(t *testing.T) {
	t.Parallel()

	fixtureDir := filepath.Join("..", "..", "schemas", "fixtures")
	fixtures := map[string]func() any{
		"agent_event.json":        func() any { return &AgentEvent{} },
		"heartbeat.json":          func() any { return &Heartbeat{} },
		"heartbeat_response.json": func() any { return &HeartbeatResponse{} },
		"model_manifest.json":     func() any { return &ModelManifest{} },
		"register_request.json":   func() any { return &RegisterRequest{} },
		"work_result.json":        func() any { return &WorkResult{} },
		"work_unit_lease.json":    func() any { return &WorkUnitLease{} },
	}
	entries, err := os.ReadDir(fixtureDir)
	if err != nil {
		t.Fatal(err)
	}
	jsonFiles := 0
	for _, entry := range entries {
		if filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		jsonFiles++
		if _, ok := fixtures[entry.Name()]; !ok {
			t.Fatalf("fixture has no Go target type: %s", entry.Name())
		}
	}
	if jsonFiles != len(fixtures) {
		t.Fatalf("fixture mapping covers %d of %d JSON files", len(fixtures), jsonFiles)
	}

	for name, newValue := range fixtures {
		name, newValue := name, newValue
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			path := filepath.Join(fixtureDir, name)
			golden, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}

			value := newValue()
			if err := json.Unmarshal(golden, value); err != nil {
				t.Fatalf("decode golden: %v", err)
			}
			roundTrip, err := json.Marshal(value)
			if err != nil {
				t.Fatalf("encode value: %v", err)
			}

			var want, got any
			if err := json.Unmarshal(golden, &want); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(roundTrip, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("round-trip mismatch\nwant: %s\n got: %s", golden, roundTrip)
			}
		})
	}
}

func TestOptionalCollectionsAreOmittedWhenEmpty(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		value  any
		absent []string
	}{
		{"heartbeat", Heartbeat{}, []string{"gpus", "lease_ids", "replicas"}},
		{"agent event", AgentEvent{}, []string{"detail"}},
		{"job submit", JobSubmit{}, []string{"params"}},
	}
	for _, test := range tests {
		encoded, err := json.Marshal(test.value)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(encoded, &fields); err != nil {
			t.Fatal(err)
		}
		for _, name := range test.absent {
			if _, present := fields[name]; present {
				t.Errorf("optional field %q was encoded in zero-value %s: %s", name, test.name, encoded)
			}
		}
	}
}
