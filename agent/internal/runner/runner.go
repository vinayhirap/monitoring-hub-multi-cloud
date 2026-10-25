// Package runner is the agent's main loop: collect on a ticker, send to the
// hub, fall back to the bounded disk queue when the hub is unreachable, replay
// oldest-first when it comes back, and apply hub-pushed config live.
package runner

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"path/filepath"
	"runtime"
	"slices"
	"sync"
	"time"

	"cloudops-agent/internal/collect"
	"cloudops-agent/internal/config"
	"cloudops-agent/internal/payload"
	"cloudops-agent/internal/queue"
	"cloudops-agent/internal/sender"
	"cloudops-agent/internal/state"
)

// ErrUnauthorized is returned by Run after the hub answered 401. The caller
// must exit (non-zero, and the service must not restart-loop on it).
var ErrUnauthorized = errors.New("hub rejected the agent token (HTTP 401)")

const (
	drainMaxEntries = 500
	drainMaxTime    = 20 * time.Second
	flushTimeout    = 5 * time.Second
)

type Options struct {
	Identity config.File // agent.json
	DataDir  string      // state.json + queue/
	Version  string
	Logger   *slog.Logger

	// Optional / test seams.
	Collect    func(context.Context, collect.Settings) collect.Result
	HTTPClient *http.Client
	Backoff    *sender.Backoff
	QueueBytes int64
	QueueAge   time.Duration
	TimeUnit   time.Duration // one "interval second"; default time.Second (tests shrink it)
}

type outcome int

const (
	sent outcome = iota
	dropped
	retry
	unauthorized
)

type Runner struct {
	o   Options
	log *slog.Logger
	cl  *sender.Client
	q   *queue.Queue
	st  *state.Store
	col func(context.Context, collect.Settings) collect.Result

	backoff    *sender.Backoff
	retryAt    time.Time
	retryTimer *time.Timer
	ticker     *time.Ticker
	failing    bool
	seqWarned  bool
	errLogged  map[string]time.Time

	// Service inventory (every service that exists on the host) is sent on the first pass and then every inventoryEvery,
	// not on every report; lastInv is when the hub last received one from this process.
	lastInv time.Time

	mu         sync.Mutex // guards cfg/cfgVersion for Interval()/Config() readers
	cfg        payload.ServerConfig
	cfgVersion int
}

func New(o Options) (*Runner, error) {
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.TimeUnit <= 0 {
		o.TimeUnit = time.Second
	}
	st, err := state.Open(o.DataDir)
	if err != nil {
		return nil, err
	}
	q, err := queue.Open(filepath.Join(o.DataDir, "queue"), o.QueueBytes, o.QueueAge, nil)
	if err != nil {
		return nil, err
	}
	st.Raise(q.MaxSeq())

	cfg, ver := o.Identity.Config, o.Identity.ConfigVersion
	if s := st.Get(); s.Config != nil { // newest hub-pushed config wins over the enroll-time one
		cfg, ver = *s.Config, s.ConfigVersion
	}
	if ver <= 0 {
		ver = 1
	}
	cl := sender.New(o.Identity.URL, o.Identity.AgentToken, !o.Identity.NoGzip, o.Version)
	if o.HTTPClient != nil {
		cl.HTTP = o.HTTPClient
	}
	bo := o.Backoff
	if bo == nil {
		bo = sender.NewBackoff()
	}
	r := &Runner{
		o: o, log: o.Logger, cl: cl, q: q, st: st, backoff: bo,
		cfg: config.Sanitize(cfg), cfgVersion: ver, errLogged: map[string]time.Time{},
	}
	if o.Collect != nil {
		r.col = o.Collect
	} else {
		c := collect.New(collect.Options{})
		r.col = c.Collect
	}
	return r, nil
}

// Interval is the current collection interval.
func (r *Runner) Interval() time.Duration {
	r.mu.Lock()
	defer r.mu.Unlock()
	return time.Duration(r.cfg.IntervalSeconds) * r.o.TimeUnit
}

// Config returns the config currently in force and its version.
func (r *Runner) Config() (payload.ServerConfig, int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.cfg, r.cfgVersion
}

// QueueLen is the number of payloads waiting on disk.
func (r *Runner) QueueLen() int { return r.q.Len() }

// Run blocks until ctx is cancelled (returns nil after a graceful flush) or
// the hub answers 401 (returns ErrUnauthorized).
func (r *Runner) Run(ctx context.Context) error {
	cfg, ver := r.Config()
	r.log.Info("agent started", "version", r.o.Version, "hub", r.o.Identity.URL,
		"host_agent_id", r.o.Identity.HostAgentID, "interval", cfg.IntervalSeconds,
		"config_version", ver, "watch_services", cfg.WatchServices, "queued", r.q.Len())

	r.retryTimer = time.NewTimer(time.Hour)
	r.stopRetryTimer()
	r.ticker = time.NewTicker(r.Interval())
	defer r.ticker.Stop()
	defer r.stopRetryTimer()

	if err := r.tick(ctx); err != nil { // first sample right away (rate baseline + first heartbeat)
		return err
	}
	for {
		select {
		case <-ctx.Done():
			r.flush()
			r.log.Info("agent stopped", "queued", r.q.Len())
			return nil
		case <-r.ticker.C:
			if err := r.tick(ctx); err != nil {
				return err
			}
		case <-r.retryTimer.C:
			if err := r.onRetry(ctx); err != nil {
				return err
			}
		}
	}
}

