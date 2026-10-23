//go:build !darwin

package collect

func darwinMountsSyscall() ([]DarwinMount, error) { return nil, errUnsupportedOS }
