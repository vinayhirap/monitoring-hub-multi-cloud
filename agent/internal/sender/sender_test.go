package sender

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const tok = "cah_SECRETSECRETSECRETSECRETSECRETSECRET1"

func asErr(t *testing.T, err error) *Error {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) {
		t.Fatalf("want *sender.Error, got %T %v", err, err)
	}
	return e
}

func TestIngestSuccessGzipAndHeaders(t *testing.T) {
	var gotAuth, gotEnc, gotCT, gotUA, gotPath string
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth, gotEnc, gotCT, gotUA, gotPath = r.Header.Get("Authorization"), r.Header.Get("Content-Encoding"), r.Header.Get("Content-Type"), r.Header.Get("User-Agent"), r.URL.Path
		zr, err := gzip.NewReader(r.Body)
		if err != nil {
			t.Errorf("not gzip: %v", err)
			return
		}
		gotBody, _ = io.ReadAll(zr)
		_ = json.NewEncoder(w).Encode(map[string]any{"accepted": 2, "rejected": 1, "config_version": 4,
			"config": map[string]any{"interval_seconds": 30, "watch_services": []string{"nginx"}, "max_batch_metrics": 100}})
	}))
	defer srv.Close()

	c := New(srv.URL+"/", tok, true, "9.9.9")
	resp, err := c.Ingest(context.Background(), []byte(`{"schema":1}`))
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != IngestPath || gotAuth != "Bearer "+tok || gotEnc != "gzip" || gotCT != "application/json" || gotUA != "cloudops-agent/9.9.9" {
		t.Errorf("headers: path=%q auth-ok=%v enc=%q ct=%q ua=%q", gotPath, gotAuth == "Bearer "+tok, gotEnc, gotCT, gotUA)
	}
	if string(gotBody) != `{"schema":1}` {
		t.Errorf("body = %q", gotBody)
	}
	if resp.Accepted != 2 || resp.Rejected != 1 || resp.ConfigVersion != 4 || resp.Config == nil ||
		resp.Config.IntervalSeconds != 30 || resp.Config.WatchServices[0] != "nginx" {
		t.Errorf("resp = %+v", resp)
	}
}

func TestIngestNoGzip(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Content-Encoding") != "" {
			t.Error("unexpected Content-Encoding")
		}
		b, _ := io.ReadAll(r.Body)
		if string(b) != "{}" {
			t.Errorf("body %q", b)
		}
		_, _ = w.Write([]byte(`{"accepted":0,"rejected":0,"config_version":1}`))
	}))
	defer srv.Close()
	if _, err := New(srv.URL, tok, false, "v").Ingest(context.Background(), []byte("{}")); err != nil {
		t.Fatal(err)
	}
}

func TestStatusClassification(t *testing.T) {
	cases := []struct {
		status int
		hdr    string
		kind   Kind
		retry  time.Duration
	}{
		{401, "", Unauthorized, 0},
		{400, "", Drop, 0},
		{413, "", Drop, 0},
		{422, "", Drop, 0},
		{429, "7", Retry, 7 * time.Second},
		{503, "120", Retry, 120 * time.Second},
		{503, "999999", Retry, time.Hour}, // capped
		{500, "", Retry, 0},
		{404, "", Retry, 0}, // feature flag off / hub not updated yet
		{403, "", Retry, 0}, // proxies/WAFs must not kill the agent
		{302, "", Retry, 0},
	}
	for _, tc := range cases {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if tc.hdr != "" {
				w.Header().Set("Retry-After", tc.hdr)
			}
			if tc.status == 302 {
				w.Header().Set("Location", "http://evil.invalid/steal")
			}
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(`{"detail":"nope"}`))
		}))
		_, err := New(srv.URL, tok, true, "v").Ingest(context.Background(), []byte("{}"))
		srv.Close()
		e := asErr(t, err)
		if e.Kind != tc.kind || e.Status != tc.status || e.RetryAfter != tc.retry {
			t.Errorf("status %d: kind=%v status=%d retryAfter=%v, want kind=%v retry=%v", tc.status, e.Kind, e.Status, e.RetryAfter, tc.kind, tc.retry)
		}
		if strings.Contains(e.Error(), tok) {
			t.Errorf("error text leaks token: %v", e)
		}
	}
}

