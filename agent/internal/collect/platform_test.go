package collect

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"
)

// ── fixtures: real output shapes from macOS 14 and Windows Server 2022 ───────────────────────────────────────

const topOut = `Processes: 412 total, 3 running, 409 sleeping, 1893 threads
2024/05/01 10:00:00
Load Avg: 1.52, 1.60, 1.63
CPU usage: 12.50% user, 9.37% sys, 78.12% idle
SharedLibs: 600M resident, 100M data, 50M linkedit.
Processes: 412 total, 2 running, 410 sleeping, 1890 threads
2024/05/01 10:00:01
Load Avg: 1.52, 1.60, 1.63
CPU usage: 3.12% user, 5.00% sys, 91.88% idle
`

const vmStatOut = `Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           50000.
Pages speculative:                         5000.
Pages throttled:                              0.
Pages wired down:                         90000.
Pages purgeable:                           1000.
"Translation faults":                 123456789.
Pages occupied by compressor:             20000.
`

const netstatOut = `Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                           100     0       5000      100     0       5000     0
lo0        16384 127           127.0.0.1            100     -       5000      100     -       5000     -
en0        1500  <Link#6>    aa:bb:cc:dd:ee:ff   1000     0    1000000     800     0     400000     0
en0        1500  192.168.1     192.168.1.5        1000     -    1000000     800     -     400000     -
utun3      1380  <Link#14>                         50     0       9000      60     0       7000     0
awdl0      1500  <Link#12>   aa:bb:cc:dd:ee:00      0     0          0       0     0          0     0
en5*       1500  <Link#9>    aa:bb:cc:dd:ee:11     10     0       2000      10     0       3000     0
`

const scOut = `
SERVICE_NAME: Spooler
DISPLAY_NAME: Print Spooler
        TYPE               : 110  WIN32_OWN_PROCESS  (interactive)
        STATE              : 4  RUNNING
                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)

SERVICE_NAME: W32Time
DISPLAY_NAME: Windows Time
        TYPE               : 20  WIN32_SHARE_PROCESS
        STATE              : 1  STOPPED
                                (NOT_STOPPABLE, NOT_PAUSABLE, IGNORES_SHUTDOWN)

SERVICE_NAME: BITS
DISPLAY_NAME: Background Intelligent Transfer Service
        TYPE               : 20  WIN32_SHARE_PROCESS
        STATE              : 2  START_PENDING
`

const launchctlOut = "PID\tStatus\tLabel\n" +
	"412\t0\tcom.example.web\n" +
	"-\t0\tcom.example.idle\n" +
	"-\t78\tcom.example.crashed\n"

func nearf(t *testing.T, got, want float64, what string) {
	t.Helper()
	if math.Abs(got-want) > 0.01 {
		t.Fatalf("%s = %v, want %v", what, got, want)
	}
}

func byName(r Result, name, labelKey, labelVal string) (float64, bool) {
	for _, m := range r.Metrics {
		if m.Name == name && (labelKey == "" || m.Labels[labelKey] == labelVal) {
			return m.Value, true
		}
	}
	return 0, false
}

// ── macOS parsers ────────────────────────────────────────────────────────────────────────────────────────────

func TestParseTopIdleUsesTheLastSample(t *testing.T) {
	idle, ok := parseTopIdle(topOut)
	if !ok {
		t.Fatal("no idle parsed")
	}
	nearf(t, idle, 91.88, "idle")
	if _, ok := parseTopIdle("nothing here"); ok {
		t.Fatal("garbage must not parse")
	}
}

