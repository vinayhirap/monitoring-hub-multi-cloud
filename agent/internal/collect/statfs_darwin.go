//go:build darwin

package collect

import "syscall"

func statfsSyscall(path string) (Statfs, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return Statfs{}, err
	}
	return Statfs{
		Blocks: uint64(st.Blocks), Bfree: uint64(st.Bfree), Bavail: uint64(st.Bavail),
		Files: uint64(st.Files), Ffree: uint64(st.Ffree),
	}, nil
}
