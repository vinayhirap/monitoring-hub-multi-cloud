package collect

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type addFn func(name string, v float64, labels map[string]string)

type cpuSample struct{ total, busy uint64 }

// cpu reads /proc/stat: cpu_percent (rate, needs two samples) and the boot
// time used in the payload's host block.
func (c *Collector) cpu(_ time.Time, add addFn) (int64, error) {
	txt, err := readFile(c.proc("stat"))
	if err != nil {
		return 0, err
	}
	var boot int64
	var cur *cpuSample
	for _, line := range strings.Split(txt, "\n") {
		f := fields(line)
		if len(f) == 0 {
			continue
		}
		switch f[0] {
		case "cpu":
			if len(f) < 5 {
				return boot, fmt.Errorf("cpu: short /proc/stat line")
			}
			var total, idle uint64
			// user nice system idle iowait irq softirq steal; guest/guest_nice are
			// already included in user/nice and must not be added again.
			for i := 1; i < len(f) && i <= 8; i++ {
				v, perr := strconv.ParseUint(f[i], 10, 64)
				if perr != nil {
					return boot, fmt.Errorf("cpu: %w", perr)
				}
				total += v
				if i == 4 || i == 5 { // idle, iowait
					idle += v
				}
			}
			cur = &cpuSample{total: total, busy: total - idle}
		case "btime":
			if len(f) >= 2 {
				boot, _ = strconv.ParseInt(f[1], 10, 64)
			}
		}
	}
	if cur == nil {
		return boot, fmt.Errorf("cpu: no aggregate cpu line in /proc/stat")
	}
	prev := c.prevCPU
	c.prevCPU = cur
	if prev == nil || cur.total <= prev.total || cur.busy < prev.busy {
		return boot, nil // first sample, or counters reset: no baseline, skip
	}
	pct := 100 * float64(cur.busy-prev.busy) / float64(cur.total-prev.total)
	add("cpu_percent", clamp(pct, 0, 100), nil)
	return boot, nil
}

func (c *Collector) load(add addFn) error {
	txt, err := readFile(c.proc("loadavg"))
	if err != nil {
		return err
	}
	f := fields(txt)
	if len(f) < 3 {
		return fmt.Errorf("loadavg: unexpected format")
	}
	for i, name := range []string{"load1", "load5", "load15"} {
		v, perr := strconv.ParseFloat(f[i], 64)
		if perr != nil {
			return fmt.Errorf("loadavg: %w", perr)
		}
		add(name, v, nil)
	}
	return nil
}

// memory reads /proc/meminfo. mem_used_percent is based on MemAvailable (the
// kernel's own estimate), falling back to free+buffers+cached on kernels < 3.14.
func (c *Collector) memory(add addFn) error {
	txt, err := readFile(c.proc("meminfo"))
	if err != nil {
		return err
	}
	kb := map[string]float64{}
	for _, line := range strings.Split(txt, "\n") {
		k, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		f := fields(rest)
		if len(f) == 0 {
			continue
		}
		if v, perr := strconv.ParseFloat(f[0], 64); perr == nil {
			kb[k] = v
		}
	}
	total := kb["MemTotal"]
	if total <= 0 {
		return fmt.Errorf("meminfo: MemTotal missing")
	}
	avail, ok := kb["MemAvailable"]
	if !ok {
		avail = kb["MemFree"] + kb["Buffers"] + kb["Cached"]
	}
	if avail > total {
		avail = total
	}
	add("mem_used_percent", clamp(100*(total-avail)/total, 0, 100), nil)
	add("mem_available_bytes", avail*1024, nil)
	if st := kb["SwapTotal"]; st > 0 { // no swap configured -> no series
		add("swap_used_percent", clamp(100*(st-kb["SwapFree"])/st, 0, 100), nil)
	}
	return nil
}

func (c *Collector) uptime(add addFn) error {
	txt, err := readFile(c.proc("uptime"))
	if err != nil {
		return err
	}
	f := fields(txt)
	if len(f) < 1 {
		return fmt.Errorf("uptime: unexpected format")
	}
	v, err := strconv.ParseFloat(f[0], 64)
	if err != nil {
		return fmt.Errorf("uptime: %w", err)
	}
	add("uptime_seconds", v, nil)
	return nil
}

// procCount counts numeric directories in /proc (processes, not threads).
func (c *Collector) procCount(add addFn) error {
	d, err := os.Open(c.opt.ProcRoot)
	if err != nil {
		return err
	}
	defer d.Close()
	names, err := d.Readdirnames(-1)
	if err != nil {
		return err
	}
	n := 0
	for _, name := range names {
		if isDigits(name) {
			n++
		}
	}
	add("proc_count", float64(n), nil)
	return nil
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