func TestParseLoadavgVMStatSwapBoottime(t *testing.T) {
	l, ok := parseLoadavg("{ 1.52 1.60 1.63 }\n")
	if !ok || l[0] != 1.52 || l[2] != 1.63 {
		t.Fatalf("loadavg %v %v", l, ok)
	}
	if _, ok := parseLoadavg("{ 1 2 }"); ok {
		t.Fatal("short loadavg must fail")
	}
	page, pages, ok := parseVMStat(vmStatOut)
	if !ok || page != 16384 || pages["free"] != 10000 || pages["inactive"] != 50000 || pages["purgeable"] != 1000 {
		t.Fatalf("vm_stat: %d %v %v", page, pages, ok)
	}
	if _, _, ok := parseVMStat("garbage"); ok {
		t.Fatal("garbage vm_stat must fail")
	}
	tot, used, ok := parseSwapUsage("total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)")
	if !ok || tot != 2048*1024*1024 || math.Abs(used-1024.5*1024*1024) > 1 {
		t.Fatalf("swap %v %v %v", tot, used, ok)
	}
	b, ok := parseBoottime("{ sec = 1714550000, usec = 123456 } Wed May  1 08:33:20 2024")
	if !ok || b != 1714550000 {
		t.Fatalf("boottime %d %v", b, ok)
	}
}

func TestParseNetstatKeepsRealInterfacesOnly(t *testing.T) {
	got := parseNetstat(netstatOut)
	if len(got) != 3 {
		t.Fatalf("want en0, en5, utun3; got %v", got)
	}
	if got["en0"] != (netIO{1000000, 400000}) {
		t.Fatalf("en0 %v (columns must be counted from the right)", got["en0"])
	}
	if got["utun3"] != (netIO{9000, 7000}) { // no Address column: 10 fields instead of 11
		t.Fatalf("utun3 %v", got["utun3"])
	}
	if _, ok := got["en5"]; !ok {
		t.Fatal("the down-marker * must be stripped")
	}
	for _, skipped := range []string{"lo0", "awdl0"} {
		if _, ok := got[skipped]; ok {
			t.Fatalf("%s must be skipped", skipped)
		}
	}
}

func TestParseLaunchctlList(t *testing.T) {
	m := parseLaunchctlList(launchctlOut)
	if m["com.example.web"] != "running" || m["com.example.idle"] != "stopped" || m["com.example.crashed"] != "failed" {
		t.Fatalf("%v", m)
	}
}

// ── macOS flow ────────────────────────────────────────────────────────────────────────────────────────────────

type fakeRun map[string]string

func (f fakeRun) run(_ context.Context, name string, args ...string) ([]byte, error) {
	key := name + " " + strings.Join(args, " ")
	for prefix, out := range f {
		if strings.HasPrefix(key, prefix) {
			return []byte(out), nil
		}
	}
	return nil, errors.New("no such command: " + key)
}

func darwinCollector(now *time.Time, extra fakeRun) *Collector {
	cmds := fakeRun{
		"top ":                    topOut,
		"sysctl -n vm.loadavg":    "{ 1.52 1.60 1.63 }",
		"sysctl -n hw.memsize":    "17179869184", // 16 GiB
		"vm_stat":                 vmStatOut,
		"sysctl -n vm.swapusage":  "total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)",
		"sysctl -n kern.boottime": "{ sec = 1714550000, usec = 0 } Wed May  1 08:33:20 2024",
		"netstat -ib":             netstatOut,
		"ps -A -o pid=":           "    1\n  100\n  200\n",
		"launchctl list":          launchctlOut,
	}
	for k, v := range extra {
		cmds[k] = v
	}
	return New(Options{
		GOOS: "darwin",
		Now:  func() time.Time { return *now },
		Run:  cmds.run,
		DarwinMounts: func() ([]DarwinMount, error) {
			return []DarwinMount{
				{"/", "apfs"}, {"/System/Volumes/Data", "apfs"}, {"/System/Volumes/VM", "apfs"},
				{"/dev", "devfs"}, {"/Volumes/Backup", "apfs"}, {"/Volumes/Share", "smbfs"},
			}, nil
		},
		Statfs: func(path string) (Statfs, error) {
			return Statfs{Blocks: 1000, Bfree: 400, Bavail: 400, Files: 5, Ffree: 5}, nil
		},
	})
}

