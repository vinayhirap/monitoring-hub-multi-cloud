//go:build darwin

package collect

import "syscall"

const mntNoWait = 2 // MNT_NOWAIT: do not block on a hung filesystem

func cstr(b []int8) string {
	n := 0
	for n < len(b) && b[n] != 0 {
		n++
	}
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		out[i] = byte(b[i])
	}
	return string(out)
}

func darwinMountsSyscall() ([]DarwinMount, error) {
	n, err := syscall.Getfsstat(nil, mntNoWait)
	if err != nil {
		return nil, err
	}
	buf := make([]syscall.Statfs_t, n+8) // a little slack in case a mount appears between the two calls
	n, err = syscall.Getfsstat(buf, mntNoWait)
	if err != nil {
		return nil, err
	}
	out := make([]DarwinMount, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, DarwinMount{Path: cstr(buf[i].Mntonname[:]), Fstype: cstr(buf[i].Fstypename[:])})
	}
	return out, nil
}
