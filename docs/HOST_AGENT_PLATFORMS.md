# Host agent: supported operating systems

One Go source tree, one static binary per OS/arch (stdlib only, no cgo, no external dependency).

| OS | Binary | Installer | Runs as | Service manager |
|---|---|---|---|---|
| Linux (systemd), amd64 / arm64 | `cloudops-agent-linux-<arch>` | `install.sh` | `cloudops-agent` (unprivileged, hardened unit) | systemd |
| macOS 12+ (Intel / Apple Silicon) | `cloudops-agent-darwin-<arch>` | `install-macos.sh` | `_cloudops-agent` (unprivileged) | launchd LaunchDaemon `com.cloudops.agent` |
| Windows Server 2016+ / Windows 10+, amd64 / arm64 | `cloudops-agent-windows-<arch>.exe` | `install.ps1` (elevated PowerShell) | `SYSTEM` | scheduled task `CloudOpsAgent` (at boot, restart every minute on failure) |

Not shipped: FreeBSD / other BSDs, AIX, Solaris, 32-bit anything, Linux without systemd (the binary runs; `install.sh` does not).

Build everything: `cd agent && make dist` (any host, Go 1.22+; cross-compiles, writes `dist/SHA256SUMS`).
`make vet-all` vets every shipped OS/arch.

## Metrics per OS

`yes` = collected, `-` = deliberately not collected (no honest equivalent; never sent as 0, never estimated).

| Contract metric | Linux | macOS | Windows | Source on macOS / Windows |
|---|---|---|---|---|
| `cpu_percent` | yes | yes | yes | macOS `top -l 2 -s 1` (second sample, ~1 s per pass); Windows `GetSystemTimes` delta |
| `load1/5/15` | yes | yes | - | macOS `sysctl vm.loadavg`; Windows has no load average |
| `mem_used_percent`, `mem_available_bytes` | yes | yes | yes | macOS `hw.memsize` + `vm_stat` (available = free + speculative + inactive + purgeable pages); Windows `GlobalMemoryStatusEx` |
| `swap_used_percent` | yes | yes | - | macOS `vm.swapusage`; Windows page-file semantics differ (commit charge), so it is omitted |
| `disk_used_percent{mount}` | yes | yes | yes | macOS `getfsstat` + `statfs`; Windows fixed drives only via `GetDiskFreeSpaceEx`, label `C:` `D:` ... |
| `disk_inodes_used_percent{mount}` | yes | - | - | APFS allocates inodes dynamically; NTFS has no inode limit |
| `disk_read/write_bytes_per_s` | yes | - | - | needs IOKit (cgo) / PDH counters |
| `net_rx/tx_bytes_per_s{iface}` | yes | yes | - | macOS `netstat -ib` (loopback, tunnels, awdl/llw skipped); Windows needs `GetIfTable2` / PDH |
| `proc_count` | yes | yes | yes | macOS `ps -A`; Windows `EnumProcesses` |
| `uptime_seconds`, boot time | yes | yes | yes | macOS `kern.boottime`; Windows `GetTickCount64` |
| `services[]` (`watch_services`) | systemctl unit | launchd label | service key name | one `systemctl is-active` / `launchctl list` / `sc.exe query state= all` call per pass |

Service states: Linux `running|stopped|failed|unknown`. macOS: PID present -> `running`; no PID and last exit status 0 ->
`stopped`; no PID and non-zero status -> `failed`; label not listed -> `unknown`. Windows: STATE code 4 -> `running`, 1 -> `stopped`,
anything else (start/stop pending, paused) and unlisted names -> `unknown`; Windows has no `failed` state. Service names are
matched case-insensitively on Windows and must match `[A-Za-z0-9:_.@\-]+` (a name with a space is reported `unknown`).
`services` are validated by the hub but **not persisted** on any OS (see HOST_AGENT_METRICS.md).

`disk_exclude_fstypes` (hub config) is honoured on Linux and macOS. On Windows only fixed drives are read, so it is ignored.

## How the hub treats the new OSes (no hub change needed)

* `host.os` is `linux` / `darwin` / `windows` (Go's `runtime.GOOS`); the hub stores it as text.
* Windows system drive `C:` is already a root path in the hub (`ROOT_PATHS = ("/", "C:")`), so it lands on `disk_used_percent`
  and the existing disk thresholds; `D:` becomes `disk_used_percent__d`. **If the Windows system drive is not C:** it is stored as
  a normal extra mount (`disk_used_percent__<letter>`), not as the root series.
* macOS `/` is the root series; `/Volumes/Backup` becomes `disk_used_percent__volumes_backup`.
* Default file locations: Linux and macOS `/etc/cloudops-agent/agent.json` and `/var/lib/cloudops-agent`; Windows
  `%ProgramData%\CloudOpsAgent\agent.json` and `%ProgramData%\CloudOpsAgent\data`.
* Logs: Linux journald; macOS and Windows `--log-file` (`.../agent.log`, 5 MiB cap, one `.1` backup).

## Behaviour that differs from Linux

* **Revoked host (hub answers 401, agent exits 3).** systemd honours `RestartPreventExitStatus=3` and stays down. launchd and Task
  Scheduler cannot exclude one exit code, so a revoked macOS / Windows host retries about once a minute (one cheap 401 each time)
  until the agent is uninstalled. Uninstall it, or revoke and uninstall together.
* **Windows is a scheduled task, not a Windows service.** A native service needs the Service Control Manager protocol, which cannot
  be done with the standard library alone, and the agent is deliberately dependency-free. Consequences: it is not listed in
  `services.msc`, it starts at boot (not before logon dependencies), and "stop" kills the process (the on-disk queue is crash-safe).
* **macOS CPU costs ~1 s per pass** (`top` takes two samples). With a 60 s interval that is under 2% of one core.
* **First pass emits no rates** (cpu on Windows, network on macOS), same as Linux.

## Verification status: read this before a fleet rollout

| What | Status |
|---|---|
| Collection flows (cpu/mem/disk/net/services arithmetic, label shapes, skip rules) | unit-tested for all three OSes on Linux CI with injected system calls and real command-output samples (`go test ./...`, mutation-checked) |
| Hub accepts Windows and macOS payloads | tested (`tests/test_host_agent_platforms.py`) |
| Binaries compile and `go vet` clean for all six targets | verified (`make vet-all`, `make dist`) |
| **Windows kernel32 wrappers** (`winapi_windows.go`) | compile-checked only; **never executed on Windows** |
| **macOS `getfsstat` wrapper, `top`/`vm_stat`/`netstat` real output on your macOS version** | compile-checked / sample-based only; **never executed on a Mac** |
| `install.ps1`, `uninstall.ps1` | parsed with the PowerShell 7 parser; **never executed** |
| `install-macos.sh`, `uninstall-macos.sh` | `bash -n` only; **never executed on macOS** |
| `launchctl list` as the unprivileged service account returning the system domain | **unverified**; if `watch_services` reports `unknown` on macOS, this is the first thing to check |

First rollout per OS: install on ONE host, run `cloudops-agent check` (prints the payload, sends nothing), compare with the OS's
own tools (Task Manager / Activity Monitor), then enrol.
