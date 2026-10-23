package collect

import (
	"context"
	"errors"
	"math"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"cloudops-agent/internal/payload"
)

type fakeHost struct {
	t    *testing.T
	proc string
	sys  string
}

func newFakeHost(t *testing.T) *fakeHost {
	t.Helper()
	d := t.TempDir()
	h := &fakeHost{t: t, proc: filepath.Join(d, "proc"), sys: filepath.Join(d, "sys")}
	for _, p := range []string{"self", "net", "1", "42", "1337", "sys-not-a-pid", "acpi"} {
		if err := os.MkdirAll(filepath.Join(h.proc, p), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return h
}

func (h *fakeHost) write(rel, content string) {
	h.t.Helper()
	p := filepath.Join(h.proc, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		h.t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		h.t.Fatal(err)
	}
}

func (h *fakeHost) sysBlock(names ...string) {
	for _, n := range names {
		if err := os.MkdirAll(filepath.Join(h.sys, "block", n), 0o755); err != nil {
			h.t.Fatal(err)
		}
	}
}

func get(ms []payload.Metric, name string, labels map[string]string) (payload.Metric, bool) {
	for _, m := range ms {
		if m.Name != name {
			continue
		}
		ok := true
		for k, v := range labels {
			if m.Labels[k] != v {
				ok = false
			}
		}
		if ok {
			return m, true
		}
	}
	return payload.Metric{}, false
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func baseline(h *fakeHost) {
	h.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 1 1 1 0 0 0 0 0 0\nbtime 1759000000\n")
	h.write("loadavg", "0.50 1.25 2.00 1/100 999\n")
	h.write("meminfo", "MemTotal: 1000000 kB\nMemFree: 100000 kB\nMemAvailable: 250000 kB\nSwapTotal: 2000 kB\nSwapFree: 500 kB\n")
	h.write("uptime", "12345.67 9999.00\n")
	h.write("net/dev", "Inter-|   Receive |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"+
		"    lo: 5000 1 0 0 0 0 0 0 5000 1 0 0 0 0 0 0\n  eth0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n")
	h.write("diskstats", "   8       0 sda 1 0 100 0 1 0 200 0 0 0 0\n"+
		"   8       1 sda1 1 0 999 0 1 0 999 0 0 0 0\n"+
		"   7       0 loop0 1 0 999 0 1 0 999 0 0 0 0\n"+
		" 253       0 dm-0 1 0 999 0 1 0 999 0 0 0 0\n")
	h.sysBlock("sda", "loop0", "dm-0")
	h.write("self/mounts", "/dev/sda1 / ext4 rw 0 0\n"+
		"tmpfs /run tmpfs rw 0 0\n"+
		"proc /proc proc rw 0 0\n"+
		"/dev/sdb1 /mnt/with\\040space xfs rw 0 0\n"+
		"/dev/sda1 /var/lib/cloudops-agent ext4 rw 0 0\n"+ // sandbox bind mount of the root device
		"cgroup2 /sys/fs/cgroup cgroup2 rw 0 0\n"+
		"nfs:/x /mnt/hung nfs rw 0 0\n")
}

func fakeStatfs(path string) (Statfs, error) {
	switch path {
	case "/":
		return Statfs{Blocks: 1000, Bfree: 400, Bavail: 300, Files: 100, Ffree: 90}, nil
	case "/mnt/with space":
		return Statfs{Blocks: 200, Bfree: 100, Bavail: 100, Files: 0, Ffree: 0}, nil // no inode info (btrfs-like)
	case "/mnt/hung":
		return Statfs{}, errors.New("stale file handle")
	}
	return Statfs{}, errors.New("unexpected path " + path)
}

func newCollector(h *fakeHost, now *time.Time, run func(context.Context, string, ...string) ([]byte, error)) *Collector {
	return New(Options{ProcRoot: h.proc, SysRoot: h.sys, Now: func() time.Time { return *now }, Statfs: fakeStatfs, Run: run})
}

func TestFirstSampleSkipsRates(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, nil)
	r := c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs", "proc"}})
	for _, n := range []string{"cpu_percent", "disk_read_bytes_per_s", "disk_write_bytes_per_s", "net_rx_bytes_per_s", "net_tx_bytes_per_s"} {
		if _, ok := get(r.Metrics, n, nil); ok {
			t.Errorf("%s must not be emitted on the first sample", n)
		}
	}
	for _, m := range r.Metrics {
		if m.Value == 0 && (m.Name == "cpu_percent" || m.Name == "net_rx_bytes_per_s") {
			t.Errorf("rate %s sent as 0", m.Name)
		}
	}
	if r.BootTime != 1759000000 {
		t.Errorf("boot time = %d", r.BootTime)
	}
}

func TestGauges(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, nil)
	r := c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs", "proc"}})
	for _, e := range r.Errs {
		t.Logf("non-fatal: %v", e)
	}
	want := map[string]float64{
		"load1": 0.5, "load5": 1.25, "load15": 2.0,
		"mem_used_percent": 75, "mem_available_bytes": 250000 * 1024,
		"swap_used_percent": 75, "uptime_seconds": 12345.67, "proc_count": 3,
	}
	for n, v := range want {
		m, ok := get(r.Metrics, n, nil)
		if !ok || !near(m.Value, v) {
			t.Errorf("%s = %v (present=%v), want %v", n, m.Value, ok, v)
		}
		if m.Ts != 1760000000 {
			t.Errorf("%s ts = %d", n, m.Ts)
		}
	}
	// df-style: used = 600, avail = 300 -> 600/900
	if m, ok := get(r.Metrics, "disk_used_percent", map[string]string{"mount": "/"}); !ok || !near(m.Value, 100*600.0/900.0) {
		t.Errorf("disk_used_percent{/} = %v present=%v", m.Value, ok)
	}
	if m, ok := get(r.Metrics, "disk_inodes_used_percent", map[string]string{"mount": "/"}); !ok || !near(m.Value, 10) {
		t.Errorf("inodes{/} = %v present=%v", m.Value, ok)
	}
	// Escaped mount path decoded; fs without inode info has no inode series.
	if m, ok := get(r.Metrics, "disk_used_percent", map[string]string{"mount": "/mnt/with space"}); !ok || !near(m.Value, 50) {
		t.Errorf("disk_used_percent{/mnt/with space} = %v present=%v", m.Value, ok)
	}
	if _, ok := get(r.Metrics, "disk_inodes_used_percent", map[string]string{"mount": "/mnt/with space"}); ok {
		t.Error("inode series emitted for fs with Files=0")
	}
	// Excluded / pseudo / duplicate-device / failing mounts do not appear.
	for _, mnt := range []string{"/run", "/proc", "/sys/fs/cgroup", "/var/lib/cloudops-agent", "/mnt/hung"} {
		if _, ok := get(r.Metrics, "disk_used_percent", map[string]string{"mount": mnt}); ok {
			t.Errorf("mount %s should be skipped", mnt)
		}
	}
	if len(r.Errs) == 0 {
		t.Error("expected the hung/stale mount error to be reported as non-fatal")
	}
}

func TestRates(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, nil)
	c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs"}})

	now = now.Add(10 * time.Second)
	// cpu: +60 busy (user 30 + system 30), +40 idle => 60%.
	h.write("stat", "cpu  130 0 130 840 0 0 0 0 0 0\nbtime 1759000000\n")
	h.write("net/dev", "h\nh\n    lo: 9999 1 0 0 0 0 0 0 9999 1 0 0 0 0 0 0\n  eth0: 6000 1 0 0 0 0 0 0 4000 1 0 0 0 0 0 0\n")
	// sda: +200 sectors read, +400 written; partitions/loop/dm changes must be ignored.
	h.write("diskstats", "   8       0 sda 1 0 300 0 1 0 600 0 0 0 0\n"+
		"   8       1 sda1 1 0 5000 0 1 0 5000 0 0 0 0\n"+
		"   7       0 loop0 1 0 5000 0 1 0 5000 0 0 0 0\n"+
		" 253       0 dm-0 1 0 5000 0 1 0 5000 0 0 0 0\n")
	r := c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs"}})

	if m, ok := get(r.Metrics, "cpu_percent", nil); !ok || !near(m.Value, 60) {
		t.Errorf("cpu_percent = %v present=%v", m.Value, ok)
	}
	if m, ok := get(r.Metrics, "disk_read_bytes_per_s", nil); !ok || !near(m.Value, 200*512/10.0) {
		t.Errorf("disk_read = %v present=%v", m.Value, ok)
	}
	if m, ok := get(r.Metrics, "disk_write_bytes_per_s", nil); !ok || !near(m.Value, 400*512/10.0) {
		t.Errorf("disk_write = %v present=%v", m.Value, ok)
	}
	if m, ok := get(r.Metrics, "net_rx_bytes_per_s", map[string]string{"iface": "eth0"}); !ok || !near(m.Value, 500) {
		t.Errorf("net rx = %v present=%v", m.Value, ok)
	}
	if m, ok := get(r.Metrics, "net_tx_bytes_per_s", map[string]string{"iface": "eth0"}); !ok || !near(m.Value, 200) {
		t.Errorf("net tx = %v present=%v", m.Value, ok)
	}
	if _, ok := get(r.Metrics, "net_rx_bytes_per_s", map[string]string{"iface": "lo"}); ok {
		t.Error("loopback must be skipped")
	}
}

