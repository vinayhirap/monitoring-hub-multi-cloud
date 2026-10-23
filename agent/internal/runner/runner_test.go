package runner

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"cloudops-agent/internal/collect"
	"cloudops-agent/internal/config"
	"cloudops-agent/internal/payload"
	"cloudops-agent/internal/sender"
)

const token = "cah_THISISASECRETTOKENTHISISASECRETTOKEN1"

// ---- fake hub ---------------------------------------------------------------

type fakeHub struct {
	mu         sync.Mutex
	srv        *httptest.Server
	mode       string // ok | down | 401 | 429 | 503 | 400-first
	retryAfter string
	received   []payload.Payload
	arrivals   []time.Time // every request, including rejected ones
	gzipSeen   bool
	authOK     bool
	cfgVersion int
	cfg        *payload.ServerConfig
	badFirst   bool
}

func newHub(t *testing.T) *fakeHub {
	t.Helper()
	h := &fakeHub{mode: "ok", cfgVersion: 1, authOK: true}
	h.srv = httptest.NewServer(http.HandlerFunc(h.serve))
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeHub) serve(w http.ResponseWriter, r *http.Request) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.arrivals = append(h.arrivals, time.Now())
	if r.URL.Path != sender.IngestPath {
		http.NotFound(w, r)
		return
	}
	switch h.mode {
	case "down":
		if hj, ok := w.(http.Hijacker); ok {
			if c, _, err := hj.Hijack(); err == nil {
				_ = c.Close()
			}
		}
		return
	case "401":
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"invalid token"}`))
		return
	case "429":
		if h.retryAfter != "" {
			w.Header().Set("Retry-After", h.retryAfter)
		}
		w.WriteHeader(http.StatusTooManyRequests)
		return
	case "503":
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if r.Header.Get("Authorization") != "Bearer "+token {
		h.authOK = false
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	var rd io.Reader = r.Body
	if r.Header.Get("Content-Encoding") == "gzip" {
		h.gzipSeen = true
		zr, err := gzip.NewReader(r.Body)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		rd = zr
	}
	b, _ := io.ReadAll(rd)
	var p payload.Payload
	if err := json.Unmarshal(b, &p); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	if h.badFirst && len(h.received) == 0 {
		h.badFirst = false
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"detail":"unknown schema"}`))
		return
	}
	h.received = append(h.received, p)
	resp := payload.IngestResponse{Accepted: len(p.Metrics), ConfigVersion: h.cfgVersion}
	if p.ConfigVersion != h.cfgVersion && h.cfg != nil {
		resp.Config = h.cfg
	}
	_ = json.NewEncoder(w).Encode(resp)
}

func (h *fakeHub) set(f func(*fakeHub)) { h.mu.Lock(); f(h); h.mu.Unlock() }
func (h *fakeHub) seqs() []int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]int64, len(h.received))
	for i, p := range h.received {
		out[i] = p.Seq
	}
	return out
}
func (h *fakeHub) nRequests() int { h.mu.Lock(); defer h.mu.Unlock(); return len(h.arrivals) }
func (h *fakeHub) nReceived() int { h.mu.Lock(); defer h.mu.Unlock(); return len(h.received) }

// ---- harness ----------------------------------------------------------------

