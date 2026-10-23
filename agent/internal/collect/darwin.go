package collect

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"cloudops-agent/internal/payload"
)

// DarwinMount is one mounted filesystem as reported by getfsstat(2).
type DarwinMount struct{ Path, Fstype string }

// Filesystems that never describe real storage on macOS (in addition to the hub's disk_exclude_fstypes).
var darwinPseudoFS = map[string]bool{
	"devfs": true, "autofs": true, "nullfs": true, "fdesc": true, "volfs": true, "procfs": true,
	"synthfs": true, "cd9660": true, "udf": true, "osxfuse": true,
}

// Interfaces that are not a network path (loopback, tunnels to nowhere, Apple wireless-direct plumbing).
var darwinSkipIface = []string{"lo", "gif", "stf", "awdl", "llw", "anpi", "ap"}

// collectDarwin emits: cpu_percent, load1/5/15, mem_used_percent, mem_available_bytes, swap_used_percent,
// disk_used_percent{mount}, net_rx/tx_bytes_per_s{iface}, proc_count, uptime_seconds. Not emitted: disk inodes
// (APFS allocates them dynamically, so a percentage is meaningless) and disk_*_bytes_per_s.
func (c *Collector) collectDarwin(ctx context.Context, now time.Time, s Settings, add addFn, fail func(error)) int64 {
	run := func(name string, args ...string) (string, error) {
		out, err := c.opt.Run(ctx, name, args...)
		if err != nil && len(out) == 0 {
			return "", fmt.Errorf("%s: %w", name, err)
		}
		return string(out), nil
	}
	var boot int64

	// cpu: `top -l 2 -s 1` takes two samples one second apart; the SECOND is the live utilisation (the first is the
	// average since boot). Costs ~1 s per pass.
	if out, err := run("top", "-l", "2", "-n", "0", "-s", "1"); err != nil {
		fail(fmt.Errorf("cpu: %w", err))
	} else if idle, ok := parseTopIdle(out); !ok {
		fail(errors.New("cpu: no 'CPU usage' line in top output"))
	} else {
		add("cpu_percent", clamp(100-idle, 0, 100), nil)
	}

	if out, err := run("sysctl", "-n", "vm.loadavg"); err != nil {
		fail(fmt.Errorf("load: %w", err))
	} else if l, ok := parseLoadavg(out); !ok {
		fail(errors.New("load: unexpected vm.loadavg format"))
	} else {
		add("load1", l[0], nil)
		add("load5", l[1], nil)
		add("load15", l[2], nil)
	}

	c.darwinMemory(run, add, fail)

	if out, err := run("sysctl", "-n", "kern.boottime"); err != nil {
		fail(fmt.Errorf("uptime: %w", err))
	} else if b, ok := parseBoottime(out); !ok {
		fail(errors.New("uptime: unexpected kern.boottime format"))
	} else {
		boot = b
		if up := now.Unix() - b; up >= 0 {
			add("uptime_seconds", float64(up), nil)
		}
	}

	fail(c.darwinDisks(s.DiskExcludeFstypes, add))

	if out, err := run("netstat", "-ib"); err != nil {
		fail(fmt.Errorf("network: %w", err))
	} else {
		c.rates(now, parseNetstat(out), add)
	}

	if out, err := run("ps", "-A", "-o", "pid="); err != nil {
		fail(fmt.Errorf("proc_count: %w", err))
	} else {
		n := 0
		for _, l := range strings.Split(out, "\n") {
			if isDigits(strings.TrimSpace(l)) {
				n++
			}
		}
		add("proc_count", float64(n), nil)
	}
	return boot
}

func (c *Collector) darwinMemory(run func(string, ...string) (string, error), add addFn, fail func(error)) {
	memOut, err := run("sysctl", "-n", "hw.memsize")
	if err != nil {
		fail(fmt.Errorf("memory: %w", err))
		return
	}
	total, perr := strconv.ParseUint(strings.TrimSpace(memOut), 10, 64)
	if perr != nil || total == 0 {
		fail(errors.New("memory: unexpected hw.memsize"))
		return
	}
	vmOut, err := run("vm_stat")
	if err != nil {
		fail(fmt.Errorf("memory: %w", err))
		return
	}
	page, pages, ok := parseVMStat(vmOut)
	if !ok {
		fail(errors.New("memory: unexpected vm_stat output"))
		return
	}
	// Available = what the kernel can hand out without paging: free + speculative + inactive + purgeable.
	avail := (pages["free"] + pages["speculative"] + pages["inactive"] + pages["purgeable"]) * page
	if avail > total {
		avail = total
	}
	add("mem_used_percent", clamp(100*float64(total-avail)/float64(total), 0, 100), nil)
	add("mem_available_bytes", float64(avail), nil)

	if swOut, err := run("sysctl", "-n", "vm.swapusage"); err != nil {
		fail(fmt.Errorf("swap: %w", err))
	} else if st, su, ok := parseSwapUsage(swOut); ok && st > 0 { // no swap in use/configured -> no series
		add("swap_used_percent", clamp(100*su/st, 0, 100), nil)
	}
}

