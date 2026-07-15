package coordinator

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

const (
	testBaseURL = "http://coordinator.test"
	testAgentID = "agent-42"
	testToken   = "device-secret"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) Do(request *http.Request) (*http.Response, error) {
	return function(request)
}

func response(status int, body any) *http.Response {
	var reader io.ReadCloser = http.NoBody
	if body != nil {
		payload, _ := json.Marshal(body)
		reader = io.NopCloser(strings.NewReader(string(payload)))
	}
	return &http.Response{StatusCode: status, Body: reader, Header: make(http.Header)}
}

func testClient(doer HTTPDoer) *Client {
	client := NewClient(testBaseURL, doer, testAgentID, testToken)
	client.SetRetry(RetryConfig{MaxRetries: 2, Backoff: time.Millisecond}, func(
		context.Context, time.Duration,
	) error {
		return nil
	})
	return client
}

func sampleHeartbeat() protocol.Heartbeat {
	return protocol.Heartbeat{
		AgentID:         testAgentID,
		GPUs:            []protocol.GpuStatus{},
		LeaseIDs:        []string{},
		ProtocolVersion: 1,
		Replicas:        []protocol.ReplicaStatus{},
		SentAt:          time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC),
		State:           protocol.AgentStateIdle,
	}
}

func TestRegisterStoresIdentityAndSendsNoBearer(t *testing.T) {
	var authorization string
	client := NewClient(testBaseURL, roundTripFunc(func(request *http.Request) (*http.Response, error) {
		authorization = request.Header.Get("Authorization")
		if request.URL.Path != "/v1/agents/register" {
			t.Fatalf("path = %q", request.URL.Path)
		}
		return response(http.StatusCreated, protocol.RegisterResponse{
			AgentID: testAgentID, DeviceToken: testToken, Config: DefaultAgentConfig(),
		}), nil
	}), "", "")

	got, err := client.Register(context.Background(), protocol.RegisterRequest{
		EnrollmentToken: "enroll-once",
		ProtocolVersion: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if authorization != "" {
		t.Fatalf("registration sent Authorization %q", authorization)
	}
	if got.AgentID != testAgentID || client.AgentID() != testAgentID || client.DeviceToken() != testToken {
		t.Fatalf("identity was not stored: %#v", got)
	}
}

func TestHeartbeatSendsBearerAndParsesResponse(t *testing.T) {
	client := testClient(roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.Header.Get("Authorization") != "Bearer "+testToken {
			t.Fatalf("Authorization = %q", request.Header.Get("Authorization"))
		}
		return response(http.StatusOK, protocol.HeartbeatResponse{
			DesiredModels: []string{"qwen"}, RevokedLeaseIDs: []string{"lease-1"},
		}), nil
	}))

	got, err := client.Heartbeat(context.Background(), sampleHeartbeat())
	if err != nil {
		t.Fatal(err)
	}
	if len(got.DesiredModels) != 1 || got.DesiredModels[0] != "qwen" {
		t.Fatalf("response = %#v", got)
	}
}

func TestHeartbeatAcceptsBodyStatuses(t *testing.T) {
	for _, status := range []int{http.StatusOK, http.StatusCreated} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			client := testClient(roundTripFunc(func(*http.Request) (*http.Response, error) {
				return response(status, protocol.HeartbeatResponse{}), nil
			}))
			if _, err := client.Heartbeat(context.Background(), sampleHeartbeat()); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestPollWorkStatusAndTimeout(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   any
		nil    bool
	}{
		{name: "no work", status: http.StatusNoContent, nil: true},
		{name: "lease", status: http.StatusOK, body: protocol.WorkUnitLease{
			Attempt: 1, JobID: "job-1", Kind: protocol.WorkerKindEmbed,
			LeaseExpires: time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC),
			ModelID:      "bge", WorkUnitID: "unit-1",
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := testClient(roundTripFunc(func(request *http.Request) (*http.Response, error) {
				if request.URL.Query().Get("timeout") != "30.0" {
					t.Fatalf("timeout = %q", request.URL.Query().Get("timeout"))
				}
				return response(test.status, test.body), nil
			}))
			lease, err := client.PollWork(context.Background(), 30*time.Second)
			if err != nil {
				t.Fatal(err)
			}
			if (lease == nil) != test.nil {
				t.Fatalf("lease = %#v", lease)
			}
		})
	}
}