type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.b.Write(p) }
func (s *syncBuf) String() string              { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

type rig struct {
	t        *testing.T
	hub      *fakeHub
	dir      string
	logs     *syncBuf
	settings []collect.Settings
	smu      sync.Mutex
	ticks    int
	r        *Runner
	cancel   context.CancelFunc
	done     chan error
}

func fastBackoff() *sender.Backoff {
	return &sender.Backoff{Base: 20 * time.Millisecond, Max: 80 * time.Millisecond, Rand: func() float64 { return 0.5 }}
}

func newRig(t *testing.T, hub *fakeHub, dir string, bo *sender.Backoff) *rig {
	t.Helper()
	if dir == "" {
		dir = t.TempDir()
	}
	g := &rig{t: t, hub: hub, dir: dir, logs: &syncBuf{}}
	logger := slog.New(slog.NewTextHandler(g.logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	id := config.File{URL: hub.srv.URL, AgentToken: token, HostAgentID: 5, Hostname: "web-1", InstanceID: "i-0abc123def4567890",
		ConfigVersion: 1, Config: payload.ServerConfig{IntervalSeconds: 60, MaxBatchMetrics: 500}}
	if bo == nil {
		bo = fastBackoff()
	}
	r, err := New(Options{
		Identity: id, DataDir: dir, Version: "0.1.0", Logger: logger, Backoff: bo,
		TimeUnit: 2 * time.Millisecond, // interval 60 -> 120 ms, min 15 -> 30 ms
		Collect: func(_ context.Context, s collect.Settings) collect.Result {
			g.smu.Lock()
			g.ticks++
			n := g.ticks
			g.settings = append(g.settings, s)
			g.smu.Unlock()
			res := collect.Result{BootTime: 1759000000, Metrics: []payload.Metric{
				{Name: "load1", Value: float64(n), Ts: time.Now().Unix()},
				{Name: "disk_used_percent", Value: 10, Ts: time.Now().Unix(), Labels: map[string]string{"mount": "/"}},
			}}
			for _, w := range s.WatchServices {
				res.Services = append(res.Services, payload.Service{Name: w, State: "running"})
			}
			return res
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	g.r = r
	return g
}

func (g *rig) start() {
	ctx, cancel := context.WithCancel(context.Background())
	g.cancel = cancel
	g.done = make(chan error, 1)
	go func() { g.done <- g.r.Run(ctx) }()
	g.t.Cleanup(func() { g.stop() })
}

func (g *rig) stop() error {
	if g.cancel == nil {
		return nil
	}
	g.cancel()
	g.cancel = nil
	select {
	case err := <-g.done:
		return err
	case <-time.After(15 * time.Second):
		g.t.Error("runner did not stop")
		return errors.New("timeout")
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", what)
}

func queueFiles(dir string) int {
	ents, _ := os.ReadDir(filepath.Join(dir, "queue"))
	n := 0
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), ".q") {
			n++
		}
	}
	return n
}

func assertNoToken(t *testing.T, g *rig) {
	t.Helper()
	if strings.Contains(g.logs.String(), token) {
		t.Errorf("token leaked into logs:\n%s", g.logs.String())
	}
}

// ---- tests ------------------------------------------------------------------

func TestPayloadShapeAndSteadyState(t *testing.T) {
	hub := newHub(t)
	g := newRig(t, hub, "", nil)
	g.start()
	waitFor(t, "3 payloads", func() bool { return hub.nReceived() >= 3 })
	g.stop()

	hub.mu.Lock()
	defer hub.mu.Unlock()
	if !hub.gzipSeen || !hub.authOK {
		t.Errorf("gzip=%v auth=%v", hub.gzipSeen, hub.authOK)
	}
	p := hub.received[0]
	if p.Schema != 1 || p.AgentVersion != "0.1.0" || p.ConfigVersion != 1 || p.Seq != 1 || p.SentAt == 0 ||
		p.Host.Hostname != "web-1" || p.Host.InstanceID == nil || *p.Host.InstanceID != "i-0abc123def4567890" ||
		p.Host.OS != "linux" || p.Host.Arch == "" || p.Host.BootTime != 1759000000 {
		t.Errorf("payload = %+v", p)
	}
	if len(p.Metrics) != 2 || p.Services == nil {
		t.Errorf("metrics/services = %+v / %+v", p.Metrics, p.Services)
	}
	// Strictly monotonic with no gaps. One repeat of the LAST seq is legitimate:
	// a send cancelled by shutdown after the hub already read it is re-sent by
	// the flush (at-least-once; the hub drops seq <= last_seq as a duplicate).
	want := int64(1)
	for i, q := range hub.received {
		switch {
		case q.Seq == want:
			want++
		case q.Seq == want-1 && i == len(hub.received)-1:
		default:
			t.Errorf("seq[%d] = %d, want %d", i, q.Seq, want)
		}
	}
	if g.r.QueueLen() != 0 || queueFiles(g.dir) != 0 {
		t.Error("healthy path must not touch the disk queue")
	}
	assertNoToken(t, g)
}

func TestOfflineBufferThenReplayOldestFirst(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "down" })
	g := newRig(t, hub, "", nil)
	g.start()
	waitFor(t, "3 payloads buffered on disk", func() bool { return queueFiles(g.dir) >= 3 })
	if hub.nReceived() != 0 {
		t.Fatal("hub is down, nothing should have been received")
	}

	hub.set(func(h *fakeHub) { h.mode = "ok" })
	waitFor(t, "queue drained", func() bool { return g.r.QueueLen() == 0 && hub.nReceived() >= 5 })
	g.stop()

	seqs := hub.seqs()
	for i, s := range seqs {
		if s != int64(i+1) {
			t.Fatalf("replay not oldest-first / contiguous: %v", seqs)
		}
	}
	if queueFiles(g.dir) != 0 {
		t.Error("queue files left behind after replay")
	}
	assertNoToken(t, g)
	if !strings.Contains(g.logs.String(), "hub reachable again") {
		t.Error("recovery should be logged")
	}
}

func TestBackoffDoesNotHammerWhileDown(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "503" })
	bo := &sender.Backoff{Base: 400 * time.Millisecond, Max: time.Second, Rand: func() float64 { return 1 }}
	g := newRig(t, hub, "", bo)
	g.start()
	time.Sleep(900 * time.Millisecond) // ~7 collection ticks at 120 ms
	g.stop()
	if n := hub.nRequests(); n > 3 {
		t.Errorf("%d requests in 0.9 s with a 0.4 s+ backoff: the agent is hammering", n)
	}
	if g.r.QueueLen() < 4 {
		t.Errorf("collection must continue into the queue while backing off, queued=%d", g.r.QueueLen())
	}
}