func TestCounterResetSkipsSample(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, nil)
	c.Collect(context.Background(), Settings{})
	now = now.Add(10 * time.Second)
	h.write("stat", "cpu  1 0 1 1 0 0 0 0 0 0\n") // went backwards (reboot / wrap)
	h.write("net/dev", "h\nh\n  eth0: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0\n")
	h.write("diskstats", "   8       0 sda 1 0 1 0 1 0 1 0 0 0 0\n")
	r := c.Collect(context.Background(), Settings{})
	for _, n := range []string{"cpu_percent", "net_rx_bytes_per_s", "disk_read_bytes_per_s"} {
		if _, ok := get(r.Metrics, n, nil); ok {
			t.Errorf("%s emitted after a counter reset", n)
		}
	}
	// Next interval has a fresh baseline and works again.
	now = now.Add(10 * time.Second)
	h.write("stat", "cpu  11 0 11 21 0 0 0 0 0 0\n") // +20 busy, +20 idle
	r = c.Collect(context.Background(), Settings{})
	if m, ok := get(r.Metrics, "cpu_percent", nil); !ok || !near(m.Value, 50) {
		t.Errorf("cpu_percent after recovery = %v present=%v", m.Value, ok)
	}
}

func TestMissingFilesAreNonFatal(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	if err := os.Remove(filepath.Join(h.proc, "meminfo")); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, nil)
	r := c.Collect(context.Background(), Settings{})
	if len(r.Errs) == 0 {
		t.Error("expected an error for missing meminfo")
	}
	if _, ok := get(r.Metrics, "load1", nil); !ok {
		t.Error("other collectors must still run")
	}
}