func (c *Collector) darwinDisks(exclude []string, add addFn) error {
	mounts, err := c.opt.DarwinMounts()
	if err != nil {
		return fmt.Errorf("disk: %w", err)
	}
	ex := map[string]bool{}
	for _, t := range exclude {
		ex[t] = true
	}
	seen := map[string]bool{}
	var errs []string
	for _, m := range mounts {
		if m.Path != "/" {
			// /System/Volumes/* are the APFS system/data/VM/preboot volumes: they share the container with "/" and
			// would only repeat its number.
			if ex[m.Fstype] || darwinPseudoFS[m.Fstype] || strings.HasPrefix(m.Path, "/System/Volumes/") || strings.HasPrefix(m.Path, "/private/var/vm") {
				continue
			}
		}
		if seen[m.Path] {
			continue
		}
		st, serr := c.statfsTimeout(m.Path)
		if serr != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", m.Path, serr))
			continue
		}
		if st.Blocks == 0 {
			continue
		}
		seen[m.Path] = true
		used := st.Blocks - minU(st.Bfree, st.Blocks)
		if denom := used + st.Bavail; denom > 0 {
			add("disk_used_percent", clamp(100*float64(used)/float64(denom), 0, 100), map[string]string{"mount": m.Path})
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("disk: %s", strings.Join(errs, "; "))
	}
	return nil
}

// rates turns cumulative per-interface byte counters into per-second rates against the previous pass.
func (c *Collector) rates(now time.Time, cur map[string]netIO, add addFn) {
	prev, prevT := c.prevNet, c.prevNetT
	c.prevNet, c.prevNetT = cur, now
	if prev == nil {
		return
	}
	el := now.Sub(prevT).Seconds()
	if el <= 0 {
		return
	}
	for name, cu := range cur {
		pv, ok := prev[name]
		if !ok || cu.rx < pv.rx || cu.tx < pv.tx {
			continue
		}
		l := map[string]string{"iface": name}
		add("net_rx_bytes_per_s", float64(cu.rx-pv.rx)/el, l)
		add("net_tx_bytes_per_s", float64(cu.tx-pv.tx)/el, l)
	}
}

// darwinServices reports watched launchd labels with ONE `launchctl list`. A numeric PID is "running"; "-" is
// "failed" when the last exit status is non-zero, else "stopped"; a label launchd does not list is "unknown".
func (c *Collector) darwinServices(ctx context.Context, names []string) []payload.Service {
	return c.watched(names, func(valid []string) map[string]string {
		out, err := c.opt.Run(ctx, "launchctl", "list")
		if err != nil && len(out) == 0 {
			return nil
		}
		return parseLaunchctlList(string(out))
	}, false)
}

// ── parsers (pure; unit-tested with real macOS output samples) ──────────────────────────────────────────────

var (
	reTopIdle  = regexp.MustCompile(`([0-9]+(?:\.[0-9]+)?)%\s+idle`)
	rePageSize = regexp.MustCompile(`page size of (\d+) bytes`)
	reSwap     = regexp.MustCompile(`total = ([0-9.]+)([KMGT])\s+used = ([0-9.]+)([KMGT])`)
	reBoottime = regexp.MustCompile(`sec = (\d+)`)
)

// parseTopIdle returns the idle percentage from the LAST "CPU usage:" line.
func parseTopIdle(out string) (float64, bool) {
	var idle float64
	found := false
	for _, l := range strings.Split(out, "\n") {
		if !strings.Contains(l, "CPU usage:") {
			continue
		}
		if m := reTopIdle.FindStringSubmatch(l); m != nil {
			if v, err := strconv.ParseFloat(m[1], 64); err == nil {
				idle, found = v, true
			}
		}
	}
	return idle, found
}