func TestRetryAfterIsHonoured(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "429"; h.retryAfter = "1" })
	g := newRig(t, hub, "", nil)
	g.start()
	waitFor(t, "first request", func() bool { return hub.nRequests() >= 1 })
	hub.set(func(h *fakeHub) { h.mode = "ok" })
	waitFor(t, "recovery", func() bool { return hub.nReceived() >= 1 })
	g.stop()
	hub.mu.Lock()
	defer hub.mu.Unlock()
	if gap := hub.arrivals[1].Sub(hub.arrivals[0]); gap < 950*time.Millisecond {
		t.Errorf("retried %v after a 429 with Retry-After: 1", gap)
	}
}

func Test401StopsAndDoesNotHammer(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "401" })
	g := newRig(t, hub, "", nil)
	g.start()
	select {
	case err := <-g.done:
		g.cancel = nil
		if !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("Run returned %v, want ErrUnauthorized", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("agent kept running after a 401")
	}
	time.Sleep(400 * time.Millisecond) // well over 3 intervals: nothing more may arrive
	if n := hub.nRequests(); n != 1 {
		t.Errorf("%d requests after 401, want exactly 1", n)
	}
	logs := g.logs.String()
	if !strings.Contains(logs, "401") || !strings.Contains(logs, "level=ERROR") {
		t.Errorf("401 must be logged as an error:\n%s", logs)
	}
	assertNoToken(t, g)
}

func Test401WhileReplayingAlsoStops(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "down" })
	g := newRig(t, hub, "", nil)
	g.start()
	waitFor(t, "buffered", func() bool { return queueFiles(g.dir) >= 2 })
	hub.set(func(h *fakeHub) { h.mode = "401" })
	select {
	case err := <-g.done:
		g.cancel = nil
		if !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("agent kept running after a 401 during replay")
	}
	if queueFiles(g.dir) < 2 {
		t.Error("queued data must stay on disk after a 401")
	}
}

