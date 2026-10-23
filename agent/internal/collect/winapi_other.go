//go:build !windows

package collect

func defaultWinAPI() WinAPI {
	return WinAPI{
		SystemTimes:  func() (uint64, uint64, uint64, error) { return 0, 0, 0, errUnsupportedOS },
		Memory:       func() (uint64, uint64, error) { return 0, 0, errUnsupportedOS },
		FixedDrives:  func() ([]WinDrive, error) { return nil, errUnsupportedOS },
		DiskSpace:    func(string) (uint64, uint64, error) { return 0, 0, errUnsupportedOS },
		UptimeMillis: func() (uint64, error) { return 0, errUnsupportedOS },
		ProcessCount: func() (int, error) { return 0, errUnsupportedOS },
	}
}
