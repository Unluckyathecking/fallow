// Package discovery — mdns.go is the production Lookup: one bounded multicast
// query over github.com/hashicorp/mdns, whose one-shot QueryContext matches this
// contract without the browse machinery a continuous-discovery library carries.
package discovery

import (
	"context"
	"io"
	"log"

	"github.com/hashicorp/mdns"
)

// entryBuffer sizes the answer channel. hashicorp/mdns never blocks on a send
// and drops answers a full channel cannot take, so the buffer is what bounds how
// many answers one query can surface. It sits well above MaxCandidates so a
// legitimate coordinator is not crowded out by a noisy segment.
const entryBuffer = 64

// quietLogger discards the library's per-packet error logging. Multicast loss,
// truncated answers and refused sockets are normal on a school VLAN; the outcome
// of a query is reported to the caller as a typed diagnostic instead, so nothing
// is silently swallowed while the daemon log stays readable.
var quietLogger = log.New(io.Discard, "", 0)

// multicastLookup runs one bounded query and returns the answers it collected.
//
// A dual-stack query fails outright on a host with no IPv6 route, because the
// library aborts the whole query when its IPv6 send fails. That is an ordinary
// school-network configuration, not a reason to report the segment as silent, so
// one IPv4-only attempt follows. The retry costs nothing measurable: the failure
// is a refused send at the start of the query, not an elapsed bound. IPv6
// addresses carried inside the answers are still returned either way; only the
// transport narrows.
func multicastLookup(ctx context.Context, q Query) ([]Entry, error) {
	entries, err := queryOnce(ctx, q, false)
	if err != nil && ctx.Err() == nil {
		entries, err = queryOnce(ctx, q, true)
	}
	return entries, err
}

// queryOnce performs a single bounded query. QueryContext blocks until the
// timeout elapses or ctx is done and tears its sockets down before returning, so
// the call leaves no goroutine behind and the channel can be drained afterwards
// without a reader running alongside it.
func queryOnce(ctx context.Context, q Query, ipv4Only bool) ([]Entry, error) {
	answers := make(chan *mdns.ServiceEntry, entryBuffer)
	err := mdns.QueryContext(ctx, &mdns.QueryParam{
		Service:     q.Service,
		Domain:      q.Domain,
		Timeout:     q.Timeout,
		Entries:     answers,
		DisableIPv6: ipv4Only,
		Logger:      quietLogger,
	})
	close(answers)
	if err != nil {
		return nil, err
	}
	var out []Entry
	for a := range answers {
		out = append(out, Entry{
			Name:   a.Name,
			AddrV4: a.AddrV4,
			AddrV6: a.AddrV6,
			Port:   a.Port,
			TXT:    a.InfoFields,
		})
	}
	return out, nil
}