func TestSeqStaysMonotonicAcrossRestart(t *testing.T) {
	hub := newHub(t)
	dir := t.TempDir()
	g1 := newRig(t, hub, dir, nil)
	g1.start()
	waitFor(t, "2 payloads", func() bool { return hub.nReceived() >= 2 })
	g1.stop()
	last := hub.seqs()[hub.nReceived()-1]

	g2 := newRig(t, hub, dir, nil)
	g2.start()
	waitFor(t, "payload from 2nd run", func() bool { return hub.nReceived() > int(last) })
	g2.stop()
	seqs := hub.seqs()
	for i := 1; i < len(seqs); i++ {
		// An equal repeat is the at-least-once replay of a send cut off by shutdown
		// (the hub drops seq <= last_seq as a duplicate); a LOWER seq is a bug.
		if seqs[i] < seqs[i-1] {
			t.Fatalf("seq went backwards across restart: %v", seqs)
		}
	}
	if seqs[len(seqs)-1] <= last {
		t.Errorf("no new seq after restart: %v", seqs)
	}
}

func TestSeqFloorFromQueueWhenStateLost(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "down" })
	dir := t.TempDir()
	g1 := newRig(t, hub, dir, nil)
	g1.start()
	waitFor(t, "buffered", func() bool { return queueFiles(g1.dir) >= 3 })
	g1.stop()
	if err := os.Remove(filepath.Join(dir, "state.json")); err != nil {
		t.Fatal(err)
	}
	hub.set(func(h *fakeHub) { h.mode = "ok" })
	g2 := newRig(t, hub, dir, nil)
	g2.start()
	waitFor(t, "queue replayed + new data", func() bool { return g2.r.QueueLen() == 0 && hub.nReceived() >= 5 })
	g2.stop()
	seqs := hub.seqs()
	for i := 1; i < len(seqs); i++ {
		if seqs[i] <= seqs[i-1] {
			t.Fatalf("seq collided/regressed after state loss: %v", seqs)
		}
	}
}

func TestConfigAppliedWithoutRestartAndPersisted(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) {
		h.cfgVersion = 2
		h.cfg = &payload.ServerConfig{IntervalSeconds: 15, WatchServices: []string{"nginx"}, MaxBatchMetrics: 500}
	})
	dir := t.TempDir()
	g := newRig(t, hub, dir, nil)
	before := g.r.Interval()
	g.start()
	waitFor(t, "config version 2 adopted", func() bool { _, v := g.r.Config(); return v == 2 })
	if after := g.r.Interval(); after != 15*2*time.Millisecond || after >= before {
		t.Errorf("interval %v -> %v", before, after)
	}
	waitFor(t, "service reported", func() bool {
		hub.mu.Lock()
		defer hub.mu.Unlock()
		for _, p := range hub.received {
			for _, s := range p.Services {
				if s.Name == "nginx" && s.State == "running" && p.ConfigVersion == 2 {
					return true
				}
			}
		}
		return false
	})
	g.smu.Lock()
	lastSettings := g.settings[len(g.settings)-1]
	g.smu.Unlock()
	if len(lastSettings.WatchServices) != 1 || lastSettings.WatchServices[0] != "nginx" {
		t.Errorf("collector not given the new watch list: %+v", lastSettings)
	}
	g.stop()
	// Restart: the pushed config is remembered (state.json), not reset to defaults.
	g2 := newRig(t, hub, dir, nil)
	cfg, ver := g2.r.Config()
	if ver != 2 || cfg.IntervalSeconds != 15 || len(cfg.WatchServices) != 1 {
		t.Errorf("config after restart = %+v v%d", cfg, ver)
	}
}

func TestGracefulShutdownFlushesQueue(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "down" })
	// Huge backoff: only the shutdown flush can drain after the hub returns.
	g := newRig(t, hub, "", &sender.Backoff{Base: time.Hour, Max: time.Hour, Rand: func() float64 { return 0.5 }})
	g.start()
	waitFor(t, "buffered", func() bool { return queueFiles(g.dir) >= 3 })
	hub.set(func(h *fakeHub) { h.mode = "ok" })
	time.Sleep(50 * time.Millisecond)
	if hub.nReceived() != 0 {
		t.Fatal("backoff should still be holding the queue")
	}
	if err := g.stop(); err != nil {
		t.Fatalf("graceful stop returned %v", err)
	}
	if hub.nReceived() < 3 || g.r.QueueLen() != 0 {
		t.Errorf("SIGTERM flush: received=%d queued=%d", hub.nReceived(), g.r.QueueLen())
	}
	if s := hub.seqs(); s[0] != 1 {
		t.Errorf("flush must be oldest-first: %v", s)
	}
}

