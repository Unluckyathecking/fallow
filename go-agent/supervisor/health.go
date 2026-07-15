package supervisor

import (
	"net"
	"net/http"
	"strconv"
	"time"
)

const httpOK = 200

// HealthCheck reports whether the replica at host:port answers healthily. It is
// injected into the supervisor so unit tests never perform real HTTP.
type HealthCheck func(host string, port int, path string, timeout time.Duration) bool

// HTTPHealthCheck issues a plain GET against http://host:port/path and returns
// true only on a 200 response. Any connection error, timeout, or non-200 status
// yields false; the caller keeps polling until the startup timeout elapses.
func HTTPHealthCheck(host string, port int, path string, timeout time.Duration) bool {
	client := &http.Client{Timeout: timeout}
	url := "http://" + net.JoinHostPort(host, strconv.Itoa(port)) + path
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == httpOK
}