func TestFireAndForgetAcceptedStatuses(t *testing.T) {
	for _, status := range []int{200, 201, 202, 204} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			client := testClient(roundTripFunc(func(request *http.Request) (*http.Response, error) {
				if request.URL.Path != "/v1/agents/agent-42/events" {
					t.Fatalf("path = %q", request.URL.Path)
				}
				return response(status, nil), nil
			}))
			err := client.PushEvent(context.Background(), protocol.AgentEvent{
				AgentID: testAgentID, At: time.Now(), Detail: map[string]string{},
				Kind: protocol.EventKindUserIdle,
			})
			if err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestCompleteUnitSendsAttemptHeader(t *testing.T) {
	client := testClient(roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.Header.Get(leaseAttemptHeader) != "3" {
			t.Fatalf("attempt = %q", request.Header.Get(leaseAttemptHeader))
		}
		if request.URL.Path != "/v1/agents/agent-42/work_units/unit-9/result" {
			t.Fatalf("path = %q", request.URL.Path)
		}
		return response(http.StatusNoContent, nil), nil
	}))
	err := client.CompleteUnit(context.Background(), protocol.WorkResult{
		Status: protocol.WorkResultStatusSucceeded, WorkUnitID: "unit-9",
	}, 3)
	if err != nil {
		t.Fatal(err)
	}
}

func TestStatusClassification(t *testing.T) {
	tests := []struct {
		status int
		kind   string
	}{
		{status: 401, kind: "auth"},
		{status: 403, kind: "auth"},
		{status: 500, kind: "transient"},
		{status: 418, kind: "protocol"},
	}
	for _, test := range tests {
		t.Run(http.StatusText(test.status), func(t *testing.T) {
			client := testClient(roundTripFunc(func(*http.Request) (*http.Response, error) {
				return response(test.status, nil), nil
			}))
			_, err := client.Heartbeat(context.Background(), sampleHeartbeat())
			matched := false
			switch test.kind {
			case "auth":
				matched = errors.As(err, new(*AuthError))
			case "transient":
				matched = errors.As(err, new(*TransientError))
			case "protocol":
				matched = errors.As(err, new(*ProtocolError))
			}
			if !matched {
				t.Fatalf("error %T, want %s", err, test.kind)
			}
		})
	}
}

func TestMalformedBodyIsProtocolError(t *testing.T) {
	client := testClient(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       io.NopCloser(strings.NewReader("{not json")),
		}, nil
	}))
	_, err := client.Heartbeat(context.Background(), sampleHeartbeat())
	var protocolError *ProtocolError
	if !errors.As(err, &protocolError) {
		t.Fatalf("error = %T %v", err, err)
	}
}

func TestIdempotentTransportErrorsRetry(t *testing.T) {
	calls := 0
	client := testClient(roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		if calls < 3 {
			return nil, errors.New("offline")
		}
		return response(http.StatusOK, protocol.HeartbeatResponse{}), nil
	}))
	if _, err := client.Heartbeat(context.Background(), sampleHeartbeat()); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3", calls)
	}
}

func TestRegisterAndEventTransportErrorsAreNotRetried(t *testing.T) {
	for _, operation := range []string{"register", "event"} {
		t.Run(operation, func(t *testing.T) {
			calls := 0
			client := testClient(roundTripFunc(func(*http.Request) (*http.Response, error) {
				calls++
				return nil, errors.New("offline")
			}))
			if operation == "register" {
				_, _ = client.Register(context.Background(), protocol.RegisterRequest{})
			} else {
				_ = client.PushEvent(context.Background(), protocol.AgentEvent{
					AgentID: testAgentID, At: time.Now(), Kind: protocol.EventKindUserIdle,
				})
			}
			if calls != 1 {
				t.Fatalf("calls = %d, want 1", calls)
			}
		})
	}
}

func TestAuthedCallRequiresIdentity(t *testing.T) {
	client := NewClient(testBaseURL, roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("request should not be sent")
		return nil, nil
	}), "", "")
	_, err := client.PollWork(context.Background(), time.Second)
	var protocolError *ProtocolError
	if !errors.As(err, &protocolError) {
		t.Fatalf("error = %T %v", err, err)
	}
}
