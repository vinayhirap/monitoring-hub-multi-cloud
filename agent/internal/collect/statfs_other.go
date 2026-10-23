//go:build !linux && !darwin

package collect

import "errors"

// Linux and macOS use statfs(2); Windows reads disk space through WinAPI.DiskSpace instead. This stub keeps the
// package compiling on every other GOOS.
func statfsSyscall(string) (Statfs, error) {
	return Statfs{}, errors.New("statfs: not supported on this OS")
}