// inventoryEvery is how often the service inventory is refreshed. It changes rarely and costs two service-manager calls,
// so it rides on one report in ~30 instead of every minute.
const inventoryEvery = 30 * time.Minute

func (r *Runner) tick(ctx context.Context) error {
	if ctx.Err() != nil {
		return nil
	}
	cfg, _ := r.Config()
	wantInv := r.lastInv.IsZero() || time.Since(r.lastInv) >= inventoryEvery
	res := r.col(ctx, collect.Settings{WatchServices: cfg.WatchServices, DiskExcludeFstypes: cfg.DiskExcludeFstypes, WithInventory: wantInv})
	if wantInv && res.Inventory != nil {
		r.lastInv = time.Now() // a failed scan (nil) is retried on the next tick rather than waiting another half hour
	}
	for _, e := range res.Errs {
		r.logCollectErr(e)
	}
	for _, p := range r.build(res, time.Now()) {
		seq, err := r.st.NextSeq()
		if err != nil && !r.seqWarned {
			r.seqWarned = true
			r.log.Warn("cannot persist sequence counter; seq may restart after a crash", "err", err)
		}
		p.Seq = seq
		body, err := json.Marshal(p)
		if err != nil {
			r.log.Error("marshal payload", "err", err)
			continue
		}
		if err := r.submit(ctx, queue.Entry{Seq: seq, SentAt: p.SentAt, Body: body}); err != nil {
			return err
		}
	}
	return nil
}

// logCollectErr logs each distinct collector problem at most once per hour.
func (r *Runner) logCollectErr(err error) {
	k := err.Error()
	if t, ok := r.errLogged[k]; ok && time.Since(t) < time.Hour {
		return
	}
	r.errLogged[k] = time.Now()
	r.log.Warn("collector problem (sample partially skipped)", "err", err)
}

// build turns one collection pass into >= 1 payloads, honouring
// max_batch_metrics. An empty pass is a valid heartbeat.
func (r *Runner) build(res collect.Result, now time.Time) []payload.Payload {
	cfg, ver := r.Config()
	var iid *string
	if r.o.Identity.InstanceID != "" {
		v := r.o.Identity.InstanceID
		iid = &v
	}
	base := payload.Payload{
		Schema: payload.SchemaVersion, AgentVersion: r.o.Version, ConfigVersion: ver, SentAt: now.Unix(),
		Host: payload.Host{Hostname: r.o.Identity.Hostname, InstanceID: iid, OS: runtime.GOOS, Arch: runtime.GOARCH, BootTime: res.BootTime},
	}
	metrics := res.Metrics
	if metrics == nil {
		metrics = []payload.Metric{}
	}
	services := res.Services
	if services == nil {
		services = []payload.Service{}
	}
	batch := cfg.MaxBatchMetrics
	if batch <= 0 {
		batch = 500
	}
	var out []payload.Payload
	for i := 0; i == 0 || i < len(metrics); i += batch {
		p := base
		end := min(i+batch, len(metrics))
		p.Metrics = slices.Clone(metrics[i:end])
		p.Services = []payload.Service{}
		if i == 0 {
			p.Services = services
			p.ServiceInventory = res.Inventory // nil (omitted) on every report except the periodic scan
		}
		out = append(out, p)
	}
	return out
}

func (r *Runner) backingOff() bool {
	return !r.retryAt.IsZero() && time.Now().Before(r.retryAt)
}

// submit sends directly when the hub is healthy and nothing is queued;
// otherwise it appends to the queue (keeping order) and drains if allowed.
func (r *Runner) submit(ctx context.Context, e queue.Entry) error {
	if r.q.Len() == 0 && !r.backingOff() {
		switch r.attempt(ctx, e) {
		case sent, dropped:
			return nil
		case unauthorized:
			return ErrUnauthorized
		}
		r.enqueue(e) // retry: keep it
		return nil
	}
	r.enqueue(e)
	if !r.backingOff() {
		return r.drain(ctx)
	}
	return nil
}

func (r *Runner) enqueue(e queue.Entry) {
	if err := r.q.Push(e.Seq, e.SentAt, e.Body); err != nil {
		r.log.Error("cannot buffer payload to disk; sample lost", "seq", e.Seq, "err", err)
		return
	}
	if d := r.q.Dropped(); d > 0 {
		r.log.Debug("queue at capacity; oldest payloads were dropped", "dropped_total", d, "queued", r.q.Len())
	}
}