func TestDarwinFlow(t *testing.T) {
	now := time.Unix(1714553600, 0) // 1 hour after boot
	c := darwinCollector(&now, nil)
	r := c.Collect(context.Background(), Settings{DiskExcludeFstypes: []string{"smbfs"}, WatchServices: []string{"com.example.web", "com.example.crashed", "com.example.missing", "bad name"}})
	if len(r.Errs) != 0 {
		t.Fatalf("errors: %v", r.Errs)
	}
	if v, _ := byName(r, "cpu_percent", "", ""); math.Abs(v-8.12) > 0.01 {
		t.Fatalf("cpu %v", v)
	}
	if v, _ := byName(r, "load1", "", ""); v != 1.52 {
		t.Fatalf("load1 %v", v)
	}
	// avail = (10000+5000+50000+1000) pages * 16384 = 1,081,344,... ; total 16 GiB
	wantAvail := float64(66000 * 16384)
	if v, _ := byName(r, "mem_available_bytes", "", ""); v != wantAvail {
		t.Fatalf("avail %v want %v", v, wantAvail)
	}
	nearf(t, mustVal(t, r, "mem_used_percent"), 100*(17179869184-wantAvail)/17179869184, "mem_used_percent")
	nearf(t, mustVal(t, r, "swap_used_percent"), 25, "swap")
	nearf(t, mustVal(t, r, "uptime_seconds"), 3600, "uptime")
	if r.BootTime != 1714550000 {
		t.Fatalf("boot %d", r.BootTime)
	}
	nearf(t, mustVal(t, r, "proc_count"), 3, "proc_count")

	var mounts []string
	for _, m := range r.Metrics {
		if m.Name == "disk_used_percent" {
			mounts = append(mounts, m.Labels["mount"])
			nearf(t, m.Value, 60, "disk "+m.Labels["mount"])
		}
		if m.Name == "disk_inodes_used_percent" {
			t.Fatal("macOS must not emit inode metrics")
		}
	}
	if strings.Join(mounts, ",") != "/,/Volumes/Backup" {
		t.Fatalf("mounts %v (system volumes, devfs and excluded smbfs must be dropped)", mounts)
	}

	want := map[string]string{"com.example.web": "running", "com.example.crashed": "failed", "com.example.missing": "unknown", "bad name": "unknown"}
	for _, s := range r.Services {
		if want[s.Name] != s.State {
			t.Fatalf("service %s = %s", s.Name, s.State)
		}
	}
	if len(r.Services) != 4 {
		t.Fatalf("services %v", r.Services)
	}
}

func mustVal(t *testing.T, r Result, name string) float64 {
	t.Helper()
	v, ok := byName(r, name, "", "")
	if !ok {
		t.Fatalf("metric %s missing", name)
	}
	return v
}

func TestDarwinNetworkRatesNeedTwoPasses(t *testing.T) {
	now := time.Unix(1714553600, 0)
	c := darwinCollector(&now, nil)
	r1 := c.Collect(context.Background(), Settings{})
	if _, ok := byName(r1, "net_rx_bytes_per_s", "iface", "en0"); ok {
		t.Fatal("first pass has no baseline and must not emit rates")
	}
	now = now.Add(10 * time.Second)
	c.opt.Run = fakeRun{
		"top ": topOut, "sysctl -n vm.loadavg": "{ 1 1 1 }", "sysctl -n hw.memsize": "17179869184", "vm_stat": vmStatOut,
		"sysctl -n vm.swapusage": "total = 0.00M  used = 0.00M  free = 0.00M", "sysctl -n kern.boottime": "{ sec = 1714550000, usec = 0 }",
		"ps -A -o pid=": "1\n",
		"netstat -ib":   strings.Replace(netstatOut, "1000000     800     0     400000", "1100000     800     0     500000", 1),
	}.run
	r2 := c.Collect(context.Background(), Settings{})
	nearf(t, mustVal2(t, r2, "net_rx_bytes_per_s", "en0"), 10000, "rx")
	nearf(t, mustVal2(t, r2, "net_tx_bytes_per_s", "en0"), 10000, "tx")
	if _, ok := byName(r2, "swap_used_percent", "", ""); ok {
		t.Fatal("no swap configured -> no swap series")
	}
}