func TestRedirectIsNeverFollowed(t *testing.T) {
	hit := false
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hit = true }))
	defer evil.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, evil.URL, http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	_, err := New(srv.URL, tok, true, "v").Ingest(context.Background(), []byte("{}"))
	if e := asErr(t, err); e.Kind != Retry {
		t.Errorf("kind = %v", e.Kind)
	}
	if hit {
		t.Error("redirect was followed; bearer token would have been forwarded")
	}
}

func TestTimeoutIsRetryAndNeverLeaksToken(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-release }))
	defer srv.Close()
	defer close(release)
	c := New(srv.URL, tok, true, "v")
	c.HTTP.Timeout = 100 * time.Millisecond
	start := time.Now()
	_, err := c.Ingest(context.Background(), []byte("{}"))
	if time.Since(start) > 3*time.Second {
		t.Error("timeout not honoured")
	}
	e := asErr(t, err)
	if e.Kind != Retry || e.Status != 0 || strings.Contains(e.Error(), tok) {
		t.Errorf("err = %+v", e)
	}
}

func TestConnectionRefusedIsRetry(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	_, err := New(url, tok, true, "v").Ingest(context.Background(), []byte("{}"))
	if e := asErr(t, err); e.Kind != Retry {
		t.Errorf("kind = %v", e.Kind)
	}
}

func TestContextCancelIsRetry(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.ReadAll(r.Body) // consuming the body lets the server notice the client going away
		<-r.Context().Done()
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(50 * time.Millisecond); cancel() }()
	_, err := New(srv.URL, tok, true, "v").Ingest(ctx, []byte("{}"))
	if e := asErr(t, err); e.Kind != Retry {
		t.Errorf("a cancelled send must be kept for later, kind=%v", e.Kind)
	}
}

func TestOKWithGarbageBodyIsSuccess(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<html>")) }))
	defer srv.Close()
	resp, err := New(srv.URL, tok, true, "v").Ingest(context.Background(), []byte("{}"))
	if err != nil || resp.Config != nil || resp.ConfigVersion != 0 {
		t.Errorf("resp=%+v err=%v", resp, err)
	}
}

func TestParseRetryAfterHTTPDate(t *testing.T) {
	now := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	if d := ParseRetryAfter(now.Add(90*time.Second).UTC().Format(http.TimeFormat), now); d != 90*time.Second {
		t.Errorf("date form = %v", d)
	}
	for _, bad := range []string{"", "soon", "-5", now.Add(-time.Hour).UTC().Format(http.TimeFormat)} {
		if d := ParseRetryAfter(bad, now); d != 0 {
			t.Errorf("ParseRetryAfter(%q) = %v, want 0", bad, d)
		}
	}
}

func TestBackoffGrowsCapsAndJitters(t *testing.T) {
	b := &Backoff{Base: 2 * time.Second, Max: 60 * time.Second, Rand: func() float64 { return 0 }}
	// Rand=0 -> lower bound d/2 of each doubling step.
	want := []time.Duration{1, 2, 4, 8, 16, 30, 30}
	for i, w := range want {
		if got := b.Next(0); got != w*time.Second {
			t.Errorf("step %d = %v, want %v", i, got, w*time.Second)
		}
	}
	b = &Backoff{Base: 2 * time.Second, Max: 60 * time.Second, Rand: func() float64 { return 0.999999 }}
	if got := b.Next(0); got < 1900*time.Millisecond || got > 2*time.Second {
		t.Errorf("upper jitter bound = %v", got)
	}
	// Retry-After beats the computed delay, with at most 10% jitter on top.
	b = &Backoff{Base: time.Second, Max: time.Minute, Rand: func() float64 { return 1 }}
	got := b.Next(100 * time.Second)
	if got < 100*time.Second || got > 110*time.Second {
		t.Errorf("retry-after delay = %v", got)
	}
	// Reset restarts the curve.
	b.Reset()
	if b.Attempts() != 0 {
		t.Error("reset failed")
	}
	// Real jitter source: many draws stay inside [d/2, d].
	rb := NewBackoff()
	for i := 0; i < 50; i++ {
		d := rb.Next(0)
		if d < time.Second || d > 5*time.Minute {
			t.Fatalf("delay %v out of bounds", d)
		}
	}
}
