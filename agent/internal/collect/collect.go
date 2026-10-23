// Package collect gathers host metrics (stdlib only, no cgo, no gopsutil, so
// the static binary stays small and the supply chain empty).
//
// Linux reads /proc and /sys directly (the v1 metric set is ~12 small files).
// macOS uses sysctl, vm_stat, netstat, top, ps and getfsstat(2); Windows uses
// kernel32 (GetSystemTimes, GlobalMemoryStatusEx, GetDiskFreeSpaceEx, ...).
// Metrics an OS has no honest equivalent for are omitted, never faked: see
// docs/HOST_AGENT_PLATFORMS.md for the per-OS coverage table.
//
// Rates (cpu_percent, disk_*_bytes_per_s, net_*_bytes_per_s) are computed as
// delta / elapsed seconds between two samples taken by this collector. The
// first sample after start has no baseline, so those series are skipped
// rather than sent as 0.
package collect

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"cloudops-agent/internal/payload"
)

// Statfs is the subset of statfs(2) the disk collector needs.
type Statfs struct {
	Blocks, Bfree, Bavail uint64
	Files, Ffree          uint64
}

// Options makes every external dependency injectable for tests.
type Options struct {
	ProcRoot string // default /proc
	SysRoot  string // default /sys
	Now      func() time.Time
	Statfs   func(path string) (Statfs, error)
	// Run executes a command and returns its stdout. A non-zero exit status
	// with output is NOT an error for our purposes (systemctl is-active exits
	// 3 for inactive units).
	Run func(ctx context.Context, name string, args ...string) ([]byte, error)

	// GOOS selects the collector set (default runtime.GOOS): "linux" reads /proc and /sys, "darwin" and "windows" use
	// the hooks below. Everything an OS-specific collector touches is injectable so the flows are unit-tested on Linux.
	GOOS string
	// Win supplies the Windows system calls (defaults to the real kernel32 wrappers on Windows).
	Win WinAPI
	// DarwinMounts lists mounted filesystems on macOS (defaults to getfsstat(2)).
	DarwinMounts func() ([]DarwinMount, error)
}

// Settings are the hub-controlled knobs the collectors honour.
type Settings struct {
	WatchServices      []string
	DiskExcludeFstypes []string
}

// Result is one collection pass.
type Result struct {
	Metrics  []payload.Metric
	Services []payload.Service
	BootTime int64
	Errs     []error // non-fatal, per-collector
}

// Collector holds the previous counter samples needed for rates. It is not
// safe for concurrent use; the runner drives it from one goroutine.
type Collector struct {
	opt Options

	prevCPU  *cpuSample
	prevDio  map[string]diskIO
	prevDioT time.Time
	prevNet  map[string]netIO
	prevNetT time.Time
	prevWin  *winCPU

	pending map[string]chan statRes // statfs calls still in flight (hung mounts)
}

// New returns a Collector with defaults filled in.
func New(opt Options) *Collector {
	if opt.ProcRoot == "" {
		opt.ProcRoot = "/proc"
	}
	if opt.SysRoot == "" {
		opt.SysRoot = "/sys"
	}
	if opt.Now == nil {
		opt.Now = time.Now
	}
	if opt.Statfs == nil {
		opt.Statfs = statfsSyscall
	}
	if opt.Run == nil {
		opt.Run = runCmd
	}
	if opt.GOOS == "" {
		opt.GOOS = runtime.GOOS
	}
	opt.Win = opt.Win.withDefaults()
	if opt.DarwinMounts == nil {
		opt.DarwinMounts = darwinMountsSyscall
	}
	return &Collector{opt: opt, pending: map[string]chan statRes{}}
}

// Collect runs every collector once.
func (c *Collector) Collect(ctx context.Context, s Settings) Result {
	now := c.opt.Now()
	ts := now.Unix()
	var r Result
	add := func(name string, v float64, labels map[string]string) {
		if !payload.Finite(v) {
			return
		}
		r.Metrics = append(r.Metrics, payload.Metric{Name: name, Value: v, Ts: ts, Labels: labels})
	}
	fail := func(err error) {
		if err != nil {
			r.Errs = append(r.Errs, err)
		}
	}

	switch c.opt.GOOS {
	case "windows":
		r.BootTime = c.collectWindows(now, add, fail)
		r.Services = c.winServices(ctx, s.WatchServices)
	case "darwin":
		r.BootTime = c.collectDarwin(ctx, now, s, add, fail)
		r.Services = c.darwinServices(ctx, s.WatchServices)
	default: // linux
		boot, err := c.cpu(now, add)
		fail(err)
		r.BootTime = boot
		fail(c.load(add))
		fail(c.memory(add))
		fail(c.disks(s.DiskExcludeFstypes, add))
		fail(c.diskIO(now, add))
		fail(c.network(now, add))
		fail(c.procCount(add))
		fail(c.uptime(add))
		r.Services = c.services(ctx, s.WatchServices)
	}

	sort.SliceStable(r.Metrics, func(i, j int) bool { return r.Metrics[i].Name < r.Metrics[j].Name })
	return r
}

func (c *Collector) proc(rel string) string { return filepath.Join(c.opt.ProcRoot, rel) }

func readFile(path string) (string, error) {
	b, err := os.ReadFile(path)
	return string(b), err
}

func runCmd(ctx context.Context, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).Output()
	if err != nil && len(out) > 0 {
		return out, nil
	}
	return out, err
}

func fields(s string) []string { return strings.Fields(s) }
