package collect

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Filesystems that never describe real storage. These are skipped in
// addition to the hub-controlled disk_exclude_fstypes list; anything else
// that reports zero blocks is skipped too.
var pseudoFS = map[string]bool{
	"cgroup": true, "cgroup2": true, "devpts": true, "mqueue": true, "pstore": true,
	"bpf": true, "debugfs": true, "tracefs": true, "securityfs": true, "configfs": true,
	"fusectl": true, "autofs": true, "hugetlbfs": true, "binfmt_misc": true, "nsfs": true,
	"ramfs": true, "rpc_pipefs": true, "efivarfs": true, "iso9660": true, "udf": true,
	"selinuxfs": true, "fuse.gvfsd-fuse": true, "fuse.lxcfs": true,
}

// hubIgnoredMountPrefixes mirrors app/hostagents/metric_map.py _IGNORED_PATH_PREFIXES (itself a copy of
// app/collector/disk_mounts.py). The hub drops samples for these paths and counts them as "rejected"; if the agent
// sent them, every payload from a stock Ubuntu host (/boot/efi) would log a WARN once a minute forever. The match is
// the hub's exact rule, including its quirk that "/run" also matches "/running": the two sides must agree on every
// path, so tests/test_host_agent_platforms.py fails if this list and the hub's ever drift apart. "/" is never ignored.
var hubIgnoredMountPrefixes = []string{"/snap/", "/var/snap/", "/run", "/dev", "/proc", "/sys", "/boot/efi"}

func hubIgnoresMount(path string) bool {
	if path == "/" {
		return false
	}
	for _, p := range hubIgnoredMountPrefixes {
		if strings.HasPrefix(path, p) {
			return true
		}
	}
	return false
}

var statTimeout = 3 * time.Second // var so tests can shorten it

var errStatTimeout = errors.New("statfs timed out (hung mount?)")

type statRes struct {
	s   Statfs
	err error
}

// statfsTimeout bounds statfs(2) so a dead NFS mount cannot freeze the agent.
// A call that outlives the timeout stays registered; the next pass waits on the
// same goroutine instead of spawning another, so hung mounts cannot pile up.
func (c *Collector) statfsTimeout(path string) (Statfs, error) {
	ch, ok := c.pending[path]
	if !ok {
		ch = make(chan statRes, 1)
		c.pending[path] = ch
		go func() {
			s, err := c.opt.Statfs(path)
			ch <- statRes{s, err}
		}()
	}
	t := time.NewTimer(statTimeout)
	defer t.Stop()
	select {
	case r := <-ch:
		delete(c.pending, path)
		return r.s, r.err
	case <-t.C:
		return Statfs{}, errStatTimeout
	}
}

// disks emits disk_used_percent{mount} and disk_inodes_used_percent{mount}.
// used% follows df: used / (used + available-to-unprivileged).
func (c *Collector) disks(exclude []string, add addFn) error {
	txt, err := readFile(c.proc("self/mounts"))
	if err != nil {
		return err
	}
	ex := map[string]bool{}
	for _, t := range exclude {
		ex[t] = true
	}
	seenMnt := map[string]bool{}
	seenSrc := map[string]bool{}
	var errs []string
	for _, line := range strings.Split(txt, "\n") {
		f := fields(line)
		if len(f) < 3 {
			continue
		}
		src, mnt, fs := unescapeMount(f[0]), unescapeMount(f[1]), f[2]
		if ex[fs] || pseudoFS[fs] || seenMnt[mnt] || hubIgnoresMount(mnt) {
			continue
		}
		// A service sandbox (PrivateTmp, ProtectSystem, StateDirectory) adds bind
		// mounts of the root device to our mount namespace; they share one
		// filesystem, so report it once, under the first (shortest-listed) path.
		if src != "" && src != "none" && seenSrc[src] {
			continue
		}
		st, serr := c.statfsTimeout(mnt)
		if serr != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", mnt, serr))
			continue
		}
		if st.Blocks == 0 {
			continue
		}
		seenMnt[mnt] = true
		if src != "" && src != "none" {
			seenSrc[src] = true
		}
		labels := map[string]string{"mount": mnt}
		used := st.Blocks - minU(st.Bfree, st.Blocks)
		if denom := used + st.Bavail; denom > 0 {
			add("disk_used_percent", clamp(100*float64(used)/float64(denom), 0, 100), labels)
		}
		if st.Files > 0 {
			iused := st.Files - minU(st.Ffree, st.Files)
			add("disk_inodes_used_percent", clamp(100*float64(iused)/float64(st.Files), 0, 100), labels)
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("disk: %s", strings.Join(errs, "; "))
	}
	return nil
}