// attempt performs one send and does all bookkeeping for its result.
func (r *Runner) attempt(ctx context.Context, e queue.Entry) outcome {
	resp, err := r.cl.Ingest(ctx, e.Body)
	if err == nil {
		r.onSuccess(resp, e)
		return sent
	}
	var se *sender.Error
	if !errors.As(err, &se) {
		se = &sender.Error{Kind: sender.Retry, Msg: err.Error()}
	}
	switch se.Kind {
	case sender.Unauthorized:
		r.log.Error("hub rejected the agent token (HTTP 401): this host was revoked or the token is invalid. "+
			"Stopping instead of retrying. Re-enroll with a new key: cloudops-agent enroll --force --url ... --key ...",
			"detail", se.Msg)
		return unauthorized
	case sender.Drop:
		r.log.Warn("hub rejected a payload as invalid; dropping it", "seq", e.Seq, "status", se.Status, "detail", se.Msg)
		return dropped
	}
	if ctx.Err() != nil { // shutting down: the entry stays queued, no noise
		return retry
	}
	d := r.backoff.Next(se.RetryAfter)
	r.setRetry(d)
	r.failing = true
	r.log.Warn("hub unreachable or busy; buffering to disk", "err", se.Error(), "retry_in", d.Round(time.Second),
		"attempt", r.backoff.Attempts(), "queued", r.q.Len()+1)
	return retry
}

func (r *Runner) onSuccess(resp payload.IngestResponse, e queue.Entry) {
	if r.failing {
		r.failing = false
		r.log.Info("hub reachable again", "queued", r.q.Len())
	}
	r.backoff.Reset()
	r.retryAt = time.Time{}
	r.stopRetryTimer()
	if resp.Rejected > 0 {
		r.log.Warn("hub rejected some samples", "seq", e.Seq, "accepted", resp.Accepted, "rejected", resp.Rejected)
	} else {
		r.log.Debug("sent", "seq", e.Seq, "accepted", resp.Accepted)
	}
	if resp.Config != nil {
		r.applyConfig(resp.ConfigVersion, *resp.Config)
	}
}

// applyConfig adopts a hub-pushed config without a restart and persists it.
func (r *Runner) applyConfig(version int, c payload.ServerConfig) {
	c = config.Sanitize(c)
	r.mu.Lock()
	oldCfg := r.cfg
	if version > 0 {
		r.cfgVersion = version
	}
	r.cfg = c
	r.mu.Unlock()

	if err := r.st.SetConfig(max(version, 1), c); err != nil {
		r.log.Warn("cannot persist config; it will be re-fetched after a restart", "err", err)
	}
	r.log.Info("config updated by hub", "config_version", version, "interval", c.IntervalSeconds, "watch_services", c.WatchServices)
	if oldCfg.IntervalSeconds != c.IntervalSeconds && r.ticker != nil {
		r.ticker.Reset(time.Duration(c.IntervalSeconds) * r.o.TimeUnit)
	}
}

// drain replays the queue oldest-first until it is empty, a send fails
// (backoff takes over), or the per-pass budget is used up.
func (r *Runner) drain(ctx context.Context) error {
	start := time.Now()
	n := 0
	for {
		if ctx.Err() != nil {
			return nil
		}
		e, ok := r.q.Peek()
		if !ok {
			return nil
		}
		switch r.attempt(ctx, e) {
		case sent, dropped:
			r.q.Ack(e.Seq)
			n++
		case unauthorized:
			return ErrUnauthorized
		default:
			return nil // retry scheduled by attempt
		}
		if n >= drainMaxEntries || time.Since(start) > drainMaxTime {
			if r.q.Len() > 0 {
				r.setRetry(500 * time.Millisecond) // keep going shortly, without starving collection
			}
			return nil
		}
	}
}

func (r *Runner) onRetry(ctx context.Context) error {
	r.retryAt = time.Time{}
	if r.q.Len() == 0 {
		return nil
	}
	return r.drain(ctx)
}

// flush is the SIGTERM path: one bounded attempt to push what is queued; if
// the hub is not reachable the entries simply stay on disk for the next start.
func (r *Runner) flush() {
	if r.q.Len() == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), flushTimeout)
	defer cancel()
	r.log.Info("flushing queued payloads before exit", "queued", r.q.Len())
	_ = r.drain(ctx)
}

func (r *Runner) setRetry(d time.Duration) {
	r.retryAt = time.Now().Add(d)
	if r.retryTimer == nil {
		return
	}
	r.stopRetryTimer()
	r.retryTimer.Reset(d)
}

func (r *Runner) stopRetryTimer() {
	if r.retryTimer == nil {
		return
	}
	if !r.retryTimer.Stop() {
		select {
		case <-r.retryTimer.C:
		default:
		}
	}
}