func mustVal2(t *testing.T, r Result, name, iface string) float64 {
	t.Helper()
	v, ok := byName(r, name, "iface", iface)
	if !ok {
		t.Fatalf("metric %s{%s} missing", name, iface)
	}
	return v
}

func TestDarwinCollectorFailuresAreNonFatalAndPerCollector(t *testing.T) {
	now := time.Unix(1714553600, 0)
	c := darwinCollector(&now, nil)
	c.opt.Run = fakeRun{"sysctl -n kern.boottime": "{ sec = 1714550000, usec = 0 }"}.run // everything else fails
	r := c.Collect(context.Background(), Settings{})
	if len(r.Errs) < 4 {
		t.Fatalf("each failed collector should report: %v", r.Errs)
	}
	if _, ok := byName(r, "uptime_seconds", "", ""); !ok {
		t.Fatal("working collectors must still report")
	}
}

// ── Windows ─────────────────────────────────────────────────────────────────────────────────────────────────

func winCollector(now *time.Time, times *[3]uint64) *Collector {
	return New(Options{
		GOOS: "windows",
		Now:  func() time.Time { return *now },
		Run:  fakeRun{"sc.exe query state= all": scOut}.run,
		Win: WinAPI{
			SystemTimes: func() (uint64, uint64, uint64, error) { return times[0], times[1], times[2], nil },
			Memory:      func() (uint64, uint64, error) { return 16 << 30, 4 << 30, nil },
			FixedDrives: func() ([]WinDrive, error) { return []WinDrive{{"C:", `C:\`}, {"D:", `D:\`}}, nil },
			DiskSpace: func(root string) (uint64, uint64, error) {
				if root == `D:\` {
					return 1000, 900, nil
				}
				return 1000, 250, nil
			},
			UptimeMillis: func() (uint64, error) { return 7200 * 1000, nil },
			ProcessCount: func() (int, error) { return 150, nil },
		},
	})
}

func TestWindowsFlow(t *testing.T) {
	now := time.Unix(1714553600, 0)
	times := [3]uint64{1000, 5000, 3000} // idle, kernel (includes idle), user
	c := winCollector(&now, &times)
	r := c.Collect(context.Background(), Settings{WatchServices: []string{"spooler", "W32Time", "BITS", "Nope"}})
	if len(r.Errs) != 0 {
		t.Fatalf("errors: %v", r.Errs)
	}
	if _, ok := byName(r, "cpu_percent", "", ""); ok {
		t.Fatal("first pass has no cpu baseline")
	}
	nearf(t, mustVal(t, r, "mem_used_percent"), 75, "mem_used_percent")
	nearf(t, mustVal(t, r, "mem_available_bytes"), float64(4<<30), "mem_available_bytes")
	if v, _ := byName(r, "disk_used_percent", "mount", "C:"); math.Abs(v-75) > 0.01 {
		t.Fatalf("C: %v", v)
	}
	if v, _ := byName(r, "disk_used_percent", "mount", "D:"); math.Abs(v-10) > 0.01 {
		t.Fatalf("D: %v", v)
	}
	nearf(t, mustVal(t, r, "proc_count"), 150, "proc_count")
	nearf(t, mustVal(t, r, "uptime_seconds"), 7200, "uptime")
	if r.BootTime != now.Unix()-7200 {
		t.Fatalf("boot %d", r.BootTime)
	}
	for _, absent := range []string{"load1", "swap_used_percent", "disk_inodes_used_percent", "net_rx_bytes_per_s", "disk_read_bytes_per_s"} {
		if _, ok := byName(r, absent, "", ""); ok {
			t.Fatalf("%s must not be faked on Windows", absent)
		}
	}
	want := map[string]string{"spooler": "running", "W32Time": "stopped", "BITS": "unknown", "Nope": "unknown"}
	for _, s := range r.Services {
		if want[s.Name] != s.State {
			t.Fatalf("service %s = %s (case-insensitive match, pending -> unknown)", s.Name, s.State)
		}
	}

	// second pass: dTotal = (9000+5000)-(5000+3000)=6000, dIdle = 1500 -> 75% busy
	times = [3]uint64{2500, 9000, 5000}
	now = now.Add(time.Minute)
	r2 := c.Collect(context.Background(), Settings{})
	nearf(t, mustVal(t, r2, "cpu_percent"), 75, "cpu_percent")

	// a counter that goes backwards (reboot / wrap) must not emit nonsense
	times = [3]uint64{10, 20, 10}
	r3 := c.Collect(context.Background(), Settings{})
	if _, ok := byName(r3, "cpu_percent", "", ""); ok {
		t.Fatal("non-monotonic times must be skipped")
	}
}

func TestWindowsErrorsAreReportedPerCollector(t *testing.T) {
	now := time.Unix(1714553600, 0)
	c := New(Options{GOOS: "windows", Now: func() time.Time { return now }, Win: WinAPI{
		Memory:      func() (uint64, uint64, error) { return 0, 0, errors.New("boom") },
		DiskSpace:   func(string) (uint64, uint64, error) { return 0, 0, errors.New("not ready") },
		FixedDrives: func() ([]WinDrive, error) { return []WinDrive{{"C:", `C:\`}}, nil },
	}})
	r := c.Collect(context.Background(), Settings{})
	if len(r.Errs) < 3 { // memory, disk (not ready), plus the unsupported default hooks on a non-Windows build host
		t.Fatalf("errors: %v", r.Errs)
	}
}

func TestParseScQuery(t *testing.T) {
	m := parseScQuery(scOut)
	if m["spooler"] != "running" || m["w32time"] != "stopped" || m["bits"] != "unknown" {
		t.Fatalf("%v", m)
	}
}

// ── mounts the hub ignores must never be sent (a stock Ubuntu host has /boot/efi) ────────────────────────────

func TestHubIgnoresMountMatchesTheHubRule(t *testing.T) {
	ignored := []string{"/boot/efi", "/boot/efi/extra", "/run", "/run/user/1000", "/running", "/dev", "/dev/shm", "/proc", "/sys/fs/cgroup",
		"/snap/core20/1", "/var/snap/lxd/common"}
	kept := []string{"/", "/boot", "/home", "/mnt/data", "/var/lib/docker", "/snapshots", "/Volumes/Backup", "C:", "D:"}
	for _, p := range ignored {
		if !hubIgnoresMount(p) {
			t.Errorf("%s must be ignored", p)
		}
	}
	for _, p := range kept {
		if hubIgnoresMount(p) {
			t.Errorf("%s must be kept", p)
		}
	}
}

func TestLinuxDoesNotSendBootEfiButKeepsBoot(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	h.write("self/mounts", "/dev/root / ext4 rw 0 0\n"+
		"/dev/nvme0n1p16 /boot ext4 rw 0 0\n"+
		"/dev/nvme0n1p15 /boot/efi vfat rw 0 0\n"+
		"/dev/loop1 /snap/core20/1 squashfs ro 0 0\n")
	now := time.Unix(1760000000, 0)
	c := New(Options{ProcRoot: h.proc, SysRoot: h.sys, Now: func() time.Time { return now }, Run: nil,
		Statfs: func(string) (Statfs, error) {
			return Statfs{Blocks: 1000, Bfree: 400, Bavail: 400, Files: 100, Ffree: 90}, nil
		}})
	r := c.Collect(context.Background(), Settings{}) // no fstype excludes at all: only the path rule can drop them
	var mounts []string
	for _, m := range r.Metrics {
		if m.Name == "disk_used_percent" {
			mounts = append(mounts, m.Labels["mount"])
		}
	}
	if len(mounts) != 2 || !(mounts[0] == "/" && mounts[1] == "/boot" || mounts[0] == "/boot" && mounts[1] == "/") {
		t.Fatalf("mounts sent: %v (want / and /boot only)", mounts)
	}
}
