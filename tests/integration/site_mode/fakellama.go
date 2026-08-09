// Command fakellama is a controlled, loopback-only stand-in for llama-server used
// by the static LAN Site Mode acceptance harness. It accepts the same argv the Go
// supervisor builds (-m MODEL --port P --host H --parallel N -c N plus any manifest
// default args), binds the requested loopback address, and answers the health
// probe and the two OpenAI routes the relay carries. It is a single stdlib-only
// file so the harness can `go build` it with the same toolchain as agentctl, giving
// a native executable on every platform (a .py cannot be spawned as argv[0] on
// Windows). Behaviour is deterministic and steered per request via _fake_mode.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func isLoopback(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// parseArgs pulls --host/--port out of the supervisor-built argv, ignoring the
// rest (-m, --parallel, -c, and any manifest default args).
func parseArgs(args []string) (host string, port int, err error) {
	host = "127.0.0.1"
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--host":
			if i+1 < len(args) {
				host = args[i+1]
				i++
			}
		case "--port":
			if i+1 < len(args) {
				port, err = strconv.Atoi(args[i+1])
				if err != nil {
					return "", 0, err
				}
				i++
			}
		}
	}
	if port == 0 {
		return "", 0, fmt.Errorf("--port is required")
	}
	return host, port, nil
}

func readBody(r *http.Request) map[string]any {
	out := map[string]any{}
	data, err := io.ReadAll(io.LimitReader(r.Body, 4*1024*1024))
	if err == nil && len(data) > 0 {
		_ = json.Unmarshal(data, &out)
	}
	return out
}

func str(m map[string]any, key, def string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return def
}

func main() {
	host, port, err := parseArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "fakellama:", err)
		os.Exit(2)
	}
	if !isLoopback(host) {
		fmt.Fprintf(os.Stderr, "fakellama refuses non-loopback host %q\n", host)
		os.Exit(2)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok"}`)
	})
	mux.HandleFunc("/v1/embeddings", func(w http.ResponseWriter, r *http.Request) {
		_ = readBody(r)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"object":"list","data":[{"embedding":[0.5,0.25],"index":0}]}`)
	})
	mux.HandleFunc("/v1/chat/completions", func(w http.ResponseWriter, r *http.Request) {
		payload := readBody(r)
		if stream, _ := payload["stream"].(bool); stream {
			serveSSE(w, payload)
			return
		}
		echo := str(payload, "_echo", "hello")
		w.Header().Set("Content-Type", "application/json")
		body := map[string]any{
			"id": "fake-chat", "object": "chat.completion",
			"choices": []any{map[string]any{
				"message": map[string]any{"role": "assistant", "content": echo},
			}},
		}
		_ = json.NewEncoder(w).Encode(body)
	})

	server := &http.Server{Addr: net.JoinHostPort(host, strconv.Itoa(port)), Handler: mux}
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "fakellama:", err)
		os.Exit(1)
	}
}

func serveSSE(w http.ResponseWriter, payload map[string]any) {
	flusher, ok := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	mode := str(payload, "_fake_mode", "")
	echo := str(payload, "_echo", "Hello")

	if mode == "truncate" {
		// Emit one complete, deliberately large first event so the relaying agent
		// flushes it across the relay, hold briefly so the client receives it, then
		// return without a terminator — the first byte is already out, so the client
		// must see a clean truncation and never a retry.
		padding := strings.Repeat("x", 16000)
		io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\""+padding+"\"}}]}\n\n")
		if ok {
			flusher.Flush()
		}
		time.Sleep(500 * time.Millisecond)
		return
	}

	head := echo
	tail := ""
	if len(echo) >= 2 {
		head = echo[:2]
		tail = echo[2:]
	}
	events := []string{
		"data: {\"choices\":[{\"delta\":{\"content\":\"" + head + "\"}}]}\n\n",
		"data: {\"choices\":[{\"delta\":{\"content\":\"" + tail + "\"}}]}\n\n",
		"data: [DONE]\n\n",
	}
	for _, ev := range events {
		io.WriteString(w, ev)
		if ok {
			flusher.Flush()
		}
		if mode == "slow" {
			time.Sleep(20 * time.Millisecond)
		}
	}
}
