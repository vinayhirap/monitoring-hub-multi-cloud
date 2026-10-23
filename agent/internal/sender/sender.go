// Package sender posts INGEST PAYLOAD v1 bodies to the hub and classifies
// every failure so the runner knows whether to retry, drop the payload, or
// stop altogether.
package sender

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"

	"cloudops-agent/internal/payload"
)

const (
	IngestPath     = "/api/host-agents/ingest"
	RequestTimeout = 10 * time.Second
	maxRetryAfter  = time.Hour
	maxRespBytes   = 1 << 20
)

// Kind tells the caller what to do with a failed send.
type Kind int

const (
	// Retry: transient (network, 5xx, 429, 404 while the hub feature is off...).
	// Keep the payload and try again later.
	Retry Kind = iota
	// Drop: the hub rejected this payload as invalid (400/413/422). Retrying
	// the same bytes can never succeed and would block everything behind it.
	Drop
	// Unauthorized: 401. The token is unknown/revoked. Stop; do not hammer.
	Unauthorized
)

// Error is returned for every failed Ingest. Its text never contains the token.
type Error struct {
	Kind       Kind
	Status     int           // 0 for transport errors
	RetryAfter time.Duration // server hint (429/503), already capped
	Msg        string
}

func (e *Error) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("hub returned HTTP %d: %s", e.Status, e.Msg)
	}
	return e.Msg
}

// Client talks to one hub with one agent token.
type Client struct {
	BaseURL   string
	Token     string
	HTTP      *http.Client
	Gzip      bool
	UserAgent string
}

// New builds a Client with a 10 s timeout that never follows redirects
// (a redirect could carry the bearer token to another host or downgrade it
// to http) and honours HTTP(S)_PROXY from the environment.
func New(baseURL, token string, gzipOn bool, version string) *Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConns = 1
	tr.MaxIdleConnsPerHost = 1
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP: &http.Client{
			Timeout:       RequestTimeout,
			Transport:     tr,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		Gzip:      gzipOn,
		UserAgent: "cloudops-agent/" + version,
	}
}

// Ingest sends one raw JSON payload body.
func (c *Client) Ingest(ctx context.Context, jsonBody []byte) (payload.IngestResponse, error) {
	var out payload.IngestResponse
	body := jsonBody
	if c.Gzip {
		var buf bytes.Buffer
		zw := gzip.NewWriter(&buf)
		if _, err := zw.Write(jsonBody); err != nil {
			return out, &Error{Kind: Drop, Msg: "gzip: " + err.Error()}
		}
		if err := zw.Close(); err != nil {
			return out, &Error{Kind: Drop, Msg: "gzip: " + err.Error()}
		}
		body = buf.Bytes()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+IngestPath, bytes.NewReader(body))
	if err != nil {
		return out, &Error{Kind: Drop, Msg: "build request: " + err.Error()}
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", c.UserAgent)
	if c.Gzip {
		req.Header.Set("Content-Encoding", "gzip")
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		// *url.Error carries the URL, never request headers, so no token here.
		return out, &Error{Kind: Retry, Msg: "request failed: " + sanitize(err.Error(), c.Token)}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxRespBytes))

	switch {
	case resp.StatusCode == http.StatusOK:
		if len(bytes.TrimSpace(raw)) > 0 {
			if jerr := json.Unmarshal(raw, &out); jerr != nil {
				out = payload.IngestResponse{} // accepted, but nothing usable to apply
			}
		}
		return out, nil
	case resp.StatusCode == http.StatusUnauthorized:
		return out, &Error{Kind: Unauthorized, Status: 401, Msg: detail(raw)}
	case resp.StatusCode == http.StatusBadRequest, resp.StatusCode == http.StatusRequestEntityTooLarge,
		resp.StatusCode == http.StatusUnprocessableEntity:
		return out, &Error{Kind: Drop, Status: resp.StatusCode, Msg: detail(raw)}
	default:
		return out, &Error{
			Kind: Retry, Status: resp.StatusCode,
			RetryAfter: ParseRetryAfter(resp.Header.Get("Retry-After"), time.Now()),
			Msg:        detail(raw),
		}
	}
}

// ParseRetryAfter accepts delta-seconds or an HTTP date, capped at one hour.
// Unparseable or past values give 0 (use normal backoff).
func ParseRetryAfter(v string, now time.Time) time.Duration {
	v = strings.TrimSpace(v)
	if v == "" {
		return 0
	}
	var d time.Duration
	if n, err := strconv.Atoi(v); err == nil {
		d = time.Duration(n) * time.Second
	} else if t, err := http.ParseTime(v); err == nil {
		d = t.Sub(now)
	}
	if d < 0 {
		return 0
	}
	if d > maxRetryAfter {
		return maxRetryAfter
	}
	return d
}

// detail extracts a short human message from a FastAPI-style error body.
func detail(raw []byte) string {
	var e struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal(raw, &e) == nil && e.Detail != nil {
		if s, ok := e.Detail.(string); ok {
			return trunc(s)
		}
		b, _ := json.Marshal(e.Detail)
		return trunc(string(b))
	}
	return trunc(strings.TrimSpace(string(raw)))
}

func trunc(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > 200 {
		return s[:200] + "..."
	}
	return s
}

func sanitize(s, token string) string {
	if token != "" {
		s = strings.ReplaceAll(s, token, "[redacted]")
	}
	return s
}

// Backoff is exponential with "equal jitter": each delay is uniform in
// [d/2, d] where d doubles from Base up to Max. A server Retry-After longer
// than that wins (plus up to 10% jitter so a fleet does not return in lockstep).
type Backoff struct {
	Base, Max time.Duration
	n         int
	Rand      func() float64 // [0,1); injectable for tests
}

func NewBackoff() *Backoff {
	return &Backoff{Base: 2 * time.Second, Max: 5 * time.Minute, Rand: rand.Float64}
}

func (b *Backoff) Next(retryAfter time.Duration) time.Duration {
	if b.Rand == nil {
		b.Rand = rand.Float64
	}
	d := b.Base
	for i := 0; i < b.n && d < b.Max; i++ {
		d *= 2
	}
	if d > b.Max {
		d = b.Max
	}
	if b.n < 30 {
		b.n++
	}
	delay := d/2 + time.Duration(b.Rand()*float64(d/2))
	if retryAfter > delay {
		delay = retryAfter + time.Duration(b.Rand()*float64(retryAfter/10))
	}
	return delay
}

func (b *Backoff) Reset() { b.n = 0 }

// Attempts is how many consecutive failures have been counted.
func (b *Backoff) Attempts() int { return b.n }