func TestShutdownWithHubDownKeepsQueueOnDisk(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.mode = "down" })
	dir := t.TempDir()
	g := newRig(t, hub, dir, &sender.Backoff{Base: time.Hour, Max: time.Hour, Rand: func() float64 { return 0.5 }})
	g.start()
	waitFor(t, "buffered", func() bool { return queueFiles(dir) >= 2 })
	start := time.Now()
	if err := g.stop(); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 7*time.Second {
		t.Error("shutdown must stay bounded")
	}
	n := queueFiles(dir)
	if n < 2 {
		t.Fatalf("queue lost on shutdown: %d files", n)
	}
	// Next start replays them.
	hub.set(func(h *fakeHub) { h.mode = "ok" })
	g2 := newRig(t, hub, dir, nil)
	g2.start()
	waitFor(t, "replay after restart", func() bool { return g2.r.QueueLen() == 0 && hub.nReceived() >= n })
	g2.stop()
}

func TestInvalidPayloadIsDroppedNotRetriedForever(t *testing.T) {
	hub := newHub(t)
	hub.set(func(h *fakeHub) { h.badFirst = true })
	g := newRig(t, hub, "", nil)
	g.start()
	waitFor(t, "later payloads flow", func() bool { return hub.nReceived() >= 2 })
	g.stop()
	if s := hub.seqs(); s[0] == 1 {
		t.Errorf("seq 1 was rejected with 400 and must be dropped, got %v", s)
	}
	if !strings.Contains(g.logs.String(), "dropping it") {
		t.Error("drop must be logged")
	}
	if g.r.QueueLen() != 0 {
		t.Error("a 400 payload must not be queued")
	}
}

func TestBuildSplitsAndHeartbeat(t *testing.T) {
	hub := newHub(t)
	g := newRig(t, hub, "", nil)
	g.r.cfg.MaxBatchMetrics = 3
	ms := make([]payload.Metric, 7)
	for i := range ms {
		ms[i] = payload.Metric{Name: fmt.Sprintf("m%d", i), Value: float64(i), Ts: 1}
	}
	ps := g.r.build(collect.Result{Metrics: ms, Services: []payload.Service{{Name: "x", State: "running"}}}, time.Unix(5, 0))
	if len(ps) != 3 || len(ps[0].Metrics) != 3 || len(ps[1].Metrics) != 3 || len(ps[2].Metrics) != 1 {
		t.Fatalf("split = %d payloads", len(ps))
	}
	if len(ps[0].Services) != 1 || len(ps[1].Services) != 0 || ps[1].Services == nil {
		t.Error("services belong on the first payload only; others carry []")
	}
	hb := g.r.build(collect.Result{}, time.Unix(5, 0))
	if len(hb) != 1 {
		t.Fatalf("heartbeat payloads = %d", len(hb))
	}
	b, _ := json.Marshal(hb[0])
	if !strings.Contains(string(b), `"metrics":[]`) || !strings.Contains(string(b), `"services":[]`) {
		t.Errorf("heartbeat must serialise empty arrays, not null: %s", b)
	}
	if !strings.Contains(string(b), `"instance_id":"i-0abc123def4567890"`) {
		t.Errorf("instance id missing: %s", b)
	}
}

func TestNullInstanceIDWhenUnknown(t *testing.T) {
	hub := newHub(t)
	g := newRig(t, hub, "", nil)
	g.r.o.Identity.InstanceID = ""
	b, _ := json.Marshal(g.r.build(collect.Result{}, time.Unix(5, 0))[0])
	if !strings.Contains(string(b), `"instance_id":null`) {
		t.Errorf("want explicit null: %s", b)
	}
}