func minU(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}

// unescapeMount decodes the \040-style octal escapes used in /proc/mounts.
func unescapeMount(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if v, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(v))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

type diskIO struct{ rd, wr uint64 } // sectors (always 512 bytes in /proc/diskstats)

// Virtual / stacked block devices are excluded so I/O is not double counted
// (an LVM or md volume sits on top of devices that are already counted).
var skipDiskPrefix = []string{"loop", "ram", "zram", "dm-", "md", "nbd", "fd", "sr"}

func (c *Collector) wholeDisk(name string) bool {
	for _, p := range skipDiskPrefix {
		if strings.HasPrefix(name, p) {
			return false
		}
	}
	// /sys/block lists whole disks only; partitions live under their parent.
	_, err := os.Stat(filepath.Join(c.opt.SysRoot, "block", name))
	return err == nil
}

func (c *Collector) diskIO(now time.Time, add addFn) error {
	txt, err := readFile(c.proc("diskstats"))
	if err != nil {
		return err
	}
	cur := map[string]diskIO{}
	for _, line := range strings.Split(txt, "\n") {
		f := fields(line)
		if len(f) < 10 || !c.wholeDisk(f[2]) {
			continue
		}
		rd, e1 := strconv.ParseUint(f[5], 10, 64)
		wr, e2 := strconv.ParseUint(f[9], 10, 64)
		if e1 != nil || e2 != nil {
			continue
		}
		cur[f[2]] = diskIO{rd, wr}
	}
	prev, prevT := c.prevDio, c.prevDioT
	c.prevDio, c.prevDioT = cur, now
	if prev == nil {
		return nil // first sample: no baseline
	}
	el := now.Sub(prevT).Seconds()
	if el <= 0 {
		return nil
	}
	var dr, dw uint64
	matched := 0
	for name, cu := range cur {
		pv, ok := prev[name]
		if !ok || cu.rd < pv.rd || cu.wr < pv.wr { // new device or counter reset
			continue
		}
		dr += cu.rd - pv.rd
		dw += cu.wr - pv.wr
		matched++
	}
	if matched == 0 {
		return nil
	}
	add("disk_read_bytes_per_s", float64(dr)*512/el, nil)
	add("disk_write_bytes_per_s", float64(dw)*512/el, nil)
	return nil
}

type netIO struct{ rx, tx uint64 }

// network emits per-interface byte rates from /proc/net/dev (lo skipped).
func (c *Collector) network(now time.Time, add addFn) error {
	txt, err := readFile(c.proc("net/dev"))
	if err != nil {
		return err
	}
	cur := map[string]netIO{}
	for i, line := range strings.Split(txt, "\n") {
		if i < 2 { // two header lines
			continue
		}
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		name = strings.TrimSpace(name)
		if name == "" || name == "lo" {
			continue
		}
		f := fields(rest)
		if len(f) < 9 {
			continue
		}
		rx, e1 := strconv.ParseUint(f[0], 10, 64)
		tx, e2 := strconv.ParseUint(f[8], 10, 64)
		if e1 != nil || e2 != nil {
			continue
		}
		cur[name] = netIO{rx, tx}
	}
	prev, prevT := c.prevNet, c.prevNetT
	c.prevNet, c.prevNetT = cur, now
	if prev == nil {
		return nil
	}
	el := now.Sub(prevT).Seconds()
	if el <= 0 {
		return nil
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
	return nil
}