func TestNoSwapNoSeries(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	h.write("meminfo", "MemTotal: 1000 kB\nMemFree: 1 kB\nBuffers: 100 kB\nCached: 400 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
	now := time.Unix(1760000000, 0)
	r := newCollector(h, &now, nil).Collect(context.Background(), Settings{})
	if _, ok := get(r.Metrics, "swap_used_percent", nil); ok {
		t.Error("swap series emitted although no swap is configured")
	}
	// Fallback when MemAvailable is absent: avail = free+buffers+cached = 501.
	if m, ok := get(r.Metrics, "mem_used_percent", nil); !ok || !near(m.Value, 100*(1000-501)/1000.0) {
		t.Errorf("mem_used_percent fallback = %v", m.Value)
	}
}

func TestStatfsHangDoesNotBlockOrPileUp(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	old := statTimeout
	statTimeout = 150 * time.Millisecond
	defer func() { statTimeout = old }()
	block := make(chan struct{})
	var calls atomic.Int32
	now := time.Unix(1760000000, 0)
	c := New(Options{ProcRoot: h.proc, SysRoot: h.sys, Now: func() time.Time { return now },
		Statfs: func(p string) (Statfs, error) {
			if p == "/" {
				calls.Add(1)
				<-block
			}
			return Statfs{Blocks: 10, Bfree: 5, Bavail: 5}, nil
		}})
	start := time.Now()
	r1 := c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs"}})
	if d := time.Since(start); d > 2*statTimeout+time.Second {
		t.Fatalf("collect blocked for %v", d)
	}
	if len(r1.Errs) == 0 {
		t.Error("timeout should be reported")
	}
	c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"tmpfs"}})
	if n := calls.Load(); n != 1 {
		t.Errorf("hung statfs was re-spawned: calls=%d", n)
	}
	close(block)
}

