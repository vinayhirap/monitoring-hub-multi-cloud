package collect

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"cloudops-agent/internal/payload"
)

var errUnsupportedOS = errors.New("not supported on this operating system")

// WinDrive is one fixed local drive. Label is what the hub sees ("C:"); Root is what the OS is asked about ("C:\").
type WinDrive struct{ Label, Root string }

// WinAPI is the Windows system-call surface the collector needs. Every field is optional: a nil field is filled in
// with the real implementation on Windows and with an "unsupported" stub elsewhere (tests inject fakes).
type WinAPI struct {
	// SystemTimes is GetSystemTimes: 100 ns ticks. kernel INCLUDES idle.
	SystemTimes func() (idle, kernel, user uint64, err error)
	// Memory is GlobalMemoryStatusEx: physical bytes.
	Memory func() (total, avail uint64, err error)
	// FixedDrives lists DRIVE_FIXED volumes only (no removable, network, optical or RAM drives).
	FixedDrives func() ([]WinDrive, error)
	// DiskSpace is GetDiskFreeSpaceExW for a drive root: total and total-free bytes.
	DiskSpace func(root string) (total, free uint64, err error)
	// UptimeMillis is GetTickCount64.
	UptimeMillis func() (uint64, error)
	// ProcessCount is the number of running processes (EnumProcesses).
	ProcessCount func() (int, error)
}

func (w WinAPI) withDefaults() WinAPI {
	d := defaultWinAPI()
	if w.SystemTimes == nil {
		w.SystemTimes = d.SystemTimes
	}
	if w.Memory == nil {
		w.Memory = d.Memory
	}
	if w.FixedDrives == nil {
		w.FixedDrives = d.FixedDrives
	}
	if w.DiskSpace == nil {
		w.DiskSpace = d.DiskSpace
	}
	if w.UptimeMillis == nil {
		w.UptimeMillis = d.UptimeMillis
	}
	if w.ProcessCount == nil {
		w.ProcessCount = d.ProcessCount
	}
	return w
}

type winCPU struct{ idle, total uint64 }

// collectWindows emits: cpu_percent, mem_used_percent, mem_available_bytes, disk_used_percent{mount="C:"},
// proc_count, uptime_seconds. Not emitted on Windows (no honest equivalent without perf counters): load1/5/15,
// swap_used_percent, disk_inodes_used_percent, disk_*_bytes_per_s, net_*_bytes_per_s.
// It returns the boot time (epoch seconds, 0 when unknown).
func (c *Collector) collectWindows(now time.Time, add addFn, fail func(error)) int64 {
	w := c.opt.Win
	var boot int64

	// cpu: delta of GetSystemTimes between two passes; the first pass has no baseline and emits nothing.
	if idle, kernel, user, err := w.SystemTimes(); err != nil {
		fail(fmt.Errorf("cpu: %w", err))
	} else {
		cur := &winCPU{idle: idle, total: kernel + user}
		if p := c.prevWin; p != nil && cur.total > p.total && cur.idle >= p.idle {
			dTotal, dIdle := float64(cur.total-p.total), float64(cur.idle-p.idle)
			add("cpu_percent", clamp(100*(dTotal-dIdle)/dTotal, 0, 100), nil)
		}
		c.prevWin = cur
	}

	if total, avail, err := w.Memory(); err != nil {
		fail(fmt.Errorf("memory: %w", err))
	} else if total == 0 {
		fail(errors.New("memory: total is 0"))
	} else {
		if avail > total {
			avail = total
		}
		add("mem_used_percent", clamp(100*float64(total-avail)/float64(total), 0, 100), nil)
		add("mem_available_bytes", float64(avail), nil)
	}

	if drives, err := w.FixedDrives(); err != nil {
		fail(fmt.Errorf("disk: %w", err))
	} else {
		var errs []string
		for _, d := range drives {
			total, free, serr := w.DiskSpace(d.Root)
			if serr != nil {
				errs = append(errs, fmt.Sprintf("%s: %v", d.Label, serr))
				continue
			}
			if total == 0 {
				continue
			}
			if free > total {
				free = total
			}
			add("disk_used_percent", clamp(100*float64(total-free)/float64(total), 0, 100), map[string]string{"mount": d.Label})
		}
		if len(errs) > 0 {
			fail(fmt.Errorf("disk: %s", strings.Join(errs, "; ")))
		}
	}

	if n, err := w.ProcessCount(); err != nil {
		fail(fmt.Errorf("proc_count: %w", err))
	} else {
		add("proc_count", float64(n), nil)
	}

	if ms, err := w.UptimeMillis(); err != nil {
		fail(fmt.Errorf("uptime: %w", err))
	} else {
		up := float64(ms) / 1000
		add("uptime_seconds", up, nil)
		boot = now.Unix() - int64(up)
	}
	return boot
}

// winServices reports watched Windows services with ONE `sc query state= all`. Matching is by service key name,
// case-insensitive. STATE is read by its numeric code (locale independent): 4 running, 1 stopped, anything else
// (start/stop pending, paused) and unlisted services are "unknown". Windows has no "failed" state.
func (c *Collector) winServices(ctx context.Context, names []string) []payload.Service {
	return c.watched(names, func(valid []string) map[string]string {
		out, err := c.opt.Run(ctx, "sc.exe", "query", "state=", "all")
		if err != nil && len(out) == 0 {
			return nil
		}
		return parseScQuery(string(out))
	}, true)
}

// scState maps the numeric STATE code of `sc query` (locale independent) to the contract state: 4 running, 1 stopped,
// anything else (start/stop pending, paused, unparsable) unknown. Shared by the watch-list and the inventory parsers so the
// two can never disagree about a service.
func scState(code string) string {
	switch n, err := strconv.Atoi(code); {
	case err != nil:
		return "unknown"
	case n == 4:
		return "running"
	case n == 1:
		return "stopped"
	}
	return "unknown"
}

// parseScQuery returns lower-cased service name -> running|stopped|unknown.
func parseScQuery(out string) map[string]string {
	res := map[string]string{}
	cur := ""
	for _, line := range strings.Split(out, "\n") {
		l := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(l, "SERVICE_NAME:"):
			cur = strings.ToLower(strings.TrimSpace(strings.TrimPrefix(l, "SERVICE_NAME:")))
		case cur != "" && strings.HasPrefix(l, "STATE"):
			_, rest, ok := strings.Cut(l, ":")
			if !ok {
				continue
			}
			f := strings.Fields(rest)
			if len(f) == 0 {
				continue
			}
			res[cur] = scState(f[0])
			cur = ""
		}
	}
	return res
}
