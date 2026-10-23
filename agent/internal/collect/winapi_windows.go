//go:build windows

package collect

import (
	"fmt"
	"syscall"
	"unsafe"
)

// Thin wrappers over kernel32 using only the standard library (no golang.org/x/sys). Keep them dumb: all arithmetic
// lives in winflow.go where it is unit-tested.
var (
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	procGetSystemTimes       = kernel32.NewProc("GetSystemTimes")
	procGlobalMemoryStatusEx = kernel32.NewProc("GlobalMemoryStatusEx")
	procGetLogicalDrives     = kernel32.NewProc("GetLogicalDrives")
	procGetDriveTypeW        = kernel32.NewProc("GetDriveTypeW")
	procGetDiskFreeSpaceExW  = kernel32.NewProc("GetDiskFreeSpaceExW")
	procGetTickCount64       = kernel32.NewProc("GetTickCount64")
	procEnumProcesses        = kernel32.NewProc("K32EnumProcesses") // exported by kernel32 since Windows 7 (psapi.dll forwards to it)
)

const driveFixed = 3

func defaultWinAPI() WinAPI {
	return WinAPI{
		SystemTimes:  winSystemTimes,
		Memory:       winMemory,
		FixedDrives:  winFixedDrives,
		DiskSpace:    winDiskSpace,
		UptimeMillis: winUptimeMillis,
		ProcessCount: winProcessCount,
	}
}

func ft(f syscall.Filetime) uint64 { return uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime) }

func winSystemTimes() (idle, kernel, user uint64, err error) {
	var i, k, u syscall.Filetime
	r, _, e := procGetSystemTimes.Call(uintptr(unsafe.Pointer(&i)), uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(&u)))
	if r == 0 {
		return 0, 0, 0, fmt.Errorf("GetSystemTimes: %w", e)
	}
	return ft(i), ft(k), ft(u), nil
}

type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

func winMemory() (total, avail uint64, err error) {
	m := memoryStatusEx{Length: uint32(unsafe.Sizeof(memoryStatusEx{}))}
	r, _, e := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&m)))
	if r == 0 {
		return 0, 0, fmt.Errorf("GlobalMemoryStatusEx: %w", e)
	}
	return m.TotalPhys, m.AvailPhys, nil
}

func winFixedDrives() ([]WinDrive, error) {
	mask, _, e := procGetLogicalDrives.Call()
	if mask == 0 {
		return nil, fmt.Errorf("GetLogicalDrives: %w", e)
	}
	var out []WinDrive
	for i := 0; i < 26; i++ {
		if mask&(1<<uint(i)) == 0 {
			continue
		}
		letter := string(rune('A' + i))
		root := letter + `:\`
		p, perr := syscall.UTF16PtrFromString(root)
		if perr != nil {
			continue
		}
		if t, _, _ := procGetDriveTypeW.Call(uintptr(unsafe.Pointer(p))); t != driveFixed {
			continue
		}
		out = append(out, WinDrive{Label: letter + ":", Root: root})
	}
	return out, nil
}

func winDiskSpace(root string) (total, free uint64, err error) {
	p, err := syscall.UTF16PtrFromString(root)
	if err != nil {
		return 0, 0, err
	}
	var availToCaller, tot, totFree uint64
	r, _, e := procGetDiskFreeSpaceExW.Call(uintptr(unsafe.Pointer(p)),
		uintptr(unsafe.Pointer(&availToCaller)), uintptr(unsafe.Pointer(&tot)), uintptr(unsafe.Pointer(&totFree)))
	if r == 0 {
		return 0, 0, fmt.Errorf("GetDiskFreeSpaceEx %s: %w", root, e)
	}
	return tot, totFree, nil
}

func winUptimeMillis() (uint64, error) {
	r, _, _ := procGetTickCount64.Call()
	return uint64(r), nil
}

func winProcessCount() (int, error) {
	size := 1024
	for size <= 1<<20 {
		pids := make([]uint32, size)
		var needed uint32
		r, _, e := procEnumProcesses.Call(uintptr(unsafe.Pointer(&pids[0])), uintptr(size*4), uintptr(unsafe.Pointer(&needed)))
		if r == 0 {
			return 0, fmt.Errorf("EnumProcesses: %w", e)
		}
		if int(needed) < size*4 { // buffer was big enough
			return int(needed) / 4, nil
		}
		size *= 2
	}
	return 0, fmt.Errorf("EnumProcesses: more than %d processes", 1<<20)
}