func TestServices(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	var gotArgs []string
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		gotArgs = append([]string{name}, args...)
		return []byte("active\ninactive\nfailed\nactivating\n"), errors.New("exit status 3")
	}
	c := newCollector(h, &now, run)
	svc := c.services(context.Background(), []string{"nginx", "sshd", "app.service", "flappy@1", "nginx", "--now", "bad name; rm -rf /"})
	want := []payload.Service{
		{Name: "nginx", State: "running"}, {Name: "sshd", State: "stopped"},
		{Name: "app.service", State: "failed"}, {Name: "flappy@1", State: "unknown"},
		{Name: "--now", State: "unknown"}, {Name: "bad name; rm -rf /", State: "unknown"},
	}
	if len(svc) != len(want) {
		t.Fatalf("got %+v", svc)
	}
	for i := range want {
		if svc[i] != want[i] {
			t.Errorf("svc[%d] = %+v, want %+v", i, svc[i], want[i])
		}
	}
	// One systemctl call, option-terminated, only valid unit names, no duplicates.
	exp := []string{"systemctl", "is-active", "--", "nginx", "sshd", "app.service", "flappy@1"}
	if len(gotArgs) != len(exp) {
		t.Fatalf("args = %v", gotArgs)
	}
	for i := range exp {
		if gotArgs[i] != exp[i] {
			t.Errorf("args[%d] = %q want %q", i, gotArgs[i], exp[i])
		}
	}
}

func TestServicesEmptyIsNonNilAndFailuresAreUnknown(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	c := newCollector(h, &now, func(context.Context, string, ...string) ([]byte, error) { return nil, errors.New("not found") })
	if s := c.services(context.Background(), nil); s == nil || len(s) != 0 {
		t.Errorf("empty watch list must give empty non-nil slice, got %#v", s)
	}
	s := c.services(context.Background(), []string{"a", "b"})
	if len(s) != 2 || s[0].State != "unknown" || s[1].State != "unknown" {
		t.Errorf("failed systemctl must give unknown: %+v", s)
	}
	// Line-count mismatch is treated as unknown rather than mis-assigning states.
	c2 := newCollector(h, &now, func(context.Context, string, ...string) ([]byte, error) { return []byte("active\n"), nil })
	s = c2.services(context.Background(), []string{"a", "b"})
	if s[0].State != "unknown" || s[1].State != "unknown" {
		t.Errorf("mismatch must give unknown: %+v", s)
	}
}

func TestNonFiniteValuesDropped(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	h.write("loadavg", "NaN +Inf 1.0 1/1 1\n")
	now := time.Unix(1760000000, 0)
	r := newCollector(h, &now, nil).Collect(context.Background(), Settings{})
	for _, m := range r.Metrics {
		if !payload.Finite(m.Value) {
			t.Errorf("non-finite value for %s", m.Name)
		}
	}
}

func TestUnescapeMount(t *testing.T) {
	cases := map[string]string{`/a\040b`: "/a b", `/tab\011x`: "/tab\tx", `/back\134slash`: `/back\slash`, `/plain`: "/plain", `/trail\04`: `/trail\04`}
	for in, want := range cases {
		if got := unescapeMount(in); got != want {
			t.Errorf("unescapeMount(%q) = %q want %q", in, got, want)
		}
	}
}
