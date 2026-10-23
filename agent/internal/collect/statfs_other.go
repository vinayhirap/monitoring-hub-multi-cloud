//go:build !linux

package collect

import "errors"

// The agent only runs on Linux; this stub keeps `go vet ./...` working on
// developer machines (Windows/macOS).
func statfsSyscall(string) (Statfs, error) { return Statfs{}, errors.New("statfs: linux only") }