// parseLoadavg parses `sysctl -n vm.loadavg`: "{ 1.52 1.60 1.63 }".
func parseLoadavg(out string) ([3]float64, bool) {
	var l [3]float64
	f := strings.Fields(strings.Trim(strings.TrimSpace(out), "{}"))
	if len(f) < 3 {
		return l, false
	}
	for i := 0; i < 3; i++ {
		v, err := strconv.ParseFloat(f[i], 64)
		if err != nil {
			return l, false
		}
		l[i] = v
	}
	return l, true
}

// parseVMStat returns the page size and the counters keyed without the "Pages " prefix ("free", "inactive", ...).
func parseVMStat(out string) (page uint64, pages map[string]uint64, ok bool) {
	pages = map[string]uint64{}
	for _, l := range strings.Split(out, "\n") {
		if m := rePageSize.FindStringSubmatch(l); m != nil {
			page, _ = strconv.ParseUint(m[1], 10, 64)
			continue
		}
		k, v, cut := strings.Cut(l, ":")
		if !cut || !strings.HasPrefix(k, "Pages ") {
			continue
		}
		n, err := strconv.ParseUint(strings.TrimSuffix(strings.TrimSpace(v), "."), 10, 64)
		if err == nil {
			pages[strings.TrimPrefix(k, "Pages ")] = n
		}
	}
	_, hasFree := pages["free"]
	return page, pages, page > 0 && hasFree
}

func unitBytes(v float64, u string) float64 {
	switch u {
	case "K":
		return v * 1024
	case "M":
		return v * 1024 * 1024
	case "G":
		return v * 1024 * 1024 * 1024
	case "T":
		return v * 1024 * 1024 * 1024 * 1024
	}
	return v
}

// parseSwapUsage parses `sysctl -n vm.swapusage`: "total = 2048.00M  used = 1024.00M  free = 1024.00M  (encrypted)".
func parseSwapUsage(out string) (total, used float64, ok bool) {
	m := reSwap.FindStringSubmatch(out)
	if m == nil {
		return 0, 0, false
	}
	t, e1 := strconv.ParseFloat(m[1], 64)
	u, e2 := strconv.ParseFloat(m[3], 64)
	if e1 != nil || e2 != nil {
		return 0, 0, false
	}
	return unitBytes(t, m[2]), unitBytes(u, m[4]), true
}

// parseBoottime parses `sysctl -n kern.boottime`: "{ sec = 1700000000, usec = 123456 } Tue Nov 14 ...".
func parseBoottime(out string) (int64, bool) {
	m := reBoottime.FindStringSubmatch(out)
	if m == nil {
		return 0, false
	}
	v, err := strconv.ParseInt(m[1], 10, 64)
	return v, err == nil && v > 0
}

// parseNetstat parses `netstat -ib`. Only the per-interface "<Link#N>" rows are used. Columns are counted from the
// right because the Address column is empty for some interfaces: ..., Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll.
func parseNetstat(out string) map[string]netIO {
	res := map[string]netIO{}
	for _, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if len(f) < 8 || f[0] == "Name" || !strings.HasPrefix(f[2], "<Link#") {
			continue
		}
		name := strings.TrimSuffix(f[0], "*")
		skip := false
		for _, p := range darwinSkipIface {
			if strings.HasPrefix(name, p) && (len(name) == len(p) || (name[len(p)] >= '0' && name[len(p)] <= '9')) {
				skip = true
				break
			}
		}
		if skip {
			continue
		}
		rx, e1 := strconv.ParseUint(f[len(f)-5], 10, 64)
		tx, e2 := strconv.ParseUint(f[len(f)-2], 10, 64)
		if e1 != nil || e2 != nil {
			continue
		}
		res[name] = netIO{rx, tx}
	}
	return res
}

// parseLaunchctlList parses `launchctl list` ("PID\tStatus\tLabel", one header line).
func parseLaunchctlList(out string) map[string]string {
	res := map[string]string{}
	for i, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if i == 0 && len(f) > 0 && f[0] == "PID" || len(f) < 3 {
			continue
		}
		pid, status, label := f[0], f[1], f[2]
		switch {
		case isDigits(pid):
			res[label] = "running"
		case status != "0":
			res[label] = "failed"
		default:
			res[label] = "stopped"
		}
	}
	return res
}
